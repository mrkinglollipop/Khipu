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
    monkeypatch.setattr(ow, "_landed", lambda *a: ("applied", None))
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


class _TxHub:
    """A transaction-aware fake hub: the record of answered write ids and the
    commitment writes land only on commit, and a rollback drops them."""

    def __init__(self):
        self.answered = {}  # write_id -> (outcome, error)
        self.commitment_writes = 0
        self.missing = False  # the commitment a snooze names does not exist

    def connect(self, **_kw):
        return _TxConn(self)


class _TxConn:
    def __init__(self, owner):
        self.hub, self.pending, self.writes = owner, {}, 0

    def __enter__(self):
        return self

    def __exit__(self, typ, *_a):
        self.commit() if typ is None else self.rollback()
        return False

    def cursor(self):
        return _TxCursor(self)

    def rollback(self):
        self.pending, self.writes = {}, 0

    def commit(self):
        self.hub.answered.update(self.pending)
        self.hub.commitment_writes += self.writes
        self.pending, self.writes = {}, 0


class _TxCursor:
    def __init__(self, conn):
        self.conn, self.rowcount, self.result = conn, 0, None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=()):
        c = self.conn
        if sql.startswith("SELECT outcome, error FROM owner_writes_applied"):
            self.result = c.hub.answered.get(params[0])
        elif sql.startswith("INSERT INTO owner_writes_applied"):
            known = params[0] in c.hub.answered or params[0] in c.pending
            self.rowcount = int(not known)
            if not known:
                c.pending[params[0]] = ("refused", params[2]) if "'refused'" in sql else ("applied", None)
        elif sql.startswith("UPDATE commitments SET due_after"):
            self.rowcount = 0 if c.hub.missing else 1
            c.writes += self.rowcount
        elif sql.startswith("UPDATE decisions SET retracted_at"):
            self.rowcount = 0  # no such decision: the real retract returns False
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.result


@pytest.fixture
def tx_hub(home, monkeypatch):
    from khipu import decisions

    fake = _TxHub()
    monkeypatch.setattr(db, "connect", fake.connect)
    monkeypatch.setattr(decisions, "_evidence_ready", lambda cur: True)
    return fake


@pytest.mark.parametrize("state", ["waiting", "sending", "reserved"])
def test_legacy_landed_snooze_is_not_applied_twice(tx_hub, state):
    # Review probe: a build before write ids committed this snooze, then lost
    # its reply before removing the entry. Nothing can tell whether it
    # landed, so it is refused for the owner to check, never sent again.
    tx_hub.commitment_writes = 1
    ow.queue_dir().mkdir(parents=True)
    job = {"tool": "owed_update", "args": {"id": 1, "action": "snooze", "until": "7d"},
           "state": state, "made_at": "2026-10-01T00:00:00+00:00"}
    (ow.queue_dir() / "00000000000000000001-1.json").write_text(json.dumps(job))
    out = ow.drain()
    assert tx_hub.commitment_writes == 1, f"legacy write applied twice; {out}"
    assert out["refused"] == 1 and out["waiting"] == 0, out
    refused = json.loads(next((ow.queue_dir() / "refused").glob("*.json")).read_text())
    assert refused["error"] == "queued by an older build; check it and redo it"


@pytest.mark.parametrize("action", ["retract", "supersede"])
def test_refused_decision_lost_reply_is_not_acknowledged_as_applied(tx_hub, monkeypatch, action):
    # Review probe: the hub refuses the edit, commits, and the reply is lost.
    from khipu import decisions

    real_apply = ow._apply

    def lost_reply(tool, args):
        result = real_apply(tool, args)
        assert result["ok"] is False
        raise psycopg.OperationalError("server closed the connection after COMMIT")

    monkeypatch.setattr(ow, "_apply", lost_reply)
    monkeypatch.setattr(decisions, "_fetch_decision_row", lambda cur, id: {"id": id, "superseded_by": 2})
    args = {"id": 999, "action": action}
    if action == "supersede":
        args["new_text"] = "owner requested replacement decision"
    assert ow.run("decisions_update", args)["waiting_to_sync"]
    result = ow.drain()
    assert result["refused"] == 1, f"unapplied action acknowledged as success: {result}"
    assert [o for o, _e in tx_hub.answered.values()] == ["refused"]


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
        cur.execute("SELECT write_id, tool FROM owner_writes_applied WHERE outcome = 'applied' ORDER BY applied_at")
        return cur.fetchall()


def _refused(pg) -> list[tuple[str, str]]:
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT write_id, error FROM owner_writes_applied WHERE outcome = 'refused' ORDER BY applied_at")
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


def test_a_refused_write_records_its_refusal(real_hub):
    args = {"id": 999999, "action": "close", "write_id": "scratch-missing"}
    with pytest.raises(ValueError):
        srv._owed_update_on_hub(args)
    assert _applied(real_hub) == []
    assert _refused(real_hub) == [("scratch-missing", "ValueError: no such commitment: 999999")]
    again = srv._owed_update_on_hub(args)
    assert again["ok"] is False and again["already_refused"], again


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
    missing = {"id": did + 1000, "action": "retract", "write_id": "scratch-missing-decision"}
    assert srv._decisions_update_on_hub(missing)["ok"] is False
    assert srv._decisions_update_on_hub(missing)["already_refused"]
    assert [w for w, _e in _refused(real_hub)] == ["scratch-missing-decision"]


def test_a_hub_without_the_record_keeps_writes_waiting(hub, home, monkeypatch):  # noqa: F811
    # The template stops at 0029: no owner_writes_applied table.
    _point_at(hub, monkeypatch)
    ow._enqueue("owed_update", {"id": 1, "action": "close"}, "connection refused")
    sent = []
    monkeypatch.setattr(ow, "_apply", lambda tool, args: sent.append(args) or {"ok": True})
    res = ow.drain()
    assert res["stopped_early"] and res["hub_needs_migrate"] and res["waiting"] == 1, res
    assert sent == []
