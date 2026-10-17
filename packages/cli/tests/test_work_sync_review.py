"""Work sync, integrated review of the combined branch: the reproduced
defects in the ledger read overlay, the offline copy's catch-up and
freshness, the summary cache and the queue's health, each as a test."""
# ruff: noqa: F811  (the hub fixtures are imported from test_ledger)
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from khipu import hub_snapshot as hs
from khipu import ledger, work
from tests.test_ledger import P, _real_connect, _snapshot, hub, ok, pg_dsn  # noqa: F401
from tests.test_work import NO_OWED, ROUTING, cfg


def _applied(conn, verb, row="r", **fields):
    return ledger.apply_op(conn, ledger.new_op(verb, P, row, fields=fields))


def _hub_away(monkeypatch):
    monkeypatch.setattr(ledger, "_connect", lambda: (_ for _ in ()).throw(ledger.HubAway("offline")))


def test_a_queued_set_the_hub_already_holds_is_not_shown_again_offline(hub, tmp_path, monkeypatch):
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Row", "--kind", "slice")
    op = ledger.new_op("set", P, "r", fields={"state": "DONE"}, op_id="lost-ack")
    op["seen"] = {"state": "NOT STARTED"}
    ledger.enqueue(op)
    with _real_connect(hub) as c:
        ledger.apply_op(c, op, late=True)  # landed, but its answer never reached this Mac
        _applied(c, "set", state="IN FLIGHT")
    assert hs.refresh_ledger()["ok"]
    _hub_away(monkeypatch)
    assert hs.ledger_read_snapshot(P)["rows"][0]["state"] == "IN FLIGHT"
    view = ledger.load(P)
    assert view["rows"][0]["state"] == "IN FLIGHT"
    assert not view["rows"][0].get("queued")


def test_a_landed_add_does_not_bring_back_a_row_removed_since(hub, tmp_path, monkeypatch):
    _snapshot(tmp_path)
    op = ledger.new_op("add", P, "r", fields={"title": "Row", "kind": "slice"}, op_id="lost-add")
    ledger.enqueue(op)
    with _real_connect(hub) as c:
        ledger.apply_op(c, op)
        _applied(c, "remove")
    assert hs.refresh_ledger()["ok"]
    _hub_away(monkeypatch)
    assert hs.ledger_read_snapshot(P)["gone"]["r"] == 2
    assert ledger.load(P)["rows"] == []


def test_a_queued_add_over_a_removal_marker_is_not_shown(hub, tmp_path, monkeypatch):
    """The write path refuses an add over a removed row: the overlay too,
    receipt or not."""
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Row", "--kind", "slice")
    ok("remove", "r")
    assert hs.refresh_ledger()["ok"]
    ledger.enqueue(ledger.new_op("add", P, "r", fields={"title": "Again", "kind": "slice"}))
    _hub_away(monkeypatch)
    assert ledger.load(P)["rows"] == []


def test_the_overlay_keeps_a_queued_set_whose_field_moved_as_late(hub, tmp_path):
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Row", "--kind", "slice")
    op = ledger.new_op("set", P, "r", fields={"state": "DONE"}, op_id="late")
    op["seen"] = {"state": "NOT STARTED"}
    ledger.enqueue(op)
    with _real_connect(hub) as c:
        _applied(c, "set", state="IN FLIGHT")
        view = ledger._overlay(ledger._hub_view(c, P), P)
        result = ledger.apply_op(c, op, late=True)
    assert result["late"] and result["state"] == "IN FLIGHT"
    assert view["rows"][0]["state"] == "IN FLIGHT"


def test_the_overlay_skips_a_queued_write_the_hub_holds_while_online(hub, tmp_path, monkeypatch):
    """Online with the drain busy: the hub's receipts decide, not the line."""
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Row", "--kind", "slice")
    op = ledger.new_op("set", P, "r", fields={"title": "Mine"}, op_id="busy-ack")
    op["seen"] = {"title": "Row"}
    ledger.enqueue(op)
    with _real_connect(hub) as c:
        ledger.apply_op(c, op, late=True)
        _applied(c, "set", title="Theirs")
    monkeypatch.setattr(ledger, "drain", lambda conn=None: {**ledger._drain_result(), "busy": True})
    view = ledger.load(P)
    assert view["rows"][0]["title"] == "Theirs" and not view["rows"][0].get("queued")


def test_catch_up_never_holds_the_mac_lock_past_its_sql_deadlines(hub, tmp_path, monkeypatch):
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Row", "--kind", "slice")
    ledger._mark_lag(P, ["r"])
    monkeypatch.setattr(ledger, "LEDGER_LOCK_TIMEOUT_MS", 100)
    monkeypatch.setattr(ledger, "LEDGER_STATEMENT_TIMEOUT_MS", 100)
    blocker = _real_connect(hub)
    blocker.execute("LOCK TABLE ledger_history IN ACCESS EXCLUSIVE MODE")
    answers = []

    def catch_up():
        with _real_connect(hub) as c:
            answers.append(ledger.replica_catch_up(c))

    t = threading.Thread(target=catch_up)
    try:
        t.start()
        time.sleep(0.5)
        with ledger._ledger_lock(wait_s=0):
            pass  # free: the blocked read gave up within its deadline
    finally:
        blocker.rollback()
        blocker.close()
        t.join(5)
    assert not t.is_alive() and answers[0]["copied"] == 0 and answers[0]["left"] == 1
    with _real_connect(hub) as c:
        assert ledger.replica_catch_up(c)["copied"] == 1
    assert ledger.replica_pending() == 0


def test_catch_up_copies_outside_the_mac_lock(hub, tmp_path, monkeypatch):
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Row", "--kind", "slice")
    ledger._mark_lag(P, ["r"])
    held = []
    real = hs.ledger_write_snapshot

    def copying(*a, **kw):
        held.append(getattr(ledger._lock_held, "depth", 0))
        return real(*a, **kw)

    monkeypatch.setattr(hs, "ledger_write_snapshot", copying)
    with _real_connect(hub) as c:
        assert ledger.replica_catch_up(c)["copied"] == 1
    assert held == [0]


def test_a_full_dump_is_as_fresh_as_its_start_not_its_end(hub, tmp_path, monkeypatch):
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Before", "--kind", "slice")
    clock = {"now": datetime.now(timezone.utc)}
    initial = clock["now"]
    monkeypatch.setattr(hs, "_utcnow_iso", lambda: clock["now"].isoformat())
    for name in ("_insert_topics", "_insert_topic_revisions", "_insert_nodes", "_insert_edges", "_insert_profiles",
                 "_insert_memory_embeddings", "_insert_decisions", "_insert_deliverables", "_insert_commitments"):
        monkeypatch.setattr(hs, name, lambda *a: 0)

    def after_snapshot(cur, con):
        with _real_connect(hub) as remote:
            _applied(remote, "set", title="During dump")
        clock["now"] = initial + timedelta(minutes=10)
        return 0

    monkeypatch.setattr(hs, "_insert_episodes", after_snapshot)
    assert hs.refresh()["ok"]
    got = hs.ledger_read_snapshot(P)
    assert got["rows"][0]["title"] == "Before"
    assert datetime.fromisoformat(got["as_of"]) <= initial


def test_an_older_read_of_a_summary_never_replaces_a_newer_one_with_the_same_time(monkeypatch, tmp_path):
    from tests.test_work import NOW, entry, job, put, summary

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    path = put(tmp_path, summary(jobs=[job(summary="Old")]))
    parsed, release = threading.Event(), threading.Event()
    real = work.parse_summary
    result = []

    def pausing(data, *a, **kw):
        out = real(data, *a, **kw)
        if threading.current_thread().name == "older":
            parsed.set()
            assert release.wait(5)
        return out

    monkeypatch.setattr(work, "parse_summary", pausing)
    t = threading.Thread(target=lambda: result.append(work.read_source(entry(path), cfg(), NOW)), name="older")
    t.start()
    assert parsed.wait(5)
    try:
        path.write_text(json.dumps(summary(jobs=[job(summary="New")])))
        assert work.read_source(entry(path), cfg(), NOW)["contents"]["jobs"][0]["summary"] == "New"
    finally:
        release.set()
        t.join(5)
    held = json.loads(work._last_file("atlas").read_text())["contents"]["jobs"][0]["summary"]
    assert held == "New"
    assert result[0]["contents"]["jobs"][0]["summary"] == "New"


def test_an_unreadable_waiting_write_is_a_work_problem_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    ledger.outbox_dir().mkdir(parents=True)
    (ledger.outbox_dir() / "00001-bad.json").write_text("{")
    _hub_away(monkeypatch)
    assert ledger.outbox_status()["unreadable"]
    v = work.build_view(cfg=cfg(), owed=NO_OWED, routing_info=ROUTING)
    assert v["problems"].get("outbox")


def test_no_offline_copy_with_the_hub_away_is_said_not_shown_as_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    _hub_away(monkeypatch)
    assert not hs.snapshot_path().exists()
    ledger.enqueue(ledger.new_op("add", P, "q", fields={"title": "Waiting", "kind": "slice"}))
    got = ledger.load(P)
    assert got["copy_unavailable"] and got["offline"] and got["as_of"] is None
    assert [r["row_id"] for r in got["rows"]] == ["q"] and got["pending"] == 1
    assert work.read_ledger()["error"]
    assert work.build_view(cfg=cfg(), owed=NO_OWED, routing_info=ROUTING)["problems"].get("ledger")


def test_a_row_carries_its_version(hub):
    ok("add", "r", "--title", "Row", "--kind", "slice")
    ok("set", "r", "--title", "Two")
    got = ledger.get(P, "r")
    assert ledger.row_json(got["row"], got["history"], P)["version"] == 2


def test_a_copy_without_receipts_still_reads(hub, tmp_path, monkeypatch):
    """A copy written before receipts were kept reads as before."""
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Row", "--kind", "slice")
    assert hs.refresh_ledger()["ok"]
    with sqlite3.connect(hs.snapshot_path()) as con:
        con.execute("DROP TABLE IF EXISTS ledger_receipts")
    _hub_away(monkeypatch)
    assert [r["row_id"] for r in ledger.load(P)["rows"]] == ["r"]


# ---- receipts never outrun their rows (round 2) ----------------------------


def _remote_apply(hub, op, monkeypatch):
    """Another Mac lands ``op``: only the hub is shared with this reader."""
    with monkeypatch.context() as m:
        m.setattr(ledger, "_mark_lag", lambda *a: None)
        m.setattr(ledger, "_replica_sync", lambda *a: None)
        with _real_connect(hub) as c:
            return ledger.apply_op(c, op, late=True)


def _waiting_done(tmp_path):
    _snapshot(tmp_path)
    ok("add", "r", "--title", "Row", "--kind", "slice")
    op = ledger.new_op("set", P, "r", fields={"state": "DONE"}, op_id="receipt-race")
    op["seen"] = {"state": "NOT STARTED"}
    ledger.enqueue(op)
    return op


def _land_during_receipt_read(hub, op, monkeypatch):
    real, calls = hs._read_ledger_receipts, []

    def racing(cur, ids):
        if ids and not calls:
            calls.append(_remote_apply(hub, op, monkeypatch))
        return real(cur, ids)

    monkeypatch.setattr(hs, "_read_ledger_receipts", racing)
    return calls


def test_catch_up_never_copies_a_receipt_newer_than_its_row(hub, tmp_path, monkeypatch):
    op = _waiting_done(tmp_path)
    ledger._mark_lag(P, ["r"])
    calls = _land_during_receipt_read(hub, op, monkeypatch)
    with _real_connect(hub) as c:
        assert ledger.replica_catch_up(c)["copied"] == 1
    assert calls and calls[0]["ok"]
    copy = hs.ledger_read_snapshot(P)
    assert copy["rows"][0]["state"] == "NOT STARTED"
    assert op["op_id"] not in (copy["receipts"] or {})
    shown = ledger._overlay(ledger._offline_view(P), P)["rows"][0]
    assert shown["state"] == "DONE" and shown["queued"]


def test_an_online_read_never_takes_a_receipt_newer_than_its_rows(hub, tmp_path, monkeypatch):
    op = _waiting_done(tmp_path)
    calls = _land_during_receipt_read(hub, op, monkeypatch)
    with _real_connect(hub) as c:
        shown = ledger._hub_overlay(c, ledger._hub_view(c, P), P)["rows"][0]
    assert calls and calls[0]["ok"]
    assert shown["state"] == "DONE" and shown["queued"]


def test_an_online_read_and_its_overlay_share_one_snapshot(hub, tmp_path, monkeypatch):
    op = _waiting_done(tmp_path)
    with _real_connect(hub) as c:
        view = ledger._hub_view(c, P)
        _remote_apply(hub, op, monkeypatch)
        shown = ledger._hub_overlay(c, view, P)["rows"][0]
    assert shown["state"] == "DONE" and shown["queued"]


def test_one_row_read_never_takes_a_receipt_newer_than_its_row(hub, tmp_path, monkeypatch):
    op = _waiting_done(tmp_path)
    monkeypatch.setattr(ledger, "drain", lambda *a, **k: ledger._drain_result())
    calls = _land_during_receipt_read(hub, op, monkeypatch)
    with _real_connect(hub) as c:
        got = ledger.get(P, "r", conn=c)
    assert calls and calls[0]["ok"]
    assert got["row"]["state"] == "DONE" and got["row"]["queued"]
    assert all(h["to_state"] != "DONE" for h in got["history"])


def test_a_snapshot_read_leaves_the_connection_as_it_found_it(hub):
    with _real_connect(hub) as c:
        before = c.isolation_level
        ledger._hub_view(c, P)
        assert c.isolation_level == before
        assert c.info.transaction_status == c.info.transaction_status.IDLE
