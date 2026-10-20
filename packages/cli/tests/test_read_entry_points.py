"""Every hub connect in the modules that serve search and the other read
tools either takes the read role (``db.read_connection`` or ``role="read"``)
or is listed here with why it needs the owner. A new bare ``connect()`` in
these modules fails this test until it is one or the other
(docs/plans/2026-10-19-swarm-ready.md, slice 2)."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import khipu

PACKAGE = Path(khipu.__file__).resolve().parent
MODULES = ("cli.py", "mcp_server.py", "recall_prompt.py", "activity.py", "library_search.py")

# (module, top-level function) -> (bare owner connects, why).
OWNER_CONNECTS = {
    ("cli.py", "_cmd_embed_profiles"): (1, "manages embedding profiles (writes)"),
    ("cli.py", "_cmd_embed_index"): (1, "builds vector indexes (writes)"),
    ("cli.py", "_cmd_embed_profile_delete"): (1, "deletes a profile (writes)"),
    ("cli.py", "cmd_library_backfill_job"): (1, "library backfill (writes)"),
    ("cli.py", "cmd_library"): (1, "library import and upkeep (writes)"),
    ("cli.py", "cmd_owed_tidy"): (1, "closes stale commitments (writes)"),
    ("cli.py", "cmd_decisions"): (6, "supersede/restore/retract/confirm/reject/backfill write; list reads"),
    ("cli.py", "_cmd_episode_edit"): (1, "edits an episode (writes)"),
    ("cli.py", "cmd_topic"): (1, "topic edits (writes)"),
    ("cli.py", "cmd_backfill"): (1, "backfill (writes)"),
    ("cli.py", "cmd_project"): (1, "project renames (writes)"),
    ("cli.py", "cmd_project_alias"): (1, "project aliases (writes)"),
    ("cli.py", "cmd_hygiene_commitments"): (1, "commitment hygiene (writes)"),
    ("cli.py", "cmd_hygiene"): (1, "hygiene (writes)"),
    ("cli.py", "cmd_notes"): (3, "note reconcile (writes)"),
    ("cli.py", "cmd_db"): (1, "migrations and owner database tools"),
    ("mcp_server.py", "_owed_update_on_hub"): (1, "closes/reopens/snoozes a commitment (writes)"),
    ("mcp_server.py", "_owed_change_on_hub"): (1, "changes commitments (writes)"),
    ("mcp_server.py", "_tool_forget"): (1, "forget: the episode read guards the write that follows"),
    ("mcp_server.py", "_decisions_update_on_hub"): (1, "decision updates (writes)"),
}


def _bare_connects(path: Path) -> Counter:
    found: Counter = Counter()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(top):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute) and getattr(func.value, "id", None) in ("db", "hub_snapshot"):
                name = func.attr
            else:
                continue  # sqlite3.connect, psycopg.connect and the like
            if name not in ("connect", "try_hub_connect"):
                continue
            if any(kw.arg == "role" for kw in node.keywords):
                continue  # names its role on purpose
            found[top.name] += 1
    return found


def test_every_owner_connect_in_the_read_modules_is_listed_with_its_reason():
    seen: dict[tuple[str, str], int] = {}
    for module in MODULES:
        for func, count in _bare_connects(PACKAGE / module).items():
            seen[(module, func)] = count
    unlisted = {k: v for k, v in seen.items() if k not in OWNER_CONNECTS}
    assert not unlisted, (
        f"owner connects not listed (a read should use db.read_connection or role='read'): {unlisted}")
    grown = {k: (v, OWNER_CONNECTS[k][0]) for k, v in seen.items() if k in OWNER_CONNECTS
             and v > OWNER_CONNECTS[k][0]}
    assert not grown, f"more owner connects than listed (found, listed): {grown}"
    stale = sorted(set(OWNER_CONNECTS) - set(seen))
    assert not stale, f"listed but gone; remove from OWNER_CONNECTS: {stale}"
