"""Downdetector scraper – fetches current report count via FlareSolverr."""

from __future__ import annotations

import json
import logging
import re
import time
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


def _get_cf_cookies(url: str, timeout: int) -> dict[str, str]:
    """Use FlareSolverr to solve Cloudflare and return bypass cookies."""
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
    logger.info("Got %d cookies from FlareSolverr", len(cookies))
    return cookies, user_agent


def fetch_html(url: str, *, timeout: int | None = None) -> str:
    """Fetch raw HTML: get Cloudflare cookies via FlareSolverr, then fetch with requests.

    This two-step approach ensures we get the raw SSR HTML (with RSC data)
    rather than the post-hydration DOM.
    """
    timeout = timeout or config.HTTP_TIMEOUT

    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            # Step 1: Get Cloudflare bypass cookies
            cookies, user_agent = _get_cf_cookies(url, timeout)

            # Step 2: Fetch raw HTML with those cookies
            headers = {"User-Agent": user_agent} if user_agent else {}
            resp = requests.get(url, cookies=cookies, headers=headers, timeout=timeout)

            if resp.status_code == 403:
                raise FetchError(f"HTTP 403 despite cookies – Cloudflare may need re-solving")
            resp.raise_for_status()

            html = resp.text
            logger.info("Fetched %d bytes of raw HTML", len(html))
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

    for push in pushes:
        # Unescape RSC content
        content = push.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')

        if '"reportsValue"' not in content:
            continue

        # Extract dataPoints array
        match = re.search(
            r'"dataPoints":\s*\[(.*?)\]\s*\}',
            content,
            re.DOTALL,
        )
        if not match:
            continue

        try:
            arr_str = "[" + match.group(1) + "]"
            data_points = json.loads(arr_str)
        except json.JSONDecodeError:
            continue

        if not data_points:
            continue

        points = []
        for dp in data_points:
            try:
                ts = datetime.fromisoformat(dp["timestampUtc"].replace("+00:00", "+00:00"))
                value = int(dp["reportsValue"])
                points.append(ReportPoint(timestamp=ts, value=value))
            except (KeyError, ValueError):
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


def parse_reports(html: str) -> list[ReportPoint]:
    """Parse HTML using strategy chain: RSC dataPoints -> aria-label -> heading -> error.

    Returns list of ReportPoint sorted by timestamp.
    Raises ParseError if no strategy succeeds.
    """
    # Strategy 1: RSC dataPoints (precise, per-interval values)
    points = _parse_rsc_strategy(html)
    if points:
        logger.info(
            "Parsed %d data points via RSC strategy (latest: %d at %s)",
            len(points), points[-1].value, points[-1].timestamp.isoformat(),
        )
        return points

    # Strategy 2: aria-label peak (approximate, 24h peak)
    points = _parse_aria_label_strategy(html)
    if points:
        logger.warning("Parsed via aria-label fallback (24h peak): %d", points[-1].value)
        return points

    # Strategy 3: heading status
    points = _parse_heading_strategy(html)
    if points:
        logger.info("Parsed via heading strategy: no current problems")
        return points

    # Strategy 4: fail
    raise ParseError("No parse strategy could extract report data from HTML")


def get_current_value(url: str | None = None) -> int:
    """Fetch and return the current report count. Main entry point."""
    url = url or config.DOWNDETECTOR_URL
    html = fetch_html(url)
    points = parse_reports(html)
    return points[-1].value
