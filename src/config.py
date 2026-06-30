"""Configuration module – reads env vars, provides defaults."""

from __future__ import annotations

import os


DOWNDETECTOR_URL: str = os.environ.get(
    "DOWNDETECTOR_URL",
    "https://downdetector.hu/problema/mbh-bank/",
)

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

ALERT_THRESHOLD: int = int(os.environ.get("ALERT_THRESHOLD", "10"))

STATE_FILE: str = os.environ.get("STATE_FILE", "state.json")

FLARESOLVERR_URL: str = os.environ.get(
    "FLARESOLVERR_URL",
    "http://localhost:8191/v1",
)

HTTP_TIMEOUT: int = 60

MAX_RETRIES: int = int(os.environ.get("MAX_RETRIES", "5"))
RETRY_BACKOFF_BASE: float = 2.0

CONSECUTIVE_FAILURE_ALERT_THRESHOLD: int = 3  # legacy, kept for backward compat

FLARESOLVERR_MAX_TIMEOUT: int = int(os.environ.get("FLARESOLVERR_MAX_TIMEOUT", "60000"))
FLARESOLVERR_PROXY: str = os.environ.get("FLARESOLVERR_PROXY", "")

JITTER_MAX_SECONDS: float = float(os.environ.get("JITTER_MAX_SECONDS", "90"))

ZENROWS_API_KEY: str = os.environ.get("ZENROWS_API_KEY", "")
ZENROWS_PROXY_COUNTRY: str = os.environ.get("ZENROWS_PROXY_COUNTRY", "HU")

PAT_EXPIRY_DATE: str = os.environ.get("PAT_EXPIRY_DATE", "2026-07-25")
PAT_EXPIRY_WARNING_DAYS: int = 30

REMEDIATION_TRIGGER_THRESHOLD: int = int(os.environ.get("REMEDIATION_TRIGGER_THRESHOLD", "1"))
REMEDIATION_COOLDOWN_MINUTES: int = int(os.environ.get("REMEDIATION_COOLDOWN_MINUTES", "120"))

NOTIFICATION_DELAY_MINUTES: int = int(os.environ.get("NOTIFICATION_DELAY_MINUTES", "30"))
NOTIFICATION_MIN_FAILURES: int = int(os.environ.get("NOTIFICATION_MIN_FAILURES", "2"))

ZENROWS_CREDIT_WARNING_THRESHOLD: int = int(os.environ.get("ZENROWS_CREDIT_WARNING_THRESHOLD", "50"))

HISTORY_FILE: str = os.environ.get("HISTORY_FILE", "history.csv")
HISTORY_MAX_SIZE_MB: int = int(os.environ.get("HISTORY_MAX_SIZE_MB", "5"))

HEARTBEAT_ENABLED: bool = os.environ.get("HEARTBEAT_ENABLED", "true").lower() in ("true", "1", "yes")
HEARTBEAT_HOURS: list[int] = [int(h.strip()) for h in os.environ.get("HEARTBEAT_HOURS", "9,19").split(",")]


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
