"""Downdetector scraper – fetches current report count via FlareSolverr."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from src import config

logger = logging.getLogger(__name__)


class ParseError(Exception):
    """Raised when no parse strategy can extract data."""


class FetchError(Exception):
    """Raised when all fetch attempts fail."""


@dataclass
class ReportPoint:
    timestamp: datetime
    value: int


@dataclass
class ParseResult:
    """Result of parsing report data, including which strategy succeeded."""
    points: list[ReportPoint]
    strategy: str  # "rsc", "json_anywhere", "aria_label", "heading"


@dataclass
class _FlareSolverrResult:
    """Internal result from FlareSolverr."""
    response_html: str
    user_agent: str


def _create_session(session_id: str) -> None:
    """Create a fresh FlareSolverr browser session."""
    payload = {"cmd": "sessions.create", "session": session_id}
    resp = requests.post(config.FLARESOLVERR_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise FetchError(f"Session create failed: {data.get('message', 'unknown')}")
    logger.info("FlareSolverr session created: %s", session_id)


def _destroy_session(session_id: str) -> None:
    """Destroy a FlareSolverr browser session (best-effort, never raises)."""
    try:
        payload = {"cmd": "sessions.destroy", "session": session_id}
        resp = requests.post(config.FLARESOLVERR_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("FlareSolverr session destroyed: %s", session_id)
        else:
            logger.warning("Session destroy returned %d for %s", resp.status_code, session_id)
    except Exception as exc:
        logger.warning("Session destroy failed for %s: %s", session_id, exc)


def _flaresolverr_fetch(url: str, *, session_id: str | None = None) -> _FlareSolverrResult:
    """Use FlareSolverr to fetch a URL, solving Cloudflare challenges.

    Args:
        url: The URL to fetch.
        session_id: Optional FlareSolverr session ID for browser instance isolation.

    Returns:
        _FlareSolverrResult with the page HTML and user agent.
    """
    payload: dict = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": config.FLARESOLVERR_MAX_TIMEOUT,
    }
    if session_id:
        payload["session"] = session_id

    http_timeout = config.FLARESOLVERR_MAX_TIMEOUT / 1000 + 30

    resp = requests.post(config.FLARESOLVERR_URL, json=payload, timeout=http_timeout)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "ok":
        raise FetchError(f"FlareSolverr error: {data.get('message', 'unknown')}")

    solution = data.get("solution", {})
    response_html = solution.get("response", "")
    user_agent = solution.get("userAgent", "")

    if not response_html:
        raise FetchError("FlareSolverr returned empty response HTML")

    logger.info("FlareSolverr fetched %d bytes (session=%s)",
                len(response_html), session_id or "default")
    return _FlareSolverrResult(response_html=response_html, user_agent=user_agent)


def fetch_html(url: str, *, timeout: int | None = None) -> str:
    """Fetch HTML via FlareSolverr with session rotation.

    Each retry attempt creates a fresh FlareSolverr browser session
    to avoid Cloudflare fingerprint-based blocking. Sessions are always
    cleaned up in the finally block.
    """
    last_exc: Exception | None = None

    for attempt in range(config.MAX_RETRIES):
        session_id = f"mbh-{uuid.uuid4().hex[:12]}"
        try:
            _create_session(session_id)
            result = _flaresolverr_fetch(url, session_id=session_id)
            logger.info("Fetched %d bytes via FlareSolverr (attempt %d, session %s)",
                        len(result.response_html), attempt + 1, session_id)
            return result.response_html

        except Exception as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES - 1:
                wait = config.RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning("Attempt %d failed (%s), retrying in %.1fs",
                               attempt + 1, exc, wait)
                time.sleep(wait)
        finally:
            _destroy_session(session_id)

    raise last_exc or FetchError("All retries exhausted")


def _parse_rsc_strategy(html: str) -> list[ReportPoint]:
    """Extract chart data from Next.js RSC stream (most accurate).

    The Downdetector Next.js app embeds chart data as dataPoints
    with timestampUtc and reportsValue in __next_f.push() calls.
    """
    # Find all RSC push payloads
    pushes = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
    logger.info("RSC strategy: found %d __next_f.push payloads", len(pushes))

    decoder = json.JSONDecoder()

    for push in pushes:
        # Unescape RSC content
        content = push.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')

        if '"reportsValue"' not in content:
            continue

        # Find "dataPoints" key and parse the array using raw_decode
        # to handle extra fields in the parent object
        dp_key = '"dataPoints"'
        idx = content.find(dp_key)
        if idx < 0:
            continue

        # Find the '[' that starts the array
        bracket_idx = content.find('[', idx + len(dp_key))
        if bracket_idx < 0:
            continue

        try:
            data_points, _ = decoder.raw_decode(content, bracket_idx)
        except json.JSONDecodeError:
            continue

        if not isinstance(data_points, list) or not data_points:
            continue

        points = []
        for dp in data_points:
            try:
                ts = datetime.fromisoformat(dp["timestampUtc"])
                value = int(dp["reportsValue"])
                points.append(ReportPoint(timestamp=ts, value=value))
            except (KeyError, ValueError):
                continue

        if points:
            return sorted(points, key=lambda p: p.timestamp)

    return []


def _parse_json_anywhere_strategy(html: str) -> list[ReportPoint]:
    """Fallback: find any JSON array containing timestampUtc/reportsValue anywhere in HTML.

    This handles cases where Downdetector changes the RSC delivery mechanism
    but keeps the same data structure.
    """
    decoder = json.JSONDecoder()
    # Search for array-like patterns that might contain report data
    for match in re.finditer(r'\[(?=\s*\{)', html):
        try:
            arr, _ = decoder.raw_decode(html, match.start())
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(arr, list) or len(arr) < 2:
            continue

        # Check if this looks like report data
        sample = arr[0]
        if not isinstance(sample, dict):
            continue
        if "timestampUtc" not in sample or "reportsValue" not in sample:
            continue

        points = []
        for dp in arr:
            try:
                ts = datetime.fromisoformat(dp["timestampUtc"])
                value = int(dp["reportsValue"])
                points.append(ReportPoint(timestamp=ts, value=value))
            except (KeyError, ValueError, TypeError):
                continue

        if points:
            return sorted(points, key=lambda p: p.timestamp)

    return []


def _parse_aria_label_strategy(html: str) -> list[ReportPoint]:
    """Fallback: extract peak from chart aria-label."""
    match = re.search(
        r'aria-label="Reports chart[^"]*?peak of (\d+) reports',
        html,
        re.IGNORECASE,
    )
    if match:
        value = int(match.group(1))
        return [ReportPoint(timestamp=datetime.now(timezone.utc), value=value)]
    return []


def _parse_heading_strategy(html: str) -> list[ReportPoint]:
    """Fallback: 'no current problems' heading → 0 reports."""
    if re.search(r'no current problems', html, re.IGNORECASE):
        return [ReportPoint(timestamp=datetime.now(timezone.utc), value=0)]
    return []


def parse_reports(html: str) -> ParseResult:
    """Parse HTML using strategy chain: RSC -> JSON anywhere -> aria-label -> heading -> error.

    Returns ParseResult with points and strategy name.
    Raises ParseError if no strategy succeeds.
    """
    # Strategy 1: RSC dataPoints (precise, per-interval values)
    points = _parse_rsc_strategy(html)
    if points:
        logger.info(
            "Parsed %d data points via RSC strategy (latest: %d at %s)",
            len(points), points[-1].value, points[-1].timestamp.isoformat(),
        )
        logger.info("Recent points: %s",
                     ", ".join("%d@%s" % (p.value, p.timestamp.strftime('%H:%M')) for p in points[-5:]))
        return ParseResult(points=points, strategy="rsc")

    # Strategy 2: JSON anywhere (RSC delivery changed but data structure intact)
    points = _parse_json_anywhere_strategy(html)
    if points:
        logger.warning(
            "Parsed %d data points via json_anywhere fallback (latest: %d at %s)",
            len(points), points[-1].value, points[-1].timestamp.isoformat(),
        )
        return ParseResult(points=points, strategy="json_anywhere")

    # Strategy 3: aria-label peak (approximate, 24h peak)
    points = _parse_aria_label_strategy(html)
    if points:
        logger.warning("Parsed via aria-label fallback (24h peak): %d", points[-1].value)
        return ParseResult(points=points, strategy="aria_label")

    # Strategy 4: heading status
    points = _parse_heading_strategy(html)
    if points:
        logger.info("Parsed via heading strategy: no current problems")
        return ParseResult(points=points, strategy="heading")

    # Strategy 5: fail
    raise ParseError("No parse strategy could extract report data from HTML")


def fetch_report_data(url: str | None = None) -> ParseResult:
    """Fetch and return parsed chart data. Main entry point."""
    url = url or config.DOWNDETECTOR_URL
    html = fetch_html(url)
    return parse_reports(html)


def get_current_value(url: str | None = None) -> int:
    """Fetch and return the current report count (last data point)."""
    return fetch_report_data(url).points[-1].value
