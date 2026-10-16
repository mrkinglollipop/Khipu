"""The Work view and the ledger, one rule per class of defect (cross-family
review of 2c61369, whose probes are ported here first):

1. every ``khipu ledger`` and ``khipu work`` answer, text or JSON, and every
   ``khipu_ledger`` result leaves through one emit (``agent_view.emit`` /
   ``agent_view.project``): each verb from the argparse tree and the tool
   table is run against data full of private strings;
2. a path the owner gives is parsed only by ``safe_read``;
3. every verb that changes anything passes ``ledger.require`` first;
4. a link is cut out of a word by its exact span, and the rest still checked;
5. a catch-up compares the whole ordered history, never a delta;
6. (the app's half is in WorkOwnerActions.test.tsx).

Everything here is synthetic: temporary folders, fake backends, the scratch
PostgreSQL of test_ledger for the two import round trips.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from khipu import agent_view, cli, ledger, ledger_import, mcp_server, routing, safe_read, work
from tests import test_ledger as _tl
from tests import test_ledger_import as _ti

pg_dsn, hub = _tl.pg_dsn, _tl.hub
PKG = Path(__file__).resolve().parents[1] / "khipu"

# Private strings of every kind the projection must keep in: a full path, a
# path joined to a link, a home path, an account key with its login folder.
PRIVATE = ("/opt/project/private.txt", "source/private.txt=https://example.com/ok", "see ~/notes/plan.md",
           "codex|/synthetic/login")
MARKERS = ("/opt/project", "source/private.txt", "~/notes", "codex|", "/synthetic/login")
SAY = " ".join(PRIVATE)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))


def _subcommands(command: str) -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices[command]
    verbs = [a for a in top._actions if isinstance(a, argparse._SubParsersAction)]
    return dict(verbs[0].choices) if verbs else {}


def _positionals(sp: argparse.ArgumentParser) -> list[str]:
    return [a.dest for a in sp._actions if not a.option_strings and a.dest != "help"]


def _clean(text: str, where: str) -> None:
    for marker in MARKERS:
        assert marker not in text, f"{where}: {marker!r} left in {text[:400]!r}"


# ---- 1. one emit ---------------------------------------------------------------------------


def _private_row(row_id: str = "one") -> dict:
    return {"row_id": row_id, "title": SAY, "kind": "slice", "parent": "", "state": "QUEUED",
            "owner": "codex|/synthetic/login", "evidence": SAY, "link": "https://example.com/ok",
            "refs": {"note": SAY}, "seq": 1, "created_at": None, "updated_at": None, "version": 1,
            "removed_at": None, "removed_by": None, "removed_note": ""}


@pytest.fixture
def private_backends(monkeypatch, tmp_path):
    """Every data source of both commands, answering with private strings;
    the owner check lets every verb through (section 3 tests the check)."""
    view = {"rows": [_private_row()], "vocab": ledger.default_vocab(), "offline": True, "as_of": SAY, "pending": 0}
    history = [{"raw": {"at": "2026-10-15T10:00:00Z", "event": SAY}}]
    line = {"ok": True, "line": f"set one: {SAY}", "result": SAY}
    report = {"ok": True, "result": "done", "words": SAY, "file": SAY, "project": "atlas", "masked": [SAY],
              "other_keys": [SAY], "counts_in_file": {"rows": 1, "history": 1, "with_parent": 0,
                                                      "by_state": {SAY: 1}, "by_kind": {SAY: 1},
                                                      "missing_parent": [SAY]}}

    class Conn:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(ledger, "require", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "_project_for", lambda args: "atlas")
    monkeypatch.setattr(ledger, "load", lambda project, **k: copy.deepcopy(view))
    monkeypatch.setattr(ledger, "get", lambda project, row_id, **k: {"row": _private_row(), "history": history,
                                                                     "offline": True, "as_of": SAY})
    monkeypatch.setattr(ledger, "removed_rows", lambda project, **k: [{"id": "one", "title": SAY, "removed_at": SAY,
                                                                       "removed_by": SAY, "days_left": 3}])
    monkeypatch.setattr(ledger, "write", lambda op, **k: dict(line))
    monkeypatch.setattr(ledger, "purge", lambda conn, project, row_id: dict(line))
    monkeypatch.setattr(ledger, "_connect", Conn)
    monkeypatch.setattr(ledger_import, "export_rows", lambda *a, **k: {"one": {"id": "one", "evidence": SAY,
                                                                               "owner": "codex|/synthetic/login"}})
    monkeypatch.setattr(ledger_import, "run_import", lambda *a, **k: copy.deepcopy(report))
    wait = {"source": "ledger", "key": "k", "project": "atlas", "id": "one", "title": SAY, "evidence": SAY}
    monkeypatch.setattr(work, "build_view", lambda *a, **k: {"format": "khipu-work-view", "note": SAY,
                                                            "waiting": [wait]})
    monkeypatch.setattr(work, "write_view", lambda view: None)
    monkeypatch.setattr(work, "render_text", lambda view: f"Waiting on you\n{SAY}")
    monkeypatch.setattr(work, "check_file", lambda path: (False, [SAY], None))
    monkeypatch.setattr(work, "read_owed", lambda: {"items": [{"id": "42", "title": SAY}], "error": None})
    monkeypatch.setattr(work, "set_hidden", lambda *a: {})
    monkeypatch.setattr(work, "read_hidden", lambda: {SAY: SAY})
    monkeypatch.setattr(work, "set_link", lambda *a: {})
    return tmp_path


def _argv(command: str, verb: str, sp: argparse.ArgumentParser, tmp_path: Path) -> list[str]:
    values = {"id": "one", "file": SAY.split()[0], "row": "atlas#one", "commitment": "42"}
    argv = [command, verb] + [values.get(p, "one") for p in _positionals(sp)]
    if verb == "add":
        argv += ["--title", "A title", "--kind", "slice"]
    if verb == "export":
        argv += ["--tracker-json"]
    if verb == "hide":
        argv[-1] = "42"
    return argv


def _run(argv: list[str]) -> tuple[str, str]:
    args = cli.build_parser().parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        args.func(args)
    return out.getvalue(), err.getvalue()


def test_every_cli_verb_answers_through_the_projection(private_backends):
    tmp_path = private_backends
    runs = 0
    for command in ("ledger", "work"):
        verbs = _subcommands(command)
        assert verbs, command
        for verb, sp in verbs.items():
            flags = {a.dest for a in sp._actions}
            for extra in ([], ["--json"]) if "json" in flags else ([],):
                argv = _argv(command, verb, sp, tmp_path) + extra
                for out_file in ([None, tmp_path / f"{verb}.out"] if "out" in flags else [None]):
                    full = argv + (["--out", str(out_file)] if out_file else [])
                    out, err = _run(full)
                    _clean(out + err, " ".join(full))
                    if out_file is not None:
                        _clean(out_file.read_text(encoding="utf-8"), f"{' '.join(full)} (the file)")
                    runs += 1
    out, err = _run(["work", "--json"])
    _clean(out + err, "work --json")
    out, err = _run(["work"])
    _clean(out + err, "work")
    assert runs >= 20


def test_a_refusal_answers_through_the_projection(monkeypatch):
    def refuse(*a, **k):
        raise ledger.LedgerError(f"refused: {SAY}", "refused")

    monkeypatch.setattr(ledger, "_project_for", lambda args: "atlas")
    monkeypatch.setattr(ledger, "load", refuse)
    for argv in (["ledger", "list"], ["ledger", "list", "--json"]):
        out, err = _run(argv)
        assert "refused" in out + err
        _clean(out + err, " ".join(argv))


def _call(name: str, arguments: dict) -> str:
    reply = mcp_server._dispatch_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": name, "arguments": arguments}})
    return json.dumps(reply)


def test_every_khipu_ledger_verb_answers_through_the_projection(private_backends, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ensure_path", lambda: None)
    tool = next(t for t in mcp_server.TOOLS if t["name"] == "khipu_ledger")
    listed = tool["inputSchema"]["properties"]["verb"]["enum"]
    assert set(listed) == set(mcp_server._LEDGER_VERBS)
    for verb in listed:
        for extra in ({}, {"summary": True}) if verb == "loose-ends" else ({},):
            args = {"verb": verb, "id": "one", "title": "A title", "kind": "slice", **extra}
            _clean(_call("khipu_ledger", args), f"khipu_ledger {verb}")
    _clean(_call("khipu_forget", {"id": "ledger:atlas#one"}), "khipu_forget ledger:")

    def refuse(*a, **k):
        raise ledger.LedgerError(f"refused: {SAY}", "refused")

    monkeypatch.setattr(ledger, "load", refuse)
    monkeypatch.setattr(ledger, "write", refuse)
    for verb in ("list", "add"):
        _clean(_call("khipu_ledger", {"verb": verb, "id": "one", "title": "A title", "kind": "slice"}),
               f"khipu_ledger {verb} refused")
    _clean(_call("khipu_forget", {"id": "ledger:atlas#one"}), "khipu_forget ledger: refused")


def test_no_ledger_or_work_module_prints_around_the_emit():
    """The one emit is agent_view.emit: these modules have no print of their
    own, so no answer can leave without the projection."""
    for name in ("ledger.py", "work.py", "ledger_import.py"):
        text = (PKG / name).read_text(encoding="utf-8")
        assert not re.search(r"(?<![\w.])print\(", text), name


# Ported: export bypassed the projection, even with --json.
def test_export_json_uses_projection(monkeypatch, capsys):
    class Connection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(ledger, "_connect", Connection)
    monkeypatch.setattr(ledger_import, "export_rows", lambda *a, **k: {
        "one": {"id": "one", "evidence": "/opt/project/private.txt", "owner": "codex|/synthetic/login",
                "history": []}})
    args = cli.build_parser().parse_args(["ledger", "export", "--tracker-json", "--project", "atlas", "--json"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "/opt/project/private.txt" not in out and "codex|" not in out


# ---- 2. one path entry point ----------------------------------------------------------------


_NORMALISERS = re.compile(r"os\.path\.(?:normpath|abspath|realpath)\(|\.resolve\(|expanduser\(")
# The modules that take a path someone gave and read it (through safe_read).
_READERS = ("work.py", "ledger.py", "ledger_import.py")


def test_only_safe_read_parses_a_path_given_to_a_reader():
    for name in _READERS:
        for number, line in enumerate((PKG / name).read_text(encoding="utf-8").splitlines(), 1):
            assert not _NORMALISERS.search(line), f"{name}:{number} parses a path itself: {line.strip()}"
    # And every module that reads through safe_read leaves the parsing to it.
    for path in PKG.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name != "safe_read.py" and re.search(r"safe_read\.(?:read_file|open_file)\(", text):
            assert path.name in _READERS, f"{path.name} reads through safe_read: add it to _READERS"


@pytest.mark.parametrize("suffix", ["/", "//", "/.", "/./", "/x/..", ""])
def test_safe_read_refuses_a_trailing_slash_and_dot_segments(tmp_path, suffix):
    path = tmp_path / "plain"
    path.write_text("the regular file")
    if not suffix:
        assert safe_read.read_file(str(path))[0] == b"the regular file"
        return
    with pytest.raises(safe_read.Refused):
        safe_read.read_file(str(path) + suffix)


def test_each_reader_refuses_what_safe_read_refuses(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{}")
    for raw in (str(path) + "/", str(tmp_path) + "/./s.json", str(tmp_path) + "//s.json"):
        with pytest.raises(ledger.LedgerError, match="Not read"):
            ledger_import.read_tracker(raw)
        assert work.check_file(raw)[1] == [work.SOURCE_PROBLEMS["refused"]]
        with pytest.raises(ValueError, match="full path"):
            work.set_setting("work_projects", [{"project": "atlas", "file": raw}])
    # A setting keeps the path exactly as given.
    work.set_setting("work_projects", [{"project": "atlas", "file": str(path)}])
    assert work.settings()["work_projects"][0]["file"] == str(path)


# Ported: normalising erased link/.. before safe_read saw it.
def test_import_refuses_dot_segments_before_normalizing(tmp_path):
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    (there / "deep").mkdir(parents=True)
    (here / "link").symlink_to(there / "deep", target_is_directory=True)
    for base, name in [(here, "decoy"), (there, "named")]:
        (base / "tracker.json").write_text(json.dumps({"rows": {name: {"id": name}}}))
    raw = str(here) + "/link/../tracker.json"
    assert json.loads(Path(raw).read_text())["rows"].keys() == {"named"}
    with pytest.raises(ledger.LedgerError, match="Not read"):
        ledger_import.read_tracker(raw)


# ---- 3. one owner guard ---------------------------------------------------------------------


def test_every_verb_is_classified():
    for command in ("ledger", "work"):
        for verb in _subcommands(command):
            name = f"{command} {verb}"
            assert (name in ledger.WRITE_POLICY) != (name in ledger.READ_VERBS), f"{name}: classify it"
    assert "work" in ledger.READ_VERBS
    for verb in mcp_server._LEDGER_VERBS:
        assert f"ledger {verb}" in ledger.WRITE_POLICY or f"ledger {verb}" in ledger.READ_VERBS
    with pytest.raises(ledger.LedgerError):
        ledger.require("ledger something-new")


class _Guarded(Exception):
    pass


def test_every_changing_verb_calls_the_guard_before_anything_else(monkeypatch, tmp_path):
    seen: list[str] = []

    def guard(command, **kw):
        seen.append(command)
        raise _Guarded(command)

    def touched(*a, **k):
        raise AssertionError("reached a backend before the owner check")

    monkeypatch.setattr(ledger, "require", guard)
    for name in ("load", "get", "write", "purge", "_connect", "removed_rows"):
        monkeypatch.setattr(ledger, name, touched)
    monkeypatch.setattr(ledger_import, "run_import", touched)
    for name in ("set_hidden", "set_link", "read_owed", "build_view", "check_file"):
        monkeypatch.setattr(work, name, touched)
    for command in ("ledger", "work"):
        for verb, sp in _subcommands(command).items():
            name = f"{command} {verb}"
            if name not in ledger.WRITE_POLICY:
                continue
            args = cli.build_parser().parse_args(_argv(command, verb, sp, tmp_path))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), pytest.raises(_Guarded):
                args.func(args)
            assert seen[-1] == name
    # khipu_ledger's writes and khipu_forget's ledger removal pass it too.
    monkeypatch.setattr(mcp_server, "_ensure_path", lambda: None)
    for verb in ("add", "set", "remove"):
        with pytest.raises(_Guarded):
            mcp_server._tool_ledger({"verb": verb, "id": "one", "title": "t", "kind": "slice", "project": "atlas"})
        assert seen[-1] == f"ledger {verb}"
    with pytest.raises(_Guarded):
        mcp_server._tool_forget({"id": "ledger:atlas#one"})
    assert seen[-1] == "ledger remove"


@pytest.mark.parametrize("verb", ["hide", "unhide", "same", "not-same", "import", "catch-up"])
def test_an_owner_verb_refuses_a_conversation_and_a_piped_yes(monkeypatch, tmp_path, verb):
    """Routing's rule (routing.typed_yes): the app, or a yes typed on an
    interactive terminal. A piped yes is never asked for and never counts."""
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["zsh"]})
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    calls: list = []
    monkeypatch.setattr(ledger_import, "run_import", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(work, "set_hidden", lambda *a: calls.append(a))
    monkeypatch.setattr(work, "set_link", lambda *a: calls.append(a))
    monkeypatch.setattr(work, "read_owed", lambda: {"items": [{"id": "42"}], "error": None})
    argv = {"hide": ["work", "hide", "42"], "unhide": ["work", "unhide", "42"],
            "same": ["work", "same", "atlas#one", "42"], "not-same": ["work", "not-same", "atlas#one", "42"],
            "import": ["ledger", "import", str(tmp_path / "t.json"), "--project", "atlas", "--json"],
            "catch-up": ["ledger", "import", str(tmp_path / "t.json"), "--project", "atlas", "--catch-up",
                         "--json"]}[verb]
    out, _err = _run(argv)
    assert calls == [] and json.loads(out)["ok"] is False
    assert work.read_links() == {"same": [], "not_same": []} and work.read_hidden() == {}


def test_a_typed_yes_on_a_terminal_counts_as_routing_does(monkeypatch):
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["zsh"]})
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    ledger.require("work same")
    monkeypatch.setattr(sys, "stdin", io.StringIO("no\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with pytest.raises(ledger.LedgerError):
        ledger.require("work same")


def test_a_from_app_claim_from_a_shell_is_refused_even_with_a_typed_yes(monkeypatch):
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["zsh"]})
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    for command in ("ledger import", "ledger restore", "ledger purge", "ledger set"):
        with pytest.raises(ledger.LedgerError, match="only the Khipu app"):
            ledger.require(command, from_app=True)


# Ported: work same stored an owner decision without the owner check.
def test_same_answer_is_owner_only(monkeypatch):
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["shell"]})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    args = cli.build_parser().parse_args(["work", "same", "atlas#one", "42"])
    with redirect_stdout(io.StringIO()):
        assert args.func(args) == 1
    assert work.read_links()["same"] == []


# ---- 4. path words around a link -------------------------------------------------------------


@pytest.mark.parametrize("value, gone, kept", [
    ("source/private.txt=https://example.com", "source/private.txt", "https://example.com"),
    ("(https://example.com/a)/opt/x/y.txt", "/opt/x/y.txt", "https://example.com/a"),
    ("key=/opt/secret/file.txt;https://example.com", "/opt/secret", "https://example.com"),
    ("see https://example.com/a/b/c.txt", None, "https://example.com/a/b/c.txt"),
])
def test_a_link_is_cut_by_its_span_and_the_rest_still_checked(value, gone, kept):
    out = agent_view.project(value)
    if gone:
        assert gone not in out, out
        assert work._private_text(value)
    assert kept in out, out


# Ported.
def test_projection_cuts_path_words_even_when_token_contains_link():
    value = "source/private.txt=https://example.com"
    assert "source/private.txt" not in agent_view.project(value)


# ---- 5. a catch-up compares the whole ordered history ----------------------------------------


# Ported: reordered history alone read as nothing new.
def test_history_reorder_only_is_not_reported_as_nothing_new(monkeypatch):
    original = {"id": "one", "title": "Title", "state": "QUEUED", "kind": "slice",
                "history": [{"at": "2026-10-15T10:00:00Z", "event": "First"},
                            {"at": "2026-10-15T11:00:00Z", "event": "Second"}]}
    changed = copy.deepcopy(original)

    class Cursor:
        def execute(self, sql, params): self.sql = sql
        def fetchall(self):
            return [] if self.sql.startswith("SELECT DISTINCT") else [("one", ledger_import._row_digest(original))]

    monkeypatch.setattr(ledger_import, "_snapshot", lambda *a: {"rows": {"one": original}})
    for history in (list(reversed(original["history"])), original["history"][1:],
                    [original["history"][0], {"at": "2026-10-15T11:00:00Z", "event": "Edited"}]):
        changed["history"] = history
        result = ledger_import._catch_up(Cursor(), "atlas", {"rows": {"one": changed}}, {},
                                         {"one": None}, {}, ledger.default_vocab(), dry_run=False)
        assert result["result"] == "refused" and result["reason"] == "history_rewritten", result


# Ported: reordered history plus a changed field passed the check.
def test_verifier_rejects_reordered_history_when_fields_change(monkeypatch):
    history = [{"at": "2026-10-15T10:00:00Z", "event": "First"},
               {"at": "2026-10-15T11:00:00Z", "event": "Second"}]
    row = {"id": "one", "title": "Title", "state": "QUEUED", "kind": "slice", "history": history}
    raw = [{"has_raw": True, "raw": h, "op_id": None} for h in history]
    stored = {"seq": 1, "created_at": "2026-10-15T10:00:00Z", "link": "",
              "removed_at": None, "removed_by": None, "removed_note": "", "refs": {}}
    before = {"ids": {"one"}, "all": {"one": stored}, "rows": {"one": row}, "hist": {"one": raw}}
    source = copy.deepcopy(row)
    source["history"].reverse()
    source["title"] = "Changed title"
    after = copy.deepcopy(before)
    after["rows"]["one"]["title"] = "Changed title"
    after["hist"]["one"].append({"has_raw": False, "op_id": ledger_import.CATCH_UP_OP + "7"})
    monkeypatch.setattr(ledger_import, "_snapshot", lambda *a: after)
    result = ledger_import._verify_catch_up(None, "atlas", {"one": source},
                                            {"new_rows": [], "updated": ["one"], "both": []}, {}, before)
    assert result["ok"] is False, result


def test_verifier_checks_every_rows_history_not_only_the_touched(monkeypatch):
    history = [{"at": "2026-10-15T10:00:00Z", "event": "First"}]
    row = {"id": "two", "title": "Title", "state": "QUEUED", "kind": "slice", "history": history}
    stored = {"seq": 1, "created_at": None, "link": "", "removed_at": None, "removed_by": None,
              "removed_note": "", "refs": {}}
    before = {"ids": {"two"}, "all": {"two": stored}, "rows": {"two": row},
              "hist": {"two": [{"has_raw": True, "raw": history[0], "op_id": None}]}}
    monkeypatch.setattr(ledger_import, "_snapshot", lambda *a: copy.deepcopy(before))
    source = dict(row, history=history + [{"at": "2026-10-15T12:00:00Z", "event": "Not brought in"}])
    result = ledger_import._verify_catch_up(None, "atlas", {"two": source},
                                            {"new_rows": [], "updated": [], "both": []}, {}, before)
    assert result["ok"] is False and "two.history" in " ".join(result["fields_differ"]), result


# Ported, against the scratch PostgreSQL.
def test_reordered_history_only_catchup_refuses(hub, tmp_path):
    doc = _ti._tracker(8)
    path = _ti._write(tmp_path, doc)
    assert _ti._import(path)[1]["result"] == "done"
    first = next(iter(doc["rows"]))
    doc["rows"][first]["history"].reverse()
    path.write_text(json.dumps(doc))
    rc, report = _ti._import(path, "--catch-up")
    assert rc == 1 and report["result"] == "refused" and report["reason"] == "history_rewritten", report


def test_reordered_history_with_field_change_changes_nothing(hub, tmp_path):
    doc = _ti._tracker(8)
    path = _ti._write(tmp_path, doc)
    assert _ti._import(path)[1]["result"] == "done"
    before = (_tl._sql(hub, "SELECT row_id, title, state FROM ledger_rows ORDER BY row_id"),
              _tl._sql(hub, "SELECT count(*) FROM ledger_history"))
    first = next(iter(doc["rows"]))
    doc["rows"][first]["history"].reverse()
    doc["rows"][first]["title"] = "A changed title"
    path.write_text(json.dumps(doc))
    rc, report = _ti._import(path, "--catch-up")
    assert rc == 1 and report["result"] in ("refused", "rolled back"), report
    assert (_tl._sql(hub, "SELECT row_id, title, state FROM ledger_rows ORDER BY row_id"),
            _tl._sql(hub, "SELECT count(*) FROM ledger_history")) == before
