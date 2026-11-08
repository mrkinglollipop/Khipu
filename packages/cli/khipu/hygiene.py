"""Topic/graph hygiene — W5.1 (topics vs tags) and W5.2 (path minting filter).

Two shapes of graph pollution measured 2026-09-03: 94% of capture-topic slugs
never match a real topic page (they become dangling `topic:` nodes), and 73%
of `path:` nodes are not paths at all (`a/b`, no extension, no leading `/` or
`~`). Both are filtered HERE, once, so every writer — the native extractor,
the legacy capture_v2 payload path, and gateway captures — goes through the
same rule instead of three drifting copies.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

# ---- W5.1 topics vs tags ---------------------------------------------------

# A worktree-minted slug: extract.py used to append the cwd basename, and a
# worktree dir name typically ends in a short hex disambiguator.
_WORKTREE_HEX_RE = re.compile(r"-[0-9a-f]{6}$")

_NOISE_EXACT = {
    "tmp", "temp", "test", "testing", "general", "misc", "stuff", "things",
    "chat", "session", "sessions", "unknown", "other", "misc-notes",
}

_HARNESS_NAMES = {
    "claude", "cursor", "codex", "aegis", "grok", "claude-code", "claude_code",
    "grok-bot", "grokbot",
}


def is_noise_slug(slug: str) -> bool:
    s = (slug or "").strip().lower()
    if not s:
        return True
    if _WORKTREE_HEX_RE.search(s):
        return True
    if s in _NOISE_EXACT or s in _HARNESS_NAMES:
        return True
    return False


def classify_topics(cur, topics: Iterable[Any]) -> tuple[list[str], list[str], bool]:
    """(resolved, tags, unresolved_due_to_error) for a capture's topic slugs.

    The junk signal is the NOISE PATTERN (worktree/hex suffix, tmp, harness
    names, the short generic list — :func:`is_noise_slug`), never "has no
    page yet". Hub/cloud captures never see the file wiki, so gating minting
    on an existing page meant EVERY previously-unseen topic became a tag —
    94% of capture-topic slugs measured dangling 2026-09-03, and this was
    why: a clean, novel topic slug was silently demoted every single time.

    A slug that matches the noise set becomes a TAG (still visible on the
    episode, never a graph node). Every other slug is ``resolved`` — minted
    as a NEW ``topic:`` node when it does not already match a live topic page
    or a ``topic_aliases`` entry, or folded onto the existing page/alias's
    canonical slug when it does. Order is preserved and slugs are deduped.

    If the existence check itself fails (PG unreachable mid-call, or no
    cursor at all — the write path could not check), the caller should keep
    today's behaviour and flag ``raw.topics_unresolved=true``; that is what
    the third return value signals. In that case ``resolved`` carries every
    non-noise candidate unchanged (nothing dropped, nothing tagged) so a
    write in a degraded environment does not silently lose topics.
    """
    candidates: list[str] = []
    tags: list[str] = []
    seen_candidates: set[str] = set()
    seen_tags: set[str] = set()
    for raw in topics or []:
        s = str(raw or "").strip().lower()
        if not s:
            continue
        if is_noise_slug(s):
            if s not in seen_tags:
                seen_tags.add(s)
                tags.append(s)
            continue
        if s not in seen_candidates:
            seen_candidates.add(s)
            candidates.append(s)
    if not candidates or cur is None:
        return candidates, tags, bool(candidates and cur is None)

    try:
        cur.execute(
            "SELECT slug FROM topics WHERE slug = ANY(%s) AND deleted_at IS NULL",
            (candidates,),
        )
        exact = {r[0] for r in cur.fetchall()}
        remaining = [c for c in candidates if c not in exact]
        alias_map: dict[str, str] = {}
        if remaining:
            cur.execute(
                "SELECT alias, slug FROM topic_aliases WHERE alias = ANY(%s)",
                (remaining,),
            )
            alias_map = {a: s for a, s in cur.fetchall()}
    except Exception:  # noqa: BLE001 — degrade to "keep everything as topics"
        return candidates, tags, True

    resolved: list[str] = []
    seen_resolved: set[str] = set()
    for c in candidates:
        # Not a noise slug: it stays a topic whether or not it already has a
        # page — alias/exact-match just canonicalizes it onto the existing
        # slug rather than minting a near-duplicate.
        slug = alias_map.get(c, c)
        if slug not in seen_resolved:
            seen_resolved.add(slug)
            resolved.append(slug)
    return resolved, tags, False


# ---- W5.2 path minting filter ----------------------------------------------

def is_real_path(rel: str, *, repo_root: str | None = None) -> bool:
    """A ``path:`` node is only minted for something that looks like a real
    path: it has a file extension, it starts with an absolute/home/relative
    marker, or it actually exists under ``repo_root``."""
    s = (rel or "").strip()
    if not s:
        return False
    if os.path.splitext(s)[1]:
        return True
    if s.startswith(("/", "~", "./")):
        return True
    # An explicit trailing slash is the author writing a directory
    # (``sojourn/art-samples/uw-intro-acut-fill-2026-07-26/``). The junk this
    # rule exists for is two-segment prose pairs (``add/remove``, ``UI/jobs``)
    # and uppercase ticker/acronym lists (``SPY/QQQ/IWM``, ``IC/PCS/STR``):
    # three or more lowercase segments with a hyphen or underscore somewhere
    # is a path, not prose.
    if s.endswith("/") and "/" in s.rstrip("/"):
        return True
    segs = [p for p in s.split("/") if p]
    if len(segs) >= 3 and not any(p.isupper() or p.isdigit() for p in segs) \
            and any(("-" in p or "_" in p) for p in segs):
        return True
    if repo_root:
        try:
            candidate = Path(repo_root).expanduser() / s.lstrip("/")
            if candidate.exists():
                return True
        except (OSError, ValueError):
            pass
    return False


def filter_real_paths(rels: Iterable[str], *, repo_root: str | None = None) -> list[str]:
    return [r for r in rels if is_real_path(r, repo_root=repo_root)]


def report_junk_paths(cur, *, sample_limit: int = 20) -> dict[str, Any]:
    # forget-fence: counts path nodes; returns counts and node ids, no episode
    """Read-only: how many existing ``path:`` nodes fail :func:`is_real_path`.

    No ``repo_root`` context is available for a historical node (the graph
    does not record which episode/topic minted it), so this checks the
    extension/leading-marker rule only — the ``exists()`` branch never
    applies here, matching the conservative dry-run report the scope calls
    for before any destructive backfill.
    """
    cur.execute("SELECT id FROM nodes WHERE type = 'path'")
    rows = [r[0] for r in cur.fetchall()]
    failing: list[str] = []
    for node_id in rows:
        rel = node_id[len("path:"):] if node_id.startswith("path:") else node_id
        if not is_real_path(rel):
            failing.append(node_id)
    return {
        "total_path_nodes": len(rows),
        "failing": len(failing),
        "sample": failing[:sample_limit],
    }


def apply_purge_junk_paths(cur) -> dict[str, Any]:
    """Report only: historical path nodes lack the repository provenance
    needed to distinguish accepted extensionless files from prose."""
    report = report_junk_paths(cur, sample_limit=10_000_000)
    ids = report["sample"]
    return {"deleted_nodes": 0, "sample": ids[:20], "report_only": True,
            "reason": "path provenance is unavailable; shape alone cannot authorize deletion"}


# ---- W1.5 identity backfill -------------------------------------------------

def apply_backfill_identity(cur, *, limit: int | None = None) -> dict[str, Any]:
    # forget-fence: owner maintenance writer; returns counts
    """Destructive: derive repo_root/project (via khipu.identity, using each
    episode's absolute-path ``scope`` as a stand-in cwd) for every episode
    that has none yet, and UPDATE it in place. Must NOT be run against the
    live shared hub without Matt's explicit go (scope §7 item 2) — callers
    are responsible for that gate; this function only executes what it is
    asked to.
    """
    from khipu.identity import resolve_repo_root

    sql = (
        "SELECT id, scope FROM episodes WHERE repo_root IS NULL "
        "AND scope IS NOT NULL AND scope LIKE '/%%' ORDER BY id DESC"
    )
    if limit:
        cur.execute(sql + " LIMIT %s", (limit,))
    else:
        cur.execute(sql)
    rows = cur.fetchall()
    updated = 0
    for eid, scope in rows:
        ident = resolve_repo_root(scope, cur=cur)
        if not ident.get("repo_root"):
            continue
        cur.execute(
            "UPDATE episodes SET repo_root = COALESCE(repo_root, %s), "
            "project = COALESCE(project, %s) WHERE id = %s",
            (ident["repo_root"], ident["project"], eid),
        )
        updated += cur.rowcount
    return {"scanned": len(rows), "updated": updated}


def backfill_identity_report(cur, *, sample_limit: int = 20) -> dict[str, Any]:
    """Dry-run only: how many episodes have an absolute-path ``scope`` and no
    ``repo_root``/``project`` yet, with a sample. Never writes.
    """
    cur.execute(
        """
        SELECT id, scope, session_id FROM episodes
        WHERE repo_root IS NULL
          AND scope IS NOT NULL AND scope LIKE '/%%'
        ORDER BY id DESC
        LIMIT %s
        """,
        (sample_limit,),
    )
    from khipu.tombstones import withhold

    sample = withhold([{"id": r[0], "scope": r[1], "session_id": r[2]} for r in cur.fetchall()],
                      fail_closed=False, episode_key="id")
    cur.execute(
        "SELECT COUNT(*) FROM episodes WHERE repo_root IS NULL AND scope IS NOT NULL AND scope LIKE '/%%'"
    )
    total = int(cur.fetchone()[0])
    return {"would_backfill": total, "sample": sample}


# ---- K6: project backfill (2026-09-14) --------------------------------------
#
# 81% of episodes carry a NULL project (finding K6). Unlike identity backfill
# above (derives repo_root/project from an absolute-path SCOPE), this walks
# episodes that have NEITHER: the only data left to resolve from is (a)
# another episode of the SAME session already carrying a project — the most
# common real case, a session whose early captures predate identity
# resolution or whose hook missed once — and (b) a `repo_root` already on the
# row (or its `raw` payload) with no `project` alongside it, an edge case
# identity resolution should not normally produce but is cheap to also cover.
# Deliberately NOT session_id/parent_session_id itself (K6's whole point) and
# NOT scope (free text — normalize_scope already drops most of the junk that
# made scope untrustworthy as an identity signal).

def _project_from_raw(repo_root: Any, raw: Any) -> str | None:
    root = repo_root
    if not root and isinstance(raw, dict):
        root = raw.get("repo_root")
    if not root:
        return None
    from pathlib import Path

    return Path(str(root)).name or None


def _resolve_project_for_episode(cur, *, episode_id: int, session_id: str | None,
                                 repo_root: Any, raw: Any) -> str | None:
    # forget-fence: resolves one project name for a backfill write
    """Best-effort project for one NULL-project episode. Never invents a
    project from a session id or free-text scope (K6's whole point)."""
    if session_id:
        cur.execute(
            "SELECT project FROM episodes WHERE session_id = %s AND project IS NOT NULL "
            "AND id != %s LIMIT 1",
            (session_id, episode_id),
        )
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    return _project_from_raw(repo_root, raw)


def backfill_project_report(cur, *, sample_limit: int = 20) -> dict[str, Any]:
    """Dry run only: every NULL-project episode, whether a resolver would
    find one, and a sample. Never writes. The caller (``khipu project
    backfill``) decides whether the resolve rate clears the bar for
    ``--apply`` on the live hub — this function only reports it."""
    cur.execute(
        "SELECT id, session_id, repo_root, raw FROM episodes WHERE project IS NULL ORDER BY id DESC"
    )
    rows = cur.fetchall()
    resolved = 0
    sample: list[dict[str, Any]] = []
    for eid, session_id, repo_root, raw in rows:
        project = _resolve_project_for_episode(
            cur, episode_id=eid, session_id=session_id, repo_root=repo_root, raw=raw,
        )
        if project:
            resolved += 1
            if len(sample) < sample_limit:
                sample.append({"id": eid, "resolved_project": project})
    total = len(rows)
    from khipu.tombstones import withhold

    return {
        "total_null_project": total,
        "would_resolve": resolved,
        "resolve_rate": round(resolved / total, 4) if total else 0.0,
        "sample": withhold(sample, fail_closed=False, episode_key="id"),
    }


def apply_backfill_project(cur, *, limit: int | None = None) -> dict[str, Any]:
    # forget-fence: owner maintenance writer; returns counts
    """Destructive: write the resolved project for every NULL-project episode
    a resolver can answer for. The UPDATE's own ``WHERE ... AND project IS
    NULL`` makes "touches no row that already has a project" structural, not
    just a convention. Idempotent: a second run finds nothing left to do
    (every row it could resolve now has a project). Must NOT be run against
    the live shared hub without Matt's explicit go (same posture as
    ``apply_purge_junk_paths``/``apply_backfill_identity``) — callers are
    responsible for that gate; this function only executes what it is asked.
    """
    sql = "SELECT id, session_id, repo_root, raw FROM episodes WHERE project IS NULL ORDER BY id DESC"
    if limit:
        cur.execute(sql + " LIMIT %s", (limit,))
    else:
        cur.execute(sql)
    rows = cur.fetchall()
    updated = 0
    for eid, session_id, repo_root, raw in rows:
        project = _resolve_project_for_episode(
            cur, episode_id=eid, session_id=session_id, repo_root=repo_root, raw=raw,
        )
        if not project:
            continue
        cur.execute(
            "UPDATE episodes SET project = %s WHERE id = %s AND project IS NULL",
            (project, eid),
        )
        updated += cur.rowcount
    return {"scanned": len(rows), "updated": updated}


# ---- commitments quality (2026-09-04) --------------------------------------
#
# Owed was measured unusable on the live hub: 328 open commitments in 7 days,
# dominated by in-flight status, the assistant's own same-session plan steps,
# inter-agent coordination chatter, and paraphrase restatements of one item
# across successive captures. The capture-time fixes (precision filter,
# paraphrase dedup, silence expiry, session-plan closure) stop the inflow;
# this job cleans up what is already there.
#
# Never DELETE: a reject becomes status 'dropped' with a close_reason, so a
# bad verdict is reversible with `khipu owed --reopen ID`.

JUDGE_BATCH = 40
MAX_JUDGE_CALLS = 8  # cost guard: at most 8 model calls (~320 items) per run
DUP_MIN_SCORE = 0.5
# Cosine paraphrase-dedup bars (2026-09-05): measured against real duplicate
# pairs (0.95..0.795 cosine) and real FALSE pairs (0.811, 0.797) — the two
# ranges overlap between ~0.78 and ~0.90, so cosine alone cannot cut cleanly
# there. Above DUP_COSINE_AUTO it merges outright; in the ask band a single
# batched model call adjudicates; below DUP_COSINE_ASK it is left alone.
DUP_COSINE_AUTO = 0.90
DUP_COSINE_ASK = 0.78


def commitments_backup_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Khipu" / "backups"


_RESTORE_MD = """# commitments backup — {ts}

Written by `khipu hygiene commitments --apply` BEFORE it changed any row.
`commitments.bin` is a psycopg binary `COPY` of the whole table at that moment.

Restore (psql against the same hub, same column order):

```sql
BEGIN;
CREATE TEMP TABLE commitments_restore (LIKE commitments INCLUDING ALL);
\\copy commitments_restore FROM 'commitments.bin' WITH (FORMAT BINARY)
-- inspect, then put back only what you want, e.g. undo the whole run:
UPDATE commitments c
   SET status = r.status, close_reason = r.close_reason,
       closed_at = r.closed_at, closed_episode = r.closed_episode
  FROM commitments_restore r
 WHERE c.id = r.id;
COMMIT;
```

A single bad verdict does not need this file: `khipu owed --reopen <ID>`
puts one row back to `open`. Nothing here was ever DELETEd.
"""


def backup_commitments(conn, *, dest_root: Path | None = None) -> Path:
    """Binary COPY of the whole commitments table + a RESTORE.md, under
    ``~/Library/Application Support/Khipu/backups/commitments-<ts>/``.

    Called by `--apply` before the first write. Raises on failure — a run that
    could not take a backup must not proceed.
    """
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = (dest_root or commitments_backup_dir()) / f"commitments-{ts}"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        data = dest / "commitments.bin"
        with conn.cursor() as cur, open(data, "wb") as fh:
            with cur.copy("COPY commitments TO STDOUT (FORMAT BINARY)") as copy:
                for block in copy:
                    fh.write(bytes(block))
        (dest / "RESTORE.md").write_text(_RESTORE_MD.format(ts=ts), encoding="utf-8")
    except OSError as exc:
        from khipu.storage import describe_os_error, disk_full

        if not disk_full(exc):
            raise
        # A half-written backup restores nothing and only holds space.
        import shutil

        shutil.rmtree(dest, ignore_errors=True)
        raise OSError(exc.errno, f"the commitments backup could not be written: {describe_os_error(exc)}") from exc
    try:
        from khipu import storage

        prune_commitments_backups(dest.parent, storage.settings()["commitments_backup_keep_days"], keep=dest)
    except Exception:  # noqa: BLE001 — the backup is taken; the daily sweep prunes again
        pass
    return dest


_BACKUP_NAME = re.compile(r"commitments-(\d{8}T\d{6}Z)")


def prune_commitments_backups(root: Path | None = None, keep_days: int = 30, *,
                              keep: Path | None = None, now=None) -> list[str]:
    """Remove ``commitments-<ts>`` folders older than ``keep_days`` (0 keeps
    every backup), judged by the time in the name. Only folders named exactly
    that way, and never ``keep`` (the one just written). Returns the names
    removed."""
    import shutil
    from datetime import datetime, timedelta, timezone

    root = root or commitments_backup_dir()
    if keep_days <= 0 or not root.is_dir():
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    removed = []
    for entry in sorted(root.iterdir()):
        match = _BACKUP_NAME.fullmatch(entry.name)
        if not match or entry.is_symlink() or not entry.is_dir():
            continue
        if keep is not None and entry.name == keep.name:
            continue
        taken = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if taken < cutoff:
            shutil.rmtree(entry)
            removed.append(entry.name)
    return removed


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


# ---- summary_kind backfill (2026-10-30) -------------------------------------
#
# 229 episodes carry the sentence the summariser's empty replies used to be
# stored under. They keep their text and every evidence column; each only
# gains raw.summary_kind = "verbatim_only", the marker new captures carry, so
# readers have one signal. Bounded per night.
SUMMARY_KIND_NIGHTLY_LIMIT = 200


def apply_backfill_summary_kind(cur, *, limit: int = SUMMARY_KIND_NIGHTLY_LIMIT) -> dict[str, Any]:
    # forget-fence: owner maintenance writer; selects ids and writes a summary_kind marker, calls no provider
    import json

    from khipu.db import has_columns
    from khipu.summary_kind import LEGACY_PLACEHOLDER, VERBATIM_ONLY

    live = "AND deleted_at IS NULL " if has_columns(cur, "episodes", "deleted_at") else ""
    # A row whose raw is not a JSON object (array, scalar, NULL) has no
    # summary_kind key to read, so it must be rewritten into an object or it is
    # selected every night.
    where = (f"summary LIKE %s {live}AND COALESCE(CASE WHEN jsonb_typeof(raw) = 'object' "
             "THEN raw->>'summary_kind' END, '') <> %s")
    params = (LEGACY_PLACEHOLDER + "%", VERBATIM_ONLY)
    cur.execute(f"SELECT id FROM episodes WHERE {where} ORDER BY id LIMIT %s", (*params, int(limit)))
    ids = [int(r[0]) for r in cur.fetchall()]
    updated = 0
    if ids:
        # Object: merge the marker. NULL or JSON null: the marker alone. Any other
        # shape: the marker plus the old value kept under raw_value.
        cur.execute(
            "UPDATE episodes SET raw = CASE "
            "WHEN jsonb_typeof(raw) = 'object' THEN raw || %s::jsonb "
            "WHEN raw IS NULL OR jsonb_typeof(raw) = 'null' THEN %s::jsonb "
            "ELSE %s::jsonb || jsonb_build_object('raw_value', raw) END WHERE id = ANY(%s)",
            (json.dumps({"summary_kind": VERBATIM_ONLY}),) * 3 + (ids,),
        )
        updated = cur.rowcount
    cur.execute(f"SELECT COUNT(*) FROM episodes WHERE {where}", params)
    return {"scanned": len(ids), "updated": updated, "remaining": int(cur.fetchone()[0])}
