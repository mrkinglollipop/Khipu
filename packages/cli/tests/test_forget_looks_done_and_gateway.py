# ruff: noqa: F811
"""Grok 2026-10-26 (batch B): a Looks-done close and a gateway forget of a grok_build row, on the hermetic
PostgreSQL. Every row is synthetic."""
from __future__ import annotations

import json

import pytest


from khipu import commitments as co, forget, mcp_server as srv, request_context
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_astra_1026_forget import PRIVATE, _ids, _rows
from tests.test_provenance_forget import _capture, env, pg  # noqa: F401


def test_a_confirmed_looks_done_close_does_not_keep_quoting_a_forgotten_decision(pg):
    pg.memory.mkdir(exist_ok=True)
    later = _capture(loop=None, decision=PRIVATE)
    earlier = _capture(loop="Choose the launch codename", kind="question", owner="user")
    cid, _ = _ids(pg, earlier)
    _, did = _ids(pg, later)
    quote = f"settled by decision {did}: '{PRIVATE}'"
    with pg.connect() as conn, conn.cursor() as cur:
        assert co.suggest_looks_done(cur, cid, rule="decision", reason=quote, key=f"decision:{did}",
                                     evidence={"text": PRIVATE, "decision_id": did}, episode=later)
        # what the app's "Looks done" button sends: the suggestion's own reason
        assert co.set_status(cur, cid, "closed", reason=quote, by=co.BY_APP)
        conn.commit()
    forget.forget_everywhere(later, memory_root=pg.memory, gateway=False)
    with pg.connect() as conn, conn.cursor() as cur:
        closed = json.dumps(co.list_owed(cur, status="closed", limit=500), default=str)
        conn.rollback()
    assert PRIVATE not in closed, "the closed item still quotes the forgotten decision"


def test_a_gateway_token_can_forget_a_grok_build_prefixed_row_it_wrote(pg, monkeypatch):
    monkeypatch.setattr(srv, "_ensure_path", lambda: None)
    eid = _capture(session_id="grok_build:sess-1", harness="grok_build", summary="cloud capture by a token")
    with pg.connect() as conn:
        conn.execute("UPDATE episodes SET origin = 'gateway:alpha' WHERE id = %s", (eid,))
    with request_context.gateway_request("alpha"):
        try:
            srv._tool_forget({"id": eid})
        except (srv._ToolFailure, ValueError) as exc:
            err = str(exc)
        else:
            err = ""
    deleted = _rows(pg, "SELECT deleted_at IS NOT NULL FROM episodes WHERE id = %s", (eid,))[0][0]
    assert deleted, f"the token's own row was refused by the harness prefix: {err}"


@pytest.mark.parametrize("origin", ["local:this-mac", None])
def test_a_gateway_token_still_cannot_forget_a_mac_or_unowned_row(pg, monkeypatch, origin):
    monkeypatch.setattr(srv, "_ensure_path", lambda: None)
    eid = _capture(session_id="grok_build:sess-2", harness="grok_build", summary="captured on the Mac")
    with pg.connect() as conn:
        conn.execute("UPDATE episodes SET origin = %s WHERE id = %s", (origin, eid))
    with request_context.gateway_request("alpha"), pytest.raises((srv._ToolFailure, ValueError)):
        srv._tool_forget({"id": eid})
    assert not _rows(pg, "SELECT deleted_at IS NOT NULL FROM episodes WHERE id = %s", (eid,))[0][0]
