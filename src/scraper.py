"""Downdetector scraper – fetches current report count via FlareSolverr."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from curl_cffi import requests as cf_requests

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


def _extract_chrome_version(user_agent: str) -> int | None:
    """Extract major Chrome version from User-Agent string."""
    match = re.search(r'Chrome/(\d+)', user_agent)
    return int(match.group(1)) if match else None


@dataclass
class _FlareSolverrResult:
    """Internal result from FlareSolverr: cookies, user agent, and response HTML."""
    cookies: dict[str, str]
    user_agent: str
    response_html: str


def _get_cf_cookies(url: str, timeout: int) -> _FlareSolverrResult:
    """Use FlareSolverr to solve Cloudflare and return bypass cookies + response HTML."""
    flaresolverr_url = config.FLARESOLVERR_URL
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": timeout * 1000,
    }

    resp = requests.post(flaresolverr_url, json=payload, timeout=timeout + 30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "ok":
        raise FetchError(f"FlareSolverr error: {data.get('message', 'unknown')}")

    cookies = {}
    for cookie in data["solution"].get("cookies", []):
        cookies[cookie["name"]] = cookie["value"]

    user_agent = data["solution"].get("userAgent", "")
    response_html = data["solution"].get("response", "")
    chrome_ver = _extract_chrome_version(user_agent)
    ver_info = f" (Chrome {chrome_ver})" if chrome_ver else ""
    logger.info("Got %d cookies from FlareSolverr%s, impersonate=%s, response=%d bytes",
                len(cookies), ver_info, config.CURL_CFFI_IMPERSONATE, len(response_html))
    return _FlareSolverrResult(cookies=cookies, user_agent=user_agent, response_html=response_html)


def fetch_html(url: str, *, timeout: int | None = None) -> str:
    """Fetch raw HTML: get Cloudflare cookies via FlareSolverr, then fetch with curl_cffi.

    Two-step approach prefers raw SSR HTML (with RSC data) from curl_cffi.
    If curl_cffi gets 403 (Cloudflare blocks the IP), falls back to using
    FlareSolverr's rendered HTML directly.
    """
    timeout = timeout or config.HTTP_TIMEOUT

    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            # Step 1: Get Cloudflare bypass cookies + response HTML
            fs_result = _get_cf_cookies(url, timeout)

            # Step 2: Try raw HTML with curl_cffi (Chrome TLS fingerprint)
            headers = {"User-Agent": fs_result.user_agent} if fs_result.user_agent else {}
            resp = cf_requests.get(
                url, cookies=fs_result.cookies, headers=headers,
                timeout=timeout, impersonate=config.CURL_CFFI_IMPERSONATE,
            )

            if resp.status_code == 403:
                cf_ray = resp.headers.get("cf-ray", "unknown")
                logger.warning(
                    "curl_cffi got 403 (cf-ray=%s, impersonate=%s), falling back to FlareSolverr HTML",
                    cf_ray, config.CURL_CFFI_IMPERSONATE,
                )
                if fs_result.response_html:
                    logger.info("Using FlareSolverr response HTML (%d bytes)", len(fs_result.response_html))
                    return fs_result.response_html
                raise FetchError(f"HTTP 403 and FlareSolverr returned empty HTML | cf-ray={cf_ray}")

            resp.raise_for_status()

            html = resp.text
            logger.info("Fetched %d bytes of raw HTML via curl_cffi", len(html))
            return html

        except FetchError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES - 1:
                wait = config.RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning("Attempt %d failed (%s), retrying in %.1fs", attempt + 1, exc, wait)
                time.sleep(wait)

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
