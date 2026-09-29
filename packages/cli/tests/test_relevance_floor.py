# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.relevance: the absolute relevance floor behind ``relevance_floor``.

Everything here runs against fakes or the synthetic corpus: no database, no
network, no real embedding call. The policy is a pure function; each of the
three query-driven paths (local prompt lane, budgeted hub lane, explicit
hybrid/semantic search) is exercised with the switch on and off.
"""
from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from unittest import mock

import pytest

from khipu import embed as em
from khipu import features, recall_eval, relevance
from khipu import recall_prompt as rp
from tests.fixtures import corpus
from tests.test_recall_prompt import _FakeConnCtx, _fake_enrich_with_zero_decisions
from tests.test_rerank import _Conn, _corpus_rows

ENV = "KHIPU_FEATURE_RELEVANCE_FLOOR"
UNRELATED = "spreadsheet macro recalculation freeze pane workbook"


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def _on(monkeypatch):
    monkeypatch.setenv(ENV, "1")


# ---- the rule ------------------------------------------------------------------

def test_a_whole_word_keyword_hit_keeps_a_row_whatever_its_cosine():
    assert relevance.keeps({"lexical_hits": 1, "cosine": 0.01}, 0.62)
    assert relevance.keeps({"lexical_hits": 2}, 0.62)


def test_a_cosine_at_or_above_the_floor_keeps_a_row_with_no_keyword_hit():
    assert relevance.keeps({"lexical_hits": 0, "cosine": 0.62}, 0.62)
    assert relevance.keeps({"cosine": 0.9}, 0.62)


def test_a_cosine_below_the_floor_and_no_keyword_hit_removes_a_row():
    assert not relevance.keeps({"lexical_hits": 0, "cosine": 0.61}, 0.62)
    assert not relevance.keeps({"cosine": 0.1}, 0.62)


def test_a_row_with_neither_signal_is_kept_because_nothing_is_known_against_it():
    assert relevance.keeps({}, 0.62)
    assert relevance.keeps({"lexical_hits": None, "cosine": None}, 0.62)
    assert relevance.keeps({"kind": "topic", "id": "t", "via": "topic:x"}, 0.62)


def test_apply_keeps_order_and_reports_how_many_it_removed():
    rows = [{"id": "a", "cosine": 0.9}, {"id": "b", "cosine": 0.1},
            {"id": "c", "lexical_hits": 1, "cosine": 0.1}, {"id": "d"}]
    kept, removed = relevance.apply(rows, floor=0.5)
    assert [r["id"] for r in kept] == ["a", "c", "d"]
    assert removed == 1


def test_the_floor_is_overridable_by_config_and_ignores_a_bad_value():
    def cfg(value):
        return mock.patch("khipu.config.load_config", return_value={"relevance": {"cosine_floor": value}})

    assert relevance.COSINE_FLOOR == 0.62
    with mock.patch("khipu.config.load_config", return_value={}):
        assert relevance.cosine_floor() == 0.62
    with cfg(0.8):
        assert relevance.cosine_floor() == 0.8
    for bad in ("0.8", True, 0, -1, 1.5, None):
        with cfg(bad):
            assert relevance.cosine_floor() == 0.62


def test_a_broken_switch_lookup_reads_as_off(monkeypatch):
    with mock.patch.object(features, "enabled", side_effect=RuntimeError("config broke")):
        assert relevance.enabled() is False


def test_the_switch_defaults_off_and_the_capability_is_advertised():
    assert features.enabled("relevance_floor") is False
    assert "search.relevance_floor" in features.capabilities()


# ---- local prompt lane ---------------------------------------------------------

def test_the_local_lane_returns_the_nearest_neighbours_with_the_switch_off(tmp_path):
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(UNRELATED, cwd=None)
    assert result["hits"], "the gap this switch closes: a nearest neighbour survives the relative floor"


def test_the_local_lane_abstains_on_an_unrelated_prompt_with_the_switch_on(tmp_path, monkeypatch):
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(UNRELATED, cwd=None)
    assert result["hits"] == []
    assert result["context"] == ""
    assert result["legs"] == ["lexical", "cosine"]


def test_the_budgeted_snapshot_lane_reports_no_match_when_every_row_is_removed(tmp_path, monkeypatch):
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(UNRELATED, cwd=None, budget_ms=600)
    assert result["hits"] == []
    assert result["prior_work_meta"]["outcome"] == "no_match"


def test_a_prompt_that_names_something_remembered_still_recalls_it_with_the_switch_on(tmp_path, monkeypatch):
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(
            "what did we approve for the widget-batching endpoint pagination", cwd=None
        )
    assert any(h["kind"] == "episode" and h["id"] == "1" for h in result["hits"])


def test_graph_candidates_do_not_resurrect_an_abstention(tmp_path, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setenv("KHIPU_FEATURE_GRAPH_CANDIDATES", "1")
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(UNRELATED, cwd=None)
    assert result["hits"] == []


def test_a_failing_policy_costs_the_local_lane_nothing(tmp_path, monkeypatch):
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path):
        with mock.patch.object(relevance, "apply", side_effect=RuntimeError("bug")):
            result = rp.prior_work_for_prompt(UNRELATED, cwd=None)
    assert result["hits"], "fail open: the rows come back untouched"


# ---- budgeted hub lane ---------------------------------------------------------

def _lex(hid, rank_text="gateway budget hit"):
    return {"kind": "episode", "id": hid, "label": "lex", "snippet": "lex", "rank_text": rank_text}


def _cos(hid, score, rank_text="unrelated gardening notes"):
    return {"kind": "episode", "id": hid, "score": score, "label": "cos", "snippet": "cos",
            "rank_text": rank_text}


def _hub(lex, cos, *, tokens=("gateway", "budget")):
    with mock.patch("khipu.db.connect", return_value=_FakeConnCtx()), \
            mock.patch("khipu.cli._literal_candidates", return_value=lex), \
            mock.patch("khipu.embed._cosine_candidates", return_value=cos), \
            mock.patch("khipu.decisions.enrich_search_results",
                       side_effect=_fake_enrich_with_zero_decisions):
        return rp._hub_hits_budgeted(
            "gateway budget query", list(tokens), project=None, budget_ms=600, limit=10
        )


def test_the_hub_lane_keeps_every_row_with_the_switch_off():
    out = _hub([_lex("1")], [_cos("2", 0.3), _cos("3", 0.3)])
    assert {h["id"] for h in out["hits"]} == {"1", "2", "3"}


def test_the_hub_lane_drops_low_cosine_rows_with_no_keyword_hit_when_on(monkeypatch):
    _on(monkeypatch)
    out = _hub([_lex("1")], [_cos("2", 0.3), _cos("3", 0.3), _cos("4", 0.9)])
    assert {h["id"] for h in out["hits"]} == {"1", "4"}


def test_the_hub_lane_counts_keywords_on_a_cosine_only_row(monkeypatch):
    _on(monkeypatch)
    out = _hub([], [_cos("2", 0.3, rank_text="the gateway budget plan"), _cos("3", 0.3)])
    assert [h["id"] for h in out["hits"]] == ["2"]


def test_the_hub_lane_abstains_when_every_row_is_removed(monkeypatch):
    _on(monkeypatch)
    out = _hub([], [_cos("2", 0.3), _cos("3", 0.2)])
    assert out["hits"] == []
    assert rp._outcome_for("ok", out["hits"]) == "no_match"


# ---- explicit search -----------------------------------------------------------

def _explicit_rows() -> list[dict]:
    relevant = "widget batching endpoint pagination"
    return [
        {"kind": "episode", "id": "r1", "label": "r1", "snippet": "s", "score": 0.9, "rank_text": relevant},
        {"kind": "episode", "id": "k1", "label": "k1", "snippet": "s", "score": 0.3,
         "rank_text": "notes on the endpoint"},
        {"kind": "episode", "id": "n1", "label": "n1", "snippet": "s", "score": 0.4,
         "rank_text": "unrelated gardening notes"},
        {"kind": "episode", "id": "n2", "label": "n2", "snippet": "s", "score": 0.35,
         "rank_text": "unrelated cooking notes"},
    ]


@contextmanager
def _hub_of(rows, literal=()):
    with mock.patch.object(em, "_cosine_candidates",
                           side_effect=lambda *a, **k: [dict(r) for r in rows]), \
            mock.patch("khipu.cli._literal_candidates",
                       side_effect=lambda *a, **k: [dict(r) for r in literal]), \
            mock.patch("khipu.hub_snapshot.try_hub_connect", return_value=_Conn()), \
            mock.patch.object(em, "_apply_search_filters", side_effect=lambda cur, r, **k: r), \
            mock.patch("khipu.topic_graph.enrich_search_results", side_effect=lambda cur, r: list(r)), \
            mock.patch("khipu.decisions.enrich_search_results", side_effect=lambda cur, r: list(r)):
        yield


def _ids(payload):
    return [r["id"] for r in payload["results"]]


@pytest.mark.parametrize("mode", ["hybrid", "semantic"])
def test_explicit_search_keeps_every_row_with_the_switch_off(mode):
    with _hub_of(_explicit_rows()):
        payload = em.hybrid_search("widget batching endpoint", limit=8, mode=mode)
    assert set(_ids(payload)) == {"r1", "k1", "n1", "n2"}
    assert "relevance_floor" not in payload


@pytest.mark.parametrize("mode", ["hybrid", "semantic"])
def test_explicit_search_drops_rows_with_no_keyword_hit_and_a_low_cosine_when_on(monkeypatch, mode):
    _on(monkeypatch)
    with _hub_of(_explicit_rows()):
        payload = em.hybrid_search("widget batching endpoint", limit=8, mode=mode)
    assert set(_ids(payload)) == {"r1", "k1"}
    assert payload["relevance_floor"] == {"applied": True, "removed": 2, "floor": relevance.COSINE_FLOOR}


def test_explicit_search_returns_an_empty_list_and_says_so_when_every_row_is_removed(monkeypatch):
    _on(monkeypatch)
    rows = [r for r in _explicit_rows() if r["id"].startswith("n")]
    with _hub_of(rows):
        payload = em.hybrid_search("widget batching endpoint", limit=8)
    assert payload["results"] == []
    assert payload["relevance_floor"] == {"applied": True, "removed": 2, "floor": relevance.COSINE_FLOOR}


def test_the_config_floor_reaches_the_explicit_payload(monkeypatch):
    _on(monkeypatch)
    with _hub_of(_explicit_rows()), \
            mock.patch("khipu.config.load_config", return_value={"relevance": {"cosine_floor": 0.95}}):
        payload = em.hybrid_search("widget batching endpoint", limit=8)
    # r1 has both a keyword hit and 0.9 cosine: it stays on the keyword.
    assert set(_ids(payload)) == {"r1", "k1"}
    assert payload["relevance_floor"] == {"applied": True, "removed": 2, "floor": 0.95}


def test_literal_mode_bypasses_the_floor_even_when_on(monkeypatch):
    literal = [{"kind": "episode", "id": "n1", "label": "n1", "snippet": "s",
                "score": 0.1, "cosine": 0.1, "rank_text": "unrelated gardening notes"}]
    with _hub_of([], literal=literal):
        off = em.hybrid_search("gardening", limit=8, mode="literal")
    _on(monkeypatch)
    with _hub_of([], literal=literal):
        on = em.hybrid_search("gardening", limit=8, mode="literal")
    assert _ids(on) == _ids(off) == ["n1"]
    assert "relevance_floor" not in on


def test_a_failing_policy_never_sinks_an_explicit_search(monkeypatch):
    _on(monkeypatch)
    with _hub_of(_explicit_rows()), mock.patch.object(relevance, "apply", side_effect=RuntimeError("bug")):
        payload = em.hybrid_search("widget batching endpoint", limit=8)
    assert set(_ids(payload)) == {"r1", "k1", "n1", "n2"}
    assert "relevance_floor" not in payload
    assert "relevance_floor_error" in payload["timing"]


def test_switching_on_over_high_cosine_corpus_rows_removes_nothing(monkeypatch):
    rows = _corpus_rows()
    with _hub_of(rows, literal=rows[:6]):
        baseline = em.hybrid_search("widget batching endpoint", limit=8)
    _on(monkeypatch)
    with _hub_of(rows, literal=rows[:6]):
        floored = em.hybrid_search("widget batching endpoint", limit=8)
    assert _ids(floored) == _ids(baseline)
    assert floored["relevance_floor"]["removed"] == 0


# ---- switch off is byte-identical ----------------------------------------------

_QUERIES = [
    "what did we approve for the widget-batching endpoint pagination",
    "widget-batching endpoint rollout deployment strategy blue-green rolling",
    "three second retry backoff sync job",
    "FileNotFoundError hub snapshot missing var khipu hub_snapshot.sqlite",
    UNRELATED,
]


def _stable(result):
    out = {k: v for k, v in result.items() if k != "ms"}
    meta = out.get("prior_work_meta")
    if meta:
        out["prior_work_meta"] = {k: v for k, v in meta.items() if k != "ms"}
    return out


def test_with_the_switch_off_the_local_lane_is_identical_and_never_consults_the_policy(tmp_path, monkeypatch):
    with corpus.installed_corpus(tmp_path):
        default = [_stable(rp.prior_work_for_prompt(q, cwd=None, budget_ms=600)) for q in _QUERIES]
        monkeypatch.setenv(ENV, "0")
        with mock.patch.object(relevance, "apply", side_effect=AssertionError("policy consulted")):
            explicit_off = [_stable(rp.prior_work_for_prompt(q, cwd=None, budget_ms=600)) for q in _QUERIES]
    assert default == explicit_off


def test_with_the_switch_off_explicit_search_is_identical(monkeypatch):
    rows = _corpus_rows()
    with _hub_of(rows, literal=rows[:6]):
        default = em.hybrid_search("widget batching endpoint", limit=8)
    monkeypatch.setenv(ENV, "0")
    with _hub_of(rows, literal=rows[:6]), \
            mock.patch.object(relevance, "apply", side_effect=AssertionError("policy consulted")):
        explicit_off = em.hybrid_search("widget batching endpoint", limit=8)
    strip = lambda p: {k: v for k, v in p.items() if k != "timing"}  # noqa: E731
    assert strip(default) == strip(explicit_off)


def test_every_positive_scenario_query_still_finds_its_episode_with_the_switch_on(tmp_path, monkeypatch):
    expected = {
        "what did we approve for the widget-batching endpoint pagination": "1",
        "FileNotFoundError hub snapshot missing var khipu hub_snapshot.sqlite": "9",
    }
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path):
        for query, episode_id in expected.items():
            result = rp.prior_work_for_prompt(query, cwd=None)
            assert episode_id in [h["id"] for h in result["hits"]], query


# ---- recall eval ---------------------------------------------------------------

def _fake_search_by_switch(query, **kw):
    on = os.environ[ENV] == "1"
    by_query = {
        "found": (["a", "b"], ["a", "b"]),
        "lost": (["b"], []),
        "nothing remembered": (["x"], []),
    }
    off_ids, on_ids = by_query[query]
    return {"results": [{"id": i} for i in (on_ids if on else off_ids)]}


_ENTRIES = [
    {"query": "found", "expect": ["b"]},
    {"query": "lost", "expect": ["b"]},
    {"query": "nothing remembered", "expect_none": True},
]


def test_eval_lists_the_positives_the_floor_removed_and_scores_abstention(monkeypatch):
    with mock.patch.object(em, "hybrid_search", side_effect=_fake_search_by_switch):
        report = recall_eval.run_relevance_eval(_ENTRIES, ("explicit",), floor=True)
    summary = report["paths"]["explicit"]
    assert summary["removed_positives"] == ["lost"]
    assert (summary["abstain_total"], summary["abstain_correct"], summary["abstain_correct_without"]) == (1, 1, 0)
    assert (summary["positives"], summary["positives_found"]) == (2, 1)
    assert ENV not in os.environ


def test_eval_with_the_switch_off_scores_only_the_baseline(monkeypatch):
    with mock.patch.object(em, "hybrid_search", side_effect=_fake_search_by_switch) as hs:
        report = recall_eval.run_relevance_eval(_ENTRIES, ("explicit",), floor=False)
    summary = report["paths"]["explicit"]
    assert "removed_positives" not in summary
    assert (summary["abstain_total"], summary["abstain_correct"]) == (1, 0)
    assert hs.call_count == 3


def test_eval_restores_a_switch_the_caller_had_set(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    with mock.patch.object(em, "hybrid_search", side_effect=_fake_search_by_switch):
        recall_eval.run_relevance_eval(_ENTRIES[:1], ("explicit",), floor=True)
    assert os.environ[ENV] == "1"


def test_eval_scores_the_prompt_path_with_the_switch_forced_each_way(monkeypatch):
    def fake_prompt(query, **kw):
        on = os.environ[ENV] == "1"
        return {"hits": [] if on else [{"id": "x"}], "reason": "ok"}

    entries = [{"query": "nothing remembered", "expect_none": True}]
    with mock.patch("khipu.recall_prompt.prior_work_for_prompt", side_effect=fake_prompt):
        report = recall_eval.run_relevance_eval(entries, ("prompt", "status"), floor=True)
    for path in ("prompt", "status"):
        assert report["paths"][path]["abstain_correct"] == 1
        assert report["paths"][path]["abstain_correct_without"] == 0


def _golden(tmp_path, entries):
    golden = tmp_path / "golden.jsonl"
    golden.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return golden


def _args(golden, **kw):
    base = dict(recall_cmd="eval", golden=str(golden), rerank=None, path=None, record=None,
                compare=None, replay=None, relevance_floor="on")
    base.update(kw)
    return argparse.Namespace(**base)


def test_the_recall_eval_command_fails_and_names_a_removed_positive(tmp_path, capsys):
    from khipu import cli

    golden = _golden(tmp_path, _ENTRIES)
    with mock.patch.object(em, "hybrid_search", side_effect=_fake_search_by_switch):
        assert cli.cmd_recall(_args(golden)) == 1
    captured = capsys.readouterr()
    assert "[REMOVED] path=explicit 'lost'" in captured.err
    assert json.loads(captured.out)["relevance_floor"] == "on"


def test_the_recall_eval_command_passes_when_nothing_is_removed(tmp_path, capsys):
    from khipu import cli

    golden = _golden(tmp_path, [_ENTRIES[0], _ENTRIES[2]])
    with mock.patch.object(em, "hybrid_search", side_effect=_fake_search_by_switch):
        assert cli.cmd_recall(_args(golden)) == 0
    assert json.loads(capsys.readouterr().out)["paths"]["explicit"]["removed_positives"] == []


def test_the_recall_eval_command_rejects_combinations_it_cannot_honour(tmp_path, capsys):
    from khipu import cli

    golden = _golden(tmp_path, _ENTRIES[:1])
    assert cli.cmd_recall(_args(golden, record=str(tmp_path / "control.json"))) == 2
    assert cli.cmd_recall(_args(golden, rerank="on")) == 2
    capsys.readouterr()
