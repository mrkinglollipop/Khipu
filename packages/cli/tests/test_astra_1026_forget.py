# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""Forget findings from Astra's 2026-10-26 memory-pipeline audit
(jobs/2026-10-26/astra-memory-pipeline/report.md, findings 1-3), on the
hermetic PostgreSQL (tests/hermetic_pg.py) at the full schema:

1. a gateway token forgets only an episode the server stamped with its own
   origin, decided inside the hub transaction;
2. the commitments and decisions of an episode this Mac refuses never reach a
   replica reader through the fact copy;
3. a forget clears the "Looks done" / "Looks settled" suggestion another
   episode's commitment holds when it quotes the forgotten episode's
   evidence, and every reader fences one written meanwhile.

Every row is synthetic."""
from __future__ import annotations

import json

import pytest

from khipu import commitments as co, forget, hub_snapshot as hs, mcp_server as srv, request_context, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import fact_text, readers, replica  # noqa: F401
from tests.test_provenance_forget import _capture, _rows, env, pg  # noqa: F401

PRIVATE = "SYNTHETIC_PRIVATE the launch codename is aubergine"


def _cloud(pg, origin, n):
    eid = _capture(session_id=f"grokbot:{origin or 'none'}-{n}", harness="grok_bot",
                   summary=f"cloud capture {n} for {origin}")
    with pg.connect() as conn:
        conn.execute("UPDATE episodes SET origin = %s WHERE id = %s", (origin, eid))
    return eid


def _deleted(pg, eid) -> bool:
    return _rows(pg, "SELECT deleted_at IS NOT NULL FROM episodes WHERE id = %s", (eid,))[0][0]


# ---- 1. gateway forget is authorized against the stored origin -------------------

def test_another_token_cannot_forget_a_cloud_episode(pg):
    eid = _cloud(pg, "gateway:alpha", 1)
    out = forget.forget_everywhere(eid, memory_root=pg.memory, gateway=True, owner="gateway:beta")
    assert not out["ok"] and out.get("refused"), out
    assert "another gateway token" in out["error"]
    assert not _deleted(pg, eid)
    # Nothing of the refused episode reached this machine's registry either.
    assert eid not in tombstones.read_gate().episode_ids


@pytest.mark.parametrize("origin,why", [(None, "unknown"), ("local:abcdef123456", "on a Mac")])
def test_a_token_cannot_forget_an_unowned_or_local_episode(pg, origin, why):
    eid = _cloud(pg, origin, 2)
    out = forget.forget_everywhere(eid, memory_root=pg.memory, gateway=True, owner="gateway:alpha")
    assert not out["ok"] and why in out["error"], out
    assert not _deleted(pg, eid)


def test_the_owning_token_forgets_its_own_episode(pg):
    eid = _cloud(pg, "gateway:alpha", 3)
    out = forget.forget_everywhere(eid, memory_root=pg.memory, gateway=True, owner="gateway:alpha")
    assert out.get("soft_deleted"), out
    assert _deleted(pg, eid)


def test_the_gateway_tool_forwards_the_calling_tokens_origin(pg, monkeypatch):
    monkeypatch.setattr(srv, "_ensure_path", lambda: None)
    theirs = _cloud(pg, "gateway:alpha", 4)
    with request_context.gateway_request("beta"), pytest.raises(ValueError, match="not this token's"):
        srv._tool_forget({"id": theirs})
    assert not _deleted(pg, theirs)
    mine = _cloud(pg, "gateway:beta", 5)
    with request_context.gateway_request("beta"):
        try:
            srv._tool_forget({"id": mine})
        except srv._ToolFailure:
            pass  # incomplete only for stores this hermetic run cannot check
    assert _deleted(pg, mine)


def test_the_machine_owner_still_forgets_any_episode(pg):
    eid = _cloud(pg, "gateway:alpha", 6)
    out = forget.forget_everywhere(eid, memory_root=pg.memory, gateway=False)
    assert out.get("soft_deleted"), out


# ---- 2. a refused episode's facts never reach a replica reader -------------------

@pytest.mark.parametrize("how", ["source", "episode_id"])
def test_a_refused_episodes_facts_never_reach_the_replica(pg, replica, how):
    replica(pg)
    pg.memory.mkdir(exist_ok=True)
    victim = _capture(loop="FORGOTTEN victim loop", decision="FORGOTTEN victim decision")
    _capture(loop="kept loop", decision="kept decision")
    assert hs.refresh()["ok"]
    assert "kept decision" in readers()
    if how == "source":
        raw = _rows(pg, "SELECT raw FROM episodes WHERE id = %s", (victim,))[0][0]
        tombstones.publish_payloads([raw])
    else:
        tombstones.publish_episode_ids({victim})
    for step in (lambda: hs.pull_changes(include_ledger=False), hs.refresh, hs.copy_facts):
        step()
        assert "FORGOTTEN" not in readers()
        assert "FORGOTTEN" not in fact_text()


# ---- 3. suggestions quoting a forgotten episode -------------------------------------

def _suggest(pg, cid, *, episode, decision):
    with pg.connect() as conn, conn.cursor() as cur:
        assert co.suggest_looks_done(cur, cid, rule="decision", reason=PRIVATE, key=f"decision:{decision}",
                                     evidence={"text": PRIVATE, "decision_id": decision}, episode=episode)


def _owed(pg) -> str:
    with pg.connect() as conn, conn.cursor() as cur:
        return json.dumps(co.finish_owed_rows(co.list_owed(cur, status="open", limit=500), 500), default=str)


def _ids(pg, eid):
    cid = _rows(pg, "SELECT id FROM commitments WHERE opened_episode = %s", (eid,))
    did = _rows(pg, "SELECT id FROM decisions WHERE episode_id = %s", (eid,))
    return (cid[0][0] if cid else None), (did[0][0] if did else None)


def test_forget_clears_a_looks_done_suggestion_quoting_the_episode(pg):
    pg.memory.mkdir(exist_ok=True)
    later = _capture(loop=None, decision=PRIVATE)
    earlier = _capture(loop="Choose the launch codename", kind="question", owner="user")
    cid, _ = _ids(pg, earlier)
    _, did = _ids(pg, later)
    _suggest(pg, cid, episode=later, decision=did)
    assert PRIVATE in _owed(pg)
    out = forget.forget_everywhere(later, memory_root=pg.memory, gateway=False)
    assert out.get("soft_deleted") and out.get("suggestions_cleared") == 1, out
    assert _rows(pg, "SELECT settle_suggestion FROM commitments WHERE id = %s", (cid,)) == [(None,)]
    assert PRIVATE not in _owed(pg)
    # The commitment itself, opened by a live episode, stays open.
    assert _rows(pg, "SELECT status FROM commitments WHERE id = %s", (cid,)) == [("open",)]


def test_forget_clears_a_looks_settled_suggestion_naming_the_episodes_decision(pg):
    pg.memory.mkdir(exist_ok=True)
    later = _capture(loop=None, decision=PRIVATE)
    earlier = _capture(loop="Choose the launch codename", kind="question", owner="user")
    cid, _ = _ids(pg, earlier)
    _, did = _ids(pg, later)
    with pg.connect() as conn:
        conn.execute("UPDATE commitments SET settle_suggestion = %s::jsonb WHERE id = %s",
                     (json.dumps({"decision_id": did, "text": PRIVATE, "score": 0.7}), cid))
    forget.forget_everywhere(later, memory_root=pg.memory, gateway=False)
    assert _rows(pg, "SELECT settle_suggestion FROM commitments WHERE id = %s", (cid,)) == [(None,)]
    assert PRIVATE not in _owed(pg)


def test_readers_fence_a_suggestion_citing_a_forgotten_episode_written_meanwhile(pg, replica):
    """A suggestion written after the forget (older code, a race) still never
    reaches the hub reader, the replica copy or the replica reader."""
    replica(pg)
    pg.memory.mkdir(exist_ok=True)
    later = _capture(loop=None, decision="the forgotten decision")
    earlier = _capture(loop="Choose the launch codename", kind="question", owner="user")
    cid, _ = _ids(pg, earlier)
    forget.forget_everywhere(later, memory_root=pg.memory, gateway=False)
    with pg.connect() as conn:
        conn.execute("UPDATE commitments SET settle_suggestion = %s::jsonb WHERE id = %s",
                     (json.dumps({"looks_done": True, "rule": "decision", "reason": PRIVATE,
                                  "evidence": {"text": PRIVATE}, "episode": later, "key": "k"}), cid))
    assert PRIVATE not in _owed(pg)
    assert hs.refresh()["ok"]
    assert PRIVATE not in readers()
    assert PRIVATE not in fact_text()
