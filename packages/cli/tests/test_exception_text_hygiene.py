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
SINKS = {"_log", "_memo", "write", "print", "note_unreachable", "log",
         "debug", "info", "warning", "error", "exception", "critical"}
# Any method called on one of these receivers is a logging sink.
LOG_RECEIVERS = {"logging", "logger", "log"}
# Names that hold an exception; a name assigned from one is tainted.
EXC_SEEDS = {"exc", "e", "err"}
# Counts, durations and fixed words: values the code builds itself.
SAFE_NAMES = {"ms", "n", "count", "kind", "reason", "tag", "stamp", "elapsed", "total"}
SAFE = re.compile(r"#\s*text-safe:\s*\S.{19,}")


def _sink_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id if f.id in SINKS else ""
    if isinstance(f, ast.Attribute):
        root = f.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if f.attr in SINKS or (isinstance(root, ast.Name) and root.id in LOG_RECEIVERS):
            return f.attr
    return ""


def _mentions(node: ast.AST, names: set[str]) -> bool:
    """Whether ``node`` reads a name in ``names``, ignoring ``type(x).__name__``
    and ``len(...)``, which carry no text."""
    if _is_type_name(node):
        return False
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len"):
        return False
    if isinstance(node, ast.Name):
        return node.id in names
    return any(_mentions(c, names) for c in ast.iter_child_nodes(node))


def _tainted_names(scope: ast.AST) -> set[str]:
    """Names in ``scope`` (a function or module) that hold exception text: the
    seeds, every ``except ... as X`` name, and any name assigned (simple,
    annotated, augmented, walrus) from a tainted expression, transitively."""
    tainted = set(EXC_SEEDS)
    tainted |= {n.name for n in ast.walk(scope) if isinstance(n, ast.ExceptHandler) and n.name}
    pairs = []
    for n in ast.walk(scope):
        if isinstance(n, ast.Assign):
            pairs += [(t, n.value) for t in n.targets]
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)) and n.value is not None:
            pairs.append((n.target, n.value))
    changed = True
    while changed:
        changed = False
        for target, value in pairs:
            if _mentions(value, tainted):
                for t in ast.walk(target):
                    if isinstance(t, ast.Name) and t.id not in tainted:
                        tainted.add(t.id)
                        changed = True
    return tainted


def _is_type_name(node: ast.AST) -> bool:
    """``type(x).__name__`` with a plain name inside, or ``name.__name__``."""
    if not (isinstance(node, ast.Attribute) and node.attr == "__name__"):
        return False
    v = node.value
    return isinstance(v, ast.Name) or (
        isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "type"
        and len(v.args) == 1 and not v.keywords and isinstance(v.args[0], ast.Name))


def _allowed(node: ast.AST, tainted: frozenset[str] = frozenset()) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool, type(None)))
    if isinstance(node, ast.JoinedStr):
        return all(_allowed(v, tainted) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return _allowed(node.value, tainted) and (
            node.format_spec is None or _allowed(node.format_spec, tainted))
    if isinstance(node, ast.Name):
        return node.id in SAFE_NAMES and node.id not in tainted
    if _is_type_name(node):
        return True
    if isinstance(node, ast.Subscript):
        return (isinstance(node.value, ast.Name) and node.value.id in SAFE_NAMES
                and node.value.id not in tainted
                and isinstance(node.slice, ast.Constant))
    if isinstance(node, ast.Call):
        return (isinstance(node.func, ast.Name) and node.func.id == "len"
                and len(node.args) == 1 and not node.keywords)
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)) and _allowed(node.left, tainted) and _allowed(node.right, tainted)
    return False


def _offences_in(src: str, name: str) -> list[str]:
    lines = src.splitlines()
    out = []
    tree = ast.parse(src)
    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        tainted = frozenset(_tainted_names(scope))
        # Direct statements only, so a nested function is judged by its own scope.
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stack.extend(ast.iter_child_nodes(node))
            if isinstance(node, ast.Call) and (sink := _sink_name(node)):
                args = [*node.args, *(k.value for k in node.keywords)]
                body = "\n".join(lines[node.lineno - 1:node.end_lineno])
                if not all(_allowed(a, tainted) for a in args) and not SAFE.search(body):
                    out.append(f"{name}:{node.lineno} {sink}(...)")
    return sorted(out)


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
    "logger.log": "def f(exc):\n    logger.log(40, 'failed: %s', exc)\n",
    "logging.log": "def f(exc):\n    logging.log(40, str(exc))\n",
    "tainted allowlisted name": "def f(exc):\n    reason = str(exc)\n    logger.error(reason)\n",
    "transitive name chain": "def f(exc):\n    a = str(exc)\n    b = a\n    log(b)\n",
    "transitive f-string": "def f(exc):\n    msg = f'{exc}'\n    _log(msg)\n",
    "tainted augmented": "def f(exc):\n    reason = ''\n    reason += repr(exc)\n    _memo(reason)\n",
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
