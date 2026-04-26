"""Telegram notification sender."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from src import config

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _send_telegram(
    message: str,
    *,
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
                logger.info("Telegram message sent successfully")
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
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = (
        f"🚨 <b>MBH Bank – Downdetector riasztás</b>\n\n"
        f"Bejelentett hibák száma: <b>{current_value}</b> (küszöb: {threshold})\n"
        f"Időpont: {now}\n\n"
        f'<a href="{config.DOWNDETECTOR_URL}">Downdetector oldal</a>'
    )
    return _send_telegram(message, **kwargs)


def send_recovery(current_value: int, threshold: int, **kwargs) -> bool:
    """Send recovery notification."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = (
        f"✅ <b>MBH Bank – Helyreállás</b>\n\n"
        f"Bejelentett hibák száma: <b>{current_value}</b> (küszöb: {threshold})\n"
        f"A hibaszám visszatért a normális szintre.\n"
        f"Időpont: {now}\n\n"
        f'<a href="{config.DOWNDETECTOR_URL}">Downdetector oldal</a>'
    )
    return _send_telegram(message, **kwargs)


def send_heartbeat(current_value: int, threshold: int, last_checked: str, *, data_time: str | None = None, **kwargs) -> bool:
    """Send daily heartbeat status message."""
    value_str = f"<b>{current_value}</b>"
    if data_time:
        value_str += f" ({data_time}-es adat)"
    message = (
        f"<b>MBH Monitor heartbeat</b>\n\n"
        f"Aktuális reports: {value_str}\n"
        f"Küszöb: {threshold}\n"
        f"Utolsó ellenőrzés: {last_checked}"
    )
    return _send_telegram(message, **kwargs)


def send_daily_summary(
    current_value: int,
    threshold: int,
    daily_max: int,
    daily_max_time: str | None,
    alert_times: list[str],
    warnings: list[str] | None = None,
    **kwargs,
) -> bool:
    """Send end-of-day summary with daily max, alerts, and current state."""
    alert_section = ""
    if alert_times:
        times_str = ", ".join(alert_times)
        alert_section = f"Riasztás volt: igen ({times_str})\n"
    else:
        alert_section = "Riasztás volt: nem\n"

    max_time_str = f" ({daily_max_time})" if daily_max_time else ""

    warning_section = ""
    if warnings:
        warning_section = "\n" + "\n".join(f"⚠️ {w}" for w in warnings) + "\n"

    message = (
        f"<b>MBH Monitor – napi összefoglaló</b>\n\n"
        f"Napi max: <b>{daily_max}</b>{max_time_str}\n"
        f"Aktuális: <b>{current_value}</b>\n"
        f"Küszöb: {threshold}\n"
        f"{alert_section}"
        f"{warning_section}\n"
        f'<a href="{config.DOWNDETECTOR_URL}">Downdetector oldal</a>'
    )
    return _send_telegram(message, **kwargs)


def send_parse_degradation_alert(strategy: str, current_value: int, **kwargs) -> bool:
    """Alert when RSC parse strategy fails and a fallback is used."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = (
        f"⚠️ <b>MBH Monitor – Parse degradáció</b>\n\n"
        f"Az elsődleges RSC adatkinyerés nem működik.\n"
        f"Fallback stratégia: <code>{strategy}</code>\n"
        f"Aktuális reports: <b>{current_value}</b>\n"
        f"Időpont: {now}\n\n"
        f"A Downdetector HTML formátuma valószínűleg megváltozott. "
        f"Ellenőrizd a debug HTML-t a GitHub Actions artifactok között."
    )
    return _send_telegram(message, **kwargs)


def send_fetch_failure_alert(failures: int, error: str, **kwargs) -> bool:
    """Alert about consecutive fetch failures."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = (
        f"⚠️ <b>MBH Monitor – Scraping hiba</b>\n\n"
        f"Egymás utáni sikertelen lekérdezések: <b>{failures}</b>\n"
        f"Utolsó hiba: <code>{error[:200]}</code>\n"
        f"Időpont: {now}\n\n"
        f"A monitor nem tud adatot lekérdezni. Ellenőrizd a logokat."
    )
    return _send_telegram(message, **kwargs)
