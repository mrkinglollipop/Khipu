"""Capture stores memory without manufacturing a work list."""
from unittest.mock import Mock
from khipu import capture, extract

def test_fact_steps_do_not_create_or_close_commitments(monkeypatch):
    from khipu import decisions, deliverables
    monkeypatch.setattr(decisions, "insert_decisions_from_episode", lambda *a: 0)
    monkeypatch.setattr(decisions, "detect_reversals_from_episode", lambda *a: 0)
    monkeypatch.setattr(deliverables, "insert_deliverables_from_episode", lambda *a: 0)
    cur = Mock()
    capture.run_fact_steps(cur, {"open_loops": [{"text": "guess a task"}], "closed_loops": ["done"]}, 1)
    sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
    assert "commitment" not in sql and "close_rules" not in sql

def test_summary_prompt_does_not_request_a_todo_list():
    prompt = extract._build_prompt(cwd="/fixture", transcript="Owner: keep this memory")
    assert "open_loops:" not in prompt and "closed_loops:" not in prompt
