# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.rerank: the optional reranking stage of explicit search.

The provider is always a stub (``khipu.rerank._call_model`` is the one seam);
no test reaches a real model, the network or a database. ``hybrid_search`` runs
against a fake hub whose candidate rows come from the synthetic corpus.
"""
from __future__ import annotations

import ast
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from khipu import embed as em
from khipu import features, recall_eval, rerank
from tests.fixtures import corpus

FAKE_KEY = "sk-ant-" + "A1b2C3d4E5f6G7h8I9j0K1l2"


def _row(i: int, **extra) -> dict:
    row = {"kind": "episode", "id": str(i), "label": f"label {i}",
           "snippet": f"snippet {i}", "score": 1.0 / (20 + i + 1)}
    row.update(extra)
    return row


def _rows(n: int) -> list[dict]:
    return [_row(i) for i in range(n)]


def _ids(rows) -> list[str]:
    return [r["id"] for r in rows]


@contextmanager
def _stub_model(answer, *, seen: list | None = None):
    """Replace the provider call: ``answer`` is the text returned, or an
    exception instance/class to raise, or a callable taking the prompt."""
    def fake(prompt: str, *, timeout: float):
        if seen is not None:
            seen.append(prompt)
        if callable(answer) and not isinstance(answer, BaseException):
            return answer(prompt), "stub-model"
        if isinstance(answer, BaseException) or (
            isinstance(answer, type) and issubclass(answer, BaseException)
        ):
            raise answer
        return answer, "stub-model"

    with mock.patch.object(rerank, "_call_model", side_effect=fake):
        yield


# ---- strict permutation parsing ----------------------------------------------

@pytest.mark.parametrize("text, count, expected", [
    ('{"order": [2, 0, 1]}', 3, [2, 0, 1]),
    ("[1, 0]", 3, [1, 0]),
    ('```json\n{"order": [1]}\n```', 3, [1]),
    ("[2]", 3, [2]),
])
def test_parse_accepts_a_clean_subset_permutation(text, count, expected):
    assert rerank.parse_order(text, count) == expected


@pytest.mark.parametrize("text", [
    "",
    "not json",
    "[]",
    '{"order": []}',
    '{"other": [0]}',
    "[0, 0]",            # duplicate
    "[0, 3]",            # out of range
    "[-1, 0]",           # negative
    '["0", 1]',          # string index
    "[0.0, 1]",          # float index
    "[true, 1]",         # boolean index
    "[0, null]",
    "3",
    '{"order": "0,1"}',
])
def test_parse_rejects_anything_that_is_not_a_clean_answer(text):
    assert rerank.parse_order(text, 3) is None


# ---- rank-space fusion ---------------------------------------------------------

def test_a_strong_direct_hit_stays_first_when_the_model_puts_it_last_of_twelve():
    fused = _rows(12)
    fused[0]["lexical_hits"] = 3
    order = list(range(11, 0, -1)) + [0]  # the model's last choice is row 0
    out = rerank.fuse(fused, order, token_count=3)
    assert out[0]["id"] == "0"
    assert sorted(_ids(out)) == sorted(_ids(fused))


def test_without_full_coverage_the_model_can_lift_a_row_over_the_head():
    fused = _rows(12)
    fused[0]["lexical_hits"] = 1
    out = rerank.fuse(fused, [1, 2, 0], token_count=3)
    assert out[0]["id"] == "1"


def test_an_omitted_row_is_not_dropped_and_keeps_its_place_among_the_rest():
    fused = _rows(6)
    out = rerank.fuse(fused, [4, 3], token_count=0)
    assert sorted(_ids(out)) == sorted(_ids(fused))
    omitted = [i for i in _ids(out) if i not in ("4", "3")]
    assert omitted == ["0", "1", "2", "5"]  # their relative order is unchanged


def test_fusion_scores_come_from_ranks_only():
    fused = _rows(3)
    out = rerank.fuse(fused, [2, 1, 0], token_count=0)
    from khipu.search_text import RRF_K

    by_id = {r["id"]: r["score"] for r in out}
    assert by_id["2"] == round(1 / (RRF_K + 3) + 1 / (RRF_K + 1), 6)


# ---- bypass: exact strings are never reordered ---------------------------------

@pytest.mark.parametrize("query, mode", [
    ("kind:slug", "hybrid"),
    ("path__node", "semantic"),
    ("plain words here", "literal"),
])
def test_id_shaped_queries_and_literal_mode_never_reach_the_model(query, mode):
    fused = _rows(5)
    with _stub_model(AssertionError("model must not be called")) as _:
        out, payload, degraded = rerank.stage(query, fused, mode=mode, token_count=2)
    assert out is fused
    assert payload["applied"] is False
    assert payload["reason"] in ("id-shaped-query", "literal-mode")
    assert degraded is False


# ---- failure drops the stage, and says so --------------------------------------

def test_a_provider_error_drops_the_stage_and_names_it():
    fused = _rows(5)
    with _stub_model(RuntimeError("boom")):
        out, payload, degraded = rerank.stage("some query", fused, mode="hybrid", token_count=2)
    assert out is fused
    assert (payload["applied"], payload["reason"], degraded) == (False, "provider-error", True)


def test_a_deadline_miss_drops_the_stage_even_when_the_provider_is_slow():
    fused = _rows(5)

    def slow(prompt: str) -> str:
        time.sleep(0.6)
        return "[0]"

    started = time.monotonic()
    with _stub_model(slow):
        out, payload, degraded = rerank.stage(
            "some query", fused, mode="hybrid", token_count=2, deadline_s=0.05
        )
    assert time.monotonic() - started < 0.5
    assert out is fused
    assert (payload["applied"], payload["reason"], degraded) == (False, "timeout", True)


def test_an_unparseable_answer_drops_the_stage():
    fused = _rows(5)
    with _stub_model("[0, 0]"):
        out, payload, degraded = rerank.stage("some query", fused, mode="hybrid", token_count=2)
    assert out is fused
    assert (payload["reason"], degraded) == ("parse-error", True)


def test_no_configured_provider_drops_the_stage():
    fused = _rows(5)
    with _stub_model(rerank._Unavailable("no key")):
        out, payload, degraded = rerank.stage("some query", fused, mode="hybrid", token_count=2)
    assert out is fused
    assert (payload["reason"], degraded) == ("no-provider", True)


def test_fewer_than_two_candidates_is_not_a_degradation():
    fused = _rows(1)
    with _stub_model(AssertionError("model must not be called")):
        out, payload, degraded = rerank.stage("some query", fused, mode="hybrid", token_count=2)
    assert out is fused
    assert (payload["reason"], degraded) == ("too-few-candidates", False)


def test_a_clean_answer_is_applied_and_reported():
    fused = _rows(4)
    with _stub_model('{"order": [3, 2]}'):
        out, payload, degraded = rerank.stage("some query", fused, mode="hybrid", token_count=2)
    assert out[0]["id"] == "3"
    assert payload["applied"] is True
    assert (payload["model"], payload["candidates"], payload["reason"]) == ("stub-model", 4, None)
    assert isinstance(payload["ms"], float)
    assert degraded is False


# ---- what the provider is sent -------------------------------------------------

def test_at_most_twelve_clipped_redacted_snippets_are_sent():
    fused = [_row(i, snippet=f"note {i} key {FAKE_KEY} " + "x" * 900) for i in range(20)]
    seen: list[str] = []
    with _stub_model("[0, 1]", seen=seen):
        rerank.stage(f"find {FAKE_KEY}", fused, mode="hybrid", token_count=2)
    (prompt,) = seen
    assert FAKE_KEY not in prompt
    assert "[REDACTED]" in prompt
    lines = re.findall(r"^\[(\d+)\] episode (\d+): (.*)$", prompt, re.M)
    assert [n for n, _, _ in lines] == [str(i) for i in range(12)]
    assert all(len(text) <= rerank.SNIPPET_CHARS for _, _, text in lines)
    assert "[12]" not in prompt


# ---- provider routing ----------------------------------------------------------

def test_the_call_uses_the_configured_cloud_synth_model_with_no_retries():
    from khipu import extract

    settings = {"provider": "cloud", "endpoint": "", "model_id": "acme-model"}
    with mock.patch("khipu.models.synth_settings", return_value=settings), \
         mock.patch.object(extract, "_key", return_value="k"), \
         mock.patch.object(extract, "_generate_cloud", return_value="[0]") as gen:
        assert rerank._call_model("p", timeout=1.5) == ("[0]", "acme-model")
    assert gen.call_args.kwargs == {"model_id": "acme-model", "timeout": 1.5, "retries": 0}


def test_the_call_uses_the_local_synth_endpoint_when_that_is_configured():
    from khipu import extract

    settings = {"provider": "local", "endpoint": "http://127.0.0.1:1/v1", "model_id": "tiny"}
    with mock.patch("khipu.models.synth_settings", return_value=settings), \
         mock.patch.object(extract, "_generate_local", return_value="[1]") as gen:
        assert rerank._call_model("p", timeout=2.0) == ("[1]", "tiny")
    assert gen.call_args.kwargs["endpoint"] == "http://127.0.0.1:1/v1"
    assert gen.call_args.kwargs["retries"] == 0


def test_a_missing_key_or_local_endpoint_is_no_provider_not_an_error():
    from khipu import extract

    with mock.patch("khipu.models.synth_settings",
                    return_value={"provider": "local", "endpoint": "", "model_id": ""}):
        with pytest.raises(rerank._Unavailable):
            rerank._call_model("p", timeout=1.0)
    with mock.patch("khipu.models.synth_settings",
                    return_value={"provider": "cloud", "endpoint": "", "model_id": "m"}), \
         mock.patch.object(extract, "_key", side_effect=RuntimeError("no key")):
        with pytest.raises(rerank._Unavailable):
            rerank._call_model("p", timeout=1.0)


# ---- hybrid_search wiring, against a fake hub ---------------------------------

def _corpus_rows() -> list[dict]:
    """Candidate rows from the synthetic corpus (no ``ts``, so the recency
    bonus, which reads the wall clock, cannot differ between two runs)."""
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for i, ep in enumerate(e for e in corpus._named_episodes(now) if not e.deleted_at):
        rows.append({
            "kind": "episode", "id": str(ep.id), "label": f"episode {ep.id}",
            "snippet": ep.summary, "score": round(0.9 - i * 0.01, 4),
            "rank_text": corpus._episode_rank_text(ep), "project": ep.project,
        })
    return rows


class _Conn:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _fake_hub(rows: list[dict]):
    literal = [dict(r) for r in rows[:6]]
    with mock.patch.object(em, "_cosine_candidates",
                           side_effect=lambda *a, **k: [dict(r) for r in rows]), \
         mock.patch("khipu.cli._literal_candidates", side_effect=lambda *a, **k: [dict(r) for r in literal]), \
         mock.patch("khipu.hub_snapshot.try_hub_connect", return_value=_Conn()), \
         mock.patch.object(em, "_apply_search_filters", side_effect=lambda cur, r, **k: r), \
         mock.patch("khipu.topic_graph.enrich_search_results", side_effect=lambda cur, r: list(r)), \
         mock.patch("khipu.decisions.enrich_search_results", side_effect=lambda cur, r: list(r)):
        yield


def _search(query="widget batching endpoint", **kw):
    return em.hybrid_search(query, limit=8, **kw)


def _stable(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "timing"}


def test_the_switch_off_leaves_hybrid_search_output_identical(monkeypatch):
    rows = _corpus_rows()
    monkeypatch.delenv("KHIPU_FEATURE_RERANK", raising=False)
    with _fake_hub(rows), _stub_model(AssertionError("model must not be called")):
        default = _search()
    monkeypatch.setenv("KHIPU_FEATURE_RERANK", "0")
    with _fake_hub(rows), _stub_model(AssertionError("model must not be called")):
        explicit_off = _search()
    assert _stable(default) == _stable(explicit_off)
    assert "rerank" not in default
    assert "degraded_legs" not in default


def test_switching_on_adds_the_payload_block_and_moves_only_ranks(monkeypatch):
    rows = _corpus_rows()
    monkeypatch.delenv("KHIPU_FEATURE_RERANK", raising=False)
    with _fake_hub(rows):
        baseline = _search()
    monkeypatch.setenv("KHIPU_FEATURE_RERANK", "1")
    last = len(baseline["results"]) - 1
    with _fake_hub(rows), _stub_model(json.dumps({"order": [last]})):
        staged = _search()
    assert staged["rerank"]["applied"] is True
    assert staged["rerank"]["model"] == "stub-model"
    assert staged["rerank"]["candidates"] == rerank.MAX_CANDIDATES
    assert "degraded_legs" not in staged
    assert set(staged) - set(baseline) == {"rerank"}
    assert len(staged["results"]) == len(baseline["results"])


@pytest.mark.parametrize("answer, reason", [
    (RuntimeError("boom"), "provider-error"),
    ("[0, 0]", "parse-error"),
])
def test_a_failed_stage_is_named_and_the_results_match_the_switch_off_search(monkeypatch, answer, reason):
    rows = _corpus_rows()
    monkeypatch.delenv("KHIPU_FEATURE_RERANK", raising=False)
    with _fake_hub(rows):
        baseline = _search()
    monkeypatch.setenv("KHIPU_FEATURE_RERANK", "1")
    with _fake_hub(rows), _stub_model(answer):
        staged = _search()
    assert staged["degraded_legs"] == ["rerank"]
    assert staged["rerank"]["applied"] is False
    assert staged["rerank"]["reason"] == reason
    assert staged["results"] == baseline["results"]


def test_a_stage_that_raises_unexpectedly_never_sinks_the_search(monkeypatch):
    rows = _corpus_rows()
    monkeypatch.setenv("KHIPU_FEATURE_RERANK", "1")
    with _fake_hub(rows), mock.patch.object(rerank, "stage", side_effect=ValueError("bug")):
        staged = _search()
    assert staged["results"]
    assert staged["degraded_legs"] == ["rerank"]
    assert staged["rerank"]["reason"] == "error"


@pytest.mark.parametrize("query, mode", [("kind:slug rollout", "hybrid"), ("widget batching", "literal")])
def test_id_shaped_and_literal_searches_are_not_reranked_end_to_end(monkeypatch, query, mode):
    rows = _corpus_rows()
    monkeypatch.setenv("KHIPU_FEATURE_RERANK", "1")
    with _fake_hub(rows), _stub_model(AssertionError("model must not be called")):
        out = _search(query, mode=mode)
    assert out["rerank"]["applied"] is False
    assert "degraded_legs" not in out


# ---- the per-prompt path never reaches the reranker ---------------------------

def _module_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{a.name}" for a in node.names)
    return names


def test_recall_prompt_never_imports_the_reranker():
    root = Path(rerank.__file__).parent
    imported = _module_level_imports(root / "recall_prompt.py")
    assert not {n for n in imported if n == "khipu.rerank" or n.startswith("khipu.rerank.")}
    assert "khipu.rerank" not in imported


def test_the_reranker_is_imported_only_inside_hybrid_search():
    tree = ast.parse(Path(em.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names} | {getattr(node, "module", None)}
            assert not names & {"rerank", "khipu.rerank"}


# ---- capability, switch, eval flag ---------------------------------------------

def test_the_capability_is_advertised_and_the_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("KHIPU_FEATURE_RERANK", raising=False)
    assert "search.rerank" in features.capabilities()
    assert features.enabled("rerank") is False


def test_eval_scores_the_same_entries_both_ways_and_restores_the_switch(monkeypatch):
    monkeypatch.delenv("KHIPU_FEATURE_RERANK", raising=False)
    seen_switch: list[str | None] = []

    def fake_search(query, **kw):
        import os

        seen_switch.append(os.environ.get("KHIPU_FEATURE_RERANK"))
        ids = ["a", "b", "c"] if os.environ["KHIPU_FEATURE_RERANK"] == "0" else ["c", "a", "b"]
        return {"results": [{"id": i} for i in ids],
                "rerank": {"applied": True, "model": "m", "ms": 1.0, "candidates": 3, "reason": None}}

    entries = [{"query": "q one", "expect": ["c"]}, {"query": "q two", "expect": ["zzz"]}]
    with mock.patch.object(em, "hybrid_search", side_effect=fake_search):
        report = recall_eval.run_rerank_eval(entries, rerank=True)
    first, second = report["rows"]
    assert (first["rank_without"], first["rank_with"]) == (3, 1)
    assert (second["rank_without"], second["rank_with"]) == (None, None)
    assert report["summary"]["improved"] == 1 and report["summary"]["worsened"] == 0
    assert seen_switch == ["0", "1", "0", "1"]
    import os

    assert "KHIPU_FEATURE_RERANK" not in os.environ


def test_eval_with_the_switch_off_reports_only_the_baseline_rank(monkeypatch):
    with mock.patch.object(em, "hybrid_search", return_value={"results": [{"id": "a"}, {"id": "b"}]}) as hs:
        report = recall_eval.run_rerank_eval([{"query": "q", "expect": ["b"]}], rerank=False)
    assert report["rows"][0]["rank_without"] == 2 and report["rows"][0]["rank_with"] is None
    assert hs.call_count == 1


def test_the_recall_eval_command_takes_rerank_on_and_off(tmp_path, capsys):
    import argparse

    from khipu import cli

    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"query": "q", "expect": ["b"]}) + "\n", encoding="utf-8")
    args = argparse.Namespace(recall_cmd="eval", golden=str(golden), rerank="on", path=None,
                              record=None, compare=None, replay=None)
    with mock.patch.object(em, "hybrid_search", return_value={"results": [{"id": "b"}]}):
        assert cli.cmd_recall(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rerank"] == "on" and out["rows"][0]["rank_with"] == 1

    args.path = "prompt"
    assert cli.cmd_recall(args) == 2
