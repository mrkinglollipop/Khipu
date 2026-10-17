"""Regression coverage for the four Work-review defects.

Every input is synthetic; the shared test harness redirects HOME and forbids
real Postgres before this module is collected.
"""
from __future__ import annotations

import threading

import pytest

from khipu import accounts, config, ledger, usage, work
from tests.test_forget_round12 import _forget, isolated as isolated
from tests.test_forget_write_gate import P
from tests.test_work import NOW, NO_LEDGER, ROUTING, cfg, commitment, ledger_data


def test_completed_forget_fences_inflight_work_writer(tmp_path, monkeypatch):
    """A refresh read before Forget must not restore its stale text later."""
    captured = commitment("c1", P["summary"], project=P["project"])
    view = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER,
                           owed={"items": [captured], "error": None}, routing_info=ROUTING)
    assert view["waiting"][0]["title"] == P["summary"]
    work.write_view(view)

    started, release = threading.Event(), threading.Event()
    real_write = work._write_json
    errors = []

    def paused_write(path, value, *args):
        started.set()
        assert release.wait(10)
        return real_write(path, value, *args)

    monkeypatch.setattr(work, "_write_json", paused_write)

    def stale_writer():
        try:
            work.write_view(view)
        except BaseException as exc:  # the writer must fail closed without crashing the refresh
            errors.append(exc)

    writer = threading.Thread(target=stale_writer)
    writer.start()
    assert started.wait(5)
    try:
        result = _forget(tmp_path, monkeypatch)
        assert result["complete"], result
        assert not work.view_file().exists()
    finally:
        release.set()
        writer.join(5)
    assert not errors, errors
    persisted = work.view_file().read_text() if work.view_file().exists() else ""
    assert P["summary"] not in persisted


def test_work_view_without_forget_keeps_its_text():
    view = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER,
                           owed={"items": [commitment("c1", P["summary"], project=P["project"])], "error": None},
                           routing_info=ROUTING)
    work.write_view(view)
    assert P["summary"] in work.view_file().read_text()


def test_completed_forget_before_a_stale_work_save_leaves_no_view(tmp_path, monkeypatch):
    view = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER,
                           owed={"items": [commitment("c1", P["summary"], project=P["project"])], "error": None},
                           routing_info=ROUTING)
    result = _forget(tmp_path, monkeypatch)
    assert result["complete"], result
    work.write_view(view)
    assert not work.view_file().exists()


def test_stale_plain_work_mapping_fails_closed_after_forget(tmp_path, monkeypatch):
    """Dropping a refresh's private generation must never reauthorize its text."""
    view = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER,
                           owed={"items": [commitment("c1", P["summary"], project=P["project"])], "error": None},
                           routing_info=ROUTING)
    stale = dict(view)
    result = _forget(tmp_path, monkeypatch)
    assert result["complete"], result
    work.write_view(stale)
    assert not work.view_file().exists()


def test_fresh_work_refresh_after_forget_keeps_unrelated_waiting_text(tmp_path, monkeypatch):
    result = _forget(tmp_path, monkeypatch)
    assert result["complete"], result
    view = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER,
                           owed={"items": [commitment("other", "Keep this unrelated item")], "error": None},
                           routing_info=ROUTING)
    work.write_view(view)
    saved = work.view_file().read_text()
    assert "Keep this unrelated item" in saved and P["summary"] not in saved


@pytest.mark.parametrize("source", ["registered", "fallback", "both"])
def test_hidden_project_never_supplies_a_work_row(source, tmp_path):
    """A hidden project cannot supply a board, live job, or either Waiting source."""
    from tests.test_work import NEEDS, entry, job, put, summary

    ledger = ledger_data([NEEDS])
    ledger["projects"]["atlas"]["hidden"] = True
    registered = [entry(put(tmp_path, summary(jobs=[job()])))] if source in ("registered", "both") else []
    owed = {"items": [
        commitment("hidden", "Never show hidden project work", project="atlas"),
        commitment("keep", "Keep projectless work", project=None),
    ], "error": None}
    routing = dict(ROUTING, rules=[{"pending": True, "n": 1}])
    view = work.build_view(NOW, cfg=cfg(work_projects=registered), ledger_data=ledger,
                           owed=owed, routing_info=routing)
    assert not view["projects"] and not view["live"], source
    waiting = {item["key"] for item in view["waiting"]}
    assert "ledger:atlas#n-1" not in waiting and "commitment:hidden" not in waiting
    assert {"commitment:keep", "routing:rule:1"} <= waiting
    assert ledger["projects"]["atlas"]["hidden"] is True
    assert [item["id"] for item in owed["items"]] == ["hidden", "keep"]


@pytest.mark.parametrize("stored, expected_source, expected_labels", [
    (None, "default", ["Claude"]),
    ([], "file", []),
    ([{"label": "Claude work", "harness": "claude", "home": "~/.claude-work"}], "file", ["Claude work"]),
])
def test_account_list_missing_empty_and_nonempty_have_distinct_meanings(tmp_path, monkeypatch, stored,
                                                                        expected_source, expected_labels):
    home = tmp_path / "home"
    (home / ".claude-work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(usage, "_command", lambda harness: "/tmp/fake-claude" if harness == "claude" else None)
    if stored is not None:
        with config.mutate_config() as data:
            data["usage_accounts"] = stored
    status = usage.accounts_status()
    assert status["source"] == expected_source
    assert [item["label"] for item in status["accounts"]] == expected_labels


def test_saving_an_explicit_empty_list_keeps_no_accounts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude-work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(usage, "_command", lambda harness: "/tmp/fake-claude" if harness == "claude" else None)
    with config.mutate_config() as data:
        data["usage_accounts"] = [{"label": "Claude work", "harness": "claude", "home": "~/.claude-work"}]
    before = accounts.status()
    accounts.save_list([], expect=before["version"], via="app")
    after = usage.accounts_status()
    assert config.load_config()["usage_accounts"] == []
    assert after["source"] == "file" and after["accounts"] == [] and not after["none"]


@pytest.mark.parametrize("sources, queue_project, expected", [
    (("snapshot",), "acme/queued", {"acme/snapshot"}),
    (("queue",), "acme/queued", {"acme/queued"}),
    (("snapshot", "queue"), "acme/queued", {"acme/snapshot", "acme/queued"}),
    (("snapshot", "queue"), "acme/snapshot", {"acme/snapshot"}),
])
def test_read_ledger_unions_snapshot_and_offline_queue_projects(tmp_path, monkeypatch, sources, queue_project,
                                                                 expected):
    """Every Work source contributes a project, with overlap appearing once."""
    snapshot = "acme/snapshot"
    monkeypatch.setattr(ledger, "_connect", lambda: (_ for _ in ()).throw(ledger.HubAway("synthetic outage")))
    monkeypatch.setattr(work, "_ledger_projects", lambda conn: [
        {"project": snapshot, "name": "Snapshot", "hidden": False, "created_at": None}
    ] if "snapshot" in sources else [])
    monkeypatch.setattr(ledger, "_offline_view", lambda project: None)
    monkeypatch.setattr(ledger, "queued_ops", lambda: [
        {"project": queue_project, "row_id": "need-1", "verb": "add", "fields": {
            "title": "Approve the next slice", "kind": "needs-owner", "state": "NEEDS OWNER"}, "at": NOW}
    ] if "queue" in sources else [])
    monkeypatch.setattr(ledger, "outbox_status", lambda: {"pending": 1 if "queue" in sources else 0})
    data = work.read_ledger()
    assert set(data["projects"]) == expected
    view = work.build_view(NOW, cfg=cfg(), ledger_data=data, owed={"items": [], "error": None}, routing_info=ROUTING)
    assert {board["project"] for board in view["projects"]} == set(data["projects"])
    if "queue" in sources:
        assert any(item.get("id") == "need-1" for item in view["waiting"])


def test_new_queued_project_appears_in_work_waiting_and_settings(tmp_path, monkeypatch):
    """Port of the review probe using the real local offline queue."""
    def away():
        raise ledger.HubAway("synthetic outage")

    monkeypatch.setattr(ledger, "_connect", away)
    op = ledger.new_op("add", "acme/new-project", "need-1", fields={
        "title": "Approve the next slice", "kind": "needs-owner", "state": "NEEDS OWNER"})
    saved = ledger.write(op)
    assert saved["queued"], saved
    assert ledger.load("acme/new-project")["rows"][0]["row_id"] == "need-1"
    view = work.build_view(NOW, cfg=cfg(), owed={"items": [], "error": None}, routing_info=ROUTING)
    assert view["counts"]["ledger_outbox"] == 1
    assert {board["project"] for board in view["projects"]} == {"acme/new-project"}
    assert any(item.get("id") == "need-1" for item in view["waiting"])
