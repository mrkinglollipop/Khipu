"""The owner's own items are changed only by the owner (fourth cross-family
review of docs/plans/2026-10-16-owed-and-ledger-ssot.md).

The invariant (Matt, 2026-10-17: "Suggest, don't close"): an item the user
owns is never closed, merged, hidden or reassigned by anything automatic,
by any rule. Every automatic rule is named in ``commitments.AUTOMATIC_RULES``
and every automatic write reads the owner under the row lock and passes
``commitments.owner_guard`` in ``close_by_rule``, ``auto_reassign`` or
``mark_stale``; a refused close lands in "Looks done". These tests enumerate
both sides: each rule against each owner, every place in the package that
writes a commitment's status or owner, and every automatic path run against
the owner's own item on a real hub.
"""
from __future__ import annotations

import ast
import json
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
    ("commitments", "stale_sql"): "aging after weeks of silence, only for the rows mark_stale's owner guard lets by",
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


def test_every_automatic_close_and_move_names_its_rule_and_never_an_earlier_owner():
    """The owner is read under the row lock at the write, so no caller may
    hand the write an owner it read earlier (a stale snapshot)."""
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
        assert "rule" in kw, f"{module}.py:{line} calls {name} without its rule"
        assert not kw & {"owner", "owner_before", "origin"}, f"{module}.py:{line} hands {name} an owner it read earlier"


@pytest.mark.parametrize("rule", sorted(co.AUTOMATIC_RULES))
def test_no_automatic_rule_changes_the_owners_item(rule):
    """Suggest, don't close (Matt, 2026-10-17): every automatic rule, a close
    on the item's own pull requests and his "done" in a conversation
    included, is refused for the owner's item and only suggests."""
    assert co.AUTOMATIC_RULES[rule] in (co.PROPOSE_LOOKS_DONE, co.PROPOSE_TIDY, co.PROPOSE_QUIET)
    why = co.owner_guard(co.OWNER_USER, rule)
    assert why and "suggest" in why, rule
    for owner in (co.OWNER_ASSISTANT, co.OWNER_AGENT, None):
        assert co.owner_guard(owner, rule) is None


def test_an_unknown_rule_is_refused():
    with pytest.raises(ValueError):
        co.owner_guard(co.OWNER_USER, "something-new")


# ---- the write itself: the owner as it is at the write ------------------------


class _RacingCursor:
    """The registry SQL in SQLite (tests/test_audit1010_rsna.py) with the
    close columns. ``move`` commits an owner-directed move of one item to
    the owner just before the automatic write's locked read (``"read"``) or
    just before its UPDATE (``"write"``: a writer outside a transaction holds
    no lock, so only the UPDATE's own check stands)."""

    def __init__(self):
        from tests.test_audit1010_rsna import _RegistryCursor

        self.inner = _RegistryCursor()
        for column in ("owner_reassigned_at TEXT", "closed_by TEXT", "close_rule_key TEXT",
                       "close_evidence TEXT", "reopened_rule_keys TEXT DEFAULT '[]'", "prs TEXT",
                       "settle_suggestion TEXT", "pr_check TEXT"):
            self.inner.conn.execute(f"ALTER TABLE commitments ADD COLUMN {column}")
        self.move: tuple[str, int] | None = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, sql, params=()):
        if self.move:
            when, cid = self.move
            if (when == "read" and re.search(r"FOR UPDATE", sql)) or \
                    (when == "write" and re.search(r"UPDATE\s+commitments\s+SET\s+(status|owner|settle)", sql)):
                self.move = None
                self.inner.conn.execute("UPDATE commitments SET owner = 'user', "
                                        "owner_reassigned_at = CURRENT_TIMESTAMP WHERE id = ?", (cid,))
        sql = sql.replace("NOT (%s = ANY(reopened_rule_keys))",
                          "NOT EXISTS (SELECT 1 FROM json_each(reopened_rule_keys) AS k WHERE k.value = %s)")
        self.inner.execute(sql, params)

    def suggestion(self, cid):
        raw = self.inner.value("commitments", cid, "settle_suggestion")
        return json.loads(raw) if raw else None


@pytest.fixture
def racing(monkeypatch):
    from khipu import db

    db._TABLE_COLUMNS_CACHE.clear()
    cur = _RacingCursor()
    yield cur
    cur.inner.conn.close()
    db._TABLE_COLUMNS_CACHE.clear()


@pytest.mark.parametrize("when", ["read", "write"])
@pytest.mark.parametrize("rule", sorted(r for r in co.AUTOMATIC_RULES if r != "stale"))
def test_an_automatic_close_reads_the_owner_at_the_write(racing, rule, when):
    """A close the rule chose while the item was the assistant's finds the
    owner took it (round 5: a stale owner snapshot closed his item)."""
    cid = racing.commitment("Rebuild the deployment index")
    racing.move = (when, cid)
    assert not co.close_by_rule(racing, cid, rule=rule, reason="synthetic automatic rule", key=f"{rule}:1")
    assert racing.value("commitments", cid, "status") == "open"
    assert racing.value("commitments", cid, "owner") == "user"
    if when == "read":
        # Refused at the read: a close of his item becomes a suggestion.
        got = racing.suggestion(cid)
        if co.AUTOMATIC_RULES[rule] == co.PROPOSE_LOOKS_DONE:
            assert got["looks_done"] and got["rule"] == rule and got["reason"] == "synthetic automatic rule"
        else:
            assert got is None


@pytest.mark.parametrize("when", ["read", "write"])
@pytest.mark.parametrize("rule", ["owner-rules", "model"])
def test_an_automatic_reassign_reads_the_owner_at_the_write(racing, rule, when):
    cid = racing.commitment("Rebuild the deployment index")
    racing.move = (when, cid)
    assert not co.auto_reassign(racing, cid, rule=rule, sets=["owner = %s"], params=("agent",))
    assert racing.value("commitments", cid, "owner") == "user"


@pytest.mark.parametrize("when", ["read", "write"])
def test_staleness_reads_the_owner_at_the_write(racing, when):
    cid = racing.commitment("Rebuild the deployment index", age=60)
    racing.move = (when, cid)
    assert co.mark_stale(racing) == 0
    assert racing.value("commitments", cid, "owner") == "user"
    assert racing.value("commitments", cid, "status") == "open"


@pytest.mark.parametrize("rule", sorted(r for r in co.AUTOMATIC_RULES if r != "stale"))
def test_someone_elses_item_still_closes_and_moves(racing, rule):
    cid = racing.commitment("Rebuild the deployment index")
    assert co.close_by_rule(racing, cid, rule=rule, reason="r", key="k")
    assert racing.value("commitments", cid, "status") == "closed"
    other = racing.commitment("Rebuild the search index")
    assert co.auto_reassign(racing, other, rule=rule, sets=["owner = %s"], params=("agent",))
    assert racing.value("commitments", other, "owner") == "agent"


def test_a_suggestion_is_not_rewritten_for_the_same_evidence_nor_after_a_reopen(racing):
    cid = racing.commitment("Rebuild the deployment index", owner=co.OWNER_USER)
    assert not co.close_by_rule(racing, cid, rule="pr", reason="pull request #3 merged", key="pr:3=merged")
    assert racing.suggestion(cid)["key"] == "pr:3=merged"
    assert not co.suggest_looks_done(racing, cid, rule="pr", reason="again", key="pr:3=merged")
    racing.inner.conn.execute("UPDATE commitments SET settle_suggestion = NULL, reopened_rule_keys = ? "
                              "WHERE id = ?", (json.dumps(["pr:3=merged"]), cid))
    assert not co.close_by_rule(racing, cid, rule="pr", reason="pull request #3 merged", key="pr:3=merged")
    assert racing.suggestion(cid) is None


def test_staleness_never_hides_the_owners_item_and_lists_it_as_quiet():
    """Weeks of silence stale anyone else's item; the owner's stays open and
    lists after his active ones as "Quiet for N weeks" (the stale rule)."""
    from tests.test_audit1010_rsna import _RegistryCursor
    from khipu import db

    cur = _RegistryCursor()
    try:
        db._TABLE_COLUMNS_CACHE.clear()
        mine = cur.commitment("User must approve the release", owner=co.OWNER_USER, kind="blocker", age=50)
        quiet_followup = cur.commitment("User must reply to the reviewer", owner=co.OWNER_USER, age=22)
        active = cur.commitment("User must read the plan", owner=co.OWNER_USER, age=2)
        theirs = cur.commitment("write the release notes", owner=co.OWNER_ASSISTANT, age=50)
        assert co.mark_stale(cur) == 1
        assert cur.value("commitments", theirs, "status") == "stale"
        for cid in (mine, quiet_followup, active):
            assert cur.value("commitments", cid, "status") == "open"

        rows = co.list_owed(cur, project="p")
        # His active item, then his quiet ones, then everyone else's.
        assert [r["id"] for r in rows][:3] == [active, mine, quiet_followup]
        by_id = {r["id"]: r for r in rows}
        assert by_id[active]["quiet_weeks"] is None and by_id[active]["group"] is None
        assert by_id[mine]["group"] == "Quiet for 7 weeks"
        assert by_id[quiet_followup]["group"] == "Quiet for 3 weeks"

        db._TABLE_COLUMNS_CACHE.clear()
        cur.execute("SELECT id, text, project, owner, kind, opened_episode, opened_at, due_after, status, "
                    "closed_episode, closed_at, close_reason, last_seen_at, seen_count FROM commitments "
                    "WHERE status = 'open'")
        cols = ["id", "text", "project", "owner", "kind", "opened_episode", "opened_at", "due_after", "status",
                "closed_episode", "closed_at", "close_reason", "last_seen_at", "seen_count"]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
        first = co.page_owed([dict(r) for r in raw], owner=co.OWNER_USER, limit=2)
        assert [r["id"] for r in first["results"]][0] == active
        rest = co.page_owed([dict(r) for r in raw], owner=co.OWNER_USER, cursor=first["next_cursor"], limit=2)
        seen = [r["id"] for r in first["results"] + rest["results"]]
        assert sorted(seen) == sorted([mine, quiet_followup, active]) and len(seen) == 3
        assert all(r["quiet_weeks"] for r in rest["results"])
    finally:
        cur.conn.close()


def test_a_snoozed_or_someone_elses_item_is_never_quiet():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).isoformat()
    row = {"owner": co.OWNER_USER, "kind": "question", "status": "open", "opened_at": old}
    assert co.quiet_weeks(row, now) == 8
    assert co.quiet_weeks({**row, "owner": co.OWNER_AGENT}, now) is None
    assert co.quiet_weeks({**row, "due_after": (now + timedelta(days=1)).isoformat()}, now) is None
    assert co.quiet_weeks({**row, "last_seen_at": (now - timedelta(days=3)).isoformat()}, now) is None
    assert co.quiet_weeks({**row, "status": "closed"}, now) is None
