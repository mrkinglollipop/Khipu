"""Suggest, don't close (Matt, 2026-10-17; docs/plans/2026-10-16-owed-and-ledger-ssot.md).

The owner's own items never change automatically: what a rule would have
closed lands in "Looks done" with its reason and evidence, and he closes it.
Job and orchestrator items keep closing by rule. Also the round 5 probes that
still describe a real risk under this design: the conversation's origin
standing in for his "done", the PR union on the closed-loop path and across
restatements, the clean-up preview's fingerprint, and quiet items repeating
across pages.

Everything runs on the throwaway PostgreSQL of tests/hermetic_pg.py; GitHub
is a fake ``gh`` (tests/test_owed_close_rules.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest import mock

import pytest

from khipu import close_rules, commitments as co, hygiene, owed_tidy, routing, work
from tests.hermetic_pg import hub, migration_sql, pg_cluster  # noqa: F401
from tests import fact_seed
from tests.test_commitments import _no_cosine
from tests.test_owed_close_rules import (
    BASE, JOB, PROJECT, _REAL_SETTINGS, _episode, _fake_gh, _migrated, _open, _row, _runner,
)


@pytest.fixture(autouse=True)
def _aliases(monkeypatch):
    monkeypatch.setenv("KHIPU_USER_ALIASES", "matt")
    co.reset_user_patterns()
    yield
    co.reset_user_patterns()


@pytest.fixture(autouse=True)
def _no_runner(monkeypatch):
    def settings(config=None):
        out = _REAL_SETTINGS({})
        out["work_projects"] = []
        return out

    monkeypatch.setattr(work, "settings", settings)


def _capture(cur, payload) -> int:
    ep = _episode(cur)
    with mock.patch.object(co, "_cosine_scores", _no_cosine), \
            mock.patch.object(co, "_has_commitment_embeddings", lambda cur, ids: False):
        return co.auto_close(cur, payload, ep)


def _listed(cur) -> dict[int, dict]:
    return {r["id"]: r for r in co.list_owed(cur, project=PROJECT, limit=None)}


# ---- the PR rule ------------------------------------------------------------


def test_the_owners_item_with_every_pr_merged_looks_done_and_a_jobs_closes(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    _runner(monkeypatch, tmp_path, [(41, "merged"), (42, "merged")])
    with conn.cursor() as cur:
        mine = _open(cur, "Matt to validate the changes in PR #41 before the release")
        theirs = _open(cur, "Land PR #42 before the release", payload=JOB)
        report = close_rules.run(cur)
        assert report["closed_pr"] == 1
        assert _row(cur, theirs)["status"] == "closed"
        row = _row(cur, mine)
        assert row["status"] == "open" and row["closed_by"] is None
        assert row["settle_suggestion"]["looks_done"] and row["settle_suggestion"]["rule"] == "pr"
        listed = _listed(cur)[mine]
        assert listed["group"] == "Looks done"
        assert "#41 merged" in listed["looks_done"]["reason"]
        assert listed["looks_done"]["evidence"]["prs"]["41"]["state"] == "merged"
        # The nightly again: the same suggestion, nothing closed.
        close_rules.run(cur)
        assert _row(cur, mine)["status"] == "open"


def test_a_pr_suggestion_is_withdrawn_when_a_restatement_adds_an_open_pr(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    _runner(monkeypatch, tmp_path, [(41, "merged")])
    _fake_gh(monkeypatch, tmp_path, {})
    with conn.cursor() as cur:
        mine = _open(cur, "Matt to validate the changes in PR #41 before the release")
        close_rules.run(cur)
        assert _row(cur, mine)["settle_suggestion"]["looks_done"]
        cur.execute("UPDATE commitments SET prs = '{41,43}' WHERE id = %s", (mine,))
        close_rules.run(cur)
        assert _row(cur, mine)["settle_suggestion"] is None
        assert _listed(cur)[mine]["group"] is None


def test_closing_from_looks_done_then_reopening_never_suggests_it_again(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    _runner(monkeypatch, tmp_path, [(41, "merged")])
    with conn.cursor() as cur:
        mine = _open(cur, "Matt to validate the changes in PR #41 before the release")
        close_rules.run(cur)
        reason = _listed(cur)[mine]["looks_done"]["reason"]
        assert co.set_status(cur, mine, "closed", reason=reason, by=co.BY_APP)
        assert _row(cur, mine)["close_reason"] == reason
        assert co.set_status(cur, mine, "open", by=co.BY_APP)
        row = _row(cur, mine)
        assert row["settle_suggestion"] is None and row["reopened_rule_keys"] == ["pr:41=merged"]
        close_rules.run(cur)
        assert _row(cur, mine)["settle_suggestion"] is None
        assert _row(cur, mine)["status"] == "open"


# ---- the closed-loop rule (round 5: conversation origin is not his "done") -------


def test_a_done_line_in_a_conversation_only_suggests_on_the_owners_item(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        mine = _open(cur, "Matt to sign the developer agreement before the release")
        assert co.capture_origin(BASE) == co.ORIGIN_CONVERSATION
        assert _capture(cur, dict(BASE, closed_loops=["done: sign the developer agreement before the release"])) == 0
        row = _row(cur, mine)
        assert row["status"] == "open" and row["settle_suggestion"]["rule"] == "loop"
        assert _listed(cur)[mine]["group"] == "Looks done"


def test_a_jobs_item_still_closes_by_a_closed_loop(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        theirs = _open(cur, "The builder will rebuild the deployment index", payload=JOB)
        assert _capture(cur, dict(JOB, closed_loops=["done: rebuild the deployment index"])) == 1
        assert _row(cur, theirs)["status"] == "closed"


def test_a_targeted_loop_checks_every_pr_the_item_was_ever_said_to_need(hub):  # noqa: F811
    """Round 5: the closed-loop path read only the first wording's PRs, so a
    git line for #12 closed an item a restatement had tied to #13 too."""
    conn = _migrated(hub)
    with conn.cursor() as cur:
        theirs = _open(cur, "The builder will land PR #12 before the release", payload=JOB)
        cur.execute("UPDATE commitments SET prs = '{12,13}', latest_text = %s WHERE id = %s",
                    ("The builder will land PR #12 and PR #13 before the release", theirs))
        loop = {"text": "Merge pull request #12 from acme/feature", "commitment_id": theirs}
        assert _capture(cur, dict(JOB, closed_loops=[loop])) == 0
        assert _row(cur, theirs)["status"] == "open"
        both = {"text": "Merge pull request #12 from acme/feature\nMerge pull request #13 from acme/next",
                "commitment_id": theirs}
        assert _capture(cur, dict(JOB, closed_loops=[both])) == 1


def test_an_in_between_restatements_other_repository_pr_is_kept(hub, monkeypatch, tmp_path):  # noqa: F811
    """Round 5: ``prs`` keeps numbers and ``latest_text`` only the newest
    wording, so another repository's PR an in-between wording named was lost
    and the item closed while it was still open."""
    conn = _migrated(hub)
    _runner(monkeypatch, tmp_path, [(41, "merged"), (42, "merged")])
    _fake_gh(monkeypatch, tmp_path, {})  # other/repo#41 reads OPEN
    original = ("The builder will land PR #41 in this project before release after the reviewer checks the "
                "updated migration and regression tests")
    with conn.cursor() as cur:
        theirs = _open(cur, original, payload=JOB)
        for wording in (original + " and https://github.com/other/repo/pull/41", original + " and PR #42"):
            ep = _episode(cur)
            with mock.patch.object(co, "_cosine_scores", _no_cosine), \
                    mock.patch.object(co, "_has_commitment_embeddings", lambda cur, ids: False):
                assert co.open_from_episode(cur, dict(JOB, open_loops=[{"text": wording}]), ep) == 0
        row = _row(cur, theirs)
        assert row["prs"] == [41, 42]
        assert row["pr_check"]["refs"] == ["acme/widget#41", "other/repo#41", "acme/widget#42"]
        close_rules.run(cur)
        row = _row(cur, theirs)
        assert row["status"] == "open"
        assert "other/repo#41" in row["pr_check"]["waiting"]
        assert row["pr_check"]["refs"] == ["acme/widget#41", "other/repo#41", "acme/widget#42"]


# ---- the clean-up preview (round 5: its fingerprint) --------------------------


def test_a_tidy_plan_made_before_a_restatement_added_a_pr_does_not_close(hub, monkeypatch, tmp_path):  # noqa: F811
    from khipu import db

    conn = _migrated(hub)
    conn.commit()
    monkeypatch.setattr(db, "connect", lambda **kw: hub.connect())
    _runner(monkeypatch, tmp_path, [(12, "merged")])
    with conn.cursor() as cur:
        theirs = _open(cur, "The builder will land PR #12 before the release", payload=JOB)
    conn.commit()
    with conn.cursor() as cur:
        plan = owed_tidy.plan(cur, PROJECT, use_model=False)
    item = next(it for it in plan["items"] if it["id"] == theirs)
    assert item["action"] == "close" and item["close"] == "pr"
    with conn.cursor() as cur:
        # The production restatement writes.
        cur.execute("UPDATE commitments SET prs = '{12,13}', latest_text = %s WHERE id = %s",
                    ("The builder will land PR #12 and PR #13 before the release", theirs))
        fact_seed.adopt(cur, "commitment", theirs)
    conn.commit()
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": ["Khipu"]})
    result = owed_tidy.apply(plan)
    assert result["changed"] == 0
    assert {s["id"]: s["why"] for s in result["skipped"]}[theirs] == "changed after the plan was made"
    with conn.cursor() as cur:
        assert _row(cur, theirs)["status"] == "open"


def test_the_fingerprint_covers_the_restatement_fields():
    row = {f: None for f in owed_tidy.FINGERPRINT_FIELDS}
    base = owed_tidy.fingerprint(row)
    for change in ({"prs": [12, 13]}, {"latest_text": "newer words"}, {"pr_check": {"refs": ["o/r#1"]}}):
        assert owed_tidy.fingerprint({**row, **change}) != base, change
    # The nightly's status words in pr_check are not the item's state.
    assert owed_tidy.fingerprint({**row, "pr_check": {"waiting": {"12": "still open"}}}) == base


# ---- every automatic path against the owner's own item --------------------------


def test_no_automatic_path_changes_the_owners_item(hub, monkeypatch, tmp_path):  # noqa: F811
    """Each automatic path Khipu runs, one after another, over items the
    owner owns: they all stay open with their owner, and what would have
    closed one is a "Looks done" suggestion."""
    conn = _migrated(hub)
    _runner(monkeypatch, tmp_path, [(5, "merged")])
    with conn.cursor() as cur:
        texts = ["Matt to approve the release notes for PR #5",
                 "Matt to decide whether the export includes archived rows",
                 "Matt to review the wording of the archive notice",
                 "Matt to reply to the reviewer about the seal fix"]
        ids = [_open(cur, t, "question" if "decide" in t else "followup") for t in texts]
        cur.execute("UPDATE commitments SET opened_at = now() - interval '90 days', "
                    "last_seen_at = now() - interval '90 days' WHERE id = ANY(%s)", (ids,))
        cur.execute("UPDATE episodes SET session_id = %s WHERE id IN "
                    "(SELECT opened_episode FROM commitments WHERE id = ANY(%s))", (BASE["session_id"], ids))

        ep = _episode(cur)
        cur.execute("INSERT INTO decisions (project, text, episode_id) VALUES (%s, %s, %s)",
                    (PROJECT, f"Settles commitment {ids[1]}: archived rows stay out of the export", ep))
        _capture(cur, dict(BASE, closed_loops=["done: reply to the reviewer about the seal fix"]))
        ended = dict(BASE, event="sessionend", closed_loops=["done: approve the release notes for PR #5"])
        co.close_session_plan(cur, ended, _episode(cur))
        co.mark_stale(cur)
        close_rules.run(cur)
        hygiene.run_session_ended_pass(cur, apply=True, project=PROJECT)
        hygiene.run_commitments_hygiene(cur, apply=True, project=PROJECT,
                                        judge=lambda batch: [{"keep": False, "reason": "not one"} for _ in batch])

        for cid in ids:
            row = _row(cur, cid)
            assert row["status"] == "open", (cid, row)
            assert row["closed_by"] is None and row["close_reason"] is None
        cur.execute("SELECT id, owner, owner_reassigned_at FROM commitments WHERE id = ANY(%s)", (ids,))
        assert all(owner == "user" and moved is None for _, owner, moved in cur.fetchall())
        listed = _listed(cur)
        assert {cid for cid in ids if listed[cid]["group"] == "Looks done"} >= {ids[0], ids[1], ids[3]}
        assert all(listed[cid]["owner"] == co.OWNER_USER for cid in ids)


# ---- paging (round 5: quiet items repeating across pages) -------------------------


def test_an_item_turning_quiet_between_pages_neither_repeats_nor_goes_missing(monkeypatch):
    clock = [datetime(2026, 10, 17, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(co, "datetime", Clock)
    rows = [
        dict(id=1, text="I will review the export", owner="user", kind="followup",
             opened_at="2026-10-03T00:00:00Z", last_seen_at="2026-10-03T00:00:00Z"),
        dict(id=2, text="I will test the export", owner="user", kind="followup",
             opened_at="2026-10-02T00:00:00Z", last_seen_at="2026-10-03T00:00:00Z"),
        dict(id=3, text="I will approve the export", owner="user", kind="followup",
             opened_at="2026-09-01T00:00:00Z", last_seen_at="2026-09-01T00:00:00Z"),
    ]
    first = co.page_owed([dict(r) for r in rows], owner="user", limit=2)
    assert [r["id"] for r in first["results"]] == [1, 2]
    clock[0] = datetime(2026, 10, 17, 0, 0, 1, tzinfo=timezone.utc)  # item 2 turns quiet now
    second = co.page_owed([dict(r) for r in rows], owner="user", limit=10, cursor=first["next_cursor"])
    assert [r["id"] for r in second["results"]] == [3]


def test_the_looks_done_filter_and_count():
    rows = [
        dict(id=1, text="Matt to approve the release", owner="user", kind="followup",
             opened_at="2026-10-16T00:00:00Z", settle_suggestion={"looks_done": True, "rule": "pr",
                                                                   "reason": "pull request #5 merged",
                                                                   "key": "pr:5=merged", "evidence": {}}),
        dict(id=2, text="Matt to read the plan", owner="user", kind="followup",
             opened_at="2026-10-16T00:00:00Z"),
    ]
    page = co.page_owed([dict(r) for r in rows], looks_done=True)
    assert [r["id"] for r in page["results"]] == [1] and page["total"] == 1
    assert page["results"][0]["group"] == "Looks done"
    assert page["results"][0]["looks_done"] == {"rule": "pr", "reason": "pull request #5 merged", "evidence": {}}
    assert co.page_owed([dict(r) for r in rows])["counts"]["looks_done"] == 1
    assert json.dumps(page, default=str)
