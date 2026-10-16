"""The Work view and the ledger, review of 78fb72a (its probes ported first):

1. a path is checked after decoding, and a link's path and query like any
   other words: only its scheme and host are never checked;
2. when the privacy table can't be built, every string that could carry a
   name is withheld and the answer says it is incomplete;
3. an unexpected error in any verb of ``khipu_ledger``, ``khipu ledger`` and
   ``khipu work`` answers through the same projection;
4. a file that grows past its cap while it is read is refused, never cut;
5. a hold's age in days sets the view's deadline (``valid_until``).

The app's half is in ReviewRound5.test.tsx. Everything here is synthetic.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from urllib.parse import unquote

import pytest

from khipu import agent_view, ledger, ledger_import, mcp_server, safe_read, work
from tests.test_work import NO_LEDGER, NO_OWED, NOW, ROUTING, cfg, entry, job, put, summary
from tests.test_work_structural import _argv, _run, _subcommands, private_backends  # noqa: F401

PRIVATE = "/opt/project/private.txt"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")


# ---- 1. decoded paths and links --------------------------------------------------------------


@pytest.mark.parametrize("text", ["%2Fopt%2Fproject%2Fprivate.txt",
                                  "https://example.com/open?file=/opt/project/private.txt",
                                  "https://example.com/open/%2Fopt%2Fproject%2Fprivate.txt",
                                  "https://example.com/open#file=/opt/project/private.txt"])
def test_projection_removes_encoded_and_url_locations(text):
    projected = agent_view.project({"title": text})
    assert PRIVATE not in unquote(json.dumps(projected))
    assert work._private_text(text)


def test_a_link_keeps_its_host_when_its_query_holds_a_path():
    out = agent_view.project("see https://example.com/open?file=/opt/project/private.txt")
    assert out.startswith("see https://example.com") and work.PATH_WORDS in out, out
    assert agent_view.project("https://github.com/example/atlas/pull/41") == "https://github.com/example/atlas/pull/41"


def test_work_view_never_contains_encoded_file_locations(tmp_path):
    path = put(tmp_path, summary(jobs=[job(summary="%2Fopt%2Fproject%2Fprivate.txt")]))
    view = work.build_view(NOW, cfg=cfg(work_projects=[entry(path)]), ledger_data=NO_LEDGER, owed=NO_OWED,
                           routing_info=ROUTING)
    assert PRIVATE not in unquote(json.dumps(agent_view.project(view)))


# ---- 2. the privacy table can't be built -------------------------------------------------------


def test_failed_privacy_table_never_exposes_login_name(monkeypatch, tmp_path, capsys):
    name = "review-private-login"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / name))
    assert name not in agent_view.project({"title": name})["title"]

    def broken(*a, **k):
        raise RuntimeError("privacy table unavailable")

    monkeypatch.setattr(agent_view, "_table", broken)
    out = agent_view.project({"title": name, "at": "2026-10-15T12:00:00Z", "rows": 3})
    assert name not in json.dumps(out)
    assert out["incomplete"] is True and out["at"] == "2026-10-15T12:00:00Z" and out["rows"] == 3
    agent_view.emit(text=f"one line\n{name}")
    printed = capsys.readouterr().out
    assert name not in printed and agent_view.INCOMPLETE in printed


# ---- 3. an unexpected error in each verb -------------------------------------------------------


def _unexpected(*a, **k):
    raise RuntimeError(f"invalid record at {PRIVATE}")


@pytest.fixture
def failing_backends(private_backends, monkeypatch):  # noqa: F811
    """Every data source of both commands raising an error no verb expects."""
    for module, names in ((ledger, ("load", "get", "removed_rows", "write", "purge")),
                          (ledger_import, ("export_rows", "run_import")),
                          (work, ("build_view", "check_file", "read_owed", "set_hidden", "read_hidden",
                                  "set_link"))):
        for attr in names:
            monkeypatch.setattr(module, attr, _unexpected)
    return private_backends


def test_an_unexpected_error_in_every_cli_verb_answers_through_the_projection(failing_backends):
    tmp_path = failing_backends
    runs = 0
    for command in ("ledger", "work"):
        for verb, sp in _subcommands(command).items():
            flags = {a.dest for a in sp._actions}
            for extra in ([], ["--json"]) if "json" in flags else ([],):
                argv = _argv(command, verb, sp, tmp_path) + extra
                out, err = _run(argv)
                assert "/opt/project" not in out + err, (argv, out, err)
                runs += 1
    for argv in (["work"], ["work", "--json"]):
        out, err = _run(argv)
        assert "RuntimeError" in out + err and "/opt/project" not in out + err, (argv, out, err)
    assert runs >= 20


@pytest.mark.parametrize("verb", mcp_server._LEDGER_VERBS)
def test_an_unexpected_error_in_every_tool_verb_answers_through_the_projection(failing_backends, verb):
    arguments = {"verb": verb, "project": "atlas", "id": "one", "title": "A title", "kind": "slice"}
    response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "khipu_ledger", "arguments": arguments}})
    assert "/opt/project" not in json.dumps(response), verb


def test_a_value_error_answers_through_the_projection(monkeypatch):
    def failed(*a, **k):
        raise ValueError(f"invalid record at {PRIVATE}")

    monkeypatch.setattr(ledger, "load", failed)
    response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "khipu_ledger", "arguments": {"verb": "list", "project": "atlas"}}})
    assert "/opt/project" not in json.dumps(response)


# ---- 4. a file that grows while it is read -----------------------------------------------------


def _grow_on_first_read(monkeypatch, file):
    real_read = os.read
    grown = []

    def growing(fd, n):
        if not grown:
            grown.append(1)
            with file.open("ab") as f:
                f.write(b"efgh")
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", growing)


def test_growing_file_is_not_silently_truncated(tmp_path, monkeypatch):
    file = tmp_path / "report.json"
    file.write_bytes(b"abcd")
    _grow_on_first_read(monkeypatch, file)
    with pytest.raises((safe_read.Refused, safe_read.TooBig)):
        safe_read.read_file(str(file), 4)


def test_a_file_that_grows_without_a_cap_is_read_whole_or_refused(tmp_path, monkeypatch):
    file = tmp_path / "report.json"
    file.write_bytes(b"abcd")
    _grow_on_first_read(monkeypatch, file)
    try:
        data, _st = safe_read.read_file(str(file))
    except safe_read.Refused:
        return
    assert data == file.read_bytes()


def test_an_unchanged_file_reads_whole_at_its_cap(tmp_path):
    file = tmp_path / "report.json"
    file.write_bytes(b"abcd")
    assert safe_read.read_file(str(file), 4)[0] == b"abcd"
    with pytest.raises(safe_read.TooBig):
        safe_read.read_file(str(file), 3)


# ---- 5. a hold's age sets the deadline ---------------------------------------------------------


def test_hold_daily_age_sets_valid_until():
    routing = {**ROUTING, "holds": [{"owner": True, "remind": True, "id": "hold1",
                                     "at": (NOW - timedelta(hours=47, minutes=59)).isoformat(),
                                     "scope_label": "Muse"}]}
    view = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER, owed=NO_OWED, routing_info=routing)
    assert view["valid_until"] is not None
    assert work._parse_time(view["valid_until"]) <= NOW + timedelta(minutes=1)
