"""Main entry point – orchestrates scrape, compare, notify, save."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src import config
from src.notifier import (
    send_alert,
    send_daily_summary,
    send_fetch_failure_alert,
    send_heartbeat,
    send_recovery,
)
from src.scraper import ParseError, ReportPoint, fetch_report_data
from src.state import State, load, save

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


def decide_action(state: State, current_value: int, threshold: int) -> str:
    """Decide what action to take based on state transition.

    Returns:
        "alert"    – threshold just crossed (was below, now above)
        "recovery" – was above threshold, now back below
        "none"     – no state change requiring notification
    """
    was_above = state.alert_active
    is_above = current_value > threshold

    if is_above and not was_above:
        return "alert"
    if not is_above and was_above:
        return "recovery"
    return "none"


def _reset_daily_stats_if_needed(state: State, today_str: str) -> None:
    """Reset daily tracking when the date changes (Budapest TZ)."""
    if state.daily_max_date != today_str:
        state.daily_max_value = 0
        state.daily_max_time = None
        state.daily_max_date = today_str
        state.daily_alert_times = []


def _update_daily_stats(state: State, max_value: int, max_time: str | None) -> None:
    """Update daily max if the given value is higher."""
    if max_value > state.daily_max_value:
        state.daily_max_value = max_value
        state.daily_max_time = max_time


def _get_chart_max_today(
    points: list[ReportPoint], budapest_now: datetime,
) -> tuple[int, str | None]:
    """Find the max value from today's chart data points (Budapest TZ)."""
    today_str = budapest_now.strftime("%Y-%m-%d")
    max_value = 0
    max_time: str | None = None
    for p in points:
        ts = p.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_bud = ts.astimezone(BUDAPEST_TZ)
        if ts_bud.strftime("%Y-%m-%d") == today_str and p.value > max_value:
            max_value = p.value
            max_time = ts_bud.strftime("%H:%M")
    return max_value, max_time


def _get_heartbeat_hour(state: State, budapest_now: datetime) -> int | None:
    """Return the heartbeat hour to send, or None if not in any window.

    Checks all configured HEARTBEAT_HOURS. Returns the hour if:
    - We're in that hour's 30-min window (HH:00-HH:29)
    - We haven't already sent a heartbeat for this hour today
    """
    if not config.HEARTBEAT_ENABLED:
        return None

    today_str = budapest_now.strftime("%Y-%m-%d")

    for hour in config.HEARTBEAT_HOURS:
        if budapest_now.hour != hour or budapest_now.minute >= 30:
            continue
        # Check if already sent for this hour today
        hour_key = str(hour)
        if state.heartbeat_sent.get(hour_key) == today_str:
            continue
        return hour

    return None


def _is_summary_hour(hour: int) -> bool:
    """The last configured heartbeat hour gets the daily summary format."""
    return hour == config.HEARTBEAT_HOURS[-1]


def run(state_path: str | None = None) -> int:
    """Main execution flow. Returns 0 on success, 1 on catastrophic failure."""
    state_path = state_path or config.STATE_FILE

    # Validate config
    errors = config.validate()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        return 1

    state = load(state_path)
    now = datetime.now(timezone.utc)
    budapest_now = now.astimezone(BUDAPEST_TZ)
    today_str = budapest_now.strftime("%Y-%m-%d")

    # Reset daily stats if date changed
    _reset_daily_stats_if_needed(state, today_str)

    # Attempt to fetch report data
    try:
        points = fetch_report_data()
        current_value = points[-1].value
        state.consecutive_fetch_failures = 0
        state.error_alert_sent = False
        logger.info("Current report count: %d (threshold: %d)", current_value, config.ALERT_THRESHOLD)
    except (ParseError, Exception) as exc:
        state.consecutive_fetch_failures += 1
        logger.error(
            "Fetch failed (%d consecutive): %s",
            state.consecutive_fetch_failures,
            exc,
        )
        if (
            state.consecutive_fetch_failures >= config.CONSECUTIVE_FAILURE_ALERT_THRESHOLD
            and not state.error_alert_sent
        ):
            send_fetch_failure_alert(state.consecutive_fetch_failures, str(exc))
            state.error_alert_sent = True
        state.last_checked = now
        save(state, state_path)
        return 0  # Not catastrophic – we'll retry next run

    # Update daily stats from chart data (covers spikes between runs)
    chart_max, chart_max_time = _get_chart_max_today(points, budapest_now)
    if current_value > chart_max:
        chart_max = current_value
        chart_max_time = budapest_now.strftime("%H:%M")
    _update_daily_stats(state, chart_max, chart_max_time)

    # Decide and act
    action = decide_action(state, current_value, config.ALERT_THRESHOLD)

    if action == "alert":
        logger.info("ALERT: threshold crossed (%d > %d)", current_value, config.ALERT_THRESHOLD)
        send_alert(current_value, config.ALERT_THRESHOLD)
        state.alert_active = True
        state.alert_started_at = now
        state.daily_alert_times.append(budapest_now.strftime("%H:%M"))
    elif action == "recovery":
        logger.info("RECOVERY: back to normal (%d <= %d)", current_value, config.ALERT_THRESHOLD)
        send_recovery(current_value, config.ALERT_THRESHOLD)
        state.alert_active = False
        state.alert_started_at = None

    # Update state
    state.last_value = current_value
    state.last_checked = now

    # Heartbeat check (runs after normal monitoring)
    hb_hour = _get_heartbeat_hour(state, budapest_now)
    if hb_hour is not None:
        if _is_summary_hour(hb_hour):
            logger.info("Sending daily summary (hour %d)", hb_hour)
            send_daily_summary(
                current_value=current_value,
                threshold=config.ALERT_THRESHOLD,
                daily_max=state.daily_max_value,
                daily_max_time=state.daily_max_time,
                alert_times=state.daily_alert_times,
            )
        else:
            last_checked_str = budapest_now.strftime("%H:%M")
            logger.info("Sending heartbeat (hour %d)", hb_hour)
            send_heartbeat(current_value, config.ALERT_THRESHOLD, last_checked_str)
        state.heartbeat_sent[str(hb_hour)] = today_str

    save(state, state_path)

    return 0


if __name__ == "__main__":
    sys.exit(run())
