"""Tests for main module – alert state machine, heartbeat, error alerting, full run."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.main import decide_action, run, should_send_heartbeat
from src.state import State, save

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")
FIXTURES = Path(__file__).parent / "fixtures"


class TestDecideAction:
    """Test the alert state machine – every transition must be covered."""

    @pytest.mark.parametrize(
        "prev_value,alert_active,curr,threshold,expected",
        [
            # Below threshold, stays below -> no action
            (10, False, 20, 30, "none"),
            # Below threshold, crosses above -> alert
            (10, False, 35, 30, "alert"),
            # Above threshold, stays above -> no action (no spam)
            (35, True, 40, 30, "none"),
            # Above threshold, drops below -> recovery
            (40, True, 25, 30, "recovery"),
            # First run (value=0, no alert), above threshold -> alert
            (0, False, 35, 30, "alert"),
            # Exactly at threshold -> no alert (> not >=)
            (10, False, 30, 30, "none"),
            # Just above threshold -> alert
            (10, False, 31, 30, "alert"),
            # Recovery to exactly threshold -> recovery
            (35, True, 30, 30, "recovery"),
        ],
    )
    def test_decide_action(self, prev_value, alert_active, curr, threshold, expected):
        state = State(last_value=prev_value, alert_active=alert_active)
        assert decide_action(state, curr, threshold) == expected


class TestShouldSendHeartbeat:
    """Test heartbeat scheduling logic."""

    def test_heartbeat_in_window_not_yet_sent(self):
        """9:15 Budapest, no heartbeat today -> should send."""
        state = State(last_heartbeat_date=None)
        # 9:15 Budapest = 7:15 UTC (in summer, CEST = UTC+2)
        now = datetime(2026, 7, 15, 7, 15, 0, tzinfo=timezone.utc)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOUR = 9
            assert should_send_heartbeat(state, now) is True

    def test_heartbeat_already_sent_today(self):
        """9:15 Budapest but already sent today -> should not send."""
        state = State(last_heartbeat_date="2026-07-15")
        now = datetime(2026, 7, 15, 7, 15, 0, tzinfo=timezone.utc)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOUR = 9
            assert should_send_heartbeat(state, now) is False

    def test_heartbeat_outside_window_hour(self):
        """10:15 Budapest (outside 9:00-9:30 window) -> should not send."""
        state = State(last_heartbeat_date=None)
        now = datetime(2026, 7, 15, 8, 15, 0, tzinfo=timezone.utc)  # 10:15 Budapest
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOUR = 9
            assert should_send_heartbeat(state, now) is False

    def test_heartbeat_outside_window_minute(self):
        """9:35 Budapest (past the 30-min window) -> should not send."""
        state = State(last_heartbeat_date=None)
        now = datetime(2026, 7, 15, 7, 35, 0, tzinfo=timezone.utc)  # 9:35 Budapest
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOUR = 9
            assert should_send_heartbeat(state, now) is False

    def test_heartbeat_disabled(self):
        """HEARTBEAT_ENABLED=false -> should not send."""
        state = State(last_heartbeat_date=None)
        now = datetime(2026, 7, 15, 7, 15, 0, tzinfo=timezone.utc)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOUR = 9
            assert should_send_heartbeat(state, now) is False

    def test_heartbeat_winter_time(self):
        """9:15 Budapest in winter (CET = UTC+1) -> 8:15 UTC."""
        state = State(last_heartbeat_date=None)
        now = datetime(2026, 1, 15, 8, 15, 0, tzinfo=timezone.utc)  # 9:15 Budapest CET
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOUR = 9
            assert should_send_heartbeat(state, now) is True

    def test_heartbeat_yesterday_sent_today_not(self):
        """Sent yesterday, not today -> should send."""
        state = State(last_heartbeat_date="2026-07-14")
        now = datetime(2026, 7, 15, 7, 15, 0, tzinfo=timezone.utc)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOUR = 9
            assert should_send_heartbeat(state, now) is True


class TestRun:
    @patch("src.main.send_alert", return_value=True)
    @patch("src.main.get_current_value", return_value=45)
    def test_run_triggers_alert(self, mock_get, mock_alert, tmp_path):
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False

            result = run(str(state_path))

        assert result == 0
        mock_alert.assert_called_once_with(45, 30)

    @patch("src.main.send_recovery", return_value=True)
    @patch("src.main.get_current_value", return_value=15)
    def test_run_triggers_recovery(self, mock_get, mock_recovery, tmp_path):
        state_path = tmp_path / "state.json"
        save(State(last_value=40, alert_active=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False

            result = run(str(state_path))

        assert result == 0
        mock_recovery.assert_called_once_with(15, 30)

    @patch("src.main.send_alert")
    @patch("src.main.get_current_value", return_value=10)
    def test_run_no_notification_below_threshold(self, mock_get, mock_alert, tmp_path):
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False

            result = run(str(state_path))

        assert result == 0
        mock_alert.assert_not_called()

    @patch("src.main.send_fetch_failure_alert")
    @patch("src.main.get_current_value", side_effect=Exception("Network error"))
    def test_run_handles_fetch_failure(self, mock_get, mock_fail_alert, tmp_path):
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.CONSECUTIVE_FAILURE_ALERT_THRESHOLD = 3

            result = run(str(state_path))

        assert result == 0  # Not catastrophic
        mock_fail_alert.assert_not_called()  # Only 1 failure, threshold is 3

    def test_run_returns_1_on_config_error(self, tmp_path):
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = ["TELEGRAM_BOT_TOKEN is not set"]

            result = run(str(tmp_path / "state.json"))

        assert result == 1


class TestErrorAlerting:
    """Test the 3x consecutive failure -> 1x alert, then silence logic."""

    @patch("src.main.send_fetch_failure_alert", return_value=True)
    @patch("src.main.get_current_value", side_effect=Exception("FlareSolverr down"))
    def test_alert_sent_at_third_failure(self, mock_get, mock_fail_alert, tmp_path):
        """After 3 consecutive failures, alert should be sent exactly once."""
        state_path = tmp_path / "state.json"
        save(State(consecutive_fetch_failures=2, error_alert_sent=False), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.CONSECUTIVE_FAILURE_ALERT_THRESHOLD = 3

            run(str(state_path))

        mock_fail_alert.assert_called_once()
        assert "FlareSolverr down" in mock_fail_alert.call_args[0][1]

    @patch("src.main.send_fetch_failure_alert", return_value=True)
    @patch("src.main.get_current_value", side_effect=Exception("Still broken"))
    def test_no_spam_after_alert_sent(self, mock_get, mock_fail_alert, tmp_path):
        """After alert was already sent, further failures should NOT re-send."""
        state_path = tmp_path / "state.json"
        save(State(consecutive_fetch_failures=5, error_alert_sent=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.CONSECUTIVE_FAILURE_ALERT_THRESHOLD = 3

            run(str(state_path))

        mock_fail_alert.assert_not_called()

    @patch("src.main.send_fetch_failure_alert")
    @patch("src.main.get_current_value", return_value=5)
    def test_success_resets_failure_state(self, mock_get, mock_fail_alert, tmp_path):
        """Successful fetch should reset both counter and alert flag."""
        state_path = tmp_path / "state.json"
        save(State(consecutive_fetch_failures=4, error_alert_sent=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False

            run(str(state_path))

        from src.state import load
        loaded = load(state_path)
        assert loaded.consecutive_fetch_failures == 0
        assert loaded.error_alert_sent is False


class TestHeartbeatIntegration:
    """Test heartbeat within the full run flow."""

    @patch("src.main.send_heartbeat", return_value=True)
    @patch("src.main.get_current_value", return_value=3)
    def test_heartbeat_sent_during_run(self, mock_get, mock_hb, tmp_path):
        """Heartbeat should be sent when in the heartbeat window."""
        state_path = tmp_path / "state.json"

        # Mock datetime.now to return 9:15 Budapest (7:15 UTC in summer)
        fake_now = datetime(2026, 7, 15, 7, 15, 0, tzinfo=timezone.utc)

        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOUR = 9
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        mock_hb.assert_called_once()
        assert mock_hb.call_args[0][0] == 3  # current_value
        assert mock_hb.call_args[0][1] == 10  # threshold

    @patch("src.main.send_heartbeat")
    @patch("src.main.get_current_value", return_value=3)
    def test_heartbeat_not_sent_outside_window(self, mock_get, mock_hb, tmp_path):
        """Heartbeat should NOT be sent outside the window."""
        state_path = tmp_path / "state.json"

        fake_now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)  # 14:00 Budapest

        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOUR = 9
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        mock_hb.assert_not_called()
