"""Offline owed and decisions reads, queued owner writes, and forget waiting
(offline and self-healing scope, slice 5). No hub and no database: the hub is
a patched connect that fails, or a fake applier."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from unittest import mock

import psycopg
import pytest

from khipu import cli, db
from khipu import hub_snapshot as hs
from khipu import mcp_server as srv
from khipu import offline_reads, owner_writes
from tests.test_owner_writes_exactly_once import tx_hub  # noqa: F401

DOWN = psycopg.OperationalError("connection timeout expired")
_REAL_LANDED = owner_writes._landed


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    (tmp_path / "data").mkdir()
    db._TABLE_COLUMNS_CACHE.clear()
    # No hub here: its record of applied writes holds none of these ids.
    monkeypatch.setattr(owner_writes, "_landed", lambda tool, args: None)
    return tmp_path / "data"


@pytest.fixture
def hub_down(monkeypatch):
    def refuse(*a, **k):
        raise DOWN

    monkeypatch.setattr("khipu.db.connect", refuse)


@pytest.fixture
def replica(home):
    con = sqlite3.connect(hs.snapshot_path())
    hs._create_schema(con)
    con.executemany("INSERT INTO episodes (id, summary, deleted_at) VALUES (?, 's', ?)",
                    [(1, None), (2, "2026-10-02T00:00:00Z")])
    con.executemany(
        "INSERT INTO commitments (id, text, project, owner, kind, opened_episode, opened_at, status, seen_count) "
        "VALUES (?, ?, 'p', NULL, 'followup', ?, ?, ?, 1)",
        [(1, "keep me", 1, "2026-10-01T00:00:00Z", "open"),
         (2, "forgotten", 2, "2026-10-01T00:00:00Z", "open"),
         (3, "done", 1, "2026-10-01T00:00:00Z", "closed")])
    con.executemany(
        "INSERT INTO decisions (id, project, text, decided_at, episode_id, superseded_by, retracted_at) "
        "VALUES (?, 'p', ?, ?, ?, ?, ?)",
        [(1, "stands", "2026-10-03T00:00:00Z", 1, None, None),
         (2, "replaced", "2026-10-02T00:00:00Z", 1, 1, None),
         (3, "forgotten", "2026-10-04T00:00:00Z", 2, None, None),
         (4, "wrong", "2026-10-01T00:00:00Z", 1, None, "2026-10-05T00:00:00Z")])
    con.commit()
    con.close()
    return hs.snapshot_path()


def _queued(home):
    return sorted((home / "owner-writes").glob("*.json"))


def test_owed_with_the_hub_down_reads_the_replica_marked_stale(replica, hub_down):
    out = srv._tool_owed({"project": "p"})
    assert [r["id"] for r in out["results"]] == [1]
    assert out["stale"] is True and out["results"][0]["stale"] is True
    assert isinstance(out["results"][0]["opened_at"], datetime)


def test_owed_with_no_replica_keeps_the_hub_error(home, hub_down):
    with pytest.raises(psycopg.OperationalError):
        srv._tool_owed({})


def test_owed_from_a_replica_without_the_table_keeps_the_hub_error(home, hub_down):
    con = sqlite3.connect(hs.snapshot_path())
    hs._create_schema(con, version=5)
    con.commit()
    con.close()
    assert offline_reads.owed(None, "open", 10) is None
    with pytest.raises(psycopg.OperationalError):
        srv._tool_owed({})


def test_decisions_with_the_hub_down_filter_and_skip_forgotten(replica, hub_down):
    standing = srv._tool_decisions({"project": "p"})
    assert [r["id"] for r in standing["results"]] == [1]
    assert standing["stale"] is True and standing["results"][0]["state"] == "current"
    everything = srv._tool_decisions({"status": "all"})
    assert {r["id"]: r["state"] for r in everything["results"]} == {1: "current", 2: "superseded", 4: "retracted"}
    conflicts = srv._tool_decisions({"status": "conflicts"})
    assert conflicts["results"] == [] and "hub" in conflicts["note"]


def test_a_close_made_with_the_hub_away_waits_then_replays_once(home):
    with mock.patch.object(srv, "_owed_update_on_hub", side_effect=DOWN):
        out = srv._tool_owed_update({"id": 5, "action": "close"})
    assert out["waiting_to_sync"] is True and out["ok"] is True and out["id"] == 5
    assert len(_queued(home)) == 1
    assert owner_writes.pending_marks("owed_update") == {5: "close"}
    applied = mock.Mock(return_value={"ok": True})
    with mock.patch.object(srv, "_owed_update_on_hub", applied):
        res = owner_writes.drain()
        again = owner_writes.drain()
    assert res["replayed"] == 1 and again["replayed"] == 0
    assert applied.call_count == 1
    assert _queued(home) == []


def test_a_refused_replay_moves_aside_with_its_error(home):
    with mock.patch.object(srv, "_owed_update_on_hub", side_effect=DOWN):
        srv._tool_owed_update({"id": 5, "action": "close"})
    with mock.patch.object(srv, "_owed_update_on_hub", side_effect=ValueError("no such commitment: 5")):
        res = owner_writes.drain()
    assert res["refused"] == 1 and _queued(home) == []
    kept = json.loads(next((home / "owner-writes" / "refused").glob("*.json")).read_text())
    assert "no such commitment" in kept["error"] and kept["args"]["id"] == 5
    assert owner_writes.status()["refused"] == 1


def test_a_hub_that_is_still_away_keeps_every_waiting_write(home):
    with mock.patch.object(srv, "_owed_update_on_hub", side_effect=DOWN):
        srv._tool_owed_update({"id": 1, "action": "close"})
        srv._tool_owed_update({"id": 2, "action": "reopen"})
        res = owner_writes.drain()
    assert res["stopped_early"] and res["waiting"] == 2
    assert owner_writes.status()["pending"] == 2


def test_a_new_write_never_lands_ahead_of_a_waiting_one(home):
    with mock.patch.object(srv, "_owed_update_on_hub", side_effect=DOWN):
        srv._tool_owed_update({"id": 1, "action": "close"})
    order = []
    with mock.patch.object(srv, "_owed_update_on_hub", side_effect=lambda a: order.append(a["id"]) or {"ok": True}):
        srv._tool_owed_update({"id": 2, "action": "close"})
    assert order == [1, 2] and _queued(home) == []
    # And while the hub stays away the newer one waits behind the older.
    with mock.patch.object(srv, "_owed_update_on_hub", side_effect=DOWN):
        srv._tool_owed_update({"id": 3, "action": "close"})
        srv._tool_owed_update({"id": 4, "action": "close"})
    ids = [json.loads(p.read_text())["args"]["id"] for p in _queued(home)]
    assert ids == [3, 4]


def test_the_gateway_never_queues(home):
    with mock.patch.object(srv, "_via_https_gateway", return_value=True), \
            mock.patch.object(srv, "_owed_update_on_hub", side_effect=DOWN):
        with pytest.raises(psycopg.OperationalError):
            srv._tool_owed_update({"id": 5, "action": "close"})
    assert _queued(home) == []


def test_a_decision_write_with_the_hub_away_waits(home):
    with mock.patch.object(srv, "_decisions_update_on_hub", side_effect=DOWN):
        out = srv._tool_decisions_update({"id": 7, "action": "retract", "reason": "wrong"})
        with pytest.raises(ValueError):
            srv._tool_decisions_update({"action": "retract"})  # still validated before anything waits
    assert out["waiting_to_sync"] and out["action"] == "retract"
    assert owner_writes.pending_marks("decisions_update") == {7: "retract"}


def test_rows_with_a_waiting_write_say_so(replica, hub_down):
    with mock.patch.object(srv, "_owed_update_on_hub", side_effect=DOWN):
        srv._tool_owed_update({"id": 1, "action": "close"})
    out = srv._tool_owed({"project": "p"})
    assert out["results"][0]["pending_sync"] == "close"
    assert out["waiting_to_sync"]["pending"] == 1


def test_forget_with_the_hub_away_says_waiting_and_changes_nothing(replica, hub_down, home):
    before = replica.read_bytes()
    with pytest.raises(srv._ToolFailure) as caught:
        srv._tool_forget({"id": 1})
    payload = caught.value.payload
    assert payload["waiting"] is True and payload["ok"] is False and payload["complete"] is False
    assert "NOT forgotten" in payload["message"]
    assert replica.read_bytes() == before
    assert not (home / "owner-writes").exists() and not (home / "forgotten_sources.json").exists()


class _Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchall(self):
        last = self.sql[-1]
        if "information_schema" in last:
            return [("id",), ("text",), ("status",), ("opened_episode",), ("deleted_at",), ("opened_at",)]
        return [(1, "kept", 10, datetime(2026, 10, 1, tzinfo=timezone.utc), "open")]


def test_the_dump_asks_the_hub_to_leave_out_forgotten_commitments(home):
    con = sqlite3.connect(":memory:")
    hs._create_schema(con)
    cur = _Cur()
    assert hs._insert_commitments(cur, con) == 1
    assert "e.deleted_at IS NOT NULL" in cur.sql[-1]
    assert con.execute("SELECT id, text FROM commitments").fetchall() == [(1, "kept")]


def test_cli_owed_envelope_offline(replica, hub_down, capsys):
    args = cli.build_parser().parse_args(["owed", "--project", "p", "--envelope"])
    assert args.func(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in out["results"]] == [1]
    assert out["stale"] is True and out["waiting_to_sync"]["pending"] == 0
    assert "snapshot_refreshed_at" in out


def test_cli_owed_close_offline_waits(home, hub_down, capsys):
    args = cli.build_parser().parse_args(["owed", "--close", "9"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "id": 9, "status": "closed", "waiting_to_sync": True}
    assert len(_queued(home)) == 1


def test_config_round_trip_for_the_statement_timeout(home, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["config", "--set", "hub_statement_timeout", "10"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["hub_statement_timeout"]["value"] == 10
    from khipu import offline

    assert offline.setting("hub_statement_timeout") == 10
    args = parser.parse_args(["config", "--unset", "hub_statement_timeout"])
    assert args.func(args) == 0
    assert offline.setting("hub_statement_timeout") == 30


def test_the_app_wait_setting_defaults_to_sixty_and_round_trips(home, capsys):
    from khipu import offline

    assert offline.SETTINGS["app_cli_timeout"][:2] == ("duration", 60)
    assert offline.setting("app_cli_timeout") == 60
    parser = cli.build_parser()
    args = parser.parse_args(["config", "--set", "app_cli_timeout", "0"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["app_cli_timeout"]["value"] == 0
    assert (home / "config.json").is_file()
    args = parser.parse_args(["config", "--unset", "app_cli_timeout"])
    assert args.func(args) == 0
    assert offline.setting("app_cli_timeout") == 60


# ---------------------------------------------------------------------------
# one replay ledger: order, exactly once, refusals kept
# ---------------------------------------------------------------------------

def test_a_crash_after_the_hub_commits_never_applies_the_write_twice(home, monkeypatch):
    owner_writes._enqueue("owed_update", {"id": 1, "action": "snooze", "until": "7d"}, "connection refused")
    commits = []

    def apply(tool, args):
        commits.append(dict(args))  # the hub's commit lands, then this process dies
        if len(commits) == 1:
            raise SystemExit("simulated process death after hub commit")
        return {"ok": True}

    monkeypatch.setattr(owner_writes, "_apply", apply)
    with pytest.raises(SystemExit):
        owner_writes.drain()
    assert owner_writes.status()["pending"] == 1
    monkeypatch.setattr(owner_writes, "_landed", lambda tool, args: ("applied", None))
    out = owner_writes.drain()
    assert len(commits) == 1, f"hub applied same write {len(commits)} times"
    assert out["replayed"] == 1 and _queued(home) == []


def test_a_send_the_hub_does_not_show_is_sent_again_once(home, monkeypatch):
    owner_writes._enqueue("owed_update", {"id": 1, "action": "close"}, "connection refused")
    commits = []

    def apply(tool, args):
        commits.append(args["id"])
        if len(commits) == 1:
            raise SystemExit("simulated process death before the hub answered")
        return {"ok": True}

    monkeypatch.setattr(owner_writes, "_apply", apply)
    with pytest.raises(SystemExit):
        owner_writes.drain()
    monkeypatch.setattr(owner_writes, "_landed", lambda tool, args: None)
    assert owner_writes.drain()["replayed"] == 1
    assert commits == [1, 1] and _queued(home) == []


def test_a_crash_during_a_direct_write_is_checked_on_the_hub_before_any_resend(home, monkeypatch):
    def die(tool, args):
        raise SystemExit("simulated process death during a direct write")

    monkeypatch.setattr(owner_writes, "_apply", die)
    with pytest.raises(SystemExit):
        owner_writes.run("owed_update", {"id": 4, "action": "close"})
    assert owner_writes.status()["pending"] == 1
    sent = []
    monkeypatch.setattr(owner_writes, "_apply", lambda tool, args: sent.append(args) or {"ok": True})
    monkeypatch.setattr(owner_writes, "_landed", lambda tool, args: ("applied", None))
    assert owner_writes.drain()["replayed"] == 1 and sent == []


def test_a_landed_check_that_cannot_reach_the_hub_keeps_the_write_waiting(home, monkeypatch):
    owner_writes._enqueue("owed_update", {"id": 1, "action": "close"}, "connection refused")
    path = _queued(home)[0]
    path.write_text(json.dumps({**json.loads(path.read_text()), "state": "sending"}))
    sent = []
    monkeypatch.setattr(owner_writes, "_apply", lambda tool, args: sent.append(args) or {"ok": True})

    def away(tool, args):
        raise DOWN

    monkeypatch.setattr(owner_writes, "_landed", away)
    out = owner_writes.drain()
    assert out["stopped_early"] and out["waiting"] == 1 and sent == []


def test_a_result_the_hub_refused_is_kept_as_refused_not_counted_as_done(home, monkeypatch):
    owner_writes._enqueue("decisions_update", {"id": 9, "action": "restore"}, "connection refused")
    monkeypatch.setattr(owner_writes, "_apply", lambda *_: {"ok": False, "id": 9, "action": "restore"})
    result = owner_writes.drain()
    assert result["refused"] == 1, result
    assert result["replayed"] == 0 and _queued(home) == []
    kept = json.loads(next((home / "owner-writes" / "refused").glob("*.json")).read_text())
    assert kept["args"]["id"] == 9 and "refused" in kept["error"]
    assert owner_writes.status()["refused"] == 1


def test_a_write_made_during_a_replay_cannot_be_overtaken_by_a_later_one(home, monkeypatch):
    owner_writes._enqueue("owed_update", {"id": 1, "action": "close"}, "connection refused")
    applying, finish = threading.Event(), threading.Event()
    enqueue_entered, release_enqueue = threading.Event(), threading.Event()
    calls = []

    def apply(tool, args):
        if args["id"] == 1:
            applying.set()
            assert finish.wait(5)
        calls.append(args["id"])
        return {"ok": True}

    monkeypatch.setattr(owner_writes, "_apply", apply)
    real_enqueue = owner_writes._enqueue

    def delayed_enqueue(tool, args, reason):
        if args["id"] == 2:
            enqueue_entered.set()
            assert release_enqueue.wait(5)
        return real_enqueue(tool, args, reason)

    monkeypatch.setattr(owner_writes, "_enqueue", delayed_enqueue)
    drain = threading.Thread(target=owner_writes.drain)
    newer = threading.Thread(target=lambda: owner_writes.run("owed_update", {"id": 2, "action": "reopen"}))
    drain.start()
    assert applying.wait(5)
    newer.start()
    assert enqueue_entered.wait(5)
    finish.set()
    drain.join(5)
    owner_writes.run("owed_update", {"id": 3, "action": "close"})
    release_enqueue.set()
    newer.join(5)
    owner_writes.drain()
    assert calls == [1, 2, 3], calls


def test_only_the_ledger_sends_an_owner_write_to_the_hub():
    """Every queued-or-direct owner write goes through owner_writes.run, so
    the order and exactly-once rules above hold for all of them."""
    import ast
    from pathlib import Path

    package = Path(owner_writes.__file__).resolve().parent
    hub_halves = {"_owed_update_on_hub", "_decisions_update_on_hub"}
    found = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "owner_writes.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            word = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if word in hub_halves:
                found.append(f"{path.name}:{node.lineno} {word}")
    assert not found, "send owner writes through owner_writes.run: " + ", ".join(found)
    callers = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "run" and getattr(node.func.value, "id", "") == "owner_writes":
                callers.append(path.name)
    assert sorted(set(callers)) == ["cli.py", "mcp_server.py"]


# Every action of every owner write tool, each made to be refused by the hub.
_REFUSED_WRITES = {
    "owed_update": [{"id": 1, "action": "close"}, {"id": 1, "action": "reopen"},
                    {"id": 1, "action": "snooze", "until": "7d"}],
    "decisions_update": [{"id": 1, "action": "supersede", "by": 2},
                         {"id": 1, "action": "supersede", "new_text": "scratch replacement"},
                         {"id": 1, "action": "restore"}, {"id": 1, "action": "retract"},
                         {"link_id": 1, "action": "confirm"}, {"link_id": 1, "action": "reject"}],
}


def test_every_owner_writer_records_its_outcome_with_the_write():
    """Sibling of the test above: each hub half applies through
    owner_writes.apply_on_hub, which records the outcome (applied or refused)
    in the write's transaction, and never commits or records on its own."""
    import ast
    import inspect
    import textwrap

    from khipu import mcp_server

    assert set(_REFUSED_WRITES) == set(owner_writes.TOOLS)
    for tool in owner_writes.TOOLS:
        fn = getattr(mcp_server, f"_{tool}_on_hub")
        calls = [n.func.attr for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn))))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        assert calls.count("apply_on_hub") == 1, tool
        assert not {"commit", "rollback", "record_on_hub"} & set(calls), (tool, calls)


@pytest.mark.parametrize("tool,args", [(t, a) for t, cases in _REFUSED_WRITES.items() for a in cases],
                         ids=lambda v: v if isinstance(v, str) else "-".join(map(str, v.values())))
def test_a_refused_owner_write_replays_as_refused_after_a_lost_reply(tx_hub, monkeypatch, tool, args):  # noqa: F811
    import psycopg

    from khipu import commitments, decisions

    monkeypatch.setattr(owner_writes, "_landed", _REAL_LANDED)
    tx_hub.missing = True
    monkeypatch.setattr(commitments, "set_status", lambda cur, cid, status, **kw: False)
    monkeypatch.setattr(decisions, "restore", lambda cur, did: False)
    monkeypatch.setattr(decisions, "retract", lambda cur, did, reason: False)
    monkeypatch.setattr(decisions, "supersede", lambda cur, old, new, **kw: False)
    monkeypatch.setattr(decisions, "_fetch_decision_row", lambda cur, did: {"id": did, "superseded_by": None})
    monkeypatch.setattr(decisions, "create_decision", lambda cur, **kw: 5)

    def resolve_link(cur, link_id, action, **kw):
        if action == "confirm":
            raise ValueError(f"no such link: {link_id}")
        return {"ok": False, "error": "decision_links table not migrated"}

    monkeypatch.setattr(decisions, "resolve_link", resolve_link)
    real_apply = owner_writes._apply

    def lost_reply(tool, args):
        try:
            assert real_apply(tool, args)["ok"] is False
        except ValueError:
            pass
        raise psycopg.OperationalError("server closed the connection after COMMIT")

    monkeypatch.setattr(owner_writes, "_apply", lost_reply)
    assert owner_writes.run(tool, dict(args))["waiting_to_sync"]
    monkeypatch.setattr(owner_writes, "_apply", lambda *a: pytest.fail("a refused write was sent again"))
    out = owner_writes.drain()
    assert out["refused"] == 1 and out["replayed"] == 0, out
    assert [o for o, _e in tx_hub.answered.values()] == ["refused"]
    assert tx_hub.commitment_writes == 0
