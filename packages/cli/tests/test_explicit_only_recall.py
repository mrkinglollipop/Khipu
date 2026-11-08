"""The deletion gate's offline golden fixture; memory survives both removals."""
from pathlib import Path

from khipu import hub_snapshot, recall_eval
from tests.test_recall_push_ranking import (  # noqa: F401
    KHIPU, _days_ago, _replica, isolated,
)


def test_episode_decision_text_remains_reachable_in_golden_recall(tmp_path, monkeypatch):
    entries = recall_eval.load_golden(Path(__file__).parent / "fixtures" / "explicit-memory-golden.jsonl")
    rows = []
    for i, entry in enumerate(entries):
        folder = tmp_path / str(i)
        folder.mkdir()
        _replica(folder, monkeypatch,
                 episodes=[(10, entry["fixture_summary"], KHIPU, _days_ago(1), *entry["fixture_decisions"])],
                 cosine=[("episode", 10, .9)])
        assert [row["id"] for row in hub_snapshot.search_snapshot(
            entry["query"], 3, kind="episode")] == ["10"]
        rows.extend(recall_eval.run_eval_paths([entry], ("prompt",))["rows"])
    result = recall_eval.summarize_rows(rows)
    assert result["hits"] == len(entries), rows
    assert result["mrr"] == 1.0 and result["errors"] == 0 and result["stale_violations"] == 0, rows
