"""khipu-mcp — stdio MCP server (server id: ``khipu``).

Reads (search, graph, status) are allowed in every ``capture_mode``.
``khipu_capture`` is a writer: the HTTPS gateway and no-hook hub installs may
write; the local stdio server declines when ``khipu-stop-hook`` /
``khipu-aegis-capture`` is the writer (double-capture). ``legacy``/``dual``
always reject. The ``khipu capture`` CLI is a separate entrypoint and is not
gated here.

Tools:

  - ``khipu_search``  — per-kind-fair ILIKE search over topics/episodes/nodes
  - ``khipu_graph``   — undirected neighborhood walk from a node id
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

import json
import os
import sys
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


def _via_https_gateway() -> bool:
    """True when this tools/call arrived through ``khipu.gateway`` (HTTPS)."""
    try:
        frame = sys._getframe()
    except (AttributeError, ValueError):
        return False
    while frame is not None:
        if frame.f_globals.get("__name__") == "khipu.gateway":
            return True
        frame = frame.f_back
    return False


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
            "Search Khipu memory. Default: deterministic per-kind-fair ILIKE over "
            "topics/episodes/graph nodes. semantic=true: cosine top-k over the "
            "active embedding profile (episodes + topics), with a score. Returns "
            "JSON rows of {kind, id, label, snippet[, score]} plus additive "
            "paths (filesystem tokens) and neighbors (capped 1-hop wiki/path "
            "edges) on topic hits. Tombstoned topics are excluded."
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
                "semantic": {
                    "type": "boolean",
                    "description": "Cosine search over embeddings instead of ILIKE (default false)",
                },
                "kind": {
                    "type": "string",
                    "description": "Semantic only: restrict to 'episode' or 'topic'",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "khipu_graph",
        "description": (
            "Neighborhood of a graph node id (undirected). hops=1 returns "
            "direct edges; hops>=2 walks a recursive undirected CTE ordered "
            "hops-then-node. Topic slugs from search expand as "
            "{slug, topic:slug, memory_topic:slug}."
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
            "file-vs-PG drift sample (slower: walks the memory root)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_drift": {"type": "boolean", "description": "Default false"},
            },
        },
    },
    {
        "name": "khipu_capture",
        "description": (
            "Remember this session in Khipu. Cloud / no capture hook: CALL THIS "
            "when you finish a substantive piece of work — a decision, a fix, a "
            "finding worth recalling later — with a 1-3 sentence summary, short "
            "lowercase topic slugs, and any decisions/preferences; set "
            "session_id to '<harness>:<stable id>' (e.g. "
            "'grokbot:<repo>:<task>'). Local Mac with khipu-stop-hook or "
            "khipu-aegis-capture: do NOT call this even in capture_mode=hub — "
            "the hook is the writer and an MCP write double-captures. Dual: "
            "this tool declines and says so — expected, the hook already has "
            "it. Through the HTTPS gateway it is the ONLY way the session is "
            "remembered, so do not skip it there."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Episode summary (required)"},
                "topics": {"type": "array", "items": {"type": "string"}},
                "people": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "preferences": {"type": "array", "items": {"type": "string"}},
                "scope": {
                    "type": "string",
                    "description": "general | trivial (trivial is skipped per protocol)",
                },
                "session_id": {"type": "string"},
            },
            "required": ["summary"],
        },
    },
]


def _tool_search(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = min(int(args.get("limit") or SEARCH_LIMIT_DEFAULT), SEARCH_LIMIT_MAX)
    _ensure_path()
    if args.get("semantic"):
        from khipu.embed import semantic_search

        kind = args.get("kind") or None
        if kind not in (None, "episode", "topic"):
            raise ValueError("kind must be 'episode' or 'topic'")
        from khipu.db import connect
        from khipu.topic_graph import enrich_search_results

        results = semantic_search(query, limit=max(1, limit), kind=kind)
        with connect() as conn:
            with conn.cursor() as cur:
                results = enrich_search_results(cur, results)
        return {"query": query, "mode": "semantic", "results": results}
    from khipu.cli import _search_query
    from khipu.db import connect
    from khipu.topic_graph import enrich_search_results

    with connect() as conn:
        with conn.cursor() as cur:
            return {
                "query": query,
                "mode": "ilike",
                "results": enrich_search_results(
                    cur, _search_query(cur, query, max(1, limit))
                ),
            }


def _tool_graph(args: dict) -> dict:
    node_id = (args.get("id") or "").strip()
    if not node_id:
        raise ValueError("id is required")
    hops = min(max(1, int(args.get("hops") or 1)), GRAPH_HOPS_MAX)
    limit = min(max(1, int(args.get("limit") or GRAPH_LIMIT_DEFAULT)), GRAPH_LIMIT_MAX)
    _ensure_path()
    from khipu.cli import _graph_query
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            return _graph_query(cur, node_id, hops, limit)


def _tool_status(args: dict) -> dict:
    _ensure_path()
    from khipu.drift import status_payload

    if args.get("include_drift"):
        return status_payload(_default_memory_root(), include_drift=True)
    return status_payload(None)


def _tool_capture(args: dict) -> dict:
    # Locked semantics (agent-integration note, "MCP + CLI write / read
    # semantics"): in legacy/dual the harness's shell hook is the writer, so an
    # MCP write here would double-capture — reject with a clear pointer. In hub
    # the HTTPS gateway (and no-hook installs) may write; local stdio with
    # khipu-stop-hook / khipu-aegis-capture must still decline. The CLI
    # ``khipu capture`` does not go through this function.
    mode = _capture_mode()
    if mode != "hub":
        raise ValueError(
            f"khipu_capture is rejected in capture_mode={mode}: in this mode the "
            "harness capture hook (khipu capture / capture_v2.py) is the writer, "
            "and an MCP write would double-capture. Set capture_mode=hub to "
            "write through MCP."
        )
    if _stdio_hook_owns_capture():
        raise ValueError(
            "khipu_capture is rejected on the local stdio MCP server: "
            "khipu-stop-hook / khipu-aegis-capture is the writer and an MCP "
            "write would double-capture. The hook already has this session. "
            "Cloud agents write through the HTTPS gateway, which is allowed."
        )
    from khipu.capture import capture, load_payload

    # load_payload validates + mints ts; capture() routes by mode (hub → PG only).
    payload = load_payload(json.dumps({k: v for k, v in args.items() if v is not None}))
    rc = capture(payload, mode="hub")
    if rc != 0:
        raise ValueError(f"khipu capture exited {rc}")
    return {"ok": True, "mode": mode, "ts": payload["ts"]}


TOOL_FUNCS = {
    "khipu_search": _tool_search,
    "khipu_graph": _tool_graph,
    "khipu_status": _tool_status,
    "khipu_capture": _tool_capture,
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
        except Exception as exc:  # noqa: BLE001 — DB down etc.; server must stay up
            return _result(
                req_id,
                _tool_text({"error": f"{type(exc).__name__}: {exc}"}, is_error=True),
            )
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
