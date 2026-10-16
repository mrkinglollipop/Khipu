"""The incremental pull keeps every replica surface honest
(docs/plans/2026-10-16-offline-and-self-healing.md, design A and C): it heals
commitments and topic bodies as well as episodes, never lets an older hub read
replace a newer live capture, reports a failed part as a failure, keeps a
pending forget out of the offline owed and decisions reads, and marks a stale
handoff-only recall answer with its age. A fake hub answers each query by its
shape; no Postgres."""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import hub_snapshot as hs
from khipu import offline_reads
from khipu import recall_prompt as rp
from khipu import replica_heal as rh
from tests.test_replica_freshness import FakeCursor, FakeHub


class PullCursor(FakeCursor):
    """The pull's own queries: episodes by cursor, tombstone or id, changed
    commitments and topics; every other table answers empty."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=()):
        self.hub.queries.append(sql)
        if sql == "SELECT now()":
            self._rows = [(datetime.now(timezone.utc),)]
            return
        table = sql.split(" FROM ")[1].split()[0]
        cols = [c.strip().split(" AS ")[-1] for c in sql[len("SELECT "):sql.index(" FROM ")].split(",")]
        if table == "episodes":
            rows = [dict(r) for r in self.hub.episodes]
            if "WHERE (ingested_at, id) >" in sql:
                rows = sorted([r for r in rows if (r["ingested_at"], r["id"]) > (params[0], params[1])],
                              key=lambda r: (r["ingested_at"], r["id"]))[:params[2]]
            elif "WHERE deleted_at >" in sql:
                rows = sorted([r for r in rows if r.get("deleted_at") and r["deleted_at"] > params[0]],
                              key=lambda r: (r["deleted_at"], r["id"]))[:params[1]]
            elif "WHERE id = ANY" in sql:
                rows = [r for r in rows if r["id"] in params[0]]
            else:
                raise AssertionError(sql)
        elif table == "topics":
            if "WHERE updated_at > %s" not in sql:
                raise AssertionError(sql)
            rows = sorted([dict(t) for t in self.hub.topics
                           if t["updated_at"] > params[0] or (t.get("deleted_at") and t["deleted_at"] > params[0])],
                          key=lambda t: (t["updated_at"], t["slug"]))[:params[-1]]
        elif table == "commitments":
            if "ORDER BY id" not in sql:
                raise AssertionError(sql)
            gone = {e["id"] for e in self.hub.episodes if e.get("deleted_at")}
            rows = sorted([dict(c) for c in self.hub.commitments if c.get("opened_episode") not in gone],
                          key=lambda c: c["id"])
        elif table in ("memory_embeddings", "decisions", "deliverables"):
            self._rows = []
            return
        else:
            raise AssertionError(sql)
        self._rows = [tuple(r.get(c) for c in cols) for r in rows]


class PullHub(FakeHub):
    def cursor(self):
        return PullCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.fixture
def replica(tmp_path, monkeypatch):
    """A replica dumped 7 hours ago with one episode, one topic, one open
    commitment and one decision; the hub has since closed the commitment
    and corrected the topic."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    now = datetime.now(timezone.utc)
    dump = now - timedelta(hours=7)
    hub = PullHub()
    hub.episodes = [{"id": 1, "summary": "old summary", "ingested_at": dump - timedelta(hours=1),
                     "deleted_at": None, "raw": {"t3_thread_id": "probe-thread"},
                     "session_id": "codex:probe", "harness": "codex"}]
    hub.topics = [{"slug": "probe", "body": "corrected", "updated_at": now}]
    hub.commitments = [{"id": 1, "text": "probe owed", "status": "closed", "opened_episode": 1,
                        "last_seen_at": now}]
    con = sqlite3.connect(hs.snapshot_path())
    hs._create_schema(con)
    con.execute("INSERT INTO episodes(id,summary,ingested_at,raw,session_id,harness) VALUES (1,?,?,?,?,?)",
                ("old summary", hub.episodes[0]["ingested_at"].isoformat(),
                 json.dumps({"t3_thread_id": "probe-thread"}), "codex:probe", "codex"))
    con.execute("INSERT INTO topics(slug,body) VALUES ('probe','original')")
    con.execute("INSERT INTO commitments(id,text,status,opened_episode) VALUES (1,'probe owed','open',1)")
    con.execute("INSERT INTO decisions(id,text,episode_id,decided_at) VALUES (1,'old decision',1,?)",
                (dump.isoformat(),))
    for key in ("refreshed_at", "hub_dumped_at"):
        con.execute("INSERT INTO snapshot_meta(key,value) VALUES (?,?)", (key, dump.isoformat()))
    con.commit()
    con.close()
    hs._write_meta({"refreshed_at": dump.isoformat(), "hub_dumped_at": dump.isoformat()})
    monkeypatch.setattr(hs, "try_hub_connect", lambda **_: hub)
    cols = {"episodes": set(hs._EPISODE_COLS), "decisions": set(hs._DECISION_COLS),
            "memory_embeddings": {"profile", "kind", "ref", "built_at"},
            "deliverables": set(hs._DELIVERABLE_COLS),
            "commitments": set(hs._COMMITMENT_COLS), "topics": set(hs._TOPIC_COLS)}
    monkeypatch.setattr(hs, "_pg_columns", lambda cur, table: cols.get(table, set()))
    return hub


def rows(sql):
    with sqlite3.connect(hs.snapshot_path()) as con:
        return con.execute(sql).fetchall()


@pytest.mark.parametrize("sql,expected", [
    ("SELECT status FROM commitments WHERE id=1", [("closed",)]),
    ("SELECT body FROM topics WHERE slug='probe'", [("corrected",)])])
def test_pull_heals_all_replica_surfaces(replica, sql, expected):
    assert hs.pull_changes()["ok"]
    assert rows(sql) == expected


def test_failed_decision_sync_does_not_claim_success(replica, monkeypatch):
    monkeypatch.setattr(hs, "sync_decision_changes", lambda: {"ok": False, "error": "hub connection lost"})
    out = hs.pull_changes()
    assert not out["ok"], out
    assert "decisions" in out["failed"], out


def test_pull_cannot_overwrite_newer_live_capture(replica, monkeypatch):
    hs._queue_missed_patch(1)
    old_read, proceed = threading.Event(), threading.Event()
    real_upsert = hs.upsert_episode

    def paused(row, *args, **kw):
        if row["summary"] == "old summary":
            old_read.set()
            assert proceed.wait(5)
        return real_upsert(row, *args, **kw)

    monkeypatch.setattr(hs, "upsert_episode", paused)
    outputs = []
    pull = threading.Thread(target=lambda: outputs.append(hs.pull_changes()))
    pull.start()
    try:
        assert old_read.wait(5)
        replica.episodes[0]["summary"] = "new live capture"
        assert real_upsert({"id": 1, "summary": "new live capture"}, [])["ok"]
    finally:
        proceed.set()
        pull.join(10)
    assert outputs[0]["ok"], outputs
    assert rows("SELECT summary FROM episodes WHERE id=1") == [("new live capture",)]


def test_handoff_only_recall_marks_stale_age(replica, monkeypatch):
    def down(**_kw):
        raise ConnectionError("hub unreachable")

    monkeypatch.setattr(hs, "try_hub_connect", down)
    prompt = "Context handoff (full):\nProvider context handoff. Thread: probe-thread.\n\nUser message:\nyes"
    out = rp.prior_work_for_prompt(prompt)
    assert out["hits"], out
    assert "7 hours old" in out["context"], out


def test_foreign_episode_and_forget_stay_clean(replica):
    replica.episodes.append({"id": 2, "summary": "foreign memory", "ingested_at": datetime.now(timezone.utc),
                             "deleted_at": None})
    assert hs.pull_changes()["ok"]
    assert rows("SELECT summary FROM episodes WHERE id=2") == [("foreign memory",)]
    replica.episodes[-1]["deleted_at"] = datetime.now(timezone.utc)
    assert hs.pull_changes()["ok"]
    assert rows("SELECT deleted_at IS NOT NULL FROM episodes WHERE id=2") == [(1,)]
    assert hs.upsert_episode({"id": 2, "summary": "late old copy"}, [])["ok"]
    assert rows("SELECT deleted_at IS NOT NULL FROM episodes WHERE id=2") == [(1,)]


@pytest.mark.parametrize("surface", ["owed", "decisions"])
def test_pending_forget_never_reappears_through_offline_reads(replica, surface):
    lock = hs._acquire_refresh_lock()
    try:
        erased = hs.forget_episode_in_snapshot(1)
        assert erased.get("pending"), erased
        con = hs.open_snapshot()
        try:
            assert con.execute("SELECT id FROM episodes WHERE id=1").fetchall() == []
        finally:
            con.close()
        out = (offline_reads.owed(None, "open", 10) if surface == "owed"
               else offline_reads.decisions(None, None, "all", 10))
        assert out["results"] == [], out
    finally:
        hs._release_refresh_lock(lock)


def test_nightly_job_and_capture_serialize_and_retry_missed_patch(replica, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()

    def insert_episodes(cur, con):
        entered.set()
        assert proceed.wait(5)
        for e in replica.episodes:
            con.execute("INSERT INTO episodes(id,summary,ingested_at) VALUES (?,?,?)",
                        (e["id"], e["summary"], e["ingested_at"].isoformat()))
        return len(replica.episodes)

    monkeypatch.setattr(hs, "_insert_episodes", insert_episodes)
    for name in ("_insert_topics", "_insert_topic_revisions", "_insert_nodes", "_insert_edges",
                 "_insert_profiles", "_insert_memory_embeddings", "_insert_decisions",
                 "_insert_deliverables", "_insert_commitments", "_insert_ledger"):
        monkeypatch.setattr(hs, name, lambda *a: 0)
    monkeypatch.setattr(rh, "_hub_reachable", lambda: (True, None))
    rh.set_setting("replica_refresh_after", 0)
    results = []
    nightly = threading.Thread(target=lambda: results.append(hs.refresh()))
    nightly.start()
    try:
        assert entered.wait(5)
        replica.episodes.append({"id": 2, "summary": "capture during dump",
                                 "ingested_at": datetime.now(timezone.utc)})
        assert not hs.upsert_episode(replica.episodes[-1], [])["ok"]
        assert 2 in hs._missed_patch_ids()
        assert not hs.refresh()["ok"]
        assert rh.run()["action"] == "skipped"
    finally:
        proceed.set()
        nightly.join(10)
    assert results[0]["ok"], results
    assert rh.run()["ok"]
    assert not hs._missed_patch_ids()
    assert rows("SELECT COUNT(*) FROM episodes WHERE id=2") == [(1,)]


# ---- the structural rules ---------------------------------------------------

PACKAGE = Path(hs.__file__).resolve().parent
_EPISODE_WRITE = re.compile(r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?episodes\b", re.I)
_REPLICA_SIDE = ("con.execute", "open_snapshot(", "sqlite3.connect(")
# Every function that writes replica episodes: (module, name) -> how the
# freshness rule holds there.
EPISODE_WRITERS = {
    ("hub_snapshot.py", "_upsert_episode_unfenced"): "the rule itself: pull, outbox replay and live patch all land here",
    ("hub_snapshot.py", "_insert_episodes"): "the dump into a fresh file; refresh records its read floor",
    ("hub_snapshot.py", "_apply_pending_erasures"): "a forget, which always lands",
    ("hub_snapshot.py", "forget_episode_in_snapshot"): "a forget, which always lands",
    ("hub_snapshot.py", "_scrub_forgotten"): "a forget, which always lands",
}
# Functions in recall_prompt that answer from the replica: each builds the one
# stale marker, unless it only works on rows another one already marked.
_REPLICA_ANSWER_CALLS = {"open_snapshot", "search_snapshot", "cosine_candidates_snapshot", "snapshot_is_fresh",
                         "semantic_search_snapshot"}
STALE_EXEMPT = {"_fuse_and_annotate": "metadata for rows _snapshot_search_hits already marked",
                "_cosine_worker": "the cosine leg inside _snapshot_search_hits, which marks the answer"}


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _functions(path):
    src = path.read_text(encoding="utf-8")
    for fn in ast.walk(ast.parse(src)):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", None)
                     for c in ast.walk(fn) if isinstance(c, ast.Call)}
            yield fn, ast.get_source_segment(src, fn) or "", calls


def test_every_replica_episode_writer_is_under_the_freshness_rule():
    found = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        for fn, segment, _calls in _functions(path):
            if not any(k in segment for k in _REPLICA_SIDE):
                continue
            if any((t := _text(n)) and "%s" not in t and _EPISODE_WRITE.search(t) for n in ast.walk(fn)):
                found.add((str(path.relative_to(PACKAGE)), fn.name))
    assert found == set(EPISODE_WRITERS), "a replica episode writer is not listed with how the freshness rule holds"
    calls = {fn.name: c for fn, _s, c in _functions(PACKAGE / "hub_snapshot.py")}
    assert {"_held_read_at", "_record_read_at"} <= calls["_upsert_episode_unfenced"]
    refresh_src = {fn.name: s for fn, s, _c in _functions(PACKAGE / "hub_snapshot.py")}["_refresh_locked"]
    assert "_ROWS_READ_FLOOR_KEY" in refresh_src
    pull_src = {fn.name: s for fn, s, _c in _functions(PACKAGE / "hub_snapshot.py")}["_pull_apply"]
    assert "read_at=read_at" in pull_src


def test_a_read_older_than_the_dump_never_replaces_its_row_but_a_tombstone_lands(replica):
    with sqlite3.connect(hs.snapshot_path()) as con:
        con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES (?, ?)",
                    (hs._ROWS_READ_FLOOR_KEY, repr(time.time())))
    before = time.time() - 60
    out = hs.upsert_episode({"id": 1, "summary": "read before the dump"}, [], read_at=before)
    assert out["ok"] and out["skipped"], out
    assert rows("SELECT summary, deleted_at IS NULL FROM episodes WHERE id=1") == [("old summary", 1)]
    assert 1 in hs._missed_patch_ids()
    out = hs.upsert_episode({"id": 1, "summary": "read before the dump", "deleted_at": "2026-10-16T00:00:00Z"},
                            [], read_at=before)
    assert out["ok"], out
    assert rows("SELECT summary, deleted_at IS NOT NULL FROM episodes WHERE id=1") == [("old summary", 1)]
    assert hs.upsert_episode({"id": 1, "summary": "live patch"}, [])["ok"]
    assert rows("SELECT summary FROM episodes WHERE id=1") == [("live patch",)]


def test_every_stale_recall_answer_path_builds_the_one_marker():
    found = {}
    for fn, _segment, calls in _functions(PACKAGE / "recall_prompt.py"):
        if calls & _REPLICA_ANSWER_CALLS:
            found[fn.name] = "_stale_marker" in calls
    unmarked = {name for name, marked in found.items() if not marked}
    assert unmarked == set(STALE_EXEMPT), "a recall answer from the replica does not carry the stale marker"
    assert {"_snapshot_search_hits", "_thread_memory_hits", "_replica_deliverable_line"} <= set(found)
    renderers = {fn.name for fn, _s, calls in _functions(PACKAGE / "recall_prompt.py") if "_stale_line" in calls}
    assert renderers == {"render_block"}


def test_failed_part_is_named_and_unreachable_hub_is_waited_out(replica, monkeypatch):
    monkeypatch.setattr(hs, "sync_decision_changes",
                        lambda: {"ok": False, "error": "OperationalError: timeout", "hub_unreachable": True})
    out = hs.pull_changes()
    assert not out["ok"] and out["failed"] == ["decisions"] and out["hub_unreachable"], out
    assert out["error"].startswith("decisions:"), out
    monkeypatch.setattr(hs, "sync_decision_changes",
                        lambda: {"ok": False, "not_applicable": True, "error": "hub decisions table not migrated"})
    assert hs.pull_changes()["ok"]
