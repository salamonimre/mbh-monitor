"""Tests for main module – alert state machine, heartbeat, daily summary, error alerting."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.main import (
    decide_action,
    run,
    _get_heartbeat_hour,
    _is_summary_hour,
    _reset_daily_stats_if_needed,
    _update_daily_stats,
)
from src.state import State, load, save

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")
FIXTURES = Path(__file__).parent / "fixtures"


class TestDecideAction:
    """Test the alert state machine – every transition must be covered."""

    @pytest.mark.parametrize(
        "prev_value,alert_active,curr,threshold,expected",
        [
            (10, False, 20, 30, "none"),
            (10, False, 35, 30, "alert"),
            (35, True, 40, 30, "none"),
            (40, True, 25, 30, "recovery"),
            (0, False, 35, 30, "alert"),
            (10, False, 30, 30, "none"),
            (10, False, 31, 30, "alert"),
            (35, True, 30, 30, "recovery"),
        ],
    )
    def test_decide_action(self, prev_value, alert_active, curr, threshold, expected):
        state = State(last_value=prev_value, alert_active=alert_active)
        assert decide_action(state, curr, threshold) == expected


class TestDailyStats:
    """Test daily max tracking and reset logic."""

    def test_reset_on_new_day(self):
        state = State(daily_max_value=50, daily_max_time="14:30",
                      daily_max_date="2026-04-22", daily_alert_times=["14:20"])
        _reset_daily_stats_if_needed(state, "2026-04-23")
        assert state.daily_max_value == 0
        assert state.daily_max_time is None
        assert state.daily_max_date == "2026-04-23"
        assert state.daily_alert_times == []

    def test_no_reset_same_day(self):
        state = State(daily_max_value=50, daily_max_time="14:30", daily_max_date="2026-04-23")
        _reset_daily_stats_if_needed(state, "2026-04-23")
        assert state.daily_max_value == 50

    def test_update_max_when_higher(self):
        state = State(daily_max_value=10, daily_max_time="10:00")
        budapest_now = datetime(2026, 7, 15, 14, 30, tzinfo=BUDAPEST_TZ)
        _update_daily_stats(state, 25, budapest_now)
        assert state.daily_max_value == 25
        assert state.daily_max_time == "14:30"

    def test_no_update_when_lower(self):
        state = State(daily_max_value=30, daily_max_time="10:00")
        budapest_now = datetime(2026, 7, 15, 14, 30, tzinfo=BUDAPEST_TZ)
        _update_daily_stats(state, 15, budapest_now)
        assert state.daily_max_value == 30
        assert state.daily_max_time == "10:00"

    def test_update_on_equal(self):
        """Equal value should not update (only strictly greater)."""
        state = State(daily_max_value=10, daily_max_time="10:00")
        budapest_now = datetime(2026, 7, 15, 14, 30, tzinfo=BUDAPEST_TZ)
        _update_daily_stats(state, 10, budapest_now)
        assert state.daily_max_value == 10
        assert state.daily_max_time == "10:00"


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
        """19:10 Budapest -> returns 19."""
        state = State(heartbeat_sent={})
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

    def test_outside_all_windows(self):
        """14:00 Budapest -> None."""
        state = State(heartbeat_sent={})
        budapest_now = datetime(2026, 7, 15, 14, 0, tzinfo=BUDAPEST_TZ)
        with patch("src.main.config") as mock_config:
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            assert _get_heartbeat_hour(state, budapest_now) is None

    def test_past_30_min_window(self):
        """9:35 Budapest -> None (past the 30-min window)."""
        state = State(heartbeat_sent={})
        budapest_now = datetime(2026, 7, 15, 9, 35, tzinfo=BUDAPEST_TZ)
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
    @patch("src.main.get_current_value", return_value=45)
    def test_run_triggers_alert(self, mock_get, mock_alert, tmp_path):
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]

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
            mock_config.HEARTBEAT_HOURS = [9, 19]

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
            mock_config.HEARTBEAT_HOURS = [9, 19]

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

        assert result == 0
        mock_fail_alert.assert_not_called()

    def test_run_returns_1_on_config_error(self, tmp_path):
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = ["TELEGRAM_BOT_TOKEN is not set"]

            result = run(str(tmp_path / "state.json"))

        assert result == 1

    @patch("src.main.send_alert", return_value=True)
    @patch("src.main.get_current_value", return_value=45)
    def test_run_tracks_daily_max_and_alert_time(self, mock_get, mock_alert, tmp_path):
        """Alert should update daily_max and record alert time."""
        state_path = tmp_path / "state.json"
        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 30
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]

            run(str(state_path))

        loaded = load(state_path)
        assert loaded.daily_max_value == 45
        assert loaded.daily_max_time is not None
        assert len(loaded.daily_alert_times) == 1


class TestErrorAlerting:
    """Test the 3x consecutive failure -> 1x alert, then silence logic."""

    @patch("src.main.send_fetch_failure_alert", return_value=True)
    @patch("src.main.get_current_value", side_effect=Exception("FlareSolverr down"))
    def test_alert_sent_at_third_failure(self, mock_get, mock_fail_alert, tmp_path):
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
        state_path = tmp_path / "state.json"
        save(State(consecutive_fetch_failures=4, error_alert_sent=True), state_path)

        with patch("src.main.config") as mock_config:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = False
            mock_config.HEARTBEAT_HOURS = [9, 19]

            run(str(state_path))

        loaded = load(state_path)
        assert loaded.consecutive_fetch_failures == 0
        assert loaded.error_alert_sent is False


class TestHeartbeatIntegration:
    """Test heartbeat and daily summary within the full run flow."""

    @patch("src.main.send_heartbeat", return_value=True)
    @patch("src.main.get_current_value", return_value=3)
    def test_morning_heartbeat_sent(self, mock_get, mock_hb, tmp_path):
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
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        mock_hb.assert_called_once()

    @patch("src.main.send_daily_summary", return_value=True)
    @patch("src.main.get_current_value", return_value=3)
    def test_evening_summary_sent(self, mock_get, mock_summary, tmp_path):
        state_path = tmp_path / "state.json"
        save(State(daily_max_value=42, daily_max_time="14:30",
                   daily_max_date="2026-07-15", daily_alert_times=["14:20"]),
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
    @patch("src.main.get_current_value", return_value=3)
    def test_no_heartbeat_outside_windows(self, mock_get, mock_summary, mock_hb, tmp_path):
        state_path = tmp_path / "state.json"
        fake_now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)  # 14:00 Budapest

        with patch("src.main.config") as mock_config, \
             patch("src.main.datetime") as mock_dt:
            mock_config.validate.return_value = []
            mock_config.STATE_FILE = str(state_path)
            mock_config.ALERT_THRESHOLD = 10
            mock_config.DOWNDETECTOR_URL = "https://example.com"
            mock_config.HEARTBEAT_ENABLED = True
            mock_config.HEARTBEAT_HOURS = [9, 19]
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            run(str(state_path))

        mock_hb.assert_not_called()
        mock_summary.assert_not_called()
