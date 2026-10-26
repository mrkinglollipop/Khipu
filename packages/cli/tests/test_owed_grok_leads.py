"""Owed leads from the 2026-10-26 Grok triage (CONFIRMED premises)."""
from __future__ import annotations

from datetime import datetime, timezone

from khipu import close_rules as cr
from khipu import commitments as co
from khipu.capture import _jaccard


def test_issue_number_after_the_word_pr_is_not_required():
    assert cr.parse_prs("I will finish the PR review once issue #44 lands") == []


def test_shorter_promise_is_not_a_near_duplicate_of_a_longer_distinct_one():
    # Containment alone must not force 1.0 (the CONFIRMED lead). A short
    # phrase inside a longer distinct commitment stays under the near-dup bar.
    short = "I will fix the parser"
    near = "I will fix the parser tests"
    assert co._match_score(short, near) < 1.0
    longer = "I will fix the parser tests, rewrite capture, and ship docs"
    score = co._match_score(short, longer)
    assert score < co.NEAR_DUP_MIN_SCORE, f"containment scores {score}"


def test_asymmetric_containment_falls_back_to_jaccard_for_paraphrases():
    """P1 review: rejection branch must not return 0.0 and skip Jaccard."""
    a = "Matt to decide whether 0.3.17 ships with the seal fix"
    b = "Matt to decide whether 0.3.17 ships with the seal fix or waits"
    score = co._match_score(a, b)
    assert score == _jaccard(a, b)
    assert score >= co.NEAR_DUP_MIN_SCORE
    assert co._match_score("ship it", "ship it now") == _jaccard("ship it", "ship it now")


def test_pr_list_keeps_mixed_bare_and_hash_numbers():
    """P2 review: list run allows bare N, /, plus, and a leading '('."""
    assert cr.parse_prs("PRs 12, #13") == [12, 13]
    assert cr.parse_prs("PR #12, 13 and #14") == [12, 13, 14]
    assert cr.parse_prs("PR #12 (and #13)") == [12, 13]
    assert cr.parse_prs("PR #12 / #13") == [12, 13]
    assert cr.parse_prs("PR #12 plus #13") == [12, 13]


def test_a_decision_mentioning_item_12_does_not_strictly_settle_commitment_12():
    item = dict(
        id=12,
        text="I will decide the rollout window next session",
        project="p",
        opened_episode=1,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        owner="agent",
        kind="question",
    )
    decisions = [(5, "Ship item 12 of the release train", 2, datetime(2026, 10, 1, tzinfo=timezone.utc))]
    out = cr._settles(item, decisions, 0.8)
    assert not (out and out[0] == "strict"), out
