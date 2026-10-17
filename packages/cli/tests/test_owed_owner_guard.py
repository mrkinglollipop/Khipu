"""The owner's own items are changed only by the owner (fourth cross-family
review of docs/plans/2026-10-16-owed-and-ledger-ssot.md).

The invariant: an item the user owns is never closed, merged, hidden or
reassigned by anything automatic. Every automatic rule is named in
``commitments.AUTOMATIC_RULES`` and every automatic write passes
``commitments.owner_guard`` in ``close_by_rule`` or ``auto_reassign``. These
tests enumerate both sides: each rule against each owner, and every place in
the package that writes a commitment's status or owner.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from khipu import commitments as co

PACKAGE = Path(co.__file__).parent

# Every function that may write a commitment's status or owner, and why.
WRITERS = {
    ("commitments", "close_by_rule"): "the one automatic close, behind the owner guard",
    ("commitments", "auto_reassign"): "the one automatic owner change, behind the owner guard",
    ("commitments", "set_status"): "the owner's own close, reopen or snooze",
    ("commitments", "reassign"): "the owner's own move",
    ("commitments", "stale_sql"): "aging after weeks of silence (the scope's rule); stale stays listed and reopens",
    ("forget", "forget_episode"): "the owner's own forget",
    ("owed_tidy", "undo"): "the owner's own undo of a batch, behind the owner gate",
    ("hygiene", None): "the restore recipe in a backup's RESTORE.md, text only",
}
# Only the SET list counts: a WHERE that reads the status writes nothing. A
# SET list built at run time ("SET {}") counts as a write.
_WRITE_RE = re.compile(r"UPDATE\s+commitments\b(?:\s+\w+)?\s+SET\b(?:\s*\{\}|(?:(?!\bWHERE\b).)*?"
                       r"\b(?:status|owner|owner_label)\s*=)",
                       re.I | re.S)


def _strings(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return ""


def _writes() -> set[tuple[str, str | None]]:
    """(module, enclosing function) of every SQL string literal that sets a
    commitment's status or owner."""
    found: set[tuple[str, str | None]] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, func):
            for child in ast.iter_child_nodes(node):
                inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
                text = _strings(child)
                if text and _WRITE_RE.search(text):
                    found.add((path.stem, func))
                elif isinstance(child, ast.BinOp):
                    # "UPDATE ... " + "SET status = ..." split over a concatenation.
                    parts = [_strings(n) for n in ast.walk(child)]
                    if _WRITE_RE.search("".join(parts)):
                        found.add((path.stem, func))
                walk(child, inner)

        walk(tree, None)
    return found


def test_every_status_or_owner_write_is_a_known_writer():
    unknown = _writes() - set(WRITERS)
    assert not unknown, (f"a new place writes a commitment's status or owner: {sorted(unknown, key=str)}. "
                         "Route an automatic change through commitments.close_by_rule or auto_reassign.")


def test_the_known_writers_are_all_still_there():
    # A writer that moved would otherwise leave the list above stale.
    missing = set(WRITERS) - _writes()
    assert not missing, missing


def test_every_automatic_close_and_move_names_its_rule_and_owner():
    calls = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in ("close_by_rule", "auto_reassign"):
                    kw = {k.arg for k in node.keywords}
                    calls.append((path.stem, node.lineno, name, kw))
    assert calls
    for module, line, name, kw in calls:
        need = {"rule", "owner"} if name == "close_by_rule" else {"rule", "owner_before"}
        assert need <= kw, f"{module}.py:{line} calls {name} without {sorted(need - kw)}"


@pytest.mark.parametrize("rule", sorted(co.AUTOMATIC_RULES))
def test_no_automatic_rule_changes_the_owners_item_except_his_own(rule):
    for origin in (None, co.ORIGIN_JOB, co.ORIGIN_CONVERSATION):
        why = co.owner_guard(co.OWNER_USER, rule, origin=origin)
        if rule == "pr":
            assert why is None, "a close on the item's own pull requests is allowed"
        elif rule == "loop" and origin == co.ORIGIN_CONVERSATION:
            assert why is None, "the owner's own done in his own conversation is allowed"
        else:
            assert why and "propose" in why, (rule, origin)
    for owner in (co.OWNER_ASSISTANT, co.OWNER_AGENT, None):
        assert co.owner_guard(owner, rule) is None


def test_an_unknown_rule_is_refused():
    with pytest.raises(ValueError):
        co.owner_guard(co.OWNER_USER, "something-new")


class _Cur:
    def __init__(self):
        self.sql = []
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.sql.append(sql)


@pytest.mark.parametrize("rule", sorted(r for r in co.AUTOMATIC_RULES if r != "pr"))
def test_the_one_close_and_the_one_move_write_nothing_for_the_owners_item(monkeypatch, rule):
    monkeypatch.setattr(co, "_close_columns_ready", lambda cur: True)
    monkeypatch.setattr(co, "_not_forgotten_clause", lambda cur: "true")
    cur = _Cur()
    assert co.close_by_rule(cur, 7, rule=rule, owner=co.OWNER_USER, origin=co.ORIGIN_JOB, reason="r", key="k") \
        is False
    assert co.auto_reassign(cur, 7, rule=rule, owner_before=co.OWNER_USER, sets=["owner = %s"],
                            params=("agent",)) is False
    assert cur.sql == []
    # The same rule on someone else's item writes.
    assert co.close_by_rule(cur, 7, rule=rule, owner=co.OWNER_ASSISTANT, reason="r", key="k") is True
    assert co.auto_reassign(cur, 7, rule=rule, owner_before=co.OWNER_ASSISTANT, sets=["owner = %s"],
                            params=("agent",)) is True
    assert len(cur.sql) == 2


def test_a_pr_close_reaches_the_owners_item(monkeypatch):
    monkeypatch.setattr(co, "_close_columns_ready", lambda cur: True)
    monkeypatch.setattr(co, "_not_forgotten_clause", lambda cur: "true")
    cur = _Cur()
    assert co.close_by_rule(cur, 7, rule="pr", owner=co.OWNER_USER, reason="pull request #3 merged",
                            key="pr:3=merged")
    assert "status = 'closed'" in cur.sql[0]
