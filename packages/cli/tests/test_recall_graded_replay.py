"""Graded replay (2026-10-30): replays {prompt, cwd} rows through automatic
push and scores what was pushed against a grades file. Fake data only; the
real prompts and grades are private and never enter this repo."""
import importlib.util
import json
from pathlib import Path

import pytest

from khipu import recall_eval

DATASET = [
    {"n": 0, "prompt": "fix the video jump glitch", "cwd": "/w/olive"},
    {"n": 1, "prompt": "yes proceed", "cwd": "/w/olive"},
    {"n": 2, "prompt": "lane check, short", "cwd": "/w/khipu"},
    {"n": 3, "prompt": "what did we decide about pricing", "cwd": "/w/olive"},
]
GRADES = [
    {"n": 0, "needs_memory": True, "ideal": ["episode:10"],
     "pushed_now": {"episode:10": "relevant", "topic:note:raw": "irrelevant"}},
    {"n": 1, "needs_memory": False, "ideal": []},
    {"n": 2, "needs_memory": False, "ideal": [], "pushed_then": {"episode:7": "irrelevant"}},
    {"n": 3, "needs_memory": True, "ideal": ["episode:30"]},
]
PUSHED = {
    "fix the video jump glitch": [("episode", "10"), ("topic", "note:raw")],
    "yes proceed": [],
    "lane check, short": [("episode", "7")],
    "what did we decide about pricing": [("episode", "99")],
}


def fake_push(prompt, *, cwd):
    return {"hits": [{"kind": k, "id": i} for k, i in PUSHED[prompt]], "reason": "ok"}


def test_scores_relevant_share_abstention_and_ideal_hits():
    report = recall_eval.run_graded_replay(DATASET, recall_eval.index_grades(GRADES), push=fake_push)
    s = report["summary"]
    assert s["prompts"] == 4
    assert (s["pushed"], s["pushed_relevant"], s["pushed_graded"], s["pushed_ungraded"]) == (4, 1, 3, 1)
    assert s["relevant_share"] == pytest.approx(1 / 3)
    assert (s["no_memory_prompts"], s["abstained_correctly"]) == (2, 1)
    assert (s["memory_prompts_with_ideal"], s["ideal_hits"]) == (2, 1)
    by_n = {r["n"]: r for r in report["rows"]}
    assert by_n[0]["pushed"] == ["episode:10", "topic:note:raw"] and by_n[0]["ideal_hit"]
    assert by_n[1]["abstained"] is True and by_n[2]["abstained"] is False
    assert by_n[3]["ideal_hit"] is False


def test_rows_without_a_grade_are_skipped_and_counted():
    rows = DATASET + [{"n": 9, "prompt": "yes proceed", "cwd": "/w"}]
    report = recall_eval.run_graded_replay(rows, recall_eval.index_grades(GRADES), push=fake_push)
    assert report["summary"]["prompts"] == 4 and report["summary"]["ungraded_prompts"] == 1


def test_a_push_error_counts_as_an_empty_push():
    def broken(prompt, *, cwd):
        raise RuntimeError("boom")

    report = recall_eval.run_graded_replay(DATASET[1:2], recall_eval.index_grades(GRADES), push=broken)
    assert report["summary"]["abstained_correctly"] == 1
    assert report["rows"][0]["reason"].startswith("error")


def test_grades_merge_across_files_and_ideal_counts_as_relevant(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text(json.dumps(GRADES[0]) + "\n")
    b.write_text("\n".join(json.dumps(g) for g in GRADES[1:]) + "\n")
    grades = recall_eval.index_grades(recall_eval.load_jsonl(a) + recall_eval.load_jsonl(b))
    assert sorted(grades) == [0, 1, 2, 3]
    assert recall_eval.grade_label(grades[3], "episode:30") == "relevant"
    assert recall_eval.grade_label(grades[0], "topic:note:raw") == "irrelevant"
    assert recall_eval.grade_label(grades[0], "episode:404") is None


def test_script_runs_on_files_with_a_fake_push(tmp_path, capsys):
    data, grades = tmp_path / "data.jsonl", tmp_path / "grades.jsonl"
    data.write_text("\n".join(json.dumps(r) for r in DATASET) + "\n")
    grades.write_text("\n".join(json.dumps(g) for g in GRADES) + "\n")
    path = Path(__file__).resolve().parents[1] / "scripts" / "recall_replay_grade.py"
    spec = importlib.util.spec_from_file_location("recall_replay_grade", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main([str(data), str(grades), "--json"], push=fake_push) == 0
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary["ideal_hits"] == 1 and summary["abstained_correctly"] == 1
    assert script.main([str(data), str(grades)], push=fake_push) == 0
    assert "relevant share" in capsys.readouterr().out
