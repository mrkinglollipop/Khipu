"""Exception text can carry the recall query (a driver echoes the statement or
the parameters). In the recall prompt path and the nightly eval, no log, memo,
write or print call may take an exception's text; log the type name only. A
call that is safe for a stated reason carries ``# text-safe: <reason>``."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "khipu"
FILES = ("recall_prompt.py", "recall_eval_nightly.py")
SINKS = {"_log", "_memo", "write", "print", "note_unreachable",
         "debug", "info", "warning", "error", "exception", "critical"}
TEXT = re.compile(r"\{(?:exc|e|err)(?:[!:}]|\.args)|\bstr\((?:exc|e|err)\)|\brepr\((?:exc|e|err)\)"
                  r"|\b(?:exc|e|err)\.args\b")
SAFE = re.compile(r"#\s*text-safe:\s*\S")


def _sink_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _offences(name: str) -> list[str]:
    src = (ROOT / name).read_text(encoding="utf-8")
    lines = src.splitlines()
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and (sink := _sink_name(node)) in SINKS:
            span = lines[node.lineno - 1:node.end_lineno]
            body = "\n".join(span)
            if TEXT.search(body) and not SAFE.search(body):
                out.append(f"{name}:{node.lineno} {sink}(...)")
    return out


@pytest.mark.parametrize("name", FILES)
def test_no_exception_text_reaches_a_log_memo_write_or_print(name):
    assert _offences(name) == []


def test_the_scan_catches_a_new_offender_and_honours_the_marker(tmp_path, monkeypatch):
    bad = tmp_path / "x.py"
    bad.write_text("def f():\n    try:\n        pass\n    except Exception as exc:\n"
                   "        _log(f'oops {exc}')\n        print(str(exc))\n"
                   "        _log(f'ok {exc}')  # text-safe: fixed literal\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    assert [o.split()[0] for o in _offences("x.py")] == ["x.py:5", "x.py:6"]
