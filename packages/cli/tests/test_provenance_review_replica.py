# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""Codex Astra review of feat/fact-provenance (2026-10-23), findings 1 and
2: its replica reproductions, ported. Each failed on 88620598.

FRESHNESS_OVERLAP_S=0 compresses an ordinary >10 minute open capture into
one deterministic interleaving; the production changed_at clock predates
the dump just as it would after the real overlap expires.
"""
from __future__ import annotations

import json

import pytest

from khipu import hub_snapshot as hs, provenance, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import env, pg, PROJECT  # noqa: F401
from tests.test_provenance_replica import replica, _replica, _replica_readers  # noqa: F401
from tests.fact_seed import announce


@pytest.mark.parametrize("kind", ["decision", "deliverable"])
@pytest.mark.parametrize("mode", ["sync", "pull"])
def test_unseen_source_registry_fence_applies_before_fact_import(pg, replica, monkeypatch, kind, mode):
    replica(pg)
    monkeypatch.setattr(hs, "FRESHNESS_OVERLAP_S", 0)
    payload = {
        "ts": "2026-10-20T12:00:00Z", "summary": "FORGOTTEN SOURCE",
        "harness": "codex", "session_id": "codex:review-unseen-source",
        "transcript_range": "0:10", "project": PROJECT,
    }
    slow = pg.connect()
    try:
        announce(slow)
        eid = slow.execute(
            "INSERT INTO episodes (ts, summary, raw, session_id, harness, transcript_range, project) "
            "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s) RETURNING id",
            (payload["ts"], payload["summary"], json.dumps(payload), payload["session_id"],
             payload["harness"], payload["transcript_range"], PROJECT),
        ).fetchone()[0]
        marker = f"PRIVATE-{kind}-WORDS"
        if kind == "decision":
            fid = slow.execute(
                "INSERT INTO decisions (project, text, episode_id) VALUES (%s, %s, %s) RETURNING id",
                (PROJECT, marker, eid),
            ).fetchone()[0]
            field = "text"
        else:
            fid = slow.execute(
                "INSERT INTO deliverables (project, kind, path, title, episode_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (PROJECT, "file", "private.md", marker, eid),
            ).fetchone()[0]
            field = "title"
        provenance.record_claim(slow.cursor(), kind, fid, field, marker, episode_id=eid)
        provenance.project(slow.cursor(), kind, fid)
        # Dump starts after changed_at was stamped but before the capture
        # commits. Its xmin correctly retains this capture for the fact stream.
        assert hs.refresh()["ok"]
        assert _replica("SELECT id FROM episodes") == []
        tombstones.publish_payloads([payload])
        slow.commit()
        # No episode in SQLite means no rejected numeric id has been learned.
        out = hs.sync_fact_changes() if mode == "sync" else hs.pull_changes(include_ledger=False)
        assert out["ok"], out
        first_read = _replica_readers()
        second = hs.pull_changes(include_ledger=False)
        assert second["ok"], second
        print("UNSEEN_REGISTRY", {"kind": kind, "mode": mode, "first_ok": out["ok"],
              "first_exposed": marker in first_read, "second_ok": second["ok"],
              "second_exposed": marker in _replica_readers(), "episodes": _replica("SELECT id FROM episodes")})
        assert marker not in _replica_readers(), (
            "Registry-forgotten source imported through the fact stream despite being absent "
            "from the replica; no source-identity gate ran", out,
        )
    finally:
        slow.rollback()
        slow.close()


def test_older_episode_patch_cannot_overwrite_newer_deliverable_stream_forever(pg, replica, monkeypatch):
    from unittest import mock
    from tests.test_provenance_forget import _capture, _forget

    replica(pg)
    monkeypatch.setattr(hs, "FRESHNESS_OVERLAP_S", 0)
    eid = _capture(loop=None, title="A public checklist")
    kept = _capture(loop=None, title="The kept checklist")
    assert hs.refresh()["ok"]
    (fid,) = _replica("SELECT id FROM deliverables")[0]
    # A missed patch legitimately asks the pull to re-read an older episode
    # whose timestamp is already behind its ordinary episode cursor.
    assert hs._queue_missed_patch(eid)
    real_upsert = hs.upsert_episode
    interleaved = []

    def owner_update_then_apply_old_episode(row, *args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            # Another Mac's forget publishes its own local registry, not
            # this replica's. Keep the real hub forget/reprojection and hub
            # tombstones, suppress only the two local registry publications.
            with mock.patch.object(tombstones, "publish_forget"), mock.patch.object(
                tombstones, "publish_episode_ids"
            ):
                _forget(pg, eid)
            newer = hs.sync_fact_changes()
            assert newer["ok"], newer
            assert _replica("SELECT episode_id, title FROM deliverables WHERE id = ?", (fid,)) == [
                (kept, "The kept checklist")
            ]
        return real_upsert(row, *args, **kwargs)

    monkeypatch.setattr(hs, "upsert_episode", owner_update_then_apply_old_episode)
    old_pull = hs.pull_changes(include_ledger=False)
    assert old_pull["ok"], old_pull
    assert interleaved
    # The old pull writes its old per-episode deliverable under the forgotten
    # anchor, then skips its stream because the independent sync advanced
    # xmin. The next pull imports that anchor's tombstone and deletes the
    # wrongly anchored row. B's episode is old and has not changed, and the
    # stream cursor is already past the reparenting: live support is lost.
    assert hs.pull_changes(include_ledger=False)["ok"]
    third = hs.pull_changes(include_ledger=False)
    assert third["ok"], third
    with pg.connect() as con:
        hub_rows = con.execute("SELECT episode_id,title FROM deliverables WHERE id=%s",(fid,)).fetchall()
    local_rows = _replica("SELECT episode_id,title FROM deliverables WHERE id=?",(fid,))
    print("STALE_DELIVERABLE", {"third_pull_ok": third["ok"], "hub": hub_rows, "replica": local_rows})
    assert local_rows == [(kept, "The kept checklist")]


def test_unseen_source_registry_fence_applies_to_pulled_commitments(pg, replica, monkeypatch):
    """The finding-1 schedule for the other independent fact import: the
    whole-table commitments copy of a pull."""
    replica(pg)
    monkeypatch.setattr(hs, "FRESHNESS_OVERLAP_S", 0)
    payload = {
        "ts": "2026-10-20T12:00:00Z", "summary": "FORGOTTEN SOURCE",
        "harness": "codex", "session_id": "codex:review-unseen-commitment",
        "transcript_range": "0:10", "project": PROJECT,
    }
    marker = "PRIVATE-commitment-WORDS"
    slow = pg.connect()
    try:
        announce(slow)
        eid = slow.execute(
            "INSERT INTO episodes (ts, summary, raw, session_id, harness, transcript_range, project) "
            "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s) RETURNING id",
            (payload["ts"], payload["summary"], json.dumps(payload), payload["session_id"],
             payload["harness"], payload["transcript_range"], PROJECT),
        ).fetchone()[0]
        fid = slow.execute(
            "INSERT INTO commitments (text, project, owner, kind, opened_episode, content_hash) "
            "VALUES (%s, %s, 'agent', 'followup', %s, 'review-unseen') RETURNING id",
            (marker, PROJECT, eid),
        ).fetchone()[0]
        provenance.record_claim(slow.cursor(), "commitment", fid, "text", marker, episode_id=eid)
        provenance.project(slow.cursor(), "commitment", fid)
        assert hs.refresh()["ok"]
        tombstones.publish_payloads([payload])
        slow.commit()
        for _ in range(2):
            out = hs.pull_changes(include_ledger=False)
            assert out["ok"], out
            assert marker not in _replica_readers(), out
    finally:
        slow.rollback()
        slow.close()
