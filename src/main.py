"""Main entry point – orchestrates scrape, compare, notify, save."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src import config
from src.notifier import send_alert, send_fetch_failure_alert, send_heartbeat, send_recovery
from src.scraper import ParseError, get_current_value
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


def should_send_heartbeat(state: State, now: datetime) -> bool:
    """Check if we should send heartbeat now.

    Conditions:
    - HEARTBEAT_ENABLED is True
    - Current Budapest time is within the heartbeat window (HEARTBEAT_HOUR:00 - HEARTBEAT_HOUR:30)
    - We haven't sent a heartbeat today yet
    """
    if not config.HEARTBEAT_ENABLED:
        return False

    budapest_now = now.astimezone(BUDAPEST_TZ)
    hour = config.HEARTBEAT_HOUR

    if budapest_now.hour != hour or budapest_now.minute >= 30:
        return False

    today_str = budapest_now.strftime("%Y-%m-%d")
    if state.last_heartbeat_date == today_str:
        return False

    return True


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

    # Attempt to fetch current value
    try:
        current_value = get_current_value()
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

    # Decide and act
    action = decide_action(state, current_value, config.ALERT_THRESHOLD)

    if action == "alert":
        logger.info("ALERT: threshold crossed (%d > %d)", current_value, config.ALERT_THRESHOLD)
        send_alert(current_value, config.ALERT_THRESHOLD)
        state.alert_active = True
        state.alert_started_at = now
    elif action == "recovery":
        logger.info("RECOVERY: back to normal (%d <= %d)", current_value, config.ALERT_THRESHOLD)
        send_recovery(current_value, config.ALERT_THRESHOLD)
        state.alert_active = False
        state.alert_started_at = None

    # Update state
    state.last_value = current_value
    state.last_checked = now

    # Heartbeat check (runs after normal monitoring)
    if should_send_heartbeat(state, now):
        last_checked_str = now.astimezone(BUDAPEST_TZ).strftime("%H:%M")
        logger.info("Sending daily heartbeat")
        send_heartbeat(current_value, config.ALERT_THRESHOLD, last_checked_str)
        state.last_heartbeat_date = now.astimezone(BUDAPEST_TZ).strftime("%Y-%m-%d")

    save(state, state_path)

    return 0


if __name__ == "__main__":
    sys.exit(run())
