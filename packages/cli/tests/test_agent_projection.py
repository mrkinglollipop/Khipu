"""Every agent-facing or JSON output of the ledger and the Work view leaves
through agent_view.project: khipu_ledger results, `khipu ledger ... --json`,
`khipu work --json`, `khipu work check --json` and the saved view.json.

Each output is fed a fixture full of private strings (a login folder by path
and by name, account keys, the home folder, another person's folder and two
key-shaped secrets) and must come out without any of them. Every folder here
is a synthetic one in a temporary tree.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

import pytest

from khipu import agent_view, ledger, mcp_server, work

LOGIN_NAME = "synthetic-private-login"
XAI = "xai-" + "Ab12" * 12
OTHER = "/" + "Users" + "/someone/private.txt"  # another person's folder
ANT = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
NOW = datetime(2026, 10, 15, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    (tmp_path / "data").mkdir()
    login = tmp_path / LOGIN_NAME
    login.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(login))
    home = os.path.expanduser("~")
    text = (f"see {login} and codex|{login} and claude| and {home}/notes/plan.md and "
            f"{OTHER} and {LOGIN_NAME} and {XAI} and {ANT}")
    forbidden = [LOGIN_NAME, "codex|", "claude|", f"{home}/notes", OTHER[:7], XAI[:12], ANT[:12]]
    return text, forbidden


def _clean(out: str, forbidden: list[str]) -> None:
    leaked = [f for f in forbidden if f in out]
    assert not leaked, (leaked, out)


def _row(text: str) -> dict:
    return {"row_id": "a", "title": text, "kind": "slice", "parent": "", "state": "QUEUED", "owner": "none",
            "evidence": text, "link": "", "refs": {"imported": {text: text}}, "seq": 1, "created_at": NOW,
            "updated_at": NOW, "version": 1, "removed_at": None, "removed_by": None, "removed_note": ""}


def _history(text: str) -> list[dict]:
    return [{"id": 1, "row_id": "a", "at": NOW, "at_text": "2026-10-15T20:00:00Z", "author": {"kind": "agent"},
             "shape": "transition", "from_state": None, "to_state": "QUEUED", "changes": {}, "note": text,
             "event": None, "raw": None, "source": "cli"}]


def _fake_ledger(monkeypatch, text: str) -> None:
    view = {"vocab": {"state": [["QUEUED", "open"]], "kind": ["slice"]}, "rows": [_row(text)],
            "as_of": None, "offline": False}
    monkeypatch.setattr(ledger, "get", lambda *a, **k: {"row": _row(text), "history": _history(text)})
    monkeypatch.setattr(ledger, "load", lambda *a, **k: view)
    monkeypatch.setattr(ledger, "removed_rows", lambda *a, **k: [{**ledger.row_json(_row(text), [], "p"),
                                                                    "removed_note": text, "removed_at": text,
                                                                    "removed_by": text, "days_left": 3}])


def _cli(*argv) -> tuple[int, str, str]:
    from khipu import cli

    args = cli.build_parser().parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = args.func(args)
    return rc, out.getvalue(), err.getvalue()


def test_khipu_ledger_results_leave_through_the_projection(private, monkeypatch):
    text, forbidden = private
    _fake_ledger(monkeypatch, text)
    for verb in ("get", "list", "check", "render", "loose-ends", "needs-owner", "removed"):
        response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "khipu_ledger", "arguments": {"verb": verb, "project": "acme/widget", "id": "a"}}})
        _clean(json.dumps(response), forbidden)


def test_khipu_ledger_errors_leave_through_the_projection(private, monkeypatch):
    text, forbidden = private

    def refuse(*a, **k):
        raise ledger.LedgerError(text, "refused")

    monkeypatch.setattr(ledger, "get", refuse)
    response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "khipu_ledger", "arguments": {"verb": "get", "project": "acme/widget", "id": "a"}}})
    _clean(json.dumps(response), forbidden)


def test_ledger_json_outputs_leave_through_the_projection(private, monkeypatch):
    text, forbidden = private
    _fake_ledger(monkeypatch, text)
    for verb in (["get", "a"], ["list"], ["check"], ["render"], ["loose-ends"], ["needs-owner"], ["removed"]):
        rc, out, err = _cli("ledger", *verb, "--project", "acme/widget", "--json")
        assert out.strip(), (verb, rc, err)
        _clean(out + err, forbidden)


def test_work_json_and_the_saved_view_leave_through_the_projection(private, monkeypatch):
    text, forbidden = private
    view = work._View(
        {"version": 1, "counts": {"waiting": 1, "blocking": 0, "running": 0}, "waiting": [{"title": text}],
         "live": [], "projects": [{"name": text, "source": {"problem": text}}], text: [text]},
        work._view_generation(),
    )
    monkeypatch.setattr(work, "build_view", lambda *a, **k: view)
    rc, out, err = _cli("work", "--json")
    assert rc == 0, err
    _clean(out, forbidden)
    _clean(work.view_file().read_text(encoding="utf-8"), forbidden)


def test_work_check_json_leaves_through_the_projection(private, tmp_path):
    from tests.test_work import job, summary

    text, forbidden = private
    path = tmp_path / "work-summary.json"
    path.write_text(json.dumps(summary(jobs=[job(LOGIN_NAME, state="unknown-state")])))
    rc, out, err = _cli("work", "check", str(path), "--json")
    assert rc == 1 and not err
    _clean(out, forbidden)


def test_the_projection_fails_closed_on_what_its_replacements_miss(private):
    text, forbidden = private
    for value in (text, {text: [text, {"k": text}]}, f"codex|{XAI}", "grok|  spaced key", "muse|x"):
        _clean(json.dumps(agent_view.project(value)), forbidden + ["grok|", "muse|"])
    # Ordinary words, links and ids pass as they are.
    plain = {"id": "row-1", "title": "Fix the CI/CD step", "link": "https://example.com/pr/1", "rows": 3}
    assert agent_view.project(plain) == plain


def test_projection_withholds_every_file_location_and_any_prefixed_account_key():
    from khipu import agent_view

    payload = {
        "absolute": "read /etc/passwd",
        "relative": "read src/private.py",
        "windows": r"read C:\Users\somebody\secret.txt",
        "account": "prefix_codex|opaque-login-folder",
        "suffixed": "codex2|opaque-login-folder",
    }
    rendered = repr(agent_view.project(payload))
    for forbidden in ("/etc/passwd", "src/private.py", r"C:\Users", "codex|", "codex2|"):
        assert forbidden not in rendered
    assert agent_view.project("read /etc/passwd") == "read (a path)"
