"""Repo renames, slice C (docs/plans/2026-10-19-repo-renames.md): ask GitHub what
each recently active project is called now, record the alias when the answer
differs, and report the last sweep to doctor.

Off the hook path (nightly and ``khipu project renames check``). Nothing merges
without GitHub's own answer; no ``gh``, no login or an error on every lookup is
a *skipped* sweep that changes nothing, never a failure."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from khipu.paths import ensure_data_dir

SWEEP_LIMIT = 50
ACTIVE_DAYS = 30
LOOKUP_TIMEOUT_S = 3
STALE_DAYS = 3  # a sweep that has not been ok for this long turns doctor yellow
PAIR_MERGED_GITHUB = "merged_github"  # slice D adds the owner states 'merged' / 'kept_separate'

_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def is_github_shaped(name: str | None) -> bool:
    return bool(name and _SLUG.match(name))


def _last_path() -> Path:
    return ensure_data_dir() / "project-renames-last.json"


def github_full_name(slug: str) -> tuple[str | None, str | None]:
    """``(full_name, None)`` from ``gh api repos/<slug> --jq .full_name`` (GitHub
    follows its own redirect after a rename), or ``(None, reason)`` when ``gh`` is
    missing, not signed in, slow or errors. ``reason`` is ``gh missing`` or the first
    line of gh's own message; it never carries a credential."""
    try:
        from khipu.synth_cli import binary

        gh = binary("gh")
    except Exception:  # noqa: BLE001
        gh = None
    if not gh:
        return None, "gh missing"
    try:
        proc = subprocess.run([gh, "api", f"repos/{slug}", "--jq", ".full_name"], capture_output=True,
                              text=True, timeout=LOOKUP_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return None, "gh timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"gh failed: {type(exc).__name__}"
    answer = (proc.stdout or "").strip()
    if proc.returncode != 0 or not answer:
        first = ((proc.stderr or "").strip().splitlines() or ["gh returned nothing"])[0]
        return None, first[:120]
    return answer, None


def _read_last() -> dict[str, Any] | None:
    try:
        data = json.loads(_last_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_last(result: dict[str, Any]) -> None:
    prev = _read_last() or {}
    now = datetime.now(timezone.utc).isoformat()
    payload = {"at": now, "result": result["result"], "reason": result.get("reason"),
               "checked": result.get("checked", 0), "renamed": result.get("renamed", []),
               "skipped_by_owner": result.get("skipped_by_owner", [])}
    if result["result"] == "ok":
        payload["non_ok_since"] = None
    else:
        payload["non_ok_since"] = prev.get("non_ok_since") or now
    try:
        _last_path().write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def sweep(conn: Any = None, lookup: Callable[[str], tuple[str | None, str | None]] | None = None) -> dict[str, Any]:
    # forget-fence: a writer; returns the sweep's outcome
    """One pass. ``result`` is ``ok``, ``skipped`` (with ``reason``) or ``failed``
    (with ``error``). Pairs the owner has not decided come first, then the
    most recently active GitHub-shaped canonical projects, up to ``SWEEP_LIMIT``
    lookups in all, one at a time."""
    from khipu import identity

    lookup = lookup or github_full_name
    own = conn is None
    out: dict[str, Any] = {"result": "ok", "checked": 0, "renamed": [], "skipped_by_owner": [], "errors": 0}
    try:
        if own:
            from khipu.db import connect

            conn = connect()
        with conn.cursor() as cur:
            cur.execute("SELECT a, b FROM project_pairs WHERE state = 'asked' ORDER BY created_at", ())
            pairs = [(str(a), str(b)) for a, b in cur.fetchall() if is_github_shaped(a) and is_github_shaped(b)]
            cur.execute(
                "SELECT e.project FROM episodes e WHERE e.ts > now() - make_interval(days => %s) "
                "AND e.project IS NOT NULL AND e.project NOT IN (SELECT alias FROM project_aliases) "
                "GROUP BY e.project ORDER BY max(e.ts) DESC", (ACTIVE_DAYS,))
            projects = [str(r[0]) for r in cur.fetchall() if is_github_shaped(r[0])]
        conn.commit()

        budget = SWEEP_LIMIT
        failures: list[str] = []
        successes = 0

        def ask(slug: str) -> str | None:
            nonlocal budget, successes
            if budget <= 0:
                return None
            budget -= 1
            out["checked"] += 1
            name, why = lookup(slug)
            if name is None:
                out["errors"] += 1
                failures.append(why or "lookup failed")
                return None
            successes += 1
            return name

        def rename(cur, old: str, new: str, full: str, *, pair: tuple[str, str] | None) -> None:
            # Whether the owner keeps the two groups apart is decided by
            # record_rename under the group lock, never here before it.
            res = identity.record_rename(cur, old, new, source="github", evidence={
                "full_name": full, "checked_at": datetime.now(timezone.utc).isoformat()})
            if res.get("code") == identity.KEPT_SEPARATE:
                out["skipped_by_owner"].append({"old": old, "new": new})
                return
            if not res.get("ok"):
                raise RuntimeError(str(res.get("error")))
            if pair:
                cur.execute("UPDATE project_pairs SET state = %s, decided_at = now() WHERE a = %s AND b = %s",
                            (PAIR_MERGED_GITHUB, pair[0], pair[1]))
            out["renamed"].append({"old": old, "new": new})

        with conn.cursor() as cur:
            for a, b in pairs:
                full = ask(a)
                if full is not None and full.lower() == b.lower():
                    rename(cur, a, b, full, pair=(a, b))
                    continue
                full = ask(b)
                if full is not None and full.lower() == a.lower():
                    rename(cur, b, a, full, pair=(a, b))
            for project in projects:
                full = ask(project)
                if full is not None and full.lower() != project.lower():
                    rename(cur, project, full, full, pair=None)
        conn.commit()
        if out["checked"] and not successes:
            out.update(result="skipped", reason=failures[0])
        elif not out["checked"] and not (pairs or projects):
            out["note"] = "nothing to check"
    except Exception as exc:  # noqa: BLE001 — a sweep never raises into the nightly
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        out.update(result="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    _write_last(out)
    return out


def doctor_block(cur: Any) -> dict[str, Any]:
    """The ``project_renames`` doctor block: two queries and one small file.
    Advisory only: a sweep that has not been ok for more than ``STALE_DAYS`` is
    ``yellow``, never red; pairs waiting for the owner are informational."""
    cur.execute("SELECT coalesce(source, 'owner'), count(*) FROM project_aliases GROUP BY 1", ())
    by_source = {str(s): int(n) for s, n in cur.fetchall()}
    cur.execute(
        "SELECT p.n, a.alias, a.project, a.source, a.created_at "
        "FROM (SELECT count(*) AS n FROM project_pairs WHERE state = 'asked') p "
        "LEFT JOIN LATERAL (SELECT alias, project, source, created_at FROM project_aliases "
        "ORDER BY created_at DESC LIMIT 1) a ON true", ())
    waiting, alias, canonical, source, created = cur.fetchone()
    newest = None if alias is None else {"alias": alias, "canonical": canonical, "source": source or "owner",
                                         "at": created.isoformat() if hasattr(created, "isoformat") else created}
    last = _read_last()
    sweep_block: dict[str, Any] = {"result": None, "at": None, "reason": None}
    severity = "ok"
    if last:
        sweep_block = {"result": last.get("result"), "at": last.get("at"), "reason": last.get("reason") or last.get("error")}
        if last.get("result") != "ok":
            since = last.get("non_ok_since")
            try:
                t = datetime.fromisoformat(str(since))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - t).total_seconds() > STALE_DAYS * 86400:
                    severity = "yellow"
            except (TypeError, ValueError):
                severity = "yellow"
    return {"aliases_total": sum(by_source.values()), "aliases_by_source": by_source, "newest_alias": newest,
            "pairs_waiting": int(waiting or 0), "last_sweep": sweep_block, "severity": severity}
