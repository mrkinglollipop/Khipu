# ruff: noqa: F811
"""2026-10-27: `khipu owed` ran past the hub's statement timeout once the
hub held ~7,000 open commitments and ~100,000 decisions, because the
forgotten-evidence check compared `decisions.id::text` and scanned every
decision once per commitment. The check must reach decisions by key, and a
non-numeric id in the JSON must not break the read. Every row is synthetic."""
from __future__ import annotations

import json

from khipu import commitments as co
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_astra_1026_forget import PRIVATE, _ids
from tests.test_provenance_forget import _capture, env, pg  # noqa: F401


def _plan(cur, sql: str) -> str:
    cur.execute("SET LOCAL enable_seqscan = off")
    cur.execute("EXPLAIN " + sql)
    return "\n".join(r[0] for r in cur.fetchall())


def test_forgotten_evidence_check_reaches_decisions_by_key(pg):
    with pg.connect() as conn, conn.cursor() as cur:
        plan = _plan(cur, f"SELECT {co.settle_suggestion_sql(cur)}, {co.close_reason_sql(cur)} FROM commitments")
        conn.rollback()
    assert "Seq Scan on decisions" not in plan, plan


def test_a_non_numeric_decision_id_neither_breaks_the_list_nor_hides_a_live_suggestion(pg):
    later = _capture(loop=None, decision=PRIVATE)
    earlier = _capture(loop="Choose the launch codename", kind="question", owner="user")
    cid, _ = _ids(pg, earlier)
    _, did = _ids(pg, later)
    with pg.connect() as conn:
        conn.execute("UPDATE commitments SET settle_suggestion=%s::jsonb WHERE id=%s",
                     (json.dumps({"decision_id": "not-a-number", "evidence": {"decision_id": str(did)},
                                  "episode": later, "text": "kept"}), cid))
    with pg.connect() as conn, conn.cursor() as cur:
        rows = co.list_owed(cur, status="open", limit=500)
        conn.rollback()
    row = next(r for r in rows if r["id"] == cid)
    assert (row["settle_suggestion"] or {}).get("text") == "kept", row
