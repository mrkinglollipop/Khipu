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
    from khipu.hub_snapshot import (
        hub_connection_failed,
        snapshot_freshness,
        status_payload_snapshot,
    )

    mem = Path(args.memory_root) if args.memory_root else None
    try:
        data = status_payload(
            mem,
            conflict_sample=int(args.sample),
            include_drift=bool(args.drift),
        )
        # Do not dump hub_snapshot here. Status is the tab people click; a full
        # embedding replica over Tailscale froze the desktop. Doctor (throttled)
        # and `khipu snapshot refresh` own the dump. snapshot_freshness (W2.4)
        # is still cheap — it only compares two timestamps already in hand.
        data["hub_snapshot"] = snapshot_freshness(data.get("latest_ingested_at"))
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        data = status_payload_snapshot()
        if mem:
            data["status_error"] = f"{type(exc).__name__}: {exc}"
    # W6.2: recall-quality ratios, same block `khipu doctor` reports. Wrapped
    # separately so a metrics-query failure never blanks the rest of status.
    try:
        from khipu.drift import recall_quality

        data["recall_quality"] = recall_quality(hub_snapshot=data.get("hub_snapshot"))
    except Exception as exc:  # noqa: BLE001
        data["recall_quality"] = {"error": f"{type(exc).__name__}: {exc}"}
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
    from khipu.hub_snapshot import (
        hub_connection_failed,
        maybe_refresh,
        snapshot_freshness,
        snapshot_health,
        status_payload_snapshot,
    )
    from khipu.keychain import secrets_status

    mem = Path(args.memory_root) if args.memory_root else None
    hub_ok = True
    try:
        status = status_payload(None)
        maybe_refresh()
    except Exception as exc:
        if hub_connection_failed(exc):
            hub_ok = False
            status = status_payload_snapshot()
            status["hub_error"] = f"{type(exc).__name__}: {exc}"
        else:
            raise
    # W2.4: behind_ingest_seconds / ok=false reason=snapshot_behind_ingest only
    # means something while PG is reachable — with the hub down, hub_ok is
    # already red from a different, more specific check.
    hub_snapshot = (
        snapshot_freshness(status.get("latest_ingested_at"))
        if hub_ok
        else snapshot_health()
    )
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
    # W6.1: `khipu doctor --probe` is the ONLY way this command writes anything
    # — it runs a fresh end-to-end capture-then-search probe (khipu.probe) and
    # records the result. Plain `khipu doctor` only reads that last recorded
    # result (probe.status()) and goes red when it is missing, failed, or
    # older than 7 days — "the host runs it" has to be proven, not assumed.
    if getattr(args, "probe", False):
        try:
            from khipu import probe

            probe_harness = getattr(args, "harness", None) or _env("KHIPU_HARNESS", default="doctor")
            probe.run_probe(probe_harness)
        except Exception:  # noqa: BLE001 — a probe crash must not crash doctor;
            pass          # probe.status() below reports whatever it managed to record
    try:
        from khipu import probe

        recall_probe = probe.status()
    except Exception as e:  # noqa: BLE001
        recall_probe = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    # W6.2: recall-quality ratios (fragmentation, dangling topics, junk paths,
    # commitments, query zero-result rate). Surfaced for visibility; per the
    # scope these only warn and never gate doctor's overall ok below.
    try:
        from khipu.drift import recall_quality

        recall_quality_block = recall_quality(hub_snapshot=hub_snapshot)
    except Exception as e:  # noqa: BLE001
        recall_quality_block = {"error": f"{type(e).__name__}: {e}"}
    out = {
        "status": status,
        "hub_ok": hub_ok,
        "hub_snapshot": hub_snapshot,
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
        "recall_probe": recall_probe,
        "recall_quality": recall_quality_block,
        "not_configured": not_configured,
        "ok": (
            hub_ok
            and drift_ok
            and graph.get("ok", False)
            and outbox_ok
            and backup["ok"]
            and bool(liveness.get("ok"))
            and bool(git_sync.get("ok"))
            and dsn_file_ok
            and index_freshness_ok
            and embed_coverage_ok
            and bool(_graph_backup.get("ok"))
            # graph_offsite_ok was reported but never aggregated, so a stale
            # offsite copy left doctor (and the soak probe reading doctor.ok)
            # green for days (08-28..08-31).
            and bool(_graph_offsite.get("ok"))
            # hub_snapshot["ok"] was computed and displayed but never folded
            # into the aggregate — a snapshot stuck behind ingest by hours
            # still read doctor.ok == true (W6, 2026-09-03).
            and bool(hub_snapshot.get("ok", True))
            # recall_probe_ok: the end-to-end "capture then search finds it"
            # signal (W6.1) — red when no probe has run, the last one failed,
            # or it is older than 7 days. This is a hard gate, not a warning.
            and bool(recall_probe.get("ok"))
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
        "recall_probe_ok": bool(recall_probe.get("ok")),
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


def _id_shaped(term: str) -> bool:
    """A query that looks like a graph node id (``kind:slug`` or ``a__b``).

    W2.2: nodes are excluded from default (literal and hybrid) results — 73%
    of path nodes were junk (2026-09-03 evidence) and let a near-zero node
    starve real hits. An id-shaped query is exactly how a caller reaches a
    node on purpose, so it still can without ``kind: "node"``.
    """
    t = term or ""
    return ":" in t or "__" in t


def _search_query(
    cur,
    term: str,
    limit: int,
    *,
    kind: str | None = None,
    include_nodes: bool | None = None,
) -> list[dict]:
    """Deterministically-ordered, per-kind-fair ILIKE search (F7).

    Each active kind gets its own fair share of `limit` and its own ORDER BY,
    so one kind can no longer starve the others under a shared LIMIT, and
    results are stable across runs instead of arbitrary scan order.

    Multi-token queries rank by token coverage (OR + hit count), not one
    giant substring. A query that yields no tokens still uses the whole
    escaped term as before.

    ``kind`` restricts to one of 'topic'/'episode'/'node' (None = all
    eligible kinds). Nodes are excluded from the eligible set by default
    (W2.2) unless ``kind == "node"``, ``include_nodes`` is explicitly True,
    or (when ``include_nodes`` is left at its default None) the query looks
    id-shaped (``_id_shaped``). Pass ``include_nodes=False`` to force nodes
    out even for an id-shaped query.
    """
    if kind is not None and kind not in ("topic", "episode", "node"):
        raise ValueError("kind must be 'topic', 'episode', or 'node'")
    if kind == "node":
        want_nodes = True
    elif include_nodes is None:
        want_nodes = _id_shaped(term)
    else:
        want_nodes = bool(include_nodes)
    active_kinds = (
        [kind]
        if kind
        else (["topic", "episode", "node"] if want_nodes else ["topic", "episode"])
    )
    shares = dict(zip(active_kinds, _fair_shares(limit, len(active_kinds))))
    topic_n = shares.get("topic", 0)
    episode_n = shares.get("episode", 0)
    node_n = shares.get("node", 0)
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


def _episode_rank_text(summary, topics, decisions, preferences, people) -> str:
    """Full (unclipped) episode text for ranking — delegates to embed.episode_text
    (no byte copy) so the two never drift on what "the episode's text" means."""
    from khipu.embed import episode_text

    return episode_text({
        "summary": summary, "topics": topics, "decisions": decisions,
        "preferences": preferences, "people": people,
    })


def _neg_ts_sort_key(ts) -> float:
    if ts is None:
        return 0.0
    try:
        return -ts.timestamp()
    except (AttributeError, OverflowError, OSError, ValueError):
        return 0.0


def _literal_candidates(
    cur, term: str, limit: int, *, kind: str | None = None
) -> list[dict]:
    """Globally-ranked literal ILIKE candidates for RRF fusion (bugfix,
    reported live: "Recorded mobile followup task" — episode 11286 named the
    query verbatim in its decisions but landed at literal rank 21 because
    ``_search_query``'s per-kind-fair split concatenates topic rows before
    episode rows, so *every* topic outranked *every* episode regardless of
    actual hit strength).

    ``_search_query`` stays fair-share-partitioned (F7) — that is correct for
    a stand-alone ILIKE listing and is what ``_pushed_memory_slice`` and
    ``mode=literal``'s per-kind test coverage still exercise. This function
    is the input to fusion instead: one flat pool (ranked by phrase-boost,
    then hit count desc, then ts desc, across every eligible kind together),
    because fairness at the *output* is ``_fair_fill``'s job, and pre-sorting
    the input into kind blocks defeats RRF's ranking entirely.

    Each row carries ``rank_text`` (unclipped: summary + topics/decisions/
    preferences/people for episodes, title + body for topics, id + name +
    payload for nodes) so the caller can also rank token overlap over the
    same text without a second query.
    """
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    if kind is not None and kind not in ("topic", "episode", "node"):
        raise ValueError("kind must be 'topic', 'episode', or 'node'")
    want_nodes = kind == "node" or (kind is None and _id_shaped(term))
    active_kinds = (
        [kind] if kind else (["topic", "episode", "node"] if want_nodes else ["topic", "episode"])
    )
    lim = max(1, int(limit))
    tokens = search_tokens(term)
    pool: list[dict] = []

    if not tokens:
        toks = [(term or "").strip()] if (term or "").strip() else []
        if not toks:
            return []
        params: dict = {"q": f"%{_escape_like(toks[0])}%", "lim": lim}
        if "topic" in active_kinds:
            cur.execute(
                """
                SELECT slug, COALESCE(title, slug) AS label, body,
                       COALESCE(updated_at, created_at) AS ts
                FROM topics
                WHERE deleted_at IS NULL AND (
                    body ILIKE %(q)s ESCAPE '\\' OR slug ILIKE %(q)s ESCAPE '\\'
                    OR COALESCE(title, '') ILIKE %(q)s ESCAPE '\\')
                ORDER BY slug ASC
                LIMIT %(lim)s
                """,
                params,
            )
            for slug, label, body, ts in cur.fetchall():
                pool.append({
                    "kind": "topic", "id": slug, "label": label, "snippet": body or "",
                    "rank_text": f"{label}\n\n{body or ''}", "hits": 1, "ts": ts,
                })
        if "episode" in active_kinds:
            # A backslash can't appear inside an f-string's {...} on Python
            # 3.11 (PEP 701 lifted this in 3.12 only) — build the ESCAPE '\'
            # clause as its own string first, same as the literal-mode
            # episode_where a few lines up, instead of nesting the join()
            # generator directly inside the outer f"""...""" query below.
            episode_ilike_where = " OR ".join(
                f"{c} ILIKE %(q)s ESCAPE '\\'" for c in _EPISODE_ILIKE_COLUMNS
            )
            cur.execute(
                f"""
                SELECT id::text, summary, topics, decisions, preferences, people, ts
                FROM episodes
                WHERE {episode_ilike_where}
                ORDER BY ts DESC NULLS LAST, id DESC
                LIMIT %(lim)s
                """,
                params,
            )
            for eid, summary, topics, decisions, preferences, people, ts in cur.fetchall():
                pool.append({
                    "kind": "episode", "id": eid, "label": summary, "snippet": summary,
                    "rank_text": _episode_rank_text(summary, topics, decisions, preferences, people),
                    "hits": 1, "ts": ts,
                })
        if "node" in active_kinds:
            cur.execute(
                """
                SELECT id, COALESCE(name, id) AS label, payload::text AS payload
                FROM nodes
                WHERE id ILIKE %(q)s ESCAPE '\\' OR COALESCE(name, '') ILIKE %(q)s ESCAPE '\\'
                ORDER BY id ASC
                LIMIT %(lim)s
                """,
                params,
            )
            for nid, label, payload in cur.fetchall():
                pool.append({
                    "kind": "node", "id": nid, "label": label,
                    "snippet": (payload or "")[:4000],
                    "rank_text": f"{label}\n{payload or ''}", "hits": 1, "ts": None,
                })
    else:
        params = _ilike_token_params(tokens)
        n = len(tokens)
        if "topic" in active_kinds:
            topic_where, topic_score = _token_match_sql(("body", "slug", "COALESCE(title, '')"), n)
            cur.execute(
                f"""
                SELECT slug, COALESCE(title, slug) AS label, body,
                       COALESCE(updated_at, created_at) AS ts, ({topic_score}) AS hits
                FROM topics
                WHERE deleted_at IS NULL AND ({topic_where})
                ORDER BY hits DESC, slug ASC
                LIMIT %(lim)s
                """,
                {**params, "lim": lim},
            )
            for slug, label, body, ts, hits in cur.fetchall():
                pool.append({
                    "kind": "topic", "id": slug, "label": label, "snippet": body or "",
                    "rank_text": f"{label}\n\n{body or ''}", "hits": int(hits), "ts": ts,
                })
        if "episode" in active_kinds:
            episode_where, episode_score = _token_match_sql(_EPISODE_ILIKE_COLUMNS, n)
            cur.execute(
                f"""
                SELECT id::text, summary, topics, decisions, preferences, people, ts,
                       ({episode_score}) AS hits
                FROM episodes
                WHERE {episode_where}
                ORDER BY hits DESC, ts DESC NULLS LAST, id DESC
                LIMIT %(lim)s
                """,
                {**params, "lim": lim},
            )
            for eid, summary, topics, decisions, preferences, people, ts, hits in cur.fetchall():
                pool.append({
                    "kind": "episode", "id": eid, "label": summary, "snippet": summary,
                    "rank_text": _episode_rank_text(summary, topics, decisions, preferences, people),
                    "hits": int(hits), "ts": ts,
                })
        if "node" in active_kinds:
            node_where, node_score = _token_match_sql(("id", "COALESCE(name, '')"), n)
            cur.execute(
                f"""
                SELECT id, COALESCE(name, id) AS label, payload::text AS payload, ({node_score}) AS hits
                FROM nodes
                WHERE {node_where}
                ORDER BY hits DESC, id ASC
                LIMIT %(lim)s
                """,
                {**params, "lim": lim},
            )
            for nid, label, payload, hits in cur.fetchall():
                pool.append({
                    "kind": "node", "id": nid, "label": label,
                    "snippet": (payload or "")[:4000],
                    "rank_text": f"{label}\n{payload or ''}", "hits": int(hits), "ts": None,
                })

    qlower = (term or "").strip().lower()

    def _phrase_hit(r: dict) -> int:
        return 1 if qlower and qlower in (r.get("rank_text") or "").lower() else 0

    pool.sort(key=lambda r: (-_phrase_hit(r), -r.get("hits", 0), _neg_ts_sort_key(r.get("ts"))))
    out = []
    for r in pool[:lim]:
        out.append({
            "kind": r["kind"], "id": r["id"],
            "label": clip_snippet(r["label"] or "", LABEL_LIMIT),
            "snippet": clip_snippet(r["snippet"] or "", SNIPPET_LIMIT),
            "rank_text": r["rank_text"],
        })
    return out


def _resolve_search_mode(args: argparse.Namespace) -> str:
    """``--mode`` wins; the legacy ``--semantic`` flag is still an alias for
    ``mode=semantic`` (W2.1) so existing scripts/muscle-memory keep working."""
    mode = getattr(args, "mode", None)
    if mode:
        return mode
    if getattr(args, "semantic", False):
        return "semantic"
    return "hybrid"


def cmd_search(args: argparse.Namespace) -> int:
    from khipu import query_log
    from khipu.hub_snapshot import hub_connection_failed, search_stale_payload

    mode = _resolve_search_mode(args)
    kind = getattr(args, "kind", None)
    project = getattr(args, "project", None)
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    session_id = getattr(args, "session_id", None)
    harness = getattr(args, "harness", None)
    try:
        from khipu.embed import hybrid_search

        payload = hybrid_search(
            args.query, limit=args.limit, mode=mode, kind=kind, project=project,
            since=since, until=until, session_id=session_id, harness=harness,
        )
    except ValueError as err:
        print(json.dumps({"ok": False, "error": str(err)}))
        return 2
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        try:
            payload = search_stale_payload(
                args.query, args.limit, semantic=(mode == "semantic"), kind=kind,
                since=since, until=until, project=project, session_id=session_id,
                harness=harness,
            )
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
    query_log.log_query(
        args.query, mode=mode,
        filters={"kind": kind, "project": project, "since": since, "until": until,
                 "session_id": session_id, "harness": harness},
        result_count=len(payload.get("results") or []), top=payload.get("results") or [],
    )
    print(json.dumps(payload, indent=2))
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
        "walk": (
            synthetic_walk
            + [
                {"node_id": a, "via": b, "type": t, "hops": h}
                for a, b, t, h in walk_rows
            ]
        )[:limit],
    }
    if episode_meta is not None:
        out["episode"] = episode_meta
    return out


def cmd_graph(args: argparse.Namespace) -> int:
    from khipu.hub_snapshot import (
        graph_neighbors_snapshot,
        hub_connection_failed,
        stale_fields,
        try_hub_connect,
    )

    try:
        with try_hub_connect() as conn:
            with conn.cursor() as cur:
                out = _graph_query(cur, args.id, args.hops, args.limit)
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        out = graph_neighbors_snapshot(args.id, args.hops, args.limit)
        out.update(stale_fields())
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


# ---------------------------------------------------------------------------
# Memory reliability (2026-09-03): owed / episode forget / topic purge /
# backfill identity / hygiene paths. New subcommands only — the search/
# status/doctor internals above are owned by a concurrent change.
# ---------------------------------------------------------------------------

def cmd_owed(args: argparse.Namespace) -> int:
    """W3.4: `khipu owed` — list, close, or reopen commitments."""
    from khipu.db import connect
    from khipu import commitments

    close_id = getattr(args, "close", None)
    reopen_id = getattr(args, "reopen", None)
    if close_id or reopen_id:
        cid = int(close_id or reopen_id)
        new_status = "closed" if close_id else "open"
        with connect() as conn:
            with conn.cursor() as cur:
                ok = commitments.set_status(cur, cid, new_status)
            conn.commit()
        print(json.dumps({"ok": ok, "id": cid, "status": new_status}))
        return 0 if ok else 1

    with connect() as conn:
        with conn.cursor() as cur:
            rows = commitments.list_owed(
                cur,
                project=getattr(args, "project", None),
                status=getattr(args, "status", None) or "open",
                limit=int(getattr(args, "limit", None) or 50),
            )
    print(json.dumps(rows, indent=2, default=str))
    return 0


def cmd_episode(args: argparse.Namespace) -> int:
    """W5.6: `khipu episode forget ID` — soft-delete + remove its vectors."""
    if getattr(args, "episode_cmd", None) != "forget":
        print(json.dumps({"ok": False, "error": "usage: khipu episode forget ID"}))
        return 2
    from khipu.db import connect

    eid = int(args.id)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE episodes SET deleted_at = now() WHERE id = %s AND deleted_at IS NULL",
                (eid,),
            )
            soft_deleted = cur.rowcount > 0
            cur.execute(
                "DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = %s",
                (str(eid),),
            )
            embeddings_removed = cur.rowcount
        conn.commit()
    print(json.dumps({
        "ok": True, "id": eid, "soft_deleted": soft_deleted,
        "embeddings_removed": embeddings_removed,
    }))
    return 0


def cmd_topic(args: argparse.Namespace) -> int:
    """W5.6: `khipu topic purge SLUG --yes` — hard-delete a TOMBSTONED topic
    (deleted_at already set) and its revisions/embeddings. The command a
    migration comment (0003_reconcile_upsert.sql) promised but nobody wrote."""
    if getattr(args, "topic_cmd", None) != "purge":
        print(json.dumps({"ok": False, "error": "usage: khipu topic purge SLUG --yes"}))
        return 2
    if not getattr(args, "yes", False):
        print(json.dumps({"ok": False, "error": "refusing: pass --yes to confirm a hard delete"}))
        return 2
    from khipu.db import connect

    slug = args.slug
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT deleted_at FROM topics WHERE slug = %s", (slug,))
            row = cur.fetchone()
            if row is None:
                print(json.dumps({"ok": False, "error": f"no such topic: {slug}"}))
                return 1
            if row[0] is None:
                print(json.dumps({
                    "ok": False,
                    "error": "topic is not tombstoned (deleted_at IS NULL) — purge only "
                             "removes a topic reconcile already tombstoned; delete its file "
                             "first and let reconcile tombstone it",
                }))
                return 1
            cur.execute("DELETE FROM memory_embeddings WHERE kind = 'topic' AND ref = %s", (slug,))
            embeddings_removed = cur.rowcount
            cur.execute("DELETE FROM topic_revisions WHERE slug = %s", (slug,))
            revisions_removed = cur.rowcount
            cur.execute("DELETE FROM topics WHERE slug = %s", (slug,))
        conn.commit()
    print(json.dumps({
        "ok": True, "slug": slug, "revisions_removed": revisions_removed,
        "embeddings_removed": embeddings_removed,
    }))
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """W1.5: `khipu backfill identity [--dry-run|--apply]`."""
    if getattr(args, "backfill_cmd", None) != "identity":
        print(json.dumps({"ok": False, "error": "usage: khipu backfill identity [--dry-run|--apply]"}))
        return 2
    from khipu.db import connect
    from khipu import hygiene

    apply = bool(getattr(args, "apply", False))
    with connect() as conn:
        with conn.cursor() as cur:
            if apply:
                report = hygiene.apply_backfill_identity(cur, limit=getattr(args, "limit", None))
            else:
                report = hygiene.backfill_identity_report(cur, sample_limit=getattr(args, "limit", None) or 20)
        if apply:
            conn.commit()
    report["dry_run"] = not apply
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_hygiene(args: argparse.Namespace) -> int:
    """W5.2: `khipu hygiene paths [--dry-run|--apply]`."""
    if getattr(args, "hygiene_cmd", None) != "paths":
        print(json.dumps({"ok": False, "error": "usage: khipu hygiene paths [--dry-run|--apply]"}))
        return 2
    from khipu.db import connect
    from khipu import hygiene

    apply = bool(getattr(args, "apply", False))
    with connect() as conn:
        with conn.cursor() as cur:
            if apply:
                report = hygiene.apply_purge_junk_paths(cur)
            else:
                report = hygiene.report_junk_paths(cur)
        if apply:
            conn.commit()
    report["dry_run"] = not apply
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    """W4.3: `khipu notes reconcile` — mirror harness-native per-project
    notes (~/.claude/projects/<slug>/memory/*.md, ~/.codex/memories/*.md)
    into topics, append-only, so search/graph/the W4 pushed slice can reach
    them. Never runs against the live hub except through the real
    khipu.db.connect() this shares with every other write path."""
    if getattr(args, "notes_cmd", None) != "reconcile":
        print(json.dumps({"ok": False, "error": "usage: khipu notes reconcile"}))
        return 2
    from khipu import notes

    report = notes.reconcile(dry_run=bool(getattr(args, "dry_run", False)))
    print(json.dumps(report, indent=2, default=str))
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


def cmd_join(args: argparse.Namespace) -> int:
    from khipu.join import (
        export_kit,
        import_kit,
        resolve_passphrase,
        verify_live_counts,
        write_kit_file,
    )

    try:
        passphrase = resolve_passphrase(getattr(args, "passphrase", None))
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    if args.join_cmd == "export":
        try:
            blob = export_kit(passphrase)
            out_path = write_kit_file(Path(args.out), blob)
            from khipu.join import decrypt_payload

            payload = decrypt_payload(blob, passphrase)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "export",
                    "out": str(out_path),
                    "expected": payload.get("expected"),
                    "created_at": payload.get("created_at"),
                },
                indent=2,
            )
        )
        return 0

    if args.join_cmd == "import":
        try:
            blob = Path(args.file).expanduser().read_bytes()
            summary = import_kit(blob, passphrase)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        migrate_out: dict | None = None
        counts = verify_live_counts(summary.get("expected") or {})
        if not getattr(args, "skip_migrate_check", False) and not counts.get("error"):
            from khipu.migrate import run

            migrate_out = run(dry_run=False)
        hub_ok = bool(counts.get("ok"))
        result = {
            "ok": True,
            "kit_imported": True,
            "hub_ok": hub_ok,
            "action": "import",
            "summary": summary,
            "counts": counts,
        }
        if counts.get("error"):
            result["warning"] = counts["error"]
        elif not hub_ok and counts.get("mismatches"):
            result["warning"] = "; ".join(counts["mismatches"])
        if migrate_out is not None:
            result["migrate"] = migrate_out
        print(json.dumps(result, indent=2))
        return 0

    if args.join_cmd == "advertise":
        try:
            from khipu.join_pair import advertise_join_kit

            out = advertise_join_kit(
                passphrase,
                timeout=int(getattr(args, "timeout", 600) or 600),
                pin=getattr(args, "pin", None),
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1

    if args.join_cmd == "receive":
        try:
            from khipu.join_pair import receive_join_kit

            pin = str(getattr(args, "pin", "") or "").strip()
            out_arg = getattr(args, "out", None)
            out_path = Path(out_arg).expanduser() if out_arg else None
            result = receive_join_kit(passphrase, pin, out_path=out_path)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(json.dumps(result, indent=2))
        return 0 if result.get("kit_imported") or result.get("ok") else 2

    print(
        json.dumps({"ok": False, "error": f"unknown join subcommand: {args.join_cmd}"})
    )
    return 2


def cmd_outbox(args: argparse.Namespace) -> int:
    """Offline outbox: captures whose PG write failed, replayed when PG is back."""
    from khipu import outbox

    if args.outbox_cmd == "status":
        print(json.dumps(outbox.status(), indent=2))
        return 0
    out = outbox.drain(limit=args.limit)
    out["remaining"] = outbox.status()["pending"]
    print(json.dumps(out, indent=2))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from khipu.hub_snapshot import refresh, snapshot_health

    if args.snapshot_cmd == "status":
        print(json.dumps(snapshot_health(), indent=2))
        return 0
    try:
        out = refresh()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 2


def cmd_recall(args: argparse.Namespace) -> int:
    """W2.5: the search query log — recent searches, and queries that came
    back empty (the seed for a zero-result / golden-query review). Also W6.3
    `khipu recall eval` — score the maintainer-local recall-golden.jsonl against default
    search and print hit@k per line plus overall."""
    if args.recall_cmd == "log":
        from khipu import query_log

        print(json.dumps(query_log.tail(args.tail), indent=2, default=str))
        return 0
    if args.recall_cmd == "zero-results":
        from khipu import query_log

        print(json.dumps(query_log.zero_results(args.days), indent=2, default=str))
        return 0
    if args.recall_cmd == "eval":
        from pathlib import Path as _Path

        from khipu import recall_eval

        path = _Path(args.golden) if getattr(args, "golden", None) else None
        try:
            report = recall_eval.run_eval(path)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
            return 2
        for row in report["rows"]:
            mark = "hit " if row["hit"] else "MISS"
            print(f"[{mark}] {row['query']!r} -> got={row['got']} expect={row['expect']}",
                  file=sys.stderr)
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["overall_hit_rate"] >= 0.8 else 1
    return 2


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
                    print(
                        json.dumps(
                            {"ok": False, "error": "JSON payload must be an object"}
                        )
                    )
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
                print(
                    json.dumps(
                        {"ok": False, "error": "--provider is required with --role"}
                    )
                )
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
        if action == "welcome":
            raw_json = getattr(args, "models_json", None)
            if not raw_json:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "models welcome requires a JSON blob",
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
                print(
                    json.dumps(
                        {"ok": False, "error": "JSON payload must be an object"}
                    )
                )
                return 2
            out = models_mod.apply_welcome_models(
                synth_choice=str(payload.get("synth_choice") or "skip"),
                embed_choice=str(payload.get("embed_choice") or "skip"),
                synth_endpoint=str(payload.get("synth_endpoint") or ""),
                synth_model_id=str(payload.get("synth_model_id") or ""),
                embed_endpoint=str(payload.get("embed_endpoint") or ""),
                embed_model_id=str(payload.get("embed_model_id") or ""),
            )
            embed_part = out.get("embed") or {}
            if embed_part.get("ok") is False:
                err = embed_part.get("error") or "embed activate failed"
                print(json.dumps({**out, "ok": False, "error": err}))
                return 2
            print(json.dumps(out, indent=2))
            return 0
        print(json.dumps({"ok": False, "error": f"unknown models action: {action}"}))
        return 2
    except (ValueError, RuntimeError) as e:
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
            print(
                json.dumps(
                    {"ok": True, "path": str(sources.resolved_path()), "resolved": out}
                )
            )
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


def _print_component_result(payload: dict) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload.get("ok") else 1


def cmd_components(args: argparse.Namespace) -> int:
    action = getattr(args, "components_cmd", None)
    if action == "select-compat-row":
        from khipu.components_matrix import select_compat_row

        return _print_component_result(
            select_compat_row(
                args.mode,
                pgvector_extversion=getattr(args, "pgvector_extversion", None),
                server_version=getattr(args, "server_version", None),
                pgvector=getattr(args, "pgvector", None),
                refresh=not getattr(args, "no_refresh", False),
            )
        )
    if action == "install-local-postgres":
        from khipu.components_postgres import install_local_postgres

        return _print_component_result(install_local_postgres())
    if action == "bootstrap-local-backup":
        from khipu.components_backup import bootstrap_local_backup

        return _print_component_result(bootstrap_local_backup())
    if action == "install-graphify":
        from khipu.components_graphify import install_graphify

        return _print_component_result(install_graphify(first_run=True))
    if action == "status-json":
        from khipu.components_postgres import components_status

        return _print_component_result(components_status())
    if action == "upgrade-postgres":
        from khipu.components_postgres import upgrade_postgres

        return _print_component_result(upgrade_postgres())
    if action == "upgrade-graphify":
        from khipu.components_graphify import upgrade_graphify

        return _print_component_result(upgrade_graphify())
    if action == "check-remote":
        from khipu.components_postgres import check_remote_postgres

        return _print_component_result(
            check_remote_postgres(full=bool(getattr(args, "full", False)))
        )
    if action == "install":
        target = getattr(args, "install_target", None)
        if target == "postgres":
            from khipu.components_postgres import install_local_postgres

            return _print_component_result(install_local_postgres())
        if target == "graphify":
            from khipu.components_graphify import install_graphify

            return _print_component_result(install_graphify(first_run=False))
        print(json.dumps({"ok": False, "error": f"unknown install target: {target}"}))
        return 1
    if action == "status":
        from khipu.components_postgres import components_status

        return _print_component_result(components_status())
    if action == "upgrade":
        target = getattr(args, "upgrade_target", None)
        if target == "postgres":
            from khipu.components_postgres import upgrade_postgres

            return _print_component_result(upgrade_postgres())
        if target == "graphify":
            from khipu.components_graphify import upgrade_graphify

            return _print_component_result(upgrade_graphify())
        print(json.dumps({"ok": False, "error": f"unknown upgrade target: {target}"}))
        return 1
    print(json.dumps({"ok": False, "error": "missing components subcommand"}))
    return 1


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
    d.add_argument(
        "--probe",
        action="store_true",
        help="Run a fresh end-to-end recall probe (writes a nonce episode, "
        "soft-deletes it after) before reporting; the only way `doctor` writes anything",
    )
    d.add_argument(
        "--harness",
        default=None,
        help="Harness label for --probe (default: $KHIPU_HARNESS or 'doctor')",
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
        "search",
        help="Hybrid search (cosine + token overlap + literal), or --mode literal/semantic",
    )
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=20)
    se.add_argument(
        "--mode",
        choices=("hybrid", "literal", "semantic"),
        help="Default hybrid. literal = old ILIKE-only. semantic = cosine + token overlap only.",
    )
    se.add_argument(
        "--semantic",
        action="store_true",
        help="Deprecated alias for --mode semantic",
    )
    se.add_argument(
        "--kind",
        choices=("episode", "topic", "node", "media"),
        help="Restrict to one kind (media is semantic-only; node is literal/hybrid-only)",
    )
    se.add_argument("--project", help="Match COALESCE(episodes.project, episodes.scope)")
    se.add_argument("--since", help="ISO date/datetime or relative, e.g. 7d / 24h")
    se.add_argument("--until", help="ISO date/datetime or relative, e.g. 7d / 24h")
    se.add_argument("--session-id", dest="session_id", help="Episode session_id prefix match")
    se.add_argument("--harness", help="Prefix of session_id before the colon")
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
    md_welcome = md_sub.add_parser(
        "welcome",
        help="First-run synth/embed choices; activates cloud embed in-process",
    )
    md_welcome.add_argument(
        "models_json",
        help="JSON: synth_choice, embed_choice, optional local endpoint/model_id fields",
    )
    md_welcome.set_defaults(func=cmd_models, models_cmd="welcome")
    md.set_defaults(func=cmd_models, models_cmd="show")

    src = sub.add_parser(
        "sources", help="Graph membership: list / enable / disable / add / export"
    )
    src_sub = src.add_subparsers(dest="sources_cmd", required=True)
    src_sub.add_parser(
        "list", help="JSON: sources + resolve_for_graphify"
    ).set_defaults(func=cmd_sources)
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

    owed = sub.add_parser("owed", help="List / close / reopen commitments (memory reliability W3)")
    owed.add_argument("--project", default=None)
    owed.add_argument("--status", default="open", choices=("open", "closed", "stale"))
    owed.add_argument("--limit", type=int, default=50)
    owed.add_argument("--close", metavar="ID", default=None, help="Close commitment ID")
    owed.add_argument("--reopen", metavar="ID", default=None, help="Reopen commitment ID")
    owed.set_defaults(func=cmd_owed)

    ep = sub.add_parser("episode", help="Episode maintenance (memory reliability W5.6)")
    ep_sub = ep.add_subparsers(dest="episode_cmd", required=True)
    ep_forget = ep_sub.add_parser("forget", help="Soft-delete an episode and remove its vectors")
    ep_forget.add_argument("id", type=int)
    ep.set_defaults(func=cmd_episode)

    tp = sub.add_parser("topic", help="Topic maintenance (memory reliability W5.6)")
    tp_sub = tp.add_subparsers(dest="topic_cmd", required=True)
    tp_purge = tp_sub.add_parser(
        "purge", help="Hard-delete a TOMBSTONED topic + its revisions/embeddings"
    )
    tp_purge.add_argument("slug")
    tp_purge.add_argument("--yes", action="store_true", help="Required to confirm the hard delete")
    tp.set_defaults(func=cmd_topic)

    bf = sub.add_parser("backfill", help="Backfill jobs (memory reliability W1.5)")
    bf_sub = bf.add_subparsers(dest="backfill_cmd", required=True)
    bf_identity = bf_sub.add_parser(
        "identity", help="Derive repo_root/project for episodes with an absolute-path scope"
    )
    bf_identity.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    bf_identity.add_argument(
        "--apply", dest="apply", action="store_true",
        help="Actually write the backfill (destructive; needs an explicit go — never run against the live shared hub without it)",
    )
    bf_identity.add_argument("--limit", type=int, default=None)
    bf.set_defaults(func=cmd_backfill)

    hy = sub.add_parser("hygiene", help="Graph hygiene jobs (memory reliability W5.2)")
    hy_sub = hy.add_subparsers(dest="hygiene_cmd", required=True)
    hy_paths = hy_sub.add_parser(
        "paths", help="Report (or purge) path: graph nodes that fail the real-path shape rule"
    )
    hy_paths.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    hy_paths.add_argument(
        "--apply", dest="apply", action="store_true",
        help="Actually delete the failing path: nodes (destructive; needs an explicit go)",
    )
    hy.set_defaults(func=cmd_hygiene)

    nt = sub.add_parser("notes", help="Index harness-native per-project notes as topics (memory reliability W4)")
    nt_sub = nt.add_subparsers(dest="notes_cmd", required=True)
    nt_sub.add_parser(
        "reconcile",
        help="Mirror ~/.claude/projects/<slug>/memory/*.md and ~/.codex/memories/*.md into topics",
    ).add_argument("--dry-run", action="store_true", help="Report without writing")
    nt.set_defaults(func=cmd_notes)

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

    join = sub.add_parser(
        "join",
        help="Export/import hub join kits (optional passphrase; never prints secrets)",
    )
    join_sub = join.add_subparsers(dest="join_cmd", required=True)
    join_export = join_sub.add_parser(
        "export", help="Write a join kit from this Mac's Keychain/config"
    )
    join_export.add_argument(
        "--passphrase",
        default=None,
        help="Optional encryption passphrase (omit for plaintext; or KHIPU_JOIN_PASSPHRASE)",
    )
    join_export.add_argument(
        "--out",
        required=True,
        help="Destination .khipujoin file path",
    )
    join_export.set_defaults(func=cmd_join)
    join_import = join_sub.add_parser(
        "import", help="Apply a join kit's hub credentials on this Mac"
    )
    join_import.add_argument(
        "--passphrase",
        default=None,
        help="Passphrase if the kit was saved encrypted (or KHIPU_JOIN_PASSPHRASE)",
    )
    join_import.add_argument(
        "--file",
        required=True,
        help="Path to a .khipujoin file",
    )
    join_import.add_argument(
        "--skip-migrate-check",
        action="store_true",
        help="Skip applying pending migrations after import",
    )
    join_import.set_defaults(func=cmd_join)
    join_advertise = join_sub.add_parser(
        "advertise",
        help="Advertise join kit on LAN via Bonjour + TLS (macOS dns-sd)",
    )
    join_advertise.add_argument(
        "--passphrase",
        default=None,
        help="Optional encryption passphrase (omit for plaintext; or KHIPU_JOIN_PASSPHRASE)",
    )
    join_advertise.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Seconds to advertise (default 600)",
    )
    join_advertise.add_argument(
        "--pin",
        default=None,
        help="Optional fixed 6-digit PIN (default: random)",
    )
    join_advertise.set_defaults(func=cmd_join)
    join_receive = join_sub.add_parser(
        "receive",
        help="Find nearby Mac, send PIN, import join kit",
    )
    join_receive.add_argument(
        "--passphrase",
        default=None,
        help="Passphrase if the kit was saved encrypted (or KHIPU_JOIN_PASSPHRASE)",
    )
    join_receive.add_argument(
        "--pin",
        required=True,
        help="Six-digit PIN shown on the exporting Mac",
    )
    join_receive.add_argument(
        "--out",
        default=None,
        help="Optional path to save the .khipujoin file",
    )
    join_receive.set_defaults(func=cmd_join)

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

    snap = sub.add_parser(
        "snapshot",
        help="Hub read replica: refresh hub_snapshot.sqlite / status",
    )
    snap_sub = snap.add_subparsers(dest="snapshot_cmd", required=True)
    snap_sub.add_parser(
        "refresh", help="Dump hub tables into hub_snapshot.sqlite (hub must be up)"
    )
    snap_sub.add_parser("status", help="Snapshot age, size, and row counts from meta")
    snap.set_defaults(func=cmd_snapshot)

    rc = sub.add_parser("recall", help="Search query log: recent / zero-result queries")
    rc_sub = rc.add_subparsers(dest="recall_cmd", required=True)
    rlog = rc_sub.add_parser("log", help="Print recent query_log.jsonl entries")
    rlog.add_argument("--tail", type=int, default=20)
    rzero = rc_sub.add_parser(
        "zero-results", help="Queries with result_count 0 in the last N days"
    )
    rzero.add_argument("--days", type=int, default=7)
    reval = rc_sub.add_parser(
        "eval", help="Score <config dir>/recall-golden.jsonl (or --golden / KHIPU_RECALL_GOLDEN) against default search (W6.3)"
    )
    reval.add_argument(
        "--golden", default=None,
        help="Path to the golden JSONL file (default: <config dir>/recall-golden.jsonl, or KHIPU_RECALL_GOLDEN)",
    )
    rc.set_defaults(func=cmd_recall)

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

    comp = sub.add_parser(
        "components",
        help="Postgres / Graphify component install, status, and upgrades",
    )
    comp_sub = comp.add_subparsers(dest="components_cmd", required=True)

    scr = comp_sub.add_parser(
        "select-compat-row",
        help="Refresh matrix and persist versions.json pending row",
    )
    scr.add_argument(
        "--mode",
        required=True,
        choices=("local_docker", "remote"),
    )
    scr.add_argument("--pgvector-extversion", default=None)
    scr.add_argument("--server-version", default=None)
    scr.add_argument("--pgvector", default=None)
    scr.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use bundled ∪ cache only; do not fetch khipu-compat",
    )
    scr.set_defaults(func=cmd_components, components_cmd="select-compat-row")

    comp_sub.add_parser(
        "install-local-postgres",
        help="Pull/run local PG19 container through migrate (Welcome Radio A)",
    ).set_defaults(func=cmd_components, components_cmd="install-local-postgres")

    comp_sub.add_parser(
        "bootstrap-local-backup",
        help="pg_dump + restore_drill into ops_events (Welcome Radio A)",
    ).set_defaults(func=cmd_components, components_cmd="bootstrap-local-backup")

    comp_sub.add_parser(
        "install-graphify",
        help="Install pending graphify tarball (Task 5)",
    ).set_defaults(func=cmd_components, components_cmd="install-graphify")

    comp_sub.add_parser(
        "status-json",
        help="JSON component versions for the desktop shell",
    ).set_defaults(func=cmd_components, components_cmd="status-json")

    comp_sub.add_parser(
        "upgrade-postgres",
        help="Dump/restore Postgres upgrade runbook",
    ).set_defaults(func=cmd_components, components_cmd="upgrade-postgres")

    comp_sub.add_parser(
        "upgrade-graphify",
        help="Upgrade installed graphify semver",
    ).set_defaults(func=cmd_components, components_cmd="upgrade-graphify")

    cr = comp_sub.add_parser(
        "check-remote",
        help="Probe remote DSN for PG19 (+ optional vector/GRAPH_TABLE)",
    )
    cr.add_argument(
        "--full",
        action="store_true",
        help="After migrate: require vector extension and GRAPH_TABLE",
    )
    cr.set_defaults(func=cmd_components, components_cmd="check-remote")

    cst = comp_sub.add_parser("status", help="Show installed component versions")
    cst.set_defaults(func=cmd_components, components_cmd="status")

    cins = comp_sub.add_parser("install", help="Install a named component")
    cins.add_argument("install_target", choices=("postgres", "graphify"))
    cins.set_defaults(func=cmd_components, components_cmd="install")

    cup = comp_sub.add_parser("upgrade", help="Upgrade a named component")
    cup.add_argument("upgrade_target", choices=("postgres", "graphify"))
    cup.set_defaults(func=cmd_components, components_cmd="upgrade")

    comp.set_defaults(func=cmd_components)

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
