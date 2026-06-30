"""Append-only CSV history logging for long-term trend tracking."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HEADER = ["timestamp", "value", "threshold", "alert_active"]


def append_row(
    path: str | Path,
    value: int,
    threshold: int,
    alert_active: bool,
    now: datetime,
    *,
    max_size_mb: int = 5,
) -> None:
    """Append a single row to the history CSV.

    Creates the file with a header if it doesn't exist yet.
    Logs WARNING and returns silently on any I/O error —
    the core monitor functionality must never be blocked by history logging.

    Args:
        path: Path to the history CSV file.
        value: Current Downdetector report count.
        threshold: Current ALERT_THRESHOLD value.
        alert_active: Whether alert is currently active (after decide_action).
        now: Timestamp in Budapest timezone (ISO 8601 with offset).
        max_size_mb: Warn if file exceeds this size in MB.
    """
    try:
        p = Path(path)

        # Size check before writing
        if p.exists():
            size_mb = p.stat().st_size / (1024 * 1024)
            if size_mb > max_size_mb:
                logger.warning(
                    "history.csv is %.1f MB (threshold: %d MB) — consider archiving",
                    size_mb,
                    max_size_mb,
                )

        write_header = not p.exists() or p.stat().st_size == 0

        with p.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(HEADER)
            writer.writerow([
                now.isoformat(),
                value,
                threshold,
                str(alert_active).lower(),
            ])

        logger.info("History row appended: value=%d threshold=%d alert_active=%s", value, threshold, alert_active)

    except Exception:
        logger.warning("Failed to write history.csv — continuing without history logging", exc_info=True)
