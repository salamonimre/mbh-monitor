"""Tests for scraper module – parse strategies and fetch logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from src import scraper, config
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


class TestSolverFetch:
    @patch("src.scraper.requests.post")
    def test_solver_fetch_success(self, mock_post):
        """Solver fetch returns HTML and user agent."""
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

        result = scraper._solver_fetch("https://example.com")
        assert result.response_html == "<html>content</html>"
        payload = mock_post.call_args[1]["json"]
        assert payload["cmd"] == "request.get"
        assert payload["url"] == "https://example.com"

    @patch("src.scraper.requests.post")
    def test_solver_fetch_sends_dual_timeout(self, mock_post):
        """Both maxTimeout (ms) and max_timeout (seconds) are sent for cross-compatibility."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html>ok</html>", "userAgent": "test"},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        scraper._solver_fetch("https://example.com")
        payload = mock_post.call_args[1]["json"]
        assert payload["maxTimeout"] == config.FLARESOLVERR_MAX_TIMEOUT
        assert payload["max_timeout"] == config.FLARESOLVERR_MAX_TIMEOUT // 1000

    @patch("src.scraper.requests.post")
    def test_solver_fetch_empty_html_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "solution": {"response": "", "userAgent": ""},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with pytest.raises(FetchError, match="empty response"):
            scraper._solver_fetch("https://example.com")

    @patch("src.scraper.requests.post")
    def test_solver_fetch_error_status_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "error", "message": "challenge failed"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with pytest.raises(FetchError, match="Solver error"):
            scraper._solver_fetch("https://example.com")

    @patch("src.scraper.config")
    @patch("src.scraper.requests.post")
    def test_solver_fetch_with_proxy(self, mock_post, mock_config):
        """Proxy is passed to solver when configured."""
        mock_config.FLARESOLVERR_URL = "http://localhost:8191/v1"
        mock_config.FLARESOLVERR_MAX_TIMEOUT = 60000
        mock_config.FLARESOLVERR_PROXY = "http://proxy:8080"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "solution": {
                "response": "<html>via proxy</html>",
                "userAgent": "Mozilla/5.0 Test",
            },
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = scraper._solver_fetch("https://example.com")
        assert result.response_html == "<html>via proxy</html>"
        payload = mock_post.call_args[1]["json"]
        assert payload["proxy"] == {"url": "http://proxy:8080"}

    @patch("src.scraper.config")
    @patch("src.scraper.requests.post")
    def test_solver_fetch_no_proxy_when_empty(self, mock_post, mock_config):
        """No proxy param when FLARESOLVERR_PROXY is empty."""
        mock_config.FLARESOLVERR_URL = "http://localhost:8191/v1"
        mock_config.FLARESOLVERR_MAX_TIMEOUT = 60000
        mock_config.FLARESOLVERR_PROXY = ""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "solution": {
                "response": "<html>direct</html>",
                "userAgent": "Mozilla/5.0 Test",
            },
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        scraper._solver_fetch("https://example.com")
        payload = mock_post.call_args[1]["json"]
        assert "proxy" not in payload


class TestFetchHtml:
    @patch("src.scraper._check_solver_health")
    @patch("src.scraper._solver_fetch")
    def test_fetch_html_success(self, mock_fetch, mock_health):
        mock_fetch.return_value = scraper._SolverResult(
            response_html="<html>ok</html>", user_agent="Mozilla/5.0",
        )

        result = fetch_html("https://example.com")
        assert result == "<html>ok</html>"
        mock_fetch.assert_called_once()
        mock_health.assert_called_once()

    @patch("src.scraper._check_solver_health")
    @patch("src.scraper._solver_fetch")
    def test_fetch_html_solver_error(self, mock_fetch, mock_health):
        mock_fetch.side_effect = FetchError("Challenge not solved")

        with pytest.raises(FetchError, match="Challenge not solved"):
            fetch_html("https://example.com")

    @patch("src.scraper._check_solver_health")
    @patch("src.scraper._solver_fetch")
    @patch("src.scraper.time.sleep")
    def test_fetch_html_retries_on_connection_error(self, mock_sleep, mock_fetch, mock_health):
        mock_fetch.side_effect = ConnectionError("fail")

        with pytest.raises(ConnectionError):
            fetch_html("https://example.com")
        assert mock_fetch.call_count == config.MAX_RETRIES

    @patch("src.scraper._check_solver_health")
    @patch("src.scraper._solver_fetch")
    @patch("src.scraper.time.sleep")
    def test_fetch_html_retries_then_succeeds(self, mock_sleep, mock_fetch, mock_health):
        """Retry eventually succeeds."""
        mock_fetch.side_effect = [
            ConnectionError("timeout"),
            ConnectionError("timeout"),
            scraper._SolverResult(response_html="<html>ok</html>", user_agent="test"),
        ]

        result = fetch_html("https://example.com")
        assert result == "<html>ok</html>"
        assert mock_fetch.call_count == 3

    @patch("src.scraper.requests.get")
    def test_health_check_fails_raises_fetch_error(self, mock_get):
        """Solver unreachable -> FetchError before any retry."""
        mock_get.side_effect = requests.ConnectionError("Connection refused")

        with pytest.raises(FetchError, match="Solver unreachable"):
            fetch_html("https://example.com")


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
