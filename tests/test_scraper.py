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


class TestFetchHtml:
    @patch("src.scraper.cf_requests.get")
    @patch("src.scraper._get_cf_cookies")
    def test_fetch_html_success(self, mock_cookies, mock_get):
        mock_cookies.return_value = scraper._FlareSolverrResult(
            cookies={"cf_clearance": "abc"}, user_agent="Mozilla/5.0", response_html=""
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>ok</html>"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_html("https://example.com")
        assert result == "<html>ok</html>"
        mock_get.assert_called_once()

    @patch("src.scraper.cf_requests.get")
    @patch("src.scraper._get_cf_cookies")
    def test_fetch_html_403_falls_back_to_flaresolverr_html(self, mock_cookies, mock_get):
        """When curl_cffi gets 403, use FlareSolverr's response HTML."""
        mock_cookies.return_value = scraper._FlareSolverrResult(
            cookies={"cf_clearance": "abc"}, user_agent="Mozilla/5.0",
            response_html="<html>flaresolverr content</html>",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {"cf-ray": "test-ray"}
        mock_get.return_value = mock_resp

        result = fetch_html("https://example.com")
        assert result == "<html>flaresolverr content</html>"

    @patch("src.scraper.cf_requests.get")
    @patch("src.scraper._get_cf_cookies")
    def test_fetch_html_403_no_flaresolverr_html_raises(self, mock_cookies, mock_get):
        """When curl_cffi gets 403 and FlareSolverr HTML is empty, raise FetchError."""
        mock_cookies.return_value = scraper._FlareSolverrResult(
            cookies={"cf_clearance": "abc"}, user_agent="Mozilla/5.0", response_html="",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {"cf-ray": "test-ray"}
        mock_get.return_value = mock_resp

        with pytest.raises(FetchError, match="403 and FlareSolverr returned empty"):
            fetch_html("https://example.com")

    @patch("src.scraper._get_cf_cookies")
    def test_fetch_html_flaresolverr_error(self, mock_cookies):
        mock_cookies.side_effect = FetchError("Challenge not solved")

        with pytest.raises(FetchError, match="Challenge not solved"):
            fetch_html("https://example.com")

    @patch("src.scraper.time.sleep")
    @patch("src.scraper._get_cf_cookies")
    def test_fetch_html_retries_on_connection_error(self, mock_cookies, mock_sleep):
        mock_cookies.side_effect = ConnectionError("fail")

        with pytest.raises(ConnectionError):
            fetch_html("https://example.com")
        assert mock_cookies.call_count == 3  # MAX_RETRIES


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
