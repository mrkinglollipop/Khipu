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
  - ``khipu_usage``   — plan headroom per subscription CLI (local only; the
    gateway answers ``available: false``)
  - ``khipu_route``: the routing record: which accounts are open, by whom
    and since when (local only until the cloud copy; the gateway answers
    ``available: false``). Khipu reports; it never picks or ranks.
  - ``khipu_route_report``: a conversation's report or hold; the author is
    set by Khipu, and the owner's changes are refused.
  - ``khipu_route_rule``: a conversation may record the owner's requested
    standing-rule addition or edit; it is live but stays pending until the
    owner confirms it in Settings or at a terminal.
  - ``khipu_route_priority``: a conversation may record the owner's priority
    (his order of accounts) at his request; live, unconfirmed until he
    confirms it; confirming, undoing and clearing stay his.

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
import re
import sys
import threading
from pathlib import Path

SERVER_NAME = "khipu"


def _server_version() -> str:
    """``khipu.__version__`` plus ``+<build>`` when KHIPU_BUILD is set to
    something real (the gateway container's own commit stamp — see
    ``khipu.gateway.BUILD``), never a bare "unknown" suffix."""
    from khipu import __version__

    build = (os.environ.get("KHIPU_BUILD") or "").strip()
    if build and build != "unknown":
        return f"{__version__}+{build}"
    return __version__


SERVER_VERSION = _server_version()
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
LATEST_PROTOCOL = "2025-06-18"

SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX = 12, 50
GRAPH_LIMIT_DEFAULT, GRAPH_LIMIT_MAX = 25, 200
GRAPH_HOPS_MAX = 4
MAX_FRAME_BYTES = 1024 * 1024

from khipu.text_pages import (  # noqa: E402  khipu is importable: _server_version ran above
    PAGE_BYTES_DEFAULT,
    PAGE_BYTES_MAX,
    PAGE_BYTES_MIN,
    page_args,
)


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
    paths = [
        home / ".cursor" / "hooks.json",
        home / ".claude" / "settings.json",
        home / ".codex" / "hooks.json",
        home / ".grok" / "config.toml",
    ]
    try:
        from khipu import claude_homes

        paths.extend(h.settings_path for h in claude_homes.discover(home=home))
    except Exception:  # noqa: BLE001 — discovery must never take the writer check down
        return tuple(paths)
    seen: set[str] = set()
    unique = []
    for p in paths:
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            unique.append(p)
    return tuple(unique)


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
    """True when this tools/call arrived through ``khipu.gateway`` (HTTPS):
    the request carries a token label. The process-wide flag stays as the
    fallback for threads a request starts, which do not inherit its label."""
    from khipu.request_context import gateway_label

    if gateway_label() or _GATEWAY_ACTIVE:
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
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Search Khipu memory. Default mode='hybrid': cosine similarity + "
            "token overlap (over the embedded text) + literal substring match, "
            "fused by reciprocal-rank fusion and ranked by score; per-kind "
            "fairness only backfills the tail if a kind is entirely absent from "
            "the top results. Graph nodes are excluded from hybrid/literal "
            "results unless kind='node' or the query looks id-shaped (contains "
            "':' or '__'). Libraries (folders of books and documents indexed as "
            "their own search space) are searched with kind='library' and "
            "included in the default search; a hit is "
            "{kind:'library', id:'library:<name>:<doc>#<chunk>', label:'Author, "
            "Title', snippet, path, source}, and `source` restricts to one "
            "library. If no embedding profile is active, hybrid degrades to "
            "literal + token overlap only and the payload carries "
            "degraded='no-embedding'. mode='literal' is the old ILIKE-only "
            "behaviour — use it for exact strings, ids, hashes, or error text. "
            "mode='semantic' (same as the legacy semantic=true) is cosine + "
            "token-overlap only, no literal list. Filters apply on every mode: "
            "kind (episode/topic/node/library, or episode/topic/media/library for "
            "semantic), "
            "project (matches episode project or scope), since/until (ISO date "
            "or relative like '7d'/'24h'), session_id (prefix match), harness "
            "(prefix of session_id before the colon), t3_thread (exact T3 thread id) "
            "and via (exact capture origin, e.g. 't3'). T3/origin filters are "
            "episode-only. Returns JSON rows of "
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
            "the full row (for a library hit, the chunk with its neighbours)."
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
                        "hybrid/literal: 'episode', 'topic', 'node', or 'library'. "
                        "semantic: 'episode', 'topic', 'media', or 'library'."
                    ),
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Library name (restricts a library search to that "
                        "library; implies kind='library')"
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
                "t3_thread": {
                    "type": "string",
                    "description": "Exact T3 thread id across providers and accounts (episodes only)",
                },
                "via": {
                    "type": "string",
                    "description": "Exact capture origin, e.g. 't3' (episodes only)",
                },
                "tz": {
                    "type": "string",
                    "description": (
                        "IANA zone name (e.g. 'America/New_York') for interpreting a natural-"
                        "language time phrase in `query` (e.g. 'last week'), when the "
                        "time_interpretation feature is on and no explicit since/until is "
                        "given. Defaults to UTC; an unrecognized name also falls back to UTC."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "khipu_get",
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Load a search hit by id. Episodes: full summary, decisions, "
            "preferences, topics (not the capture raw blob). Topics: the page "
            "body in pages of at most max_bytes UTF-8 bytes (default 65536, "
            "most topics fit in one page). Every topic page carries id, kind, "
            "slug, title, updated_at, content_hash, body_total_bytes, "
            "body_offset, body_next_offset and complete; while complete is "
            "false, call again with offset set to body_next_offset to read the "
            "rest. Media: path/sha256/mime. Library ids "
            "(library:<name>:<doc>#<chunk>): the chunk text, its document "
            "(title, author, tags, rel_path, root) and the previous/next chunk "
            "text so you can read around a hit; library:<name>:<doc> returns the "
            "document and its chunk count. Search snippets are teasers; "
            "use this instead of guessing from a clipped line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "Episode id (digits), topic slug, media_assets id, or "
                        "library id (library:<name>:<doc>[#<chunk>])"
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Optional: 'episode', 'topic', 'media', or 'library'. "
                        "Inferred from id when omitted."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Topics only: UTF-8 byte offset into the body to start "
                        "the page at (default 0). Pass the previous page's "
                        "body_next_offset."
                    ),
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": PAGE_BYTES_MIN,
                    "maximum": PAGE_BYTES_MAX,
                    "description": (
                        "Topics only: largest page body in UTF-8 bytes "
                        f"(default {PAGE_BYTES_DEFAULT}, at most {PAGE_BYTES_MAX}). "
                        "A page never splits a character."
                    ),
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "khipu_graph",
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
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
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Khipu hub status: PG table counts, latest episode ts, true "
            "mirror lag, recent captures. include_drift=true adds the "
            "file-vs-PG drift sample (slower: walks the memory root). "
            "When `prompt` is given and `full` is not set, the schema "
            "narrows to a LIGHT payload — {hub_ok, prior_work, "
            "prior_work_text, prior_work_meta, notes_freshness, contract, "
            "routing} only (`routing` is the routing summary a conversation "
            "gets at its start: `khipu_route` has the rest), no counts/drift/recent_captures — so this call costs roughly what `prior_work` "
            "alone costs (the heavy status queries run concurrently with "
            "the prior_work lane on a background thread, never sequentially "
            "in front of it; measured live: the full payload used to add "
            "100ms+ on top of prior_work_meta.ms even though nothing in it "
            "depends on `prompt`). Pass `full=true` with `prompt` to get "
            "the complete schema below anyway (unchanged, still pays the "
            "full sequential cost). Omitting `prompt` always returns the "
            "complete schema, exactly as before this note. The complete "
            "schema's `warnings` lists plans nearly used up (a window still "
            "in force at 90% or more), read from the khipu_usage cache only."
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
                "project": {
                    "type": "string",
                    "description": (
                        "Optional, with `prompt`: the project boost to use when `cwd` is "
                        "absent or cannot be resolved on this host (the gateway cannot "
                        "resolve a caller's path at all). A `cwd` that resolves still wins."
                    ),
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
                        "prior_work_meta, notes_freshness, contract} payload. Default false. "
                        "Ignored without `prompt` — the complete schema is already "
                        "the only shape."
                    ),
                },
            },
        },
    },
    {
        "name": "khipu_capture",
        "annotations": {"readOnlyHint": False},
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
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
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
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
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
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
        "description": (
            "Forget one episode: every reader stops returning it, its vectors "
            "and the commitments it opened go with it, and its lines leave the "
            "legacy logs. The hub and each Mac's local replica keep its row and "
            "text, marked forgotten; no command purges it yet, and no command "
            "restores it. A queued capture job that is an exact copy of it is "
            "removed; a job that cannot be read, matches it only loosely, or that "
            "another live episode also holds is kept. The result says plainly "
            "(message, locations) whether the forget is complete, what was removed, "
            "and what was kept or could not be checked and why. Use it for a capture that was wrong, a test, or something "
            "that should not have been remembered. Through the HTTPS gateway only "
            "episodes captured by a cloud harness can be forgotten; local captures "
            "are forgotten from the Mac that owns them. "
            "A ledger id (ledger:<project>#<row id>) removes that ledger row, the "
            "same as `khipu ledger remove`: it leaves every list at once and the "
            "owner can restore it in the app for 30 days."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": ["integer", "string"],
                       "description": "Episode id (khipu_capture returns it), or ledger:<project>#<row id>"},
                "note": {"type": "string", "description": "Ledger rows only: why it is removed"},
                "children": {"type": "string", "enum": ["remove", "move-up"],
                             "description": "Ledger rows with children only: remove them too, or move them up"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "khipu_decisions",
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "List decisions for a project, with their validity state. Call this "
            "before answering what was decided or whether a choice is still "
            "current. status='standing' (default) excludes anything superseded "
            "or retracted; 'superseded'/'retracted'/'all' widen it; "
            "'conflicts' instead returns unresolved candidate links (both "
            "sides' text) that a person or agent has not yet confirmed or "
            "rejected. Supersede the moment the user reverses or replaces an "
            "earlier decision (khipu_decisions_update) — never for a mere "
            "refinement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to one project"},
                "status": {
                    "type": "string",
                    "description": "standing (default) | superseded | retracted | conflicts | all",
                },
                "since": {"type": "string", "description": "ISO date or relative, e.g. '7d', '24h'"},
                "limit": {"type": "integer", "description": "Default 20, cap 100"},
            },
        },
    },
    {
        "name": "khipu_decisions_update",
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
        "description": (
            "Supersede, restore, retract, confirm or reject one decision (ids "
            "come from khipu_decisions). action='supersede' takes id and "
            "EITHER by (an existing decision id) OR new_text (the replacement, "
            "in the user's own words — a new decision row is created for it) "
            "plus an optional reason; call it the moment the user reverses or "
            "replaces an earlier decision, never for a mere refinement, and "
            "give the reason in the user's own words. action='restore' undoes "
            "a supersession. action='retract' marks a decision wrong outright "
            "(not merely replaced) — takes id and an optional reason. "
            "action='confirm'/'reject' resolve a candidate link from "
            "khipu_decisions(status='conflicts') — take link_id, not id. "
            "Before migration 0024 this still supersedes (today's column) and "
            "the result says evidence fields were not recorded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "supersede | restore | retract | confirm | reject",
                },
                "id": {"type": "integer", "description": "Decision id (supersede/restore/retract)"},
                "link_id": {"type": "integer", "description": "Link id (confirm/reject)"},
                "by": {"type": "integer", "description": "supersede: an existing decision id"},
                "new_text": {"type": "string", "description": "supersede: replacement decision text"},
                "source_kind": {
                    "type": "string",
                    "description": "supersede with new_text: user | assistant | tool (default assistant)",
                },
                "reason": {
                    "type": "string",
                    "description": "supersede/retract: the reason, in the user's own words",
                },
                "project": {"type": "string", "description": "supersede with new_text: project for the new row"},
                "session_id": {
                    "type": "string",
                    "description": "supersede with new_text: session id for the new row",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "khipu_brief",
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Read the source-backed brief for one topic: a derived summary in "
            "which every claim lists the episode ids it came from (fetch one "
            "with khipu_get before acting on it). state is 'current' or "
            "'stale' (a source changed, was forgotten or was retracted since "
            "it was built); age_seconds and source_count say how fresh and "
            "how well-grounded it is. It is a summary, never evidence itself: "
            "cite the episodes. Returns {available: false, reason} when "
            "briefs are switched off or not yet set up, and found: false when "
            "the topic has none."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "Topic slug (from a search hit)"}},
            "required": ["topic"],
        },
    },
    {
        "name": "khipu_reflect",
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Answer one question from memory and show the evidence. Runs one "
            "search, then asks the configured model to answer only from what "
            "it found; every claim lists the episode or topic ids it rests on "
            "(fetch one with khipu_get before acting on it). Validation checks "
            "citation identifiers, not whether sources support the claims or "
            "the model's separate answer. Superseded or retracted "
            "decisions are treated as history. Abstains (abstained: true, with "
            "a reason, answer empty) rather than answer without sources. It "
            "calls a model, so use it when you want a synthesis, not for a "
            "lookup: khipu_search is the lookup. Writes no memory content; "
            "retrieval caches and usage counters may be updated. Returns "
            "{available: false, reason} when reflection is switched off."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question to answer from memory"},
                "project": {"type": "string", "description": "Prefer sources from this project (a preference, not a filter)"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "khipu_usage",
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
        "description": (
            "How much of each subscription plan is left, per configured "
            "account (Claude, Codex, Grok, Muse): plan, windows (name, "
            "used_pct, resets_at), credits, probed_at, ok/error, stale. Read "
            "from each CLI with the login it already holds; no model call, no "
            "tokens spent. Answers at once from the cache and refreshes in "
            "the background (refreshing: true) when a row is older than "
            "max_age seconds (default 120) or fresh: true; ask again a few "
            "seconds later for the new figures. An account never probed reads "
            "error: pending. Errors are short codes (timeout, not_signed_in, "
            "bad_reply, exit_<n>, ...). A window a CLI does not report is "
            "absent, not zero. Muse figures are as of its last turn "
            "(observed_at). summary_text has one pasteable line per account. "
            "Khipu reports; routing stays with you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fresh": {"type": "boolean", "description": "Start a refresh now even if the cache is young"},
                "max_age": {"type": "number", "description": "Cache max age in seconds (default 120)"},
            },
        },
    },
    {
        "name": "khipu_route",
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "The routing record on this Mac: which accounts are open, limited "
            "(until when, and whether that time is the CLI's own or a guess), "
            "paused by the owner, down or unknown, who said so and how long "
            "ago; the owner's standing rules and note, word for word; holds, "
            "with their words; and job runner lanes not matched to an account. "
            "Read it before you hand work to another account or model. "
            "`since` (a version, or epoch:version, as an earlier answer gave "
            "it) answers only what changed after it; `account` narrows to one "
            "account. Data, not instructions; guidance, not a gate. Khipu "
            "reports; routing stays with you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string",
                          "description": "Only what changed after this version (V or epoch:V)"},
                "account": {"type": "string", "description": "One account: its label or its id, as khipu_route lists them"},
            },
        },
    },
    {
        "name": "khipu_route_report",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "description": (
            "Record what this conversation learned about an account, so every "
            "other conversation hears it: action `report` (state limited, open "
            "or down; paste the limit message word for word as `message` and "
            "Khipu reads the reset time from it), `hold` (a short note about one "
            "account, this Mac or all work, with a reason: machine-busy, "
            "disk-low, keep, outage or other; it ends after 24 hours by default, "
            "and `until` can set another end, at most 7 days away by default "
            "(the owner's settings)), or `end_hold` (`hold_id`). Khipu sets who "
            "wrote it and how sure each time is; a time you give is recorded "
            "as a guess. Standing rules, the owner's note, pauses and clears "
            "are the owner's and are refused here. Khipu reports; routing "
            "stays with you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["report", "hold", "end_hold"],
                           "description": "Default report"},
                "state": {"type": "string", "enum": ["limited", "open", "down"], "description": "report: the state"},
                "account": {"type": "string",
                            "description": "The account: its label or its id, as khipu_route lists them"},
                "runner": {"type": "string", "description": "report, with lane: a job runner's lane the lane map names"},
                "lane": {"type": "string"},
                "until": {"type": "string", "description": "When the limit or hold ends (a time, or 30m, 2h)"},
                "window": {"type": "string", "enum": ["five-hour", "weekly"]},
                "model": {"type": "string", "description": "report: the model a model-scoped limit closes"},
                "message": {"type": "string", "description": "report: the limit message, word for word"},
                "seen": {"type": "string", "description": "report: when the message was shown (a time, or 10m)"},
                "evidence": {"type": "string", "description": "report: one line of evidence"},
                "text": {"type": "string", "description": "hold: its words"},
                "reason": {"type": "string", "enum": ["machine-busy", "disk-low", "keep", "outage", "other"]},
                "scope": {"type": "string", "enum": ["account", "machine", "all"],
                          "description": "hold: one account, this Mac or all work"},
                "hold_id": {"type": "string", "description": "end_hold: the hold's id, as khipu_route lists it"},
                "name": {"type": "string",
                         "description": "What this conversation calls itself (shown beside its author)"},
            },
        },
    },
    {
        "name": "khipu_route_rule",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "description": (
            "Record the owner's request to add a standing rule, or to change "
            "one when `rule` names its number. The wording is live for every "
            "conversation immediately, marked with this conversation as the "
            "requester, and remains pending until the owner confirms it in "
            "Settings -> Routing or with a terminal confirmation. This tool "
            "cannot confirm, retire, dismiss or undo a rule."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The owner's requested rule wording"},
                "rule": {"type": "integer", "minimum": 1,
                         "description": "Existing standing rule number to change; omit to add"},
                "owner_request": {"type": "string", "description": "This conversation's one-line words describing the owner's request"},
            },
            "required": ["text", "owner_request"],
        },
    },
    {
        "name": "khipu_route_priority",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "description": (
            "Record the owner's priority at his request: the full ordered list "
            "of accounts he wants used first (each by its label or its id, as "
            "khipu_route lists them), with his words for each one's role and an "
            "optional end. It is live for every conversation at once, marked "
            "with this conversation and unconfirmed, with the priority it "
            "replaced shown beside it, until the owner confirms or undoes it "
            "in Settings -> Routing. Khipu keeps his order as given and never "
            "reorders it. This tool cannot confirm, undo or clear a priority."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array", "minItems": 1,
                    "description": "The accounts in the owner's order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account": {"type": "string", "description": "Its label or its id, as khipu_route lists them"},
                            "role": {"type": "string", "description": "The owner's words for its role"},
                            "until": {"type": "string", "description": "When this entry ends (a time, or 2d, 30h)"},
                        },
                        "required": ["account"],
                    },
                },
                "until": {"type": "string", "description": "When the whole priority ends (a time, or 3d)"},
                "owner_request": {"type": "string",
                                  "description": "This conversation's one-line words describing the owner's request"},
            },
            "required": ["entries", "owner_request"],
        },
    },
    {
        "name": "khipu_ledger",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "description": (
            "The project's ledger of what work is done and what is not, with "
            "the old tracker script's verbs: add, set, get, list, loose-ends, "
            "needs-owner (needs-matt is the same), render (the markdown board), "
            "check, remove and removed. Writes keep the old and new value of "
            "every field in the row's history; pass if_state to write only if "
            "the row is still in that state, and op_id to retry a write safely. "
            "Rows carry titles and short plain-words lines about the work, "
            "never code, diffs, file contents, command output, logs, prompts, "
            "briefs, customer or financial details. Restoring a removed row, "
            "removing it for good and importing an old tracker are the "
            "owner's, in the app."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "verb": {"type": "string", "enum": ["add", "set", "get", "list", "loose-ends", "needs-owner",
                                                    "needs-matt", "render", "check", "remove", "removed"]},
                "project": {"type": "string", "description": "Project key (default: this folder's project)"},
                "id": {"type": "string", "description": "add, set, get, remove: the row id"},
                "title": {"type": "string"},
                "kind": {"type": "string", "description": "add: one of the project's kinds"},
                "parent": {"type": "string", "description": "add: the parent row's id"},
                "state": {"type": "string", "description": "add, set: one of the project's states; list: filter"},
                "evidence": {"type": "string", "description": "A short plain-words line, such as a PR number"},
                "owner": {"type": "string"},
                "note": {"type": "string", "description": "A short plain-words line for the row's history"},
                "link": {"type": "string", "description": "An https link"},
                "if_state": {"type": "string", "description": "set: write only if the row is still in this state"},
                "children": {"type": "string", "enum": ["remove", "move-up"],
                             "description": "remove: what happens to the row's children"},
                "op_id": {"type": "string", "description": "Pass the same id to retry a write safely"},
                "stale_hours": {"type": "number", "description": "list: only rows unchanged for this long"},
                "summary": {"type": "boolean", "description": "loose-ends: counts per state only"},
            },
            "required": ["verb"],
        },
    },
]


def _positive_id(value, field: str) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value and value.isascii() and value.isdecimal():
        parsed = int(value)
        if parsed > 0:
            return parsed
    raise ValueError(f"{field} must be a positive integer id")


def _detail_response(kind: str, row: dict, **metadata) -> dict:
    row = dict(row)
    if kind == "episode":
        row.pop("raw", None)
    return {"kind": kind, **row, **metadata}


def _tool_search(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    from khipu.redact import redact_secrets

    query = redact_secrets(query)[0]
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
    source = (args.get("source") or "").strip() or None
    allowed_kinds = (
        ("episode", "topic", "media", "library") if mode == "semantic"
        else ("episode", "topic", "node", "library")
    )
    if kind not in (None, *allowed_kinds):
        raise ValueError(f"kind must be one of {allowed_kinds}")
    if source and kind not in (None, "library"):
        raise ValueError("source applies to kind 'library' only")
    project = args.get("project") or None
    since = args.get("since") or None
    until = args.get("until") or None
    session_id = args.get("session_id") or None
    harness = args.get("harness") or None
    t3_thread = (args.get("t3_thread") or "").strip() or None
    via = (args.get("via") or "").strip() or None
    tz = args.get("tz") or None

    offline = False
    try:
        from khipu.embed import hybrid_search

        payload = hybrid_search(
            query, limit=max(1, limit), mode=mode, kind=kind, project=project,
            since=since, until=until, session_id=session_id, harness=harness, t3_thread=t3_thread, via=via,
            tz=tz, source=source,
        )
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        if source or kind == "library":
            raise ValueError(
                "library search needs the hub; the offline snapshot holds no libraries"
            ) from exc
        offline = True
        payload = search_stale_payload(
            query, max(1, limit), semantic=(mode == "semantic"), kind=kind,
            since=since, until=until, project=project, session_id=session_id,
            harness=harness, t3_thread=t3_thread, via=via, tz=tz,
        )
    query_log.log_query(
        query, mode=mode,
        filters={"kind": kind, "project": project, "since": since, "until": until,
                 "session_id": session_id, "harness": harness, "t3_thread": t3_thread,
                 "via": via, "source": source},
        result_count=len(payload.get("results") or []), top=payload.get("results") or [],
        # The gateway host is public: it keeps a hash of the query, never the text.
        redact=_GATEWAY_ACTIVE or os.environ.get(GATEWAY_ACTIVE_ENV) == "1",
        degraded=payload.get("degraded"),
        offline=offline,
    )
    return payload


def _tool_get(args: dict) -> dict:
    ident = (args.get("id") or "").strip()
    if not ident:
        raise ValueError("id is required")
    kind = (args.get("kind") or "").strip().lower() or None
    if kind not in (None, "episode", "topic", "media", "library"):
        raise ValueError("kind must be 'episode', 'topic', 'media', or 'library'")
    offset, max_bytes = page_args(args.get("offset"), args.get("max_bytes"))
    _ensure_path()
    from khipu import library_search

    if kind == "library" or (kind is None and library_search.is_library_id(ident)):
        try:
            return library_search.get(ident)
        except ValueError:
            raise
        except Exception as exc:
            from khipu.hub_snapshot import hub_connection_failed

            if hub_connection_failed(exc):
                raise ValueError(
                    "library text needs the hub; the offline snapshot holds no libraries"
                ) from exc
            raise
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
                # Additive (Phase 2A): decision_states (one entry per decision
                # this episode produced) + validity (current/superseded/
                # retracted counts). Hub path only — the sqlite replica has no
                # decisions table, so the stale-fallback payload below stays
                # exactly as it was.
                from khipu.decisions import decision_states_for_episode

                row.update(decision_states_for_episode(int(ident)))
                return _detail_response("episode", row)
        if inferred == "topic":
            row = topic_detail(ident, offset=offset, max_bytes=max_bytes)
            if row is None and kind is None:
                media = media_detail(ident)
                if media is not None:
                    return {"kind": "media", **media}
            if row is None:
                raise ValueError(f"topic not found: {ident}")
            return _detail_response("topic", row)
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
            return _detail_response("episode", row, **stale_fields())
    if inferred == "topic":
        row = topic_detail_snapshot(ident, offset=offset, max_bytes=max_bytes)
        if row is None:
            raise ValueError(f"topic not found: {ident}")
        return _detail_response("topic", row, **stale_fields())
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
            from khipu import agent_view

            probe["hub_error"] = agent_view.error_text(exc)

    routing: dict = {}

    def _routing() -> None:
        routing.update(_routing_light())

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    r = threading.Thread(target=_routing, daemon=True)
    r.start()
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
    # Local files only (no process, no model, no hub): long done by now.
    r.join(2.0)
    if routing:
        payload["routing"] = routing
    _attach_contract(payload)
    return payload


ROUTING_GATEWAY_REASON = (
    "the routing record lives on the Mac that runs the accounts; the HTTPS gateway has no copy yet. "
    "Use the local khipu MCP server or `khipu route`."
)


def _routing_light() -> dict:
    """The light payload's ``routing`` field: the summary a conversation
    gets at its start. Optional: a failure here costs only this field."""
    if _via_https_gateway():
        return {"available": False, "reason": ROUTING_GATEWAY_REASON}
    try:
        _ensure_path()
        from khipu import routing

        return routing.light_status()
    except Exception:  # noqa: BLE001 - an optional field must never break khipu_status
        return {}


def _attach_contract(payload: dict) -> None:
    """The capability signal. Optional: a failure here must never cost the
    caller the status it asked for."""
    try:
        from khipu import features

        payload["contract"] = features.contract()
    except Exception:  # noqa: BLE001
        pass


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
        payload["hub_snapshot"] = snapshot_freshness(
            payload.get("latest_ingested_at"), lag=payload.pop("replica_lag", None))
        # D4/R10: top-level convenience fields so a caller (or the model
        # reading its own tool result) does not have to know to dig into
        # hub_snapshot / session_capture for the two numbers that answer
        # "how stale is the local replica" and "is anything waiting to be
        # captured right now".
        payload["snapshot_age_seconds"] = payload["hub_snapshot"].get("age_seconds")
        payload["snapshot_state"] = payload["hub_snapshot"].get("state")
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
        from khipu import agent_view

        payload["hub_error"] = agent_view.error_text(exc)
    _attach_ledger(payload)
    _attach_prior_work(payload, args)
    _attach_contract(payload)
    _attach_plan_warnings(payload)
    return payload


def _attach_ledger(payload: dict) -> None:
    """The ledger line: rows, loose ends, rows waiting on the owner and
    waiting writes, across every project. When the hub already failed above
    it reports the outbox alone, without trying the hub again. Optional: a
    failure costs only this field."""
    with contextlib.suppress(Exception):
        from khipu import ledger

        payload["ledger"] = ledger.status_line(hub="hub_error" not in payload)


def _attach_plan_warnings(payload: dict) -> None:
    """The same plan warnings ``khipu doctor`` carries ("Plan nearly used up:
    Codex (pro) week at 94%, resets Thu 02:00Z"), read from the usage cache
    only: never a probe, never a wait. Optional: a failure here must never
    cost the caller the status it asked for."""
    try:
        from khipu import usage

        warnings = usage.doctor_block()["warnings"]
    except Exception:  # noqa: BLE001
        return
    with contextlib.suppress(Exception):
        from khipu import agent_view

        warnings = agent_view.scrub(warnings)
    payload["warnings"] = [*(payload.get("warnings") or []), *warnings]


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

    Phase 2, session B: an episode item's `status` now also comes from the
    row — `khipu.validity.annotate` only ever sets it when the episode's
    state is not current ("superseded"/"partly superseded"/"retracted"), so
    this is `None` for every episode this upgrade leaves unchanged, exactly
    as before. It also gains `validity` (the same dict `render_block`/
    `_row_tag` read) when the row carries one. Topic items are unchanged.
    """
    from khipu.snippets import clip_snippet

    out = []
    for h in hits[:3]:
        kind = h.get("kind")
        ts = h.get("ts")
        date = str(ts)[:10] if ts else None
        raw = str(h.get("snippet") or h.get("label") or "")
        item = {
            "kind": kind,
            "id": h.get("id"),
            "date": date,
            "project": h.get("project"),
            "status": h.get("status") if kind in ("topic", "episode") else None,
            "snippet": clip_snippet(" ".join(raw.split()), _PRIOR_WORK_SNIPPET_LIMIT),
        }
        if kind == "episode" and h.get("validity") is not None:
            item["validity"] = h["validity"]
        out.append(item)
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

        result = prior_work_for_prompt(
            prompt, cwd=args.get("cwd"), project=args.get("project"), budget_ms=budget_ms,
        )
        if _prior_work_gated(str(result.get("reason") or "")):
            payload["prior_work"] = None
        else:
            payload["prior_work"] = _prior_work_items(result.get("hits") or [])
        context = result["context"] or ""
        if len(context) > 600:
            context = context[:600].rsplit("\n", 1)[0]
        payload["prior_work_text"] = context or None
        meta = result.get("prior_work_meta")
        if meta is not None:
            if (not result.get("hits") and not meta.get("legs")
                    and meta.get("degraded") == "no legs completed"
                    and meta.get("outcome") == "no_match"):
                meta = {**meta, "outcome": "error", "reason": "error: no recall legs completed"}
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
        from khipu.session_capture import request_capture_now, resolve_session_ref

        sid_arg = str(args.get("session_id") or "")
        harness, _, sid = sid_arg.partition(":") if ":" in sid_arg else ("", "", sid_arg)
        # Which session is the caller's is decided from what the hooks recorded
        # about themselves, never from which state file is newest: two sessions
        # can be live at once. An unresolvable caller is refused, not guessed.
        harness, sid, resolved_by = resolve_session_ref(harness or None, sid or None)
        request_capture_now(harness, sid, note=summary.strip())
        return {"queued": True, "captured_by": "next stop", "harness": harness, "session_id": sid,
                "resolved_by": resolved_by}

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
    written: dict = {}
    try:
        with contextlib.redirect_stdout(sys.stderr):
            rc = capture(payload, mode="hub", result=written)
    finally:
        if prev_mirror is None:
            os.environ.pop("KHIPU_HUB_FILE_MIRROR", None)
        else:
            os.environ["KHIPU_HUB_FILE_MIRROR"] = prev_mirror
    if rc != 0:
        raise ValueError(f"khipu capture exited {rc}")
    out = {"ok": True, "mode": mode, "ts": payload["ts"]}
    episode_id = _captured_episode_id(written)
    if episode_id is not None:
        out["episode_id"] = episode_id
    return out


def _captured_episode_id(written: dict) -> int | None:
    """The row this capture landed on, as write_pg reported it. No lookup: the
    (ts, md5(summary)) re-select it replaced named the wrong row once a merge
    or edit had moved the summary. None when the write did not report one."""
    try:
        episode_id = written.get("episode_id")
        return int(episode_id) if episode_id is not None else None
    except (TypeError, ValueError):  # best-effort enrichment only
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
    cid = _positive_id(args.get("id"), "id")
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


class _ToolFailure(ValueError):
    def __init__(self, payload: dict):
        super().__init__(str(payload.get("error") or "tool failed"))
        self.payload = payload


def _tool_forget(args: dict) -> dict:
    """Complete forget of one episode (khipu.forget). From the gateway only a
    cloud harness's own captures may be forgotten: a local episode belongs to
    the Mac that captured it and is forgotten there."""
    if isinstance(args.get("id"), str) and args["id"].startswith("ledger:"):
        _ensure_path()
        from khipu import agent_view, ledger

        # A ledger answer, whichever tool gives it, leaves through
        # agent_view.project, like khipu_ledger's.
        try:
            return agent_view.project(ledger.remove_ref(args["id"], note=str(args.get("note") or ""),
                                                        children=args.get("children"), source="mcp"))
        except ledger.LedgerError as exc:
            raise _ToolFailure(agent_view.project({"ok": False, "error": str(exc), "reason": exc.reason})) from None
    eid = _positive_id(args.get("id"), "id")
    _ensure_path()
    from khipu.forget import KEPT, LOCAL_HARNESSES, forget_everywhere, plain_summary
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
    out.pop("identity", None)
    # Said plainly in every result, a partial one above all: what is kept by
    # design, and what still holds a copy or could not be checked.
    out.setdefault("kept", KEPT)
    out.setdefault("message", plain_summary(out))
    if not out.get("ok"):
        raise _ToolFailure(out)
    return out


def _tool_decisions(args: dict) -> dict:
    """Read half of the decisions tool pair (khipu_decisions). Reachable
    through stdio and the gateway exactly like khipu_owed — no snapshot
    fallback (decisions are hub-only, same posture as commitments)."""
    status = (args.get("status") or "standing").strip().lower()
    valid_status = ("standing", "superseded", "retracted", "conflicts", "all")
    if status not in valid_status:
        raise ValueError(f"status must be one of {valid_status}")
    limit = min(max(1, int(args.get("limit") or 20)), 100)
    project = args.get("project") or None
    since_raw = (args.get("since") or "").strip()
    since = None
    if since_raw:
        from khipu.search_text import parse_time_filter

        since = parse_time_filter(since_raw)
    _ensure_path()
    from khipu import decisions as _decisions
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            if status == "conflicts":
                results = _decisions.list_links(cur, state="candidate", project=project, limit=limit)
            else:
                results = _decisions.list_decisions(
                    cur, project=project, since=since, limit=limit, status=status,
                )
    return {"status": status, "project": project, "results": results}


def _tool_decisions_update(args: dict) -> dict:
    """Write half of the decisions tool pair. supersede/restore/retract take
    a decision id; confirm/reject take a link_id (from
    khipu_decisions(status='conflicts')). Every write here records its
    source as 'agent'. Every argument is validated before a connection is
    opened, same posture as ``_tool_owed_update``."""
    action = (args.get("action") or "").strip().lower()
    valid_actions = ("supersede", "restore", "retract", "confirm", "reject")
    if action not in valid_actions:
        raise ValueError(f"action must be one of {valid_actions}")

    if action in ("confirm", "reject"):
        link_id = _positive_id(args.get("link_id"), "link_id")
    else:
        decision_id = _positive_id(args.get("id"), "id")

    by = new_id_arg = new_text = None
    if action == "supersede":
        by = args.get("by")
        new_text = args.get("new_text")
        if (by is None) == (not new_text):
            raise ValueError("supersede needs exactly one of 'by' or 'new_text'")
        if by is not None:
            new_id_arg = _positive_id(by, "by")

    _ensure_path()
    from khipu import decisions as _decisions
    from khipu.db import connect
    from khipu.redact import redact_secrets

    reason = redact_secrets(str(args.get("reason") or ""))[0] or None

    with connect() as conn:
        with conn.cursor() as cur:
            evidence_ready = _decisions._evidence_ready(cur)
            degrade_note = (
                None if evidence_ready
                else "evidence fields were not recorded (migration 0024 not applied)"
            )
            if action in ("confirm", "reject"):
                out = _decisions.resolve_link(cur, link_id, action, source="agent")
                conn.commit()
                return out
            if action == "restore":
                ok = _decisions.restore(cur, decision_id)
                conn.commit()
                return {"ok": ok, "id": decision_id, "action": "restore"}
            if action == "retract":
                ok = _decisions.retract(cur, decision_id, reason)
                conn.commit()
                out = {"ok": ok, "id": decision_id, "action": "retract"}
                if degrade_note:
                    out["note"] = degrade_note
                return out
            # action == "supersede"
            if new_id_arg is not None:
                new_id = new_id_arg
            else:
                original = _decisions._fetch_decision_row(cur, decision_id)
                if original is None:
                    raise ValueError(f"no such decision: {decision_id}")
                if original.get("superseded_by") is not None:
                    return {"ok": False, "id": decision_id,
                            "superseded_by": original["superseded_by"], "action": "supersede"}
                new_id = _decisions.create_decision(
                    cur, project=args.get("project") or original.get("project"),
                    text=redact_secrets(str(new_text))[0],
                    session_id=args.get("session_id") or None,
                    source_kind=args.get("source_kind") or "assistant",
                )
            ok = _decisions.supersede(cur, decision_id, new_id, source="agent", reason=reason)
            if not ok and new_id_arg is None:
                conn.rollback()
            else:
                conn.commit()
            out = {"ok": ok, "id": decision_id, "action": "supersede"}
            if ok or new_id_arg is not None:
                out["superseded_by"] = new_id
            if degrade_note:
                out["note"] = degrade_note
            return out


def _tool_brief(args: dict) -> dict:
    """khipu_brief: the current source-backed brief for a topic. Hub-only, no
    snapshot fallback (same posture as decisions); the switch is checked before
    any connection is opened."""
    topic = (args.get("topic") or "").strip()
    if not topic:
        raise ValueError("topic is required")
    _ensure_path()
    from khipu import briefs, features

    if not features.enabled("briefs"):
        return {"available": False, "reason": briefs.REASON_SWITCH_OFF}
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            return briefs.read_brief(cur, topic)


def _tool_reflect(args: dict) -> dict:
    """khipu_reflect: an explicit cited answer. The switch is checked before
    any search or connection; nothing else in the server calls this."""
    question = (args.get("question") or "").strip()
    if not question:
        raise ValueError("question is required")
    _ensure_path()
    from khipu import features, reflect

    if not features.enabled("reflect"):
        return {"available": False, "reason": reflect.REASON_SWITCH_OFF}
    return reflect.reflect(question, project=args.get("project") or None)


USAGE_GATEWAY_REASON = (
    "plan usage is read from the CLIs on the machine that runs them; the HTTPS gateway has none. "
    "Use the local khipu MCP server or `khipu usage`."
)


def _tool_usage(args: dict) -> dict:
    """khipu_usage: each subscription CLI's own plan headroom. Local only: the
    gateway host runs no subscription CLI, so it says so instead of probing."""
    if _via_https_gateway():
        return {"available": False, "reason": USAGE_GATEWAY_REASON}
    _ensure_path()
    from khipu import usage

    max_age = args.get("max_age")
    if max_age is None or isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
        max_age = usage.DEFAULT_MAX_AGE_S
    # Cache-first: never waits on a probe; at most one refresh runs in the background.
    # Projected: a conversation never sees a login folder (the app's own command keeps them).
    return usage.public_report(usage.report(fresh=bool(args.get("fresh")), max_age=max(0.0, float(max_age)),
                                            background=True))


def _tool_route(args: dict) -> dict:
    """khipu_route: the routing record as ``khipu route --json`` gives it,
    plus the words the push leaves out. Local only until the cloud copy."""
    if _via_https_gateway():
        return {"available": False, "reason": ROUTING_GATEWAY_REASON}
    _ensure_path()
    from khipu import routing

    account = args.get("account")
    if account is not None and not isinstance(account, str):
        raise ValueError("account must be text")
    since = args.get("since")
    if since is not None and (isinstance(since, bool) or not isinstance(since, (str, int))):
        raise ValueError("since must be a version number, or epoch:version")
    return routing.conversation_view(since_version=since, account=account or None)


def _tool_route_report(args: dict) -> dict:
    """khipu_route_report: a report, a hold or a hold's end. Who wrote it is
    Khipu's to say, from this server's own environment and folder (the
    harness that launched it), never from the arguments."""
    if _via_https_gateway():
        return {"available": False, "reason": ROUTING_GATEWAY_REASON}
    _ensure_path()
    from khipu import routing

    # A write waits for the routing lock as long as a read does, then is
    # refused as busy (nothing written), never left hanging. An MCP call is
    # never a shell: an undetected harness is recorded as unknown.
    return routing.conversation_write(args, author=routing.conversation_author())


def _tool_route_rule(args: dict) -> dict:
    """Record a conversation's owner-requested rule through the trusted
    report author path; this tool never lets it speak as the owner."""
    if _via_https_gateway():
        return {"available": False, "reason": ROUTING_GATEWAY_REASON}
    _ensure_path()
    from khipu import routing

    if not isinstance(args, dict) or set(args) - {"text", "rule", "owner_request"}:
        raise ValueError("khipu_route_rule takes text, owner_request and optional rule")
    text = args.get("text")
    if not isinstance(text, str):
        raise ValueError("text must be text")
    owner_request = args.get("owner_request")
    if not isinstance(owner_request, str) or not owner_request.strip() or "\n" in owner_request or "\r" in owner_request:
        raise ValueError("owner_request must be one line of text")
    number = args.get("rule")
    if number is not None and (type(number) is not int or number < 1):
        raise ValueError("rule must be a positive integer")
    return routing.request_rule(text, n=number, owner_request=owner_request,
                                author=routing.conversation_author(), via="mcp")


def _tool_route_priority(args: dict) -> dict:
    """Record the owner's priority at his request through the conversation
    author path; it never speaks as the owner, and its answer names each
    account by its label and id, never a key or a login folder."""
    if _via_https_gateway():
        return {"available": False, "reason": ROUTING_GATEWAY_REASON}
    _ensure_path()
    from khipu import routing

    if not isinstance(args, dict) or set(args) - {"entries", "until", "owner_request"}:
        raise ValueError("khipu_route_priority takes entries, owner_request and optional until")
    entries = args.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("entries must be the owner's accounts, in his order")
    owner_request = args.get("owner_request")
    if not isinstance(owner_request, str) or not owner_request.strip() or "\n" in owner_request or "\r" in owner_request:
        raise ValueError("owner_request must be one line of text")
    until = args.get("until")
    if until is not None and not isinstance(until, str):
        raise ValueError("until must be text")
    # The version as of building this request, read inside the call's lock
    # budget: one that moves before the write lands refuses it, and the
    # request still waits for the owner.
    return routing.conversation_priority(entries, until=until, owner_request=owner_request,
                                         author=routing.conversation_author(), expect_current=True)


_LEDGER_VERBS = ("add", "set", "get", "list", "loose-ends", "needs-owner", "needs-matt", "render", "check",
                 "remove", "removed")
_LEDGER_TEXT = ("project", "id", "title", "kind", "parent", "state", "evidence", "owner", "note", "link", "if_state",
                "children", "op_id")


# Fields that name a row or a project: cutting a path in one would point at
# another, so a path there is refused instead. A link is not a path.
_LEDGER_NAMES = ("project", "id", "parent", "children", "if_state", "op_id", "link")
# A path at a word's start ("/x", "~/x", a backslash path, a drive letter)
# or a word holding two or more folder separators.
_LEDGER_PATH_START = r"^(?:~[/\\]|/(?=[\w.~-])|\\|[A-Za-z]:[/\\])"
_LEDGER_PATH = re.compile(_LEDGER_PATH_START + r"|[^/\\]+[/\\][^/\\]*[/\\]")


def _ledger_args(args: dict) -> dict:
    """The one check every text field of ``khipu_ledger`` passes before
    anything is written or queued (rule 4: titles and short plain words,
    never code, file contents, logs or private paths). Text only; nothing
    key-shaped; one line with no control characters and no code fence; in a
    field naming a row or project no word starting as a path; and in every
    other field each path-shaped word cut to its last name. Returns the
    fields as they will be saved."""
    from khipu import ledger

    for key in _LEDGER_TEXT:
        if args.get(key) is not None and not isinstance(args[key], str):
            raise ValueError(f"{key} must be text")
    ledger.refuse_secrets([args.get(k) for k in _LEDGER_TEXT])
    out = dict(args)
    for key in _LEDGER_TEXT:
        value = args.get(key)
        if value is None:
            continue
        if "```" in value or any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029" for c in value):
            raise ledger.LedgerError(f"{key} must be one line of plain words about the work: no code, file "
                                     "contents, command output or logs. Nothing was saved", "not_one_line")
        if key in _LEDGER_NAMES:
            if key != "link" and any(re.match(_LEDGER_PATH_START, w) for w in value.split()):
                raise ledger.LedgerError(f"{key} must not be a file path. Nothing was saved", "path")
        else:
            words = []
            for word in value.split(" "):
                if not word.startswith("https://") and _LEDGER_PATH.search(word):
                    word = re.split(r"[/\\]+", word.rstrip("/\\"))[-1] or word
                words.append(word)
            out[key] = " ".join(words)
    return out


def _tool_ledger(args: dict) -> dict:
    """``khipu ledger`` as a tool: the same verbs and fields, JSON back. A
    refusal is a tool error carrying the error and its reason word. Restore,
    purge and import are the owner's, in the app, so they are not verbs here."""
    verb = args.get("verb")
    if verb not in _LEDGER_VERBS:
        raise ValueError(f"verb must be one of: {', '.join(_LEDGER_VERBS)}")
    stale = args.get("stale_hours")
    if stale is not None and (isinstance(stale, bool) or not isinstance(stale, (int, float))):
        raise ValueError("stale_hours must be a number")
    _ensure_path()
    import argparse

    from khipu import ledger

    try:
        args = _ledger_args(args)
        ledger.require(f"ledger {verb}")
        project = ledger._project_for(argparse.Namespace(project=args.get("project") or None))
        row_id = args.get("id") or ""
        if verb in ("add", "set", "remove"):
            if verb == "add" and not (args.get("title") and args.get("kind")):
                raise ledger.LedgerError("add needs a title and a kind", "missing")
            if verb == "add":
                fields = {k: args.get(k) or "" for k in ("title", "kind", "parent", "evidence", "link")}
                fields.update(state=args.get("state"), owner=args.get("owner") or "none")
            elif verb == "set":
                fields = {k: args.get(k) for k in ("state", "evidence", "owner", "title", "link")}
            else:
                fields = {}
            op = ledger.new_op(verb, project, row_id, fields=fields, note=args.get("note") or "",
                               if_state=args.get("if_state") if verb == "set" else None,
                               children=args.get("children") if verb == "remove" else None,
                               op_id=args.get("op_id"), source="mcp")
            result = ledger.write(op)
            return {**result, "op_id": op["op_id"], "project": project}
        if verb == "get":
            got = ledger.get(project, row_id)
            out = {"ok": True, "row": ledger.row_json(got["row"], got["history"], project)}
            if got.get("offline"):
                out["offline_as_of"] = got.get("as_of")
            return out
        if verb == "removed":
            return {"ok": True, "project": project, "removed": ledger.removed_rows(project)}
        view = ledger.load(project)
        head = {"ok": True, "project": project, "offline": view["offline"], "as_of": view.get("as_of")}
        if verb == "render":
            return {**head, "board": ledger.render_board(view)}
        if verb == "check":
            return {**head, **ledger.check(view)}
        if verb == "list":
            rows, _text = ledger.render_list(view, state=args.get("state"), kind=args.get("kind"),
                                             owner=args.get("owner"), stale_hours=stale)
        elif verb == "loose-ends":
            rows, text = ledger.render_loose_ends(view, summary=bool(args.get("summary")))
            if args.get("summary"):
                return {**head, "summary": text}
        else:
            rows, _text = ledger.render_needs(view)
        return {**head, "rows": [ledger.row_json(r, [], project) for r in rows]}
    except ledger.LedgerError as exc:
        raise _ToolFailure({"ok": False, "error": str(exc), "reason": exc.reason}) from None


TOOL_FUNCS = {
    "khipu_search": _tool_search,
    "khipu_get": _tool_get,
    "khipu_graph": _tool_graph,
    "khipu_status": _tool_status,
    "khipu_capture": _tool_capture,
    "khipu_owed": _tool_owed,
    "khipu_owed_update": _tool_owed_update,
    "khipu_forget": _tool_forget,
    "khipu_decisions": _tool_decisions,
    "khipu_decisions_update": _tool_decisions_update,
    "khipu_brief": _tool_brief,
    "khipu_reflect": _tool_reflect,
    "khipu_usage": _tool_usage,
    "khipu_route": _tool_route,
    "khipu_route_report": _tool_route_report,
    "khipu_route_rule": _tool_route_rule,
    "khipu_route_priority": _tool_route_priority,
    "khipu_ledger": _tool_ledger,
}


def _result(req_id, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


# Tools whose whole answer an agent reads about this Mac's accounts and
# setup: scrubbed at the boundary (agent_view), over stdio and the gateway.
# Memory content (khipu_search, khipu_get, khipu_status's prior work) is
# never scrubbed: that would alter what was remembered.
_AGENT_SCRUBBED = frozenset({"khipu_usage", "khipu_route", "khipu_route_report", "khipu_route_rule",
                             "khipu_route_priority", "khipu_status"})
# The ledger's answers leave through agent_view.project (its stricter final
# scan: account keys and secrets as well as paths).
_AGENT_PROJECTED = frozenset({"khipu_ledger"})
_MEMORY_FIELDS = ("prior_work", "prior_work_text", "prior_work_meta")


def _scrub_for_agents(name: str, payload):
    from khipu import agent_view

    if not isinstance(payload, dict):
        return agent_view.scrub(payload)
    memory = {k: payload[k] for k in _MEMORY_FIELDS if k in payload}
    out = agent_view.scrub({k: v for k, v in payload.items() if k not in memory})
    out.update(memory)
    return out


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_text(payload: dict, *, is_error: bool = False) -> dict:
    # Non-ASCII stays literal: escaping made a CJK body six bytes per
    # character in this text and seven once the frame was dumped again.
    # Strict at this level too: a client parses this text as JSON in turn.
    from khipu import wire

    return {
        "content": [
            {"type": "text", "text": wire.dumps(payload, indent=2, default=str)}
        ],
        "isError": is_error,
    }


def encode_frame(message) -> bytes:
    """One JSON-RPC message as UTF-8 bytes, for the stdio writer and the gateway.

    Strict JSON (khipu.wire): no lone surrogate escape, U+2028 and U+2029
    escaped, so Foundation, serde_json and line-based readers all parse it."""
    from khipu import wire

    return wire.encode(message, default=str)


def _write_frame(message) -> None:
    # Bytes to the buffer, so the frame is UTF-8 whatever the locale says
    # stdout's encoding is (a C locale made it ASCII).
    out = sys.stdout
    buffer = getattr(out, "buffer", None)
    if buffer is None:
        out.write(encode_frame(message).decode("utf-8") + "\n")
        out.flush()
        return
    out.flush()
    buffer.write(encode_frame(message) + b"\n")
    buffer.flush()


# A stdio server lives as long as its client session, and a deploy (a
# fast-forward of the checkout) can land mid-session. Tool handlers import
# khipu lazily, so without this a call after a deploy loads a new module that
# imports names a module loaded before the deploy does not have (2026-10-08:
# khipu_capture failed with "cannot import name 'cache_thread'"). When any
# loaded khipu source has changed, every khipu module but this one is dropped
# so the next imports load one consistent version. Stdio only: the gateway is
# multi-threaded and restarts on deploy.
_RELOAD_ON_CHANGE = False
_SEEN_MTIMES: dict[str, float] = {}


def _khipu_modules() -> dict[str, object]:
    return {name: mod for name, mod in list(sys.modules.items())
            if name.startswith("khipu.") and name != __name__}


def _source_mtime(mod: object) -> float | None:
    try:
        return os.stat(mod.__file__).st_mtime
    except (AttributeError, TypeError, OSError):
        return None


def _note_loaded_modules() -> None:
    for name, mod in _khipu_modules().items():
        if name not in _SEEN_MTIMES:
            mtime = _source_mtime(mod)
            if mtime is not None:
                _SEEN_MTIMES[name] = mtime


def _drop_modules_if_code_changed() -> bool:
    mods = _khipu_modules()
    if not any(
        name in _SEEN_MTIMES and _source_mtime(mod) not in (None, _SEEN_MTIMES[name])
        for name, mod in mods.items()
    ):
        return False
    package = sys.modules.get("khipu")
    for name in mods:
        sys.modules.pop(name, None)
        # `from khipu import x` returns the package attribute when present.
        child = name.split(".", 1)[1]
        if package is not None and "." not in child and hasattr(package, child):
            with contextlib.suppress(AttributeError):
                delattr(package, child)
    _SEEN_MTIMES.clear()
    print("[khipu-mcp] khipu code changed on disk; reloading modules",
          file=sys.stderr, flush=True)
    return True


_MESSAGE_STATE = threading.local()


def take_message_error() -> str | None:
    error = getattr(_MESSAGE_STATE, "error", None)
    _MESSAGE_STATE.error = None
    return error


def _response_error(response: dict | None) -> str | None:
    if response is not None:
        if response.get("error"):
            return "protocol error"
        result = response.get("result")
        if isinstance(result, dict) and result.get("isError"):
            return "tool error"
    return None


def handle_message(msg) -> dict | None:
    """Validate before dispatch; valid notifications never emit responses."""
    _MESSAGE_STATE.error = None
    if (not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0"
            or not isinstance(msg.get("method"), str) or not msg["method"]
            or ("id" in msg and (isinstance(msg["id"], bool)
                                or not isinstance(msg["id"], (str, int, float, type(None)))))):
        _MESSAGE_STATE.error = "protocol error"
        return _error(None, -32600, "invalid request")
    req_id = msg.get("id")
    params = msg.get("params", {})
    error = None
    if not isinstance(params, dict):
        error = "params must be an object"
    elif msg["method"] == "initialize" and "protocolVersion" in params and not isinstance(params["protocolVersion"], str):
        error = "protocolVersion must be a string"
    elif msg["method"] == "tools/call":
        if not isinstance(params.get("name"), str):
            error = "tool name must be a string"
        elif not isinstance(params.get("arguments", {}), dict):
            error = "arguments must be an object"
    response = _error(req_id, -32602, error) if error else _dispatch_message(msg)
    _MESSAGE_STATE.error = _response_error(response)
    return response if "id" in msg else None


def _dispatch_message(msg: dict) -> dict | None:
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
        from khipu import features
        from khipu.recall_rule import RULE_MD

        return _result(
            req_id,
            {
                "protocolVersion": version,
                # The capability signal, under `experimental` (MCP
                # lifecycle spec, optional/vendor-namespaced) so a client can
                # test for a feature instead of inferring it. `tools` stays
                # exactly as it was.
                "capabilities": {"tools": {}, "experimental": {"khipu": features.contract()}},
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
        if _RELOAD_ON_CHANGE:
            _drop_modules_if_code_changed()
        from khipu import agent_view

        # Every error leaves through the tool's own pass: a projected tool's
        # through agent_view.project, whatever raised it.
        scrub = agent_view.project if name in _AGENT_PROJECTED else agent_view.scrub
        try:
            payload = func(params.get("arguments") or {})
            if name in _AGENT_SCRUBBED:
                payload = _scrub_for_agents(name, payload)
            elif name in _AGENT_PROJECTED:
                payload = agent_view.project(payload)
        except _ToolFailure as exc:
            return _result(req_id, _tool_text(scrub(exc.payload), is_error=True))
        except ValueError as exc:  # argument/mode rejections → tool error, not crash
            return _result(req_id, _tool_text(scrub({"error": str(exc)}), is_error=True))
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 — DB down, or SystemExit from
            # capture.load_payload (it exits EX_DATAERR on a malformed payload,
            # which inside a long-lived MCP server killed the whole process and
            # took every other client's session with it — audit 2026-09-04).
            # A tool failing is a tool error; only the transport may end us.
            # Its own text may name a file: fixed words for a filesystem
            # error, anything else scrubbed (agent_view.error_text).
            error = {"error": agent_view.error_text(exc)}
            return _result(req_id, _tool_text(scrub(error) if name in _AGENT_PROJECTED else error, is_error=True))
        finally:
            if _RELOAD_ON_CHANGE:
                _note_loaded_modules()
        return _result(req_id, _tool_text(payload))
    if is_notification:
        return None  # notifications/initialized, cancellations, etc.
    return _error(req_id, -32601, f"method not found: {method}")


def main() -> int:
    global _RELOAD_ON_CHANGE
    _RELOAD_ON_CHANGE = True
    _note_loaded_modules()
    print(f"[khipu-mcp] ready (pid {os.getpid()})", file=sys.stderr, flush=True)
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    while True:
        line = stream.readline(MAX_FRAME_BYTES + 1)
        if not line:
            break
        newline = b"\n" if isinstance(line, bytes) else "\n"
        size = len(line) if isinstance(line, bytes) else len(line.encode("utf-8"))
        if size > MAX_FRAME_BYTES:
            while line and not line.endswith(newline):
                line = stream.readline(MAX_FRAME_BYTES + 1)
            _write_frame(_error(None, -32700, "frame too large"))
            continue
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            _write_frame(_error(None, -32700, "parse error"))
            continue
        response = handle_message(msg)
        if response is not None:
            _write_frame(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
