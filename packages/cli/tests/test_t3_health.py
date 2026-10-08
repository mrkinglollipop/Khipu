"""Read-only doctor evidence for the optional T3 Code desktop card."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from khipu import hub_snapshot, t3


def _settings(home: Path, text: str = "{}") -> None:
    path = home / ".t3" / "userdata" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def _thread_db(path: Path, *, current_shape: bool = True) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        if current_shape:
            con.execute(
                "CREATE TABLE orchestration_v2_projection_provider_threads "
                "(thread_id TEXT, payload_json TEXT, updated_at TEXT)"
            )
        else:
            con.execute("CREATE TABLE replacement_threads (id TEXT)")
        con.commit()
    finally:
        con.close()


def _snapshot(path: Path, rows: list[tuple[str, dict]]) -> None:
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE episodes (ts TEXT, raw TEXT)")
        for ts, raw in rows:
            con.execute("INSERT INTO episodes VALUES (?, ?)", (ts, json.dumps(raw)))


def _freeze_now(monkeypatch, db: Path) -> None:
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(t3.time, "time", lambda: now)
    os.utime(db, (now, now))


def test_t3_health_is_absent_without_its_settings_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "statev2.sqlite"))
    assert t3.health(tmp_path) is None


def test_t3_health_reports_a_missing_or_garbled_thread_db_quietly(tmp_path, monkeypatch):
    _settings(tmp_path)
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("KHIPU_T3_DB", str(missing))
    assert t3.health(tmp_path)["lookup"]["ok"] is False

    broken = tmp_path / "broken.sqlite"
    broken.write_text("not sqlite", encoding="utf-8")
    monkeypatch.setenv("KHIPU_T3_DB", str(broken))
    assert t3.health(tmp_path)["lookup"]["ok"] is False


def test_t3_health_accepts_garbled_settings_and_reports_a_working_lookup(tmp_path, monkeypatch):
    _settings(tmp_path, "{")
    db = tmp_path / "statev2.sqlite"
    _thread_db(db)
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    _freeze_now(monkeypatch, db)

    out = t3.health(tmp_path)

    assert out is not None
    assert out["detected"] is True
    assert out["lookup"] == {"ok": True, "error": None}
    assert out["warnings"] == ["T3 was used in the last day without a thread-linked capture"]


def test_t3_health_reports_schema_drift_without_inventing_provenance(tmp_path, monkeypatch):
    _settings(tmp_path)
    db = tmp_path / "statev2.sqlite"
    _thread_db(db, current_shape=False)
    snap = tmp_path / "hub_snapshot.sqlite"
    _snapshot(snap, [("2026-10-08T12:00:00+00:00", {"via": "t3", "t3_thread_id": "thread-1"})])
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    monkeypatch.setattr(hub_snapshot, "snapshot_path", lambda: snap)
    _freeze_now(monkeypatch, db)

    out = t3.health(tmp_path)

    assert out is not None
    assert out["lookup"]["ok"] is False
    assert out["lookup"]["error"] == "T3's thread records changed shape (table not found)"
    assert out["last_thread_linked_capture_at"] == "2026-10-08T12:00:00+00:00"
    assert out["warnings"] == ["T3's thread records changed shape (table not found)"]


def test_t3_health_accepts_a_usable_mapping_and_recent_stamped_capture(tmp_path, monkeypatch):
    _settings(tmp_path)
    db = tmp_path / "statev2.sqlite"
    _thread_db(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
            ("thread-1", json.dumps({"nativeThreadRef": {"nativeId": "native-1"}}), "2026-10-08"),
        )
    snap = tmp_path / "hub_snapshot.sqlite"
    _snapshot(snap, [("2026-10-08T12:00:00+00:00", {"via": "t3", "t3_thread_id": "thread-1"})])
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    monkeypatch.setattr(hub_snapshot, "snapshot_path", lambda: snap)
    _freeze_now(monkeypatch, db)

    out = t3.health(tmp_path)

    assert out is not None
    assert out["lookup"]["ok"] is True
    assert out["last_thread_linked_capture_at"] == "2026-10-08T12:00:00+00:00"
    assert out["thread_linked_captures_today"] == 1
    assert out["warnings"] == []


def test_t3_health_warns_when_recent_t3_activity_has_no_linked_capture(tmp_path, monkeypatch):
    _settings(tmp_path)
    db = tmp_path / "statev2.sqlite"
    _thread_db(db)
    snap = tmp_path / "hub_snapshot.sqlite"
    _snapshot(snap, [("2026-10-08T12:00:00+00:00", {"via": "t3"})])
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    monkeypatch.setattr(hub_snapshot, "snapshot_path", lambda: snap)
    _freeze_now(monkeypatch, db)

    out = t3.health(tmp_path)

    assert out is not None
    assert out["used_recently"] is True
    assert out["last_thread_linked_capture_at"] is None
    assert out["warnings"] == ["T3 was used in the last day without a thread-linked capture"]


def test_t3_health_warns_when_recent_use_has_only_an_old_linked_capture(tmp_path, monkeypatch):
    _settings(tmp_path)
    db = tmp_path / "statev2.sqlite"
    _thread_db(db)
    snap = tmp_path / "hub_snapshot.sqlite"
    _snapshot(snap, [("2026-10-06T12:00:00+00:00", {"via": "t3", "t3_thread_id": "thread-1"})])
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    monkeypatch.setattr(hub_snapshot, "snapshot_path", lambda: snap)
    _freeze_now(monkeypatch, db)

    assert t3.health(tmp_path)["warnings"] == ["T3 was used in the last day without a thread-linked capture"]


def test_t3_health_rejects_rows_without_a_usable_native_session_id(tmp_path, monkeypatch):
    _settings(tmp_path)
    db = tmp_path / "statev2.sqlite"
    _thread_db(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
            ("thread-1", "{bad json", "2026-10-08"),
        )
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    _freeze_now(monkeypatch, db)

    out = t3.health(tmp_path)

    assert out is not None
    assert out["lookup"] == {
        "ok": False,
        "error": "T3's thread records changed shape (no native session ids found)",
    }


def test_t3_health_closed_wal_does_not_create_sidecars(tmp_path, monkeypatch):
    _settings(tmp_path)
    db = tmp_path / "statev2.sqlite"
    _thread_db(db)
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    _freeze_now(monkeypatch, db)

    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert t3.health(tmp_path) is not None
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert before == after


def test_t3_health_uses_a_recent_wal_when_the_main_db_is_old(tmp_path, monkeypatch):
    _settings(tmp_path)
    db = tmp_path / "statev2.sqlite"
    _thread_db(db)
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    _freeze_now(monkeypatch, db)
    old = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    os.utime(db, (old, old))
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)", ("thread", json.dumps({"nativeThreadRef": {"nativeId": "native"}}), "2026-10-08"))
    con.commit()
    try:
        assert Path(f"{db}-wal").is_file()
        before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
        assert t3.health(tmp_path)["used_recently"] is True
        after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
        assert before == after
    finally:
        con.close()


def test_t3_health_tolerates_an_old_replica_without_raw(tmp_path, monkeypatch):
    _settings(tmp_path)
    db = tmp_path / "statev2.sqlite"
    _thread_db(db)
    snap = tmp_path / "hub_snapshot.sqlite"
    with sqlite3.connect(snap) as con:
        con.execute("CREATE TABLE episodes (ts TEXT)")
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    monkeypatch.setattr(hub_snapshot, "snapshot_path", lambda: snap)
    _freeze_now(monkeypatch, db)

    assert t3.health(tmp_path)["last_thread_linked_capture_at"] is None
