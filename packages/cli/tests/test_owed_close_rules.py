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
        assert row["close_evidence"]["prs"]["13"] == {"state": "merged", "source": "runner"}


def test_before_pr_merges_closes_on_the_merge_a_later_capture_reports(hub):  # noqa: F811
    conn = _migrated(hub)
    with conn.cursor() as cur:
        cid = _open(cur, "Implement the two fixes before PR #366 can merge", "blocker")
        later = dict(BASE, decisions=["PR #366 merged after the second review"])
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
    assert close_rules.capture_pr_states(["PR #366 merged", "closed #367", "merged PR #368"]) == {
        366: "merged", 367: "closed", 368: "merged"}
    assert close_rules.capture_pr_states(["will merge #366", "before #366 merges",
                                          "we have not merged PR #366", "PR #366 will be merged"]) == {}


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
