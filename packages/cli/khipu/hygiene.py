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
    """Destructive: delete every ``path:`` node failing :func:`is_real_path`
    (and its edges). Must NOT be run against the live shared hub without
    Matt's explicit go (scope §7 item 2) — callers are responsible for that
    gate; this function only executes what it is asked to.
    """
    report = report_junk_paths(cur, sample_limit=10_000_000)
    ids = report["sample"]
    if ids:
        cur.execute("DELETE FROM edges WHERE src = ANY(%s) OR dst = ANY(%s)", (ids, ids))
        cur.execute("DELETE FROM nodes WHERE id = ANY(%s)", (ids,))
    return {"deleted_nodes": len(ids), "sample": ids[:20]}


# ---- W1.5 identity backfill -------------------------------------------------

def apply_backfill_identity(cur, *, limit: int | None = None) -> dict[str, Any]:
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
        ident = resolve_repo_root(scope)
        if not ident.get("repo_root"):
            continue
        cur.execute(
            "UPDATE episodes SET repo_root = %s, project = %s WHERE id = %s",
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
    sample = [{"id": r[0], "scope": r[1], "session_id": r[2]} for r in cur.fetchall()]
    cur.execute(
        "SELECT COUNT(*) FROM episodes WHERE repo_root IS NULL AND scope IS NOT NULL AND scope LIKE '/%%'"
    )
    total = int(cur.fetchone()[0])
    return {"would_backfill": total, "sample": sample}


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


def _judge_prompt(texts: list[str]) -> str:
    from khipu.commitments import COMMITMENT_DEFINITION

    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    return (
        "You are auditing a list of stored 'commitments' (open loops) from an "
        "assistant's memory. Decide, for each one, whether it is a real "
        "commitment.\n\n"
        f"{COMMITMENT_DEFINITION}\n\n"
        "Output ONLY a JSON object: "
        '{"verdicts": [{"i": <index>, "keep": true|false, "reason": "<8 words max>"}]} '
        "with exactly one entry per numbered item.\n\n"
        f"Items:\n{numbered}\n"
    )


def _model_judge(texts: list[str]) -> list[dict[str, Any]]:
    """Re-judge one batch with the summariser model (khipu.extract._generate —
    same routing, retries and cloud/local fallback capture uses).

    Fail-OPEN in every direction: an unparseable response, a short response,
    or a transport failure leaves the items kept. This job drops rows; it must
    never drop them because a model call went sideways.
    """
    from khipu.extract import _generate, parse_model_json

    out: list[dict[str, Any]] = [
        {"keep": True, "reason": "model returned no verdict"} for _ in texts
    ]
    try:
        raw = _generate(_judge_prompt(texts), timeout=90, retries=1)
    except Exception as exc:  # noqa: BLE001 — keep everything on a failed call
        return [{"keep": True, "reason": f"judge unavailable: {type(exc).__name__}"} for _ in texts]
    parsed = parse_model_json(raw) or {}
    for item in parsed.get("verdicts") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(out):
            out[idx] = {
                "keep": bool(item.get("keep", True)),
                "reason": str(item.get("reason") or "").strip()[:120] or "model verdict",
            }
    return out


def _select_open_commitments(cur, *, project: str | None, limit: int | None) -> list[dict[str, Any]]:
    clauses = ["status = 'open'"]
    params: list[Any] = []
    if project:
        clauses.append("project = %s")
        params.append(project)
    sql = (
        "SELECT id, text, project, kind, owner, opened_at FROM commitments "
        f"WHERE {' AND '.join(clauses)} ORDER BY id ASC"
    )
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    cur.execute(sql, tuple(params))
    cols = ("id", "text", "project", "kind", "owner", "opened_at")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _duplicate_groups(rows: list[dict[str, Any]]) -> dict[int, int]:
    """{duplicate_id: keeper_id} for paraphrases within one project.

    Greedy single pass in id order, so the KEEPER is always the oldest row of
    its group — the one whose opened_at the user has already seen.
    """
    from khipu.commitments import _match_score

    keepers: list[dict[str, Any]] = []
    dupes: dict[int, int] = {}
    for row in sorted(rows, key=lambda r: r["id"]):
        hit = next(
            (
                k for k in keepers
                if k["project"] == row["project"]
                and _match_score(row["text"], k["text"]) >= DUP_MIN_SCORE
            ),
            None,
        )
        if hit is None:
            keepers.append(row)
        else:
            dupes[row["id"]] = hit["id"]
    return dupes


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
    dest.mkdir(parents=True, exist_ok=True)
    data = dest / "commitments.bin"
    with conn.cursor() as cur, open(data, "wb") as fh:
        with cur.copy("COPY commitments TO STDOUT (FORMAT BINARY)") as copy:
            for block in copy:
                fh.write(bytes(block))
    (dest / "RESTORE.md").write_text(_RESTORE_MD.format(ts=ts), encoding="utf-8")
    return dest


def run_commitments_hygiene(
    cur,
    *,
    apply: bool = False,
    project: str | None = None,
    limit: int | None = None,
    judge=None,
    max_calls: int = MAX_JUDGE_CALLS,
) -> dict[str, Any]:
    """Re-judge every OPEN commitment and report (or apply) the verdicts.

    Order is deliberate and cheapest-first: the deterministic filter
    (``commitments.rejection_reason``) first, so obvious status chatter never
    costs a model call; then the summariser model in batches of
    ``JUDGE_BATCH``, capped at ``max_calls``; then paraphrase de-duplication
    among whatever survived. Anything past the call cap is reported
    ``unjudged`` and left strictly alone.
    """
    from khipu.commitments import rejection_reason, resolve_owner
    from khipu.db import has_columns

    judge = judge or _model_judge
    rows = _select_open_commitments(cur, project=project, limit=limit)
    verdicts: dict[int, dict[str, Any]] = {}
    survivors: list[dict[str, Any]] = []
    for row in rows:
        owner = resolve_owner(row["text"], kind=row.get("kind"), declared=row.get("owner"))
        reason = rejection_reason(row["text"], owner=owner, kind=row.get("kind"))
        if reason is not None:
            verdicts[row["id"]] = {"verdict": "drop", "reason": f"filter:{reason}"}
        else:
            survivors.append(row)

    calls = 0
    judged: list[dict[str, Any]] = []
    for start in range(0, len(survivors), JUDGE_BATCH):
        batch = survivors[start:start + JUDGE_BATCH]
        if calls >= max_calls:
            for row in batch:
                verdicts[row["id"]] = {"verdict": "unjudged", "reason": "model call cap reached"}
            continue
        calls += 1
        results = judge([r["text"] for r in batch])
        for row, res in zip(batch, list(results) + [{"keep": True, "reason": "no verdict"}] * len(batch)):
            if res.get("keep", True):
                verdicts[row["id"]] = {"verdict": "keep", "reason": res.get("reason") or "model kept"}
                judged.append(row)
            else:
                verdicts[row["id"]] = {
                    "verdict": "drop",
                    "reason": f"model:{res.get('reason') or 'not a commitment'}",
                }

    dupes = _duplicate_groups(judged)
    for dup_id, keeper_id in dupes.items():
        verdicts[dup_id] = {"verdict": "duplicate", "reason": f"duplicate-of-{keeper_id}"}

    by_id = {r["id"]: r for r in rows}
    counts = {"keep": 0, "drop": 0, "duplicate": 0, "unjudged": 0}
    for v in verdicts.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1

    applied = 0
    if apply:
        seen_ready = False
        try:
            seen_ready = has_columns(cur, "commitments", "seen_count", "last_seen_at")
        except Exception:  # noqa: BLE001 — pre-migration hub: skip the fold
            seen_ready = False
        stamp = _today()
        for cid, v in verdicts.items():
            if v["verdict"] == "drop":
                cur.execute(
                    "UPDATE commitments SET status = 'dropped', closed_at = now(), "
                    "close_reason = %s WHERE id = %s AND status = 'open'",
                    (f"hygiene-{stamp}: {v['reason']}"[:400], cid),
                )
                applied += cur.rowcount
            elif v["verdict"] == "duplicate":
                keeper = dupes[cid]
                if seen_ready:
                    cur.execute(
                        "UPDATE commitments SET seen_count = seen_count + "
                        "COALESCE((SELECT seen_count FROM commitments d WHERE d.id = %s), 1), "
                        "last_seen_at = now() WHERE id = %s",
                        (cid, keeper),
                    )
                cur.execute(
                    "UPDATE commitments SET status = 'closed', closed_at = now(), "
                    "close_reason = %s WHERE id = %s AND status = 'open'",
                    (f"duplicate-of-{keeper}", cid),
                )
                applied += cur.rowcount

    report: dict[str, Any] = {
        "ok": True,
        "dry_run": not apply,
        "project": project,
        "scanned": len(rows),
        "model_calls": calls,
        "max_calls": max_calls,
        "counts": counts,
        "applied": applied,
    }
    listing = [
        {
            "id": cid,
            "verdict": v["verdict"],
            "reason": v["reason"],
            "text": (by_id.get(cid) or {}).get("text"),
        }
        for cid, v in sorted(verdicts.items())
    ]
    if apply:
        report["sample"] = [r for r in listing if r["verdict"] != "keep"][:20]
    else:
        report["verdicts"] = listing
    return report


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


# ---- commitments: the session-ended pass (2026-09-04, second pass) ---------
#
# The first pass cut 326 open commitments to 29 — and about half of THOSE were
# still the assistant's own in-session promises from sessions that had already
# ended ("Reply with the SHA each PR now points at", "Tell user the moment
# their app relaunches"). The capture-time rule now closes those when their
# session ends (khipu.commitments.close_session_plan); this one-off pass
# applies the same rule to rows that are already in the table.
#
# A session counts as ENDED when its last episode is older than
# SESSION_ENDED_HOURS, or when one of its episodes is a sessionend capture.
# The event is only recorded opportunistically (payload `event` / a "…
# sessionend" scope — measured near-absent on the live hub), so the age rule
# is the one that actually decides; the event check is a free extra.

SESSION_ENDED_HOURS = 6
_SESSION_END_EVENTS_SQL = "('sessionend', 'session_end', 'session-end', 'sessionended')"


def _select_open_with_session(cur, *, project: str | None, limit: int | None,
                              hours: int) -> list[dict[str, Any]]:
    clauses = ["c.status = 'open'"]
    params: list[Any] = []
    if project:
        clauses.append("c.project = %s")
        params.append(project)
    sql = f"""
        SELECT c.id, c.text, c.project, c.kind, c.owner, c.opened_at, e.session_id,
               (SELECT max(e2.ts) FROM episodes e2 WHERE e2.session_id = e.session_id) AS last_ts,
               COALESCE(
                 (SELECT max(e2.ts) FROM episodes e2 WHERE e2.session_id = e.session_id)
                 < now() - interval '{int(hours)} hours', false) AS aged,
               EXISTS (
                 SELECT 1 FROM episodes e3
                 WHERE e3.session_id = e.session_id
                   AND (lower(COALESCE(e3.raw->>'event', '')) IN {_SESSION_END_EVENTS_SQL}
                        OR strpos(lower(COALESCE(e3.scope, '')), 'sessionend') > 0)
               ) AS end_event
        FROM commitments c
        LEFT JOIN episodes e ON e.id = c.opened_episode
        WHERE {' AND '.join(clauses)}
        ORDER BY c.id ASC
    """
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    cur.execute(sql, tuple(params))
    cols = ("id", "text", "project", "kind", "owner", "opened_at", "session_id",
            "last_ts", "aged", "end_event")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def run_session_ended_pass(
    cur,
    *,
    apply: bool = False,
    project: str | None = None,
    limit: int | None = None,
    hours: int = SESSION_ENDED_HOURS,
) -> dict[str, Any]:
    """Re-own every OPEN commitment and retire the ones that died with their
    session.

    For each row: infer ``owner`` and ``future_trigger`` with the SAME
    deterministic rules capture uses (``khipu.commitments.resolve_owner`` /
    ``has_future_trigger`` — it never asks a model), decide whether the
    opening session has ended, then:

    * ``keep``  — the user owes it, or it carries an explicit cross-session
      trigger, or its session is still live and it is not a reporting duty;
    * ``close`` — assistant-owned, no future trigger, session ended
      (``close_reason 'session-ended'``);
    * ``drop``  — a within-session reporting duty the assistant owes ("reply
      with the SHAs", "tell the user when it relaunches"). Rule 3: never
      durable, whatever the session is doing.

    Dry run by default and never DELETEs — a drop is ``status = 'dropped'``
    with a close_reason, reversible with ``khipu owed --reopen ID``. Works
    with or without migration 0013: the two fields are computed in memory and
    only written back when the column is there.
    """
    from khipu.commitments import has_future_trigger, is_reporting_text, resolve_owner
    from khipu.db import has_columns

    rows = _select_open_with_session(cur, project=project, limit=limit, hours=hours)
    trigger_col = False
    try:
        trigger_col = has_columns(cur, "commitments", "future_trigger")
    except Exception:  # noqa: BLE001 — pre-migration hub: compute only
        trigger_col = False

    listing: list[dict[str, Any]] = []
    counts = {"keep": 0, "close": 0, "drop": 0}
    for row in rows:
        text = row["text"] or ""
        trigger = has_future_trigger(text)
        owner = resolve_owner(text, kind=row.get("kind"), declared=row.get("owner"),
                              future_trigger=trigger)
        ended = bool(row.get("aged")) or bool(row.get("end_event"))
        if owner == "user":
            verdict, reason = "keep", "user-owed"
        elif trigger:
            verdict, reason = "keep", "future trigger"
        elif ended:
            verdict, reason = "close", "session-ended"
        elif is_reporting_text(text):
            verdict, reason = "drop", "reporting: within-session duty"
        else:
            verdict, reason = "keep", "session still open"
        counts[verdict] += 1
        listing.append({
            "id": row["id"],
            "owner": owner,
            "future_trigger": trigger,
            "session_ended": ended,
            "verdict": verdict,
            "reason": reason,
            "text": text,
            "session_id": row.get("session_id"),
            "project": row.get("project"),
        })

    applied = 0
    if apply:
        stamp = _today()
        for item in listing:
            cid = item["id"]
            if trigger_col:
                cur.execute(
                    "UPDATE commitments SET owner = %s, future_trigger = %s WHERE id = %s",
                    (item["owner"], item["future_trigger"], cid),
                )
            else:
                cur.execute(
                    "UPDATE commitments SET owner = %s WHERE id = %s",
                    (item["owner"], cid),
                )
            if item["verdict"] == "close":
                cur.execute(
                    "UPDATE commitments SET status = 'closed', closed_at = now(), "
                    "close_reason = 'session-ended' WHERE id = %s AND status = 'open'",
                    (cid,),
                )
                applied += cur.rowcount
            elif item["verdict"] == "drop":
                cur.execute(
                    "UPDATE commitments SET status = 'dropped', closed_at = now(), "
                    "close_reason = %s WHERE id = %s AND status = 'open'",
                    (f"hygiene-{stamp}: {item['reason']}"[:400], cid),
                )
                applied += cur.rowcount

    return {
        "ok": True,
        "mode": "session-ended",
        "dry_run": not apply,
        "project": project,
        "session_ended_after_hours": hours,
        "future_trigger_column": trigger_col,
        "scanned": len(rows),
        "counts": counts,
        "applied": applied,
        "verdicts": listing,
    }
