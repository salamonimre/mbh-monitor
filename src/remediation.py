"""Auto-remediation – alternative fetch strategies when solver fails."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import requests

from src import config
from src.scraper import FetchError, ParseResult, parse_reports
from src.state import State

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    SOLVER_UNREACHABLE = "solver_unreachable"
    CLOUDFLARE_BLOCK = "cloudflare_block"
    RATE_LIMITED = "rate_limited"
    ZENROWS_CREDITS = "zenrows_credits"
    TARGET_DOWN = "target_down"
    NETWORK_ERROR = "network_error"
    PARSE_FAILURE = "parse_failure"
    UNKNOWN = "unknown"


@dataclass
class AttemptDetail:
    strategy: str
    result: str  # "SUCCESS", "FAILED", "SKIPPED"
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class RemediationResult:
    success: bool
    html: str | None = None
    parse_result: ParseResult | None = None
    strategy_used: str | None = None
    error_category: ErrorCategory = ErrorCategory.UNKNOWN
    attempts: list[AttemptDetail] = field(default_factory=list)
    duration_s: float = 0.0
    zenrows_credits_remaining: int | None = None


def classify_error(exc: Exception) -> ErrorCategory:
    """Classify an exception into an ErrorCategory based on message and type."""
    msg = str(exc).lower()

    if "unreachable" in msg or "connection refused" in msg or "connectionerror" in msg:
        return ErrorCategory.SOLVER_UNREACHABLE
    if "challenge" in msg or "cloudflare" in msg or "403" in msg:
        return ErrorCategory.CLOUDFLARE_BLOCK
    if "429" in msg or "rate limit" in msg or "too many" in msg:
        return ErrorCategory.RATE_LIMITED
    if "credit" in msg or "quota" in msg or "limit exceeded" in msg:
        return ErrorCategory.ZENROWS_CREDITS
    if "502" in msg or "503" in msg or "504" in msg or "target" in msg:
        return ErrorCategory.TARGET_DOWN
    if isinstance(exc, (requests.ConnectionError, ConnectionError, OSError)):
        return ErrorCategory.NETWORK_ERROR
    if "parse" in msg or "no parse strategy" in msg:
        return ErrorCategory.PARSE_FAILURE

    return ErrorCategory.UNKNOWN


def _get_cooldown_minutes(fail_count: int) -> int:
    """Progressive cooldown: 30min per consecutive fail, capped at REMEDIATION_COOLDOWN_MINUTES."""
    return min(30 * fail_count, config.REMEDIATION_COOLDOWN_MINUTES)


def _is_on_cooldown(strategy_name: str, state: State) -> int | None:
    """Check if a strategy is on cooldown. Returns remaining minutes or None.

    Uses progressive cooldown: longer wait for repeated failures.
    """
    attempt_data = state.remediation_attempts.get(strategy_name)
    if not attempt_data:
        return None
    if attempt_data.get("last_result") != "fail":
        return None

    last_tried = attempt_data.get("last_tried")
    if not last_tried:
        return None

    try:
        last_dt = datetime.fromisoformat(last_tried)
    except (ValueError, TypeError):
        return None

    fail_count = attempt_data.get("fail_count", 1)
    cooldown = _get_cooldown_minutes(fail_count)

    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
    remaining = cooldown - elapsed
    if remaining > 0:
        return int(remaining)
    return None


def _record_attempt(strategy_name: str, success: bool, state: State) -> None:
    """Record a remediation attempt result in state."""
    entry = state.remediation_attempts.get(strategy_name, {})
    entry["last_tried"] = datetime.now(timezone.utc).isoformat()
    entry["last_result"] = "success" if success else "fail"
    entry["fail_count"] = 0 if success else entry.get("fail_count", 0) + 1
    state.remediation_attempts[strategy_name] = entry


# Module-level credit tracker – updated by ZenRows strategies
_last_zenrows_remaining: int | None = None


def _parse_zenrows_credits(resp: requests.Response) -> None:
    """Extract remaining credits from ZenRows response header."""
    global _last_zenrows_remaining
    remaining = resp.headers.get("X-Zen-Remaining-Requests")
    if remaining and remaining != "?":
        try:
            _last_zenrows_remaining = int(remaining)
        except ValueError:
            pass


def _strategy_zenrows_no_premium(url: str) -> str:
    """ZenRows without premium proxy (cheaper, 1 credit)."""
    if not config.ZENROWS_API_KEY:
        raise FetchError("No ZenRows API key configured")

    params = {
        "url": url,
        "apikey": config.ZENROWS_API_KEY,
        "js_render": "true",
    }
    resp = requests.get("https://api.zenrows.com/v1/", params=params, timeout=120)
    resp.raise_for_status()
    html = resp.text

    if not html or len(html) < 100:
        raise FetchError("ZenRows (no premium) returned empty/minimal response")

    _parse_zenrows_credits(resp)
    remaining = resp.headers.get("X-Zen-Remaining-Requests", "?")
    logger.info("ZenRows (no premium) fetched %d bytes (remaining: %s)", len(html), remaining)
    return html


def _strategy_zenrows_premium_hu(url: str) -> str:
    """ZenRows with premium proxy + HU country (most reliable CF bypass, 10-25 credits)."""
    if not config.ZENROWS_API_KEY:
        raise FetchError("No ZenRows API key configured")

    params = {
        "url": url,
        "apikey": config.ZENROWS_API_KEY,
        "js_render": "true",
        "premium_proxy": "true",
    }
    if config.ZENROWS_PROXY_COUNTRY:
        params["proxy_country"] = config.ZENROWS_PROXY_COUNTRY

    resp = requests.get("https://api.zenrows.com/v1/", params=params, timeout=120)
    resp.raise_for_status()
    html = resp.text

    if not html or len(html) < 100:
        raise FetchError("ZenRows (premium HU) returned empty/minimal response")

    _parse_zenrows_credits(resp)
    remaining = resp.headers.get("X-Zen-Remaining-Requests", "?")
    logger.info("ZenRows (premium HU) fetched %d bytes (remaining: %s)", len(html), remaining)
    return html


def _strategy_zenrows_alt_country(url: str) -> str:
    """ZenRows with alternative country proxy (DE, AT, US rotation)."""
    if not config.ZENROWS_API_KEY:
        raise FetchError("No ZenRows API key configured")

    alt_countries = ["DE", "AT", "US"]
    # Exclude the default country
    alt_countries = [c for c in alt_countries if c != config.ZENROWS_PROXY_COUNTRY]

    last_exc: Exception | None = None
    for country in alt_countries:
        try:
            params = {
                "url": url,
                "apikey": config.ZENROWS_API_KEY,
                "js_render": "true",
                "premium_proxy": "true",
                "proxy_country": country,
            }
            resp = requests.get("https://api.zenrows.com/v1/", params=params, timeout=120)
            resp.raise_for_status()
            html = resp.text

            if not html or len(html) < 100:
                raise FetchError(f"ZenRows ({country}) returned empty/minimal response")

            _parse_zenrows_credits(resp)
            remaining = resp.headers.get("X-Zen-Remaining-Requests", "?")
            logger.info("ZenRows (country=%s) fetched %d bytes (remaining: %s)",
                        country, len(html), remaining)
            return html
        except Exception as exc:
            logger.warning("ZenRows alt country %s failed: %s", country, exc)
            last_exc = exc

    raise last_exc or FetchError("All ZenRows alt country attempts failed")


def _strategy_direct_request(url: str) -> str:
    """Direct HTTP request without any solver (plain requests.get with browser UA)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    html = resp.text

    if not html or len(html) < 100:
        raise FetchError("Direct request returned empty/minimal response")

    logger.info("Direct request fetched %d bytes", len(html))
    return html


# Strategy registry: (name, function_name, applicable_categories)
# Order: cheapest first, most reliable in the middle, free last resort.
# Uses function names (not references) so patching works in tests.
_STRATEGIES: list[tuple[str, str, set[ErrorCategory]]] = [
    (
        "zenrows_no_premium",
        "_strategy_zenrows_no_premium",
        {ErrorCategory.CLOUDFLARE_BLOCK, ErrorCategory.SOLVER_UNREACHABLE,
         ErrorCategory.RATE_LIMITED, ErrorCategory.NETWORK_ERROR, ErrorCategory.UNKNOWN},
    ),
    (
        "zenrows_premium_hu",
        "_strategy_zenrows_premium_hu",
        {ErrorCategory.CLOUDFLARE_BLOCK, ErrorCategory.SOLVER_UNREACHABLE,
         ErrorCategory.RATE_LIMITED, ErrorCategory.NETWORK_ERROR, ErrorCategory.UNKNOWN},
    ),
    (
        "zenrows_alt_country",
        "_strategy_zenrows_alt_country",
        {ErrorCategory.CLOUDFLARE_BLOCK, ErrorCategory.RATE_LIMITED,
         ErrorCategory.SOLVER_UNREACHABLE, ErrorCategory.UNKNOWN},
    ),
    (
        "direct_request",
        "_strategy_direct_request",
        {ErrorCategory.SOLVER_UNREACHABLE, ErrorCategory.CLOUDFLARE_BLOCK,
         ErrorCategory.NETWORK_ERROR, ErrorCategory.RATE_LIMITED,
         ErrorCategory.TARGET_DOWN, ErrorCategory.UNKNOWN},
    ),
]

# Module reference for dynamic lookup
import sys as _sys
_THIS_MODULE = _sys.modules[__name__]


def attempt_remediation(url: str, original_error: Exception, state: State) -> RemediationResult:
    """Try alternative fetch strategies after primary fetch fails.

    Called immediately on first failure. Strategies are tried in order,
    skipping those on progressive cooldown.

    Args:
        url: The URL to fetch.
        original_error: The exception from the primary fetch chain.
        state: Current state (for cooldown tracking).

    Returns:
        RemediationResult with success status, HTML/parse result if successful.
    """
    global _last_zenrows_remaining
    _last_zenrows_remaining = None

    start = time.monotonic()
    category = classify_error(original_error)
    failures = state.consecutive_fetch_failures

    logger.info("Remediation started | category=%s | failures=%d | error=%s",
                category.value, failures, str(original_error)[:200])

    attempts: list[AttemptDetail] = []

    for strategy_name, strategy_fn_name, applicable_categories in _STRATEGIES:
        strategy_fn = getattr(_THIS_MODULE, strategy_fn_name)
        # Check applicability
        if category not in applicable_categories:
            continue

        # Check cooldown
        cooldown_remaining = _is_on_cooldown(strategy_name, state)
        if cooldown_remaining is not None:
            logger.info("Strategy %s: SKIPPED (cooldown, %dmin remaining)",
                        strategy_name, cooldown_remaining)
            attempts.append(AttemptDetail(
                strategy=strategy_name,
                result="SKIPPED",
                error=f"cooldown ({cooldown_remaining}min remaining)",
            ))
            continue

        # Try strategy
        logger.info("Strategy %s: TRYING", strategy_name)
        attempt_start = time.monotonic()
        try:
            html = strategy_fn(url)
            # Validate by parsing
            parse_result = parse_reports(html)
            duration = time.monotonic() - attempt_start

            logger.info(
                "Strategy %s: SUCCESS (%.1fs) | html_size=%d | parse=%s | points=%d",
                strategy_name, duration, len(html), parse_result.strategy, len(parse_result.points),
            )

            _record_attempt(strategy_name, True, state)
            state.remediation_last_success = strategy_name

            total_duration = time.monotonic() - start
            attempts.append(AttemptDetail(
                strategy=strategy_name, result="SUCCESS", duration_s=duration,
            ))

            if _last_zenrows_remaining is not None:
                state.zenrows_credits_remaining = _last_zenrows_remaining

            logger.info("Remediation complete | result=SUCCESS | strategy=%s | attempts=%d | duration=%.1fs | credits=%s",
                        strategy_name, len(attempts), total_duration,
                        _last_zenrows_remaining if _last_zenrows_remaining is not None else "n/a")

            return RemediationResult(
                success=True,
                html=html,
                parse_result=parse_result,
                strategy_used=strategy_name,
                error_category=category,
                attempts=attempts,
                duration_s=total_duration,
                zenrows_credits_remaining=_last_zenrows_remaining,
            )

        except Exception as exc:
            duration = time.monotonic() - attempt_start
            logger.warning("Strategy %s: FAILED (%.1fs) | error=%s",
                           strategy_name, duration, exc)
            _record_attempt(strategy_name, False, state)
            attempts.append(AttemptDetail(
                strategy=strategy_name, result="FAILED",
                duration_s=duration, error=str(exc)[:200],
            ))

    total_duration = time.monotonic() - start
    tried = sum(1 for a in attempts if a.result != "SKIPPED")
    skipped = sum(1 for a in attempts if a.result == "SKIPPED")

    logger.error("Remediation complete | result=FAILED | attempts=%d | skipped=%d | duration=%.1fs",
                 tried, skipped, total_duration)
    logger.error("Remediation report | category=%s | strategies_tried=%s",
                 category.value,
                 ",".join(a.strategy for a in attempts if a.result != "SKIPPED"))

    return RemediationResult(
        success=False,
        error_category=category,
        attempts=attempts,
        duration_s=total_duration,
    )
