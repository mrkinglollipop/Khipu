# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""An episode is never lost to an optional step.

With ``decision_details`` and ``auto_supersede`` switched on, capture runs
extra steps around the episode: a detail block in the model's answer, a
per-decision detail insert, a reversal detector and a candidate-link insert.
Each test breaks one of them and asserts the episode still lands with its
summary, topics and plain decisions. The model call and the database are
fakes: nothing here reaches a provider or a hub.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from khipu import capture as cap
from khipu import extract
from tests.test_capture import _EpisodesFakeCursor, _FakeConn
from tests.test_decisions import _Cursor as _DecisionsCursor

SUMMARY = "Reworked the deploy plan for the widget service."
TOPICS = ["deploy-plan"]
DECISIONS = ["Use rolling deploys", "Keep the retry budget"]
OLD_DECISION = "Use blue-green deploys for rollout"
REVERSAL = {"text": "Use rolling deploys", "by": "user", "rationale": "faster",
            "reverses": OLD_DECISION}


@pytest.fixture(autouse=True)
def _switches_on(monkeypatch):
    monkeypatch.setenv("KHIPU_FEATURE_DECISION_DETAILS", "1")
    monkeypatch.setenv("KHIPU_FEATURE_AUTO_SUPERSEDE", "1")


# ---- the model's answer ---------------------------------------------------------

def _answer(block: str | None, *, last: bool = False) -> str:
    """A model answer with ``block`` spliced in raw as the detail block's value,
    in the middle of the object or as its last key."""
    head = (f'{{"summary": "{SUMMARY}", "topics": {json.dumps(TOPICS)}, '
            f'"decisions": {json.dumps(DECISIONS)}, "preferences": [], "people": [], "scope": "repo", ')
    tail_keys = '"open_loops": [], "closed_loops": []'
    if block is None:
        return head + tail_keys + "}"
    if last:
        return head + tail_keys + f', "decision_details": {block}}}'
    return head + f'"decision_details": {block}, ' + tail_keys + "}"


def _extract(raw: str) -> dict | None:
    with mock.patch.object(extract, "_generate", return_value=raw):
        return extract.extract_memory("some transcript", cwd="/work/widget")


def _assert_plain_episode(payload: dict) -> None:
    assert payload["summary"] == SUMMARY
    assert payload["topics"] == TOPICS
    assert payload["decisions"] == DECISIONS
    assert payload["open_loops"] == [] and payload["closed_loops"] == []


_SYNTAX_BROKEN = [
    '[{"text": "Use rolling deploys", "by": "user", "reverses": }]',
    '[{"text": "Use rolling deploys" "by": "user"}]',
    '[{"text": "Use rolling deploys", "by": "user",}]',
    '[{"text": "Use rolling deploys", "by": "user"}',
    '[{"text": "Use rolling deploys", "by": "user"}] trailing words',
    "not a list at all",
]


@pytest.mark.parametrize("last", [False, True], ids=["middle", "last"])
@pytest.mark.parametrize("block", _SYNTAX_BROKEN)
def test_a_detail_block_that_breaks_the_json_costs_only_the_details(block, last):
    payload = _extract(_answer(block, last=last))
    assert payload is not None
    _assert_plain_episode(payload)
    assert payload["decision_details"] == []


@pytest.mark.parametrize("block", [
    '"a string, not a list"', "7", "null", "[1, \"x\", {\"text\": null}, [], {}]",
    '{"text": "Use rolling deploys"}',
])
def test_a_wrongly_typed_or_garbage_detail_block_is_dropped_not_raised(block):
    payload = _extract(_answer(block))
    _assert_plain_episode(payload)
    assert payload["decision_details"] == []


def test_a_detail_naming_a_decision_that_is_not_in_the_list_is_dropped():
    block = json.dumps([
        {"text": "Adopt a monorepo", "by": "user", "reverses": OLD_DECISION},
        REVERSAL,
    ])
    payload = _extract(_answer(block))
    _assert_plain_episode(payload)
    assert [d["text"] for d in payload["decision_details"]] == ["Use rolling deploys"]


def test_a_good_detail_block_still_arrives_intact():
    payload = _extract(_answer(json.dumps([REVERSAL])))
    _assert_plain_episode(payload)
    assert payload["decision_details"] == [
        {"text": "Use rolling deploys", "by": "user", "rationale": "faster", "reverses": OLD_DECISION}
    ]


def test_an_answer_that_is_broken_outside_the_detail_block_still_fails_closed():
    broken = _answer("[]").replace('"topics"', "topics")
    with mock.patch.object(extract, "_generate", return_value=broken):
        with pytest.raises(RuntimeError):
            extract.extract_memory("some transcript")


def test_cutting_a_broken_block_leaves_the_text_around_it_untouched():
    raw = _answer('[{"text": "x" "by": "user"}]').replace(
        SUMMARY, "Explained why decision_details, open_loops and } stay put."
    )
    payload = _extract(raw)
    assert payload["summary"] == "Explained why decision_details, open_loops and } stay put."
    assert payload["topics"] == TOPICS and payload["decisions"] == DECISIONS
    assert payload["decision_details"] == []


# ---- the write path -------------------------------------------------------------

class _CaptureCursor(_EpisodesFakeCursor):
    """The episode fake plus the decisions fake behind one cursor: SQL that
    names ``decisions``/``decision_links`` goes to the decisions fake, the rest
    to the episode fake."""

    def __init__(self, decisions: _DecisionsCursor):
        super().__init__()
        self.decisions = decisions

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        table = params[0] if params else None
        to_decisions = (
            (s.startswith("SELECT column_name FROM information_schema.columns")
             and table in ("decisions", "decision_links"))
            or "decision_links" in s
            or any(f"{kw} decisions" in s for kw in ("FROM", "INTO", "UPDATE"))
            or "pg_extension" in s
        )
        if not to_decisions:
            return super().execute(sql, params)
        self.calls.append((s, params or ()))
        self.decisions.execute(sql, params)
        self.rowcount = self.decisions.rowcount
        self._result = self.decisions._result


def _payload(details) -> dict:
    return {
        "ts": "2026-09-20T00:00:00Z", "session_id": "s1", "summary": SUMMARY, "topics": list(TOPICS),
        "decisions": list(DECISIONS), "project": "acme/widget", "decision_details": details,
    }


def _capture(payload, *, evidence=True, links=True, patches=()):
    decisions = _DecisionsCursor(evidence=evidence, links=links)
    old = decisions.seed(project="acme/widget", text=OLD_DECISION, decided_at="2026-09-01T00:00:00+00:00")
    cur = _CaptureCursor(decisions)
    conn = _FakeConn(cur)
    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch("khipu.topic_graph.persist_capture_graph",
                       return_value={"nodes_minted": 0, "edges_minted": 0}), \
            mock.patch("khipu.config.path_setting", return_value=None), \
            mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key in test")), \
            mock.patch("khipu.hygiene.classify_topics", return_value=(list(TOPICS), [], False)):
        for p in patches:
            p.start()
        try:
            stats = cap.write_pg(payload)
        finally:
            for p in patches:
                p.stop()
    return stats, cur, decisions, old, conn


def _assert_episode_and_plain_decisions(stats, cur, decisions, conn):
    assert stats["episode_inserted"]
    episode = cur.episodes[stats["episode_id"]]
    assert episode["summary"] == SUMMARY
    assert episode["topics"] == TOPICS
    assert episode["decisions"] == DECISIONS
    texts = sorted(r["text"] for r in decisions.rows.values() if r["episode_id"] == stats["episode_id"])
    assert texts == sorted(DECISIONS)
    assert conn.commits >= 1


def test_a_healthy_capture_records_the_detail_and_applies_the_user_reversal():
    stats, cur, decisions, old, conn = _capture(_payload([REVERSAL]))
    _assert_episode_and_plain_decisions(stats, cur, decisions, conn)
    assert decisions.rows[old]["superseded_by"] is not None
    assert [link["state"] for link in decisions.links.values()] == ["applied"]


def test_a_detail_naming_a_decision_outside_the_list_leaves_the_capture_whole():
    detail = {"text": "Adopt a monorepo", "by": "user", "reverses": OLD_DECISION}
    stats, cur, decisions, old, conn = _capture(_payload([detail]))
    _assert_episode_and_plain_decisions(stats, cur, decisions, conn)
    assert decisions.rows[old]["superseded_by"] is None
    assert decisions.links == {}


@pytest.mark.parametrize("details", ["garbage", [1, None, "x"], {"text": "Use rolling deploys"}, None])
def test_a_malformed_detail_payload_leaves_the_capture_whole(details):
    stats, cur, decisions, old, conn = _capture(_payload(details))
    _assert_episode_and_plain_decisions(stats, cur, decisions, conn)
    assert decisions.links == {}


def test_a_raising_reversal_detector_leaves_the_capture_whole():
    boom = mock.patch("khipu.decisions.detect_reversals_from_episode", side_effect=RuntimeError("boom"))
    stats, cur, decisions, old, conn = _capture(_payload([REVERSAL]), patches=[boom])
    _assert_episode_and_plain_decisions(stats, cur, decisions, conn)
    statements = [s for s, _ in cur.calls]
    assert "ROLLBACK TO SAVEPOINT capture_decision_detection" in statements
    assert "ROLLBACK TO SAVEPOINT capture_decisions" not in statements
    assert decisions.rows[old]["superseded_by"] is None


def test_a_raising_candidate_link_insert_leaves_the_capture_whole():
    boom = mock.patch("khipu.decisions.add_link", side_effect=RuntimeError("link insert failed"))
    stats, cur, decisions, old, conn = _capture(_payload([REVERSAL]), patches=[boom])
    _assert_episode_and_plain_decisions(stats, cur, decisions, conn)
    statements = [s for s, _ in cur.calls]
    assert "ROLLBACK TO SAVEPOINT capture_decision_detection" in statements
    assert "ROLLBACK TO SAVEPOINT capture_decisions" not in statements
    assert decisions.links == {}


def test_a_raising_auto_apply_leaves_the_capture_and_the_candidate_whole():
    boom = mock.patch("khipu.decisions.resolve_link", side_effect=RuntimeError("apply failed"))
    stats, cur, decisions, old, conn = _capture(_payload([REVERSAL]), patches=[boom])
    _assert_episode_and_plain_decisions(stats, cur, decisions, conn)
    assert decisions.rows[old]["superseded_by"] is None


@pytest.mark.parametrize("evidence, links", [(False, False), (True, False)],
                         ids=["before-the-migration", "no-links-table"])
def test_a_hub_without_the_new_columns_captures_the_episode_and_plain_decisions(evidence, links):
    stats, cur, decisions, old, conn = _capture(_payload([REVERSAL]), evidence=evidence, links=links)
    _assert_episode_and_plain_decisions(stats, cur, decisions, conn)
    assert decisions.links == {}
    assert decisions.rows[old]["superseded_by"] is None
    if not evidence:
        assert all(r["source_kind"] is None and r["rationale"] is None
                   for r in decisions.rows.values() if r["episode_id"] == stats["episode_id"])


def test_the_merge_path_survives_a_raising_detector_too():
    """A capture folded into an earlier episode runs the same optional steps
    in their own savepoints."""
    decisions = _DecisionsCursor(evidence=True, links=True)
    cur = _CaptureCursor(decisions)
    cur.episodes[1] = {
        "ts": "2026-09-19T00:00:00Z", "session_id": "s1", "summary": "Earlier work on the widget service.",
        "topics": list(TOPICS), "decisions": [], "preferences": [], "people": [], "raw": {},
        "harness": None, "repo_root": None, "project": "acme/widget", "parent_session_id": None,
        "transcript_range": None, "tags": [],
    }
    conn = _FakeConn(cur)
    boom = mock.patch("khipu.decisions.detect_reversals_from_episode", side_effect=RuntimeError("boom"))
    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch("khipu.topic_graph.persist_capture_graph",
                       return_value={"nodes_minted": 0, "edges_minted": 0}), \
            mock.patch("khipu.config.path_setting", return_value=None), \
            mock.patch("khipu.hygiene.classify_topics", return_value=(list(TOPICS), [], False)), \
            mock.patch.object(cap, "_reembed_merged_episode"), boom:
        cap._merge_into_episode(cur, 1, _payload([REVERSAL]), matched_via="jaccard", score=0.9)
    statements = [s for s, _ in cur.calls]
    assert "ROLLBACK TO SAVEPOINT capture_merge_decision_detection" in statements
    assert sorted(r["text"] for r in decisions.rows.values() if r["episode_id"] == 1) == sorted(DECISIONS)
