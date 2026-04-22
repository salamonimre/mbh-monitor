"""Tests for main module – alert state machine and full run."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.main import decide_action, run
from src.state import State

FIXTURES = Path(__file__).parent / "fixtures"


class TestDecideAction:
    """Test the alert state machine – every transition must be covered."""

    @pytest.mark.parametrize(
        "prev_value,alert_active,curr,threshold,expected",
        [
            # Below threshold, stays below → no action
            (10, False, 20, 30, "none"),
            # Below threshold, crosses above → alert
            (10, False, 35, 30, "alert"),
            # Above threshold, stays above → no action (no spam)
            (35, True, 40, 30, "none"),
            # Above threshold, drops below → recovery
            (40, True, 25, 30, "recovery"),
            # First run (value=0, no alert), above threshold → alert
            (0, False, 35, 30, "alert"),
            # Exactly at threshold → no alert (> not >=)
            (10, False, 30, 30, "none"),
            # Just above threshold → alert
            (10, False, 31, 30, "alert"),
            # Recovery to exactly threshold → recovery
            (35, True, 30, 30, "recovery"),
        ],
    )
    def test_decide_action(self, prev_value, alert_active, curr, threshold, expected):
        state = State(last_value=prev_value, alert_active=alert_active)
        assert decide_action(state, curr, threshold) == expected


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

            result = run(str(state_path))

        assert result == 0
        mock_alert.assert_called_once_with(45, 30)

    @patch("src.main.send_recovery", return_value=True)
    @patch("src.main.get_current_value", return_value=15)
    def test_run_triggers_recovery(self, mock_get, mock_recovery, tmp_path):
        # Pre-set alert-active state
        from src.state import save

        state_path = tmp_path / "state.json"
        save(State(last_value=40, alert_active=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"

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
