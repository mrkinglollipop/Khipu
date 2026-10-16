"""Owner writes replay exactly once, from the hub's record of applied write
ids (migration 0031), never from the rows the write touched
(docs/plans/2026-10-16-offline-and-self-healing.md, decision 4). The first
two cases are review probes on a fake hub; the rest run against a throwaway
PostgreSQL (tests/hermetic_pg.py), never the live hub. Every row is
synthetic."""
from __future__ import annotations

import json
import time

import psycopg
import pytest

from khipu import db
from khipu import mcp_server as srv
from khipu import owner_writes as ow
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    db._TABLE_COLUMNS_CACHE.clear()
    return tmp_path


def test_direct_lost_commit_reply_is_not_applied_twice(home, monkeypatch):
    commits = []

    def apply(tool, args):
        commits.append(dict(args))
        if len(commits) == 1:
            raise psycopg.OperationalError("server closed the connection after COMMIT")
        return {"ok": True}

    monkeypatch.setattr(ow, "_apply", apply)
    monkeypatch.setattr(ow, "_landed", lambda *a: True)
    ow.run("owed_update", {"id": 1, "action": "snooze", "until": "7d"})
    ow.drain()
    assert len(commits) == 1, f"committed {len(commits)} times after a lost reply"


class _QueryHub:
    """A hub whose commitment shows an older snooze still in the future,
    and whose record of applied writes does not hold this write's id."""

    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchone(self):
        if "owner_writes_applied" in self.sql[-1]:
            return None
        return ("open", True)


def test_old_snooze_does_not_acknowledge_unsent_new_snooze(home, monkeypatch):
    # Ported from a probe whose fake hub answered every query with the
    # commitment row; replay now asks the record of applied ids instead.
    ow._enqueue("owed_update", {"id": 1, "action": "snooze", "until": "7d"}, "connection refused")
    path = next(ow.queue_dir().glob("*.json"))
    # Death after persisting sending, before the hub write. A previous 1d
    # snooze is still active on the hub.
    job = json.loads(path.read_text())
    job["state"] = "sending"
    path.write_text(json.dumps(job))
    fake = _QueryHub()
    monkeypatch.setattr(db, "connect", lambda **kw: fake)
    applied = []
    monkeypatch.setattr(ow, "_apply", lambda *a: applied.append(a) or {"ok": True})
    out = ow.drain()
    assert len(applied) == 1, f"new 7d snooze was silently dropped: {out}"
    assert not any("commitments" in sql for sql in fake.sql), "replay read the row instead of the record"


def test_every_write_carries_its_own_id(home, monkeypatch):
    seen = []
    monkeypatch.setattr(ow, "_apply", lambda tool, args: seen.append(args["write_id"]) or {"ok": True})
    ow.run("owed_update", {"id": 1, "action": "close"})
    ow.run("owed_update", {"id": 1, "action": "close"})
    ow.run("owed_update", {"id": 1, "action": "close"}, queue_ok=False)
    assert len(set(seen)) == 3 and all(seen)


# ---- against a real PostgreSQL ------------------------------------------------


def _seed(pg) -> int:
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO commitments (text, content_hash) VALUES ('scratch owed', 'h1') RETURNING id")
        cid = cur.fetchone()[0]
        conn.commit()
    return cid


def _row(pg, cid):
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, due_after FROM commitments WHERE id = %s", (cid,))
        return cur.fetchone()


def _applied(pg) -> list[tuple[str, str]]:
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT write_id, tool FROM owner_writes_applied ORDER BY applied_at")
        return cur.fetchall()


def _point_at(pg, monkeypatch) -> None:
    # The suite forbids psycopg.connect; the throwaway cluster is reached the
    # way tests/hermetic_pg.py reaches it.
    monkeypatch.setattr(db, "connect", lambda **_kw: psycopg.Connection.connect(pg.dsn))


@pytest.fixture
def real_hub(hub, home, monkeypatch):  # noqa: F811
    result = hub.migrate()
    assert "0031_owner_writes_applied" in result["ran"], result
    _point_at(hub, monkeypatch)
    return hub


def test_the_hub_applies_an_id_once(real_hub):
    cid = _seed(real_hub)
    args = {"id": cid, "action": "snooze", "until": "7d", "write_id": "scratch-write-1"}
    assert srv._owed_update_on_hub(args) == {"ok": True, "id": cid, "action": "snooze", "until": "7d"}
    first = _row(real_hub, cid)
    time.sleep(0.01)
    again = srv._owed_update_on_hub(args)
    assert again["already_applied"] and again["ok"]
    assert _row(real_hub, cid) == first
    assert _applied(real_hub) == [("scratch-write-1", "owed_update")]


def test_a_refused_write_records_nothing(real_hub):
    with pytest.raises(ValueError):
        srv._owed_update_on_hub({"id": 999999, "action": "close", "write_id": "scratch-missing"})
    assert _applied(real_hub) == []


def test_a_lost_commit_reply_on_a_real_hub_replays_as_landed(real_hub, monkeypatch):
    cid = _seed(real_hub)
    real_apply = ow._apply
    sent = []

    def apply(tool, args):
        sent.append(args["write_id"])
        out = real_apply(tool, args)
        if len(sent) == 1:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return out

    monkeypatch.setattr(ow, "_apply", apply)
    out = ow.run("owed_update", {"id": cid, "action": "snooze", "until": "7d"})
    assert out["waiting_to_sync"], out
    first = _row(real_hub, cid)
    time.sleep(0.01)
    res = ow.drain()
    assert res["replayed"] == 1 and len(sent) == 1, (res, sent)
    assert _row(real_hub, cid) == first
    assert len(_applied(real_hub)) == 1 and ow.status()["pending"] == 0


def test_a_write_lost_before_sending_is_sent_though_an_older_snooze_is_live(real_hub):
    cid = _seed(real_hub)
    assert srv._owed_update_on_hub({"id": cid, "action": "snooze", "until": "1d", "write_id": "scratch-old"})["ok"]
    old_due = _row(real_hub, cid)[1]
    ow._enqueue("owed_update", {"id": cid, "action": "snooze", "until": "7d"}, "connection refused")
    path = next(ow.queue_dir().glob("*.json"))
    path.write_text(json.dumps({**json.loads(path.read_text()), "state": "sending"}))
    res = ow.drain()
    assert res["replayed"] == 1, res
    assert _row(real_hub, cid)[1] > old_due
    assert len(_applied(real_hub)) == 2


def test_a_decision_update_records_its_id_with_the_write(real_hub):
    with real_hub.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO decisions (text) VALUES ('scratch decision') RETURNING id")
        did = cur.fetchone()[0]
        conn.commit()
    args = {"id": did, "action": "retract", "reason": "scratch", "write_id": "scratch-decision"}
    assert srv._decisions_update_on_hub(args)["ok"]
    assert srv._decisions_update_on_hub(args)["already_applied"]
    assert _applied(real_hub) == [("scratch-decision", "decisions_update")]


def test_a_hub_without_the_record_keeps_writes_waiting(hub, home, monkeypatch):  # noqa: F811
    # The template stops at 0029: no owner_writes_applied table.
    _point_at(hub, monkeypatch)
    ow._enqueue("owed_update", {"id": 1, "action": "close"}, "connection refused")
    sent = []
    monkeypatch.setattr(ow, "_apply", lambda tool, args: sent.append(args) or {"ok": True})
    res = ow.drain()
    assert res["stopped_early"] and res["hub_needs_migrate"] and res["waiting"] == 1, res
    assert sent == []
