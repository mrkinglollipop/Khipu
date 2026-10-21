"""One rule says when an error means the hub could not be reached
(khipu.hub_errors). Only connection-level failures count; a statement timeout
or any other error from a hub that answered is a real failure. The source scan
fails when a module keeps its own "connect"/"timeout" string match."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from khipu import hub_errors

PACKAGE = Path(hub_errors.__file__).resolve().parent


@pytest.mark.parametrize("text", [
    "OperationalError",
    "mirror OperationalError",
    "OperationalError: connection to server at \"hub\" failed: Connection refused",
    "ConnectionTimeout: connection timeout expired",
    "InterfaceError: the connection is closed",
    "RuntimeError: could not translate host name \"hub\" to address",
    "TimeoutError: timed out",
    "RuntimeError: no khipu dsn configured",
])
def test_connection_failures_are_offline(text):
    assert hub_errors.unreachable_text(text)


@pytest.mark.parametrize("text", [
    "",
    None,
    "QueryCanceled",
    "QueryCanceled: canceling statement due to statement timeout",
    "OperationalError: canceling statement due to statement timeout",
    "ValueError: invalid connect adapter",
    "UndefinedColumn: column \"x\" does not exist",
    "DiskFull: could not extend file",
    "AdminShutdown: terminating connection due to administrator command",
])
def test_errors_from_a_hub_that_answered_are_not_offline(text):
    assert not hub_errors.unreachable_text(text)


def test_a_live_exception_uses_the_same_rule():
    class QueryCanceled(Exception):
        pass

    assert not hub_errors.unreachable_error(QueryCanceled("canceling statement due to statement timeout"))
    assert hub_errors.unreachable_error(ConnectionRefusedError("refused"))


# Words an offline classification matches on.
_WORDS = ("connect", "timeout", "timed out", "refused", "operational", "unreachable", "name resolution")
# Modules allowed a match of their own: module -> why.
OWN_MATCH = {
    "hub_errors.py": "the rule itself",
    "read_role.py": "spots a full read role (a hub that answered), which must never read as offline",
    "setup.py": "turns a connection test's error into advice for the owner; classifies nothing as offline",
    "join_pair.py": "the pairing socket between two Macs, not the hub",
    "join.py": "explains a database address that could not be parsed; classifies nothing as offline",
}


def _string_matches(tree: ast.AST) -> list[int]:
    """Lines where a string literal holding one of ``_WORDS`` is tested with
    ``in`` against text, directly or as a loop over a tuple of words. A
    module spotting its own statement timeout (a hub that answered) is not
    an offline match."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.In) for op in node.ops):
            left = node.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str) \
                    and "statement" not in left.value.lower() \
                    and any(w in left.value.lower() for w in _WORDS):
                hits.append(node.lineno)
        if isinstance(node, ast.comprehension) and isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
            words = [e.value for e in node.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if any(w in v.lower() for v in words for w in _WORDS):
                hits.append(node.iter.lineno)
    return hits


def test_no_module_keeps_its_own_offline_string_match():
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        hits = _string_matches(ast.parse(path.read_text(encoding="utf-8")))
        if hits and path.name not in OWN_MATCH:
            found[str(path.relative_to(PACKAGE))] = hits
    assert not found, f"classify offline errors with khipu.hub_errors, not a string match: {found}"


def test_the_outbox_classifies_with_the_rule():
    source = (PACKAGE / "outbox.py").read_text(encoding="utf-8")
    assert source.count("unreachable_text(") >= 1 and source.count("unreachable_error(") >= 1
