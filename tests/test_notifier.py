"""Tests for notifier module – Telegram message sending."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.notifier import send_alert, send_recovery, send_fetch_failure_alert, send_heartbeat, send_daily_summary, send_parse_degradation_alert, _send_telegram


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

    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_heartbeat(self, mock_send):
        result = send_heartbeat(5, 10, "09:15", data_time="08:51")
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "5" in msg
        assert "10" in msg
        assert "09:15" in msg
        assert "08:51" in msg
        assert "heartbeat" in msg.lower()

    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_heartbeat_without_data_time(self, mock_send):
        result = send_heartbeat(5, 10, "09:15")
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "5" in msg
        assert "adat" not in msg

    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_daily_summary_with_alerts(self, mock_send):
        result = send_daily_summary(8, 10, daily_max=25, daily_max_time="14:30", alert_times=["14:00", "14:30"])
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "25" in msg
        assert "14:30" in msg
        assert "14:00" in msg
        assert "összefoglaló" in msg.lower()

    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_daily_summary_no_alerts(self, mock_send):
        result = send_daily_summary(3, 10, daily_max=7, daily_max_time="11:00", alert_times=[])
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "nem" in msg.lower()
        assert "7" in msg

    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_daily_summary_with_warnings(self, mock_send):
        result = send_daily_summary(3, 10, daily_max=7, daily_max_time="11:00",
                                     alert_times=[], warnings=["PAT 5 nap múlva lejár"])
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "PAT" in msg
        assert "⚠️" in msg

    @patch("src.notifier._send_telegram", return_value=True)
    def test_send_parse_degradation_alert(self, mock_send):
        result = send_parse_degradation_alert("json_anywhere", 7)
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "json_anywhere" in msg
        assert "degradáció" in msg.lower() or "Parse" in msg
