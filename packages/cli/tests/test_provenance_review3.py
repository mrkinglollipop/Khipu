# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""Third Codex Astra review of feat/fact-provenance (2026-10-23,
jobs/2026-10-23/astra-prov3): its reproductions, ported. Each failed on
ffd73155.

1. P1 the replica source fence misses a different episode's wording on a
   shared commitment (only the anchor was checked);
2. P2 JSONL recovery skips a distinct fact at an existing (ts, summary) key;
3. P2 a deferred recovery line takes the recovery caller's ownership;
4. P2 gap replay resolves decision insertion without the reversal
   detection that depends on it;
5. P2 a held replica write blocks capture and the pending-outbox search,
   and a changed registry makes the next write scrub the whole replica
   (every raw loaded at once) under that lock.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import tracemalloc
from unittest import mock

import psycopg
import pytest

from khipu import capture, hub_snapshot as hs, mirror, outbox, provenance, request_context, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import ALT, CANON, env, pg, _capture, _payload  # noqa: F401
from tests.test_provenance_replica import replica, _replica, _replica_readers  # noqa: F401


# ---- 1. P1 a non-anchor claimant's wording on a shared commitment ---------------


@pytest.mark.parametrize("method", ["pull", "dump", "scrub"])
def test_nonanchor_claimant_source_is_fenced(pg, replica, method):
    """A opens a commitment; a separate episode B deduplicates into it and
    supplies latest_text. The replica's registry then records B's source as
    forgotten (the hub is unchanged). B's words must leave A's commitment in
    the replica on every write path."""
    replica(pg)
    a = _capture(loop=CANON)
    bpayload = _payload(loop=ALT)
    b = capture.write_pg(dict(bpayload))["episode_id"]
    with pg.connect() as c:
        before = c.execute("SELECT opened_episode, text, latest_text FROM commitments").fetchall()
    assert before == [(a, CANON, ALT)], before
    assert a != b
    assert hs.refresh()["ok"]
    tombstones.publish_payloads([bpayload])
    for _ in range(2):
        out = (hs.pull_changes(include_ledger=False) if method == "pull" else hs.refresh()
               if method == "dump" else hs.scrub_forgotten_snapshot())
        assert out["ok"], out
    rows = _replica("SELECT opened_episode, text, latest_text FROM commitments")
    assert ALT not in str(rows), "forgotten claimant wording remained under a different anchor"
    assert ALT not in _replica_readers()
    # A's own wording stays: only B's contribution is fenced.
    assert rows == [(a, CANON, None)], rows
    with pg.connect() as c:
        assert c.execute("SELECT latest_text FROM commitments").fetchall() == [(ALT,)]


# ---- 2. P2 recovery at an existing legacy key -----------------------------------


@pytest.mark.parametrize("method", ["sync_recent_episodes", "reconcile_memory_root"])
def test_recovery_same_legacy_key_keeps_each_fact(pg, method):
    pg.memory.mkdir()
    a = _payload(loop=None, decision="Use weekly release trains")
    b = {**a, "decisions": ["Keep staging deployments isolated"]}
    (pg.memory / "episodes.jsonl").write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    getattr(mirror, method)(pg.memory)
    getattr(mirror, method)(pg.memory)
    with pg.connect() as c:
        rows = c.execute("SELECT text FROM decisions ORDER BY id").fetchall()
        gaps = c.execute("SELECT episode_id, step FROM fact_gaps").fetchall()
    jobs = [json.loads(p.read_text()) for p in outbox.jobs()]
    assert any("staging" in r[0] for r in rows) or gaps or jobs, \
        "second line evidence has no fact, gap, or queued deferral"
    # An equal replay of the first line is never queued.
    assert [j["payload"]["decisions"] for j in jobs] == [["Keep staging deployments isolated"]], jobs
    assert jobs[0].get("origin", "absent") is None


# ---- 3. P2 a deferred recovery line keeps unknown ownership ---------------------


@pytest.mark.parametrize("method", ["sync_recent_episodes", "reconcile_memory_root"])
def test_recovery_deferral_preserves_unknown_origin(pg, monkeypatch, method):
    pg.memory.mkdir()
    a = _payload(loop=None, decision="Use recovery line alpha")
    b = _payload(loop=None, decision="Use recovery line beta")
    (pg.memory / "episodes.jsonl").write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    real = provenance.record_claim
    fired = []

    def once(cur, kind, *args, **kw):
        if kind == "decision" and not fired:
            fired.append(True)
            raise psycopg.errors.QueryCanceled("synthetic per-line timeout")
        return real(cur, kind, *args, **kw)

    from khipu.request_context import request_origin

    # Normal request-origin stamping, not the shared fixture's anonymous stub.
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": request_origin()})
    with request_context.replaying("local:recovery-review"):
        with mock.patch.object(provenance, "record_claim", once):
            getattr(mirror, method)(pg.memory)
        outbox.drain()
    with pg.connect() as c:
        rows = c.execute("SELECT summary, origin FROM episodes ORDER BY summary").fetchall()
        facts = c.execute("SELECT text FROM decisions").fetchall()
    assert len(rows) == 2 and len(facts) == 2 and not outbox.jobs()
    assert all(r[1] is None for r in rows), "retry attributed a historical line to the recovery caller"


# ---- 4. P2 gap replay runs the dependent reversal detection ---------------------


def test_decision_gap_replay_keeps_reversal_evidence(pg):
    old = "Use blue-green deploys for rollout"
    new = "Use rolling deploys for rollout"
    _capture(loop=None, decision=old)
    payload = _payload(loop=None, decision=new, decision_details=[{"text": new, "by": "user", "reverses": old}])
    real = provenance.record_claim

    def broken(cur, kind, *args, **kw):
        if kind == "decision":
            raise RuntimeError("synthetic insert step failure")
        return real(cur, kind, *args, **kw)

    with mock.patch.object(provenance, "record_claim", broken):
        capture.write_pg(payload)
    with pg.connect() as c:
        assert c.execute("SELECT step FROM fact_gaps WHERE resolved_at IS NULL").fetchall() == [("decisions",)]
        result = provenance.replay_gaps(c)
        links = c.execute("SELECT kind FROM decision_links").fetchall()
        unresolved = c.execute("SELECT count(*) FROM fact_gaps WHERE resolved_at IS NULL").fetchone()[0]
    assert result["replayed"] == 1 and unresolved == 0, result
    assert links == [("supersedes",)], "replay resolved all gaps but omitted derivable reversal evidence"


# ---- 5. P2 replica lock waits and the changed-registry scrub --------------------


def test_registry_gate_does_not_block_capture_or_outbox_read(pg, replica, tmp_path):
    """Hold the real replica gate; capture (with a disposable hub) and the
    pending-outbox search in separate interpreters must finish while it is
    held. The prompt-time replica search was never blocked."""
    replica(pg)
    assert hs.refresh()["ok"]
    payload = _payload(loop=None)
    outbox.enqueue(_payload(loop=None), reason="synthetic pending recall")
    registry = str(tombstones.registry_path())
    snippets = {
        "capture": ("import psycopg;from khipu import db,capture,embed,topic_graph;"
                    f"db.connect=lambda *a,**k:psycopg.Connection.connect({pg.dsn!r});"
                    "embed.embed_on_capture=lambda *a,**k:None;"
                    "topic_graph.persist_capture_graph=lambda *a,**k:{};"
                    "import sys;sys.exit(capture.capture(payload, mode='hub'))"),
        "outbox_search": "from khipu import hub_snapshot;hub_snapshot.merge_outbox_episodes([],query='synthetic')",
        "prompt_recall": ("from khipu import recall_prompt;"
                          "print(recall_prompt._snapshot_search_hits('synthetic capture',project=None))"),
    }
    children = []
    with hs._replica_write_gate():
        for label, body in snippets.items():
            marker = tmp_path / f"{label}.entered"
            code = ("import json;from pathlib import Path;from khipu import tombstones;"
                    f"tombstones.registry_path=lambda:Path({registry!r});"
                    f"payload=json.loads({json.dumps(payload)!r});"
                    f"Path({str(marker)!r}).write_text('entered');" + body)
            e = {**os.environ, "KHIPU_HUB_FILE_MIRROR": "0"}
            p = subprocess.Popen([sys.executable, "-c", code], env=e, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
            children.append((label, p, marker))
        deadline = time.monotonic() + 30
        while (any(p.poll() is None for _l, p, _m in children)) and time.monotonic() < deadline:
            time.sleep(0.05)
        blocked = {label: p.poll() is None for label, p, _m in children}
    details = {}
    for label, p, _m in children:
        try:
            stdout, stderr = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            stdout, stderr = p.communicate()
            raise
        details[label] = {"rc": p.returncode, "stderr": stderr[-600:]}
    assert not any(blocked.values()), ("held replica gate blocks synchronous callers", blocked)
    assert all(d["rc"] == 0 for d in details.values()), details
    with pg.connect() as c:
        assert c.execute("SELECT count(*) FROM episodes WHERE summary = %s",
                         (payload["summary"],)).fetchone() == (1,)


def _bulk_replica(n: int, raw_bytes: int) -> None:
    path = hs.snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    hs._create_schema(con)
    filler = "x" * raw_bytes

    def rows():
        for i in range(n):
            p = {"ts": "2026-10-23T12:00:00Z", "summary": f"public {i}", "harness": "codex",
                 "session_id": f"large:{i}", "transcript_range": "0:1", "unrelated": filler}
            yield (i + 1, p["ts"], p["summary"], p["harness"], p["session_id"], p["transcript_range"],
                   json.dumps(p))

    con.executemany("INSERT INTO episodes(id,ts,summary,harness,session_id,transcript_range,raw) "
                    "VALUES(?,?,?,?,?,?,?)", rows())
    con.commit()
    con.close()


def _unrelated(n: int) -> dict:
    return {"ts": "2026-10-23T11:00:00Z", "summary": f"unrelated forgotten {n}",
            "session_id": f"forgotten:unrelated:{n}", "transcript_range": "0:1"}


def test_full_rescrub_memory_is_bounded(tmp_path, monkeypatch):
    """An explicit scrub of a large replica never loads every raw at once."""
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "large-state"))
    _bulk_replica(10_000, 8192)
    tombstones.publish_payloads([_unrelated(1)])
    tracemalloc.start()
    try:
        out = hs.scrub_forgotten_snapshot()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert out["ok"], out
    with sqlite3.connect(hs.snapshot_path()) as c:
        assert c.execute("SELECT count(*) FROM episodes WHERE deleted_at IS NULL").fetchone() == (10_000,)
    assert peak < 32 * 1024 * 1024, f"scrub peak {peak / 1048576:.1f} MiB"


def test_changed_registry_scrub_reads_only_matching_rows(tmp_path, monkeypatch):
    """After a full scrub indexed the replica, a registry change makes the
    next fenced write look only at the rows whose identities match it."""
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    _bulk_replica(3_000, 64)
    tombstones.publish_payloads([_unrelated(1)])
    assert hs.scrub_forgotten_snapshot()["ok"]
    real = hs._anchor_identities
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(hs, "_anchor_identities", counted)
    tombstones.publish_payloads([_unrelated(2)])
    with hs._replica_write_gate():
        pass
    assert len(calls) < 10, f"a changed registry re-read {len(calls)} episodes"
    # A change that names a held episode still erases exactly that one.
    target = {"ts": "2026-10-23T12:00:00Z", "summary": "public 7", "harness": "codex",
              "session_id": "large:7", "transcript_range": "0:1", "unrelated": "x" * 64}
    tombstones.publish_payloads([target])
    with hs._replica_write_gate():
        pass
    with sqlite3.connect(hs.snapshot_path()) as c:
        live = {r[0] for r in c.execute("SELECT id FROM episodes WHERE deleted_at IS NULL")}
    assert 8 not in live and len(live) == 2_999
    assert len(calls) < 20
