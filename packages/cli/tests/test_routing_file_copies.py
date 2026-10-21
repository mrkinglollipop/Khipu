"""Owner-local routing copies in files must never become delivery data.

Only disposable homes and synthetic words; every outward projection is
scanned recursively, including strings nested in lists and dictionary keys.
"""
from __future__ import annotations

import copy
import json

import pytest

from khipu import cli, notes, routing, routing_delivery, routing_scan_report
from tests.test_routing_acceptance import replay  # noqa: F401

FIELD = "routing_file_copies"
WORDS = "Codex is low with 9% left QuasarSentinel JuniperSentinel"
PROJECT_FILE = "QuasarProjectSentinel"


def strings(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from strings(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from strings(inner)
    elif isinstance(value, str):
        yield value


@pytest.fixture
def scanned(replay, monkeypatch):
    replay.configure(routing_cloud_copy=True, routing_cloud_copy_rules=True,
                     routing_cloud_copy_hold_words=True, routing_child_chars=400)
    home = replay.root
    global_file = home / ".claude" / "CLAUDE.md"
    global_file.parent.mkdir(parents=True, exist_ok=True)
    global_file.write_text(f"# Global\n{WORDS} " + "x" * 200 + "\n", encoding="utf-8")
    proj_dir = home / ".claude" / "projects" / "synthetic-slug"
    note = proj_dir / "memory" / "note.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\nname: Synthetic\n---\nPrefer Claude for dispatch. JuniperSentinel\n",
                    encoding="utf-8")
    project_root = home / "Projects" / PROJECT_FILE
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "AGENTS.md").write_text("Delegation order: second Claude account first\n",
                                           encoding="utf-8")
    monkeypatch.setattr(notes, "resolve_claude_project_path",
                        lambda slug, **kw: project_root)
    # A file the scanner never reads must stay out, however loud it is.
    (home / "unread.md").write_text(f"{WORDS}\n", encoding="utf-8")
    return replay


def test_not_looked_at_yet_is_an_explicit_state(scanned):
    public = routing.public_view(scanned.view(), scanned.now)
    assert public[FIELD] == {"status": "not_checked", "scanned_at": None,
                             "files_checked": 0, "hits": []}


def test_refresh_reports_each_hit_owner_locally(scanned, capsys):
    routing_scan_report.refresh(now=scanned.now)
    view = scanned.view()
    before = copy.deepcopy(view)
    public = routing.public_view(view, scanned.now)
    assert view == before
    assert FIELD not in view
    report = public[FIELD]
    assert report["status"] == "checked"
    assert report["scanned_at"] is not None
    assert report["files_checked"] >= 3
    by_file = {hit["file"]: hit for hit in report["hits"]}
    assert by_file["~/.claude/CLAUDE.md"]["line"] == 2
    assert by_file["~/.claude/CLAUDE.md"]["superseded"] is True
    assert "QuasarSentinel" in by_file["~/.claude/CLAUDE.md"]["quote"]
    assert len(by_file["~/.claude/CLAUDE.md"]["quote"]) <= 141
    note_hits = [h for h in report["hits"] if h["file"].endswith("memory/note.md")]
    assert note_hits and note_hits[0]["line"] == 4
    assert note_hits[0]["file"].startswith("~/")
    agent_hits = [h for h in report["hits"] if h["file"].endswith("AGENTS.md")]
    assert agent_hits and agent_hits[0]["line"] == 1
    assert not any("unread.md" in h["file"] for h in report["hits"])
    assert not any(str(scanned.root) in h["file"] for h in report["hits"])
    assert cli.main(["route", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[FIELD] == report


def test_a_damaged_scan_report_never_breaks_the_route_view(scanned, monkeypatch):
    routing_scan_report.store_path().parent.mkdir(parents=True, exist_ok=True)
    routing_scan_report.store_path().write_text("{not json", encoding="utf-8")
    public = routing.public_view(scanned.view(), scanned.now)
    assert public[FIELD]["status"] == "not_checked"
    assert public[FIELD]["hits"] == []

    def broken():
        raise KeyError("hits")

    monkeypatch.setattr(routing_scan_report, "report", broken)
    public = routing.public_view(scanned.view(), scanned.now)
    assert public[FIELD]["hits"] == []


@pytest.mark.parametrize("surface", ["push", "child", "mcp", "replication", "gateway", "doctor"])
def test_owner_local_file_copies_never_reach_other_surfaces(scanned, monkeypatch, surface):
    for i in range(3):
        routing.add_rule(f"Synthetic ordinary rule {i} " + "x" * 900, via="app")
    routing_scan_report.refresh(now=scanned.now)
    view = scanned.view()
    public = routing.public_view(view, scanned.now)
    assert public[FIELD]["hits"]
    enriched = dict(view, **{FIELD: public[FIELD]})
    monkeypatch.setattr(routing, "read_view", lambda *a, **kw: enriched)
    monkeypatch.setattr(routing, "_start_view", lambda *a, **kw: enriched)
    if surface == "push":
        output = routing.start_push(budget=2, session_id="synthetic-recipient")
        assert output and output["text"]
    elif surface == "child":
        start = routing.start_push(budget=2, moment="delegation",
                                   child_component="agent:synthetic", child_session="synthetic-recipient")
        assert routing_delivery.acknowledge("synthetic-recipient", start["routing_changes"], now=scanned.now)
        pages = []
        for _ in range(20):
            page = routing._child_page_push_inline("synthetic-recipient", "synthetic", scanned.now)
            if not page:
                break
            pages.append(page)
            assert routing_delivery.acknowledge("synthetic-recipient", page["routing_changes"], now=scanned.now)
        assert start and start["text"] and pages
        output = [start, pages, routing_delivery.snapshot("synthetic-recipient", "agent:synthetic")]
    elif surface == "mcp":
        error, output = scanned.call(session_id="synthetic-recipient")
        assert not error and output["accounts"]
    elif surface == "replication":
        events = routing._read_all_events()[0]
        events = [dict(e, **{FIELD: public[FIELD]}) for e in events]
        output = routing.replication_payload(events, bindings={})
        assert output
    elif surface == "doctor":
        output = routing.doctor_block(now=scanned.now)
        assert output
    else:
        output = routing.gateway_projection(public, now=scanned.now)
        assert output and output["accounts"] and output["rules"]
    forbidden = (FIELD, "QuasarSentinel", "JuniperSentinel", PROJECT_FILE,
                 "~/.claude/CLAUDE.md", "memory/note.md",
                 str(scanned.root / ".claude" / "CLAUDE.md"),
                 str(scanned.root / ".claude" / "projects"))
    for leaf in strings(output):
        assert not any(secret in leaf for secret in forbidden), (surface, leaf)
