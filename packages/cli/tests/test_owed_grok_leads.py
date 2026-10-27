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


class _NoDb:
    """Cursor stand-in: any query fails, so the cosine fallback reads as
    "no embeddings" and only the token scores decide."""

    def execute(self, *a, **k):
        raise RuntimeError("no database in this test")

    def fetchone(self):
        return None


def _fold(new: str, text: str, latest: str | None) -> int | None:
    return co._near_duplicate_open(_NoDb(), "p", new, rows=[{"id": 7, "text": text, "latest_text": latest}])


def test_a_shared_pull_link_in_latest_text_does_not_fold_a_different_item():
    """Review of 1790d06b: scoring against latest_text let a pull URL's six
    boilerplate tokens carry a different item over the near-dup bar."""
    url12 = "https://github.com/acme/widget/pull/12"
    assert _fold(f"Deploy the gateway after {url12} merges",
                 "Review the parser fix", f"Review the parser fix {url12}") is None
    assert _fold("Land the docs https://github.com/acme/widget/pull/13",
                 "Land the fix", f"Land the fix {url12}") is None
    # The restatement that adds another repository's PR still folds.
    assert _fold(f"Merge the release fix {url12} and https://github.com/other/repo/pull/7",
                 "Merge the release fix", f"Merge the release fix {url12}") == 7


def test_rows_without_latest_text_score_as_before():
    for new, text in (("Review the parser fix now", "Review the parser fix"),
                      ("I will fix the parser tests", "I will fix the parser")):
        assert co._row_near_dup_score(new, {"text": text}) == co._match_score(new, text)
        assert co._row_near_dup_score(new, {"text": text, "latest_text": None}) == co._match_score(new, text)


def test_item_with_a_hash_still_names_the_commitment():
    """Merge with main: a decision "Resolved item #1" (the hash marks an id)
    still settles commitment 1; bare "item 12" stays prose."""
    names = lambda text: [int(m.group(1)) for m in cr._NAMES_ITEM_RE.finditer(text)]
    assert names("Resolved item #1") == [1]
    assert names("Answered question #7") == [7]
    assert names("Closed commitment 4 and owed item #5") == [4, 5]
    assert names("Ship item 12 of the release train") == []
    assert names("See https://example.test/board/item#5 for context") == []
    assert names("question 3 of the survey") == []
