"""Capture stores memory without manufacturing a work list."""
from unittest.mock import Mock
from khipu import capture, extract
from tests.owed_fixtures import owed  # noqa: F401
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

def test_fact_steps_do_not_create_or_close_commitments(monkeypatch):
    from khipu import deliverables
    monkeypatch.setattr(deliverables, "insert_deliverables_from_episode", lambda *a: 0)
    cur = Mock()
    cur.fetchone.return_value = (False,)
    capture.run_fact_steps(cur, {"open_loops": [{"text": "guess a task"}], "closed_loops": ["done"]}, 1)
    sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
    assert "commitment" not in sql and "decision" not in sql

def test_summary_prompt_does_not_request_a_todo_list():
    prompt = extract._build_prompt(cwd="/fixture", transcript="Owner: keep this memory")
    assert "open_loops:" not in prompt and "closed_loops:" not in prompt


def test_capture_retains_episode_memory_without_new_registry_rows(owed, monkeypatch):  # noqa: F811
    from khipu import embed, topic_graph
    monkeypatch.setattr(capture, "_dedup_candidates", lambda *a: [])
    monkeypatch.setattr(embed, "embed_on_capture", lambda *a, **k: None)
    monkeypatch.setattr(topic_graph, "persist_capture_graph", lambda *a, **k: {})
    from tests.owed_fixtures import Flood
    from tests.fact_seed import archived_facts
    flood = Flood(owed)
    old_episode = flood.episode()
    flood.item("archive", "Guess a new task", owner="assistant", episode=old_episode)
    with owed.transaction(), owed.cursor() as cur:
        archived_facts(cur, {"project": "acme/widget", "decisions": ["Keep existing choice"]}, old_episode)
        cur.execute("SELECT (SELECT count(*) FROM commitments), (SELECT count(*) FROM decisions)")
        before = cur.fetchone()
        cur.execute("SELECT id, text, status FROM commitments ORDER BY id")
        old_commitments = cur.fetchall()
        cur.execute("SELECT id, text, episode_id FROM decisions ORDER BY id")
        old_decisions = cur.fetchall()
    payload = {"ts": "2026-11-08T00:00:00Z", "session_id": "codex:fixture-explicit-only",
               "summary": "Owner chose a bounded fixture for memory recall", "project": "acme/widget",
               "decisions": ["Preserve multilingual café quotes"],
               "open_loops": [{"text": "Guess a new task"}], "closed_loops": ["Finish an old task"]}
    result = capture.write_pg(payload)
    with owed.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM commitments), (SELECT count(*) FROM decisions)")
        assert cur.fetchone() == before
        cur.execute("SELECT id, text, status FROM commitments ORDER BY id")
        assert cur.fetchall() == old_commitments
        cur.execute("SELECT id, text, episode_id FROM decisions ORDER BY id")
        assert cur.fetchall() == old_decisions
        cur.execute("SELECT decisions, summary FROM episodes WHERE id = %s", (result["episode_id"],))
        assert cur.fetchone() == (payload["decisions"], payload["summary"])




def test_remaining_fact_gaps_reach_the_mcp_caller(monkeypatch):
    from khipu import mcp_server as server
    monkeypatch.setattr(server, "_stdio_hook_owns_capture", lambda: False)
    monkeypatch.setattr(server, "_capture_mode", lambda: "hub")
    def recorded(payload, *, result, **kwargs):
        result.update(episode_id=42, fact_gaps=[{"step": "deliverables", "reason": "fixture failure"}])
        return 0
    monkeypatch.setattr(capture, "capture", recorded)
    out = server._tool_capture({"summary": "The episode landed with a deliverable gap"})
    assert out["ok"] and out["partial"] and out["episode_id"] == 42
    assert out["fact_gaps"] == [{"step": "deliverables", "reason": "fixture failure"}]
    assert "commitments" not in out


def test_fact_gap_reporting_is_independent_of_the_retired_todo_report(monkeypatch):
    from khipu import provenance
    monkeypatch.setattr(provenance, "ready", lambda cur: False)
    gaps = []
    token = capture._FACT_GAPS.set(gaps)
    try:
        capture._record_fact_gap(Mock(), 42, "deliverables", RuntimeError("fixture failure"))
    finally:
        capture._FACT_GAPS.reset(token)
    assert gaps == [{"step": "deliverables", "reason": "RuntimeError: fixture failure"}]
