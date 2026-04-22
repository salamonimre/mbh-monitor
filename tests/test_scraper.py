"""Tests for scraper module – parse strategies and fetch logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.scraper import FetchError, ParseError, ReportPoint, parse_reports, fetch_html, get_current_value

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseReports:
    def test_parse_normal_response_aria_label(self):
        html = (FIXTURES / "normal_response.html").read_text()
        reports = parse_reports(html)
        assert len(reports) == 1
        assert reports[0].value == 12

    def test_parse_high_alert_response(self):
        html = (FIXTURES / "high_alert_response.html").read_text()
        reports = parse_reports(html)
        assert len(reports) > 0
        assert reports[-1].value == 152

    def test_parse_no_problems_heading_only(self):
        html = (FIXTURES / "no_problems_heading_only.html").read_text()
        reports = parse_reports(html)
        assert len(reports) == 1
        assert reports[0].value == 0

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

    def test_regex_fallback_when_no_json(self):
        """HTML with current-number class but no script/JSON data."""
        html = """
        <html><body>
        <div class="current-number">42</div>
        </body></html>
        """
        reports = parse_reports(html)
        assert len(reports) == 1
        assert reports[0].value == 42

    def test_aria_label_strategy_priority(self):
        """aria-label should take priority over heading."""
        html = """
        <html><body>
        <h1>User reports show <span>no current problems</span></h1>
        <div aria-label="Reports chart for the last 24 hours with a peak of 5 reports, status: no problems"></div>
        </body></html>
        """
        reports = parse_reports(html)
        assert reports[0].value == 5  # aria-label wins, not 0 from heading


class TestFetchHtml:
    @patch("src.scraper.cffi_requests.get")
    def test_fetch_html_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>ok</html>"
        mock_get.return_value = mock_resp

        result = fetch_html("https://example.com")
        assert result == "<html>ok</html>"
        mock_get.assert_called_once()

    @patch("src.scraper.time.sleep")
    @patch("src.scraper.cffi_requests.get")
    def test_fetch_html_retries_on_429(self, mock_get, mock_sleep):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.text = "<html>ok</html>"
        mock_get.side_effect = [resp_429, resp_200]

        result = fetch_html("https://example.com")
        assert result == "<html>ok</html>"
        assert mock_sleep.called

    @patch("src.scraper.cffi_requests.get")
    def test_fetch_html_raises_on_403(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_get.return_value = mock_resp

        with pytest.raises(FetchError, match="403"):
            fetch_html("https://example.com")

    @patch("src.scraper.time.sleep")
    @patch("src.scraper.cffi_requests.get")
    def test_fetch_html_raises_after_all_retries(self, mock_get, mock_sleep):
        mock_get.side_effect = ConnectionError("fail")

        with pytest.raises(ConnectionError):
            fetch_html("https://example.com")


class TestGetCurrentValue:
    @patch("src.scraper.fetch_html")
    def test_returns_last_value(self, mock_fetch):
        mock_fetch.return_value = (FIXTURES / "normal_response.html").read_text()
        value = get_current_value("https://example.com")
        assert value == 12

    @patch("src.scraper.fetch_html")
    def test_returns_high_value(self, mock_fetch):
        mock_fetch.return_value = (FIXTURES / "high_alert_response.html").read_text()
        value = get_current_value("https://example.com")
        assert value == 152
