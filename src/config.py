"""Configuration module – reads env vars, provides defaults."""

from __future__ import annotations

import os


DOWNDETECTOR_URL: str = os.environ.get(
    "DOWNDETECTOR_URL",
    "https://downdetector.hu/problema/mbh-bank/",
)

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

ALERT_THRESHOLD: int = int(os.environ.get("ALERT_THRESHOLD", "30"))

STATE_FILE: str = os.environ.get("STATE_FILE", "state.json")

HTTP_TIMEOUT: int = 15

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MAX_RETRIES: int = 3
RETRY_BACKOFF_BASE: float = 2.0

CONSECUTIVE_FAILURE_ALERT_THRESHOLD: int = 3


def validate() -> list[str]:
    """Return list of config errors (empty = OK)."""
    errors: list[str] = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is not set")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID is not set")
    if ALERT_THRESHOLD < 0:
        errors.append(f"ALERT_THRESHOLD must be >= 0, got {ALERT_THRESHOLD}")
    return errors
