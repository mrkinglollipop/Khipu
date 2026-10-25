"""Replica reads of episode-linked rows have one forget fence:
``hub_snapshot.forget_fence_sql``.

A forgotten episode's commitments, decisions and deliverables stay out of
every answer this Mac's copy gives, whether the forget is applied
(``deleted_at``) or only queued (the pending-erasure marker that
``open_snapshot``'s views honour). The source scan below fails when a
function reads one of those tables from the replica without the fence; the
runtime checks pin what the fence itself promises.
"""
from __future__ import annotations

import ast
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from khipu import hub_snapshot as hs
from khipu import offline_reads

PACKAGE = Path(hs.__file__).resolve().parent
LINKED = ("commitments", "decisions", "deliverables")
_SELECT = re.compile(r"\bSELECT\b.*?\bFROM\s+(?:main\.)?(" + "|".join(LINKED) + r")\b", re.S | re.I)
# Signs a function works on the sqlite replica rather than the hub.
_REPLICA_SIDE = ("con.execute", "open_snapshot(", "sqlite3.connect(", "_present(con")
# Functions that read a linked table on the replica side without the fence:
# (module, name) -> why.
UNFENCED = {
    ("hub_snapshot.py", "_write_copy"): "the copy's publication checks each vector against the rows it just wrote",
    ("hub_snapshot.py", "_lag_decisions"): "the freshness measurement compares every row with the hub by id",
    ("hub_snapshot.py", "_lag_deliverables"): "the freshness measurement compares every row with the hub by id",
}
# The readers the fence is known to guard: a scan that stops seeing them is broken.
KNOWN = {("hub_snapshot.py", "decision_counts_snapshot"), ("hub_snapshot.py", "recent_deliverables_snapshot"),
         ("offline_reads.py", "owed_rows"), ("offline_reads.py", "decisions"),
         ("recall_prompt.py", "_thread_memory_hits")}


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _replica_readers():
    """(module, function, fenced) for every function whose SQL selects from a
    linked table on the replica side (hub SQL carries ``%s``)."""
    for path in sorted(PACKAGE.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            segment = ast.get_source_segment(src, fn) or ""
            if not any(k in segment for k in _REPLICA_SIDE):
                continue
            reads = any((t := _text(n)) and "%s" not in t and _SELECT.search(t) for n in ast.walk(fn))
            if not reads:
                continue
            calls = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", None)
                     for c in ast.walk(fn) if isinstance(c, ast.Call)}
            yield str(path.relative_to(PACKAGE)), fn.name, "forget_fence_sql" in calls


def test_every_replica_read_of_episode_linked_rows_uses_the_fence():
    found = list(_replica_readers())
    unfenced = {(m, f) for m, f, fenced in found if not fenced}
    assert unfenced - set(UNFENCED) == set(), (
        "a replica read of commitments/decisions/deliverables skips hub_snapshot.forget_fence_sql")
    assert set(UNFENCED) <= unfenced, "an exemption no longer applies: remove it"
    assert KNOWN <= {(m, f) for m, f, fenced in found if fenced}


@pytest.fixture
def replica(tmp_path, monkeypatch):
    """Episodes 1 (live), 2 (forgotten), 3 (erasure queued); one decision,
    deliverable and open commitment for each, plus one with no episode."""
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    con = sqlite3.connect(hs.snapshot_path())
    hs._create_schema(con)
    con.executemany("INSERT INTO episodes (id, summary, deleted_at) VALUES (?, 's', ?)",
                    [(1, None), (2, "2026-10-02T00:00:00Z"), (3, None)])
    for eid in (1, 2, 3, None):
        rid = eid or 9
        con.execute("INSERT INTO decisions (id, text, episode_id, decided_at) VALUES (?, 'd', ?, '2026-10-01')",
                    (rid, eid))
        con.execute("INSERT INTO deliverables (id, project, kind, path, episode_id, created_at) "
                    "VALUES (?, 'p', 'file', 'x', ?, '2026-10-01')", (rid, eid))
        con.execute("INSERT INTO commitments (id, text, project, opened_episode, status) "
                    "VALUES (?, 'c', 'p', ?, 'open')", (rid, eid))
    con.commit()
    con.close()
    hs._queue_erasure(3)
    # These rows stand for a copy stamped against the registry with
    # episode 3's erasure queued: record that fingerprint, as the copy that
    # published them would (an unstamped replica shows no fact row, design
    # section 6.2).
    with closing(sqlite3.connect(hs.snapshot_path())) as con, con:
        hs._meta_put(con, hs._FACT_DIGEST_KEY, hs._current_refusal_digest(hs.snapshot_path()))
    return tmp_path


@pytest.mark.parametrize("table,col", [("decisions", "episode_id"), ("deliverables", "episode_id"),
                                       ("commitments", "opened_episode")])
def test_fence_leaves_out_forgotten_and_queued_erasures_on_any_connection(replica, table, col):
    for con in (hs.open_snapshot(), sqlite3.connect(hs.snapshot_path())):
        try:
            got = con.execute(f"SELECT id FROM {table} WHERE 1 = 1{hs.forget_fence_sql(con, table, col)} "
                              "ORDER BY id").fetchall()
        finally:
            con.close()
        assert got == [(1,), (9,)]


def test_fenced_readers_answer_without_forgotten_rows(replica):
    con = hs.open_snapshot()
    try:
        assert set(hs.decision_counts_snapshot(con, [1, 2, 3])) == {"1"}
        assert [r["id"] for r in hs.recent_deliverables_snapshot(con, project="p")] == [9, 1]
    finally:
        con.close()
    assert sorted(r["id"] for r in offline_reads.owed(None, "open", 10)["results"]) == [1, 9]
    assert sorted(r["id"] for r in offline_reads.decisions(None, None, "all", 10)["results"]) == [1, 9]
