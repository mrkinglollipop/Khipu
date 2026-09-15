"""khipu-mcp — stdio MCP server (server id: ``khipu``).

Reads (search, get, graph, status) are allowed in every ``capture_mode``.
``khipu_capture`` is a writer: the HTTPS gateway and no-hook hub installs may
write; the local stdio server declines when ``khipu-stop-hook`` /
``khipu-aegis-capture`` is the writer (double-capture). ``legacy``/``dual``
always reject. The ``khipu capture`` CLI is a separate entrypoint and is not
gated here.

Tools:

  - ``khipu_search``  — token-coverage ILIKE or hybrid semantic search
  - ``khipu_get``     — full episode / topic / media row by search-hit id
  - ``khipu_graph``   — undirected neighborhood; digit ids are episodes
  - ``khipu_status``  — PG counts + mirror lag (optional drift sample)
  - ``khipu_capture`` — hub writer on the HTTPS gateway / no-hook installs;
    local stdio + capture hook declines with an error pointing at the hook.

Zero-dependency by design: the official MCP SDK requires pydantic/anyio which
are not vendored in ``.python_libs``. The stdio transport is newline-delimited
JSON-RPC 2.0 — small enough to speak directly. stdout carries protocol frames
only; all logging goes to stderr.

Manual harness wiring (Install packs remain P3 — do not auto-edit configs):

    {"mcpServers": {"khipu": {
        "command": "/path/to/Khipu/packages/cli/bin/khipu-mcp"}}}

(``khipu integrations install <harness>`` writes this for you.)
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from pathlib import Path

SERVER_NAME = "khipu"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
LATEST_PROTOCOL = "2025-06-18"

SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX = 12, 50
GRAPH_LIMIT_DEFAULT, GRAPH_LIMIT_MAX = 25, 200
GRAPH_HOPS_MAX = 4


def _ensure_path() -> None:
    # This file is packages/cli/khipu/mcp_server.py; the repo is three levels
    # up. Cannot import khipu.paths yet — that is what this function enables.
    root = Path(os.environ.get("KHIPU_ROOT") or Path(__file__).resolve().parents[3])
    for p in (root / "packages" / "cli", root / ".python_libs"):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def _capture_mode() -> str:
    _ensure_path()
    from khipu.config import capture_mode

    return capture_mode()


_CAPTURE_HOOK_MARKERS = ("khipu-stop-hook", "khipu-aegis-capture")


def _local_hook_config_paths() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".cursor" / "hooks.json",
        home / ".claude" / "settings.json",
        home / ".codex" / "hooks.json",
        home / ".grok" / "config.toml",
    )


def _local_capture_hook_is_writer() -> bool:
    """True when a local harness is configured to run the Khipu capture hook."""
    for path in _local_hook_config_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            # Missing files skip; an existing but unreadable harness config
            # must not fail-open into a second writer (Night School 466).
            print(
                f"[khipu-mcp] unreadable {path}: {exc}; "
                "treating as capture-hook writer to avoid dual-write",
                file=sys.stderr,
            )
            return True
        if any(marker in text for marker in _CAPTURE_HOOK_MARKERS):
            return True
    return False


# Set by khipu.gateway at startup (both in-process and, for a container that
# re-execs, through the env var). The old detector walked the call stack for a
# frame whose ``__name__`` was "khipu.gateway" — which is never true in the
# shipped container, because it runs ``python -m khipu.gateway`` and that
# module's __name__ is "__main__". Every gateway capture therefore looked like
# a local stdio call and was rejected as a double-write (audit 2026-09-04).
GATEWAY_ACTIVE_ENV = "KHIPU_GATEWAY_ACTIVE"
_GATEWAY_ACTIVE = False


def mark_gateway_active() -> None:
    """Called once by ``khipu.gateway.serve`` — this process IS the gateway."""
    global _GATEWAY_ACTIVE
    _GATEWAY_ACTIVE = True
    os.environ[GATEWAY_ACTIVE_ENV] = "1"


def _via_https_gateway() -> bool:
    """True when this tools/call arrived through ``khipu.gateway`` (HTTPS)."""
    if _GATEWAY_ACTIVE:
        return True
    return (os.environ.get(GATEWAY_ACTIVE_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _stdio_hook_owns_capture() -> bool:
    """Local stdio + installed capture hook: MCP must not write."""
    if _via_https_gateway():
        return False
    return _local_capture_hook_is_writer()


def _default_memory_root() -> Path | None:
    _ensure_path()
    from khipu.config import path_setting

    return path_setting("memory_root")


TOOLS: list[dict] = [
    {
        "name": "khipu_search",
        "description": (
            "Search Khipu memory. Default mode='hybrid': cosine similarity + "
            "token overlap (over the embedded text) + literal substring match, "
            "fused by reciprocal-rank fusion and ranked by score; per-kind "
            "fairness only backfills the tail if a kind is entirely absent from "
            "the top results. Graph nodes are excluded from hybrid/literal "
            "results unless kind='node' or the query looks id-shaped (contains "
            "':' or '__'). If no embedding profile is active, hybrid degrades to "
            "literal + token overlap only and the payload carries "
            "degraded='no-embedding'. mode='literal' is the old ILIKE-only "
            "behaviour — use it for exact strings, ids, hashes, or error text. "
            "mode='semantic' (same as the legacy semantic=true) is cosine + "
            "token-overlap only, no literal list. Filters apply on every mode: "
            "kind (episode/topic/node, or episode/topic/media for semantic), "
            "project (matches episode project or scope), since/until (ISO date "
            "or relative like '7d'/'24h'), session_id (prefix match), harness "
            "(prefix of session_id before the colon). Returns JSON rows of "
            "{kind, id, label, snippet, score, cosine, lexical_hits} plus "
            "additive paths (filesystem tokens) and neighbors (capped 1-hop "
            "wiki/path edges) on topic hits; a topic hit also carries status "
            "(active/superseded/retired/abandoned — the latter three are "
            "de-ranked, not excluded) and, when known, project. `score` is a "
            "fused rank, NOT a similarity — it sits in the same narrow band "
            "for a real hit and for gibberish. `cosine` (raw cosine "
            "similarity, hybrid/semantic only) and `lexical_hits` (how many "
            "query tokens this row names verbatim) are the honest signals; "
            "the top-level `confidence` ('none'|'weak'|'strong') is derived "
            "from them across the whole result set — treat 'none' as "
            "'nothing found', not as a low-ranked hit. Tombstoned topics are "
            "excluded. Snippets are word-boundary teasers; call khipu_get for "
            "the full row."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term (plain text, not a pattern)",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max total results, default {SEARCH_LIMIT_DEFAULT}, "
                        f"cap {SEARCH_LIMIT_MAX}"
                    ),
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "'hybrid' (default), 'literal', or 'semantic'. Omit to get hybrid."
                    ),
                },
                "semantic": {
                    "type": "boolean",
                    "description": (
                        "Deprecated alias for mode='semantic' (default false)"
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "hybrid/literal: 'episode', 'topic', or 'node'. "
                        "semantic: 'episode', 'topic', or 'media'."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": "Match episode project (or scope, before migration 0008)",
                },
                "since": {
                    "type": "string",
                    "description": "ISO date/datetime or relative, e.g. '7d', '24h'",
                },
                "until": {
                    "type": "string",
                    "description": "ISO date/datetime or relative, e.g. '7d', '24h'",
                },
                "session_id": {
                    "type": "string",
                    "description": "Episode session_id prefix match",
                },
                "harness": {
                    "type": "string",
                    "description": "Prefix of session_id before the colon, e.g. 'claude_code'",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "khipu_get",
        "description": (
            "Load a search hit by id. Episodes: full summary, decisions, "
            "preferences, topics (not the capture raw blob). Topics: full "
            "page body. Media: path/sha256/mime. Search snippets are teasers; "
            "use this instead of guessing from a clipped line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "Episode id (digits), topic slug, or media_assets id"
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Optional: 'episode', 'topic', or 'media'. "
                        "Inferred from id when omitted."
                    ),
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "khipu_graph",
        "description": (
            "Neighborhood of a graph node id (undirected). hops=1 returns "
            "direct edges; hops>=2 walks a recursive undirected CTE ordered "
            "hops-then-node. Topic slugs from search expand as "
            "{slug, topic:slug, memory_topic:slug}. Digit ids are episodes: "
            "the walk is that episode's capture topics (synthetic "
            "capture_topic edges), not a node named with the episode number."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Node id to expand"},
                "hops": {
                    "type": "integer",
                    "description": f"1..{GRAPH_HOPS_MAX}, default 1",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max rows, default {GRAPH_LIMIT_DEFAULT}, cap {GRAPH_LIMIT_MAX}"
                    ),
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "khipu_status",
        "description": (
            "Khipu hub status: PG table counts, latest episode ts, true "
            "mirror lag, recent captures. include_drift=true adds the "
            "file-vs-PG drift sample (slower: walks the memory root). "
            "When `prompt` is given and `full` is not set, the schema "
            "narrows to a LIGHT payload — {hub_ok, prior_work, "
            "prior_work_text, prior_work_meta, notes_freshness} only, no "
            "counts/drift/recent_captures — so this call costs roughly what `prior_work` "
            "alone costs (the heavy status queries run concurrently with "
            "the prior_work lane on a background thread, never sequentially "
            "in front of it; measured live: the full payload used to add "
            "100ms+ on top of prior_work_meta.ms even though nothing in it "
            "depends on `prompt`). Pass `full=true` with `prompt` to get "
            "the complete schema below anyway (unchanged, still pays the "
            "full sequential cost). Omitting `prompt` always returns the "
            "complete schema, exactly as before this note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_drift": {"type": "boolean", "description": "Default false"},
                "prompt": {
                    "type": "string",
                    "description": (
                        "R10: for a harness with no pushed slice and no per-prompt "
                        "push (Aegis, the gateway) — pass the first user prompt of "
                        "the session and the response carries the same top-3 'prior "
                        "work' a UserPromptSubmit hook would otherwise have pushed, "
                        "as `prior_work`: a list of up to 3 {kind, id, date, project, "
                        "status, snippet} items ([] when nothing clears the relevance "
                        "floor, null only when the prompt itself gated). "
                        "`prior_work_text` carries the same material pre-rendered as a "
                        "≤600-char Markdown block for a caller that just wants to "
                        "paste it. Omit `prompt` once the session already has that "
                        "context. Also switches the response to the LIGHT payload "
                        "(see the tool description) unless `full` is set."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional, with `prompt`: boosts hits from this repo's project.",
                },
                "budget_ms": {
                    "type": "integer",
                    "description": (
                        "Optional, with `prompt`: server-side time budget for `prior_work`, "
                        "default 600. Runs the lexical and query-embedding+cosine legs "
                        "concurrently and answers with whatever finished by the deadline "
                        "(fused when both did, lexical-only with prior_work_meta.degraded "
                        "= 'embedding late' when the embedding leg is still running) — it "
                        "keeps running in the background either way, so a repeat of the "
                        "same prompt is warm next time. Clamped to [100, 5000]."
                    ),
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "With `prompt`: return the complete status schema (counts, "
                        "drift, recent_captures, search_degraded_last_24h, …) instead "
                        "of the light {hub_ok, prior_work, prior_work_text, "
                        "prior_work_meta, notes_freshness} payload. Default false. "
                        "Ignored without `prompt` — the complete schema is already "
                        "the only shape."
                    ),
                },
            },
        },
    },
    {
        "name": "khipu_capture",
        "description": (
            "Remember this session in Khipu — works everywhere now (K1). Cloud / "
            "no capture hook: CALL THIS when you finish a substantive piece of "
            "work — a decision, a fix, a finding worth recalling later — with a "
            "1-3 sentence summary, short lowercase topic slugs, and any "
            "decisions/preferences; set session_id to '<harness>:<stable id>' "
            "(e.g. 'grokbot:<repo>:<task>'). Local Mac with khipu-stop-hook or "
            "khipu-aegis-capture (capture_mode=hub or dual): this does NOT write "
            "directly — the hook is the writer and a direct MCP write would "
            "double-capture — instead it flags the session and returns "
            "{queued: true, captured_by: 'next stop'}; the summary you pass "
            "becomes the job's note and lands in the episode's verbatim.note at "
            "the next Stop/PreCompact/SessionEnd, regardless of cadence. Through "
            "the HTTPS gateway (no local hook) it is the ONLY way the session is "
            "remembered and writes immediately, so do not skip it there."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Episode summary (required)",
                },
                "topics": {"type": "array", "items": {"type": "string"}},
                "people": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "preferences": {"type": "array", "items": {"type": "string"}},
                "scope": {
                    "type": "string",
                    "description": "general | trivial (trivial is skipped per protocol)",
                },
                "session_id": {"type": "string"},
                "project": {
                    "type": "string",
                    "description": (
                        "Stable project identity (e.g. 'owner/repo'). No local hook can "
                        "resolve this for a gateway/cloud caller, so pass it explicitly "
                        "when known — it is what W1/W4 key identity, dedup and the pushed "
                        "slice off of. Omit only when there truly is no repo context."
                    ),
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "khipu_owed",
        "description": (
            "List open (or closed/stale) commitments — followups, blockers, "
            "questions, promises captured as open_loops and not yet closed by "
            "a matching closed_loop/decision. Call this at session start for "
            "a harness with no pushed slice (Aegis), or whenever 'what do I "
            "still owe on this project' would change the answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to one project"},
                "status": {
                    "type": "string",
                    "description": "open (default) | closed | stale",
                },
                "limit": {"type": "integer", "description": "Default 50"},
            },
        },
    },
    {
        "name": "khipu_owed_update",
        "description": (
            "Close, reopen or snooze one commitment by id (ids come from "
            "khipu_owed). Close it when the work is done or the question is "
            "answered; snooze it with `until` (a date, or 7d / 2w / 1m) when it "
            "is real but not now. Cloud agents: this is how you close what you "
            "opened through khipu_capture."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Commitment id"},
                "action": {"type": "string", "description": "close | reopen | snooze"},
                "until": {"type": "string", "description": "snooze only: 2026-09-30, 7d, 2w, 1m"},
            },
            "required": ["id", "action"],
        },
    },
    {
        "name": "khipu_forget",
        "description": (
            "Forget one episode completely: the row is tombstoned, its vectors "
            "and the commitments it opened go with it, and its line leaves the "
            "legacy file. Use it for a capture that was wrong, a test, or "
            "something that should not have been remembered. Through the "
            "HTTPS gateway only episodes captured by a cloud harness can be "
            "forgotten; local captures are forgotten from the Mac that owns them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Episode id (khipu_capture returns it)"}},
            "required": ["id"],
        },
    },
]


def _tool_search(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = min(int(args.get("limit") or SEARCH_LIMIT_DEFAULT), SEARCH_LIMIT_MAX)
    _ensure_path()
    from khipu import query_log
    from khipu.hub_snapshot import hub_connection_failed, search_stale_payload

    mode = (args.get("mode") or "").strip().lower() or None
    if not mode:
        mode = "semantic" if bool(args.get("semantic")) else "hybrid"
    if mode not in ("hybrid", "literal", "semantic"):
        raise ValueError("mode must be 'hybrid', 'literal', or 'semantic'")
    kind = args.get("kind") or None
    allowed_kinds = ("episode", "topic", "media") if mode == "semantic" else ("episode", "topic", "node")
    if kind not in (None, *allowed_kinds):
        raise ValueError(f"kind must be one of {allowed_kinds}")
    project = args.get("project") or None
    since = args.get("since") or None
    until = args.get("until") or None
    session_id = args.get("session_id") or None
    harness = args.get("harness") or None

    try:
        from khipu.embed import hybrid_search

        payload = hybrid_search(
            query, limit=max(1, limit), mode=mode, kind=kind, project=project,
            since=since, until=until, session_id=session_id, harness=harness,
        )
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        payload = search_stale_payload(
            query, max(1, limit), semantic=(mode == "semantic"), kind=kind,
            since=since, until=until, project=project, session_id=session_id,
            harness=harness,
        )
    query_log.log_query(
        query, mode=mode,
        filters={"kind": kind, "project": project, "since": since, "until": until,
                 "session_id": session_id, "harness": harness},
        result_count=len(payload.get("results") or []), top=payload.get("results") or [],
        # The gateway host is public: it keeps a hash of the query, never the text.
        redact=_GATEWAY_ACTIVE or os.environ.get(GATEWAY_ACTIVE_ENV) == "1",
        degraded=payload.get("degraded"),
    )
    return payload


def _tool_get(args: dict) -> dict:
    ident = (args.get("id") or "").strip()
    if not ident:
        raise ValueError("id is required")
    kind = (args.get("kind") or "").strip().lower() or None
    if kind not in (None, "episode", "topic", "media"):
        raise ValueError("kind must be 'episode', 'topic', or 'media'")
    _ensure_path()
    from khipu.hub_snapshot import (
        episode_detail_snapshot,
        hub_connection_failed,
        stale_fields,
        topic_detail_snapshot,
    )

    inferred = kind
    if inferred is None:
        inferred = "episode" if ident.isdigit() else "topic"

    hub_failed = False
    try:
        from khipu.activity import episode_detail, media_detail, topic_detail

        if inferred == "episode":
            if not ident.isdigit():
                raise ValueError("episode id must be digits")
            row = episode_detail(int(ident))
            if row is None:
                if kind is not None:
                    raise ValueError(f"episode not found: {ident}")
                inferred = "topic"
            else:
                row = dict(row)
                row.pop("raw", None)
                return {"kind": "episode", **row}
        if inferred == "topic":
            row = topic_detail(ident)
            if row is None and kind is None:
                media = media_detail(ident)
                if media is not None:
                    return {"kind": "media", **media}
            if row is None:
                raise ValueError(f"topic not found: {ident}")
            return {"kind": "topic", **row}
        row = media_detail(ident)
        if row is None:
            raise ValueError(f"media not found: {ident}")
        return {"kind": "media", **row}
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        hub_failed = True

    if inferred == "episode":
        if not ident.isdigit():
            raise ValueError("episode id must be digits")
        row = episode_detail_snapshot(int(ident))
        if row is None:
            if kind is not None:
                raise ValueError(f"episode not found: {ident}")
            inferred = "topic"
        else:
            return {"kind": "episode", **row, **stale_fields()}
    if inferred == "topic":
        row = topic_detail_snapshot(ident)
        if row is None:
            raise ValueError(f"topic not found: {ident}")
        return {"kind": "topic", **row, **stale_fields()}
    if hub_failed:
        raise ValueError(f"media not found: {ident}")
    raise ValueError(f"not found: {ident}")


def _tool_graph(args: dict) -> dict:
    node_id = (args.get("id") or "").strip()
    if not node_id:
        raise ValueError("id is required")
    hops = min(max(1, int(args.get("hops") or 1)), GRAPH_HOPS_MAX)
    limit = min(max(1, int(args.get("limit") or GRAPH_LIMIT_DEFAULT)), GRAPH_LIMIT_MAX)
    _ensure_path()
    from khipu.cli import _graph_query
    from khipu.hub_snapshot import (
        graph_neighbors_snapshot,
        hub_connection_failed,
        stale_fields,
        try_hub_connect,
    )

    try:
        with try_hub_connect() as conn:
            with conn.cursor() as cur:
                return _graph_query(cur, node_id, hops, limit)
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        out = graph_neighbors_snapshot(node_id, hops, limit)
        out.update(stale_fields())
        return out


def _tool_status_light(args: dict) -> dict:
    """The ``prompt``-given, ``full``-not-set schema (2026-09-15): ``{hub_ok,
    prior_work, prior_work_text, prior_work_meta, notes_freshness}`` only.

    ``drift.status_payload`` does several sequential round trips (table
    counts, now()/max(ts), a second query for the most-recent capture row,
    ``notes_freshness`` on the same cursor, then two MORE connections for
    ``recent_captures`` and ``search_degraded_last_24h``) — measured live
    against the deployed gateway at ~100ms even before ``prior_work``'s own
    budgeted lane starts, none of which a caller passing ``prompt`` asked
    for. This path skips all of it and answers with just what such a caller
    needs.

    The one cheap probe this path still owes a caller (hub reachability +
    notes freshness) runs on a background thread CONCURRENTLY with
    ``_attach_prior_work``'s own budgeted lane (which runs its lexical/
    cosine legs on their own threads) — so this function's wall clock is
    ``max(probe, lane)``, not their sum. The probe is best-effort: any
    failure degrades ``hub_ok`` to False rather than raising, same posture
    as every other optional field on this tool.
    """
    payload: dict = {}
    probe: dict = {}

    def _probe() -> None:
        try:
            from khipu.db import connect
            from khipu.notes import notes_freshness

            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    probe["hub_ok"] = True
                    probe["notes_freshness"] = notes_freshness(cur)
        except Exception as exc:  # noqa: BLE001 — best-effort; prior_work
            # (on its own threads below) must land regardless.
            probe["hub_ok"] = False
            probe["hub_error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    _attach_prior_work(payload, args)
    # The probe is one SELECT 1 + one indexed-ish MAX(event_at) query
    # (~20-40ms measured) — by the time the budgeted lane above returns
    # (>= its own budget_ms, default 600) it has long since finished; this
    # join is a backstop against a genuinely wedged connection, not the
    # expected path.
    t.join(2.0)
    payload["hub_ok"] = probe.get("hub_ok", False)
    if "hub_error" in probe:
        payload["hub_error"] = probe["hub_error"]
    if "notes_freshness" in probe:
        payload["notes_freshness"] = probe["notes_freshness"]
    return payload


def _tool_status(args: dict) -> dict:
    _ensure_path()
    if str(args.get("prompt") or "").strip() and not args.get("full"):
        return _tool_status_light(args)
    from khipu.drift import status_payload
    from khipu.hub_snapshot import (
        hub_connection_failed,
        snapshot_freshness,
        status_payload_snapshot,
    )

    try:
        if args.get("include_drift"):
            payload = status_payload(_default_memory_root(), include_drift=True)
        else:
            payload = status_payload(None)
        # W2.4: behind_ingest_seconds only means something while PG answers.
        payload["hub_snapshot"] = snapshot_freshness(payload.get("latest_ingested_at"))
        # D4/R10: top-level convenience fields so a caller (or the model
        # reading its own tool result) does not have to know to dig into
        # hub_snapshot / session_capture for the two numbers that answer
        # "how stale is the local replica" and "is anything waiting to be
        # captured right now".
        payload["snapshot_age_seconds"] = payload["hub_snapshot"].get("age_seconds")
        try:
            from khipu import session_capture as _sc

            payload["pending_turns"] = sum(
                int(v.get("pending_turns") or 0)
                for v in _sc.liveness_all().get("harnesses", {}).values()
            )
        except Exception:  # noqa: BLE001 — an optional field must never break khipu_status
            pass
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        payload = status_payload_snapshot()
        if args.get("include_drift"):
            payload["drift_error"] = "hub unreachable; drift omitted"
        payload["hub_error"] = f"{type(exc).__name__}: {exc}"
    _attach_prior_work(payload, args)
    return payload


_BUDGET_MS_MIN = 100
_BUDGET_MS_MAX = 5000


_PRIOR_WORK_SNIPPET_LIMIT = 160
# _gated()'s own reason strings (recall_prompt.prior_work_for_prompt) — the
# ONLY terminal reasons produced before a search ever runs. Everything else
# (ok, dedup, a timeout, a search-time error) means the search DID run, so
# `prior_work` reads as `[]` ("checked, nothing there"), not `null` ("never
# checked" — reserved for these).
_PRIOR_WORK_GATED_REASONS = frozenset({"no content tokens", "trivial acknowledgment"})


def _prior_work_gated(reason: str) -> bool:
    return reason in _PRIOR_WORK_GATED_REASONS or reason.startswith("gate error:")


def _prior_work_items(hits: list[dict]) -> list[dict]:
    """The `prior_work` wire shape (2026-09-15): a list of up to 3 items —
    exactly `kind`/`id`/`date`/`project`/`status`/`snippet` — instead of the
    rendered Markdown string. Aegis (the client this field exists for) parses
    `prior_work` as this array; seeing a string instead, it fell back to a
    second `khipu_search` round trip, which is most of what put a "budgeted
    to 600ms" lane over a 1s wall in practice. `prior_work_text` (set by the
    caller) carries the old rendered block for a caller that just wants to
    paste it.

    `date`/`project`/`status` come from whatever the hit already carries —
    real for a hit found via `hub_snapshot` (a Mac with a fresh local
    replica) or one that also matched the budgeted lexical leg (episodes/
    topics both select `ts` there), `null` for a cosine-only hit or a caller
    with no snapshot and no lexical match on that row (the gateway's
    `_cosine_candidates` doesn't select project/status — no query added here
    to keep it that way).
    """
    from khipu.snippets import clip_snippet

    out = []
    for h in hits[:3]:
        kind = h.get("kind")
        ts = h.get("ts")
        date = str(ts)[:10] if ts else None
        raw = str(h.get("snippet") or h.get("label") or "")
        out.append({
            "kind": kind,
            "id": h.get("id"),
            "date": date,
            "project": h.get("project"),
            "status": h.get("status") if kind == "topic" else None,
            "snippet": clip_snippet(" ".join(raw.split()), _PRIOR_WORK_SNIPPET_LIMIT),
        })
    return out


def _attach_prior_work(payload: dict, args: dict) -> None:
    """R10: khipu_status's fallback push for a harness with no SessionStart
    or UserPromptSubmit hook to inject through (Aegis, the gateway) — the
    caller passes the session's first user prompt and gets the same top-3
    "prior work" a hook would otherwise have pushed. Mutates `payload` in
    place; never raises (prior_work_for_prompt already never does, but this
    is the one place a caller cannot afford a crash to reach from an
    optional field).

    Budgeted phase (2026-09-15): always runs through prior_work_for_prompt's
    budget_ms path now (default DEFAULT_HUB_BUDGET_MS = 600ms, clamped to
    [100, 5000] against a bad caller value) — this is precisely the R10 lane
    (Aegis, the gateway) the 1.0s hard slot-drop measurement targeted.

    Wire shape (2026-09-15): `prior_work` is a LIST of up to 3 items (see
    `_prior_work_items`) — `[]` when the search ran and nothing cleared the
    relevance floor, `None` only when the prompt itself gated before any
    search ran (`_prior_work_gated`). `prior_work_text` carries the same
    material pre-rendered as the old ≤600-char Markdown block, `None` when
    empty — unchanged in content, just a new key (`prior_work` used to BE
    this string). `prior_work_meta` carries legs/ms/degraded/reason when the
    callee provides it, exactly as before.
    """
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return
    try:
        from khipu.recall_prompt import DEFAULT_HUB_BUDGET_MS, prior_work_for_prompt

        budget_ms = args.get("budget_ms")
        try:
            budget_ms = int(budget_ms) if budget_ms is not None else None
        except (TypeError, ValueError):
            budget_ms = None
        if not budget_ms or budget_ms <= 0:
            budget_ms = DEFAULT_HUB_BUDGET_MS
        budget_ms = max(_BUDGET_MS_MIN, min(budget_ms, _BUDGET_MS_MAX))

        result = prior_work_for_prompt(prompt, cwd=args.get("cwd"), budget_ms=budget_ms)
        if _prior_work_gated(str(result.get("reason") or "")):
            payload["prior_work"] = None
        else:
            payload["prior_work"] = _prior_work_items(result.get("hits") or [])
        payload["prior_work_text"] = result["context"] or None
        meta = result.get("prior_work_meta")
        if meta is not None:
            payload["prior_work_meta"] = meta
    except Exception:  # noqa: BLE001 — an optional field must never break khipu_status
        pass


def _tool_capture(args: dict) -> dict:
    # Locked semantics (agent-integration note, "MCP + CLI write / read
    # semantics"), amended for K1: a local capture hook (khipu-stop-hook /
    # khipu-aegis-capture) is ALWAYS the writer when one is configured — an
    # MCP write here would double-capture, in any capture_mode. K1 changed
    # what happens next: instead of refusing outright, it flags the session
    # for its next Stop/PreCompact/SessionEnd (khipu.session_capture
    # request_capture_now), regardless of cadence, and the summary becomes
    # that job's note (verbatim.note on the resulting episode). No local hook
    # configured and capture_mode != hub: still a hard reject — nothing would
    # ever act on a flag with no hook to read it. The CLI ``khipu capture``
    # does not go through this function.
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("khipu_capture requires a non-empty string 'summary'")

    if _stdio_hook_owns_capture():
        from khipu.session_capture import newest_session_ref, request_capture_now

        sid_arg = str(args.get("session_id") or "")
        harness, sid = (sid_arg.split(":", 1) if ":" in sid_arg else (None, None))
        if not harness or not sid:
            ref = newest_session_ref()
            if ref is None:
                raise ValueError(
                    "khipu_capture found a local capture hook (khipu-stop-hook / "
                    "khipu-aegis-capture) but no active session to flag — no "
                    "per-session state file exists yet (the hook has not run once "
                    "in this session). Pass session_id='<harness>:<id>' explicitly, "
                    "or wait for the hook's first run and retry."
                )
            harness, sid = ref
        request_capture_now(harness, sid, note=summary.strip())
        return {"queued": True, "captured_by": "next stop", "harness": harness, "session_id": sid}

    mode = _capture_mode()
    if mode != "hub":
        raise ValueError(
            f"khipu_capture is rejected in capture_mode={mode}: in this mode the "
            "harness capture hook (khipu capture / capture_v2.py) is the writer, "
            "and an MCP write would double-capture. Set capture_mode=hub to "
            "write through MCP."
        )
    from khipu.capture import capture, load_payload

    # load_payload validates + mints ts; capture() routes by mode (hub → PG only).
    payload = load_payload(json.dumps({k: v for k, v in args.items() if v is not None}))
    # PG-row-only, exactly as khipu.probe does it: the legacy file leg prints
    # its own "OK · episode appended" lines on stdout, which on the stdio
    # transport is the JSON-RPC channel itself — one capture would corrupt the
    # protocol stream for the rest of the session.
    prev_mirror = os.environ.get("KHIPU_HUB_FILE_MIRROR")
    os.environ["KHIPU_HUB_FILE_MIRROR"] = "0"
    try:
        with contextlib.redirect_stdout(sys.stderr):
            rc = capture(payload, mode="hub")
    finally:
        if prev_mirror is None:
            os.environ.pop("KHIPU_HUB_FILE_MIRROR", None)
        else:
            os.environ["KHIPU_HUB_FILE_MIRROR"] = prev_mirror
    if rc != 0:
        raise ValueError(f"khipu capture exited {rc}")
    out = {"ok": True, "mode": mode, "ts": payload["ts"]}
    episode_id = _captured_episode_id(payload)
    if episode_id is not None:
        out["episode_id"] = episode_id
    return out


def _captured_episode_id(payload: dict) -> int | None:
    """The row this capture landed on, by the (ts, md5(summary)) identity every
    writer shares. Fail-open — the capture already succeeded, so an id lookup
    failure must not turn it into a tool error."""
    try:
        import hashlib

        from khipu.db import connect

        summary = (payload.get("summary") or "").strip()
        ts = payload.get("ts")
        if not summary or not ts:
            return None
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s",
                    (ts, hashlib.md5(summary.encode("utf-8")).hexdigest()),
                )
                row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        return None


def _tool_owed(args: dict) -> dict:
    """W3.4: list commitments. Read-only; goes straight at the hub (no
    snapshot fallback yet — commitments are new and small, same posture as
    khipu_capture rather than the read tools' stale-snapshot degrade)."""
    status = (args.get("status") or "open").strip().lower()
    if status not in ("open", "closed", "stale"):
        raise ValueError("status must be 'open', 'closed', or 'stale'")
    limit = min(max(1, int(args.get("limit") or 50)), 200)
    _ensure_path()
    from khipu import commitments
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            rows = commitments.list_owed(
                cur, project=args.get("project") or None, status=status, limit=limit
            )
    return {"status": status, "project": args.get("project") or None, "results": rows}


def _tool_owed_update(args: dict) -> dict:
    """Close / reopen / snooze one commitment — the write half of khipu_owed,
    so a cloud agent can close what it opened (audit 2026-09-04: cloud
    clients could open commitments they could never close)."""
    try:
        cid = int(args.get("id"))
    except (TypeError, ValueError):
        raise ValueError("id must be an integer commitment id") from None
    action = (args.get("action") or "").strip().lower()
    if action not in ("close", "reopen", "snooze"):
        raise ValueError("action must be 'close', 'reopen' or 'snooze'")
    _ensure_path()
    from khipu import commitments
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            if action == "snooze":
                from khipu.cli import _snooze_until_sql

                parsed = _snooze_until_sql(str(args.get("until") or ""))
                if parsed is None:
                    raise ValueError("until must be a date (2026-09-30) or a window (7d, 2w, 1m)")
                expr, param = parsed
                params = ([param] if param is not None else []) + [cid]
                cur.execute(f"UPDATE commitments SET due_after = {expr} WHERE id = %s", tuple(params))
                ok = cur.rowcount > 0
            else:
                ok = commitments.set_status(cur, cid, "closed" if action == "close" else "open")
        conn.commit()
    if not ok:
        raise ValueError(f"no such commitment: {cid}")
    return {"ok": True, "id": cid, "action": action,
            **({"until": str(args.get("until") or "")} if action == "snooze" else {})}


def _tool_forget(args: dict) -> dict:
    """Complete forget of one episode (khipu.forget). From the gateway only a
    cloud harness's own captures may be forgotten: a local episode belongs to
    the Mac that captured it and is forgotten there."""
    try:
        eid = int(args.get("id"))
    except (TypeError, ValueError):
        raise ValueError("id must be an integer episode id") from None
    _ensure_path()
    from khipu.forget import LOCAL_HARNESSES, forget_everywhere
    from khipu.db import connect

    if _via_https_gateway():
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT session_id FROM episodes WHERE id = %s", (eid,))
                row = cur.fetchone()
        if row is None:
            raise ValueError(f"no such episode: {eid}")
        harness = str(row[0] or "").split(":", 1)[0]
        if harness in LOCAL_HARNESSES:
            raise ValueError(
                f"episode {eid} was captured by {harness} on a Mac; forget it there "
                "(khipu episode forget) — the gateway forgets only cloud captures"
            )
    out = forget_everywhere(eid)
    if not out.get("ok"):
        raise ValueError(str(out.get("error") or "forget failed"))
    out.pop("identity", None)
    return out


TOOL_FUNCS = {
    "khipu_search": _tool_search,
    "khipu_get": _tool_get,
    "khipu_graph": _tool_graph,
    "khipu_status": _tool_status,
    "khipu_capture": _tool_capture,
    "khipu_owed": _tool_owed,
    "khipu_owed_update": _tool_owed_update,
    "khipu_forget": _tool_forget,
}


def _result(req_id, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_text(payload: dict, *, is_error: bool = False) -> dict:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, indent=2, default=str)}
        ],
        "isError": is_error,
    }


def handle_message(msg: dict) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    method = msg.get("method")
    req_id = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
        # `instructions` (MCP lifecycle, optional) is how the recall rule reaches
        # EVERY client without a per-repo rule file: it travels with the server,
        # so a cloud agent that connects account-level gets it in any repo.
        # Same text as the file-based rules, so the two can never drift.
        from khipu.recall_rule import RULE_MD

        return _result(
            req_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": RULE_MD.strip(),
            },
        )
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        func = TOOL_FUNCS.get(name)
        if func is None:
            return _error(req_id, -32602, f"unknown tool: {name}")
        try:
            payload = func(params.get("arguments") or {})
        except ValueError as exc:  # argument/mode rejections → tool error, not crash
            return _result(req_id, _tool_text({"error": str(exc)}, is_error=True))
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 — DB down, or SystemExit from
            # capture.load_payload (it exits EX_DATAERR on a malformed payload,
            # which inside a long-lived MCP server killed the whole process and
            # took every other client's session with it — audit 2026-09-04).
            # A tool failing is a tool error; only the transport may end us.
            if isinstance(exc, SystemExit):
                detail = f"SystemExit: capture rejected the payload (exit {exc.code})"
            else:
                detail = f"{type(exc).__name__}: {exc}"
            return _result(req_id, _tool_text({"error": detail}, is_error=True))
        return _result(req_id, _tool_text(payload))
    if is_notification:
        return None  # notifications/initialized, cancellations, etc.
    return _error(req_id, -32601, f"method not found: {method}")


def main() -> int:
    print(f"[khipu-mcp] ready (pid {os.getpid()})", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print(json.dumps(_error(None, -32700, "parse error")), flush=True)
            continue
        response = handle_message(msg)
        if response is not None:
            print(json.dumps(response, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
