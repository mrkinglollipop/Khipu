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
    (project_root / "CLAUDE.md").write_text("Prefer Codex first ProjectClaudeSentinel\n",
                                           encoding="utf-8")
    for rel in (".claude/AGENTS.md", ".codex/AGENTS.md", "AGENTS.md"):
        extra = home / rel
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("Prefer Claude for dispatch. GlobalExtraSentinel\n", encoding="utf-8")
    monkeypatch.setattr(routing_scan_report, "known_project_roots",
                        lambda: [project_root])
    # Files the approved set never reads must stay out, however loud they are.
    (home / "unread.md").write_text(f"{WORDS}\n", encoding="utf-8")
    for rel in (".codex/CLAUDE.md", "CLAUDE.md"):
        decoy = home / rel
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text(f"{WORDS} DecoySentinel\n", encoding="utf-8")
    stranger = home / "Projects" / "StrangerRepo"
    stranger.mkdir(parents=True, exist_ok=True)
    (stranger / "AGENTS.md").write_text(f"{WORDS} StrangerSentinel\n", encoding="utf-8")
    return replay


def test_not_looked_at_yet_is_an_explicit_state(scanned):
    public = routing.public_view(scanned.view(), scanned.now)
    assert public[FIELD] == {"status": "not_checked", "scanned_at": None,
                             "files_checked": 0, "hits": [], "hits_total": 0,
                             "more_not_shown": 0, "too_large": []}


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


def test_scan_reads_exactly_the_approved_set(scanned, monkeypatch):
    import sys

    home = scanned.root
    expected = {
        home / ".claude" / "CLAUDE.md", home / ".claude" / "AGENTS.md",
        home / ".codex" / "AGENTS.md", home / "AGENTS.md",
        home / "Projects" / PROJECT_FILE / "CLAUDE.md",
        home / "Projects" / PROJECT_FILE / "AGENTS.md",
        home / ".claude" / "projects" / "synthetic-slug" / "memory" / "note.md",
    }
    assert set(routing_scan_report.candidate_files()) == expected
    opened = []

    def hook(event, args):
        if event == "open" and args and isinstance(args[0], str) and args[0].endswith(".md"):
            opened.append(args[0])

    sys.addaudithook(hook)
    try:
        routing_scan_report.refresh(now=scanned.now)
    finally:
        pass
    assert {p for p in opened if p.endswith(".md")} == {str(p) for p in expected}
    files = {h["file"] for h in routing_scan_report.report()["hits"]}
    assert f"{PROJECT_FILE}/AGENTS.md" in files
    assert f"{PROJECT_FILE}/CLAUDE.md" in files
    assert not any(f.startswith("/") for f in files)
    assert not any("DecoySentinel" in h["quote"] or "StrangerSentinel" in h["quote"]
                   for h in routing_scan_report.report()["hits"])


def test_report_refreshes_on_read_when_a_scanned_file_changes(scanned):
    routing_scan_report.refresh(now=scanned.now)
    assert routing_scan_report.report()["hits"]
    (scanned.root / ".claude" / "CLAUDE.md").write_text("# Global\nclean now\n",
                                                        encoding="utf-8")
    report = routing_scan_report.report()
    assert report["status"] == "checked"
    assert not any(h["file"] == "~/.claude/CLAUDE.md" for h in report["hits"])


def test_reconcile_without_hub_still_refreshes_the_report(scanned):
    routing_scan_report.refresh(now=scanned.now)
    (scanned.root / ".claude" / "CLAUDE.md").write_text("# Global\nclean now\n",
                                                        encoding="utf-8")
    plan = notes._build_plan()
    notes._write_state({"last_reconcile_at": None, "files": {
        item["path"]: {**item["signature"], "slug": item["parsed"]["slug"],
                       "base_slug": item["parsed"]["slug"]} for item in plan}})
    out = notes.reconcile(changed_only=True)
    assert out["candidates"] == 0
    assert not any(h["file"] == "~/.claude/CLAUDE.md"
                   for h in routing_scan_report.report()["hits"])


def test_limits_cap_file_size_and_hit_counts(scanned):
    note = (scanned.root / ".claude" / "projects" / "synthetic-slug"
            / "memory" / "note.md")
    note.write_text("---\nname: Synthetic\n---\n"
                    + "Prefer Codex first LimitSentinel\n" * 600, encoding="utf-8")
    big = note.parent / "big.md"
    big.write_text("---\nname: big\n---\n" + "x" * (routing_scan_report.MAX_FILE_BYTES + 10),
                   encoding="utf-8")
    report = routing_scan_report.refresh(now=scanned.now)
    assert len(report["hits"]) <= routing_scan_report.MAX_HITS_TOTAL
    assert report["hits_total"] > len(report["hits"])
    assert report["more_not_shown"] == report["hits_total"] - len(report["hits"])
    assert any(h["file"].endswith("memory/note.md") for h in report["hits"])
    assert sum(1 for h in report["hits"] if h["file"].endswith("memory/note.md")) \
        <= routing_scan_report.MAX_HITS_PER_FILE
    assert any(t.endswith("memory/big.md") for t in report["too_large"])
    assert "LimitSentinel" not in json.dumps(report["too_large"])


def test_bool_line_in_a_damaged_report_is_rejected(scanned):
    routing_scan_report.refresh(now=scanned.now)
    path = routing_scan_report.store_path()
    path.write_text(json.dumps({"status": "checked", "scanned_at": "x",
                                "files_checked": 1,
                                "hits": [{"file": "x", "line": True, "quote": "q"}]}),
                    encoding="utf-8")
    # A damaged report reads as not checked; staleness refresh only
    # replaces a valid report, never resurrects a damaged one.
    assert routing_scan_report.report()["status"] == "not_checked"


def test_report_write_is_atomic_with_owner_only_modes(scanned):
    routing_scan_report.refresh(now=scanned.now)
    path = routing_scan_report.store_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(path.parent.glob("*.tmp"))
