"""Tests for notifier module – Telegram message sending."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.notifier import send_alert, send_recovery, send_fetch_failure_alert, _send_telegram


class TestSendTelegram:
    def test_successful_send(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_session.post.return_value = mock_resp

        result = _send_telegram("test msg", token="tok", chat_id="123", session=mock_session)
        assert result is True
        mock_session.post.assert_called_once()

    def test_retries_on_failure(self):
        mock_session = MagicMock()
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.text = "Internal Server Error"
        mock_session.post.return_value = resp_500

        result = _send_telegram("test msg", token="tok", chat_id="123", session=mock_session)
        assert result is False
        assert mock_session.post.call_count == 3  # MAX_RETRIES

    def test_retries_on_exception(self):
        mock_session = MagicMock()
        mock_session.post.side_effect = requests.ConnectionError("fail")

        result = _send_telegram("test msg", token="tok", chat_id="123", session=mock_session)
        assert result is False


class TestAlertMessages:
    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_alert_contains_value(self, mock_send):
        result = send_alert(45, 30)
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "45" in msg
        assert "riasztás" in msg.lower() or "riaszt" in msg.lower()

    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_recovery_contains_value(self, mock_send):
        result = send_recovery(15, 30)
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "15" in msg
        assert "Helyreállás" in msg or "helyreáll" in msg.lower()

    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_fetch_failure_alert(self, mock_send):
        result = send_fetch_failure_alert(3, "TimeoutError")
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "3" in msg
        assert "TimeoutError" in msg
