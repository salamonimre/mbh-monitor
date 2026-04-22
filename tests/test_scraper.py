"""Tests for scraper module – parse strategies and fetch logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.scraper import ParseError, ReportPoint, parse_reports, fetch_html, get_current_value

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseReports:
    def test_parse_normal_response_json_strategy(self):
        html = (FIXTURES / "normal_response.html").read_text()
        reports = parse_reports(html)
        assert len(reports) == 5
        assert all(isinstance(r, ReportPoint) for r in reports)
        assert all(r.value >= 0 for r in reports)
        # Should be sorted by timestamp
        timestamps = [r.timestamp for r in reports]
        assert timestamps == sorted(timestamps)

    def test_parse_high_alert_response(self):
        html = (FIXTURES / "high_alert_response.html").read_text()
        reports = parse_reports(html)
        assert len(reports) > 0
        assert reports[-1].value == 152

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


class TestFetchHtml:
    @patch("src.scraper.requests.Session")
    def test_fetch_html_success(self, mock_session_cls):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>ok</html>"
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        result = fetch_html("https://example.com", session=mock_session)
        assert result == "<html>ok</html>"

    @patch("src.scraper.time.sleep")
    @patch("src.scraper.requests.Session")
    def test_fetch_html_retries_on_429(self, mock_session_cls, mock_sleep):
        mock_session = MagicMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.text = "<html>ok</html>"
        resp_200.raise_for_status = MagicMock()
        mock_session.get.side_effect = [resp_429, resp_200]

        result = fetch_html("https://example.com", session=mock_session)
        assert result == "<html>ok</html>"
        assert mock_sleep.called

    @patch("src.scraper.time.sleep")
    def test_fetch_html_raises_after_all_retries(self, mock_sleep):
        mock_session = MagicMock()
        mock_session.get.side_effect = requests.ConnectionError("fail")

        with pytest.raises(requests.ConnectionError):
            fetch_html("https://example.com", session=mock_session)


class TestGetCurrentValue:
    @patch("src.scraper.fetch_html")
    def test_returns_last_value(self, mock_fetch):
        mock_fetch.return_value = (FIXTURES / "normal_response.html").read_text()
        value = get_current_value("https://example.com")
        assert value == 12  # last value in the fixture

    @patch("src.scraper.fetch_html")
    def test_returns_high_value(self, mock_fetch):
        mock_fetch.return_value = (FIXTURES / "high_alert_response.html").read_text()
        value = get_current_value("https://example.com")
        assert value == 152
