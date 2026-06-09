"""Telegram notification sender."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")

from src import config  # noqa: E402

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _send_telegram(
    message: str,
    *,
    msg_type: str = "unknown",
    token: str | None = None,
    chat_id: str | None = None,
    session: requests.Session | None = None,
) -> bool:
    """Send a message via Telegram bot API. Returns True on success."""
    token = token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    sess = session or requests.Session()

    url = TELEGRAM_API_URL.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    for attempt in range(config.MAX_RETRIES):
        try:
            resp = sess.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 200:
                logger.info("Telegram sent | type=%s", msg_type)
                return True
            logger.warning(
                "Telegram API returned %d: %s (attempt %d)",
                resp.status_code,
                resp.text[:200],
                attempt + 1,
            )
        except requests.RequestException as exc:
            logger.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)

    logger.error("Failed to send Telegram message after %d attempts", config.MAX_RETRIES)
    return False


def send_alert(current_value: int, threshold: int, **kwargs) -> bool:
    """Send threshold-crossed alert."""
    now = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d %H:%M")
    message = (
        f"🚨 <b>MBH Bank – Downdetector riasztás</b>\n\n"
        f"Bejelentett hibák száma: <b>{current_value}</b> (küszöb: {threshold})\n"
        f"Időpont: {now}\n\n"
        f'<a href="{config.DOWNDETECTOR_URL}">Downdetector oldal</a>'
    )
    return _send_telegram(message, msg_type="alert", **kwargs)


def send_retroactive_alert(
    spike_value: int,
    spike_time: str,
    current_value: int,
    threshold: int,
    **kwargs,
) -> bool:
    """Send retroactive alert when a between-checks spike crossed the threshold."""
    now = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d %H:%M")
    message = (
        f"⚡ <b>MBH Bank – Visszamenőleges riasztás</b>\n\n"
        f"A két lekérdezés között csúcsérték lépte át a küszöböt.\n\n"
        f"Csúcsérték: <b>{spike_value}</b> ({spike_time})\n"
        f"Aktuális érték: <b>{current_value}</b>\n"
        f"Küszöb: {threshold}\n\n"
        f"A csúcsérték már a küszöb alá esett, nincs aktív riasztás.\n"
        f"Észlelés ideje: {now}\n\n"
        f'<a href="{config.DOWNDETECTOR_URL}">Downdetector oldal</a>'
    )
    return _send_telegram(message, msg_type="retroactive_alert", **kwargs)


def send_recovery(current_value: int, threshold: int, **kwargs) -> bool:
    """Send recovery notification."""
    now = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d %H:%M")
    message = (
        f"✅ <b>MBH Bank – Helyreállás</b>\n\n"
        f"Bejelentett hibák száma: <b>{current_value}</b> (küszöb: {threshold})\n"
        f"A hibaszám visszatért a normális szintre.\n"
        f"Időpont: {now}\n\n"
        f'<a href="{config.DOWNDETECTOR_URL}">Downdetector oldal</a>'
    )
    return _send_telegram(message, msg_type="recovery", **kwargs)


def send_heartbeat(
    current_value: int,
    threshold: int,
    last_checked: str,
    *,
    data_time: str | None = None,
    strategy: str | None = None,
    **kwargs,
) -> bool:
    """Send daily heartbeat status message."""
    value_str = f"<b>{current_value}</b>"
    if data_time:
        value_str += f" ({data_time}-es adat)"
    if strategy and strategy != "rsc":
        status_line = f"Lekérdezés: fallback parser (<code>{strategy}</code>)"
    else:
        status_line = "Lekérdezés: rendben"
    message = (
        f"<b>MBH Monitor heartbeat</b>\n\n"
        f"Aktuális reports: {value_str}\n"
        f"Küszöb: {threshold}\n"
        f"{status_line}\n"
        f"Utolsó ellenőrzés: {last_checked}"
    )
    return _send_telegram(message, msg_type="heartbeat", **kwargs)


def send_daily_summary(
    current_value: int,
    threshold: int,
    daily_max: int,
    daily_max_time: str | None,
    alert_times: list[str],
    warnings: list[str] | None = None,
    fetch_stats: tuple[int, int] | None = None,
    **kwargs,
) -> bool:
    """Send end-of-day summary with daily max, alerts, and current state.

    Args:
        fetch_stats: Optional (total_fetches, failed_fetches) tuple for reliability display.
    """
    alert_section = ""
    if alert_times:
        times_str = ", ".join(alert_times)
        alert_section = f"Riasztás volt: igen ({times_str})\n"
    else:
        alert_section = "Riasztás volt: nem\n"

    max_time_str = f" ({daily_max_time})" if daily_max_time else ""

    reliability_section = ""
    if fetch_stats and fetch_stats[0] > 0:
        total, failed = fetch_stats
        success_rate = ((total - failed) / total) * 100
        reliability_section = f"Napi SLA szint: {success_rate:.0f}% ({total - failed}/{total} sikeres)\n"

    warning_section = ""
    if warnings:
        warning_section = "\n" + "\n".join(f"⚠️ {w}" for w in warnings) + "\n"

    message = (
        f"<b>MBH Monitor – napi összefoglaló</b>\n\n"
        f"Napi max: <b>{daily_max}</b>{max_time_str}\n"
        f"Aktuális: <b>{current_value}</b>\n"
        f"Küszöb: {threshold}\n"
        f"{alert_section}"
        f"{reliability_section}"
        f"{warning_section}\n"
        f'<a href="{config.DOWNDETECTOR_URL}">Downdetector oldal</a>'
    )
    return _send_telegram(message, msg_type="daily_summary", **kwargs)


def send_parse_degradation_alert(strategy: str, current_value: int, **kwargs) -> bool:
    """Alert when RSC parse strategy fails and a fallback is used."""
    now = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d %H:%M")
    message = (
        f"⚠️ <b>MBH Monitor – Parse degradáció</b>\n\n"
        f"Az elsődleges RSC adatkinyerés nem működik.\n"
        f"Fallback stratégia: <code>{strategy}</code>\n"
        f"Aktuális reports: <b>{current_value}</b>\n"
        f"Időpont: {now}\n\n"
        f"A Downdetector HTML formátuma valószínűleg megváltozott. "
        f"Ellenőrizd a debug HTML-t a GitHub Actions artifactok között."
    )
    return _send_telegram(message, msg_type="parse_degradation", **kwargs)


def send_remediation_report(
    success: bool,
    error_category: str,
    consecutive_failures: int,
    attempts: list[dict],
    strategy_used: str | None = None,
    duration_s: float = 0.0,
    **kwargs,
) -> bool:
    """Send remediation result report (success or failure variant).

    Args:
        success: Whether remediation succeeded.
        error_category: Human-readable error category.
        consecutive_failures: Current consecutive failure count.
        attempts: List of dicts with keys: strategy, result, duration_s, error.
        strategy_used: Which strategy succeeded (if success=True).
        duration_s: Total remediation duration.
    """
    now = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d %H:%M")

    attempts_lines = []
    for a in attempts:
        line = f"  {'✅' if a['result'] == 'SUCCESS' else '⏭️' if a['result'] == 'SKIPPED' else '❌'} "
        line += f"<code>{a['strategy']}</code>: {a['result']}"
        if a.get("duration_s"):
            line += f" ({a['duration_s']:.1f}s)"
        if a.get("error"):
            line += f"\n    <i>{a['error'][:100]}</i>"
        attempts_lines.append(line)

    attempts_text = "\n".join(attempts_lines) if attempts_lines else "  (nincs)"

    if success:
        message = (
            f"🔧 <b>MBH Monitor – Auto-remediation sikeres</b>\n\n"
            f"Hiba kategória: <code>{error_category}</code>\n"
            f"Egymás utáni hibák: {consecutive_failures}\n"
            f"Sikeres stratégia: <code>{strategy_used}</code>\n"
            f"Időtartam: {duration_s:.1f}s\n\n"
            f"<b>Próbálkozások:</b>\n{attempts_text}\n\n"
            f"Időpont: {now}"
        )
    else:
        message = (
            f"🔴 <b>MBH Monitor – Auto-remediation sikertelen</b>\n\n"
            f"Hiba kategória: <code>{error_category}</code>\n"
            f"Egymás utáni hibák: <b>{consecutive_failures}</b>\n"
            f"Időtartam: {duration_s:.1f}s\n\n"
            f"<b>Próbálkozások:</b>\n{attempts_text}\n\n"
            f"<b>Tennivaló:</b>\n"
            f"  1. Ellenőrizd a GitHub Actions logokat\n"
            f"  2. Nézd meg a Downdetector oldalt böngészőben\n"
            f"  3. Ha CF blokkol, fontold meg a solver/proxy cserét\n\n"
            f"Időpont: {now}"
        )

    return _send_telegram(message, msg_type="remediation_report", **kwargs)


def send_zenrows_credit_warning(credits_remaining: int, **kwargs) -> bool:
    """Warn when ZenRows credits are running low."""
    now = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d %H:%M")
    message = (
        f"⚠️ <b>MBH Monitor – ZenRows kredit alacsony</b>\n\n"
        f"Hátralévő kreditek: <b>{credits_remaining}</b>\n"
        f"A monitor ZenRows-t használ fallback-ként a scraping hibák javításához.\n"
        f"Ha a kreditek elfogynak, a fallback nem fog működni.\n\n"
        f"<b>Tennivaló:</b> Töltsd fel a ZenRows egyenleget vagy válts csomagot.\n"
        f"Időpont: {now}"
    )
    return _send_telegram(message, msg_type="zenrows_credit_warning", **kwargs)


def send_fetch_recovery(
    previous_failures: int,
    current_value: int,
    strategy: str,
    **kwargs,
) -> bool:
    """Notify that fetching recovered after consecutive failures."""
    now = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d %H:%M")
    message = (
        f"✅ <b>MBH Monitor – Scraping helyreállt</b>\n\n"
        f"A monitor újra sikeresen le tudja kérdezni az adatokat.\n"
        f"Korábbi sikertelen lekérdezések: <b>{previous_failures}</b>\n"
        f"Aktuális reports: <b>{current_value}</b>\n"
        f"Stratégia: <code>{strategy}</code>\n"
        f"Időpont: {now}\n\n"
        f'<a href="{config.DOWNDETECTOR_URL}">Downdetector oldal</a>'
    )
    return _send_telegram(message, msg_type="fetch_recovery", **kwargs)
