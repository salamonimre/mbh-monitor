"""Main entry point – orchestrates scrape, compare, notify, save."""

from __future__ import annotations

import logging
import random
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src import config
from src.notifier import (
    send_alert,
    send_daily_summary,
    send_fetch_recovery,
    send_heartbeat,
    send_parse_degradation_alert,
    send_recovery,
    send_remediation_report,
    send_retroactive_alert,
    send_zenrows_credit_warning,
)
from pathlib import Path

from src.history import append_row
from src.remediation import attempt_remediation
from src.scraper import FetchError, ParseError, ParseResult, ReportPoint, fetch_html, parse_reports
from src.state import State, load, save

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


DEBUG_HTML_PATH = Path("/tmp/debug_response.html")

# Escalation: re-send diagnostic report at these failure counts
_ESCALATION_FAILURES = {6, 12, 24}


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


def _detect_retroactive_spike(
    points: list[ReportPoint],
    last_checked: datetime | None,
    threshold: int,
    alert_active: bool,
    current_value: int,
) -> tuple[int, str] | None:
    """Detect if any data point since last_checked crossed the threshold.

    Returns (spike_value, spike_time_HH:MM_Budapest) if a retroactive spike
    is found, or None otherwise.

    Skips detection when:
    - alert_active is True (normal alert cycle handles it)
    - current_value >= threshold (normal alert handles it)
    """
    if alert_active or current_value >= threshold:
        return None

    best_value = 0
    best_time: str | None = None

    for p in points:
        # Filter to points after last_checked (if set)
        if last_checked is not None:
            ts = p.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts <= last_checked:
                continue

        if p.value >= threshold and p.value > best_value:
            best_value = p.value
            ts = p.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            best_time = ts.astimezone(BUDAPEST_TZ).strftime("%H:%M")

    if best_value >= threshold and best_time is not None:
        return best_value, best_time
    return None


def _reset_daily_stats_if_needed(state: State, today_str: str) -> None:
    """Reset daily tracking when the date changes (Budapest TZ)."""
    if state.daily_max_date != today_str:
        state.daily_max_value = 0
        state.daily_max_time = None
        state.daily_max_date = today_str
        state.daily_alert_times = []
        state.daily_total_fetches = 0
        state.daily_failed_fetches = 0


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


def _process_successful_fetch(
    result: ParseResult,
    html: str,
    state: State,
    now: datetime,
    budapest_now: datetime,
    today_str: str,
    state_path: str,
    *,
    via_remediation: str | None = None,
) -> int:
    """Process a successful fetch+parse result: degradation check, daily stats, alerts, heartbeat.

    Args:
        via_remediation: If set, the remediation strategy name that produced this result.

    Returns 0 always (success path).
    """
    points = result.points
    current_value = points[-1].value

    strategy_label = result.strategy
    if via_remediation:
        strategy_label = f"{result.strategy} (via {via_remediation})"

    logger.info("Current report count: %d (threshold: %d, strategy: %s)",
                current_value, config.ALERT_THRESHOLD, strategy_label)

    # Fetch recovery notification
    if state.error_alert_sent:
        ok = send_fetch_recovery(
            previous_failures=state.consecutive_fetch_failures,
            current_value=current_value,
            strategy=strategy_label,
        )
        logger.info("Fetch recovery notification -> ok=%s (after %d failures)",
                     ok, state.consecutive_fetch_failures)

    state.consecutive_fetch_failures = 0
    state.error_alert_sent = False
    state.remediation_report_sent = False
    state.first_failure_at = None

    # Degradation detection
    if result.strategy == "rsc":
        state.degraded_parse_alert_sent = False
    elif not state.degraded_parse_alert_sent:
        logger.warning("Parse degradation: using %s instead of rsc", result.strategy)
        _save_debug_html(html)
        ok = send_parse_degradation_alert(result.strategy, current_value)
        logger.info("Parse degradation alert -> ok=%s", ok)
        state.degraded_parse_alert_sent = True

    # Update daily stats from chart data
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

        # Retroactive spike detection: check if any point since last check crossed threshold
        spike = _detect_retroactive_spike(
            points, state.last_checked, config.ALERT_THRESHOLD,
            state.alert_active, current_value,
        )
        if spike is not None:
            spike_value, spike_time = spike
            logger.info("RETROACTIVE SPIKE detected: %d at %s (current=%d, threshold=%d)",
                        spike_value, spike_time, current_value, config.ALERT_THRESHOLD)
            ok = send_retroactive_alert(spike_value, spike_time, current_value, config.ALERT_THRESHOLD)
            logger.info("Retroactive alert notification -> ok=%s", ok)
            state.daily_alert_times.append(f"{spike_time}*")
            action = "retroactive_alert"

    # Append to history CSV (after alert_active is updated)
    append_row(
        config.HISTORY_FILE,
        current_value,
        config.ALERT_THRESHOLD,
        state.alert_active,
        budapest_now,
        max_size_mb=config.HISTORY_MAX_SIZE_MB,
    )

    # Update state
    state.last_value = current_value
    state.last_checked = now

    # Heartbeat check
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
                fetch_stats=(state.daily_total_fetches, state.daily_failed_fetches),
            )
            logger.info("Daily summary notification -> ok=%s", ok)
        else:
            check_time_str = budapest_now.strftime("%H:%M")
            last_point_ts = points[-1].timestamp
            if last_point_ts.tzinfo is None:
                last_point_ts = last_point_ts.replace(tzinfo=timezone.utc)
            data_time = last_point_ts.astimezone(BUDAPEST_TZ).strftime("%H:%M")
            data_delay_minutes = int((now - last_point_ts).total_seconds() / 60)
            logger.info("Sending heartbeat (hour %d)", hb_hour)
            ok = send_heartbeat(current_value, config.ALERT_THRESHOLD, check_time_str,
                                data_time=data_time, data_delay_minutes=data_delay_minutes,
                                strategy=result.strategy)
            logger.info("Heartbeat notification -> ok=%s", ok)
        logger.info("Heartbeat sent for hour %d (actual: %s) | type=%s",
                     hb_hour, budapest_now.strftime("%H:%M"), "summary" if _is_summary_hour(hb_hour) else "heartbeat")
        state.heartbeat_sent[str(hb_hour)] = today_str

    log_suffix = f" (via {via_remediation})" if via_remediation else ""
    logger.info("Run complete%s | value=%d | daily_max=%d | action=%s | strategy=%s",
                log_suffix, current_value, state.daily_max_value, action, strategy_label)
    save(state, state_path)

    return 0


def run(state_path: str | None = None) -> int:
    """Main execution flow. Returns 0 on success, 1 on catastrophic failure."""
    state_path = state_path or config.STATE_FILE

    # Validate config
    errors = config.validate()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        return 1

    # Random jitter to break predictable timing patterns (Cloudflare evasion)
    jitter = random.uniform(0, config.JITTER_MAX_SECONDS)
    logger.info("Jitter delay: %.1fs", jitter)
    time.sleep(jitter)

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
    state.total_fetches += 1
    state.daily_total_fetches += 1
    html: str | None = None
    try:
        html = fetch_html(config.DOWNDETECTOR_URL)
        result = parse_reports(html)

        # Skeleton page: heading-only = no chart data, try re-fetch
        if result.strategy == "heading":
            logger.warning("Skeleton page detected (heading strategy), attempting re-fetch")
            _save_debug_html(html)
            rem_result = attempt_remediation(
                config.DOWNDETECTOR_URL,
                ParseError("Skeleton page: no chart data in HTML"),
                state,
            )
            if (rem_result.success and rem_result.parse_result
                    and rem_result.parse_result.strategy != "heading"
                    and rem_result.html):
                logger.info("Skeleton re-fetch improved: heading -> %s (via %s)",
                            rem_result.parse_result.strategy, rem_result.strategy_used)
                return _process_successful_fetch(
                    rem_result.parse_result, rem_result.html, state, now,
                    budapest_now, today_str, state_path,
                    via_remediation=rem_result.strategy_used,
                )
            logger.warning("Skeleton re-fetch failed, proceeding with heading result")

        return _process_successful_fetch(
            result, html, state, now, budapest_now, today_str, state_path,
        )

    except (FetchError, ParseError, Exception) as exc:
        state.failed_fetches += 1
        state.daily_failed_fetches += 1
        if isinstance(exc, ParseError) and html:
            _save_debug_html(html)
        state.consecutive_fetch_failures += 1
        logger.error(
            "Fetch failed (%d consecutive): %s",
            state.consecutive_fetch_failures,
            exc,
        )

        # Record first failure timestamp for time-based notification
        if state.first_failure_at is None:
            state.first_failure_at = now
            logger.info("First failure in streak recorded at %s", now.isoformat())

        # Immediate remediation on every failure
        logger.info("Remediation triggered | failures=%d",
                    state.consecutive_fetch_failures)

        rem_result = attempt_remediation(config.DOWNDETECTOR_URL, exc, state)

        if rem_result.success and rem_result.parse_result and rem_result.html:
            logger.info("Remediation result: SUCCESS via %s | processing normally",
                        rem_result.strategy_used)

            # Send success report
            attempt_dicts = [
                {"strategy": a.strategy, "result": a.result,
                 "duration_s": a.duration_s, "error": a.error}
                for a in rem_result.attempts
            ]
            ok = send_remediation_report(
                success=True,
                error_category=rem_result.error_category.value,
                consecutive_failures=state.consecutive_fetch_failures,
                attempts=attempt_dicts,
                strategy_used=rem_result.strategy_used,
                duration_s=rem_result.duration_s,
            )
            logger.info("Remediation report sent -> ok=%s", ok)

            # Check ZenRows credit warning
            if (
                rem_result.zenrows_credits_remaining is not None
                and rem_result.zenrows_credits_remaining <= config.ZENROWS_CREDIT_WARNING_THRESHOLD
            ):
                ok = send_zenrows_credit_warning(rem_result.zenrows_credits_remaining)
                logger.info("ZenRows credit warning sent -> ok=%s (remaining: %d)",
                            ok, rem_result.zenrows_credits_remaining)

            return _process_successful_fetch(
                rem_result.parse_result, rem_result.html, state, now,
                budapest_now, today_str, state_path,
                via_remediation=rem_result.strategy_used,
            )
        else:
            logger.warning("Remediation result: FAILED | checking notification criteria")

            # Time-based notification: elapsed >= N min AND failures >= M
            elapsed_minutes = (now - state.first_failure_at).total_seconds() / 60
            is_escalation = state.consecutive_fetch_failures in _ESCALATION_FAILURES

            should_notify = (
                not state.error_alert_sent
                and elapsed_minutes >= config.NOTIFICATION_DELAY_MINUTES
                and state.consecutive_fetch_failures >= config.NOTIFICATION_MIN_FAILURES
            )

            if should_notify or is_escalation:
                attempt_dicts = [
                    {"strategy": a.strategy, "result": a.result,
                     "duration_s": a.duration_s, "error": a.error}
                    for a in rem_result.attempts
                ]
                ok = send_remediation_report(
                    success=False,
                    error_category=rem_result.error_category.value,
                    consecutive_failures=state.consecutive_fetch_failures,
                    attempts=attempt_dicts,
                    duration_s=rem_result.duration_s,
                )
                logger.info(
                    "Remediation report sent -> ok=%s | first_notify=%s | escalation=%s | "
                    "elapsed=%.0fmin | failure=#%d",
                    ok, should_notify, is_escalation, elapsed_minutes,
                    state.consecutive_fetch_failures,
                )
                state.error_alert_sent = True
                state.remediation_report_sent = True
            else:
                logger.info(
                    "Notification suppressed | error_alert_sent=%s | elapsed=%.0fmin | "
                    "failures=%d | delay_threshold=%dmin | min_failures=%d",
                    state.error_alert_sent, elapsed_minutes,
                    state.consecutive_fetch_failures,
                    config.NOTIFICATION_DELAY_MINUTES, config.NOTIFICATION_MIN_FAILURES,
                )

            logger.info("Run complete (error+remediation_failed) | failures=%d | category=%s",
                        state.consecutive_fetch_failures, rem_result.error_category.value)

        state.last_checked = now
        save(state, state_path)
        return 0  # Not catastrophic – we'll retry next run


if __name__ == "__main__":
    sys.exit(run())
