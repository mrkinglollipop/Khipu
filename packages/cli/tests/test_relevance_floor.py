"""khipu.relevance: the all-or-nothing abstention gate behind ``relevance_floor``.

Everything here runs against fakes or the synthetic corpus: no database, no
network, no real embedding call. The gate is a pure function; each of the
three query-driven paths (local prompt lane, budgeted hub lane, explicit
hybrid/semantic search) is exercised with the switch on and off.
"""
from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

import pytest

from khipu import embed as em
from khipu import features, recall_eval, relevance
from khipu import recall_prompt as rp
from tests.fixtures import corpus
from tests.test_recall_prompt import _FakeConnCtx, _fake_enrich_with_zero_decisions

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


ENV = "KHIPU_FEATURE_RELEVANCE_FLOOR"
UNRELATED = "spreadsheet macro recalculation freeze pane workbook"
FLOOR = relevance.COSINE_FLOOR


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def _on(monkeypatch):
    monkeypatch.setenv(ENV, "1")


# ---- the gate ------------------------------------------------------------------

def test_the_floor_constant_is_the_measured_one():
    assert relevance.COSINE_FLOOR == 0.65


def test_a_cosine_at_or_above_the_floor_is_evidence_whatever_the_keywords():
    assert relevance.is_evidence({"cosine": 0.65, "lexical_hits": 0}, FLOOR, 3)
    assert relevance.is_evidence({"cosine": 0.9}, FLOOR, 3)


def test_a_cosine_below_the_floor_without_enough_keywords_is_not_evidence():
    assert not relevance.is_evidence({"cosine": 0.64, "lexical_hits": 0}, FLOOR, 2)
    assert not relevance.is_evidence({"cosine": 0.1, "lexical_hits": 1}, FLOOR, 2)


@pytest.mark.parametrize("tokens, needed", [(1, 1), (2, 2), (3, 2), (4, 2), (5, 3), (8, 4)])
def test_need_is_half_the_tokens_never_below_two_and_never_above_the_count(tokens, needed):
    assert relevance.need(tokens) == needed


@pytest.mark.parametrize("tokens", [1, 2, 3, 5, 8])
def test_keyword_coverage_is_evidence_exactly_at_need(tokens):
    needed = relevance.need(tokens)
    assert relevance.is_evidence({"lexical_hits": needed}, FLOOR, needed)
    assert relevance.is_evidence({"lexical_hits": needed + 1, "cosine": 0.1}, FLOOR, needed)
    if needed > 0:
        assert not relevance.is_evidence({"lexical_hits": needed - 1}, FLOOR, needed)


@pytest.mark.parametrize("tokens", [1, 2, 3, 5, 8])
def test_one_common_word_never_covers_a_multi_word_query(tokens):
    rows = [{"id": "a", "lexical_hits": 1, "cosine": 0.3}]
    kept, info = relevance.gate(rows, tokens)
    assert (kept == rows) == (tokens == 1)
    assert info["abstained"] is (tokens != 1)


def test_a_row_with_neither_field_is_not_evidence():
    assert not relevance.is_evidence({}, FLOOR, 2)
    assert not relevance.is_evidence({"lexical_hits": None, "cosine": None}, FLOOR, 2)
    kept, info = relevance.gate([{"kind": "topic", "id": "t", "via": "topic:x"}], 3)
    assert kept == [] and info["abstained"] is True


def test_an_exact_phrase_row_is_evidence_without_cosine_or_keyword_hits():
    # A session-id query has no cosine, and the id is not in the row text as
    # tokens, so the row matched through an identity index: lexical_hits is 0.
    row = {"kind": "episode", "id": "1", "exact_phrase": True, "lexical_hits": 0}
    assert relevance.is_evidence(row, FLOOR, 3)
    assert relevance.is_evidence({"exact_phrase": True}, FLOOR, 3)
    assert relevance.is_evidence({"exact_phrase": True, "cosine": 0.1}, FLOOR, 3, {"p@1": 0.9})
    for not_exact in (False, None, 1, "true"):
        assert not relevance.is_evidence({"exact_phrase": not_exact, "lexical_hits": 0}, FLOOR, 3)
    kept, info = relevance.gate([{"id": "a", "cosine": 0.1}, row], 3)
    assert [r["id"] for r in kept] == ["a", "1"]
    assert info["evidence_rows"] == 1 and info["abstained"] is False


def test_the_list_comes_back_unchanged_when_any_row_is_evidence():
    rows = [{"id": "a", "cosine": 0.1}, {"id": "b"}, {"id": "c", "cosine": 0.9}, {"id": "d", "lexical_hits": 0}]
    kept, info = relevance.gate(rows, 3)
    assert kept == rows
    assert all(k is r for k, r in zip(kept, rows))
    assert info == {"applied": True, "abstained": False, "evidence_rows": 1, "floor": FLOOR, "need": 2}
    assert kept is not rows, "a new list, the same row objects"


def test_the_list_is_emptied_when_no_row_is_evidence():
    rows = [{"id": "a", "cosine": 0.4}, {"id": "b", "cosine": 0.63, "lexical_hits": 1}]
    kept, info = relevance.gate(rows, 3)
    assert kept == []
    assert info == {"applied": True, "abstained": True, "evidence_rows": 0, "floor": FLOOR, "need": 2}


def test_the_gate_never_removes_individual_rows():
    rows = [{"id": str(i), "cosine": 0.1} for i in range(5)] + [{"id": "hit", "lexical_hits": 2}]
    kept, _ = relevance.gate(rows, 3)
    assert [r["id"] for r in kept] == [r["id"] for r in rows]


def test_with_no_content_tokens_the_gate_does_not_run():
    rows = [{"id": "a", "cosine": 0.1}]
    kept, info = relevance.gate(rows, 0)
    assert kept == rows and info is None


def test_the_floor_is_overridable_by_config_and_ignores_a_bad_value():
    def cfg(value):
        return mock.patch("khipu.config.load_config", return_value={"relevance": {"cosine_floor": value}})

    with mock.patch("khipu.config.load_config", return_value={}):
        assert relevance.cosine_floor() == 0.65
    with cfg(0.8):
        assert relevance.cosine_floor() == 0.8
    for bad in ("0.8", True, 0, -1, 1.5, None):
        with cfg(bad):
            assert relevance.cosine_floor() == 0.65
    rows = [{"id": "a", "cosine": 0.7}]
    with cfg(0.8):
        assert relevance.gate(rows, 3)[0] == []
    with cfg(0.6):
        assert relevance.gate(rows, 3)[0] == rows






# ---- local prompt lane ---------------------------------------------------------

def test_the_local_lane_abstains_on_an_unrelated_prompt_with_the_switch_off(tmp_path):
    # Automatic push has its own per-row evidence floor (recency.PUSH_COVERAGE,
    # 2026-10-30): a nearest neighbour no longer survives with the switch off.
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(UNRELATED, cwd=None)
    assert result["hits"] == []


def test_the_local_lane_abstains_on_an_unrelated_prompt_with_the_switch_on(tmp_path, monkeypatch):
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(UNRELATED, cwd=None)
    assert result["hits"] == []
    assert result["context"] == ""
    assert result["legs"] == ["lexical", "cosine", "graph_candidates"]


def test_the_budgeted_snapshot_lane_reports_no_match_when_the_gate_empties_the_list(tmp_path, monkeypatch):
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


def test_the_local_lane_runs_the_gate_once(tmp_path, monkeypatch):
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path), \
            mock.patch.object(relevance, "gate", wraps=relevance.gate) as gate:
        rp.prior_work_for_prompt(UNRELATED, cwd=None)
    assert gate.call_count == 1


def test_a_failing_gate_costs_the_local_lane_nothing(tmp_path, monkeypatch):
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path):
        with mock.patch.object(relevance, "gate", side_effect=RuntimeError("bug")):
            result = rp.prior_work_for_prompt(
                "what did we approve for the widget-batching endpoint pagination", cwd=None)
    assert any(h["kind"] == "episode" and h["id"] == "1" for h in result["hits"]), \
        "fail open: the gate's failure costs the lane nothing"


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


# The switch's gate is all or nothing; automatic push then keeps only the rows
# that are evidence themselves (recency.apply_push_policy, 2026-10-30).

def test_the_hub_lane_keeps_only_evidence_rows_with_the_switch_off():
    out = _hub([_lex("1")], [_cos("2", 0.3), _cos("3", 0.3)])
    assert {h["id"] for h in out["hits"]} == {"1"}


def test_the_hub_lane_keeps_the_row_that_covers_the_query(monkeypatch):
    _on(monkeypatch)
    out = _hub([_lex("1")], [_cos("2", 0.3), _cos("3", 0.3)])
    assert {h["id"] for h in out["hits"]} == {"1"}


def test_the_hub_lane_keeps_the_row_with_a_high_cosine(monkeypatch):
    _on(monkeypatch)
    out = _hub([_lex("1", "gateway only")], [_cos("2", 0.3), _cos("4", 0.9)])
    assert {h["id"] for h in out["hits"]} == {"4"}


def test_the_hub_lane_counts_keywords_on_a_cosine_only_row(monkeypatch):
    _on(monkeypatch)
    out = _hub([], [_cos("2", 0.3, rank_text="the gateway budget plan"), _cos("3", 0.3)])
    assert {h["id"] for h in out["hits"]} == {"2"}


def test_the_hub_lane_abstains_when_one_common_word_is_all_there_is(monkeypatch):
    _on(monkeypatch)
    out = _hub([_lex("1", "gateway only")], [_cos("2", 0.3), _cos("3", 0.2)])
    assert out["hits"] == []
    assert rp._outcome_for("ok", out["hits"]) == "no_match"


# ---- explicit search -----------------------------------------------------------

QUERY = "widget batching endpoint"


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


def _no_evidence_rows() -> list[dict]:
    return [r for r in _explicit_rows() if r["id"] != "r1"]


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
def test_explicit_search_returns_the_whole_list_when_one_row_is_evidence(monkeypatch, mode):
    _on(monkeypatch)
    with _hub_of(_explicit_rows()):
        payload = em.hybrid_search(QUERY, limit=8, mode=mode)
    assert set(_ids(payload)) == {"r1", "k1", "n1", "n2"}
    assert payload["relevance_floor"] == {
        "applied": True, "abstained": False, "evidence_rows": 1, "floor": FLOOR, "need": 2}


@pytest.mark.parametrize("mode", ["hybrid", "semantic"])
def test_explicit_search_abstains_and_says_so_when_no_row_is_evidence(monkeypatch, mode):
    _on(monkeypatch)
    with _hub_of(_no_evidence_rows()):
        payload = em.hybrid_search(QUERY, limit=8, mode=mode)
    assert payload["results"] == []
    assert payload["relevance_floor"] == {
        "applied": True, "abstained": True, "evidence_rows": 0, "floor": FLOOR, "need": 2}


def test_the_config_floor_reaches_the_explicit_payload(monkeypatch):
    _on(monkeypatch)
    rows = _no_evidence_rows() + [
        {"kind": "episode", "id": "h1", "label": "h1", "snippet": "s", "score": 0.7,
         "rank_text": "unrelated sailing notes"}
    ]
    with _hub_of(rows):
        payload = em.hybrid_search(QUERY, limit=8)
    assert payload["relevance_floor"]["evidence_rows"] == 1 and len(payload["results"]) == 4
    with _hub_of(rows), \
            mock.patch("khipu.config.load_config", return_value={"relevance": {"cosine_floor": 0.95}}):
        payload = em.hybrid_search(QUERY, limit=8)
    assert payload["results"] == []
    assert payload["relevance_floor"]["floor"] == 0.95
    assert payload["relevance_floor"]["abstained"] is True


def test_literal_mode_bypasses_the_gate_even_when_on(monkeypatch):
    literal = [{"kind": "episode", "id": "n1", "label": "n1", "snippet": "s",
                "score": 0.1, "cosine": 0.1, "rank_text": "unrelated gardening notes"}]
    with _hub_of([], literal=literal):
        off = em.hybrid_search("gardening", limit=8, mode="literal")
    _on(monkeypatch)
    with _hub_of([], literal=literal):
        on = em.hybrid_search("gardening", limit=8, mode="literal")
    assert _ids(on) == _ids(off) == ["n1"]
    assert "relevance_floor" not in on


def test_a_query_with_no_content_tokens_is_not_gated(monkeypatch):
    _on(monkeypatch)
    with _hub_of(_no_evidence_rows()):
        payload = em.hybrid_search("the and of", limit=8)
    assert set(_ids(payload)) == {"k1", "n1", "n2"}
    assert "relevance_floor" not in payload


def test_a_failing_gate_never_sinks_an_explicit_search(monkeypatch):
    _on(monkeypatch)
    with _hub_of(_no_evidence_rows()), mock.patch.object(relevance, "gate", side_effect=RuntimeError("bug")):
        payload = em.hybrid_search(QUERY, limit=8)
    assert set(_ids(payload)) == {"k1", "n1", "n2"}
    assert "relevance_floor" not in payload
    assert "relevance_floor_error" in payload["timing"]


def test_switching_on_over_high_cosine_corpus_rows_changes_nothing(monkeypatch):
    rows = _corpus_rows()
    with _hub_of(rows, literal=rows[:6]):
        baseline = em.hybrid_search(QUERY, limit=8)
    _on(monkeypatch)
    with _hub_of(rows, literal=rows[:6]):
        gated = em.hybrid_search(QUERY, limit=8)
    assert _ids(gated) == _ids(baseline)
    assert gated["relevance_floor"]["abstained"] is False


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


def test_the_no_relevant_memory_scenario_passes_with_the_switch_on(tmp_path, monkeypatch):
    """The scenario suite's abstention case runs with no embedding profile
    because a nearest-neighbour cosine leg always returns something; with the
    gate on it passes with the profile active too."""
    _on(monkeypatch)
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(UNRELATED, cwd=None, budget_ms=600)
    assert result["hits"] == []
    assert result["context"] == ""
    assert result["prior_work_meta"]["outcome"] == "no_match"
