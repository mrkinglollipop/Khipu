"""One project, many names: the conversation side of repo renames.

Scope: docs/plans/2026-10-19-repo-renames.md, slice D (conversation part).

* ``show``: the answer behind ``khipu_project`` and ``khipu project show``:
  the canonical name, every name in the group with where it came from, what
  each holds, and the "same project?" pairs involving the group.
* The owner actions (``alias_add``, ``alias_remove``, ``merge``,
  ``keep_separate``) and their Undo, which the owner-direction gate logs and
  undoes the way it does every other owner action.
* ``asked_pairs``: the pairs the session-start slice asks the owner about.

Groups are read through ``project_aliases`` exactly as the other readers
read them (``identity.project_canonical_group``); nothing here rewrites a
stored row's project name.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from khipu import identity

ACTION_ALIAS_ADD = "project.alias_add"
ACTION_ALIAS_REMOVE = "project.alias_remove"
ACTION_MERGE = "project.merge"
ACTION_KEEP_SEPARATE = "project.keep_separate"
ACTIONS = (ACTION_ALIAS_ADD, ACTION_ALIAS_REMOVE, ACTION_MERGE, ACTION_KEEP_SEPARATE)

ASKED, MERGED, KEPT = "asked", "merged", "kept_separate"
_PAIR_COLS = "a, b, root_commit, state, created_at, decided_at"


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _savepoint(cur, name: str, sql: str, params: tuple) -> list[tuple]:
    """A read that must never break the caller's transaction: ``[]`` when the
    table or column is not there (a hub older than migration 0039)."""
    try:
        cur.execute(f"SAVEPOINT {name}", ())
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute(f"RELEASE SAVEPOINT {name}", ())
        return rows
    except Exception:  # noqa: BLE001 - a missing table reads as no rows
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {name}", ())
            cur.execute(f"RELEASE SAVEPOINT {name}", ())
        except Exception:  # noqa: BLE001
            pass
        return []


def _pair_dict(r) -> dict:
    return {"a": r[0], "b": r[1], "root_commit": r[2], "state": r[3], "created_at": _iso(r[4]),
            "decided_at": _iso(r[5])}


# ---- reads ---------------------------------------------------------------------------------


def alias_rows(cur, names: list[str]) -> list[dict]:
    """Every alias row that has one of ``names`` as its alias or canonical."""
    rows = _savepoint(
        cur, "project_group_aliases",
        "SELECT alias, project, source, evidence, created_at FROM project_aliases "
        "WHERE alias = ANY(%s) OR project = ANY(%s) ORDER BY alias", (names, names))
    return [{"alias": r[0], "project": r[1], "source": r[2], "evidence": r[3], "created_at": _iso(r[4])}
            for r in rows]


def _counts(cur, names: list[str]) -> dict[str, dict[str, int]]:
    """Open commitments, standing decisions and episodes per name, counted
    the way the readers list them (a forgotten episode's rows do not count)."""
    from khipu import commitments, decisions
    from khipu.embed import _episode_schema_flags

    out = {n: {"open_commitments": 0, "standing_decisions": 0, "episodes": 0} for n in names}
    rows = _savepoint(
        cur, "project_group_commitments",
        f"SELECT project, count(*) FROM commitments WHERE status = 'open' AND project = ANY(%s) "
        f"AND {commitments._not_forgotten_clause(cur)} GROUP BY project", (names,))
    for project, n in rows:
        out[project]["open_commitments"] = int(n)
    clauses = ["project = ANY(%s)", "superseded_by IS NULL"]
    if decisions._evidence_ready(cur):
        clauses.append("retracted_at IS NULL")
    forgotten = decisions._not_forgotten_clause(cur, "decisions")
    if forgotten:
        clauses.append(forgotten)
    rows = _savepoint(cur, "project_group_decisions",
                      f"SELECT project, count(*) FROM decisions WHERE {' AND '.join(clauses)} GROUP BY project",
                      (names,))
    for project, n in rows:
        out[project]["standing_decisions"] = int(n)
    flags = _episode_schema_flags(cur)
    expr = "COALESCE(project, scope)" if flags["project"] else "scope"
    deleted = " AND deleted_at IS NULL" if flags["deleted_at"] else ""
    rows = _savepoint(cur, "project_group_episodes",
                      f"SELECT {expr}, count(*) FROM episodes WHERE {expr} = ANY(%s){deleted} GROUP BY {expr}",
                      (names,))
    for project, n in rows:
        out[project]["episodes"] = int(n)
    return out


def pair_rows(cur, names: list[str], *, state: str | None = None) -> list[dict]:
    """The ``project_pairs`` involving any of ``names``, asked ones first."""
    sql = f"SELECT {_PAIR_COLS} FROM project_pairs WHERE (a = ANY(%s) OR b = ANY(%s))"
    params: tuple = (names, names)
    if state:
        sql, params = sql + " AND state = %s", (*params, state)
    rows = _savepoint(cur, "project_group_pairs", sql + " ORDER BY (state = 'asked') DESC, state, created_at, a, b",
                      params)
    return [_pair_dict(r) for r in rows]


def _evidence_summary(source: str | None, evidence: dict | None, alias: str, canonical: str) -> str:
    ev = evidence if isinstance(evidence, dict) else {}
    old, new = ev.get("old") or alias, ev.get("new") or canonical
    root = str(ev.get("root_commit") or "")[:10]
    tail = f" (root commit {root})" if root else ""
    if source == "checkout":
        return f"the same checkout reported {old} as now called {new}{tail}"
    if source == "github":
        return f"GitHub says {old} is now {new}{tail}"
    if source == "owner":
        return "added by the owner"
    return source or "unknown"


def show(cur, name: str) -> dict:
    """Everything an agent needs to prove "this is one project now", the same
    whichever name of the group is passed."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name the project"}
    canonical, group = identity.project_canonical_group(cur, name)
    canonical = canonical or name
    group = group or [canonical]
    by_alias = {r["alias"]: r for r in alias_rows(cur, group)}
    counts = _counts(cur, group)
    names = []
    for member in group:
        row = by_alias.get(member)
        entry: dict[str, Any] = {"name": member, "role": "canonical" if member == canonical else "alias"}
        if row and member != canonical:
            entry.update(source=row["source"], created_at=row["created_at"], evidence=row["evidence"],
                         evidence_summary=_evidence_summary(row["source"], row["evidence"], member, canonical))
        entry.update(counts[member])
        names.append(entry)
    totals = {k: sum(n[k] for n in names) for k in ("open_commitments", "standing_decisions", "episodes")}
    return {"ok": True, "asked": name, "canonical": canonical, "one_project": len(group) > 1,
            "names": names, "totals": totals, "pairs": pair_rows(cur, group)}


def list_all(cur) -> dict:
    """What Settings > Projects shows, in one read: every project that has more
    than one name (as ``show`` gives it, newest name canonical), every pair
    with its state (asked, merged, kept_separate) and the counts of the names in
    an asked pair, and the last GitHub sweep's outcome as the sweep file has
    it. Read-only: it never runs the sweep."""
    from khipu import project_renames

    canonicals = [r[0] for r in _savepoint(
        cur, "project_group_list", "SELECT DISTINCT project FROM project_aliases ORDER BY project", ())]
    groups = [show(cur, c) for c in canonicals]
    pairs = [_pair_dict(r) for r in _savepoint(
        cur, "project_group_list_pairs",
        f"SELECT {_PAIR_COLS} FROM project_pairs ORDER BY (state = 'asked') DESC, state, created_at, a, b", ())]
    asked_names = sorted({n for p in pairs if p["state"] == ASKED for n in (p["a"], p["b"])})
    counts = _counts(cur, asked_names) if asked_names else {}
    if asked_names:
        from khipu.embed import _episode_schema_flags

        flags = _episode_schema_flags(cur)
        expr = "COALESCE(project, scope)" if flags["project"] else "scope"
        deleted = " AND deleted_at IS NULL" if flags["deleted_at"] else ""
        for name, ts in _savepoint(
                cur, "project_group_last_used",
                f"SELECT {expr}, max(ts) FROM episodes WHERE {expr} = ANY(%s){deleted} GROUP BY {expr}",
                (asked_names,)):
            counts[name]["last_used"] = _iso(ts)
    last = project_renames._read_last() or {}
    sweep = {"at": last.get("at"), "result": last.get("result"), "reason": last.get("reason") or last.get("error")}
    return {"ok": True, "groups": groups, "pairs": pairs, "pair_counts": counts, "sweep": sweep}


def asked_pairs(cur, project: str | None) -> list[dict]:
    """The pairs still waiting on the owner that involve ``project``'s group."""
    if not project:
        return []
    return pair_rows(cur, identity.project_group(cur, project), state=ASKED)


# ---- owner writes --------------------------------------------------------------------------


def _pair_row(cur, a: str, b: str) -> dict | None:
    a, b = identity._pair(a, b)
    rows = _savepoint(cur, "project_group_pair",
                      f"SELECT {_PAIR_COLS} FROM project_pairs WHERE a = %s AND b = %s", (a, b))
    return _pair_dict(rows[0]) if rows else None


def _set_pair(cur, a: str, b: str, state: str) -> None:
    a, b = identity._pair(a, b)
    cur.execute("UPDATE project_pairs SET state = %s, decided_at = CASE WHEN %s = 'asked' THEN NULL ELSE now() END "
                "WHERE a = %s AND b = %s", (state, state, a, b))


def _asked(cur, a: str, b: str) -> tuple[dict | None, str | None]:
    row = _pair_row(cur, a, b)
    if row is None or row["state"] != ASKED:
        have = f" (it is {row['state']})" if row else ""
        return None, f"{a} and {b} are not an asked pair{have}; nothing was changed"
    return row, None


def merge(cur, a: str, b: str, show_as: str) -> dict:
    """Merge an asked pair under the name ``show_as`` (one of the two):
    ``record_rename`` with source 'owner', and the pair becomes 'merged'."""
    a, b, show_as = (a or "").strip(), (b or "").strip(), (show_as or "").strip()
    if show_as not in (a, b):
        return {"ok": False, "error": f"show_as must be one of the pair: {a} or {b}"}
    row, why = _asked(cur, a, b)
    if row is None:
        return {"ok": False, "error": why}
    old = b if show_as == a else a
    evidence = {"old": old, "new": show_as, "pair": [row["a"], row["b"]], "root_commit": row["root_commit"]}
    res = identity.record_rename(cur, old, show_as, source="owner", evidence=evidence)
    if not res.get("ok"):
        return res
    _set_pair(cur, a, b, MERGED)
    return {**res, "pair": [row["a"], row["b"]], "pair_state": MERGED}


def keep_separate(cur, a: str, b: str) -> dict:
    row, why = _asked(cur, (a or "").strip(), (b or "").strip())
    if row is None:
        return {"ok": False, "error": why}
    _set_pair(cur, a, b, KEPT)
    return {"ok": True, "pair": [row["a"], row["b"]], "pair_state": KEPT}


# ---- what an Undo needs, read before the change ---------------------------------------------


def _members(cur, names: list[str]) -> list[str]:
    """``names`` and everyone grouped with any of them."""
    out: set[str] = set(names)
    for n in names:
        out.update(identity.project_group(cur, n))
    return sorted(out)


def _alias_row(cur, alias: str) -> dict | None:
    return next((r for r in alias_rows(cur, [alias]) if r["alias"] == alias), None)


def before_state(cur, action: str, target: str) -> dict:
    """The log line's ``before`` for ``action`` (``target`` is
    ``alias|canonical``, ``alias`` or ``a|b``)."""
    if action == ACTION_ALIAS_ADD:
        alias, _, canonical = target.partition("|")
        row = _alias_row(cur, alias)
        return {"existed": bool(row and row["project"] == canonical)}
    if action == ACTION_ALIAS_REMOVE:
        return {"row": _alias_row(cur, target)}
    a, _, b = target.partition("|")
    pair = _pair_row(cur, a, b)
    if action == ACTION_KEEP_SEPARATE:
        return {"pair": pair}
    members = _members(cur, [a, b])
    return {"pair": pair, "members": members, "aliases": alias_rows(cur, members)}


def _restore_alias_rows(cur, members: list[str], rows: list[dict]) -> None:
    """Put the alias rows among ``members`` back as they were: the rows the
    change added go, the re-pointed and dropped ones return exactly."""
    cur.execute("DELETE FROM project_aliases WHERE alias = ANY(%s)", (members,))
    for r in rows:
        if r["alias"] in members:
            cur.execute("INSERT INTO project_aliases (alias, project, source, evidence, created_at) "
                        "VALUES (%s, %s, %s, %s::jsonb, %s)",
                        (r["alias"], r["project"], r["source"],
                         json.dumps(r["evidence"]) if r["evidence"] is not None else None, r["created_at"]))


def undo(cur, action: str, target: str, before: dict) -> dict:
    """The inverse of one logged owner action; ``ValueError`` says why it
    can't be put back."""
    if action == ACTION_ALIAS_ADD:
        if before.get("existed"):
            return {"ok": True, "changed": False}
        alias, _, canonical = target.partition("|")
        cur.execute("DELETE FROM project_aliases WHERE alias = %s AND project = %s", (alias, canonical))
        return {"ok": True, "changed": bool(cur.rowcount)}
    if action == ACTION_ALIAS_REMOVE:
        row = before.get("row")
        if not row:
            return {"ok": True, "changed": False}
        have = _alias_row(cur, row["alias"])
        if have:
            raise ValueError(f"{row['alias']} is an alias again (of {have['project']}), so it was not put back")
        _restore_alias_rows(cur, [row["alias"]], [row])
        return {"ok": True, "changed": True}
    a, _, b = target.partition("|")
    pair = before.get("pair")
    if action == ACTION_KEEP_SEPARATE:
        if not pair:
            raise ValueError("that pair is gone, so there is nothing to put back")
        _set_pair(cur, a, b, ASKED)
        return {"ok": True, "pair_state": ASKED}
    members = before.get("members") or []
    if not members:
        raise ValueError("Khipu can't tell which names that merge joined, so it can't undo it")
    identity._xact_lock(cur, identity.PROJECT_LOCK_CLASS, "*project-groups*")
    _restore_alias_rows(cur, members, before.get("aliases") or [])
    if pair:
        _set_pair(cur, a, b, pair["state"])
    return {"ok": True, "pair_state": (pair or {}).get("state")}


def run_undo(action: str, target: str, before: dict) -> dict:
    """``undo`` on its own hub connection, committed on success."""
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            out = undo(cur, action, target, before)
        conn.commit()
    return out


def read_before(action: str, target: str):
    """``before_state`` on its own connection; None when the hub can't be read."""
    from khipu.db import connect

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                return before_state(cur, action, target)
    except Exception:  # noqa: BLE001 - the gate refuses what it can't undo
        return None


# ---- the session-start question ------------------------------------------------------------

_SLICE_PAIRS = 2


def slice_lines(pairs: list[dict]) -> list[str]:
    """The block the pushed slice carries for asked pairs: nothing when none."""
    if not pairs:
        return []
    first = pairs[0]
    lines = ["### Same project?",
             f"These look like the same project: {first['a']} and {first['b']} (same first commit; GitHub "
             "didn't confirm a rename). Ask the owner: merge them, or keep them separate?",
             "Check with `khipu_project`, then on the owner's answer call `khipu_owner_act`: `project.merge` "
             "{a, b, show_as: the name to keep} or `project.keep_separate` {a, b}."]
    if len(pairs) > 1:
        more = ", ".join(f"{p['a']} / {p['b']}" for p in pairs[1:_SLICE_PAIRS + 1])
        lines.append(f"Also asked: {more}" + (" …" if len(pairs) > _SLICE_PAIRS + 1 else ""))
    return lines


# ---- the CLI -------------------------------------------------------------------------------


def _emit(data: dict, code: int = 0) -> int:
    print(json.dumps(data, default=str))
    return code


def render_text(data: dict) -> str:
    lines = [data["canonical"] + (" (one project, several names)" if data["one_project"] else "")]
    for n in data["names"]:
        how = "" if n["role"] == "canonical" else f"  alias via {n.get('source')}: {n.get('evidence_summary')}"
        lines.append(f"- {n['name']}: {n['open_commitments']} open commitments, {n['standing_decisions']} "
                     f"standing decisions, {n['episodes']} episodes{how}")
    t = data["totals"]
    lines.append(f"Total: {t['open_commitments']} open commitments, {t['standing_decisions']} standing decisions, "
                 f"{t['episodes']} episodes")
    lines += [f"pair {p['a']} / {p['b']}: {p['state']}" for p in data["pairs"]]
    return "\n".join(lines)


def cmd_show(args) -> int:
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            out = show(cur, args.name)
    if not out.get("ok"):
        print(out["error"], file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, default=str) if getattr(args, "json", False) else render_text(out))
    return 0


def cmd_list(args) -> int:
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            out = list_all(cur)
    print(json.dumps(out, indent=2, default=str))
    return 0


def _gate(args, want: dict) -> int | None:
    """The owner's gate for a project change, as the ledger's owner verbs
    have it: a direction (the conversation the owner is typing in), the app,
    or a yes typed on a terminal. A refusal is printed; the return is its
    exit code, else None."""
    from khipu import owner_direction, routing

    if getattr(args, "by_direction", False) and not getattr(args, "from_app", False):
        try:
            owner_direction.gate(want)
        except owner_direction.DirectionRefused as err:
            return _emit(err.refusal(), 2)
        return None
    if getattr(args, "from_app", False) and not routing._caller()["app"]:
        return _emit({"ok": False, "code": "not_app", "error": "only the Khipu app can make this change as you; "
                      "it was not the app that asked, so nothing was changed"}, 2)
    if getattr(args, "from_app", False) or routing._caller()["app"]:
        owner_direction.noted(want, {"auth": "app"})
        return None
    if routing.typed_yes(f"This is the owner's change ({want['action']} {want['target']}). Type yes to confirm: "):
        owner_direction.noted(want, {"auth": "terminal"})
        return None
    return _emit({"ok": False, "code": "owner_only",
                  "error": "only the Khipu app, the owner's yes typed on a terminal, or a conversation the owner is "
                           "typing in (owner direction) can make this change; nothing was changed"}, 2)


def cmd_owner(args) -> int:
    """``khipu project alias add|remove``, ``merge`` and ``keep-separate``
    behind the owner's gate, with the owner-actions log and Undo."""
    from khipu import owner_direction
    from khipu.db import connect

    cmd, sub = args.project_cmd, getattr(args, "alias_cmd", None)
    if cmd == "alias" and sub == "add":
        action, target = ACTION_ALIAS_ADD, f"{args.alias.strip()}|{args.canonical.strip()}"
    elif cmd == "alias":
        action, target = ACTION_ALIAS_REMOVE, args.alias.strip()
    else:
        pa, pb = identity._pair(args.a.strip(), args.b.strip())
        action, target = (ACTION_MERGE if cmd == "merge" else ACTION_KEEP_SEPARATE), f"{pa}|{pb}"
    facts = {"show_as": args.show_as.strip()} if cmd == "merge" else None
    try:
        refused = _gate(args, owner_direction.intent(action, target, facts=facts))
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 2)
    if refused is not None:
        return refused
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                if action == ACTION_ALIAS_ADD:
                    out = identity.add_project_alias(cur, *target.split("|", 1))
                elif action == ACTION_ALIAS_REMOVE:
                    out = identity.remove_project_alias(cur, target)
                elif action == ACTION_MERGE:
                    out = merge(cur, args.a, args.b, args.show_as)
                else:
                    out = keep_separate(cur, args.a, args.b)
            if out.get("ok"):
                conn.commit()
            else:
                conn.rollback()
    except Exception as exc:  # noqa: BLE001 - answered like a refusal; the log line says it failed
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = owner_direction.finish(out) or out
    if out.get("ok"):
        return _emit(out)
    print(out.get("error"), file=sys.stderr)
    return _emit(out, 2)
