"""Tests for main module – alert state machine, heartbeat, daily summary, error alerting."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.main import (
    decide_action,
    run,
    _detect_retroactive_spike,
    _get_heartbeat_hour,
    _is_summary_hour,
    _reset_daily_stats_if_needed,
    _update_daily_stats,
    _get_chart_max_today,
)
from src.scraper import ParseResult, ReportPoint
from src.state import State, load, save

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")
FIXTURES = Path(__file__).parent / "fixtures"


def _make_points(value: int, ts: datetime | None = None) -> list[ReportPoint]:
    """Create a single-point list for simple test cases."""
    ts = ts or datetime(2026, 7, 15, 11, 0, 0, tzinfo=timezone.utc)
    return [ReportPoint(timestamp=ts, value=value)]


def _make_result(value: int, strategy: str = "rsc", ts: datetime | None = None) -> ParseResult:
    """Create a ParseResult for simple test cases."""
    return ParseResult(points=_make_points(value, ts), strategy=strategy)


class TestDecideAction:
    """Test the alert state machine – every transition must be covered."""

    @pytest.mark.parametrize(
        "prev_value,alert_active,curr,threshold,expected",
        [
            (10, False, 20, 30, "none"),       # below threshold, no change
            (10, False, 35, 30, "alert"),      # crossed above
            (35, True, 40, 30, "none"),        # already active, stays above
            (40, True, 25, 30, "recovery"),    # dropped below
            (0, False, 35, 30, "alert"),       # first run, above
            (10, False, 30, 30, "alert"),      # exactly at threshold (>=) → alert
            (10, False, 31, 30, "alert"),      # above threshold
            (35, True, 30, 30, "none"),        # exactly at threshold, alert active → stays active
            (35, True, 29, 30, "recovery"),    # dropped below threshold
        ],
    )
    def test_decide_action(self, prev_value, alert_active, curr, threshold, expected):
        state = State(last_value=prev_value, alert_active=alert_active)
        assert decide_action(state, curr, threshold) == expected


class TestDailyStats:
    """Test daily max tracking and reset logic."""

    def test_reset_on_new_day(self):
        state = State(daily_max_value=50, daily_max_time="14:30",
                      daily_max_date="2026-04-22", daily_alert_times=["14:20"],
                      daily_total_fetches=48, daily_failed_fetches=2)
        _reset_daily_stats_if_needed(state, "2026-04-23")
        assert state.daily_max_value == 0
        assert state.daily_max_time is None
        assert state.daily_max_date == "2026-04-23"
        assert state.daily_alert_times == []
        assert state.daily_total_fetches == 0
        assert state.daily_failed_fetches == 0

    def test_no_reset_same_day(self):
        state = State(daily_max_value=50, daily_max_time="14:30", daily_max_date="2026-04-23",
                      daily_total_fetches=10, daily_failed_fetches=1)
        _reset_daily_stats_if_needed(state, "2026-04-23")
        assert state.daily_max_value == 50
        assert state.daily_total_fetches == 10

    def test_update_max_when_higher(self):
        state = State(daily_max_value=10, daily_max_time="10:00")
        _update_daily_stats(state, 25, "14:30")
        assert state.daily_max_value == 25
        assert state.daily_max_time == "14:30"

    def test_no_update_when_lower(self):
        state = State(daily_max_value=30, daily_max_time="10:00")
        _update_daily_stats(state, 15, "14:30")
        assert state.daily_max_value == 30
        assert state.daily_max_time == "10:00"

    def test_update_on_equal(self):
        """Equal value should not update (only strictly greater)."""
        state = State(daily_max_value=10, daily_max_time="10:00")
        _update_daily_stats(state, 10, "14:30")
        assert state.daily_max_value == 10
        assert state.daily_max_time == "10:00"


class TestGetChartMaxToday:
    """Test chart max extraction from RSC data points."""

    def test_finds_max_from_today(self):
        budapest_now = datetime(2026, 7, 15, 13, 0, tzinfo=BUDAPEST_TZ)
        points = [
            ReportPoint(datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc), 2),   # 9:00 Budapest
            ReportPoint(datetime(2026, 7, 15, 7, 41, tzinfo=timezone.utc), 5),  # 9:41 Budapest
            ReportPoint(datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc), 1),   # 10:00 Budapest
            ReportPoint(datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc), 0),  # 13:00 Budapest
        ]
        max_val, max_time = _get_chart_max_today(points, budapest_now)
        assert max_val == 5
        assert max_time == "09:41"

    def test_ignores_yesterday_points(self):
        budapest_now = datetime(2026, 7, 15, 13, 0, tzinfo=BUDAPEST_TZ)
        points = [
            ReportPoint(datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc), 50),  # Yesterday
            ReportPoint(datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc), 3),   # Today
        ]
        max_val, max_time = _get_chart_max_today(points, budapest_now)
        assert max_val == 3

    def test_returns_zero_when_no_today_points(self):
        budapest_now = datetime(2026, 7, 15, 1, 0, tzinfo=BUDAPEST_TZ)
        points = [
            ReportPoint(datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc), 50),
        ]
        max_val, max_time = _get_chart_max_today(points, budapest_now)
        assert max_val == 0
        assert max_time is None

    def test_handles_naive_timestamps(self):
        """Timestamps without tzinfo should be treated as UTC."""
        budapest_now = datetime(2026, 7, 15, 13, 0, tzinfo=BUDAPEST_TZ)
        points = [
            ReportPoint(datetime(2026, 7, 15, 7, 30), 8),  # naive, assumed UTC -> 9:30 Budapest
        ]
        max_val, max_time = _get_chart_max_today(points, budapest_now)
        assert max_val == 8
        assert max_time == "09:30"


class TestGetHeartbeatHour:
    """Test multi-hour heartbeat scheduling."""

    def test_morning_window(self):
        """9:15 Budapest -> returns 9."""
        state = State(heartbeat_sent={})
        budapest_now = datetime(2026, 7, 15, 9, 15, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _get_heartbeat_hour(state, budapest_now) == 9

    def test_evening_window(self):
        """19:10 Budapest, 9 already sent -> returns 19."""
        state = State(heartbeat_sent={"9": "2026-07-15"})
        budapest_now = datetime(2026, 7, 15, 19, 10, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _get_heartbeat_hour(state, budapest_now) == 19

    def test_already_sent_this_hour_today(self):
        """9:15 but already sent at 9 today -> None."""
        state = State(heartbeat_sent={"9": "2026-07-15"})
        budapest_now = datetime(2026, 7, 15, 9, 15, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _get_heartbeat_hour(state, budapest_now) is None

    def test_sent_yesterday_not_today(self):
        """9:15, sent yesterday -> should send."""
        state = State(heartbeat_sent={"9": "2026-07-14"})
        budapest_now = datetime(2026, 7, 15, 9, 15, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _get_heartbeat_hour(state, budapest_now) == 9

    def test_before_first_hour(self):
        """8:00 Budapest -> None (before any configured hour)."""
        state = State(heartbeat_sent={})
        budapest_now = datetime(2026, 7, 15, 8, 0, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _get_heartbeat_hour(state, budapest_now) is None

    def test_catchup_after_missed_window(self):
        """10:00 Budapest, 9 not yet sent -> returns 9 (catchup)."""
        state = State(heartbeat_sent={})
        budapest_now = datetime(2026, 7, 15, 10, 0, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _get_heartbeat_hour(state, budapest_now) == 9

    def test_no_catchup_when_already_sent(self):
        """10:00 Budapest, 9 already sent -> None (between windows)."""
        state = State(heartbeat_sent={"9": "2026-07-15"})
        budapest_now = datetime(2026, 7, 15, 10, 0, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _get_heartbeat_hour(state, budapest_now) is None

    def test_disabled(self):
        state = State(heartbeat_sent={})
        budapest_now = datetime(2026, 7, 15, 9, 15, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = False
            assert _get_heartbeat_hour(state, budapest_now) is None


class TestIsSummaryHour:
    """Last hour in HEARTBEAT_HOURS gets the summary format."""

    def test_last_hour_is_summary(self):
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _is_summary_hour(19) is True

    def test_first_hour_is_not_summary(self):
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _is_summary_hour(9) is False

    def test_single_hour_is_summary(self):
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_HOURS = [19]
            assert _is_summary_hour(19) is True


class TestRun:
    @patch("src.main.send_alert", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_run_triggers_alert(self, mock_html, mock_parse, mock_alert, tmp_path):
        mock_parse.return_value = _make_result(45)
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            result = run(str(state_path))

        assert result == 0
        mock_alert.assert_called_once_with(45, 30)

    @patch("src.main.send_recovery", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_run_triggers_recovery(self, mock_html, mock_parse, mock_recovery, tmp_path):
        mock_parse.return_value = _make_result(15)
        state_path = tmp_path / "state.json"
        save(State(last_value=40, alert_active=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            result = run(str(state_path))

        assert result == 0
        mock_recovery.assert_called_once_with(15, 30)

    @patch("src.main.send_alert")
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_run_no_notification_below_threshold(self, mock_html, mock_parse, mock_alert, tmp_path):
        mock_parse.return_value = _make_result(10)
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            result = run(str(state_path))

        assert result == 0
        mock_alert.assert_not_called()

    @patch("src.main.send_remediation_report", return_value=True)
    @patch("src.main.attempt_remediation")
    @patch("src.main.fetch_html", side_effect=Exception("Network error"))
    def test_run_handles_fetch_failure(self, mock_html, mock_remediation, mock_rem_report, tmp_path):
        from src.remediation import ErrorCategory, RemediationResult
        mock_remediation.return_value = RemediationResult(
            success=False, error_category=ErrorCategory.UNKNOWN, attempts=[], duration_s=0.0,
        )
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.NOTIFICATION_DELAY_MINUTES = 30
            mock_config.NOTIFICATION_MIN_FAILURES = 2
            mock_config.JITTER_MAX_SECONDS = 0

            result = run(str(state_path))

        assert result == 0
        # First failure: no notification (elapsed=0, failures=1)
        mock_rem_report.assert_not_called()

    def test_run_returns_1_on_config_error(self, tmp_path):
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = ["TELEGRAM_BOT_TOKEN is not set"]
            mock_config.JITTER_MAX_SECONDS = 0

            result = run(str(tmp_path / "state.json"))

        assert result == 1

    @patch("src.main.send_alert", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_run_tracks_daily_max_and_alert_time(self, mock_html, mock_parse, mock_alert, tmp_path):
        """Alert should update daily_max and record alert time."""
        mock_parse.return_value = _make_result(45)
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        loaded = load(state_path)
        assert loaded.daily_max_value == 45
        assert loaded.daily_max_time is not None
        assert len(loaded.daily_alert_times) == 1

    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_run_uses_chart_max_not_current(self, mock_html, mock_parse, tmp_path):
        """Daily max should come from chart data, not just the current value."""
        # Chart has a spike at 9:41 (value=15), current value is 2
        mock_parse.return_value = ParseResult(
            points=[
                ReportPoint(datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc), 1),
                ReportPoint(datetime(2026, 7, 15, 7, 41, tzinfo=timezone.utc), 15),
                ReportPoint(datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc), 3),
                ReportPoint(datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc), 2),
            ],
            strategy="rsc",
        )
        state_path = tmp_path / "state.json"
        fake_now = datetime(2026, 7, 15, 11, 0, 0, tzinfo=timezone.utc)  # 13:00 Budapest

        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        loaded = load(state_path)
        assert loaded.daily_max_value == 15
        assert loaded.daily_max_time == "09:41"
        assert loaded.last_value == 2  # current value is the last point

    @patch("src.main.time.sleep")
    @patch("src.main.random.uniform", return_value=42.5)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_jitter_delay_applied(self, mock_html, mock_parse, mock_uniform, mock_sleep, tmp_path):
        """Jitter delay is applied before fetching."""
        mock_parse.return_value = _make_result(5)
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 90

            run(str(state_path))

        mock_uniform.assert_called_once_with(0, 90)
        mock_sleep.assert_called_once_with(42.5)


class TestErrorAlerting:
    """Test the 3x consecutive failure -> 1x alert, then silence logic."""

    @patch("src.main.send_remediation_report", return_value=True)
    @patch("src.main.attempt_remediation")
    @patch("src.main.fetch_html", side_effect=Exception("FlareSolverr down"))
    def test_notification_sent_after_delay_and_min_failures(self, mock_html, mock_remediation, mock_rem_report, tmp_path):
        """Notification fires when elapsed >= 30min AND failures >= 2."""
        from src.remediation import ErrorCategory, RemediationResult
        mock_remediation.return_value = RemediationResult(
            success=False, error_category=ErrorCategory.SOLVER_UNREACHABLE, attempts=[], duration_s=0.0,
        )
        state_path = tmp_path / "state.json"
        # 1 previous failure, 35 minutes ago
        first_fail = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
        save(State(consecutive_fetch_failures=1, error_alert_sent=False,
                   first_failure_at=first_fail), state_path)

        fake_now = datetime(2026, 7, 15, 10, 35, 0, tzinfo=timezone.utc)
        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.NOTIFICATION_DELAY_MINUTES = 30
            mock_config.NOTIFICATION_MIN_FAILURES = 2
            mock_config.JITTER_MAX_SECONDS = 0
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        mock_rem_report.assert_called_once()
        loaded = load(state_path)
        assert loaded.error_alert_sent is True

    @patch("src.main.send_remediation_report", return_value=True)
    @patch("src.main.attempt_remediation")
    @patch("src.main.fetch_html", side_effect=Exception("Still broken"))
    def test_no_spam_after_alert_sent(self, mock_html, mock_remediation, mock_rem_report, tmp_path):
        from src.remediation import ErrorCategory, RemediationResult
        mock_remediation.return_value = RemediationResult(
            success=False, error_category=ErrorCategory.UNKNOWN, attempts=[], duration_s=0.0,
        )
        state_path = tmp_path / "state.json"
        first_fail = datetime(2026, 7, 15, 9, 0, 0, tzinfo=timezone.utc)
        # Use 7 (not 5) so after +1 it becomes 8, which is NOT an escalation point (6,12,24)
        save(State(consecutive_fetch_failures=7, error_alert_sent=True,
                   first_failure_at=first_fail), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.NOTIFICATION_DELAY_MINUTES = 30
            mock_config.NOTIFICATION_MIN_FAILURES = 2
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        # Already notified + not an escalation point → no new report
        mock_rem_report.assert_not_called()

    @patch("src.main.send_fetch_recovery", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_success_resets_failure_state(self, mock_html, mock_parse, mock_recovery, tmp_path):
        mock_parse.return_value = _make_result(5)
        state_path = tmp_path / "state.json"
        save(State(consecutive_fetch_failures=4, error_alert_sent=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        loaded = load(state_path)
        assert loaded.consecutive_fetch_failures == 0
        assert loaded.error_alert_sent is False
        mock_recovery.assert_called_once_with(previous_failures=4, current_value=5, strategy="rsc")

    @patch("src.main.send_fetch_recovery", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_no_recovery_when_no_prior_error_alert(self, mock_html, mock_parse, mock_recovery, tmp_path):
        """If error_alert_sent was False, no recovery notification is sent."""
        mock_parse.return_value = _make_result(5)
        state_path = tmp_path / "state.json"
        save(State(consecutive_fetch_failures=1, error_alert_sent=False), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        mock_recovery.assert_not_called()


class TestFetchStats:
    """Test total_fetches and failed_fetches counters."""

    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_success_increments_total(self, mock_html, mock_parse, tmp_path):
        mock_parse.return_value = _make_result(5)
        state_path = tmp_path / "state.json"
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Budapest")).strftime("%Y-%m-%d")
        save(State(total_fetches=10, failed_fetches=2,
                   daily_total_fetches=5, daily_failed_fetches=1,
                   daily_max_date=today), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        loaded = load(state_path)
        assert loaded.total_fetches == 11
        assert loaded.failed_fetches == 2  # unchanged
        assert loaded.daily_total_fetches == 6
        assert loaded.daily_failed_fetches == 1  # unchanged

    @patch("src.main.send_remediation_report")
    @patch("src.main.attempt_remediation")
    @patch("src.main.fetch_html", side_effect=Exception("down"))
    def test_failure_increments_both(self, mock_html, mock_remediation, mock_rem_report, tmp_path):
        from src.remediation import ErrorCategory, RemediationResult
        mock_remediation.return_value = RemediationResult(
            success=False, error_category=ErrorCategory.UNKNOWN, attempts=[], duration_s=0.0,
        )
        state_path = tmp_path / "state.json"
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Budapest")).strftime("%Y-%m-%d")
        save(State(total_fetches=10, failed_fetches=2,
                   daily_total_fetches=5, daily_failed_fetches=1,
                   daily_max_date=today), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.NOTIFICATION_DELAY_MINUTES = 30
            mock_config.NOTIFICATION_MIN_FAILURES = 99
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        loaded = load(state_path)
        assert loaded.total_fetches == 11
        assert loaded.failed_fetches == 3
        assert loaded.daily_total_fetches == 6
        assert loaded.daily_failed_fetches == 2


class TestHeartbeatIntegration:
    """Test heartbeat and daily summary within the full run flow."""

    @patch("src.main.send_heartbeat", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_morning_heartbeat_sent(self, mock_html, mock_parse, mock_hb, tmp_path):
        mock_parse.return_value = _make_result(3)
        state_path = tmp_path / "state.json"
        fake_now = datetime(2026, 7, 15, 7, 15, 0, tzinfo=timezone.utc)  # 9:15 Budapest

        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        mock_hb.assert_called_once()

    @patch("src.main.send_daily_summary", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_evening_summary_sent(self, mock_html, mock_parse, mock_summary, tmp_path):
        mock_parse.return_value = _make_result(3)
        state_path = tmp_path / "state.json"
        save(State(daily_max_value=42, daily_max_time="14:30",
                   daily_max_date="2026-07-15", daily_alert_times=["14:20"],
                   heartbeat_sent={"9": "2026-07-15"}),
             state_path)
        fake_now = datetime(2026, 7, 15, 17, 10, 0, tzinfo=timezone.utc)  # 19:10 Budapest

        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.PAT_EXPIRY_DATE = ""
            mock_config.JITTER_MAX_SECONDS = 0
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        mock_summary.assert_called_once()
        call_kwargs = mock_summary.call_args
        assert call_kwargs[1]["daily_max"] == 42
        assert call_kwargs[1]["daily_max_time"] == "14:30"
        assert call_kwargs[1]["alert_times"] == ["14:20"]

    @patch("src.main.send_heartbeat")
    @patch("src.main.send_daily_summary")
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_no_heartbeat_when_all_sent(self, mock_html, mock_parse, mock_summary, mock_hb, tmp_path):
        mock_parse.return_value = _make_result(3)
        state_path = tmp_path / "state.json"
        save(State(heartbeat_sent={"9": "2026-07-15"}), state_path)
        fake_now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)  # 14:00 Budapest

        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        mock_hb.assert_not_called()
        mock_summary.assert_not_called()


class TestDegradationDetection:
    """Test that non-RSC parse triggers degradation alert."""

    @patch("src.main.send_parse_degradation_alert", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>degraded</html>")
    def test_degradation_alert_on_fallback(self, mock_html, mock_parse, mock_degrad, tmp_path):
        mock_parse.return_value = _make_result(5, strategy="json_anywhere")
        state_path = tmp_path / "state.json"

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        mock_degrad.assert_called_once_with("json_anywhere", 5)
        loaded = load(state_path)
        assert loaded.degraded_parse_alert_sent is True

    @patch("src.main.send_parse_degradation_alert")
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_no_spam_when_already_alerted(self, mock_html, mock_parse, mock_degrad, tmp_path):
        mock_parse.return_value = _make_result(5, strategy="aria_label")
        state_path = tmp_path / "state.json"
        save(State(degraded_parse_alert_sent=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        mock_degrad.assert_not_called()

    @patch("src.main.send_parse_degradation_alert")
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_rsc_resets_degradation_flag(self, mock_html, mock_parse, mock_degrad, tmp_path):
        mock_parse.return_value = _make_result(5, strategy="rsc")
        state_path = tmp_path / "state.json"
        save(State(degraded_parse_alert_sent=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0

            run(str(state_path))

        mock_degrad.assert_not_called()
        loaded = load(state_path)
        assert loaded.degraded_parse_alert_sent is False


class TestDetectRetroactiveSpike:
    """Test retroactive spike detection from chart data points."""

    def test_spike_after_last_checked(self):
        """Spike after last_checked should be detected."""
        last_checked = datetime(2026, 6, 8, 10, 0, 0, tzinfo=timezone.utc)
        points = [
            ReportPoint(datetime(2026, 6, 8, 10, 10, tzinfo=timezone.utc), 6),
            ReportPoint(datetime(2026, 6, 8, 10, 25, tzinfo=timezone.utc), 4),
            ReportPoint(datetime(2026, 6, 8, 10, 40, tzinfo=timezone.utc), 13),  # spike
            ReportPoint(datetime(2026, 6, 8, 10, 55, tzinfo=timezone.utc), 6),
            ReportPoint(datetime(2026, 6, 8, 11, 10, tzinfo=timezone.utc), 7),  # current
        ]
        result = _detect_retroactive_spike(points, last_checked, 10, False, 7)
        assert result is not None
        assert result[0] == 13
        assert result[1] == "12:40"  # Budapest = UTC+2

    def test_all_below_threshold(self):
        """No spike when all points are below threshold."""
        last_checked = datetime(2026, 6, 8, 11, 0, 0, tzinfo=timezone.utc)
        points = [
            ReportPoint(datetime(2026, 6, 8, 11, 10, tzinfo=timezone.utc), 3),
            ReportPoint(datetime(2026, 6, 8, 11, 25, tzinfo=timezone.utc), 5),
            ReportPoint(datetime(2026, 6, 8, 11, 40, tzinfo=timezone.utc), 7),
        ]
        result = _detect_retroactive_spike(points, last_checked, 10, False, 7)
        assert result is None

    def test_alert_active_skips(self):
        """When alert is active, normal cycle handles it."""
        last_checked = datetime(2026, 6, 8, 11, 0, 0, tzinfo=timezone.utc)
        points = [
            ReportPoint(datetime(2026, 6, 8, 11, 10, tzinfo=timezone.utc), 15),
        ]
        result = _detect_retroactive_spike(points, last_checked, 10, True, 7)
        assert result is None

    def test_current_above_threshold_skips(self):
        """When current value >= threshold, normal alert handles it."""
        last_checked = datetime(2026, 6, 8, 11, 0, 0, tzinfo=timezone.utc)
        points = [
            ReportPoint(datetime(2026, 6, 8, 11, 10, tzinfo=timezone.utc), 15),
        ]
        result = _detect_retroactive_spike(points, last_checked, 10, False, 12)
        assert result is None

    def test_spike_before_last_checked_ignored(self):
        """Points at or before last_checked are already seen."""
        last_checked = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
        points = [
            ReportPoint(datetime(2026, 6, 8, 11, 30, tzinfo=timezone.utc), 15),  # before
            ReportPoint(datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc), 12),  # at last_checked
            ReportPoint(datetime(2026, 6, 8, 12, 15, tzinfo=timezone.utc), 5),  # after, but below
        ]
        result = _detect_retroactive_spike(points, last_checked, 10, False, 5)
        assert result is None

    def test_last_checked_none_checks_all(self):
        """First run (last_checked=None) should consider all points."""
        points = [
            ReportPoint(datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc), 3),
            ReportPoint(datetime(2026, 6, 8, 10, 15, tzinfo=timezone.utc), 14),  # spike
            ReportPoint(datetime(2026, 6, 8, 10, 30, tzinfo=timezone.utc), 5),
        ]
        result = _detect_retroactive_spike(points, None, 10, False, 5)
        assert result is not None
        assert result[0] == 14

    def test_multiple_spikes_returns_highest(self):
        """When multiple points cross threshold, return the highest."""
        last_checked = datetime(2026, 6, 8, 11, 0, 0, tzinfo=timezone.utc)
        points = [
            ReportPoint(datetime(2026, 6, 8, 11, 10, tzinfo=timezone.utc), 12),
            ReportPoint(datetime(2026, 6, 8, 11, 25, tzinfo=timezone.utc), 18),  # highest
            ReportPoint(datetime(2026, 6, 8, 11, 40, tzinfo=timezone.utc), 11),
            ReportPoint(datetime(2026, 6, 8, 11, 55, tzinfo=timezone.utc), 7),
        ]
        result = _detect_retroactive_spike(points, last_checked, 10, False, 7)
        assert result is not None
        assert result[0] == 18
        assert result[1] == "13:25"  # Budapest = UTC+2


class TestRetroactiveAlertIntegration:
    """Integration tests for retroactive spike detection within the full run flow."""

    @patch("src.main.send_retroactive_alert", return_value=True)
    @patch("src.main.send_alert")
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_chart_spike_current_below_triggers_retroactive(
        self, mock_html, mock_parse, mock_alert, mock_retro, tmp_path,
    ):
        """Spike in chart + current below threshold → retroactive alert, alert_active stays False."""
        # Chart: spike at 12:40 UTC (14:40 Budapest), current=7
        mock_parse.return_value = ParseResult(
            points=[
                ReportPoint(datetime(2026, 6, 8, 10, 10, tzinfo=timezone.utc), 6),
                ReportPoint(datetime(2026, 6, 8, 10, 25, tzinfo=timezone.utc), 4),
                ReportPoint(datetime(2026, 6, 8, 10, 40, tzinfo=timezone.utc), 13),
                ReportPoint(datetime(2026, 6, 8, 10, 55, tzinfo=timezone.utc), 6),
                ReportPoint(datetime(2026, 6, 8, 11, 10, tzinfo=timezone.utc), 7),
            ],
            strategy="rsc",
        )
        state_path = tmp_path / "state.json"
        # last_checked before the spike
        save(State(last_checked=datetime(2026, 6, 8, 10, 0, 0, tzinfo=timezone.utc)), state_path)

        fake_now = datetime(2026, 6, 8, 11, 30, 0, tzinfo=timezone.utc)
        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = run(str(state_path))

        assert result == 0
        mock_retro.assert_called_once_with(13, "12:40", 7, 10)
        mock_alert.assert_not_called()
        loaded = load(state_path)
        assert loaded.alert_active is False
        assert any("*" in t for t in loaded.daily_alert_times)

    @patch("src.main.send_retroactive_alert")
    @patch("src.main.send_alert", return_value=True)
    @patch("src.main.parse_reports")
    @patch("src.main.fetch_html", return_value="<html>ok</html>")
    def test_current_above_threshold_normal_alert_not_retroactive(
        self, mock_html, mock_parse, mock_alert, mock_retro, tmp_path,
    ):
        """Current value above threshold → normal alert, no retroactive."""
        mock_parse.return_value = ParseResult(
            points=[
                ReportPoint(datetime(2026, 6, 8, 10, 10, tzinfo=timezone.utc), 6),
                ReportPoint(datetime(2026, 6, 8, 10, 25, tzinfo=timezone.utc), 15),
                ReportPoint(datetime(2026, 6, 8, 10, 40, tzinfo=timezone.utc), 12),
            ],
            strategy="rsc",
        )
        state_path = tmp_path / "state.json"

        fake_now = datetime(2026, 6, 8, 11, 0, 0, tzinfo=timezone.utc)
        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_config.JITTER_MAX_SECONDS = 0
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = run(str(state_path))

        assert result == 0
        mock_alert.assert_called_once_with(12, 10)
        mock_retro.assert_not_called()
