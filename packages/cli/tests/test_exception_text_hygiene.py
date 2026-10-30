"""Exception text can carry the recall query (a driver echoes the statement or
the parameters). In the recall prompt path and the nightly eval, a log, memo,
write or print call may take only what is provably free of owner text: string
literals, ``type(x).__name__``, ``len(...)``, a subscript of an allowed name,
and the names in ``SAFE_NAMES``. Any other interpolation, %-format, ``.format``
or logging argument fails, whatever the exception variable is called. A call
that is safe for a stated reason carries ``# text-safe: <reason>`` of at least
20 characters on one of its lines."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "khipu"
FILES = ("recall_prompt.py", "recall_eval_nightly.py")
SINKS = {"_log", "_memo", "write", "print", "note_unreachable",
         "debug", "info", "warning", "error", "exception", "critical"}
# Counts, durations and fixed words: values the code builds itself.
SAFE_NAMES = {"ms", "n", "count", "kind", "reason", "tag", "stamp", "elapsed", "total"}
SAFE = re.compile(r"#\s*text-safe:\s*\S.{19,}")


def _sink_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _is_type_name(node: ast.AST) -> bool:
    """``type(x).__name__`` with a plain name inside, or ``name.__name__``."""
    if not (isinstance(node, ast.Attribute) and node.attr == "__name__"):
        return False
    v = node.value
    return isinstance(v, ast.Name) or (
        isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "type"
        and len(v.args) == 1 and not v.keywords and isinstance(v.args[0], ast.Name))


def _allowed(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool, type(None)))
    if isinstance(node, ast.JoinedStr):
        return all(_allowed(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return _allowed(node.value) and (node.format_spec is None or _allowed(node.format_spec))
    if isinstance(node, ast.Name):
        return node.id in SAFE_NAMES
    if _is_type_name(node):
        return True
    if isinstance(node, ast.Subscript):
        return (isinstance(node.value, ast.Name) and node.value.id in SAFE_NAMES
                and isinstance(node.slice, ast.Constant))
    if isinstance(node, ast.Call):
        return (isinstance(node.func, ast.Name) and node.func.id == "len"
                and len(node.args) == 1 and not node.keywords)
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)) and _allowed(node.left) and _allowed(node.right)
    return False


def _offences_in(src: str, name: str) -> list[str]:
    lines = src.splitlines()
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and (sink := _sink_name(node)) in SINKS:
            args = [*node.args, *(k.value for k in node.keywords)]
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            if not all(_allowed(a) for a in args) and not SAFE.search(body):
                out.append(f"{name}:{node.lineno} {sink}(...)")
    return out


def _offences(name: str) -> list[str]:
    return _offences_in((ROOT / name).read_text(encoding="utf-8"), name)


@pytest.mark.parametrize("name", FILES)
def test_no_exception_text_reaches_a_log_memo_write_or_print(name):
    assert _offences(name) == []


BYPASSES = {
    "renamed exception": "def f():\n    try:\n        pass\n    except Exception as boom:\n        _log(f'x {boom}')\n",
    "percent format": "def f(exc):\n    _log('failed: %s' % (exc,))\n",
    "str.format": "def f(exc):\n    _log('failed: {}'.format(exc))\n",
    "logging args": "def f(exc):\n    logger.error('failed: %s', exc)\n",
    "plain interpolation": "def f(query):\n    print(f'looked for {query}')\n",
    "write of a variable": "def f(fh, text):\n    fh.write(text)\n",
}


@pytest.mark.parametrize("shape", sorted(BYPASSES))
def test_the_scan_rejects_each_known_bypass_shape(shape):
    assert _offences_in(BYPASSES[shape], "x.py"), shape


def test_the_scan_accepts_literals_type_names_and_counts():
    ok = ("def f(exc, ms, hits):\n"
          "    _log(f'slow {ms:.1f} {type(exc).__name__} {len(hits)}')\n"
          "    print('plain literal')\n")
    assert _offences_in(ok, "x.py") == []


def test_the_marker_needs_a_real_reason():
    bare = "def f(exc):\n    _log(f'x {exc}')  # text-safe: ok\n"
    good = "def f(exc):\n    _log(f'x {exc}')  # text-safe: exc is a fixed internal enum value here\n"
    assert _offences_in(bare, "x.py") and _offences_in(good, "x.py") == []
