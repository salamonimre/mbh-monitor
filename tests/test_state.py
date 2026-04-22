"""Tests for state module – load/save/serialization."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.state import State, load, save


class TestState:
    def test_default_state(self):
        s = State()
        assert s.last_value == 0
        assert s.last_checked is None
        assert s.alert_active is False
        assert s.alert_started_at is None
        assert s.consecutive_fetch_failures == 0

    def test_to_dict_and_back(self):
        now = datetime(2024, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
        s = State(last_value=42, last_checked=now, alert_active=True, alert_started_at=now)
        d = s.to_dict()
        restored = State.from_dict(d)
        assert restored.last_value == 42
        assert restored.last_checked == now
        assert restored.alert_active is True
        assert restored.alert_started_at == now

    def test_to_dict_with_none_dates(self):
        s = State()
        d = s.to_dict()
        assert d["last_checked"] is None
        assert d["alert_started_at"] is None

    def test_from_dict_ignores_extra_keys(self):
        d = {"last_value": 10, "extra_key": "ignored"}
        s = State.from_dict(d)
        assert s.last_value == 10


class TestLoadSave:
    def test_load_missing_file_returns_default(self, tmp_path):
        s = load(tmp_path / "nonexistent.json")
        assert s == State()

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        now = datetime(2024, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
        original = State(last_value=55, last_checked=now, alert_active=True, alert_started_at=now)
        save(original, path)
        loaded = load(path)
        assert loaded.last_value == 55
        assert loaded.last_checked == now
        assert loaded.alert_active is True

    def test_load_corrupt_json_returns_default(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("not json at all", encoding="utf-8")
        s = load(path)
        assert s == State()

    def test_load_empty_file_returns_default(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("", encoding="utf-8")
        s = load(path)
        assert s == State()

    def test_saved_file_is_valid_json(self, tmp_path):
        path = tmp_path / "state.json"
        save(State(last_value=10), path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["last_value"] == 10
