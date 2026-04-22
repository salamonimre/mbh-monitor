"""State persistence – load/save JSON state file."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class State:
    last_value: int = 0
    last_checked: datetime | None = None
    alert_active: bool = False
    alert_started_at: datetime | None = None
    consecutive_fetch_failures: int = 0
    error_alert_sent: bool = False
    last_heartbeat_date: str | None = None  # "YYYY-MM-DD"

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("last_checked", "alert_started_at"):
            val = d[key]
            d[key] = val.isoformat() if isinstance(val, datetime) else val
        return d

    @classmethod
    def from_dict(cls, d: dict) -> State:
        for key in ("last_checked", "alert_started_at"):
            val = d.get(key)
            if isinstance(val, str):
                d[key] = datetime.fromisoformat(val)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def load(path: str | Path) -> State:
    """Load state from JSON file. Returns default State if file missing/corrupt."""
    p = Path(path)
    if not p.exists():
        logger.info("State file not found at %s, using defaults", p)
        return State()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return State.from_dict(data)
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning("Corrupt state file %s (%s), using defaults", p, exc)
        return State()


def save(state: State, path: str | Path) -> None:
    """Write state to JSON file."""
    p = Path(path)
    p.write_text(
        json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("State saved to %s", p)
