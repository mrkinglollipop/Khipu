"""khipu CLI entry — status / search / graph / doctor / regen-memory / reconcile."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from khipu.search_text import search_tokens


def _env(*names: str, default: str = "") -> str:
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return default


def _memory_root_default() -> str | None:
    """argparse default for --memory-root: env → config.json → None.

    Resolved lazily so importing this module never touches the config file, and
    so a fresh install (no legacy file wiki) gets None rather than a path that
    exists only on the machine this was first written on.
    """
    from khipu.config import path_setting

    v = path_setting("memory_root")
    return str(v) if v else None


def _require_memory_root(args: argparse.Namespace) -> Path | None:
    """The legacy memory tree, or a JSON error on stdout and None.

    Commands that read or write the file wiki cannot do anything useful without
    it, so they fail loudly rather than walk a nonexistent directory and report
    zero of everything.
    """
    v = getattr(args, "memory_root", None)
    if v:
        return Path(v)
    print(
        json.dumps(
            {
                "ok": False,
                "error": "memory_root is not configured",
                "fix": "khipu config --set memory_root /path/to/memory/conversations "
                "(or export KHIPU_MEMORY_ROOT)",
            }
        )
    )
    return None


def _add_paths() -> None:
    from khipu.paths import repo_root

    root = repo_root()
    for p in (root / "packages" / "cli", root / ".python_libs"):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def cmd_status(args: argparse.Namespace) -> int:
    from khipu.drift import status_payload

    mem = Path(args.memory_root) if args.memory_root else None
    data = status_payload(
        mem,
        conflict_sample=int(args.sample),
        include_drift=bool(args.drift),
    )
    print(json.dumps(data, indent=2, default=str))
    return 0


def cmd_revisions(args: argparse.Namespace) -> int:
    from khipu.revisions import conflict_report, recent_revisions, revision_for_id

    if args.show is not None:
        row = revision_for_id(args.show)
        if row is None:
            print(f"error: revision {args.show} not found", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, default=str))
        return 0

    mem = _require_memory_root(args)
    if mem is None:
        return 2
    out = {
        "conflicts": conflict_report(mem, sample=args.sample),
        "recent": recent_revisions(limit=args.limit, slug=args.slug),
    }
    print(json.dumps(out, indent=2, default=str))
    # Exit 2 only on active file↔pg drift — multi_revision alone is healthy LWW archive
    return 0 if out["conflicts"]["ok"] else 2


def cmd_doctor(args: argparse.Namespace) -> int:
    from khipu.drift import backup_health, sample_drift, status_payload
    from khipu.keychain import secrets_status

    mem = Path(args.memory_root) if args.memory_root else None
    status = status_payload(None)
    # A check whose input is not configured on this machine is SKIPPED and
    # named in `not_configured` — distinct from red (configured but broken)
    # and from green (checked and clean). A fresh install has no legacy file
    # wiki, so drift against it is inapplicable, not passing.
    not_configured: list[str] = []
    # Every other check below is wrapped; this one was not, so a single
    # unreadable topic file aborted the entire health report with a traceback
    # instead of reporting red (audit 2026-08-17).
    if mem is None:
        not_configured.append("memory_root")
        drift = {
            "skipped": "memory_root not configured",
            "episodes_missing_in_pg": 0,
            "topic_mismatches": [],
            "topic_files_unreadable": [],
        }
    else:
        try:
            drift = sample_drift(mem, sample=args.sample)
        except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
            drift = {
                "error": f"{type(e).__name__}: {e}",
                "episodes_missing_in_pg": -1,
                "topic_mismatches": [],
                "topic_files_unreadable": [],
            }
    backup = backup_health()
    try:
        from khipu import graph_backup

        _graph_backup = graph_backup.local_health()
        _graph_offsite = graph_backup.offsite_health()
    except Exception as e:  # noqa: BLE001
        _graph_backup = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        _graph_offsite = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # P2b gate: directional. Every file episode must exist in PG by identity
    # (ts, md5(summary)); PG holding MORE than the file is healthy (hub writes,
    # archived episodes survive the upsert-only reconcile), so the old
    # count-delta tolerance is gone — it could not fail once PG ⊃ file.
    drift_ok = (
        drift.get("episodes_missing_in_pg") == 0
        and len(drift.get("topic_mismatches") or []) == 0
        # A topic we could not read is a topic we did not check; unchecked is
        # not the same as clean.
        and not drift.get("topic_files_unreadable")
        and not drift.get("error")
    )
    # Graph: graphify's graph.sqlite vs PG's graphify-owned rows, both
    # directions (audit 2026-08-17 — PG had silently diverged for two weeks
    # while doctor stayed green because it only looked at episodes/topics).
    # A CONFIGURED-but-missing graph.sqlite is a red result, not a skip: that
    # machine is supposed to be able to see the graph's source. An UNCONFIGURED
    # one is skipped and named.
    try:
        from khipu.config import path_setting
        from khipu.graph_sync import graph_drift

        if path_setting("graph_sqlite") is None:
            not_configured.append("graph_sqlite")
            graph = {"ok": True, "skipped": "graph_sqlite not configured"}
        else:
            graph = graph_drift()
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        graph = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # Outbox: a queued capture is something PG does not have yet — red until
    # drained. Doctor tries a drain first so a transient outage that has
    # already cleared does not show as a failure.
    try:
        from khipu.outbox import drain as outbox_drain, status as outbox_status

        if outbox_status()["pending"]:
            outbox_drain()
        outbox = outbox_status()
    except Exception as e:  # noqa: BLE001
        outbox = {"pending": -1, "error": f"{type(e).__name__}: {e}"}
    outbox_ok = outbox.get("pending") == 0
    # Capture liveness: is each harness ACTUALLY being recorded? Red on evidence
    # of a failure (hook error, drain failure, stale queue, cadence never
    # firing) — never on idleness. maintainer, 2026-08-17: a session that captured
    # only at compaction looked healthy on every other check here.
    try:
        from khipu.session_capture import (
            drain as capture_drain,
            liveness_all,
            queued_jobs,
        )

        if queued_jobs():
            capture_drain()  # like the outbox: clear what can be cleared, then judge
        liveness = liveness_all()
    except Exception as e:  # noqa: BLE001
        liveness = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "red": [],
            "harnesses": {},
        }
    # Git sync: is the memory tree's nightly auto-sync landing on GitHub? It is
    # soft-failed inside the nightly by design, so until this it could die and
    # every light stayed green (state-of-play 2026-08-17 item 7). Judged from
    # the heartbeat git_sync.py writes plus local repo evidence; not applicable
    # (green) on any Mac that does not run the nightly.
    try:
        from khipu.git_sync_health import status as git_sync_status

        git_sync = git_sync_status()
    except Exception as e:  # noqa: BLE001
        git_sync = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "reasons": ["check failed"],
        }
    secrets = secrets_status()
    # The file DSN is what every sandboxed harness falls back to when the
    # Keychain is denied. It went stale on 2026-08-04 and nothing noticed until
    # 2026-08-18, because every check ran where the Keychain worked.
    dsn_file_ok = bool((secrets.get("dsn_file") or {}).get("ok", True))
    try:
        from khipu.jobs import index_freshness, job_status

        jobs = job_status()
        index_fresh = index_freshness(memory_root=mem)
        index_freshness_ok = bool(index_fresh.get("ok"))
    except Exception as e:  # noqa: BLE001
        jobs = {"error": f"{type(e).__name__}: {e}"}
        index_fresh = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        index_freshness_ok = False
    try:
        from khipu.embed import coverage

        embed_coverage = coverage()
        embed_coverage_ok = (
            embed_coverage.get("episodes", {}).get("missing", 0) == 0
            and embed_coverage.get("topics", {}).get("missing", 0) == 0
        )
    except Exception as e:  # noqa: BLE001
        embed_coverage = {"error": f"{type(e).__name__}: {e}"}
        embed_coverage_ok = False
    out = {
        "status": status,
        "drift": drift,
        "graph_drift": graph,
        "outbox": outbox,
        "capture_liveness": liveness,
        "git_sync": git_sync,
        "backup": backup,
        "secrets": secrets,
        "jobs": jobs,
        "index_freshness": index_fresh,
        "embed_coverage": embed_coverage,
        "not_configured": not_configured,
        "ok": (
            drift_ok
            and graph.get("ok", False)
            and outbox_ok
            and backup["ok"]
            and bool(liveness.get("ok"))
            and bool(git_sync.get("ok"))
            and dsn_file_ok
            and index_freshness_ok
            and embed_coverage_ok
            and bool(_graph_backup.get("ok"))
        ),
        "graph_backup": _graph_backup,
        "graph_backup_ok": bool(_graph_backup.get("ok")),
        "graph_offsite": _graph_offsite,
        "graph_offsite_ok": bool(_graph_offsite.get("ok")),
        "drift_ok": drift_ok,
        "graph_drift_ok": bool(graph.get("ok")),
        "outbox_ok": outbox_ok,
        "capture_liveness_ok": bool(liveness.get("ok")),
        "git_sync_ok": bool(git_sync.get("ok")),
        "backup_ok": backup["ok"],
        "dsn_file_ok": dsn_file_ok,
        "index_freshness_ok": index_freshness_ok,
        "embed_coverage_ok": embed_coverage_ok,
    }
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["ok"] else 2


def _escape_like(term: str) -> str:
    """Escape ILIKE metacharacters so user input can't act as a wildcard (F7).

    Backslash first (it's the escape char itself), then the two ILIKE wildcards.
    Paired with ``ESCAPE '\\'`` at each call site.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fair_shares(total: int, n: int) -> list[int]:
    """Split `total` into `n` non-negative shares, remainder to the first ones.

    Pure arithmetic (no DB) so per-kind fairness (F7) is unit testable (F8).
    """
    total = max(0, int(total))
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def _clip_search_row(r) -> dict:
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    return {
        "kind": r[0],
        "id": r[1],
        "label": clip_snippet(r[2], LABEL_LIMIT),
        "snippet": clip_snippet(r[3], SNIPPET_LIMIT),
    }


def _token_match_sql(columns: tuple[str, ...], n: int) -> tuple[str, str]:
    """WHERE any-token-hits and ORDER BY how-many-tokens-hit.

    ``n`` is the token count already in the bound params ``t0``..``t{n-1}``.
    """
    wheres: list[str] = []
    scores: list[str] = []
    for i in range(n):
        ors = " OR ".join(f"{c} ILIKE %(t{i})s ESCAPE '\\'" for c in columns)
        wheres.append(f"({ors})")
        scores.append(f"(CASE WHEN {ors} THEN 1 ELSE 0 END)")
    return " OR ".join(wheres), " + ".join(scores)


def _ilike_token_params(tokens: list[str]) -> dict[str, str]:
    return {f"t{i}": f"%{_escape_like(tok)}%" for i, tok in enumerate(tokens)}


# Episode ILIKE used to search summary only, so a capture tagged
# ``recap-chip`` was invisible unless the summary also said those words.
# Extract JSON is the same family episode_text() already embeds.
_EPISODE_ILIKE_COLUMNS = (
    "summary",
    "COALESCE(topics::text, '')",
    "COALESCE(decisions::text, '')",
    "COALESCE(preferences::text, '')",
    "COALESCE(people::text, '')",
)


def _search_query(cur, term: str, limit: int) -> list[dict]:
    """Deterministically-ordered, per-kind-fair ILIKE search (F7).

    Each kind (topic/episode/node) gets its own fair share of `limit` and its
    own ORDER BY, so one kind can no longer starve the others under a shared
    LIMIT, and results are stable across runs instead of arbitrary scan order.

    Multi-token queries rank by token coverage (OR + hit count), not one
    giant substring. A query that yields no tokens still uses the whole
    escaped term as before.
    """
    topic_n, episode_n, node_n = _fair_shares(limit, 3)
    tokens = search_tokens(term)
    if not tokens:
        tokens = [(term or "").strip()] if (term or "").strip() else []
        if not tokens:
            return []
        params: dict = {"q": f"%{_escape_like(tokens[0])}%"}
        topic_where = (
            "body ILIKE %(q)s ESCAPE '\\' OR slug ILIKE %(q)s ESCAPE '\\' "
            "OR COALESCE(title, '') ILIKE %(q)s ESCAPE '\\'"
        )
        topic_order = "slug ASC"
        episode_where = " OR ".join(
            f"{c} ILIKE %(q)s ESCAPE '\\'" for c in _EPISODE_ILIKE_COLUMNS
        )
        episode_order = "ts DESC NULLS LAST, id DESC"
        node_where = (
            "id ILIKE %(q)s ESCAPE '\\' OR COALESCE(name, '') ILIKE %(q)s ESCAPE '\\'"
        )
        node_order = "id ASC"
    else:
        params = _ilike_token_params(tokens)
        n = len(tokens)
        topic_where, topic_score = _token_match_sql(
            ("body", "slug", "COALESCE(title, '')"), n
        )
        topic_order = f"({topic_score}) DESC, slug ASC"
        episode_where, episode_score = _token_match_sql(_EPISODE_ILIKE_COLUMNS, n)
        episode_order = f"({episode_score}) DESC, ts DESC NULLS LAST, id DESC"
        node_where, node_score = _token_match_sql(("id", "COALESCE(name, '')"), n)
        node_order = f"({node_score}) DESC, id ASC"
    results: list[dict] = []

    if topic_n > 0:
        cur.execute(
            f"""
            SELECT 'topic' AS kind, slug AS id, COALESCE(title, slug) AS label,
                   left(body, 4000) AS snippet
            FROM topics
            WHERE deleted_at IS NULL
              AND ({topic_where})
            ORDER BY {topic_order}
            LIMIT %(lim)s
            """,
            {**params, "lim": topic_n},
        )
        results.extend(_clip_search_row(r) for r in cur.fetchall())

    if episode_n > 0:
        cur.execute(
            f"""
            SELECT 'episode' AS kind, id::text AS id, summary AS label,
                   summary AS snippet
            FROM episodes
            WHERE {episode_where}
            ORDER BY {episode_order}
            LIMIT %(lim)s
            """,
            {**params, "lim": episode_n},
        )
        results.extend(_clip_search_row(r) for r in cur.fetchall())

    if node_n > 0:
        cur.execute(
            f"""
            SELECT 'node' AS kind, id AS id, COALESCE(name, id) AS label,
                   left(COALESCE(payload::text, ''), 4000) AS snippet
            FROM nodes
            WHERE {node_where}
            ORDER BY {node_order}
            LIMIT %(lim)s
            """,
            {**params, "lim": node_n},
        )
        results.extend(_clip_search_row(r) for r in cur.fetchall())

    return results


def cmd_search(args: argparse.Namespace) -> int:
    from khipu.db import connect
    from khipu.topic_graph import enrich_search_results

    if getattr(args, "semantic", False):
        from khipu.embed import semantic_search

        results = semantic_search(
            args.query, limit=args.limit, kind=getattr(args, "kind", None)
        )
        with connect() as conn:
            with conn.cursor() as cur:
                results = enrich_search_results(cur, results)
        print(json.dumps(results, indent=2))
        return 0

    with connect() as conn:
        with conn.cursor() as cur:
            results = enrich_search_results(
                cur, _search_query(cur, args.query, args.limit)
            )
    print(json.dumps(results, indent=2))
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    from khipu.embed import activate, backfill, coverage

    if args.embed_cmd == "status":
        print(json.dumps(coverage(profile=getattr(args, "profile", None)), indent=2))
        return 0
    if args.embed_cmd == "activate":
        try:
            out = activate(args.profile, force=bool(args.force))
        except (ValueError, RuntimeError) as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        print(json.dumps(out, indent=2))
        return 0
    stats = backfill(
        kind=args.kind,
        limit=args.limit,
        dry_run=args.dry_run,
        profile=getattr(args, "profile", None),
    )
    print(json.dumps(stats, indent=2))
    return 0


def cmd_embed_media_backfill(args: argparse.Namespace) -> int:
    """Top-level hyphenated job: native PNG/JPEG into gemini-embedding-2@768."""
    from khipu.embed import backfill_media
    from khipu.jobs import _write_job_state

    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    try:
        stats = backfill_media(
            dry_run=dry_run,
            yes=yes,
            limit=getattr(args, "limit", None),
            source_id=getattr(args, "source_id", None),
            profile=getattr(args, "profile", None),
        )
    except Exception as e:  # noqa: BLE001
        if not dry_run:
            _write_job_state("embed_media_backfill", 1)
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    if stats.get("needs_yes"):
        if not dry_run:
            _write_job_state("embed_media_backfill", 2)
        print(json.dumps({"ok": False, **stats}, indent=2))
        return 2
    if not dry_run:
        _write_job_state("embed_media_backfill", 0)
    print(json.dumps({"ok": True, **stats}, indent=2))
    return 0


def _graph_query(cur, node_id: str, hops: int, limit: int) -> dict:
    """Neighborhood lookup for `khipu graph`.

    hops == 1 returns the direct edges (either direction) plus a best-effort
    SQL/PGQ GRAPH_TABLE sample. hops >= 2 walks a recursive CTE that must stay
    undirected at every step (F4) — the 1-hop branch above is already
    undirected (`e.src = id OR e.dst = id`); the old recursive branch seeded
    and walked outbound-only, which silently dropped inbound-only neighbors
    (e.g. "…__doc" / "…__smry" concept nodes) once hops >= 2. The walk is
    ordered hops-then-node so a shared LIMIT can never let a hop>=2 row bump
    a hop=1 row out of the result — that ordering is what makes "hops>=2 is a
    superset of hops=1" hold under a LIMIT, not just in an unbounded query.
    """
    hops = max(1, int(hops))
    from khipu.topic_graph import (
        PATH_PREFIX,
        TOPIC_PREFIX,
        extract_paths,
        graph_query_aliases,
        topic_slug_from_label,
    )

    episode_meta: dict | None = None
    raw_id = (node_id or "").strip()
    synthetic: list[dict] = []
    if raw_id.isdigit():
        cur.execute(
            "SELECT topics, summary FROM episodes WHERE id = %s",
            (int(raw_id),),
        )
        row = cur.fetchone()
        slugs: list[str] = []
        aliases: list[str] = []
        if row is None:
            episode_meta = {"id": int(raw_id), "missing": True, "topics": []}
        else:
            topics_raw, summary = row
            for item in topics_raw or []:
                slug = topic_slug_from_label(str(item))
                if slug:
                    slugs.append(slug)
            slugs = list(dict.fromkeys(slugs))
            for slug in slugs:
                aliases.extend(graph_query_aliases(slug))
            for rel in extract_paths(summary or ""):
                aliases.append(PATH_PREFIX + rel.rstrip("/"))
            aliases = list(dict.fromkeys(a for a in aliases if a))
            episode_meta = {
                "id": int(raw_id),
                "missing": False,
                "topics": slugs,
            }
            synthetic = [
                {
                    "src": f"episode:{raw_id}",
                    "dst": f"{TOPIC_PREFIX}{slug}",
                    "type": "capture_topic",
                    "weight": 1.0,
                }
                for slug in slugs
            ]
    else:
        aliases = graph_query_aliases(node_id)

    if hops == 1:
        sql_edges: list[tuple] = []
        gt: list[tuple] = []
        if aliases:
            cur.execute(
                """
                SELECT e.src, e.dst, e.type, e.weight
                FROM edges e
                WHERE e.src = ANY(%(ids)s) OR e.dst = ANY(%(ids)s)
                ORDER BY (CASE WHEN e.src = ANY(%(ids)s) THEN e.dst ELSE e.src END) ASC, e.type ASC
                LIMIT %(lim)s
                """,
                {"ids": aliases, "lim": limit},
            )
            sql_edges = cur.fetchall()
            try:
                cur.execute(
                    """
                    SELECT * FROM GRAPH_TABLE (
                      alzy_graph
                      MATCH (a IS node WHERE a.id = ANY(%(ids)s))-[r IS edge]->(b IS node)
                      COLUMNS (a.id AS src, b.id AS dst, r.type AS edge_type)
                    ) LIMIT %(lim)s
                    """,
                    {"ids": aliases, "lim": limit},
                )
                gt = cur.fetchall()
            except Exception:  # noqa: BLE001 — beta GRAPH_TABLE may flake
                gt = []
        merged = synthetic + [
            {"src": a, "dst": b, "type": t, "weight": w} for a, b, t, w in sql_edges
        ]
        out = {
            "id": node_id,
            "hops": 1,
            "edges": merged[:limit],
            "graph_table": [{"src": a, "dst": b, "type": t} for a, b, t in gt],
        }
        if episode_meta is not None:
            out["episode"] = episode_meta
        return out

    walk_rows: list[tuple] = []
    if aliases:
        cur.execute(
            """
            WITH RECURSIVE walk AS (
              SELECT
                CASE WHEN e.src = ANY(%(ids)s) THEN e.dst ELSE e.src END AS node_id,
                %(id)s AS via,
                e.type,
                1 AS hops
              FROM edges e
              WHERE e.src = ANY(%(ids)s) OR e.dst = ANY(%(ids)s)
              UNION
              SELECT
                CASE WHEN e.src = w.node_id THEN e.dst ELSE e.src END AS node_id,
                w.node_id AS via,
                e.type,
                w.hops + 1
              FROM walk w
              JOIN edges e ON e.src = w.node_id OR e.dst = w.node_id
              WHERE w.hops < %(max_hops)s
            )
            SELECT node_id, via, type, hops FROM walk
            ORDER BY hops ASC, node_id ASC
            LIMIT %(lim)s
            """,
            {"ids": aliases, "id": node_id, "max_hops": hops, "lim": limit},
        )
        walk_rows = cur.fetchall()
    synthetic_walk = [
        {
            "node_id": e["dst"],
            "via": e["src"],
            "type": e["type"],
            "hops": 1,
        }
        for e in synthetic
    ]
    out = {
        "id": node_id,
        "hops": hops,
        "walk": (synthetic_walk + [
            {"node_id": a, "via": b, "type": t, "hops": h} for a, b, t, h in walk_rows
        ])[:limit],
    }
    if episode_meta is not None:
        out["episode"] = episode_meta
    return out


def cmd_graph(args: argparse.Namespace) -> int:
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            out = _graph_query(cur, args.id, args.hops, args.limit)
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_topic_graph_backfill(args: argparse.Namespace) -> int:
    from khipu.mirror import backfill_topic_graph

    mem = _require_memory_root(args)
    if mem is None:
        return 2
    stats = backfill_topic_graph(mem, dry_run=bool(getattr(args, "dry_run", False)))
    print(json.dumps(stats, indent=2, default=str))
    return 0


# Secrets the UI and CLI may write. Anything outside this set is refused rather
# than silently creating a stray Keychain item under the Khipu service.
# Keyed by account, because secrets_status() does not name its fields after the
# Keychain accounts: database_url reports as "dsn_in_keychain", not "database_*".
SETTABLE_SECRETS = {
    "gemini_api_key": "gemini_in_keychain",
    "database_url": "dsn_in_keychain",
    "openai_compat_api_key": "openai_compat_in_keychain",
}


def cmd_secrets(args: argparse.Namespace) -> int:
    from khipu.keychain import secrets_status, set_password

    account = getattr(args, "set", None)
    if account:
        if account not in SETTABLE_SECRETS:
            print(
                json.dumps({"ok": False, "error": f"not a settable secret: {account}"})
            )
            return 2
        # The value arrives on stdin, never as an argument: argv is world-readable
        # through `ps`, and a secret typed as a flag would also land in shell
        # history. Only the trailing newline the caller adds is stripped.
        value = sys.stdin.read().strip()
        if not value:
            print(json.dumps({"ok": False, "error": "empty value on stdin"}))
            return 2
        try:
            set_password(account, value)
        except (RuntimeError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        # Report presence by re-reading, so a success claim reflects the
        # Keychain's state rather than the absence of an exception.
        status = secrets_status()
        stored = bool(status.get(SETTABLE_SECRETS[account]))
        print(json.dumps({"ok": True, "account": account, "stored": stored}))
        return 0

    print(json.dumps(secrets_status(), indent=2))
    return 0


def cmd_activity(args: argparse.Namespace) -> int:
    from khipu.activity import activity_payload, episode_detail

    if args.show is not None:
        row = episode_detail(args.show)
        if row is None:
            print(f"error: episode {args.show} not found", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, default=str))
        return 0
    print(json.dumps(activity_payload(limit=args.limit), indent=2, default=str))
    return 0


def cmd_regen_memory(args: argparse.Namespace) -> int:
    if getattr(args, "index", False) is True:
        from khipu.jobs import BUILD_INDEX, run_build_index

        # build_index.py hardcodes ROOT; --memory-root cannot redirect it.
        engine_root = BUILD_INDEX.resolve().parent.parent
        mem = getattr(args, "memory_root", None)
        if mem:
            passed = Path(mem).resolve()
            if passed != engine_root:
                print(
                    f"error: --memory-root {passed} does not match build_index.py "
                    f"engine root {engine_root} (the engine hardcodes ROOT; "
                    "omit --memory-root or pass that path)",
                    file=sys.stderr,
                )
                return 2
        rc = run_build_index()
        print(f"build_index exited {rc} (memory root {engine_root})")
        return rc

    from khipu.memory_md import regen_memory_md

    out = args.out
    if not out:
        mem = _require_memory_root(args)
        if mem is None:
            return 2
        out = mem / "MEMORY.from-khipu.md"
    n = regen_memory_md(Path(out), limit=args.limit)
    print(f"wrote {n} topics → {out}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from khipu.mirror import reconcile_memory_root

    mem = _require_memory_root(args)
    if mem is None:
        return 2
    stats = reconcile_memory_root(mem)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    from khipu.capture import capture, load_payload

    raw = (
        Path(args.payload_file).read_text(encoding="utf-8")
        if args.payload_file
        else sys.stdin.read()
    )
    return capture(load_payload(raw), mode=args.mode)


def cmd_config(args: argparse.Namespace) -> int:
    from khipu.config import (
        capture_mode,
        config_file,
        gateway_url,
        load_config,
        set_capture_mode,
        set_gateway_url,
    )

    if args.set_capture_mode:
        path = set_capture_mode(args.set_capture_mode)
        print(json.dumps({"capture_mode": capture_mode(), "config_file": str(path)}))
        return 0
    if args.set_gateway_url is not None:
        path = set_gateway_url(args.set_gateway_url)
        print(json.dumps({"gateway_url": gateway_url(), "config_file": str(path)}))
        return 0
    if args.set or args.unset:
        from khipu.config import path_settings_status, set_path_setting

        try:
            if args.set:
                key, value = args.set
                path = set_path_setting(key, value)
            else:
                key, path = args.unset, set_path_setting(args.unset, None)
        except KeyError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        print(
            json.dumps(
                {"ok": True, key: path_settings_status()[key], "config_file": str(path)}
            )
        )
        return 0
    from khipu.config import path_settings_status

    out = {
        "capture_mode": capture_mode(),
        "gateway_url": gateway_url() or None,
        "capture_mode_source": (
            "env"
            if os.environ.get("KHIPU_CAPTURE_MODE")
            else "file"
            if "capture_mode" in load_config()
            else "default"
        ),
        "paths": path_settings_status(),
        "config_file": str(config_file()),
        "config": load_config(),
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from khipu.migrate import run

    out = run(dry_run=bool(args.dry_run))
    print(json.dumps(out, indent=2))
    # A pending list after a real run means a migration failed to apply.
    return 0 if args.dry_run or not out["pending"] else 1


def cmd_outbox(args: argparse.Namespace) -> int:
    """Offline outbox: captures whose PG write failed, replayed when PG is back."""
    from khipu import outbox

    if args.outbox_cmd == "status":
        print(json.dumps(outbox.status(), indent=2))
        return 0
    out = outbox.drain(limit=args.limit)
    out["remaining"] = outbox.status()["pending"]
    print(json.dumps(out, indent=2))
    return 0 if out["failed"] == 0 else 2


def cmd_graph_sync(args: argparse.Namespace) -> int:
    """Mirror graphify's graph.sqlite into PG (idempotent) or report drift."""
    from khipu.graph_sync import graph_drift, sync_from_sqlite

    sqlite = Path(args.sqlite) if args.sqlite else None
    if args.check:
        d = graph_drift(sqlite)
        print(json.dumps(d, indent=2))
        return 0 if d.get("ok") else 2
    out = sync_from_sqlite(sqlite, dry_run=args.dry_run)
    print(json.dumps(out, indent=2))
    return 0 if args.dry_run or out.get("drift", {}).get("ok") else 2


def cmd_sessions(args: argparse.Namespace) -> int:
    """Native session capture across every harness: the shared queue, per-harness
    heartbeat + liveness, and the drain (model extraction + capture) that turns
    queued sessions into episodes. `khipu aegis ...` is the older name for the
    same thing and still works."""
    from khipu import session_capture as sc

    if args.aegis_cmd == "status":
        harness = getattr(args, "harness", None)
        print(
            json.dumps(
                sc.status(harness)
                if harness and harness != "all"
                else sc.liveness_all(),
                indent=2,
            )
        )
        return 0
    if args.aegis_cmd == "liveness":
        lv = sc.liveness_all()
        print(json.dumps(lv, indent=2))
        return 0 if lv["ok"] else 2
    out = sc.drain(limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(out, indent=2))
    return 0 if out["failed"] == 0 else 2


def cmd_git_sync(args: argparse.Namespace) -> int:
    """Is the memory tree's nightly git auto-sync landing? Exit 2 when red."""
    from khipu.git_sync_health import status

    out = status()
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["ok"] else 2


cmd_aegis = cmd_sessions


def cmd_integrations(args: argparse.Namespace) -> int:
    from khipu import integrations as integ

    targets = list(integ.HARNESSES) if args.harness == "all" else [args.harness]
    project = getattr(args, "project", None)

    def _status(h):
        return integ.status(h, project=project) if h == "grok_bot" else integ.status(h)

    def _verify(h):
        return integ.verify(h, project=project) if h == "grok_bot" else integ.verify(h)

    if args.integ_cmd == "status":
        print(json.dumps([_status(h) for h in targets], indent=2))
        return 0
    if args.integ_cmd == "verify":
        results = [_verify(h) for h in targets]
        print(json.dumps(results, indent=2))
        return 0 if all(r.get("ok", not r["detected"]) for r in results) else 2
    fn = integ.install if args.integ_cmd == "install" else integ.uninstall
    results = [fn(h, dry_run=args.dry_run, project=project) for h in targets]
    print(json.dumps(results, indent=2))
    if args.integ_cmd == "install" and not args.dry_run and not args.no_verify:
        verified = [_verify(h) for h in targets]
        print(json.dumps({"verify": verified}, indent=2))
        return 0 if all(v.get("ok", not v["detected"]) for v in verified) else 2
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    from khipu.paths import paths_status, set_data_dir

    if args.set:
        path = set_data_dir(Path(args.set))
        print(json.dumps({"ok": True, "data_dir": str(path)}, indent=2))
        return 0
    print(json.dumps(paths_status(), indent=2))
    return 0


def cmd_backup_local(args: argparse.Namespace) -> int:
    from khipu.paths import backup_local

    out = backup_local(dest=Path(args.out))
    print(json.dumps(out, indent=2))
    return 0


def cmd_nightly(_args: argparse.Namespace) -> int:
    from khipu.jobs import run_nightly

    return run_nightly()


def cmd_monthly(args: argparse.Namespace) -> int:
    from khipu.jobs import run_monthly

    return run_monthly(dry_run=bool(args.dry_run))


def cmd_graph_build(_args: argparse.Namespace) -> int:
    from khipu.jobs import run_graph_build

    return run_graph_build()


def cmd_models(args: argparse.Namespace) -> int:
    from khipu import models as models_mod

    action = getattr(args, "models_cmd", None) or "show"
    try:
        if action == "show":
            print(models_mod.dump_show_json())
            return 0
        if action == "set":
            raw_json = getattr(args, "models_json", None)
            role = getattr(args, "role", None)
            if raw_json:
                if role:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "error": "pass either a JSON blob or --role flags, not both",
                            }
                        )
                    )
                    return 2
                try:
                    payload = json.loads(raw_json)
                except ValueError as e:
                    print(json.dumps({"ok": False, "error": f"invalid JSON: {e}"}))
                    return 2
                if not isinstance(payload, dict):
                    print(json.dumps({"ok": False, "error": "JSON payload must be an object"}))
                    return 2
                out = models_mod.set_models_replace(payload)
                print(json.dumps({"ok": True, "models": out}, indent=2))
                return 0
            if not role:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "models set requires a JSON blob or --role …",
                        }
                    )
                )
                return 2
            provider = getattr(args, "provider", None)
            if not provider:
                print(json.dumps({"ok": False, "error": "--provider is required with --role"}))
                return 2
            endpoint = getattr(args, "endpoint", None)
            model_id = getattr(args, "model_id", None)
            out = models_mod.set_models_merge_role(
                role,
                provider=provider,
                endpoint=endpoint,
                model_id=model_id,
            )
            print(json.dumps({"ok": True, "models": out}, indent=2))
            return 0
        print(json.dumps({"ok": False, "error": f"unknown models action: {action}"}))
        return 2
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


def cmd_sources(args: argparse.Namespace) -> int:
    from khipu import sources

    action = getattr(args, "sources_cmd", None)
    try:
        if action == "list":
            out = {
                "sources": sources.load_sources().get("sources", []),
                "resolved": sources.resolve_for_graphify(),
                "graph_producer": __import__(
                    "khipu.graph_sync", fromlist=["is_graph_producer"]
                ).is_graph_producer(),
            }
            print(json.dumps(out, indent=2, default=str))
            return 0
        if action == "enable":
            sources.set_enabled(args.source_id, True)
            print(json.dumps({"ok": True, "id": args.source_id, "enabled": True}))
            return 0
        if action == "disable":
            sources.set_enabled(args.source_id, False)
            print(json.dumps({"ok": True, "id": args.source_id, "enabled": False}))
            return 0
        if action == "set-embed-media":
            raw = str(getattr(args, "embed_media_value", "")).strip().lower()
            if raw not in ("on", "off"):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "set-embed-media expects on|off",
                        }
                    )
                )
                return 2
            on = raw == "on"
            sources.set_embed_media(args.source_id, on)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "id": args.source_id,
                        "embed_media": on,
                    }
                )
            )
            return 0
        if action == "add":
            sources.add_code_root(Path(args.root))
            print(json.dumps({"ok": True, "root": args.root}))
            return 0
        if action == "remove":
            sources.remove_user_source(args.source_id)
            print(json.dumps({"ok": True, "id": args.source_id}))
            return 0
        if action == "export":
            out = sources.export_resolved()
            print(json.dumps({"ok": True, "path": str(sources.resolved_path()), "resolved": out}))
            return 0
        print(json.dumps({"ok": False, "error": f"unknown sources action: {action}"}))
        return 2
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


def cmd_graph_backup(args: argparse.Namespace) -> int:
    from khipu import graph_backup

    action = getattr(args, "graph_backup_cmd", None)
    if action == "record-local":
        out = graph_backup.record_local()
    elif action == "offsite":
        out = graph_backup.run_offsite()
    elif action == "drill":
        out = graph_backup.scratch_drill()
    elif action == "status":
        out = graph_backup.status_payload()
        print(json.dumps(out, indent=2, default=str))
        return 0
    else:
        print(
            json.dumps({"ok": False, "error": f"unknown graph-backup action: {action}"})
        )
        return 2
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", False) else 2


def cmd_import_local(args: argparse.Namespace) -> int:
    from khipu.paths import import_local

    out = import_local(source=Path(args.source), merge=not args.replace)
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="khipu", description="Khipu memory hub CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="PG counts + optional drift")
    s.add_argument("--memory-root", default=_memory_root_default())
    s.add_argument(
        "--sample",
        type=int,
        default=0,
        help="file↔pg hash sample size (0 = PG-only conflicts, skip NAS walk)",
    )
    s.add_argument(
        "--drift",
        action="store_true",
        help="Include sample_drift (slower; doctor already covers this)",
    )
    s.set_defaults(func=cmd_status)

    d = sub.add_parser("doctor", help="Drift samples + health JSON")
    d.add_argument("--memory-root", default=_memory_root_default())
    # Default None = check every topic. It used to be 25, and because the walk
    # is alphabetical that meant doctor compared the same first 4% of 622 topics
    # forever (audit 2026-08-17). The full pass measures 0.09 s.
    d.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Cap the topic drift pass at N topics (default: all)",
    )
    d.set_defaults(func=cmd_doctor)

    rv = sub.add_parser(
        "revisions",
        help="Topic revisions + conflict visibility (LWW losers queryable)",
    )
    rv.add_argument("--memory-root", default=_memory_root_default())
    rv.add_argument("--limit", type=int, default=40)
    # Default None = compare every topic. It was 40, and because the walk is
    # alphabetical this report cleared all 622 topics after checking the first
    # 40 (audit 2026-08-17) — the same defect doctor's --sample carried.
    rv.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Cap the file↔pg pass at N topics (0 = PG-only, default: all)",
    )
    rv.add_argument("--slug", default=None, help="Filter recent revisions to one slug")
    rv.add_argument(
        "--show",
        type=int,
        default=None,
        metavar="ID",
        help="Print full body for one topic_revisions.id",
    )
    rv.set_defaults(func=cmd_revisions)

    act = sub.add_parser(
        "activity",
        help="Recent capture_v2 → PG episodes (read/inspect)",
    )
    act.add_argument("--limit", type=int, default=40)
    act.add_argument(
        "--show",
        type=int,
        default=None,
        metavar="ID",
        help="Full episode row by id",
    )
    act.set_defaults(func=cmd_activity)

    sec = sub.add_parser(
        "secrets",
        help="Keychain/env/file presence for DSN + Gemini (no secret values)",
    )
    sec.add_argument(
        "--set",
        metavar="ACCOUNT",
        choices=SETTABLE_SECRETS,
        help="store a secret read from stdin (never pass the value as an argument)",
    )
    sec.set_defaults(func=cmd_secrets)

    se = sub.add_parser(
        "search", help="ILIKE search topics/episodes/nodes (or --semantic)"
    )
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=20)
    se.add_argument(
        "--semantic",
        action="store_true",
        help="Cosine search over memory_embeddings (active profile) instead of ILIKE",
    )
    se.add_argument(
        "--kind",
        choices=("episode", "topic", "media"),
        help="Semantic: restrict to one kind",
    )
    se.set_defaults(func=cmd_search)

    em = sub.add_parser(
        "embed", help="Vectors: backfill / status / activate (profiles)"
    )
    em_sub = em.add_subparsers(dest="embed_cmd", required=True)
    bf = em_sub.add_parser(
        "backfill", help="Embed missing/changed episode+topic chunks"
    )
    bf.add_argument("--kind", choices=("episode", "topic"))
    bf.add_argument("--limit", type=int, help="Cap chunks this run (pilot / smoke)")
    bf.add_argument("--dry-run", action="store_true", help="Count only; no API calls")
    bf.add_argument(
        "--profile",
        help="Target profile id (default: active). e.g. gemini-embedding-2@768",
    )
    st = em_sub.add_parser(
        "status", help="Coverage per kind for active or named profile"
    )
    st.add_argument(
        "--profile",
        help="Profile id to report (default: active)",
    )
    act = em_sub.add_parser(
        "activate",
        help="Flip the one-active search pointer (refuses if coverage incomplete)",
    )
    act.add_argument(
        "profile", help="Profile id to activate, e.g. gemini-embedding-2@768"
    )
    act.add_argument(
        "--force",
        action="store_true",
        help="Activate even when the profile still has missing vectors",
    )
    em.set_defaults(func=cmd_embed)

    g = sub.add_parser("graph", help="Neighbors (join / GRAPH_TABLE / CTE)")
    g.add_argument("id", help="Node id")
    g.add_argument("--hops", type=int, default=1)
    g.add_argument("--limit", type=int, default=50)
    g.set_defaults(func=cmd_graph)

    r = sub.add_parser("regen-memory", help="Write MEMORY.md from PG topics")
    r.add_argument(
        "--out",
        default=None,  # resolved against memory_root at run time
    )
    r.add_argument("--limit", type=int, default=200)
    r.add_argument(
        "--index",
        action="store_true",
        help="Rebuild live MEMORY.md index via build_index.py (file wiki SSOT); "
        "default without --index writes MEMORY.from-khipu.md sidecar from PG",
    )
    r.add_argument("--memory-root", default=_memory_root_default())
    r.set_defaults(func=cmd_regen_memory)

    nt = sub.add_parser("nightly", help="Run consolidate_nightly.py (memory wiki)")
    nt.set_defaults(func=cmd_nightly)

    mo = sub.add_parser(
        "monthly",
        help="Run conversation-memory-monthly.py (wiki classify)",
    )
    mo.add_argument(
        "--dry-run",
        action="store_true",
        help="Only for Cursor-era consolidate_monthly.py; refused on the live Claude driver",
    )
    mo.set_defaults(func=cmd_monthly)

    gb = sub.add_parser(
        "graph-build", help="Run graphify_nightly.py once (graph.sqlite)"
    )
    gb.set_defaults(func=cmd_graph_build)

    emb = sub.add_parser(
        "embed-media-backfill",
        help="Embed PNG/JPEG under sources with embed_media (active Gemini Embedding 2)",
    )
    emb.add_argument("--dry-run", action="store_true", help="Count only; no API calls")
    emb.add_argument(
        "--yes",
        action="store_true",
        help=f"Required when more than {1000} images would be scanned",
    )
    emb.add_argument("--limit", type=int, default=None, help="Cap files this run")
    emb.add_argument(
        "--source-id",
        dest="source_id",
        default=None,
        help="Only walk this source id (must have embed_media on + root)",
    )
    emb.add_argument(
        "--profile",
        default=None,
        help="Profile id (default gemini-embedding-2@768)",
    )
    emb.set_defaults(func=cmd_embed_media_backfill)

    md = sub.add_parser(
        "models",
        help="Per-role model Settings (synth / embed / vision): show / set",
    )
    md_sub = md.add_subparsers(dest="models_cmd", required=False)
    md_sub.add_parser("show", help="JSON: three roles + models_error").set_defaults(
        func=cmd_models, models_cmd="show"
    )
    md_set = md_sub.add_parser(
        "set",
        help="Merge one role (--role …) or replace all three (JSON blob)",
    )
    md_set.add_argument(
        "models_json",
        nargs="?",
        default=None,
        help="Full models object JSON (all three roles); replaces the whole models key",
    )
    md_set.add_argument(
        "--role",
        choices=("synth", "embed", "vision"),
        default=None,
        help="Merge one role (other roles unchanged)",
    )
    md_set.add_argument(
        "--provider",
        choices=("cloud", "local", "off"),
        default=None,
        help="Provider for --role merge",
    )
    md_set.add_argument("--endpoint", default=None, help="Local endpoint origin")
    md_set.add_argument(
        "--model-id",
        dest="model_id",
        default=None,
        help="Model id for the role",
    )
    md_set.set_defaults(func=cmd_models, models_cmd="set")
    md.set_defaults(func=cmd_models, models_cmd="show")

    src = sub.add_parser("sources", help="Graph membership: list / enable / disable / add / export")
    src_sub = src.add_subparsers(dest="sources_cmd", required=True)
    src_sub.add_parser("list", help="JSON: sources + resolve_for_graphify").set_defaults(
        func=cmd_sources
    )
    en = src_sub.add_parser("enable", help="Enable a seeded or user source")
    en.add_argument("source_id")
    en.set_defaults(func=cmd_sources)
    dis = src_sub.add_parser("disable", help="Disable a source (does not purge PG)")
    dis.add_argument("source_id")
    dis.set_defaults(func=cmd_sources)
    sem = src_sub.add_parser(
        "set-embed-media",
        help="Opt a source into native image embed (on|off; default off)",
    )
    sem.add_argument("source_id")
    sem.add_argument(
        "embed_media_value",
        choices=("on", "off"),
        help="on = walk PNG/JPEG under this source's root on embed-media-backfill",
    )
    sem.set_defaults(func=cmd_sources)
    add = src_sub.add_parser("add", help="Add a code_ast root (absolute path)")
    add.add_argument("--root", required=True, dest="root", help="Absolute code root")
    add.set_defaults(func=cmd_sources)
    rem = src_sub.add_parser("remove", help="Remove a user-added source row")
    rem.add_argument("source_id")
    rem.set_defaults(func=cmd_sources)
    src_sub.add_parser("export", help="Write graph_sources.resolved.json").set_defaults(
        func=cmd_sources
    )
    src.set_defaults(func=cmd_sources)

    gbu = sub.add_parser(
        "graph-backup",
        help="Graph.sqlite snapshot record / offsite / drill / status (producer Mac)",
    )
    gbu_sub = gbu.add_subparsers(dest="graph_backup_cmd", required=True)
    gbu_sub.add_parser(
        "record-local",
        help="Integrity-check latest snapshot and record graph_snapshot ops_event",
    ).set_defaults(func=cmd_graph_backup)
    gbu_sub.add_parser(
        "offsite",
        help="rclone copyto latest snapshot to r2:matt-db-backups/khipu-graph",
    ).set_defaults(func=cmd_graph_backup)
    gbu_sub.add_parser(
        "status", help="Local + offsite health and last ops_events"
    ).set_defaults(func=cmd_graph_backup)
    gbu_sub.add_parser(
        "drill",
        help="Scratch restore drill on latest snapshot (never touches live graph.sqlite)",
    ).set_defaults(func=cmd_graph_backup)
    gbu.set_defaults(func=cmd_graph_backup)

    rc = sub.add_parser("reconcile", help="Full file→PG episodes/topics sync")
    rc.add_argument("--memory-root", default=_memory_root_default())
    rc.set_defaults(func=cmd_reconcile)

    paths = sub.add_parser(
        "paths",
        help="Show or set Mac-local data directory (DSN/certs/cache files)",
    )
    paths.add_argument(
        "--set",
        default=None,
        metavar="DIR",
        help="Set local data folder (copies dsn/root.crt if missing)",
    )
    paths.set_defaults(func=cmd_paths)

    bl = sub.add_parser(
        "backup-local",
        help="Zip Mac-local data dir for download/backup",
    )
    bl.add_argument(
        "--out",
        required=True,
        help="Destination .zip path, or a directory to write khipu-local-*.zip into",
    )
    bl.set_defaults(func=cmd_backup_local)

    il = sub.add_parser(
        "import-local",
        help="Import a local backup zip/dir into the Mac data folder",
    )
    il.add_argument("--source", required=True, help="Path to .zip or directory")
    il.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite existing files (default merges / skips existing)",
    )
    il.set_defaults(func=cmd_import_local)

    cap = sub.add_parser(
        "capture",
        help="Capture an episode (JSON payload on stdin) — routed by capture_mode",
    )
    cap.add_argument("--payload-file", help="Read the JSON payload from a file")
    cap.add_argument(
        "--mode",
        choices=("legacy", "dual", "hub"),
        help="Override capture_mode for this run",
    )
    cap.set_defaults(func=cmd_capture)

    mg = sub.add_parser(
        "migrate", help="Apply pending ops/migrations/*.sql to the database"
    )
    mg.add_argument(
        "--dry-run", action="store_true", help="Show applied/pending; change nothing"
    )
    mg.set_defaults(func=cmd_migrate)

    cfg = sub.add_parser("config", help="Show / set Hub config (capture_mode, paths)")
    cfg.add_argument(
        "--set",
        nargs=2,
        metavar=("KEY", "PATH"),
        help="Persist a machine-specific path: memory_root, memory_repo, "
        "capture_v2, graph_sqlite, gemini_key_file",
    )
    cfg.add_argument("--unset", metavar="KEY", help="Remove a path setting")
    cfg.add_argument(
        "--set-gateway-url",
        metavar="URL",
        help="Public https:// URL of the Khipu gateway (empty string clears it)",
    )
    cfg.add_argument(
        "--set-capture-mode",
        choices=("legacy", "dual", "hub"),
        help="Persist capture_mode to the Hub config file",
    )
    cfg.set_defaults(func=cmd_config)

    ig = sub.add_parser(
        "integrations",
        help="Per-harness native packs: install / verify / uninstall / status",
    )
    ig_sub = ig.add_subparsers(dest="integ_cmd", required=True)
    for name, help_ in (
        (
            "install",
            "Write MCP + Khipu stop-hook entries (alongside legacy), then verify",
        ),
        ("verify", "Probe MCP handshake + hook exit for installed components"),
        ("uninstall", "Remove Khipu-owned entries only (backups kept)"),
        ("status", "Which components are present per harness"),
    ):
        sp = ig_sub.add_parser(name, help=help_)
        sp.add_argument(
            "harness",
            nargs="?",
            default="all",
            choices=("all", "claude_code", "cursor", "aegis", "codex", "grok_bot"),
        )
        if name in ("install", "uninstall"):
            sp.add_argument("--dry-run", action="store_true")
        if name in ("install", "uninstall", "status", "verify"):
            sp.add_argument(
                "--project",
                help="Cursor / Grok Bot: repo dir for .cursor/rules/khipu.mdc and .cursor/mcp.json",
            )
        if name == "install":
            sp.add_argument("--no-verify", action="store_true")
    ig.set_defaults(func=cmd_integrations)

    for name, help_ in (
        (
            "sessions",
            "Native session capture (every harness): drain / status / liveness",
        ),
        ("aegis", "Older name for `sessions`; same queue, same drain"),
    ):
        ag = sub.add_parser(name, help=help_)
        ag_sub = ag.add_subparsers(dest="aegis_cmd", required=True)
        dr = ag_sub.add_parser(
            "drain", help="Turn queued sessions (any harness) into episodes"
        )
        dr.add_argument(
            "--limit", type=int, default=None, help="Process at most N jobs"
        )
        dr.add_argument(
            "--dry-run", action="store_true", help="Extract and print; write nothing"
        )
        stt = ag_sub.add_parser(
            "status",
            help="Per-harness liveness (default) or one harness's queue + last dispatch",
        )
        stt.add_argument(
            "--harness",
            default="aegis" if name == "aegis" else "all",
            choices=("all", "claude_code", "cursor", "codex", "aegis"),
        )
        ag_sub.add_parser(
            "liveness",
            help="Red/green per harness; exit 2 if any harness is not being recorded",
        )
        ag.set_defaults(func=cmd_sessions)

    gsy = sub.add_parser(
        "git-sync",
        help="Memory-tree git auto-sync liveness (heartbeat + repo evidence); exit 2 if red",
    )
    gsy.set_defaults(func=cmd_git_sync)

    gs = sub.add_parser(
        "graph-sync",
        help="Mirror graphify's graph.sqlite into PG (nodes/edges); --check reports drift only",
    )
    gs.add_argument(
        "--sqlite",
        help="Path to graph.sqlite (default: KHIPU_GRAPH_SQLITE or the Graph volume)",
    )
    gs.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the sync in a transaction and roll back",
    )
    gs.add_argument(
        "--check", action="store_true", help="Drift only; exit 2 when not zero"
    )
    gs.set_defaults(func=cmd_graph_sync)

    ob = sub.add_parser(
        "outbox", help="Offline outbox: drain (replay into PG) / status"
    )
    ob_sub = ob.add_subparsers(dest="outbox_cmd", required=True)
    obd = ob_sub.add_parser("drain", help="Replay queued captures into PG")
    obd.add_argument("--limit", type=int, default=None)
    ob_sub.add_parser("status", help="Pending count + oldest age")
    ob.set_defaults(func=cmd_outbox)

    tg = sub.add_parser(
        "topic-graph",
        help="Persist topic wiki/path graph (topics.links + Khipu-owned nodes/edges)",
    )
    tg_sub = tg.add_subparsers(dest="topic_graph_cmd", required=True)
    tgb = tg_sub.add_parser(
        "backfill",
        help="Walk all topic .md files: UPDATE topics.links/frontmatter and mint topic:/path: edges",
    )
    tgb.add_argument(
        "--dry-run",
        action="store_true",
        help="Report column updates + graph upserts; roll back writes",
    )
    tgb.add_argument("--memory-root", default=_memory_root_default())
    tg.set_defaults(func=cmd_topic_graph_backfill)

    gb = sub.add_parser(
        "grok-bot-config",
        help="Print the account-level Cursor cloud config (covers every repo) + the secret to add",
    )
    gb.set_defaults(
        func=lambda a: (
            print(
                json.dumps(
                    __import__(
                        "khipu.integrations", fromlist=["x"]
                    ).grok_bot_account_config(),
                    indent=2,
                )
            )
            or 0
        )
    )

    return p


def main(argv: list[str] | None = None) -> int:
    _add_paths()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as e:  # noqa: BLE001 — CLI surface
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
