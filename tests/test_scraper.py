"""Tests for scraper module – parse strategies and fetch logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import scraper
from src.scraper import FetchError, ParseError, ParseResult, ReportPoint, parse_reports, fetch_html, get_current_value

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseReports:
    def test_parse_rsc_data_points(self):
        """RSC strategy extracts precise per-interval values."""
        html = (FIXTURES / "normal_response.html").read_text()
        result = parse_reports(html)
        assert isinstance(result, ParseResult)
        assert result.strategy == "rsc"
        assert len(result.points) == 5
        assert all(isinstance(r, ReportPoint) for r in result.points)
        # Should be sorted by timestamp
        timestamps = [r.timestamp for r in result.points]
        assert timestamps == sorted(timestamps)
        # Last value should be 2 (from fixture)
        assert result.points[-1].value == 2

    def test_parse_high_alert_rsc(self):
        html = (FIXTURES / "high_alert_response.html").read_text()
        result = parse_reports(html)
        assert result.strategy == "rsc"
        assert len(result.points) == 5
        assert result.points[-1].value == 152

    def test_parse_no_problems_heading_only(self):
        html = (FIXTURES / "no_problems_heading_only.html").read_text()
        result = parse_reports(html)
        assert result.strategy == "heading"
        assert len(result.points) == 1
        assert result.points[0].value == 0

    def test_parse_empty_response_raises_parse_error(self):
        html = (FIXTURES / "empty_response.html").read_text()
        with pytest.raises(ParseError):
            parse_reports(html)

    def test_parse_cloudflare_challenge_raises_parse_error(self):
        html = (FIXTURES / "cloudflare_challenge.html").read_text()
        with pytest.raises(ParseError):
            parse_reports(html)

    def test_parse_garbage_html_raises_parse_error(self):
        with pytest.raises(ParseError):
            parse_reports("<html><body>nothing useful here</body></html>")

    def test_rsc_strategy_takes_priority_over_aria_label(self):
        """RSC data should be preferred over aria-label (24h peak)."""
        html = (FIXTURES / "normal_response.html").read_text()
        result = parse_reports(html)
        assert result.strategy == "rsc"
        # RSC says last value is 2, aria-label says peak is 5
        assert result.points[-1].value == 2

    def test_aria_label_fallback_when_no_rsc(self):
        """Falls back to aria-label when RSC data missing."""
        html = """
        <html><body>
        <div aria-label="Reports chart for the last 24 hours with a peak of 42 reports, status: ok"></div>
        </body></html>
        """
        result = parse_reports(html)
        assert result.strategy == "aria_label"
        assert len(result.points) == 1
        assert result.points[0].value == 42

    def test_json_anywhere_fallback(self):
        """JSON anywhere strategy finds data outside RSC push payloads."""
        html = """
        <html><body><script>
        var data = [{"timestampUtc": "2026-04-26T10:00:00+00:00", "reportsValue": 7},
                     {"timestampUtc": "2026-04-26T10:15:00+00:00", "reportsValue": 3}];
        </script></body></html>
        """
        result = parse_reports(html)
        assert result.strategy == "json_anywhere"
        assert len(result.points) == 2
        assert result.points[-1].value == 3


class TestSessionManagement:
    @patch("src.scraper.requests.post")
    def test_create_session_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        scraper._create_session("test-session-1")
        payload = mock_post.call_args[1]["json"]
        assert payload["cmd"] == "sessions.create"
        assert payload["session"] == "test-session-1"

    @patch("src.scraper.requests.post")
    def test_create_session_failure_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "error", "message": "session limit"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with pytest.raises(FetchError, match="Session create failed"):
            scraper._create_session("test-session-2")

    @patch("src.scraper.requests.post")
    def test_destroy_session_best_effort(self, mock_post):
        """Destroy should not raise, even on error."""
        mock_post.side_effect = ConnectionError("FlareSolverr down")
        scraper._destroy_session("test-session-3")  # Should not raise

    @patch("src.scraper.requests.post")
    def test_flaresolverr_fetch_with_session(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "solution": {
                "response": "<html>content</html>",
                "userAgent": "Mozilla/5.0 Test",
            },
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = scraper._flaresolverr_fetch("https://example.com", session_id="sess-1")
        assert result.response_html == "<html>content</html>"
        payload = mock_post.call_args[1]["json"]
        assert payload["session"] == "sess-1"
        assert payload["maxTimeout"] == 60000

    @patch("src.scraper.requests.post")
    def test_flaresolverr_fetch_empty_html_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "solution": {"response": "", "userAgent": ""},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with pytest.raises(FetchError, match="empty response"):
            scraper._flaresolverr_fetch("https://example.com")


class TestFetchHtml:
    @patch("src.scraper._destroy_session")
    @patch("src.scraper._flaresolverr_fetch")
    @patch("src.scraper._create_session")
    def test_fetch_html_success(self, mock_create, mock_fetch, mock_destroy):
        mock_fetch.return_value = scraper._FlareSolverrResult(
            response_html="<html>ok</html>", user_agent="Mozilla/5.0",
        )

        result = fetch_html("https://example.com")
        assert result == "<html>ok</html>"
        mock_create.assert_called_once()
        mock_destroy.assert_called_once()

    @patch("src.scraper._destroy_session")
    @patch("src.scraper._flaresolverr_fetch")
    @patch("src.scraper._create_session")
    def test_fetch_html_flaresolverr_error(self, mock_create, mock_fetch, mock_destroy):
        mock_fetch.side_effect = FetchError("Challenge not solved")

        with pytest.raises(FetchError, match="Challenge not solved"):
            fetch_html("https://example.com")

    @patch("src.scraper._destroy_session")
    @patch("src.scraper._flaresolverr_fetch")
    @patch("src.scraper._create_session")
    @patch("src.scraper.time.sleep")
    def test_fetch_html_retries_on_connection_error(self, mock_sleep, mock_create, mock_fetch, mock_destroy):
        mock_fetch.side_effect = ConnectionError("fail")

        with pytest.raises(ConnectionError):
            fetch_html("https://example.com")
        assert mock_create.call_count == 3  # MAX_RETRIES
        assert mock_destroy.call_count == 3  # Cleanup after each attempt

    @patch("src.scraper._destroy_session")
    @patch("src.scraper._flaresolverr_fetch")
    @patch("src.scraper._create_session")
    @patch("src.scraper.time.sleep")
    def test_session_rotation_uses_unique_ids(self, mock_sleep, mock_create, mock_fetch, mock_destroy):
        """Each retry attempt uses a different session ID."""
        mock_fetch.side_effect = [
            ConnectionError("timeout"),
            ConnectionError("timeout"),
            scraper._FlareSolverrResult(response_html="<html>ok</html>", user_agent="test"),
        ]

        result = fetch_html("https://example.com")
        assert result == "<html>ok</html>"
        assert mock_create.call_count == 3
        assert mock_destroy.call_count == 3

        # All session IDs should be unique
        session_ids = [call[0][0] for call in mock_create.call_args_list]
        assert len(set(session_ids)) == 3
        assert all(sid.startswith("mbh-") for sid in session_ids)

    @patch("src.scraper._destroy_session")
    @patch("src.scraper._flaresolverr_fetch")
    @patch("src.scraper._create_session")
    def test_session_cleanup_on_success(self, mock_create, mock_fetch, mock_destroy):
        """Session is destroyed even on successful fetch."""
        mock_fetch.return_value = scraper._FlareSolverrResult(
            response_html="<html>ok</html>", user_agent="test",
        )

        fetch_html("https://example.com")
        # Verify the same session ID was created and destroyed
        created_id = mock_create.call_args[0][0]
        destroyed_id = mock_destroy.call_args[0][0]
        assert created_id == destroyed_id


class TestGetCurrentValue:
    @patch("src.scraper.fetch_html")
    def test_returns_last_value(self, mock_fetch):
        mock_fetch.return_value = (FIXTURES / "normal_response.html").read_text()
        value = get_current_value("https://example.com")
        assert value == 2  # last RSC data point

    @patch("src.scraper.fetch_html")
    def test_returns_high_value(self, mock_fetch):
        mock_fetch.return_value = (FIXTURES / "high_alert_response.html").read_text()
        value = get_current_value("https://example.com")
        assert value == 152
