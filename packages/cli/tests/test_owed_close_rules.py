"""Auto-close by pull request and by decision (scope
docs/plans/2026-10-16-owed-and-ledger-ssot.md, design (b), slice 2, with
Matt's decision 1: Khipu asks GitHub read-only through ``gh pr view``).

An open item closes when every pull request it names is merged or closed,
with the reason, ``closed_by`` "Khipu, by rule" and the rule's key. A
question settles when a later decision answers it strictly; a looser match
is only a "Looks settled by decision N" suggestion. A reopened item is never
closed again by the same rule and evidence, and a hand close records its
reason and author.

Everything runs on the throwaway PostgreSQL of tests/hermetic_pg.py. GitHub
is a fake ``gh`` script on PATH; the session-wide shim in conftest.py fails
every other call, so no test reaches the real GitHub.
"""
from __future__ import annotations

import itertools
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from khipu import close_rules, commitments as co, config, work
from tests.hermetic_pg import hub, migration_sql, pg_cluster  # noqa: F401
from tests.test_commitments import _no_cosine

PROJECT = "acme/widget"
BASE = {"project": PROJECT, "session_id": "conv-1"}


@pytest.fixture(autouse=True)
def _aliases(monkeypatch):
    monkeypatch.setenv("KHIPU_USER_ALIASES", "matt")
    co.reset_user_patterns()
    yield
    co.reset_user_patterns()


@pytest.fixture(autouse=True)
def _no_runner(monkeypatch):
    """No registered job runner unless a test registers one."""
    monkeypatch.setattr(work, "settings", lambda config=None: _work_cfg([]))


_REAL_SETTINGS = work.settings


def _work_cfg(projects):
    out = _REAL_SETTINGS({})
    out["work_projects"] = projects
    return out


def _migrated(hub):  # noqa: F811
    from khipu import db

    conn = hub.connect()
    hub.migrate(conn=conn)
    db._TABLE_COLUMNS_CACHE.clear()
    return conn


_EPISODES = itertools.count(1)


def _episode(cur, *, summary=None, raw=None) -> int:
    summary = summary or f"episode {next(_EPISODES)}"
    cur.execute("INSERT INTO episodes (ts, summary, raw, project) VALUES (now(), %s, %s::jsonb, %s) "
                "RETURNING id", (summary, json.dumps(raw or {}), PROJECT))
    return int(cur.fetchone()[0])


def _open(cur, text, kind="followup", payload=None) -> int:
    ep = _episode(cur)
    with mock.patch.object(co, "_cosine_scores", _no_cosine), \
         mock.patch.object(co, "_has_commitment_embeddings", lambda cur, ids: False):
        assert co.open_from_episode(cur, dict(payload or BASE, open_loops=[{"text": text, "kind": kind}]), ep) == 1
    cur.execute("SELECT id FROM commitments WHERE opened_episode = %s", (ep,))
    return int(cur.fetchone()[0])


def _row(cur, cid) -> dict:
    cols = ("status", "close_reason", "closed_by", "close_rule_key", "prs", "reopened_rule_keys",
            "settle_suggestion", "pr_check", "close_evidence")
    cur.execute(f"SELECT {', '.join(cols)} FROM commitments WHERE id = %s", (cid,))
    return dict(zip(cols, cur.fetchone()))


def _runner(monkeypatch, tmp_path, prs):
    """Register a job runner for PROJECT whose summary lists ``prs``."""
    now = datetime.now(timezone.utc)
    z = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = []
    for number, stage in prs:
        item = {"id": str(number), "project": "widget", "title": f"Change {number}", "stage": stage,
                "stage_at": z}
        if stage == "merged":
            item["merged_at"] = z
        items.append(item)
    doc = {"format": "khipu-work-summary", "version": 1, "written_at": z,
           "project": {"id": "widget", "name": "Widget"}, "runner": {"name": "lanes", "interval_s": 20},
           "jobs": [], "prs": items, "trains": []}
    path = tmp_path / "runner" / "work-summary.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(work, "settings", lambda config=None: _work_cfg(
        [{"project": PROJECT, "name": "Widget", "file": str(path), "on": True, "show_now": True}]))


def _fake_gh(monkeypatch, tmp_path, answers: dict[int, str]):
    """A fake ``gh`` first on PATH. It logs its argv, one call per line, and
    answers ``gh pr view N`` with the state in ``answers`` (OPEN when not
    listed)."""
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    log = tmp_path / "gh-calls.log"
    cases = "".join(f'  {n}) echo \'{{"state":"{s}","mergedAt":null}}\';;\n' for n, s in answers.items())
    script = bindir / "gh"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\ncase "$3" in\n{cases}'
                      '  *) echo \'{"state":"OPEN","mergedAt":null}\';;\nesac\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("KHIPU_GH_CLI", str(script))
    return log


def _calls(log) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# ---- PR numbers -------------------------------------------------------------

def test_pr_numbers_are_parsed_from_the_shapes_items_use():
    assert close_rules.parse_prs("Land PR #12 and PR 13 before the release") == [12, 13]
    assert close_rules.parse_prs("Review pull request 366 once CI is green") == [366]
    assert close_rules.parse_prs("PRs #7, #8 and #9 need a rebase") == [7, 8, 9]
    assert close_rules.parse_prs("See https://github.com/acme/widget/pull/41 for the fix") == [41]
    assert close_rules.parse_prs("PR #234 author to renumber migration 115") == [234]
    assert close_rules.parse_prs("Fix issue #5 in the parser") == []


def test_a_new_item_stores_its_prs_and_the_backfill_fills_old_rows(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Implement the two fixes before PR #366 can merge", "blocker")
        assert _row(cur, cid)["prs"] == [366]
        none = _open(cur, "Write the release notes for the next version")
        assert _row(cur, none)["prs"] == []
        cur.execute("UPDATE commitments SET prs = NULL")
        assert close_rules.backfill_prs(cur) == 2
        assert _row(cur, cid)["prs"] == [366]
        assert _row(cur, none)["prs"] == []
        assert close_rules.backfill_prs(cur) == 0


# ---- the all-named-PRs-done rule --------------------------------------------

def test_one_merged_of_two_named_stays_open(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    _runner(monkeypatch, tmp_path, [(12, "merged"), (13, "fixing")])
    with conn.cursor() as cur:
        cid = _open(cur, "Land PR #12 and PR #13 before the release")
        report = close_rules.run(cur)
        row = _row(cur, cid)
        assert row["status"] == "open"
        assert report["closed_pr"] == 0
        assert row["pr_check"]["states"]["12"] == "merged (from the runner's summary)"
        assert "13" in row["pr_check"]["waiting"]


def test_both_merged_closes_with_the_reason(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    _runner(monkeypatch, tmp_path, [(12, "merged"), (13, "merged")])
    with conn.cursor() as cur:
        cid = _open(cur, "Land PR #12 and PR #13 before the release")
        assert close_rules.run(cur)["closed_pr"] == 1
        row = _row(cur, cid)
        assert row["status"] == "closed"
        assert row["close_reason"] == "pull requests #12 and #13 merged (from the runner's summary)"
        assert row["closed_by"] == "Khipu, by rule"
        assert row["close_rule_key"] == "pr:12=merged,13=merged"
        evidence = row["close_evidence"]["prs"]["13"]
        assert (evidence["state"], evidence["source"]) == ("merged", "runner") and evidence["read_at"]


def test_before_pr_merges_closes_on_the_merge_a_later_capture_reports(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Implement the two fixes before PR #366 can merge", "blocker")
        # A model's sentence is not evidence; GitHub is asked instead (the
        # test shim fails), so the item stays open.
        said = dict(BASE, decisions=["PR #366 merged after the second review"])
        assert close_rules.run(cur, payload=said, episode_id=_episode(cur, raw=said))["closed_pr"] == 0
        later = dict(BASE, closed_loops=["Merge pull request #366 from acme/fix-copy"])
        ep = _episode(cur, raw=later)
        assert close_rules.run(cur, payload=later, episode_id=ep)["closed_pr"] == 1
        row = _row(cur, cid)
        assert row["status"] == "closed"
        assert row["close_reason"] == "pull request #366 merged (from a later conversation)"


def test_will_merge_in_a_later_capture_does_not_count(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Implement the two fixes before PR #366 can merge", "blocker")
        later = dict(BASE, decisions=["We will merge #366 tomorrow", "Fix the copy before #366 merges"],
                     closed_loops=["PR #366 is not merged yet"], summary="Planned to merge PR #366.")
        ep = _episode(cur, summary=later["summary"], raw=later)
        close_rules.run(cur, payload=later, episode_id=ep)
        # The nightly reads the same capture from the episodes table.
        close_rules.run(cur)
        assert _row(cur, cid)["status"] == "open"
    assert close_rules.capture_pr_states([
        "Merge pull request #366 from acme/fix-copy", "✓ Closed pull request acme/widget#367",
        "a1b2c3d Merge pull request #368 from acme/docs\n✓ Squashed and merged pull request #369 (Docs)",
    ]) == {366: "merged", 367: "closed", 368: "merged", 369: "merged"}
    assert close_rules.capture_pr_states(["will merge #366", "before #366 merges", "PR #366 merged",
                                          "we have not merged PR #366", "PR #366 will be merged",
                                          "closed #367", "merged PR #368"]) == {}


def test_a_ledger_row_naming_the_pr_as_verified_closes_it(hub):  # noqa: F811
    from khipu import ledger

    conn = _migrated(hub)
    with conn.cursor() as cur:
        ledger.ensure_project(cur, PROJECT)
        cur.execute("INSERT INTO ledger_rows (project, row_id, title, kind, state, refs, seq) "
                    "VALUES (%s, 's1', 'Sign-in fix', 'slice', 'VERIFIED', %s::jsonb, 1)",
                    (PROJECT, json.dumps({"pr": "41"})))
        cid = _open(cur, "Check PR #41 against the staging build")
        assert close_rules.run(cur)["closed_pr"] == 1
        assert _row(cur, cid)["close_reason"] == "pull request #41 merged (from the ledger)"


def test_a_reopened_item_is_not_closed_again_by_the_same_evidence(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    _runner(monkeypatch, tmp_path, [(12, "merged")])
    with conn.cursor() as cur:
        cid = _open(cur, "Follow up on PR #12 with the docs change")
        assert close_rules.run(cur)["closed_pr"] == 1
        assert co.set_status(cur, cid, "open", by=co.BY_APP)
        row = _row(cur, cid)
        assert row["status"] == "open"
        assert row["close_reason"] is None and row["closed_by"] is None and row["close_rule_key"] is None
        assert row["reopened_rule_keys"] == ["pr:12=merged"]
        report = close_rules.run(cur)
        assert report["closed_pr"] == 0 and report["kept_open_reopened"] == 1
        assert _row(cur, cid)["status"] == "open"


def test_a_hand_close_records_reason_and_author(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Write the release notes for the next version")
        assert co.set_status(cur, cid, "closed", reason="Done in the release branch", by=co.BY_APP)
        row = _row(cur, cid)
        assert (row["status"], row["close_reason"], row["closed_by"]) == (
            "closed", "Done in the release branch", "you, in the app")
        other = _open(cur, "Draft the migration guide for the next version")
        assert co.set_status(cur, other, "closed", by=co.BY_CONVERSATION)
        row = _row(cur, other)
        assert (row["close_reason"], row["closed_by"]) == ("closed by hand", "a conversation")


def test_the_mcp_and_cli_writes_say_who_closed(monkeypatch):
    from khipu import mcp_server, owner_writes

    seen = []
    monkeypatch.setattr(owner_writes, "run", lambda tool, args, **kw: seen.append(args) or {"ok": True})
    mcp_server._tool_owed_update({"id": 3, "action": "close", "reason": "answered", "by": "you, in the app"})
    assert seen[-1]["by"] == "a conversation" and seen[-1]["reason"] == "answered"


# ---- GitHub (decision 1) ----------------------------------------------------

def test_github_fake_gh_merged_closes_and_asks_once(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    log = _fake_gh(monkeypatch, tmp_path, {41: "MERGED"})
    with conn.cursor() as cur:
        a = _open(cur, "Check PR #41 against the staging build")
        b = _open(cur, "Tell the reviewer that PR #41 needs a second look")
        report = close_rules.run(cur)
        assert report["closed_pr"] == 2
        assert _row(cur, a)["close_reason"] == "pull request #41 merged (from GitHub)"
        assert _row(cur, b)["status"] == "closed"
    assert _calls(log) == ["pr view 41 --repo acme/widget --json state,mergedAt"]


def test_gh_missing_leaves_the_item_open_with_the_reason(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("KHIPU_GH_CLI", str(empty / "gh"))  # named, but not there
    with conn.cursor() as cur:
        cid = _open(cur, "Check PR #41 against the staging build")
        report = close_rules.run(cur)
        row = _row(cur, cid)
        assert row["status"] == "open"
        assert "gh is not installed" in row["pr_check"]["waiting"]["41"]
        assert "gh is not installed" in report["github"]


def test_a_failing_gh_leaves_the_item_open_with_the_reason(hub):  # noqa: F811
    conn = _migrated(hub)  # conftest's shim gh fails every call
    with conn.cursor() as cur:
        cid = _open(cur, "Check PR #41 against the staging build")
        close_rules.run(cur)
        row = _row(cur, cid)
        assert row["status"] == "open"
        assert row["pr_check"]["waiting"]["41"].startswith("GitHub could not say")


def test_setting_off_means_gh_is_never_run(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    log = _fake_gh(monkeypatch, tmp_path, {41: "MERGED"})
    real = config.load_config
    monkeypatch.setattr(config, "load_config", lambda: {**real(), "owed_pr_check_github": "off"})
    with conn.cursor() as cur:
        cid = _open(cur, "Check PR #41 against the staging build")
        close_rules.run(cur)
        row = _row(cur, cid)
        assert row["status"] == "open"
        assert "turned off" in row["pr_check"]["waiting"]["41"]
    assert _calls(log) == []


def test_a_project_with_no_known_repo_skips_github(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    log = _fake_gh(monkeypatch, tmp_path, {41: "MERGED"})
    with conn.cursor() as cur:
        cid = _open(cur, "Check PR #41 against the staging build", payload={"project": "widget",
                                                                            "session_id": "c"})
        close_rules.run(cur)
        assert "repository is not known" in _row(cur, cid)["pr_check"]["waiting"]["41"]
    assert _calls(log) == []


# ---- settled by a decision --------------------------------------------------

def _decide(cur, text) -> int:
    ep = _episode(cur)
    cur.execute("INSERT INTO decisions (project, text, episode_id) VALUES (%s, %s, %s) RETURNING id",
                (PROJECT, text, ep))
    return int(cur.fetchone()[0])


def test_a_decision_strict_match_closes_with_the_decision_id_in_the_reason(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        named = _open(cur, "Should the nightly keep the hourly backup window?", "question")
        same = _open(cur, "Decide whether the gateway keeps the legacy route", "question")
        by_id = _decide(cur, f"Settles commitment {named}: keep the hourly window")
        by_text = _decide(cur, "Decide whether the gateway keeps the legacy route")
        report = close_rules.run(cur)
        assert report["closed_decision"] == 2
        row = _row(cur, named)
        assert row["status"] == "closed" and f"decision {by_id}" in row["close_reason"]
        assert row["close_rule_key"] == f"decision:{by_id}" and row["closed_by"] == "Khipu, by rule"
        assert f"decision {by_text}" in _row(cur, same)["close_reason"]


def test_a_loose_decision_match_only_suggests(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Should the release ship with the seal fix or wait?", "question")
        did = _decide(cur, "Ship the release with the seal fix")
        report = close_rules.run(cur)
        assert report["closed_decision"] == 0 and report["suggested"] == 1
        row = _row(cur, cid)
        assert row["status"] == "open"
        assert row["settle_suggestion"]["decision_id"] == did
        listed = co.list_owed(cur, project=PROJECT, limit=None)
        assert listed[0]["looks_settled"] == f"Looks settled by decision {did}"


# ---- a hub without migration 0034 -------------------------------------------

def test_a_pre_migration_hub_still_answers(hub):  # noqa: F811
    from khipu import db

    conn = hub.connect()
    with conn.cursor() as cur:
        assert not db.has_columns(cur, "commitments", "prs")
        cid = _open(cur, "Implement the two fixes before PR #366 can merge", "blocker")
        assert close_rules.run(cur) == {"ok": True, "skipped": "the hub has no close-rule columns yet "
                                                              "(migration 0034)"}
        assert co.set_status(cur, cid, "closed", reason="done", by=co.BY_APP)
        assert co.set_status(cur, cid, "open")
        assert len(co.list_owed(cur, project=PROJECT, limit=None)) == 1
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    hub.migrate(conn=conn)
    with conn.cursor() as cur:
        cur.execute(migration_sql("0034_commitment_close_rules.sql"))  # safe to run twice
        db._TABLE_COLUMNS_CACHE.clear()
        assert close_rules.backfill_prs(cur) == 1
    conn.commit()


def test_the_nightly_backfills_then_runs_the_rules(monkeypatch):
    from khipu import db, jobs

    calls = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn(Cur):
        def cursor(self):
            return Cur()

        def commit(self):
            calls.append("commit")

    monkeypatch.setattr(db, "connect", lambda **kw: Conn())
    monkeypatch.setattr(close_rules, "backfill_prs", lambda cur: calls.append("backfill") or 4)
    monkeypatch.setattr(close_rules, "run", lambda cur, **kw: calls.append("run") or {"ok": True, "closed_pr": 1})
    monkeypatch.setattr(jobs, "_nightly_log", lambda msg: None)
    out = jobs._commitment_close_rules()
    assert out["ok"] and out["backfilled"] == 4 and out["closed_pr"] == 1
    assert calls == ["backfill", "commit", "run", "commit"]


@pytest.mark.parametrize("from_app, by", [(True, "you, in the app"), (False, "a conversation")])
def test_khipu_owed_close_says_you_in_the_app_only_when_the_app_started_it(monkeypatch, capsys, from_app, by):
    import argparse

    from khipu import cli, owner_writes, routing

    seen = []
    monkeypatch.setattr(routing, "_caller", lambda: {"app": from_app, "chain": []})
    monkeypatch.setattr(owner_writes, "run", lambda tool, args, **kw: seen.append(args) or {"ok": True})
    args = argparse.Namespace(close="7", reopen=None, snooze=None, until=None, reason="shipped")
    assert cli.cmd_owed(args) == 0
    assert seen[-1]["by"] == by and seen[-1]["reason"] == "shipped"


def test_gh_is_found_in_the_install_folders_when_not_on_path(hub, monkeypatch, tmp_path):  # noqa: F811
    """launchd's bare PATH has no gh: the usual install folders are looked in
    after PATH, as for the other CLIs (synth_cli.binary)."""
    from khipu import synth_cli

    conn = _migrated(hub)
    log = _fake_gh(monkeypatch, tmp_path, {41: "MERGED"})
    fake = tmp_path / "fake-bin" / "gh"
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("KHIPU_GH_CLI", raising=False)
    monkeypatch.setitem(synth_cli._CANDIDATES, "gh", (str(tmp_path / "nope" / "gh"), str(fake)))
    with conn.cursor() as cur:
        cid = _open(cur, "Check PR #41 against the staging build")
        assert close_rules.run(cur)["closed_pr"] == 1
        assert _row(cur, cid)["status"] == "closed"
    assert _calls(log) == ["pr view 41 --repo acme/widget --json state,mergedAt"]


# ---- the review's probes: one evidence record per repository and number ----

def test_stale_runner_summary_is_not_close_evidence(monkeypatch):
    """A Work source marked stale must not auto-close from cached contents."""
    monkeypatch.setattr(work, "settings", lambda: {"work_projects": [{"project": "acme/widget"}]})
    monkeypatch.setattr(
        work,
        "read_source",
        lambda entry, cfg, now: {"state": "stale", "contents": {"prs": [{"id": "41", "stage": "closed"}]}},
    )
    assert close_rules._runner_states("acme/widget") == {}


def test_two_linked_repositories_with_same_pr_number_do_not_collapse(monkeypatch):
    """Every named PR must be independently evidenced, even when numbers collide."""
    class Github:
        def state(self, repo, number):
            # Only the second repository's PR is done.
            return ("merged", None) if repo == "second/repo" and number == 41 else ("open", None)

    monkeypatch.setattr(close_rules, "_runner_states", lambda project: {})
    monkeypatch.setattr(close_rules, "_ledger_states", lambda cur, project: {})
    monkeypatch.setattr(close_rules, "_episode_states", lambda cur, project, after: {})
    text = (
        "Follow https://github.com/first/repo/pull/41 and "
        "https://github.com/second/repo/pull/41 before release"
    )
    item = {
        "id": 1, "text": text, "project": "not-a-repository", "kind": "blocker",
        "opened_episode": 1, "prs": close_rules.parse_prs(text), "reopened": [], "pr_check": {},
    }
    report = {"closed_pr": 0, "kept_open_reopened": 0}
    planned = {}
    close_rules._pr_rule(None, [item], report, payload=None, episode_id=None, github=Github(), sink=planned)
    assert planned == {}
    assert report["closed_pr"] == 0


def test_question_about_merge_is_not_capture_close_evidence():
    """Unanswered status questions must not be read as a past-tense fact."""
    assert close_rules.capture_pr_states(["Was PR #366 merged?"]) == {}


def test_a_question_is_never_evidence_on_any_close_path():
    assert close_rules.evidence_problem("Was PR #366 merged?", "Land PR #366") == "a question is not evidence"
    assert close_rules.evidence_problem("Did we rebuild the deployment index", "Rebuild the deployment index")
    assert close_rules.evidence_problem("PR #366 merged", "Land PR #366")
    assert close_rules.evidence_problem("Merge pull request #366 from acme/x", "Land PR #366") is None
    assert close_rules.evidence_problem("Rebuilt the deployment index", "Rebuild the deployment index") is None


def test_a_reopened_pr_revokes_the_earlier_merge_line(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Implement the two fixes before PR #366 can merge", "blocker")
        _episode(cur, raw=dict(BASE, closed_loops=["Merge pull request #366 from acme/fix-copy"]))
        _episode(cur, raw=dict(BASE, closed_loops=["✓ Reopened pull request acme/widget#366"]))
        assert close_rules.run(cur)["closed_pr"] == 0
        row = _row(cur, cid)
        assert row["status"] == "open"
        assert row["pr_check"]["waiting"]["366"].startswith("still open (from a later conversation)")


def test_a_runner_reading_newer_than_a_merge_line_wins(hub, monkeypatch, tmp_path):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Follow up on PR #12 with the docs change")
        cur.execute("INSERT INTO episodes (ts, summary, raw, project) VALUES (now() - interval '1 day', "
                    "'Merge pull request #12 from acme/docs', '{}'::jsonb, %s)", (PROJECT,))
        _runner(monkeypatch, tmp_path, [(12, "fixing")])
        assert close_rules.run(cur)["closed_pr"] == 0
        assert _row(cur, cid)["status"] == "open"


# ---- every close path: one guarded write, one evidence check ----------------

def _episode_in(cur, session_id) -> int:
    cur.execute("INSERT INTO episodes (ts, summary, raw, project, session_id) VALUES (now(), %s, '{}'::jsonb, "
                "%s, %s) RETURNING id", (f"episode {next(_EPISODES)}", PROJECT, session_id))
    return int(cur.fetchone()[0])


def test_reopened_legacy_auto_close_does_not_reclose_on_same_evidence(hub):  # noqa: F811
    """The plan's reopen guarantee applies to existing closed-loop matching."""
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Rebuild the deployment index")
        payload = dict(BASE, closed_loops=[{"text": "done: Rebuild the deployment index", "commitment_id": cid}])
        with mock.patch.object(co, "_has_commitment_embeddings", return_value=False):
            assert co.auto_close(cur, payload, episode_id=_episode(cur)) == 1
            assert co.set_status(cur, cid, "open")
            assert co.auto_close(cur, payload, episode_id=_episode(cur)) == 0
        assert _row(cur, cid)["status"] == "open"


def test_a_question_closed_loop_does_not_close_by_text(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Land PR #366 after the second review")
        payload = dict(BASE, closed_loops=["done: Land PR #366 after the second review?",
                                           "done: PR #366 merged after the second review"])
        with mock.patch.object(co, "_has_commitment_embeddings", return_value=False):
            assert co.auto_close(cur, payload, episode_id=_episode(cur)) == 0
        assert _row(cur, cid)["status"] == "open"


def test_session_end_close_records_rule_author_after_0034(hub):  # noqa: F811
    """The older session-end automatic close must remain auditable too, and
    is not repeated on an item the owner reopened."""
    conn = _migrated(hub)
    with conn.cursor() as cur:
        ep = _episode_in(cur, "codex:s")
        cur.execute("INSERT INTO commitments (text, project, owner, kind, opened_episode, status, content_hash) "
                    "VALUES ('Rebuild the deployment index', %s, 'assistant', 'followup', %s, 'open', 'h-s') "
                    "RETURNING id", (PROJECT, ep))
        cid = int(cur.fetchone()[0])
        end = {"session_id": "codex:s", "event": "sessionend", "project": PROJECT}
        assert co.close_session_plan(cur, end, episode_id=_episode_in(cur, "codex:s")) == 1
        row = _row(cur, cid)
        assert row["closed_by"] == co.BY_RULE and row["close_rule_key"] == "session-ended:codex:s"
        assert co.set_status(cur, cid, "open")
        assert co.close_session_plan(cur, end, episode_id=_episode_in(cur, "codex:s")) == 0


def _functions_that(pattern, *, calls=None):
    """``{(module, function)}`` in the khipu package whose own body writes
    ``pattern`` (or, with ``calls``, calls that name)."""
    import ast
    import re as _re
    from pathlib import Path

    found = set()
    for path in sorted((Path(co.__file__).parent).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            inner = {id(n) for sub in ast.walk(fn) if sub is not fn
                     and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(sub)}
            for node in ast.walk(fn):
                if id(node) in inner:
                    continue
                if calls is not None and isinstance(node, ast.Call):
                    f = node.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if name == calls:
                        found.add((path.stem, fn.name))
                if calls is None and isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and _re.search(pattern, node.value):
                    found.add((path.stem, fn.name))
    return found


def test_every_close_path_goes_through_the_one_guarded_close():
    """Every write that closes an item is ``close_by_rule`` (the reopen guard
    and the rule's author), a hand close (``set_status``), or the forget of an
    episode. A new close path fails here until it uses ``close_by_rule``."""
    writes = _functions_that(r"SET\s+status\s*=\s*'closed'")
    assert writes == {("commitments", "close_by_rule"), ("forget", "forget_episode")}, writes
    callers = _functions_that(None, calls="close_by_rule")
    assert {("close_rules", "_close"), ("commitments", "auto_close"), ("commitments", "close_session_plan"),
            ("commitments", "open_from_episode"), ("owed_tidy", "apply")} <= callers, callers
    assert {m for m, _ in callers} >= {"hygiene"}, callers


# ---- the second cross-family review: every named PR, each in its repository ----


def test_bare_project_pr_and_other_repo_link_same_number_are_both_required(monkeypatch):
    """The review's probe: "PR #41" is this project's, the link is another
    repository's; the open first/repo#41 keeps the item open."""
    class Github:
        def state(self, repo, number):
            return ("merged", None) if repo == "second/repo" else ("open", None)

    monkeypatch.setattr(close_rules, "_runner_states", lambda project: {})
    monkeypatch.setattr(close_rules, "_ledger_states", lambda cur, project: {})
    monkeypatch.setattr(close_rules, "_episode_states", lambda cur, project, after: {})
    text = "Land PR #41 in this project and https://github.com/second/repo/pull/41 before release"
    assert close_rules.pr_refs(text, "first/repo") == [("first/repo", 41), ("second/repo", 41)]
    item = dict(id=1, text=text, project="first/repo", kind="blocker", opened_episode=1,
                prs=close_rules.parse_prs(text), reopened=[], pr_check={})
    report = {"closed_pr": 0, "kept_open_reopened": 0}
    planned: dict = {}
    close_rules._pr_rule(None, [item], report, payload=None, episode_id=None, github=Github(), sink=planned)
    assert planned == {}, "The open first/repo#41 must prevent closing this two-repository item"


@pytest.mark.parametrize("text,evidence", [
    ("Land PR #12 and PR #13 before the release", "Merge pull request #12 from acme/feature"),
    ("Land PR #366 before the release", "Reopened pull request #366"),
    ("Land PR #366 before the release", "Landed the release work for PR #366"),
    ("Land PR #12 and https://github.com/other/repo/pull/12", "Merge pull request #12 from acme/feature"),
])
def test_loop_close_cannot_override_pr_state_requirements(monkeypatch, text, evidence):
    """The review's probe and its kin: a legacy closed loop goes through the
    same every-PR check."""
    class Cursor:
        rowcount = 1

        def execute(self, sql, params=()):
            pass

    monkeypatch.setattr(co, "_open_commitments", lambda *args, **kwargs: [{"id": 7, "text": text}])
    monkeypatch.setattr(co, "_close_columns_ready", lambda cur: True)
    monkeypatch.setattr(co, "_not_forgotten_clause", lambda cur: "TRUE")
    monkeypatch.setattr(co, "_has_commitment_embeddings", lambda *args: False)
    monkeypatch.setattr("khipu.config.float_setting", lambda key: 0.8)
    payload = {"project": "acme/widget", "closed_loops": [{"text": evidence, "commitment_id": 7}]}
    assert co.auto_close(Cursor(), payload, episode_id=19) == 0


def test_a_loop_close_passes_when_every_named_pr_is_shown_finished():
    """The mirror: git's merge line for #12 and gh's close line for #13."""
    text = "Land PR #12 and PR #13 before the release"
    evidence = "Merge pull request #12 from acme/feature\n✓ Closed pull request #13"
    assert close_rules.evidence_problem(evidence, text, PROJECT) is None
    # The newest line about a PR wins: a reopen after the merge blocks.
    assert close_rules.evidence_problem(evidence + "\n✓ Reopened pull request #12", text, PROJECT)
    # An item naming no PR is not held by this check.
    assert close_rules.evidence_problem("Finished the release notes", "Write the release notes") is None


# ---- the third cross-family review's probe: a long PR list is read whole ----

def _round3_empty_sources(monkeypatch):
    monkeypatch.setattr(close_rules, "_runner_states", lambda project: {})
    monkeypatch.setattr(close_rules, "_ledger_states", lambda cur, project: {})
    monkeypatch.setattr(close_rules, "_episode_states", lambda cur, project, after: {})


def test_long_pr_list_does_not_close_when_its_unparsed_tail_is_open(monkeypatch):
    """The parser cannot silently omit the later members of a PR list.

    Omitting them turns an all-PRs-done check into an early close when the
    retained prefix is merged but a later listed PR is still open.
    """
    _round3_empty_sources(monkeypatch)
    numbers = list(range(1, 31))
    text = "Land PR " + ", ".join(f"#{number}" for number in numbers) + " before release"
    asked = []

    class Github:
        def state(self, repo, number):
            asked.append(number)
            return ("open", None) if number == 30 else ("merged", None)

    item = {
        "id": 1,
        "text": text,
        "project": "acme/widget",
        "kind": "blocker",
        "opened_episode": 1,
        "prs": close_rules.parse_prs(text),
        "reopened": [],
        "pr_check": {},
    }
    planned = {}
    report = {"closed_pr": 0, "kept_open_reopened": 0}
    close_rules._pr_rule(None, [item], report, payload=None, episode_id=None, github=Github(), sink=planned)

    assert planned == {}, f"An unfinished #30 was omitted and the item was planned closed: {planned}"
    assert asked == numbers
