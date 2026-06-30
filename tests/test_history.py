"""Tests for history module – append-only CSV logging."""

import csv
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.history import HEADER, append_row

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


def _make_now(hour=12, minute=30):
    return datetime(2026, 6, 30, hour, minute, 0, tzinfo=BUDAPEST_TZ)


class TestAppendRow:
    def test_creates_file_with_header_and_row(self, tmp_path):
        path = tmp_path / "history.csv"
        append_row(path, value=5, threshold=10, alert_active=False, now=_make_now())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert lines[0] == ",".join(HEADER)
        reader = csv.reader([lines[1]])
        row = next(reader)
        assert row[1] == "5"
        assert row[2] == "10"
        assert row[3] == "false"
        assert "2026-06-30" in row[0]

    def test_header_only_on_new_file(self, tmp_path):
        path = tmp_path / "history.csv"
        append_row(path, value=1, threshold=10, alert_active=False, now=_make_now(12, 0))
        append_row(path, value=2, threshold=10, alert_active=False, now=_make_now(12, 30))
        append_row(path, value=3, threshold=10, alert_active=True, now=_make_now(13, 0))
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4  # 1 header + 3 data rows
        # Only first line is the header
        assert lines[0] == ",".join(HEADER)
        for line in lines[1:]:
            assert not line.startswith("timestamp")

    def test_appends_in_order(self, tmp_path):
        path = tmp_path / "history.csv"
        for i in range(5):
            append_row(path, value=i, threshold=10, alert_active=False, now=_make_now(12, i))
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 5
        assert [int(r["value"]) for r in rows] == [0, 1, 2, 3, 4]

    def test_alert_active_recorded_correctly(self, tmp_path):
        path = tmp_path / "history.csv"
        append_row(path, value=5, threshold=10, alert_active=False, now=_make_now())
        append_row(path, value=15, threshold=10, alert_active=True, now=_make_now(13))
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert rows[0]["alert_active"] == "false"
        assert rows[1]["alert_active"] == "true"

    def test_timestamp_is_iso8601_with_offset(self, tmp_path):
        path = tmp_path / "history.csv"
        now = _make_now()
        append_row(path, value=5, threshold=10, alert_active=False, now=now)
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            row = next(reader)
        ts = row["timestamp"]
        assert "+02:00" in ts or "+01:00" in ts  # CEST or CET
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None

    def test_io_error_does_not_crash(self, tmp_path, caplog):
        path = tmp_path / "nonexistent_dir" / "history.csv"
        # Parent dir doesn't exist -> should log warning, not raise
        append_row(path, value=5, threshold=10, alert_active=False, now=_make_now())
        assert any("Failed to write history.csv" in r.message for r in caplog.records)

    def test_io_error_with_mock(self, tmp_path, caplog):
        path = tmp_path / "history.csv"
        with patch.object(type(path), "open", side_effect=PermissionError("mock permission denied")):
            append_row(path, value=5, threshold=10, alert_active=False, now=_make_now())
        assert any("Failed to write history.csv" in r.message for r in caplog.records)

    def test_size_warning(self, tmp_path, caplog):
        path = tmp_path / "history.csv"
        # Create a file just over the threshold (use small threshold for test)
        path.write_text("x" * 1024, encoding="utf-8")  # 1 KB
        append_row(
            path, value=5, threshold=10, alert_active=False, now=_make_now(),
            max_size_mb=0,  # 0 MB threshold -> any file triggers warning
        )
        assert any("consider archiving" in r.message for r in caplog.records)

    def test_no_size_warning_under_threshold(self, tmp_path, caplog):
        path = tmp_path / "history.csv"
        append_row(path, value=5, threshold=10, alert_active=False, now=_make_now())
        assert not any("consider archiving" in r.message for r in caplog.records)

    def test_empty_file_gets_header(self, tmp_path):
        path = tmp_path / "history.csv"
        path.write_text("", encoding="utf-8")
        append_row(path, value=5, threshold=10, alert_active=False, now=_make_now())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == ",".join(HEADER)
        assert len(lines) == 2

    def test_threshold_value_persisted(self, tmp_path):
        """Different threshold values are recorded per-row for historical context."""
        path = tmp_path / "history.csv"
        append_row(path, value=5, threshold=10, alert_active=False, now=_make_now(12))
        append_row(path, value=5, threshold=20, alert_active=False, now=_make_now(13))
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert rows[0]["threshold"] == "10"
        assert rows[1]["threshold"] == "20"
