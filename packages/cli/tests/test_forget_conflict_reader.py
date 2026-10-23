"""Regression reproducer: main must hide forgotten decisions in conflicts too.

Only the repository's throwaway loopback PostgreSQL fixture is used.
"""
import json
from contextlib import contextmanager

import pytest

from tests.fact_seed import adopt
from tests.hermetic_pg import hub, pg_cluster
from khipu import db, decisions, forget, mcp_server
from khipu.request_context import gateway_request


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    db._TABLE_COLUMNS_CACHE.clear()


@pytest.mark.parametrize("reverse", [False, True], ids=["forgotten-old", "forgotten-new"])
@pytest.mark.parametrize("gateway", [False, True], ids=["stdio", "gateway"])
def test_conflicts_hide_a_forgotten_decision(hub, monkeypatch, tmp_path, gateway, reverse):
    conn = hub.connect()
    hub.migrate(conn=conn)
    cur = conn.cursor()
    episode_ids = []
    for name in ("forgotten", "live-one", "live-two"):
        payload = {"ts": "2026-10-22T00:00:00Z", "summary": name,
                   "harness": "codex", "session_id": "codex:audit-" + name,
                   "transcript_range": "0:10"}
        cur.execute("INSERT INTO episodes (ts, session_id, harness, summary, raw, transcript_range) "
                    "VALUES (%s::timestamptz,%s,%s,%s,%s::jsonb,%s) RETURNING id",
                    (payload["ts"], payload["session_id"], payload["harness"],
                     name, json.dumps(payload), payload["transcript_range"]))
        episode_ids.append(cur.fetchone()[0])
    decision_ids = []
    for eid, text in zip(episode_ids, ("FORGOTTEN-DECISION-SENTINEL", "live decision", "other live decision")):
        cur.execute("INSERT INTO decisions (text, decided_at, episode_id) "
                    "VALUES (%s, now(), %s) RETURNING id", (text, eid))
        decision_ids.append(cur.fetchone()[0])
    adopt(cur, "decision", decision_ids)  # setup: the seeded rows' claims (0047 fact gate)
    for old, new in ((decision_ids[1], decision_ids[0]) if reverse else (decision_ids[0], decision_ids[1]),
                     (decision_ids[1], decision_ids[2])):
        cur.execute("INSERT INTO decision_links (old_id,new_id,kind,state,reason) "
                    "VALUES (%s,%s,'conflicts','candidate','synthetic conflict')", (old, new))
    conn.commit()
    outcome = forget.forget_episode(cur, episode_ids[0], memory_root=tmp_path / "memory")
    conn.commit()
    assert outcome["ok"] and outcome["soft_deleted"] and outcome["decisions_retracted"] == 1
    listed = decisions.list_decisions(cur, status="all")
    assert decision_ids[0] not in {r["id"] for r in listed}
    assert {r["id"] for r in listed} == set(decision_ids[1:])
    conn.commit()

    @contextmanager
    def connect_only_fixture(**kwargs):
        yield conn

    monkeypatch.setattr(db, "connect", connect_only_fixture)
    monkeypatch.setattr(mcp_server, "_ensure_path", lambda: None)
    if gateway:
        with gateway_request("audit-synthetic-client"):
            out = mcp_server._tool_decisions({"status": "conflicts"})
    else:
        out = mcp_server._tool_decisions({"status": "conflicts"})
    print("forgotten episode:", episode_ids[0], "cascade:", outcome["decisions_retracted"])
    print("normal decision ids:", [r["id"] for r in listed])
    print("conflicts:", json.dumps(out, default=str))
    assert any(r["old_id"] == decision_ids[1] for r in out["results"]), "live control must survive"
    assert "FORGOTTEN-DECISION-SENTINEL" not in json.dumps(out, default=str)
