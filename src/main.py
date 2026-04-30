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
    send_fetch_recovery,
    send_heartbeat,
    send_parse_degradation_alert,
    send_recovery,
)
from pathlib import Path

from src.scraper import FetchError, ParseError, ParseResult, ReportPoint, fetch_html, parse_reports
from src.state import State, load, save

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


DEBUG_HTML_PATH = Path("/tmp/debug_response.html")


def _save_debug_html(html: str) -> None:
    """Save raw HTML for debugging when parse strategies fail."""
    try:
        DEBUG_HTML_PATH.write_text(html, encoding="utf-8")
        logger.info("Debug HTML saved to %s (%d bytes)", DEBUG_HTML_PATH, len(html))
    except Exception:
        logger.warning("Could not save debug HTML")


def decide_action(state: State, current_value: int, threshold: int) -> str:
    """Decide what action to take based on state transition.

    Returns:
        "alert"    – threshold just crossed (was below, now above)
        "recovery" – was above threshold, now back below
        "none"     – no state change requiring notification
    """
    was_above = state.alert_active
    is_above = current_value >= threshold

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
        logger.info("Daily max updated: %d -> %d (at %s)", state.daily_max_value, max_value, max_time)
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
    """Return the heartbeat hour to send, or None.

    Checks all configured HEARTBEAT_HOURS. Returns the earliest hour if:
    - The configured hour has already passed (current hour >= configured hour)
    - We haven't already sent a heartbeat for this hour today

    This ensures heartbeats are sent even when GitHub Actions cron skips
    the exact hour window due to scheduling delays.
    """
    if not config.HEARTBEAT_ENABLED:
        return None

    today_str = budapest_now.strftime("%Y-%m-%d")

    for hour in config.HEARTBEAT_HOURS:
        if budapest_now.hour < hour:
            continue
        # Check if already sent for this hour today
        hour_key = str(hour)
        if state.heartbeat_sent.get(hour_key) == today_str:
            continue
        return hour

    return None


def _get_pat_expiry_warning() -> str | None:
    """Return warning text if PAT expires within configured days, else None."""
    if not config.PAT_EXPIRY_DATE:
        return None
    try:
        expiry = datetime.strptime(config.PAT_EXPIRY_DATE, "%Y-%m-%d").date()
    except ValueError:
        return None
    days_left = (expiry - datetime.now(timezone.utc).date()).days
    if days_left <= 0:
        return f"LEJÁRT a cron-job.org PAT ({config.PAT_EXPIRY_DATE})! Azonnal rotáld."
    if days_left <= config.PAT_EXPIRY_WARNING_DAYS:
        return f"cron-job.org PAT {days_left} nap múlva lejár ({config.PAT_EXPIRY_DATE})"
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

    logger.info("Run started | threshold=%d | hb_hours=%s | state: value=%d failures=%d alert=%s",
                config.ALERT_THRESHOLD, config.HEARTBEAT_HOURS,
                state.last_value, state.consecutive_fetch_failures, state.alert_active)

    # Reset daily stats if date changed
    _reset_daily_stats_if_needed(state, today_str)

    # Attempt to fetch and parse report data (split for debug HTML on parse failure)
    html: str | None = None
    try:
        html = fetch_html(config.DOWNDETECTOR_URL)
        result = parse_reports(html)
        points = result.points
        current_value = points[-1].value
        logger.info("Current report count: %d (threshold: %d, strategy: %s)",
                     current_value, config.ALERT_THRESHOLD, result.strategy)

        # Fetch recovery notification: if we had sent an error alert, notify that it's resolved
        if state.error_alert_sent:
            ok = send_fetch_recovery(
                previous_failures=state.consecutive_fetch_failures,
                current_value=current_value,
                strategy=result.strategy,
            )
            logger.info("Fetch recovery notification -> ok=%s (after %d failures)",
                         ok, state.consecutive_fetch_failures)

        state.consecutive_fetch_failures = 0
        state.error_alert_sent = False
    except (FetchError, ParseError, Exception) as exc:
        if isinstance(exc, ParseError) and html:
            _save_debug_html(html)
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
            ok = send_fetch_failure_alert(state.consecutive_fetch_failures, str(exc))
            logger.info("Fetch failure alert -> ok=%s", ok)
            state.error_alert_sent = True
        state.last_checked = now
        logger.info("Run complete (error) | failures=%d | error_alert_sent=%s",
                     state.consecutive_fetch_failures, state.error_alert_sent)
        save(state, state_path)
        return 0  # Not catastrophic – we'll retry next run

    # Degradation detection: alert if RSC strategy failed
    if result.strategy == "rsc":
        state.degraded_parse_alert_sent = False
    elif not state.degraded_parse_alert_sent:
        logger.warning("Parse degradation: using %s instead of rsc", result.strategy)
        _save_debug_html(html)
        ok = send_parse_degradation_alert(result.strategy, current_value)
        logger.info("Parse degradation alert -> ok=%s", ok)
        state.degraded_parse_alert_sent = True

    # Update daily stats from chart data (covers spikes between runs)
    chart_max, chart_max_time = _get_chart_max_today(points, budapest_now)
    logger.info("Chart max today: %d at %s (previous daily_max: %d)",
                chart_max, chart_max_time or "n/a", state.daily_max_value)
    if current_value > chart_max:
        chart_max = current_value
        chart_max_time = budapest_now.strftime("%H:%M")
    _update_daily_stats(state, chart_max, chart_max_time)

    # Decide and act
    action = decide_action(state, current_value, config.ALERT_THRESHOLD)

    if action == "alert":
        logger.info("ALERT: threshold crossed (%d > %d)", current_value, config.ALERT_THRESHOLD)
        ok = send_alert(current_value, config.ALERT_THRESHOLD)
        logger.info("Alert notification -> ok=%s", ok)
        state.alert_active = True
        state.alert_started_at = now
        state.daily_alert_times.append(budapest_now.strftime("%H:%M"))
    elif action == "recovery":
        logger.info("RECOVERY: back to normal (%d <= %d)", current_value, config.ALERT_THRESHOLD)
        ok = send_recovery(current_value, config.ALERT_THRESHOLD)
        logger.info("Recovery notification -> ok=%s", ok)
        state.alert_active = False
        state.alert_started_at = None
    else:
        logger.info("No action needed (value=%d, threshold=%d, alert_active=%s)",
                     current_value, config.ALERT_THRESHOLD, state.alert_active)

    # Update state
    state.last_value = current_value
    state.last_checked = now

    # Heartbeat check (runs after normal monitoring)
    hb_hour = _get_heartbeat_hour(state, budapest_now)
    if hb_hour is not None:
        if _is_summary_hour(hb_hour):
            logger.info("Sending daily summary (hour %d)", hb_hour)
            warnings = []
            pat_warning = _get_pat_expiry_warning()
            if pat_warning:
                warnings.append(pat_warning)
            ok = send_daily_summary(
                current_value=current_value,
                threshold=config.ALERT_THRESHOLD,
                daily_max=state.daily_max_value,
                daily_max_time=state.daily_max_time,
                alert_times=state.daily_alert_times,
                warnings=warnings,
            )
            logger.info("Daily summary notification -> ok=%s", ok)
        else:
            last_checked_str = budapest_now.strftime("%H:%M")
            last_point_ts = points[-1].timestamp
            if last_point_ts.tzinfo is None:
                last_point_ts = last_point_ts.replace(tzinfo=timezone.utc)
            data_time = last_point_ts.astimezone(BUDAPEST_TZ).strftime("%H:%M")
            logger.info("Sending heartbeat (hour %d)", hb_hour)
            ok = send_heartbeat(current_value, config.ALERT_THRESHOLD, last_checked_str, data_time=data_time, strategy=result.strategy)
            logger.info("Heartbeat notification -> ok=%s", ok)
        logger.info("Heartbeat sent for hour %d (actual: %s) | type=%s",
                     hb_hour, budapest_now.strftime("%H:%M"), "summary" if _is_summary_hour(hb_hour) else "heartbeat")
        state.heartbeat_sent[str(hb_hour)] = today_str

    logger.info("Run complete | value=%d | daily_max=%d | action=%s | strategy=%s",
                current_value, state.daily_max_value, action, result.strategy)
    save(state, state_path)

    return 0


if __name__ == "__main__":
    sys.exit(run())
