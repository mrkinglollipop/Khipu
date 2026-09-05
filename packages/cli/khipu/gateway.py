"""Khipu gateway — the MCP server over HTTPS, for harnesses that cannot reach
Postgres (Grok Bot / Cursor cloud agents run on xAI's Linux VMs, outside the
private network).

Decision 2026-08-17 (maintainer decision, option B): Postgres stays private. This process runs
next to the database, behind a TLS-terminating reverse proxy, and exposes exactly the MCP
tools (`khipu_search`, `khipu_get`, `khipu_graph`, `khipu_status`, `khipu_capture`) that the
stdio server exposes locally — same `handle_message`, different transport. The
cloud agent needs nothing installed: no Python libs, no DSN, no Gemini key; a
URL and a bearer token in its `.cursor/mcp.json`.

Transport: MCP *Streamable HTTP* — `POST /mcp` with a JSON-RPC message (or
batch), JSON response; notifications get 202 with no body; `GET /mcp` is 405
(no server-initiated stream — the spec allows that). Stateless: no session ids.

Security model (this is the only thing on the public internet):
  * bearer token, compared in constant time; missing/wrong → 401, no detail
  * body cap 1 MiB → 413; per-IP token bucket → 429; every request logged
    (ip, method, path, tool name, status, ms) — never arguments or results
  * capture is only accepted in `hub` mode (the tool itself enforces it), and
    on the gateway host the file mirror is off (`KHIPU_HUB_FILE_MIRROR=0`): a cloud
    capture is a PG row + vector, nothing else
  * `GET /healthz` is unauthenticated and touches nothing but the process
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from khipu.mcp_server import (
    SERVER_NAME,
    SERVER_VERSION,
    handle_message,
    mark_gateway_active,
)

MAX_BODY = 1024 * 1024
RATE_PER_MIN = int(os.environ.get("KHIPU_GATEWAY_RATE_PER_MIN", "120"))
AUTH_FAIL_PER_MIN = int(os.environ.get("KHIPU_GATEWAY_AUTH_FAIL_PER_MIN", "10"))
# A JSON-RPC batch is one request but N pieces of work. Unbounded, a single
# 1 MiB body holds ~9,900 tool calls — each one a Postgres query — for the price
# of one rate-limit token, which turned the limiter into a suggestion (audit
# 2026-08-17). Both halves of the fix matter: cap the batch, and charge the
# limiter per message rather than per request.
MAX_BATCH = int(os.environ.get("KHIPU_GATEWAY_MAX_BATCH", "32"))
# The limiter keeps a deque per client key and this is a long-lived public
# daemon, so without pruning the map grows for every address ever seen.
RATE_KEYS_MAX = int(os.environ.get("KHIPU_GATEWAY_RATE_KEYS_MAX", "4096"))
# The commit this container was built from, stamped in by deploy_gateway.sh.
# /healthz used to report only SERVER_VERSION, a constant that had read 0.1.0
# since the module was written — so the one unauthenticated endpoint could not
# answer the question that actually matters, "is the deployed fix live?". The
# gateway ran the commit before its own batch-cap fix for hours and nothing
# short of SSH could tell (audit 2026-08-18).
BUILD = os.environ.get("KHIPU_BUILD", "unknown")


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [khipu-gateway] {msg}", file=sys.stderr, flush=True)


class _Rate:
    def __init__(self, per_min: int) -> None:
        self.per_min = per_min
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> bool:
        """Take `cost` tokens for `key`. An over-budget call takes nothing, so a
        rejected batch does not also starve the caller's next real request."""
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) + cost > self.per_min:
                self._prune(now)
                return False
            q.extend([now] * cost)
            self._prune(now)
            return True

    def _prune(self, now: float) -> None:
        """Drop keys with no live hits. Called under the lock."""
        if len(self._hits) <= RATE_KEYS_MAX:
            dead = [k for k, v in self._hits.items() if not v or now - v[-1] > 60]
            for k in dead:
                del self._hits[k]
            return
        for k in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
            del self._hits[k]
        # Still over the cap after dropping the idle ones: the map itself is the
        # attack. Keep the most recently active and let the rest re-register.
        if len(self._hits) > RATE_KEYS_MAX:
            keep = sorted(self._hits.items(), key=lambda kv: kv[1][-1], reverse=True)[:RATE_KEYS_MAX]
            self._hits.clear()
            self._hits.update(dict(keep))


class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}-gateway/{SERVER_VERSION}"
    token: str = ""
    rate: _Rate = _Rate(RATE_PER_MIN)
    # Failed auth gets its own, much tighter budget. The rate check runs BEFORE
    # the token check (audit 2026-08-17: it ran after, so unauthenticated
    # attempts — the ones worth throttling — were not limited at all).
    auth_fail_rate: _Rate = _Rate(AUTH_FAIL_PER_MIN)

    # ---- plumbing --------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # silence default access log
        return

    def _client(self) -> str:
        """The real caller, for rate limiting and the log line.

        X-Forwarded-For is a client-supplied header that our own proxy APPENDS
        to, so the trustworthy hop is the RIGHTMOST entry (added by Caddy), not
        the leftmost. Audit 2026-08-17 proved the leftmost read let one caller
        bypass the rate limit entirely by varying the header per request."""
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):          # only our own reverse proxy may assert XFF
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                hops = [h.strip() for h in xff.split(",") if h.strip()]
                if hops:
                    return hops[-1]
        return peer

    def _send(self, status: int, body: bytes | None = None, ctype: str = "application/json") -> None:
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:].strip(), self.token)

    # ---- routes ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, json.dumps({"ok": True, "server": SERVER_NAME,
                                        "version": SERVER_VERSION, "build": BUILD}).encode())
            return
        if self.path.rstrip("/") == "/mcp":
            self._send(405)  # no server-initiated SSE stream; allowed by the spec
            return
        self._send(404)

    def do_DELETE(self) -> None:  # noqa: N802
        self._send(405)

    def do_POST(self) -> None:  # noqa: N802
        t0 = time.time()
        ip = self._client()
        if self.path.rstrip("/") != "/mcp":
            self._send(404)
            return
        if not self.rate.allow(ip):
            self._send(429)
            _log(f"{ip} POST /mcp 429")
            return
        if not self.token or not self._authorized():
            # Throttle the attempts worth throttling: a caller that keeps
            # failing auth is spending our budget and our log, not doing work.
            if not self.auth_fail_rate.allow(ip):
                self._send(429)
                _log(f"{ip} POST /mcp 429 (auth failures)")
                return
            self._send(401)
            _log(f"{ip} POST /mcp 401")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._send(413 if n > MAX_BODY else 400)
            return
        raw = self.rfile.read(n)
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}}).encode())
            return
        msgs = msg if isinstance(msg, list) else [msg]
        if not all(isinstance(m, dict) for m in msgs) or not msgs:
            self._send(400)
            return
        if len(msgs) > MAX_BATCH:
            self._send(413, json.dumps({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32600,
                                                  "message": f"batch too large (max {MAX_BATCH})"}}).encode())
            _log(f"{ip} POST /mcp 413 (batch {len(msgs)} > {MAX_BATCH})")
            return
        # Charge the rest of the batch: the first message was paid for above.
        if len(msgs) > 1 and not self.rate.allow(ip, cost=len(msgs) - 1):
            self._send(429)
            _log(f"{ip} POST /mcp 429 (batch of {len(msgs)} over budget)")
            return
        tool = ""
        responses = []
        for m in msgs:
            if m.get("method") == "tools/call":
                tool = str((m.get("params") or {}).get("name") or "")
            try:
                r = handle_message(m)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:  # noqa: BLE001 — never let one message kill
                # the server. BaseException, not Exception: capture.load_payload
                # raises SystemExit on a malformed payload, which used to unwind
                # straight through the request thread (audit 2026-09-04).
                detail = (f"SystemExit: capture rejected the payload (exit {exc.code})"
                          if isinstance(exc, SystemExit) else f"{type(exc).__name__}: {exc}")
                r = {"jsonrpc": "2.0", "id": m.get("id"),
                     "error": {"code": -32603, "message": detail}}
            if r is not None:
                responses.append(r)
        if not responses:  # only notifications
            self._send(202)
            status = 202
        else:
            body = responses if isinstance(msg, list) else responses[0]
            self._send(200, json.dumps(body, default=str).encode("utf-8"))
            status = 200
        _log(f"{ip} POST /mcp {status} {int((time.time() - t0) * 1000)}ms"
             f"{' tool=' + tool if tool else ''} n={len(msgs)}")


def serve(bind: str = "127.0.0.1:8787", token: str | None = None) -> None:
    token = token if token is not None else os.environ.get("KHIPU_GATEWAY_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("refusing to start: KHIPU_GATEWAY_TOKEN missing or shorter than 24 chars")
    # Tell mcp_server this process IS the gateway. The old detector walked the
    # call stack for a "khipu.gateway" frame, which never matches in the
    # container (`python -m khipu.gateway` → __name__ == "__main__"), so every
    # gateway capture was rejected as a local double-write (audit 2026-09-04).
    mark_gateway_active()
    host, _, port = bind.rpartition(":")
    Handler.token = token
    Handler.rate = _Rate(RATE_PER_MIN)
    Handler.auth_fail_rate = _Rate(AUTH_FAIL_PER_MIN)
    httpd = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    httpd.daemon_threads = True
    _log(f"listening on {host or '127.0.0.1'}:{port} (pid {os.getpid()}, capture_mode={os.environ.get('KHIPU_CAPTURE_MODE', 'default')})")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="khipu gateway", description=__doc__.split("\n\n")[0])
    ap.add_argument("--bind", default=os.environ.get("KHIPU_GATEWAY_BIND", "127.0.0.1:8787"))
    args = ap.parse_args(argv)
    serve(args.bind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
