"""Owed leads from the 2026-10-26 Grok triage (CONFIRMED premises)."""
from __future__ import annotations

from datetime import datetime, timezone

from khipu import close_rules as cr
from khipu import commitments as co


def test_issue_number_after_the_word_pr_is_not_required():
    assert cr.parse_prs("I will finish the PR review once issue #44 lands") == []


def test_shorter_promise_is_not_a_near_duplicate_of_a_longer_distinct_one():
    score = co._match_score("I will fix the parser", "I will fix the parser tests")
    assert score < co.NEAR_DUP_MIN_SCORE, f"containment scores {score}"


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
