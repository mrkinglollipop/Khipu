# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""Codex Astra review of feat/fact-provenance (2026-10-23), findings 3 to 6:
its reproductions, ported. Each failed on 88620598 and passes with the fix.
Report: jobs/2026-10-23/astra-prov/report.md; fixes: jobs/2026-10-23/prov-fix1.
The fixture/helpers use tests.hermetic_pg only, with all provider hooks stubbed.
"""


from khipu import capture, forget, provenance
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










def test_gap_of_a_forgotten_episode_is_resolved_without_replay(pg, monkeypatch):
    """The replay never re-derives a forgotten capture's facts (I1)."""
    _finish_witness_backfill(pg)
    real = provenance.record_claim

    def broken(cur, kind, *args, **kwargs):
        if kind == "deliverable":
            raise RuntimeError("one-off decision derivation bug")
        return real(cur, kind, *args, **kwargs)

    monkeypatch.setattr(provenance, "record_claim", broken)
    eid = capture.write_pg(_payload(loop=None, decision=None, title="the checklist"))["episode_id"]
    monkeypatch.setattr(provenance, "record_claim", real)
    _forget(pg, eid)
    with pg.connect() as conn:
        out = provenance.replay_gaps(conn)
    assert out["forgotten"] == 1 and out["replayed"] == 0, out
    with pg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM deliverables").fetchone()[0] == 0
        assert conn.execute("SELECT resolution FROM fact_gaps").fetchall() == [("forgotten",)]


def test_failed_gap_replay_keeps_the_gap_open(pg, monkeypatch):
    _finish_witness_backfill(pg)

    def broken(cur, kind, *args, **kwargs):
        raise RuntimeError("still broken")

    real = provenance.record_claim
    monkeypatch.setattr(provenance, "record_claim", broken)
    capture.write_pg(_payload(loop=None, decision=None, title="the checklist"))
    with pg.connect() as conn:
        out = provenance.replay_gaps(conn)
    monkeypatch.setattr(provenance, "record_claim", real)
    assert out["failed"] == 1, out
    with pg.connect() as conn:
        gap = conn.execute("SELECT resolved_at, attempts, resolution FROM fact_gaps").fetchone()
    assert gap[0] is None and gap[1] == 1 and gap[2].startswith("replay failed"), gap
