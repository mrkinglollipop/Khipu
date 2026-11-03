"""Retention of the hub's receipt tables, against a throwaway PostgreSQL
(tests/hermetic_pg.py); never the live hub. Every row is synthetic."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from khipu import db, owner_writes, receipt_prune
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


@pytest.fixture
def pg(hub, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_TEST", "1")
    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    return hub


def _count(pg, table):
    with pg.connect() as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _writes(pg, ages):
    with pg.connect() as conn:
        for n, days in enumerate(ages):
            conn.execute("INSERT INTO owner_writes_applied (write_id, tool, applied_at) "
                         "VALUES (%s, 'owed_update', now() - make_interval(days => %s))", (f"w{n}", days))


def test_old_owner_write_receipts_go_and_recent_ones_stay(pg):
    _writes(pg, [200, 120, 95, 80, 10, 0])
    with pg.connect() as conn:
        out = receipt_prune.prune(conn, keep_days=90)
    assert out["owner_writes_applied"] == 3 and out["ok"] is True
    with pg.connect() as conn:
        left = sorted(r[0] for r in conn.execute("SELECT write_id FROM owner_writes_applied"))
    assert left == ["w3", "w4", "w5"]


def test_the_window_is_never_shorter_than_the_replay_horizon(pg):
    _writes(pg, [200, 66, 50])
    with pg.connect() as conn:
        out = receipt_prune.prune(conn, keep_days=1)  # asks for less than the horizon allows
    assert out["owner_writes_keep_days"] == owner_writes.REPLAY_HORIZON_DAYS + 7
    assert out["owner_writes_applied"] == 1  # only the 200-day-old one
    assert _count(pg, "owner_writes_applied") == 2


def test_ops_events_keep_the_newest_of_each_kind_and_the_newest_ok(pg):
    def add(kind, status, days):
        with pg.connect() as conn:
            conn.execute("INSERT INTO ops_events (kind, status, created_at) "
                         "VALUES (%s, %s, now() - make_interval(days => %s))", (kind, status, days))

    for days in (400, 300, 200):
        add("pg_dump", "ok", days)
    add("pg_dump", "fail", 150)      # newest of its kind, older than the window: kept
    add("restore_drill", "ok", 500)  # the only one of its kind: kept
    add("graph_snapshot", "ok", 5)
    add("graph_snapshot", "ok", 400)
    with pg.connect() as conn:
        out = receipt_prune.prune(conn, keep_days=90)
        left = sorted(conn.execute("SELECT kind, status, (now() - created_at) > interval '90 days' "
                                   "FROM ops_events").fetchall())
    assert out["ops_events"] == 3
    assert left == [("graph_snapshot", "ok", False), ("pg_dump", "fail", True), ("pg_dump", "ok", True),
                    ("restore_drill", "ok", True)]


def test_zero_keeps_everything_and_a_missing_table_is_skipped(pg):
    _writes(pg, [500])
    with pg.connect() as conn:
        assert receipt_prune.prune(conn, keep_days=0)["disabled"] is True
        conn.execute("DROP TABLE ops_events")
        conn.commit()
        out = receipt_prune.prune(conn, keep_days=90)
    assert out["skipped"] == ["ops_events"] and out["owner_writes_applied"] == 1


def test_a_big_backlog_is_deleted_in_batches_and_the_rest_waits_for_tomorrow(pg, monkeypatch):
    monkeypatch.setattr(receipt_prune, "BATCH", 10)
    monkeypatch.setattr(receipt_prune, "MAX_BATCHES", 2)
    _writes(pg, [300] * 25)
    with pg.connect() as conn:
        out = receipt_prune.prune(conn, keep_days=90)
    assert out["owner_writes_applied"] == 20 and "deferred" in out
    with pg.connect() as conn:
        assert receipt_prune.prune(conn, keep_days=90)["owner_writes_applied"] == 5
    assert _count(pg, "owner_writes_applied") == 0


def test_the_tables_that_are_needed_for_good_are_left_alone(pg):
    with pg.connect() as conn:
        conn.execute("INSERT INTO ledger_ops (op_id, project, row_id, verb, at) "
                     "VALUES ('op1', 'p', 'r', 'add', now() - interval '900 days')")
        conn.execute("INSERT INTO commitment_tidy_batches (id, project, plan_version, applied_at) "
                     "VALUES ('b1', 'p', 1, now() - interval '900 days')")
    with pg.connect() as conn:
        receipt_prune.prune(conn, keep_days=1)
    assert _count(pg, "ledger_ops") == 1 and _count(pg, "commitment_tidy_batches") == 1


# ---- the replay horizon in owner_writes ------------------------------------------


def _queue(tmp_path, monkeypatch, age_days, landed):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    stamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    path = owner_writes.queue_dir() / "00000000000000000001-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tool": "owed_update", "args": {"id": 1, "write_id": "w1"},
                                "state": "waiting", "queued_at": stamp, "reason": "hub away"}))
    monkeypatch.setattr(owner_writes, "_landed", lambda tool, args: landed)
    applied = []
    monkeypatch.setattr(owner_writes, "_apply", lambda tool, args: applied.append(args) or {"ok": True})
    return path, applied


def test_a_write_too_old_to_be_checked_is_refused_not_sent(tmp_path, monkeypatch):
    path, applied = _queue(tmp_path, monkeypatch, owner_writes.REPLAY_HORIZON_DAYS + 5, None)
    out = owner_writes.drain()
    assert out["refused"] == 1 and applied == [] and not path.exists()
    refused = json.loads((owner_writes.queue_dir() / "refused" / path.name).read_text())
    assert "no longer keeps the record" in refused["error"]


def test_a_recent_write_is_sent_and_an_old_one_the_hub_holds_is_just_cleared(tmp_path, monkeypatch):
    path, applied = _queue(tmp_path, monkeypatch, 5, None)
    assert owner_writes.drain()["replayed"] == 1 and len(applied) == 1
    path, applied = _queue(tmp_path, monkeypatch, owner_writes.REPLAY_HORIZON_DAYS + 5, ("applied", None))
    assert owner_writes.drain()["replayed"] == 1 and applied == [] and not path.exists()
