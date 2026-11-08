"""Pin the remaining explicit commitment writers and archival Undo paths."""
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
    ("commitments", "set_status"): "the owner's own close, reopen or snooze",
    ("commitments", "reassign"): "the owner's own move",
    ("forget", "tombstone_on_hub"): "the owner's own forget (and the carriers it refuses)",
    ("owed_changes", "_put"): "the owner's own undo or redo of a batch, behind the owner gate",
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


def test_the_known_writers_are_all_still_there():
    # A writer that moved would otherwise leave the list above stale.
    missing = set(WRITERS) - _writes()
    assert not missing, missing






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


# ---- the stored owner decides, never an owner inferred again --------------------
# Third confirmation review: a legacy item stored as the user's whose text the
# owner rules now read as the assistant's ("Test the installation after the
# reviewer finishes", a deferred promise) was closed, staled and reassigned by
# the automatic rules, because the guard read the inferred owner. Inference is
# for an item's first capture; once stored, the owner moves only by the owner.

_LEGACY = "Test the installation after the reviewer finishes"


def _legacy_users_item(cur, **kw) -> int:
    cid = cur.commitment(_LEGACY, owner=co.OWNER_USER, kind="promise", **kw)
    cur.inner.conn.execute("UPDATE commitments SET future_trigger = 1 WHERE id = ?", (cid,))
    return cid


def test_the_legacy_item_reads_as_the_users_whatever_its_text_says(racing):
    # A read gives the stored owner, not one worked out from the words, so the
    # guard sees the same owner on every Mac. The tests below hold the guard
    # to the stored owner itself.
    cid = _legacy_users_item(racing)
    row = co.row_at_write(racing, cid)
    assert row["stored_owner"] == co.OWNER_USER and row["owner"] == co.OWNER_USER


# ---- every close of someone else's item waits on every pull request -----------




