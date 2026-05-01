"""Tests for auto-remediation module."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.remediation import (
    ErrorCategory,
    RemediationResult,
    attempt_remediation,
    classify_error,
    _get_cooldown_minutes,
    _is_on_cooldown,
    _record_attempt,
)
from src.scraper import FetchError, ParseError, ParseResult, ReportPoint
from src.state import State


class TestClassifyError:
    def test_solver_unreachable(self):
        assert classify_error(FetchError("Solver unreachable at http://localhost:8191")) == ErrorCategory.SOLVER_UNREACHABLE

    def test_connection_refused(self):
        assert classify_error(FetchError("Connection refused")) == ErrorCategory.SOLVER_UNREACHABLE

    def test_cloudflare_block(self):
        assert classify_error(FetchError("Solver error: challenge failed")) == ErrorCategory.CLOUDFLARE_BLOCK

    def test_cloudflare_403(self):
        assert classify_error(FetchError("HTTP 403 Forbidden")) == ErrorCategory.CLOUDFLARE_BLOCK

    def test_rate_limited(self):
        assert classify_error(FetchError("HTTP 429 Too Many Requests")) == ErrorCategory.RATE_LIMITED

    def test_zenrows_credits(self):
        assert classify_error(FetchError("Credit limit exceeded")) == ErrorCategory.ZENROWS_CREDITS

    def test_target_down_502(self):
        assert classify_error(FetchError("HTTP 502 Bad Gateway")) == ErrorCategory.TARGET_DOWN

    def test_target_down_503(self):
        assert classify_error(FetchError("HTTP 503 Service Unavailable")) == ErrorCategory.TARGET_DOWN

    def test_network_error_connection(self):
        assert classify_error(requests.ConnectionError("DNS failed")) == ErrorCategory.NETWORK_ERROR

    def test_parse_failure(self):
        assert classify_error(ParseError("No parse strategy could extract")) == ErrorCategory.PARSE_FAILURE

    def test_unknown(self):
        assert classify_error(RuntimeError("something weird")) == ErrorCategory.UNKNOWN


class TestCooldown:
    def test_no_previous_attempt(self):
        state = State()
        assert _is_on_cooldown("zenrows_no_premium", state) is None

    def test_successful_attempt_no_cooldown(self):
        state = State()
        state.remediation_attempts = {
            "zenrows_no_premium": {
                "last_tried": datetime.now(timezone.utc).isoformat(),
                "last_result": "success",
                "fail_count": 0,
            }
        }
        assert _is_on_cooldown("zenrows_no_premium", state) is None

    def test_recent_failure_on_cooldown(self):
        state = State()
        state.remediation_attempts = {
            "direct_request": {
                "last_tried": datetime.now(timezone.utc).isoformat(),
                "last_result": "fail",
                "fail_count": 1,
            }
        }
        remaining = _is_on_cooldown("direct_request", state)
        assert remaining is not None
        assert remaining > 0

    def test_old_failure_off_cooldown(self):
        state = State()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=150)).isoformat()
        state.remediation_attempts = {
            "direct_request": {
                "last_tried": old_time,
                "last_result": "fail",
                "fail_count": 1,
            }
        }
        assert _is_on_cooldown("direct_request", state) is None

    def test_record_attempt_success_resets(self):
        state = State()
        state.remediation_attempts = {
            "direct_request": {
                "last_tried": "2024-01-01T00:00:00+00:00",
                "last_result": "fail",
                "fail_count": 3,
            }
        }
        _record_attempt("direct_request", True, state)
        entry = state.remediation_attempts["direct_request"]
        assert entry["last_result"] == "success"
        assert entry["fail_count"] == 0

    def test_record_attempt_failure_increments(self):
        state = State()
        _record_attempt("zenrows_no_premium", False, state)
        entry = state.remediation_attempts["zenrows_no_premium"]
        assert entry["last_result"] == "fail"
        assert entry["fail_count"] == 1

        _record_attempt("zenrows_no_premium", False, state)
        assert state.remediation_attempts["zenrows_no_premium"]["fail_count"] == 2


class TestProgressiveCooldown:
    """Test progressive cooldown: longer wait for repeated failures."""

    def test_cooldown_increases_with_fail_count(self):
        assert _get_cooldown_minutes(1) == 30
        assert _get_cooldown_minutes(2) == 60
        assert _get_cooldown_minutes(3) == 90
        assert _get_cooldown_minutes(4) == 120  # cap

    def test_cooldown_capped_at_max(self):
        assert _get_cooldown_minutes(10) == 120  # never exceeds cap

    def test_progressive_cooldown_applies_in_is_on_cooldown(self):
        """fail_count=2 → cooldown should be 60 min, not 30."""
        state = State()
        # Failed 15 minutes ago, fail_count=2 → cooldown=60min → 45min remaining
        recent = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        state.remediation_attempts = {
            "direct_request": {
                "last_tried": recent,
                "last_result": "fail",
                "fail_count": 2,
            }
        }
        remaining = _is_on_cooldown("direct_request", state)
        assert remaining is not None
        assert remaining > 30  # should be ~45min, not ~15min

    def test_first_failure_short_cooldown_expires(self):
        """fail_count=1 → cooldown=30min. After 35min, should be off cooldown."""
        state = State()
        old = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()
        state.remediation_attempts = {
            "direct_request": {
                "last_tried": old,
                "last_result": "fail",
                "fail_count": 1,
            }
        }
        assert _is_on_cooldown("direct_request", state) is None


class TestAttemptRemediation:
    def _make_parse_result(self):
        return ParseResult(
            points=[ReportPoint(timestamp=datetime.now(timezone.utc), value=5)],
            strategy="rsc",
        )

    @patch("src.remediation._strategy_direct_request")
    @patch("src.remediation._strategy_zenrows_alt_country")
    @patch("src.remediation._strategy_zenrows_premium_hu")
    @patch("src.remediation._strategy_zenrows_no_premium")
    @patch("src.remediation.parse_reports")
    def test_first_strategy_succeeds(self, mock_parse, mock_zr_np, mock_zr_prem, mock_zr_alt, mock_direct):
        mock_zr_np.return_value = "<html>ok</html>"
        mock_parse.return_value = self._make_parse_result()

        state = State(consecutive_fetch_failures=1)
        result = attempt_remediation("http://example.com", FetchError("challenge failed"), state)

        assert result.success is True
        assert result.strategy_used == "zenrows_no_premium"
        assert result.error_category == ErrorCategory.CLOUDFLARE_BLOCK
        assert len(result.attempts) == 1
        assert result.attempts[0].result == "SUCCESS"
        mock_zr_prem.assert_not_called()
        mock_zr_alt.assert_not_called()
        mock_direct.assert_not_called()

    @patch("src.remediation._strategy_direct_request")
    @patch("src.remediation._strategy_zenrows_alt_country")
    @patch("src.remediation._strategy_zenrows_premium_hu")
    @patch("src.remediation._strategy_zenrows_no_premium")
    @patch("src.remediation.parse_reports")
    def test_fallback_to_later_strategy(self, mock_parse, mock_zr_np, mock_zr_prem, mock_zr_alt, mock_direct):
        # zenrows_no_premium: fetch error
        # zenrows_premium_hu: fetch error
        # zenrows_alt_country: fetch OK, parse fails → FAILED
        # direct_request: fetch OK, parse OK → SUCCESS
        mock_zr_np.side_effect = FetchError("ZenRows failed")
        mock_zr_prem.side_effect = FetchError("ZenRows failed")
        mock_zr_alt.return_value = "<html>bad</html>"
        mock_direct.return_value = "<html>ok</html>"
        mock_parse.side_effect = [ParseError("bad html"), self._make_parse_result()]

        state = State(consecutive_fetch_failures=1)
        result = attempt_remediation("http://example.com", FetchError("challenge failed"), state)

        assert result.success is True
        assert result.strategy_used == "direct_request"
        assert len(result.attempts) == 4
        assert result.attempts[0].result == "FAILED"  # zenrows_no_premium
        assert result.attempts[1].result == "FAILED"  # zenrows_premium_hu
        assert result.attempts[2].result == "FAILED"  # zenrows_alt_country (parse failed)
        assert result.attempts[3].result == "SUCCESS"  # direct_request

    @patch("src.remediation._strategy_direct_request")
    @patch("src.remediation._strategy_zenrows_alt_country")
    @patch("src.remediation._strategy_zenrows_premium_hu")
    @patch("src.remediation._strategy_zenrows_no_premium")
    def test_all_strategies_fail(self, mock_zr_np, mock_zr_prem, mock_zr_alt, mock_direct):
        mock_zr_np.side_effect = FetchError("fail1")
        mock_zr_prem.side_effect = FetchError("fail2")
        mock_zr_alt.side_effect = FetchError("fail3")
        mock_direct.side_effect = FetchError("fail4")

        state = State(consecutive_fetch_failures=1)
        result = attempt_remediation("http://example.com", FetchError("challenge failed"), state)

        assert result.success is False
        assert result.error_category == ErrorCategory.CLOUDFLARE_BLOCK
        assert len(result.attempts) == 4
        assert all(a.result == "FAILED" for a in result.attempts)

    @patch("src.remediation._strategy_direct_request")
    @patch("src.remediation._strategy_zenrows_alt_country")
    @patch("src.remediation._strategy_zenrows_premium_hu")
    @patch("src.remediation._strategy_zenrows_no_premium")
    @patch("src.remediation.parse_reports")
    def test_cooldown_skips_strategy(self, mock_parse, mock_zr_np, mock_zr_prem, mock_zr_alt, mock_direct):
        # Put zenrows_no_premium on cooldown
        state = State(consecutive_fetch_failures=1)
        state.remediation_attempts = {
            "zenrows_no_premium": {
                "last_tried": datetime.now(timezone.utc).isoformat(),
                "last_result": "fail",
                "fail_count": 1,
            }
        }

        mock_zr_prem.return_value = "<html>ok</html>"
        mock_parse.return_value = self._make_parse_result()

        result = attempt_remediation("http://example.com", FetchError("challenge failed"), state)

        assert result.success is True
        assert result.strategy_used == "zenrows_premium_hu"
        # zenrows_no_premium should be skipped
        mock_zr_np.assert_not_called()
        skipped = [a for a in result.attempts if a.result == "SKIPPED"]
        assert len(skipped) == 1
        assert skipped[0].strategy == "zenrows_no_premium"

    @patch("src.remediation._strategy_direct_request")
    @patch("src.remediation._strategy_zenrows_alt_country")
    @patch("src.remediation._strategy_zenrows_premium_hu")
    @patch("src.remediation._strategy_zenrows_no_premium")
    @patch("src.remediation.parse_reports")
    def test_parse_failure_counts_as_failed(self, mock_parse, mock_zr_np, mock_zr_prem, mock_zr_alt, mock_direct):
        """If fetch succeeds but parse fails, the strategy counts as FAILED."""
        mock_zr_np.return_value = "<html>bad</html>"
        mock_parse.side_effect = ParseError("No parse strategy")

        mock_zr_prem.side_effect = FetchError("no key")
        mock_zr_alt.side_effect = FetchError("no key")
        mock_direct.side_effect = FetchError("blocked")

        state = State(consecutive_fetch_failures=1)
        result = attempt_remediation("http://example.com", FetchError("challenge"), state)

        assert result.success is False
        zr_np_attempt = next(a for a in result.attempts if a.strategy == "zenrows_no_premium")
        assert zr_np_attempt.result == "FAILED"

    @patch("src.remediation._strategy_direct_request")
    @patch("src.remediation._strategy_zenrows_alt_country")
    @patch("src.remediation._strategy_zenrows_premium_hu")
    @patch("src.remediation._strategy_zenrows_no_premium")
    def test_records_attempts_in_state(self, mock_zr_np, mock_zr_prem, mock_zr_alt, mock_direct):
        mock_zr_np.side_effect = FetchError("fail")
        mock_zr_prem.side_effect = FetchError("fail")
        mock_zr_alt.side_effect = FetchError("fail")
        mock_direct.side_effect = FetchError("fail")

        state = State(consecutive_fetch_failures=1)
        attempt_remediation("http://example.com", FetchError("challenge failed"), state)

        assert "zenrows_no_premium" in state.remediation_attempts
        assert state.remediation_attempts["zenrows_no_premium"]["last_result"] == "fail"
        assert "zenrows_premium_hu" in state.remediation_attempts
        assert state.remediation_attempts["zenrows_premium_hu"]["last_result"] == "fail"
        assert "direct_request" in state.remediation_attempts
        assert state.remediation_attempts["direct_request"]["last_result"] == "fail"

    @patch("src.remediation._strategy_direct_request")
    @patch("src.remediation._strategy_zenrows_alt_country")
    @patch("src.remediation._strategy_zenrows_premium_hu")
    @patch("src.remediation._strategy_zenrows_no_premium")
    @patch("src.remediation.parse_reports")
    def test_success_updates_remediation_last_success(self, mock_parse, mock_zr_np, mock_zr_prem, mock_zr_alt, mock_direct):
        mock_direct.return_value = "<html>ok</html>"
        mock_parse.return_value = self._make_parse_result()
        mock_zr_np.side_effect = FetchError("fail")
        mock_zr_prem.side_effect = FetchError("fail")
        mock_zr_alt.side_effect = FetchError("fail")

        state = State(consecutive_fetch_failures=1)
        result = attempt_remediation("http://example.com", FetchError("challenge failed"), state)

        assert result.success is True
        assert state.remediation_last_success == "direct_request"
