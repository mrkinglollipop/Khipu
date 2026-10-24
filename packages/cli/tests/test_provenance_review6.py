"""Sixth Astra review (jobs/2026-10-23/astra-prov6), ported.

The replica follows the standard snapshot contract (owner decision, Matt,
2026-10-24; design section 6): a read that began before a forget completed
may reflect pre-forget state; a read that begins after never does; the
replica on disk keeps no refused row past the rebuild. Review 6's P1 (a
materialized response keeps the rows its read saw) is accepted behaviour
under that contract, so its tests assert the contract. The two P2s (an older
rebuild erasing a newer ordinary pull; the explicit commitment backfill
writing a stale hub vector) failed on 498979ec."""
from __future__ import annotations

import json
from unittest import mock

import pytest

from khipu import capture, commitments, decisions, deliverables, embed, forget, offline_reads
from khipu import hub_snapshot as hs
from khipu import recall_prompt as rp
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401 - fixtures
from tests.test_provenance_forget import ALT, CANON, PROJECT, _capture, _payload, env, pg  # noqa: F401
from tests.test_provenance_replica import _replica, _replica_readers, replica  # noqa: F401


def _raw_fact_text() -> str:
    """Every fact row the replica file holds, unfenced."""
    return json.dumps([_replica("SELECT text, latest_text FROM commitments"),
                       _replica("SELECT text FROM decisions"),
                       _replica("SELECT title, path FROM deliverables"),
                       _replica("SELECT chunk_text FROM memory_embeddings WHERE kind = 'commitment'")])


@pytest.mark.parametrize("reader", ["owed", "decisions"])
def test_offline_reads_follow_the_snapshot_contract(pg, replica, reader):
    """Review 6 #1 (accepted): the read in flight while the forget completes
    may return its pre-forget rows; a read begun after returns none, and the
    replica file keeps no refused row."""
    replica(pg)
    pg.memory.mkdir()
    eid = _capture(loop=CANON, decision="private decision before forget")
    assert hs.refresh()["ok"]
    fired = []
    target, attr = (commitments, "finish_owed_rows") if reader == "owed" else (decisions, "state_of")
    real = getattr(target, attr)

    def interleave(*a, **kw):
        if not fired:
            fired.append(True)
            assert forget.forget_everywhere(eid, memory_root=pg.memory, gateway=False)["ok"]
            assert not hs.facts_rebuilding()
        return real(*a, **kw)

    def read():
        return (offline_reads.owed(None, "open", 100) if reader == "owed"
                else offline_reads.decisions(None, None, "standing", 100))

    with mock.patch.object(target, attr, interleave):
        read()  # in flight: may reflect pre-forget state
    assert fired
    assert not read()["results"]
    assert CANON not in _raw_fact_text() and "private decision before forget" not in _raw_fact_text()


def test_prompt_deliverable_line_follows_the_snapshot_contract(pg, replica):
    """Review 6 #1 (accepted), prompt deliverable line."""
    replica(pg)
    pg.memory.mkdir()
    eid = _capture(loop=None, title="Private release checklist")
    assert hs.refresh()["ok"]
    real = deliverables.line_from_rows
    fired = []

    def interleave(*a, **kw):
        if not fired:
            fired.append(True)
            assert forget.forget_everywhere(eid, memory_root=pg.memory, gateway=False)["ok"]
        return real(*a, **kw)

    with mock.patch.object(deliverables, "line_from_rows", interleave):
        rp._replica_deliverable_line(PROJECT, ["docs", "checklist"], stale_box={})
    assert fired
    assert not rp._replica_deliverable_line(PROJECT, ["docs", "checklist"], stale_box={})
    assert "Private release checklist" not in _raw_fact_text()


def test_prompt_thread_hits_after_a_forget_show_nothing(pg, replica):
    """Review 6 #1 (accepted), prompt thread recall: a read begun after the
    forget completed, with the hub away, shows no forgotten decision."""
    replica(pg)
    pg.memory.mkdir()
    payload = _payload(loop=None, decision="Private thread decision", t3_thread_id="synthetic-thread")
    eid = capture.write_pg(payload)["episode_id"]
    assert hs.refresh()["ok"]
    assert forget.forget_everywhere(eid, memory_root=pg.memory, gateway=False)["ok"]
    with mock.patch.object(hs, "try_hub_connect", side_effect=ConnectionError("hermetic hub away")):
        hits = rp._thread_memory_hits("synthetic-thread", progress={})
    assert "Private thread decision" not in json.dumps(hits, default=str)
    assert "Private thread decision" not in _raw_fact_text()


def test_healthy_rebuild_cannot_overwrite_newer_normal_pull(pg, replica):
    """Review 6 #2 (P2): an ordinary pull between a rebuild's hub read and
    its load is a newer generation; the older read is discarded."""
    replica(pg)
    _capture(loop=CANON)
    assert hs.refresh()["ok"]
    real = hs._read_all_facts
    fired = []

    def interleave(cur):
        read = real(cur)
        if fired:
            return read
        fired.append(True)
        _capture(loop=None, decision="new normal pull decision")
        assert hs.pull_changes(include_ledger=False)["ok"]
        assert "new normal pull decision" in _replica_readers()
        return read

    with mock.patch.object(hs, "_read_all_facts", interleave):
        result = hs.rebuild_facts()
    assert "new normal pull decision" in _replica_readers(), "older rebuild erased a newer normal pull"
    assert result.get("superseded")


def test_public_scrub_rebuild_cannot_regress_normal_pull(pg, replica):
    """Review 6 #2 (P2), through the public scrub: another rebuild completes
    after the scrub's drop, then an ordinary pull lands during the scrub's
    own rebuild read."""
    replica(pg)
    _capture(loop=CANON)
    assert hs.refresh()["ok"]
    real_rebuild = hs.rebuild_facts
    real_read = hs._read_all_facts
    fired = []

    def read_interleave(cur):
        read = real_read(cur)
        if not fired:
            fired.append(True)
            _capture(loop=None, decision="public scrub newest decision")
            assert hs.pull_changes(include_ledger=False)["ok"]
            assert "public scrub newest decision" in _replica_readers()
        return read

    def scheduled_rebuild(**kw):
        assert real_rebuild(**kw)["rebuilt"]
        with mock.patch.object(hs, "_read_all_facts", read_interleave):
            return real_rebuild(**kw)

    with mock.patch.object(hs, "rebuild_facts", scheduled_rebuild):
        out = hs.scrub_forgotten_snapshot()
    assert out["ok"] and fired
    assert "public scrub newest decision" in _replica_readers(), \
        "public scrub published older facts over successful normal pull"


def test_manual_commitment_backfill_does_not_restore_old_hub_chunks(pg, replica):
    """Review 6 #3 (P2): ``backfill(kind='commitment')`` writes a vector only
    while the locked hub row still carries the wording it embedded."""
    replica(pg)
    pg.memory.mkdir()
    a = _capture(loop=CANON)
    _capture(loop=ALT)
    assert hs.refresh()["ok"]
    with pg.connect() as c:
        _profile, dim = c.execute("SELECT id,dim FROM embedding_profiles WHERE is_active LIMIT 1").fetchone()
        cid = c.execute("SELECT id FROM commitments").fetchone()[0]
    vector = [1.0] + [0.0] * (dim - 1)
    calls = []

    def provider(texts, **kw):
        calls.append(texts)
        if len(calls) == 1:
            assert forget.forget_everywhere(a, memory_root=pg.memory, gateway=False)["ok"]
        return [vector for _ in texts]

    with mock.patch.object(embed, "embed_batch", provider), \
            mock.patch.object(embed, "_api_texts", side_effect=lambda p, items: [t for _, t in items]), \
            mock.patch.object(embed, "episode_text", return_value=""):
        embed.backfill(kind="commitment")
        embed.embed_recent_missing(limit=1)
    assert hs.pull_changes(include_ledger=False)["ok"]
    with pg.connect() as c:
        hubrows = c.execute("SELECT chunk_text FROM memory_embeddings WHERE kind='commitment' AND ref=%s",
                            (str(cid),)).fetchall()
    assert CANON not in str(_replica("SELECT chunk_text FROM memory_embeddings WHERE kind='commitment'"))
    assert CANON not in str(hubrows), "manual backfill restored old wording on the hub"
