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
import os
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
        if sql in ("SAVEPOINT ledger_pull", "ROLLBACK TO SAVEPOINT ledger_pull", "RELEASE SAVEPOINT ledger_pull"):
            self._rows = []
            return
        if sql == "SELECT now()":
            self._rows = [(datetime.now(timezone.utc),)]
            return
        table = sql.split(" FROM ")[1].split()[0]
        cols = [c.strip().split(" AS ")[-1] for c in sql[len("SELECT "):sql.index(" FROM ")].split(",")]
        if table == "episodes":
            rows = [dict(r) for r in self.hub.episodes]
            if "WHERE (changed_at, id) >" in sql:
                # The change stamp, or the ingest time on a row the stamp
                # has not reached yet, as the hub's migration 0032 reads it.
                def after(r):
                    if r.get("changed_at") is not None:
                        return (r["changed_at"], r["id"]) > (params[0], params[1])
                    return (r["ingested_at"], r["id"]) > (params[2], params[3])
                rows = sorted([r for r in rows if after(r)],
                              key=lambda r: (r.get("changed_at") or r["ingested_at"], r["id"]))[:params[4]]
            elif "WHERE (ingested_at, id) >" in sql:
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
            if sql == "SELECT slug FROM topics WHERE slug = ANY(%s)":
                self._rows = [(r["slug"],) for r in self.hub.topics if r["slug"] in params[0]]
                return
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
    hub.columns = cols
    monkeypatch.setattr(hs, "_pg_columns", lambda cur, table: hub.columns.get(table, set()))
    return hub


def rows(sql):
    with sqlite3.connect(hs.snapshot_path()) as con:
        return con.execute(sql).fetchall()


# Commitments heal with the fact copy (design 6.2; tests/test_provenance_copy.py).
@pytest.mark.parametrize("sql,expected", [
    ("SELECT body FROM topics WHERE slug='probe'", [("corrected",)])])
def test_pull_heals_all_replica_surfaces(replica, sql, expected):
    assert hs.pull_changes()["ok"]
    assert rows(sql) == expected


def test_a_failed_fact_copy_is_reported_apart_from_the_memory_parts(replica, monkeypatch):
    """Since phase 7B the pull's facts come with one copy (design 6.2),
    reported as ``facts``; a failed copy keeps the last good one, so it is
    no reason for the memory parts to fall back to a full dump."""
    monkeypatch.setattr(hs, "copy_facts", lambda **kw: {"ok": False, "error": "hub connection lost"})
    out = hs.pull_changes()
    assert out["ok"] and out["facts"] == {"ok": False, "error": "hub connection lost"}, out
    assert "facts" not in out.get("failed", []), out


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
                 "_insert_profiles", "_insert_memory_embeddings", "_insert_ledger"):
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


def test_stale_pull_vector_cannot_replace_new_capture_vector(replica, monkeypatch):
    old = dict(profile="probe-profile", kind="episode", ref="1", chunk_idx=0,
               content_hash="old", embedding="[1,0]", built_at=datetime.now(timezone.utc))
    newer = {**old, "content_hash": "new", "embedding": "[0,1]",
             "built_at": old["built_at"] + timedelta(seconds=1)}
    replica.columns["memory_embeddings"] = set(hs._EMBED_COLS)
    real_execute = PullCursor.execute

    def execute(cur, sql, params=()):
        if "FROM memory_embeddings" not in sql:
            return real_execute(cur, sql, params)
        if "GROUP BY profile, kind, ref" in sql:
            cur._rows = [(old["profile"], old["kind"], old["ref"], old["built_at"])]
        elif "WHERE profile = %s AND kind = %s" in sql:
            cur._rows = [tuple(old.get(c) for c in hs._EMBED_COLS)]
        else:
            cur._rows = []

    monkeypatch.setattr(PullCursor, "execute", execute)
    real_upsert = hs.upsert_embeddings

    def interleave(rows, **kwargs):
        # A live capture lands after the pull read its vectors, before it writes them.
        assert hs.upsert_episode({**replica.episodes[0], "summary": "new capture"}, [newer])["ok"]
        return real_upsert(rows, **kwargs)

    monkeypatch.setattr(hs, "upsert_embeddings", interleave)
    assert hs.pull_changes()["ok"]
    got = rows("SELECT content_hash FROM memory_embeddings WHERE kind='episode' AND ref='1'")
    assert got == [("new",)], f"older pull overwrote live capture vector: {got}"


def test_pulled_commitments_cannot_overwrite_newer_dump(replica, monkeypatch):
    """Since phase 7B a pull writes no commitment row: commitments come only
    with the fact copy, whose copies are serial (design 6.2), so no older
    pull read can replace a newer dump's."""
    import inspect

    assert "commitments" not in inspect.signature(hs._write_pulled_tables).parameters
    real_write = hs._write_pulled_tables

    def interleave(topics, aliases, **kw):
        with sqlite3.connect(hs.snapshot_path()) as con:
            con.execute("INSERT OR REPLACE INTO commitments(id,text,status,opened_episode) "
                        "VALUES (1,'updated by owner','open',1)")
        return real_write(topics, aliases, **kw)

    monkeypatch.setattr(hs, "_write_pulled_tables", interleave)
    assert hs.pull_changes()["ok"]
    got = rows("SELECT text,status FROM commitments WHERE id=1")
    assert got == [("updated by owner", "open")], f"a pull replaced commitments: {got}"


def test_foreign_in_place_capture_edit_arrives_within_refresh_window(replica, monkeypatch):
    # The hub stamps changed_at on every change (migration 0032); a foreign
    # capture merged into an older episode keeps its ingest time.
    replica.columns["episodes"] = set(hs._EPISODE_COLS) | {"changed_at"}
    replica.episodes[0]["summary"] = "new memory merged from a foreign capture"
    replica.episodes[0]["raw"]["merged_from"] = [{"source_id": "foreign-capture"}]
    replica.episodes[0]["changed_at"] = datetime.now(timezone.utc)
    monkeypatch.setattr(rh, "_hub_reachable", lambda: (True, None))
    out = rh.run()
    assert out["ok"] and out["action"] == "sync", out
    got = rows("SELECT summary FROM episodes WHERE id=1")
    assert got == [(replica.episodes[0]["summary"],)], f"heal reported success but omitted merged capture: {got}"
    assert not any("changed_at" in c for (c,) in rows("SELECT name FROM pragma_table_info('episodes')"))


def test_hub_without_change_stamp_still_pulls_by_ingest_time(replica):
    replica.episodes.append({"id": 2, "summary": "new on an old hub", "ingested_at": datetime.now(timezone.utc),
                             "deleted_at": None})
    assert hs.pull_changes()["ok"]
    assert rows("SELECT summary FROM episodes WHERE id=2") == [("new on an old hub",)]
    assert not any("changed_at" in q for q in replica.queries)


@pytest.mark.parametrize("when", ["before_rename", "after_rename"])
def test_new_missed_patch_survives_clear_race(replica, monkeypatch, when):
    hs._queue_missed_patch(1)
    cutoff = time.time()
    real_rename = os.rename
    fired = []

    def rename(src, dst, *a, **kw):
        src_path = Path(src)
        racing = src_path.name == "1" and src_path.parent == hs._missed_patch_dir() and not fired
        if racing and when == "before_rename":
            fired.append(True)
            assert hs._queue_missed_patch(1)
            os.utime(src_path, (cutoff + 1, cutoff + 1))
        out = real_rename(src, dst, *a, **kw)
        if racing and when == "after_rename":
            fired.append(True)
            assert hs._queue_missed_patch(1)
        return out

    monkeypatch.setattr(hs.os, "rename", rename)
    hs._clear_missed_patches({1}, written_before=cutoff)
    assert fired
    assert hs._missed_patch_ids() == {1}
    assert not [p for p in hs._missed_patch_dir().iterdir() if p.name.startswith(".")]


def test_missed_patch_older_than_the_read_is_cleared(replica):
    hs._queue_missed_patch(1)
    hs._queue_missed_patch(2)
    old = time.time() - 60
    os.utime(hs._missed_patch_dir() / "1", (old, old))
    hs._clear_missed_patches(written_before=old + 1)
    assert hs._missed_patch_ids() == {2}


# ---- the structural rules ---------------------------------------------------

PACKAGE = Path(hs.__file__).resolve().parent


def _write_pattern(table):
    return re.compile(r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?" + table + r"\b",
                      re.I)


_EPISODE_WRITE = _write_pattern("episodes")
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
# The same for replica vectors and replica commitments.
VECTOR_WRITERS = {
    ("hub_snapshot.py", "_write_pulled_tables"): "remote topic purges use the refresh lock and read_at protection",
    ("hub_snapshot.py", "upsert_embeddings"): "the rule itself, per (profile, kind, ref): pull and catch-up land here",
    ("hub_snapshot.py", "_upsert_episode_unfenced"): "the rule itself, per profile, with the episode row",
    ("hub_snapshot.py", "_insert_memory_embeddings"): "the dump into a fresh file; refresh records its read floor",
    ("hub_snapshot.py", "_apply_pending_erasures"): "a forget, which always lands",
    ("hub_snapshot.py", "forget_episode_in_snapshot"): "a forget, which always lands",
    ("hub_snapshot.py", "_scrub_forgotten"): "a forget, which always lands",
    ("hub_snapshot.py", "purge_topic_in_snapshot"): "a topic purge, which always lands",
    ("hub_snapshot.py", "_erase_episode_commitments"): "a forget, which always lands",
    ("hub_snapshot.py", "_drop_facts"): "only removes: a backup copy's or a restored replica's fact rows",
    ("hub_snapshot.py", "_write_copy"): "the fact copy's publication: copies are serial under fact-copy.lock",
    ("hub_snapshot.py", "_insert_vector_row"): "the row writer of the fact copy's publication",
}
COMMITMENT_WRITERS = {
    ("hub_snapshot.py", "_scrub_forgotten"): "a forget always lands before replica publication",
    ("hub_snapshot.py", "_write_commitment_rows"): "the fact copy's row writer only",
    ("hub_snapshot.py", "_erase_episode_commitments"): "a forget always lands",
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


def _replica_writers(pattern):
    found = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        for fn, segment, _calls in _functions(path):
            if not any(k in segment for k in _REPLICA_SIDE):
                continue
            if any((t := _text(n)) and "%s" not in t and pattern.search(t) for n in ast.walk(fn)):
                found.add((str(path.relative_to(PACKAGE)), fn.name))
    return found


def test_every_replica_episode_writer_is_under_the_freshness_rule():
    assert _replica_writers(_EPISODE_WRITE) == set(EPISODE_WRITERS), \
        "a replica episode writer is not listed with how the freshness rule holds"
    assert _replica_writers(_write_pattern("memory_embeddings")) == set(VECTOR_WRITERS), \
        "a replica vector writer is not listed with how the freshness rule holds"
    assert _replica_writers(_write_pattern("commitments")) == set(COMMITMENT_WRITERS), \
        "a replica commitments writer is not listed with how the freshness rule holds"
    fns = list(_functions(PACKAGE / "hub_snapshot.py"))
    calls = {fn.name: c for fn, _s, c in fns}
    for name in ("_upsert_episode_unfenced", "upsert_embeddings", "_write_pulled_tables"):
        assert {"_held_read_at", "_record_read_at"} <= calls[name], name
    assert {name for name, c in calls.items() if "_write_commitment_rows" in c} == {"_write_copy"}
    src = {fn.name: s for fn, s, _c in fns}
    assert "_ROWS_READ_FLOOR_KEY" in src["_refresh_locked"]
    pull = ast.parse(src["_pull_apply"].strip())
    for callee in ("upsert_episode", "upsert_embeddings", "_write_pulled_tables"):
        passed = [c for c in ast.walk(pull) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == callee
                  and any(k.arg == "read_at" and getattr(k.value, "id", None) == "read_at" for k in c.keywords)]
        assert passed, f"_pull_apply does not pass read_at to {callee}"


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


def test_an_unreachable_hub_during_the_fact_copy_is_reported(replica, monkeypatch):
    monkeypatch.setattr(hs, "copy_facts",
                        lambda **kw: {"ok": False, "error": "OperationalError: timeout", "hub_unreachable": True})
    out = hs.pull_changes()
    assert out["ok"] and out["facts"]["hub_unreachable"], out
    monkeypatch.setattr(hs, "copy_facts",
                        lambda **kw: {"ok": False, "not_ready": True, "error": "the hub is still indexing"})
    assert hs.pull_changes()["ok"]
