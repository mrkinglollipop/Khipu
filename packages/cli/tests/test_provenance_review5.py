"""Fifth Astra review's reproductions (jobs/2026-10-23/astra-prov5), ported:
each failed on a717b6ec. The rebuild rule's boundaries: an unfingerprinted
replica is unverified, the refusal set is read when SQL runs, a rebuild
publishes only into the generation it was read for, closure-only lines are
recovered, the hub binds a vector to its wording, a backup carries no fact
row of a refused generation."""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from khipu import capture, commitments, embed, forget, mirror, offline_reads, outbox, paths, tombstones
from khipu import hub_snapshot as hs
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401 - fixtures
from tests.test_provenance_forget import ALT, CANON, _capture, _payload, env, pg  # noqa: F401
from tests.test_provenance_replica import _replica, _replica_readers, hub_forget, replica  # noqa: F401


def test_first_live_patch_does_not_certify_unchecked_legacy_facts(pg, replica):
    """#1 (P1): a replica with no recorded fingerprint (written before the
    rule) is unverified: its first fenced write drops and rebuilds."""
    replica(pg)
    _capture(loop=CANON)
    private = _payload(loop=ALT)
    private_id = capture.write_pg(private)["episode_id"]
    live = _capture(loop=None)
    assert hs.refresh()["ok"]
    with sqlite3.connect(hs.snapshot_path()) as c:
        c.execute("DELETE FROM snapshot_meta WHERE key IN (?,?)", (hs._FACT_DIGEST_KEY, hs._SCRUB_DIGEST_KEY))
    unverified = (hs._replica_fact_state(), json.dumps(offline_reads.owed(None, "open", 100), default=str))
    tombstones.publish_payloads([private])
    with pg.connect() as c:
        row = c.execute("SELECT to_jsonb(e) FROM episodes e WHERE id=%s", (live,)).fetchone()[0]
    assert hs.forget_episode_in_snapshot(private_id)["ok"]
    hs.upsert_episode(row, [])
    assert hs.sync_fact_changes()["ok"]
    rows = offline_reads.owed(None, "open", 100)
    assert ALT not in json.dumps(rows, default=str), "first fenced write blessed unchecked legacy facts"
    # The wait rule (2026-10-24 ruling): live wording returns once the hub's
    # forget of the private episode lands.
    hub_forget(pg, private_id)
    assert hs.sync_fact_changes()["ok"]
    assert CANON in _replica_readers() and ALT not in _replica_readers()
    # Before the first write, the unfingerprinted replica served no fact row.
    assert unverified[0] == "unverified" and ALT not in unverified[1]


def test_reader_refusal_published_before_sql(pg, replica, monkeypatch):
    """#2 (P1): a refusal published after the fence SQL was built but before
    it ran is still honoured: the digest is read when SQLite runs it."""
    replica(pg)
    _capture(loop=CANON)
    private = _payload(loop=ALT)
    capture.write_pg(private)
    assert hs.refresh()["ok"]
    real = hs.forget_fence_sql
    fired = []

    def interleave(*a, **kw):
        sql = real(*a, **kw)
        if not fired:
            fired.append(True)
            tombstones.publish_payloads([private])
        return sql

    monkeypatch.setattr(hs, "forget_fence_sql", interleave)
    rows = offline_reads.owed(None, "open", 100)
    assert fired
    assert ALT not in json.dumps(rows, default=str)


def test_retained_vector_view_rechecks_registry(pg, replica):
    """#2 (P1): a connection opened before a refusal never serves a vector
    of the refused generation through its TEMP view."""
    replica(pg)
    private = _payload(loop=CANON)
    capture.write_pg(private)
    assert hs.refresh()["ok"]
    cid = _replica("SELECT id FROM commitments")[0][0]
    assert hs.upsert_embeddings([dict(profile="probe", kind="commitment", ref=str(cid), chunk_idx=0,
                                      chunk_text=CANON, embedding=[1.0, 0.0], content_hash=embed._md5(CANON),
                                      fact_wording=embed._md5(CANON))])["embeddings"] == 1
    con = hs.open_snapshot()
    try:
        before = con.execute("SELECT chunk_text FROM memory_embeddings WHERE kind='commitment'").fetchall()
        tombstones.publish_payloads([private])
        held = con.execute("SELECT chunk_text FROM memory_embeddings WHERE kind='commitment'").fetchall()
    finally:
        con.close()
    fresh = hs.open_snapshot()
    try:
        current = fresh.execute("SELECT chunk_text FROM memory_embeddings WHERE kind='commitment'").fetchall()
    finally:
        fresh.close()
    assert before == [(CANON,)]
    assert not current
    assert not held, "reader view captured the old refusal digest"


def test_older_rebuild_cannot_overwrite_newer_pull(pg, replica):
    """#3 (P2): a rebuild whose read predates a newer load is discarded."""
    replica(pg)
    _capture(loop=CANON)
    assert hs.refresh()["ok"]
    unrelated = _payload(loop=None)
    tombstones.publish_payloads([unrelated])
    # A forget the hub confirmed (it holds no carrier): the wait rule passes.
    tombstones.record_confirmations({t: [] for t in tombstones.identity_strings(unrelated)})
    with hs._replica_write_gate():
        pass
    assert hs.facts_rebuilding()
    real = hs._read_all_facts
    fired = []

    def interleave(cur):
        read = real(cur)
        if fired:
            return read
        fired.append(True)
        _capture(loop=None, decision="new decision after old rebuild snapshot")
        assert hs.pull_changes(include_ledger=False)["ok"]
        assert "new decision" in _replica_readers()
        return read

    with mock.patch.object(hs, "_read_all_facts", interleave):
        result = hs.rebuild_facts()
    assert "new decision" in _replica_readers()
    assert result.get("superseded")


@pytest.mark.parametrize("method", ["sync_recent_episodes", "reconcile_memory_root"])
@pytest.mark.parametrize("held", [False, True])
def test_closed_only_recovery(pg, method, held):
    """#4 (P2): a memory-root line carrying only ``closed_loops`` is recovered
    by both recovery entry points."""
    pg.memory.mkdir()
    _capture(loop=CANON)
    payload = _payload(loop=None, closed_loops=[{"text": "done: " + CANON}])
    if held:
        first = dict(payload)
        first.pop("closed_loops")
        capture.write_pg(first)
    (pg.memory / "episodes.jsonl").write_text(json.dumps(payload) + "\n")
    for _ in range(2):
        getattr(mirror, method)(pg.memory)
    with pg.connect() as c:
        status = c.execute("SELECT status FROM commitments").fetchone()[0]
        gaps = c.execute("SELECT step FROM fact_gaps WHERE resolved_at IS NULL").fetchall()
    jobs = [json.loads(p.read_text()) for p in outbox.jobs()]
    assert status == "closed" or gaps or jobs, "closure evidence silently skipped by recovery"


def test_actual_embed_catchup_stale_hub_vector(pg, replica):
    """#5 (P2): catch-up publishes a commitment vector only while the hub row
    still carries the wording it embedded."""
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

    with mock.patch.object(embed, "episode_text", return_value=""), \
            mock.patch.object(embed, "embed_batch", provider), \
            mock.patch.object(embed, "_api_texts", side_effect=lambda p, items: [t for _, t in items]):
        embed.embed_recent_missing(limit=1)
        embed.embed_recent_missing(limit=1)
    assert hs.pull_changes(include_ledger=False)["ok"]
    with pg.connect() as c:
        hubrows = c.execute("SELECT chunk_text FROM memory_embeddings WHERE kind='commitment' AND ref=%s",
                            (str(cid),)).fetchall()
    local = _replica("SELECT chunk_text FROM memory_embeddings WHERE kind='commitment'")
    # The probe asserted ``not local``; the canonical (ALT) wording's own
    # vector legitimately lands on the second catch-up, so the invariant is
    # that the forgotten wording is in neither store.
    assert CANON not in str(local)
    assert CANON not in str(hubrows), "hub keeps old vector and catch-up never repairs it"


def test_backup_after_refusal_contains_no_stale_fact_wording(pg, replica, tmp_path, monkeypatch):
    """#6 (P2): a new backup's replica copy carries no fact row of a refused
    generation; restoring it still hides the forgotten wording."""
    replica(pg)
    _capture(loop=CANON)
    private = _payload(loop=ALT)
    capture.write_pg(private)
    monkeypatch.setattr(tombstones, "registry_path", lambda: paths.data_dir() / "forgotten_sources.json")
    assert hs.refresh()["ok"]
    tombstones.publish_payloads([private])
    assert ALT not in _replica_readers()
    result = paths.backup_local(dest=tmp_path / "after-refusal.zip")
    assert result["ok"]
    with zipfile.ZipFile(result["archive"]) as z:
        names = z.namelist()
        name = next(n for n in names if n.endswith("hub_snapshot.sqlite"))
        copied = tmp_path / "archive-copy.sqlite"
        copied.write_bytes(z.read(name))
    with sqlite3.connect(copied) as c:
        raw = c.execute("SELECT text, latest_text FROM commitments").fetchall()
    restored = paths.import_local(source=Path(result["archive"]))
    assert restored["ok"] and ALT not in _replica_readers()
    assert ALT not in str(raw), "new backup exports facts from the refused generation"
