"""Forget in a project the witness backfill has not covered (2026-10-27).

Live evidence: every forget in an unbackfilled project ran the whole
project's witness backfill (one claim INSERT per witness) inside its own
transaction and deadline, timed out after 90 s, rolled back and repeated, so
nothing in that project could be forgotten. Forget now backfills only the
facts it re-projects (section 4's protection for exactly those facts) and
leaves the project pending for the nightly.
"""
from __future__ import annotations

import time

import pytest

from khipu import forget, provenance
from tests.hermetic_pg import chain_dir, hub, migration_sql, pg_cluster  # noqa: F401
from tests.test_provenance_forget import (  # noqa: F401
    ALT, PROJECT, _capture, _rows, _visible_commitments, env,
)

OTHER = "Migrate the billing exports to the new storage bucket"
WITNESSES = 10000
DEADLINE_S = 5.0


def test_a_large_unbackfilled_project_does_not_hold_forget_past_a_short_deadline(env):  # noqa: F811
    a = _capture()
    b = _capture(loop=ALT)  # a legacy restater of a's commitment
    _capture(loop=OTHER)
    with env.connect() as conn:
        # Legacy episodes restating another fact: the whole-project
        # backfill owes each a claim, one INSERT apiece.
        conn.execute(
            "INSERT INTO episodes (ts, session_id, harness, summary, project, raw) "
            "SELECT now(), 'codex:bulk-' || g, 'codex', 'bulk ' || g, %s, jsonb_build_object("
            "'open_loops', jsonb_build_array(jsonb_build_object("
            "'text', %s::text, 'kind', 'followup', 'owner', 'assistant'))) "
            "FROM generate_series(1, %s) g", (PROJECT, OTHER, WITNESSES))
        conn.commit()
    env.to_0047()
    with env.connect() as conn, conn.cursor() as cur:
        assert not provenance.backfilled(cur, PROJECT)

    def work(tx):
        out = forget.forget_episode(tx.cur, a, memory_root=None)
        tx.commit()
        return out

    start = time.monotonic()
    out = provenance.hub_call(work, deadline=start + DEADLINE_S)
    elapsed = time.monotonic() - start
    assert out["ok"] and out["soft_deleted"], out
    assert elapsed < DEADLINE_S, elapsed

    # The fact the forgotten episode anchored keeps its live witness's words.
    assert out["commitments_closed"] == 0
    assert out["provenance_backfill"]["claims"] >= 1
    assert [r[1:] for r in _visible_commitments(env) if r[1] == ALT] == [(ALT, None)]
    assert _rows(env, "SELECT count(*) FROM commitments WHERE opened_episode = %s", (b,)) == [(1,)]
    # No claim was written on facts the forget does not touch, and the
    # project stays pending for the nightly's whole pass.
    assert _rows(env, "SELECT count(*) FROM fact_claims fc JOIN episodes e ON e.id = fc.episode_id "
                      "WHERE e.session_id LIKE 'codex:bulk-%%'") == [(0,)]
    with env.connect() as conn, conn.cursor() as cur:
        assert not provenance.backfilled(cur, PROJECT)

    # The nightly's whole pass still covers the rest of the project.
    with env.connect() as conn:
        report = provenance.backfill(conn)
    assert report["claims"] >= WITNESSES, report
    with env.connect() as conn, conn.cursor() as cur:
        assert provenance.backfilled(cur, PROJECT)
    assert [r[1:] for r in _visible_commitments(env) if r[1] == ALT] == [(ALT, None)]


@pytest.mark.parametrize("whole", [True, False], ids=["whole-pass", "targeted"])
def test_an_owner_separated_twin_is_not_credited_to_the_forgotten_fact(env, whole):  # noqa: F811
    """Review P1 (2026-10-27): the targeted pass filtered the witness query to
    the covered facts, so the twin's own fact was invisible, the own-fact rule
    saw nothing and the twin restated the forgotten episode's commitment,
    leaving it open. Both passes must close it and claim nothing new."""
    twin = "Review the probe checklist before release"
    a = _capture(loop=twin, owner="assistant")
    b = _capture(loop=twin, owner="user")
    env.to_0047()
    if whole:
        with env.connect() as conn:
            provenance.backfill(conn)
    with env.connect() as conn, conn.cursor() as cur:
        out = forget.forget_episode(cur, a, memory_root=None)
        conn.commit()
    assert out["ok"] and out["soft_deleted"], out
    assert out["commitments_closed"] == 1, out
    fa, = _rows(env, "SELECT id FROM commitments WHERE opened_episode = %s", (a,))
    fb, = _rows(env, "SELECT id FROM commitments WHERE opened_episode = %s", (b,))
    assert _rows(env, "SELECT status, close_reason FROM commitments WHERE id = %s", fa) == [("closed", "forgotten")]
    assert _rows(env, "SELECT fact_id, field, episode_id FROM fact_claims WHERE kind = 'commitment' "
                      "AND claimant = 'episode' AND episode_id = %s", (b,)) == [(fb[0], "text", b)]


def test_a_targeted_forget_keeps_decisions_and_deliverables_witnessed_elsewhere(env):  # noqa: F811
    decision = "Keep the release train weekly"
    a = _capture(decision=decision, title="the checklist")
    b = _capture(loop=None, decision=decision, title="the checklist")
    env.to_0047()
    with env.connect() as conn, conn.cursor() as cur:
        out = forget.forget_episode(cur, a, memory_root=None)
        conn.commit()
    assert out["ok"] and out["soft_deleted"], out
    assert out["decisions_retracted"] == 0 and out["deliverables_removed"] == 0, out
    assert _rows(env, "SELECT text FROM decisions WHERE retracted_at IS NULL") == [(decision,)]
    assert _rows(env, "SELECT title FROM deliverables") == [("the checklist",)]
    assert _rows(env, "SELECT DISTINCT episode_id FROM fact_claims WHERE kind IN ('decision', 'deliverable') "
                      "AND claimant = 'episode'") == [(b,)]
    with env.connect() as conn, conn.cursor() as cur:
        assert not provenance.backfilled(cur, PROJECT)
