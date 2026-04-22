"""Downdetector scraper – fetches current report count."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

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


def fetch_html(url: str, *, timeout: int | None = None) -> str:
    """Fetch HTML from URL using curl_cffi (Cloudflare bypass) with retries."""
    timeout = timeout or config.HTTP_TIMEOUT

    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            resp = cffi_requests.get(
                url,
                impersonate="chrome",
                timeout=timeout,
            )
            if resp.status_code == 429:
                wait = config.RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning("429 received, backing off %.1fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} for {url}")
            return resp.text
        except FetchError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES - 1:
                wait = config.RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning("Attempt %d failed (%s), retrying in %.1fs", attempt + 1, exc, wait)
                time.sleep(wait)

    raise last_exc or FetchError("All retries exhausted")


def _parse_aria_label_strategy(html: str) -> list[ReportPoint]:
    """Extract peak report count from chart aria-label (Next.js Downdetector format).

    Looks for: aria-label="Reports chart for the last 24 hours with a peak of 15 reports, status: ..."
    """
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
    """Extract status from the main heading text.

    'no current problems' → 0 reports (safe baseline).
    """
    if re.search(r'no current problems', html, re.IGNORECASE):
        return [ReportPoint(timestamp=datetime.now(timezone.utc), value=0)]
    return []


def _parse_json_strategy(html: str) -> list[ReportPoint]:
    """Try to extract report data from embedded JSON/script tags (legacy format)."""
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script"):
        text = script.string or ""
        match = re.search(r'xAxis.*?categories["\s:]+\[(.*?)\]', text, re.DOTALL)
        values_match = re.search(r'series.*?data["\s:]+\[([\d,\s]+)\]', text, re.DOTALL)
        if match and values_match:
            try:
                timestamps_raw = re.findall(r'"([^"]+)"', match.group(1))
                values_raw = [int(v.strip()) for v in values_match.group(1).split(",") if v.strip()]
                points = []
                for ts_str, val in zip(timestamps_raw, values_raw):
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except ValueError:
                        ts = datetime.now(timezone.utc)
                    points.append(ReportPoint(timestamp=ts, value=val))
                if points:
                    return sorted(points, key=lambda p: p.timestamp)
            except (ValueError, IndexError):
                continue

    return []


def _parse_regex_strategy(html: str) -> list[ReportPoint]:
    """Fallback: extract the main visible report count via regex."""
    patterns = [
        r'class="[^"]*current-number[^"]*"[^>]*>\s*(\d+)',
        r'class="[^"]*report-count[^"]*"[^>]*>\s*(\d+)',
        r'<span[^>]*id="[^"]*gauge[^"]*"[^>]*>\s*(\d+)',
        r'"reportCount"\s*:\s*(\d+)',
        r'"currentValue"\s*:\s*(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            return [ReportPoint(timestamp=datetime.now(timezone.utc), value=value)]
    return []


def parse_reports(html: str) -> list[ReportPoint]:
    """Parse HTML using strategy chain: aria-label -> heading -> JSON -> regex -> error.

    Returns list of ReportPoint sorted by timestamp.
    Raises ParseError if no strategy succeeds.
    """
    # Strategy 1: aria-label on chart (current Next.js format)
    points = _parse_aria_label_strategy(html)
    if points:
        logger.info("Parsed via aria-label strategy: %d reports", points[-1].value)
        return points

    # Strategy 2: "no current problems" heading
    points = _parse_heading_strategy(html)
    if points:
        logger.info("Parsed via heading strategy: no current problems")
        return points

    # Strategy 3: JSON from script tags (legacy)
    points = _parse_json_strategy(html)
    if points:
        logger.info("Parsed %d points via JSON strategy", len(points))
        return points

    # Strategy 4: regex fallback
    points = _parse_regex_strategy(html)
    if points:
        logger.warning("Parsed via regex fallback (%d points)", len(points))
        return points

    # Strategy 5: fail
    raise ParseError("No parse strategy could extract report data from HTML")


def get_current_value(
    url: str | None = None,
) -> int:
    """Fetch and return the current report count. Main entry point for scraper."""
    url = url or config.DOWNDETECTOR_URL
    html = fetch_html(url)
    points = parse_reports(html)
    # Return the most recent (last) value
    return points[-1].value
