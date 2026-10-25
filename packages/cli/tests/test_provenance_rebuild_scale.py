# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""The replica fact rebuild is bounded and never blocks a capture (design
section 6, owner decision 2026-10-24).

A hermetic hub holds N facts (commitments, decisions and deliverables in a
2:2:1 mix, each claimed by its own anchor episode, ten facts per episode).
After a dump, a forget changes the refusal set; ``rebuild_facts`` drops the
replica's fact state and re-reads every fact through the source fence. The
test times it and, while the rebuild runs, makes a capture's live replica
patch, which must return within ``LIVE_PATCH_WAIT_S`` plus slack (deferred
or written, never blocked).

N defaults to 5,000 so the suite stays quick; the 50,000-fact measurement
in the design doc is ``PROVENANCE_REBUILD_BENCH_FACTS=50000``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from khipu import hub_snapshot as hs, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import PROJECT, env, pg  # noqa: F401
from tests.test_provenance_replica import replica  # noqa: F401
from tests.fact_seed import announce

FACTS = int(os.environ.get("PROVENANCE_REBUILD_BENCH_FACTS", "5000"))


def _seed(h, n: int) -> int:
    episodes = max(1, n // 10)
    commitments, decisions = (2 * n) // 5, (2 * n) // 5
    deliverables = n - commitments - decisions
    with h.connect() as conn:
        announce(conn)
        conn.execute(
            "INSERT INTO episodes (ts, session_id, summary, raw, harness, project, transcript_range) "
            "SELECT now(), 'codex:bench-' || g, 'bench episode ' || g, "
            "jsonb_build_object('ts', now()::text, 'summary', 'bench episode ' || g, "
            "'session_id', 'codex:bench-' || g, 'harness', 'codex', 'transcript_range', '0:1'), "
            "'codex', %s, '0:1' FROM generate_series(1, %s) g", (PROJECT, episodes))
        first = conn.execute("SELECT min(id) FROM episodes").fetchone()[0]
        conn.execute(
            "INSERT INTO commitments (text, project, owner, kind, opened_episode, content_hash) "
            "SELECT 'bench commitment ' || g, %s, 'agent', 'followup', %s + (g %% %s), 'bench-c-' || g "
            "FROM generate_series(1, %s) g", (PROJECT, first, episodes, commitments))
        conn.execute(
            "INSERT INTO decisions (project, text, episode_id) "
            "SELECT %s, 'bench decision ' || g, %s + (g %% %s) FROM generate_series(1, %s) g",
            (PROJECT, first, episodes, decisions))
        conn.execute(
            "INSERT INTO deliverables (project, kind, path, title, episode_id) "
            "SELECT %s, 'file', 'docs/bench-' || g || '.md', 'bench deliverable ' || g, %s + (g %% %s) "
            "FROM generate_series(1, %s) g", (PROJECT, first, episodes, deliverables))
        for kind, table, field, col, anchor in (("commitment", "commitments", "text", "text", "opened_episode"),
                                                ("decision", "decisions", "text", "text", "episode_id"),
                                                ("deliverable", "deliverables", "title", "title", "episode_id")):
            conn.execute(f"INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id) "
                         f"SELECT '{kind}', id, '{field}', {col}, 'episode', {anchor} FROM {table}")
            conn.execute(f"SELECT count(khipu_project_fact('{kind}', id)) FROM {table}")
    return episodes


def test_rebuild_is_bounded_and_does_not_block_a_live_patch(pg, replica):
    replica(pg)
    episodes = _seed(pg, FACTS)
    started = time.monotonic()
    dumped = hs.refresh()
    dump_s = time.monotonic() - started
    assert dumped["ok"], dumped
    held = sum(dumped["counts"][t] for t in ("commitments", "decisions", "deliverables"))
    assert held == FACTS, dumped["counts"]
    # A forget elsewhere in the registry, confirmed by the hub (which holds
    # no carrier of it): the refusal set changes, and the wait rule passes.
    unrelated = {"ts": "2026-10-24T09:00:00Z", "summary": "unrelated forgotten",
                 "session_id": "codex:unrelated", "transcript_range": "0:1"}
    tombstones.publish_payloads([unrelated])
    tombstones.record_confirmations({t: [] for t in tombstones.identity_strings(unrelated)})
    assert hs.facts_rebuilding()
    with pg.connect() as conn:
        live_row = conn.execute("SELECT to_jsonb(e) FROM episodes e ORDER BY id DESC LIMIT 1").fetchone()[0]
    patch: dict = {}

    def live_patch():
        time.sleep(0.2)
        t0 = time.monotonic()
        patch["out"] = hs.upsert_episode(live_row, [])
        patch["seconds"] = time.monotonic() - t0

    t = threading.Thread(target=live_patch)
    t.start()
    out = hs.rebuild_facts()
    t.join(60)
    assert out["ok"] and out["rebuilt"], out
    assert out["commitments"] + out["decisions"] + out["deliverables"] == FACTS, out
    assert not hs.facts_rebuilding()
    print(json.dumps({"facts": FACTS, "episodes": episodes, "dump_seconds": round(dump_s, 2),
                      "rebuild_seconds": out["seconds"], "rebuild_read_seconds": out["read_seconds"],
                      "live_patch_seconds": round(patch["seconds"], 3), "live_patch": patch["out"]}))
    # Bounded: about one second per 2,500 facts on this hardware, with room.
    assert out["seconds"] < 10 + FACTS / 500, out
    # The live patch waited at most its bound, then deferred or wrote.
    assert patch["seconds"] < hs.LIVE_PATCH_WAIT_S + 3, patch
    assert patch["out"].get("ok") or patch["out"].get("deferred") or "in progress" in str(patch["out"]), patch
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == (2 * FACTS) // 5
