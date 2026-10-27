# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""Fact provenance, phase 5: doctor's block for 0047's state, open
``fact_gaps`` and section 4's limit count
(docs/plans/2026-10-23-fact-provenance.md). Hermetic PostgreSQL for the
block itself; cmd_doctor with every other check mocked green for how it
reaches doctor's output.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest import mock

from khipu import cli, provenance
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_cli_recall_quality import _green_doctor_patches
from tests.test_provenance_forget import ALT, CANON, _capture, _forget, env, pg  # noqa: F401

THIRD = "Review the deployment checklist before the release ships"


def _check(h) -> dict:
    with h.connect() as conn:
        out = provenance.doctor_check(conn)
        conn.rollback()
    return out


def test_a_hub_without_0047_is_a_plain_state_not_red(env):
    _capture()
    block = _check(env)
    assert block["ok"] is True
    assert block["state"] == "not_migrated"
    assert "0047" in block["reason"]
    assert block["warnings"] == []


def test_a_clean_0047_hub_shows_active_with_nothing_open(pg):
    _capture()
    block = _check(pg)
    assert block["ok"] is True and block["state"] == "active"
    assert block["applied_at"] is not None
    assert block["fact_gaps"] == {"open": 0, "oldest_at": None, "by_step": {}}
    assert block["limit"] == {"at_risk": 0, "retired": 0}
    assert block["warnings"] == []


def test_open_fact_gaps_are_counted_and_warned(pg):
    eid = _capture()
    with pg.connect() as conn:
        conn.execute("INSERT INTO fact_gaps (episode_id, step, error) VALUES "
                     "(%s, 'decisions', 'boom'), (%s, 'decisions', 'boom'), (%s, 'deliverables', 'x')",
                     (eid, eid, eid))
        conn.execute("INSERT INTO fact_gaps (episode_id, step, error, resolved_at) "
                     "VALUES (%s, 'commitments', 'old', now())", (eid,))
        conn.commit()
    block = _check(pg)
    assert block["ok"] is True
    assert block["fact_gaps"]["open"] == 3
    assert block["fact_gaps"]["by_step"] == {"decisions": 2, "deliverables": 1}
    assert block["fact_gaps"]["oldest_at"] is not None
    assert any("3 capture fact steps failed" in w for w in block["warnings"])


def test_the_section_4_limit_is_counted_at_risk_then_retired(env):
    """A legacy restater whose words a later restater overwrote cannot be
    proven: the fact is at risk while it is open, and retires (the accepted
    limit) once every proven claimant is forgotten."""
    a = _capture()
    b = _capture(loop=ALT)
    c = _capture(loop=THIRD)
    with env.connect() as conn:
        assert conn.execute("SELECT count(*), max(seen_count) FROM commitments").fetchone() == (1, 3)
    env.to_0047()
    with env.connect() as conn:
        assert provenance.backfill(conn)["ok"]
    block = _check(env)
    assert block["limit"] == {"at_risk": 1, "retired": 0}
    assert block["backfill_pending_projects"] == 0
    assert block["warnings"] == [], "an at-risk fact is shown, not warned"
    _forget(env, a)
    _forget(env, c)
    block = _check(env)
    assert block["limit"] == {"at_risk": 0, "retired": 1}
    assert any("1 older commitment retired" in w for w in block["warnings"])
    assert b


def test_a_post_0047_fact_never_counts_toward_the_limit(pg):
    a = _capture()
    _forget(pg, a)
    assert _check(pg)["limit"] == {"at_risk": 0, "retired": 0}


def test_no_database_configured_is_a_skip():
    with mock.patch("khipu.db.dsn_configured", return_value=False):
        block = provenance.doctor_check()
    assert block["ok"] is True and "skipped" in block


def test_a_failed_read_is_red_not_a_pass():
    with mock.patch("khipu.db.dsn_configured", return_value=True), \
         mock.patch("khipu.db.connect", side_effect=OSError("refused")):
        block = provenance.doctor_check()
    assert block["ok"] is False and "refused" in block["error"]


# ---- how the block reaches doctor's output -----------------------------------------


def _doctor(block, *, hub_down=False) -> tuple[int, dict]:
    patches = _green_doctor_patches() + [
        mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}),
        mock.patch("khipu.drift.recall_quality", return_value={}),
        mock.patch("khipu.migrate.doctor_check", return_value={
            "ok": True, "pending": [], "unknown_applied": [], "warnings": []}),
        mock.patch("khipu.provenance.doctor_check", return_value=block),
    ]
    if hub_down:
        patches.append(mock.patch("khipu.drift.status_payload", side_effect=OSError("down")))
        patches.append(mock.patch("khipu.hub_snapshot.hub_connection_failed", return_value=True))
        patches.append(mock.patch("khipu.hub_snapshot.status_payload_snapshot", return_value={}))
    for p in patches:
        p.start()
    try:
        args = cli.build_parser().parse_args(["doctor"])
        with redirect_stdout(io.StringIO()) as out:
            code = cli.cmd_doctor(args)
    finally:
        for p in reversed(patches):
            p.stop()
    return code, json.loads(out.getvalue())


def _baseline_ok() -> bool:
    return _doctor({"ok": True, "warnings": [], "skipped": "no database configured"})[1]["ok"]


def test_doctor_shows_the_block_and_a_hub_without_0047_does_not_change_ok():
    base = _baseline_ok()
    block = {"ok": True, "warnings": [], "state": "not_migrated", "reason": "not yet"}
    _code, out = _doctor(block)
    assert out["fact_provenance"]["state"] == "not_migrated"
    assert out["ok"] == base
    assert "fact_provenance_ok" not in out


def test_doctor_lifts_gap_and_limit_warnings_into_its_warnings_without_gating_ok():
    base = _baseline_ok()
    block = {"ok": True, "state": "active", "fact_gaps": {"open": 2}, "limit": {"at_risk": 0, "retired": 1},
             "warnings": ["2 capture fact steps failed", "1 older commitment retired"]}
    _code, out = _doctor(block)
    assert out["fact_provenance"]["fact_gaps"]["open"] == 2
    assert "2 capture fact steps failed" in out["warnings"]
    assert "1 older commitment retired" in out["warnings"]
    assert out["ok"] == base


def test_with_the_hub_down_a_failed_read_is_skipped_not_doubled():
    block = {"ok": False, "error": "OperationalError: down", "warnings": ["could not be checked"]}
    _code, out = _doctor(block, hub_down=True)
    assert out["fact_provenance"]["skipped"] == "the database did not answer"
    assert "could not be checked" not in out["warnings"]
