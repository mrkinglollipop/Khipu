# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""Codex Astra review of feat/fact-provenance (2026-10-23), findings 3 to 6:
its reproductions, ported. Each failed on 88620598 and passes with the fix.
Report: jobs/2026-10-23/astra-prov/report.md; fixes: jobs/2026-10-23/prov-fix1.
The fixture/helpers use tests.hermetic_pg only, with all provider hooks stubbed.
"""
from unittest import mock

import pytest

from khipu import capture, commitments as co, decisions, deliverables, forget, jobs, outbox, provenance
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import env, pg, _payload, PROJECT  # noqa: F401


def _forget(pg, eid):
    with pg.connect() as conn, conn.cursor() as cur:
        result = forget.forget_episode(cur, eid, memory_root=pg.memory)
        conn.commit()
        assert result["ok"] and result["soft_deleted"], result


def _finish_witness_backfill(pg):
    with pg.connect() as conn:
        assert provenance.backfill(conn)["ok"]


def test_old_exact_dedup_capture_is_not_gated_and_loses_live_facts(pg):
    """Run actual pre-provenance branches; no arbitrary bypass SQL."""
    params = {"decision": "Keep the release train weekly", "title": "the checklist"}
    first = capture.write_pg(_payload(**params))["episode_id"]
    _finish_witness_backfill(pg)
    with mock.patch.object(co, "_provenance_ready", return_value=False), \
         mock.patch.object(decisions, "_provenance_ready", return_value=False), \
         mock.patch.object(deliverables, "_provenance_ready", return_value=False):
        try:
            second = capture.write_pg(_payload(**params))["episode_id"]
        except capture.FactStepDeferred:
            return  # Refusal is the approved old-writer contract.
    assert second != first
    with pg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM fact_claims WHERE episode_id = %s", (second,)).fetchone()[0] == 0
    _forget(pg, first)
    with pg.connect() as conn, conn.cursor() as cur:
        observed = {
            "owed": co.list_owed(cur, project=PROJECT, limit=None),
            "decisions": decisions.list_decisions(cur, project=PROJECT, status="standing"),
            "deliverables": deliverables.recent_deliverables(cur, project=PROJECT),
        }
    assert all(observed.values()), observed


def test_old_owner_supersede_reusing_existing_decision_loses_owner_endorsement(pg):
    anchor = capture.write_pg(_payload(loop=None, decision="Keep the release train weekly"))["episode_id"]
    _finish_witness_backfill(pg)
    with pg.connect() as conn, conn.cursor() as cur:
        old = decisions.create_decision(cur, project=PROJECT, text="Release whenever ready")
        conn.commit()
    with mock.patch.object(decisions, "_provenance_ready", return_value=False):
        with pg.connect() as conn, conn.cursor() as cur:
            replacement = decisions.create_decision(cur, project=PROJECT, text="Keep the release train weekly",
                                                    decided_at="2026-10-23T23:59:59Z")
            assert decisions.supersede(cur, old, replacement, source="manual")
            # The old owner path records no owner claim: the writer gate
            # refuses it at COMMIT (the approved old-writer contract, 5.2),
            # rather than committing a supersession a forget then undoes.
            with pytest.raises(Exception) as refused:
                conn.commit()
            assert provenance.is_old_writer_refusal(refused.value), refused.value
    with pg.connect() as conn:
        assert conn.execute("SELECT superseded_by FROM decisions WHERE id = %s", (old,)).fetchone() == (None,)
        replacement = conn.execute("SELECT id FROM decisions WHERE episode_id = %s", (anchor,)).fetchone()[0]
    # The same action by current code lands and keeps the replacement.
    with pg.connect() as conn, conn.cursor() as cur:
        assert decisions.supersede(cur, old, replacement, source="manual")
        conn.commit()
    _forget(pg, anchor)
    with pg.connect() as conn:
        row = conn.execute("SELECT retracted_at, retract_reason FROM decisions WHERE id = %s", (replacement,)).fetchone()
    assert row == (None, None), row


def test_nightly_does_not_replay_fact_gap_after_outbox_ack(pg, monkeypatch):
    _finish_witness_backfill(pg)
    real = provenance.record_claim

    def broken(cur, kind, *args, **kwargs):
        if kind == "decision":
            raise RuntimeError("one-off decision derivation bug")
        return real(cur, kind, *args, **kwargs)

    monkeypatch.setattr(provenance, "record_claim", broken)
    outbox.enqueue(_payload(loop=None, decision="Keep the release train weekly"), reason="synthetic")
    drained = outbox.drain()
    assert drained["replayed"] == 1 and not outbox.jobs(), drained
    with pg.connect() as conn:
        assert conn.execute("SELECT step FROM fact_gaps WHERE resolved_at IS NULL").fetchall() == [("decisions",)]
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    monkeypatch.setattr(provenance, "record_claim", real)
    nightly = jobs._commitment_close_rules()
    assert nightly.get("ok"), nightly
    with pg.connect() as conn:
        n_facts = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
        open_gaps = conn.execute("SELECT count(*) FROM fact_gaps WHERE resolved_at IS NULL").fetchone()[0]
    assert (n_facts, open_gaps) == (1, 0), (n_facts, open_gaps, nightly)


def test_conflict_winner_closing_before_recheck_drops_new_capture_claim(pg, monkeypatch):
    """Two captures race; the winning row is closed after DO NOTHING.

    Hooks only order real source helpers and separate committed connections.
    No SQL error is injected, and the losing source is distinct and live.
    """
    real_near = co._near_duplicate_open
    real_exact = co._open_id_for_hash
    state = {"inside": False, "winner": None, "closed": False}

    def near(cur, *args, **kwargs):
        if state["inside"] or state["winner"] is not None:
            return real_near(cur, *args, **kwargs)
        state["inside"] = True
        try:
            winner_episode = capture.write_pg(_payload())["episode_id"]
            with pg.connect() as conn:
                state["winner"] = conn.execute("SELECT id FROM commitments WHERE opened_episode = %s",
                                                (winner_episode,)).fetchone()[0]
        finally:
            state["inside"] = False
        return None  # This capture's original empty lookup remains empty.

    def exact(cur, *args, **kwargs):
        if state["winner"] is not None and not state["inside"] and not state["closed"]:
            # Only runs on the post-ON-CONFLICT recheck. An ordinary lifecycle
            # write (owner close) is deliberately allowed by the migration gate.
            with pg.connect() as conn:
                conn.execute("UPDATE commitments SET status = 'closed', closed_at = now(), "
                             "close_reason = 'manual' WHERE id = %s", (state["winner"],))
                conn.commit()
            state["closed"] = True
        return real_exact(cur, *args, **kwargs)

    monkeypatch.setattr(co, "_near_duplicate_open", near)
    monkeypatch.setattr(co, "_open_id_for_hash", exact)
    losing = capture.write_pg(_payload())["episode_id"]
    assert state["closed"]
    with pg.connect() as conn:
        claim_count = conn.execute("SELECT count(*) FROM fact_claims WHERE episode_id = %s AND kind = 'commitment'",
                                   (losing,)).fetchone()[0]
        gap_count = conn.execute("SELECT count(*) FROM fact_gaps WHERE episode_id = %s", (losing,)).fetchone()[0]
    assert claim_count or gap_count, (losing, claim_count, gap_count)


@pytest.mark.parametrize("supersede_before_migration", [True, False], ids=["legacy", "current"])
def test_existing_id_owner_supersede_preserves_owner_chosen_replacement(env, supersede_before_migration):
    """Same API action is owner-endorsed by migration but not current code."""
    if not supersede_before_migration:
        env.to_0047()
    anchor = capture.write_pg(_payload(loop=None, decision="Keep the release train weekly"))["episode_id"]
    with env.connect() as conn, conn.cursor() as cur:
        replacement = conn.execute("SELECT id FROM decisions WHERE episode_id = %s", (anchor,)).fetchone()[0]
        old = decisions.create_decision(cur, project=PROJECT, text="Release whenever ready")
        # Existing-id CLI/MCP action: it does not pass through create_decision
        # for the replacement, and both code versions call this exact helper.
        assert decisions.supersede(cur, old, replacement, source="manual")
        conn.commit()
    if supersede_before_migration:
        env.to_0047()
    _finish_witness_backfill(env)
    _forget(env, anchor)
    with env.connect() as conn:
        row = conn.execute("SELECT retracted_at, retract_reason FROM decisions WHERE id = %s", (replacement,)).fetchone()
    assert row == (None, None), row


def test_gap_of_a_forgotten_episode_is_resolved_without_replay(pg, monkeypatch):
    """The replay never re-derives a forgotten capture's facts (I1)."""
    _finish_witness_backfill(pg)
    real = provenance.record_claim

    def broken(cur, kind, *args, **kwargs):
        if kind == "decision":
            raise RuntimeError("one-off decision derivation bug")
        return real(cur, kind, *args, **kwargs)

    monkeypatch.setattr(provenance, "record_claim", broken)
    eid = capture.write_pg(_payload(loop=None, decision="Keep the release train weekly"))["episode_id"]
    monkeypatch.setattr(provenance, "record_claim", real)
    _forget(pg, eid)
    with pg.connect() as conn:
        out = provenance.replay_gaps(conn)
    assert out["forgotten"] == 1 and out["replayed"] == 0, out
    with pg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
        assert conn.execute("SELECT resolution FROM fact_gaps").fetchall() == [("forgotten",)]


def test_failed_gap_replay_keeps_the_gap_open(pg, monkeypatch):
    _finish_witness_backfill(pg)

    def broken(cur, kind, *args, **kwargs):
        raise RuntimeError("still broken")

    real = provenance.record_claim
    monkeypatch.setattr(provenance, "record_claim", broken)
    capture.write_pg(_payload(loop=None, decision="Keep the release train weekly"))
    with pg.connect() as conn:
        out = provenance.replay_gaps(conn)
    monkeypatch.setattr(provenance, "record_claim", real)
    assert out["failed"] == 1, out
    with pg.connect() as conn:
        gap = conn.execute("SELECT resolved_at, attempts, resolution FROM fact_gaps").fetchone()
    assert gap[0] is None and gap[1] == 1 and gap[2].startswith("replay failed"), gap
