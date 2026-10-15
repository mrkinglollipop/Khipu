# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.recall_daemon — a warm process behind the per-prompt recall hook.

Every prompt used to start a new Python process, import the package, open a
new TLS connection to the embedding API, read the key, ask git for the project
and load the replica's vectors from disk: 750 to 1,000 ms against a 950 ms
internal deadline, and a timeout on a busy machine. This service pays those
costs once and answers over a Unix socket; ``bin/khipu-prompt-recall`` asks it
first and runs the one-shot path when it is not there.

Protocol: one JSON object per line each way, on ``<khipu_home>/recall.sock``.

  {"op": "recall", "raw": "<the hook's stdin payload>", "shape": "claude"|"cursor"}
      with protocol=1, answers {"ok": true, "output": <hook output>} or
      {"ok": false, "error": ...}; a successful client sends {"op": "ack"}
  {"op": "ping"}
      answers {"ok", "version", "pid", "started", "code_stamp", "served"}
  anything else answers {"error": "..."}

Routing (phase 3b and 3c) lives in a helper this service spawns once
(``recall_prompt.RoutingForkServer``): it forks each prompt's routing child,
and between prompts a poll child that refolds the routing record when a
source or the log changed or the clock reached the view's ``valid_until``.
This process never forks itself and never folds on its own threads.

The service never dies from a request: handler failures answer explicit errors.
It does exit on purpose when the package's code changes on disk (a ``git pull``
landed), so launchd starts the new code instead of the process serving old
code indefinitely.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import io
import json
import math
import os
import signal
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from khipu import __version__

SOCKET_NAME = "recall.sock"
# How often, at most, the service looks at the package's files again.
STALE_RECHECK_S = 5.0
# A prompt payload is a few kilobytes; this only bounds a broken client.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
# A client that connects and says nothing must not hold a thread forever.
CONN_READ_TIMEOUT_S = 10.0
# The client tries the one-shot path after a failed answer only while this
# much of its deadline is left (bin/khipu-recall-client.py).
CLIENT_COLD_RETRY_S = 1.4
# In-flight requests get this long to finish once the service is stopping.
DRAIN_S = 3.0
_ACCEPT_TICK_S = 0.5
# How often the idle service reads its routing switches (``keep_routing``):
# a push switched off stops the routing helper, and its poll, within this.
_ROUTING_LOOK_S = 2.0
# A helper that a prompt wanted this recently (ready or still starting) is
# kept even with this service's push off (that prompt's environment had it
# on); one still starting is never stopped. Its poll follows its own switches.
_ROUTING_IDLE_STOP_S = 60.0
# sockaddr_un.sun_path is 104 bytes on macOS, 108 on Linux; stay under both.
_SUN_PATH_MAX = 100
_SHAPES = ("claude", "cursor")


def _log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} [khipu-recall-daemon] {msg}", file=sys.stderr, flush=True)


def socket_path() -> Path:
    from khipu.session_capture import khipu_home

    return khipu_home() / SOCKET_NAME


def code_stamp_parts(root: Path | None = None) -> tuple[int, int]:
    """(newest modification time in ns, file count) over a package's .py
    files, this one's by default. The count is there so a deleted file, which
    leaves the newest time alone, still changes the stamp."""
    root = Path(__file__).resolve().parent if root is None else Path(root)
    newest = 0
    count = 0
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        try:
            newest = max(newest, p.stat().st_mtime_ns)
        except OSError:
            continue
        count += 1
    return newest, count


def code_stamp(root: Path | None = None) -> str:
    root = Path(__file__).resolve().parent if root is None else Path(root)
    newest, count = code_stamp_parts(root)
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" not in p.parts:
            digest.update(str(p.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(p.read_bytes())
            digest.update(b"\0")
    return f"{newest}:{count}:{digest.hexdigest()}"


# ---- the embedding connection ------------------------------------------------


class KeepAliveTransport:
    """An ``embed`` transport that keeps one idle HTTPS connection per host.

    Same contract as the urllib one it replaces: returns the body, raises
    ``urllib.error.HTTPError`` for a non-2xx status and ``URLError``/``OSError``
    for a network failure, so ``embed_batch``'s retry ladder cannot tell them
    apart. A connection the server closed while it sat idle is replaced by a
    fresh one, once, inside the same call."""

    def __init__(self, *, connect: Callable[..., Any] | None = None) -> None:
        self._ctx: ssl.SSLContext | None = None
        self._connect = connect or self._https
        self._idle: dict[tuple[str, int], Any] = {}
        self._lock = threading.Lock()

    def _https(self, host: str, port: int, timeout: float) -> http.client.HTTPSConnection:
        # Building the context loads the system's CA bundle; do it once.
        if self._ctx is None:
            self._ctx = ssl.create_default_context()
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._ctx)

    def _take(self, key: tuple[str, int]) -> Any:
        with self._lock:
            return self._idle.pop(key, None)

    def _give_back(self, key: tuple[str, int], conn: Any) -> None:
        with self._lock:
            spare = self._idle.get(key)
            if spare is None:
                self._idle[key] = conn
                return
        conn.close()

    def close(self) -> None:
        with self._lock:
            conns, self._idle = list(self._idle.values()), {}
        for conn in conns:
            conn.close()

    def send(self, url: str, data: bytes, headers: dict[str, str], timeout: float) -> bytes:
        parts = urllib.parse.urlsplit(url)
        key = (parts.hostname or "", parts.port or 443)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        conn = self._take(key)
        reused = conn is not None
        while True:
            if conn is None:
                conn = self._connect(key[0], key[1], timeout)
            try:
                conn.timeout = timeout
                if getattr(conn, "sock", None) is not None:
                    conn.sock.settimeout(timeout)
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                status, reason, resp_headers = resp.status, resp.reason, resp.msg
                closing = resp.will_close
            except (http.client.HTTPException, OSError) as exc:
                conn.close()
                conn = None
                # A timeout is the server being slow, not a dead connection:
                # a second try would spend the caller's budget twice.
                if reused and not isinstance(exc, TimeoutError):
                    reused = False
                    continue
                if isinstance(exc, http.client.HTTPException):
                    raise urllib.error.URLError(exc) from exc
                raise
            if closing:
                conn.close()
            else:
                self._give_back(key, conn)
            if status >= 300:
                raise urllib.error.HTTPError(url, status, reason, resp_headers, io.BytesIO(body))
            return body


# ---- the service --------------------------------------------------------------


class _Refused(Exception):
    """The service is stopping and will not serve this request."""


class RecallService:
    def __init__(
        self,
        path: Path,
        *,
        stamp_fn: Callable[[], str] = code_stamp,
        recheck_s: float = STALE_RECHECK_S,
        clock: Callable[[], float] = time.monotonic,
        idle_check: bool = True,
        routing_forkserver: bool = True,
    ) -> None:
        self.path = Path(path)
        # The per-prompt routing child is forked by a small helper this
        # service spawns (``start_routing`` at start when the push is on,
        # else on a thread at the first prompt with the push on), never by
        # this process (its heap is the vector matrix: a fork of it slowed
        # the search). False forks from this process, as the one-shot hook
        # does: a test seam, so a test's patched clock and fold reach the read.
        from khipu import recall_prompt

        self.routing_forkserver = recall_prompt.RoutingForkServer() if routing_forkserver else None
        self._routing_on: bool | None = None
        self._routing_look_at = 0.0
        self._stamp_fn = stamp_fn
        self._recheck_s = recheck_s
        self._clock = clock
        # An idle service also looks at the disk, so the first prompt after a
        # pull does not run on old code. A test that needs the request to be
        # the one that notices turns this off.
        self._idle_check = idle_check
        self._watching = False
        self.stamp = stamp_fn()
        self.started = datetime.now(timezone.utc).isoformat()
        self.served = 0
        self._lock = threading.Lock()
        self._last_check = clock()
        self._stopping = threading.Event()
        self._listener: socket.socket | None = None
        self._ino: int | None = None
        self._threads: list[threading.Thread] = []

    # -- socket ownership --

    def _probe_live(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(self.path))
            return True
        except socket.timeout:
            return True  # a full backlog is a busy server, not a dead one
        except OSError:
            return False
        finally:
            probe.close()

    def bind(self) -> bool:
        """Take the socket. False when a live instance already holds it;
        raises when it cannot be taken safely (foreign directory, a file that
        is not a socket, a path too long for the OS)."""
        d = self.path.parent
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = d.stat()
        if st.st_uid != os.getuid():
            raise RuntimeError(f"{d} is not owned by this user")
        if st.st_mode & 0o022:
            raise RuntimeError(f"{d} is writable by other users")
        if len(os.fsencode(str(self.path))) >= _SUN_PATH_MAX:
            raise RuntimeError(f"socket path too long: {self.path}")
        try:
            existing = os.lstat(self.path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise RuntimeError(f"{self.path} exists and is not a socket")
            if self._probe_live():
                return False
            os.unlink(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE and self._probe_live():
                return False  # another instance won the race for it
            raise
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        sock.listen(64)
        sock.settimeout(_ACCEPT_TICK_S)
        self._listener = sock
        self._ino = os.stat(self.path).st_ino
        return True

    def start_routing(self) -> bool:
        """Start the routing helper now, on a thread, when this service's own
        switches have the per-prompt push on, so the first prompt never waits
        for it (a prompt with the push on starts it otherwise). Never waits;
        True when it was asked to start."""
        if self.routing_forkserver is None:
            return False
        from khipu import recall_prompt

        if not recall_prompt.daemon_routing_on():
            return False
        self.routing_forkserver.prepare()
        return True

    def keep_routing(self) -> bool:
        """While this service's switches have the per-prompt push on, start
        the routing helper again if it died after it started, so the record
        keeps being refolded between prompts (phase 3c: the helper polls the
        sources and the log and refolds at the view's ``valid_until``; the
        refold runs in a child of the helper, never on this process's GIL).
        With the push off (the feature, the bypass or ``routing_push_prompt``)
        it stops the helper: nothing polls. The switches are read at most
        every ``_ROUTING_LOOK_S``. Never waits; True when the helper was
        asked to start again."""
        if self.routing_forkserver is None or self._stopping.is_set():
            return False
        now = time.monotonic()
        if self._routing_on is None or now >= self._routing_look_at:
            from khipu import recall_prompt

            self._routing_on = recall_prompt.daemon_routing_on()
            self._routing_look_at = now + _ROUTING_LOOK_S
        if not self._routing_on:
            self.routing_forkserver.stop(idle_for=_ROUTING_IDLE_STOP_S)
            return False
        return self.routing_forkserver.keep_alive()

    def close(self) -> None:
        if self.routing_forkserver is not None:
            self.routing_forkserver.close()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        try:
            # Only our own socket: a successor may already have replaced it.
            if self._ino is not None and os.lstat(self.path).st_ino == self._ino:
                os.unlink(self.path)
        except OSError:
            pass

    # -- staleness --

    def _detect_stale(self) -> bool:
        """True once the package's code has changed since this process
        started. Looks at the disk at most once per ``recheck_s``. The caller
        uses a single background watcher in a running service."""
        if self._stopping.is_set():
            return False
        now = self._clock()
        if now - self._last_check < self._recheck_s:
            return False
        self._last_check = now
        try:
            changed = self._stamp_fn() != self.stamp
        except Exception as exc:  # noqa: BLE001 — a failed look is not a reason to stop
            _log(f"code stamp unreadable: {type(exc).__name__}: {exc}")
            return False
        if changed:
            _log("code changed on disk; stopping so launchd starts the new code")
            self._stopping.set()
        return changed

    def _admit(self, *, refuse_stale: bool = True) -> bool:
        """Refuse recall as soon as stale code is detected. Package I/O runs
        on the watcher, outside the admission lock and prompt deadline."""
        with self._lock:
            if self._stopping.is_set():
                raise _Refused()
            if not self._watching and self._detect_stale():
                if refuse_stale:
                    raise _Refused()
                return True
            return False

    def stop(self) -> None:
        self._stopping.set()

    # -- requests --

    def _recall(self, req: dict[str, Any], delivery: dict | None = None) -> dict[str, Any]:
        raw = req.get("raw", "")
        shape = req.get("shape", "claude")
        if not isinstance(raw, str):
            return {"ok": False, "error": "raw must be a string"}
        if shape not in _SHAPES:
            return {"ok": False, "error": f"shape must be one of {list(_SHAPES)}"}
        try:
            from khipu import recall_prompt

            options = {}
            if req.get("protocol") == 1:
                deadline = req.get("deadline", time.monotonic() + recall_prompt.TIMEOUT_S)
                if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
                    raise ValueError("invalid request deadline")
                if time.monotonic() >= deadline:
                    return {"ok": False, "error": "timeout>request deadline"}
                options = {"deadline": deadline, "delivery": delivery}
            # Routing's switches as the hook's own environment set them, not this service's.
            environ = recall_prompt.caller_environ(req.get("env"))
            if environ is not None:
                options["environ"] = environ
            if self.routing_forkserver is not None:
                options["routing_forkserver"] = self.routing_forkserver
            out = recall_prompt.hook_main(raw, shape=shape, **options)
            if not isinstance(out, dict):
                raise TypeError("recall handler returned a non-object")
        except Exception as exc:  # noqa: BLE001 — the one thing a request must never do is kill us
            _log(f"recall handler raised: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"handler failed: {type(exc).__name__}"}
        if delivery is not None:
            result = delivery.get("result") or {}
            if recall_prompt._outcome_for(result.get("reason", "ok"), result.get("hits", [])) in ("error", "timeout"):
                # A failed search answers an error, so the client can still try
                # the one-shot path. When it no longer can (too little time
                # left), routing lines this prompt carries are delivered on
                # their own (phase 3b): the search's outcome is logged as it was.
                routed = (delivery.get("routing") or {}).get("pending")
                left = options.get("deadline", 0) - time.monotonic()
                if not routed or left >= CLIENT_COLD_RETRY_S:
                    return {"ok": False, "error": result["reason"]}
        with self._lock:
            self.served += 1
        return {"ok": True, "output": out} if req.get("protocol") == 1 else out

    def dispatch(self, req: Any, delivery: dict | None = None) -> dict[str, Any]:
        if not isinstance(req, dict):
            return {"error": "request must be a JSON object"}
        op = req.get("op")
        if op == "ping":
            with self._lock:
                served = self.served
            return {
                "ok": True, "version": __version__, "pid": os.getpid(),
                "started": self.started, "code_stamp": self.stamp, "served": served,
                "root": str(Path(__file__).resolve().parent),
            }
        if op == "recall":
            return self._recall(req, delivery)
        return {"error": f"unknown op: {op!r}"}

    def _answer(self, line: bytes, delivery: dict | None = None) -> dict[str, Any]:
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"error": "malformed request"}
        raw = req.get("raw", "") if isinstance(req, dict) else ""
        raw = raw if isinstance(raw, str) else ""
        try:
            last = self._admit(refuse_stale=isinstance(req, dict) and req.get("op") == "recall")
        except _Refused:
            from khipu import recall_prompt
            recall_prompt.log_terminal_outcome(raw, "error: daemon restarting")
            return {"ok": False, "error": "restarting"}
        try:
            answer = self.dispatch(req, delivery)
            if answer.get("ok") is False and not (delivery or {}).get("result"):
                from khipu import recall_prompt
                reason = answer["error"]
                if not reason.startswith(("error:", "timeout>")):
                    reason = f"error: {reason}"
                recall_prompt.log_terminal_outcome(raw, reason)
            return answer
        except Exception as exc:  # noqa: BLE001 — dispatch handles its own; this is the backstop
            _log(f"request raised: {type(exc).__name__}: {exc}")
            from khipu import recall_prompt
            recall_prompt.log_terminal_outcome(raw, f"error: dispatch crashed: {type(exc).__name__}")
            return {"ok": False, "error": "dispatch failed"}
        finally:
            if last:
                self._wake()

    def _wake(self) -> None:
        """Nudge the accept loop so it notices it is stopping now rather than
        at its next tick."""
        try:
            nudge = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            nudge.settimeout(0.2)
            nudge.connect(str(self.path))
            nudge.close()
        except OSError:
            pass

    def _serve_conn(self, conn: socket.socket) -> None:
        delivery: dict = {}
        try:
            conn.settimeout(CONN_READ_TIMEOUT_S)
            buf = b""
            while True:
                while b"\n" not in buf:
                    if len(buf) > MAX_REQUEST_BYTES:
                        conn.sendall(b'{"error": "request too large"}\n')
                        return
                    chunk = conn.recv(65536)
                    if not chunk:
                        if buf.strip():
                            conn.sendall(json.dumps(self._answer(buf)).encode("utf-8") + b"\n")
                        return
                    buf += chunk
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                delivery = {}
                answer = self._answer(line, delivery)
                conn.sendall(json.dumps(answer).encode("utf-8") + b"\n")
                if delivery:
                    from khipu import recall_prompt
                    deadline = json.loads(line).get("deadline", time.monotonic() + 0.2)
                    delivered = False
                    if answer.get("ok"):
                        conn.settimeout(max(0.001, deadline - time.monotonic()))
                        while b"\n" not in buf and len(buf) <= MAX_REQUEST_BYTES:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            buf += chunk
                        if b"\n" in buf:
                            ack, buf = buf.split(b"\n", 1)
                            delivered = json.loads(ack) == {"op": "ack"} and time.monotonic() <= deadline
                    recall_prompt.finish_delivery(
                        delivery, delivered=delivered,
                        reason=None if delivered else answer.get("error", "timeout>delivery deadline"),
                    )
        except OSError:
            pass  # the client went away; nothing to answer
        except Exception as exc:  # noqa: BLE001
            _log(f"connection raised: {type(exc).__name__}: {exc}")
        finally:
            if delivery:
                from khipu import recall_prompt
                recall_prompt.finish_delivery(delivery, delivered=False, reason="timeout>delivery deadline")
            conn.close()

    def run(self) -> int:
        """Accept until stopped; returns the process exit code (always 0)."""
        listener = self._listener
        if listener is None:
            raise RuntimeError("bind() first")
        if self._idle_check:
            self._watching = True
            def watch_code():
                while not self._stopping.wait(_ACCEPT_TICK_S):
                    self._detect_stale()
                    self.keep_routing()
            threading.Thread(target=watch_code, daemon=True).start()
        while not self._stopping.is_set():
            try:
                conn, _addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    break
                time.sleep(0.05)
                continue
            t = threading.Thread(target=self._serve_conn, args=(conn,), daemon=True)
            self._threads = [x for x in self._threads if x.is_alive()]
            self._threads.append(t)
            t.start()
        self.close()
        deadline = time.monotonic() + DRAIN_S
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return 0


def _warm_imports() -> None:
    """Pay the package's import cost at start, not on the first prompt."""
    try:
        from khipu import embed, hub_snapshot, recall_prompt, vector_scan  # noqa: F401
        # Routing's switches are decided here for every prompt (the child
        # itself runs in the routing helper, which imports what it reads).
        from khipu import routing

        routing.prompt_imports()
    except Exception as exc:  # noqa: BLE001 — the first prompt will import them anyway
        _log(f"warm imports skipped: {type(exc).__name__}: {exc}")


def _warm_caches() -> None:
    """Load the replica's vector matrix and the topic slug map now, on a thread.
    The service restarts on every code change, and a cold matrix load (about a
    second, competing with the lexical scan for the same process) cost the
    first prompts after each restart their whole budget (2026-10-12)."""
    try:
        from khipu import graph_candidates, hub_snapshot

        profile = hub_snapshot.active_snapshot_profile()
        if profile:
            hub_snapshot._matrix_rows(hub_snapshot.snapshot_path(), profile, None)
        graph_candidates._episode_slug_map()
    except Exception as exc:  # noqa: BLE001 — the first prompt loads them instead
        _log(f"warm caches skipped: {type(exc).__name__}: {exc}")


def serve(
    path: Path | None = None, *, install_signals: bool = True, keep_alive: bool = True
) -> int:
    """Run the service in the foreground. Exit 0 when another live instance
    holds the socket, and after a stop for changed code or a signal."""
    svc = RecallService(path or socket_path())
    if not svc.bind():
        _log(f"another instance is serving {svc.path}; exiting")
        return 0
    transport: KeepAliveTransport | None = None
    if keep_alive:
        from khipu import embed

        transport = KeepAliveTransport()
        embed.set_transport(transport.send)
    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda _s, _f: svc.stop())
    _warm_imports()
    svc.start_routing()
    threading.Thread(target=_warm_caches, daemon=True).start()
    _log(f"serving {svc.path} pid={os.getpid()} version={__version__} stamp={svc.stamp}")
    try:
        return svc.run()
    finally:
        svc.close()
        if transport is not None:
            from khipu import embed

            embed.set_transport(None)
            transport.close()


# ---- the client side, for `khipu recall status` and doctor ---------------------


def ping(path: Path | None = None, *, timeout: float = 1.0) -> dict[str, Any] | None:
    """The service's ping answer, or None when nothing answers on the socket."""
    p = Path(path) if path else socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    connected = False
    try:
        sock.connect(str(p))
        connected = True
        sock.sendall(b'{"op": "ping"}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        out = json.loads(buf.decode("utf-8"))
        return out if isinstance(out, dict) and out.get("ok") is True else {"ok": False, "error": "invalid ping response"}
    except (OSError, ValueError):
        return {"ok": False, "error": "connected daemon did not answer ping"} if connected else None
    finally:
        sock.close()


def daemon_health(path: Path | None = None, *, now_ns: int | None = None) -> dict[str, Any]:
    """The doctor block. Not running is fine: the hook falls back to the
    one-shot path. Running on code that differs from what is on disk is not,
    once the change is old enough that the service should have restarted."""
    answer = ping(path)
    if answer is None:
        return {"ok": True, "running": False}
    if not answer.get("ok"):
        return {"ok": False, "running": True, "reason": answer.get("error", "invalid ping response")}
    out: dict[str, Any] = {
        "ok": True, "running": True, "pid": answer.get("pid"),
        "version": answer.get("version"), "served": answer.get("served"),
    }
    # The service may run from another copy of the package than this doctor
    # (the desktop app's bundle asking about a service run from a checkout):
    # judge it against the files it runs from, not against this copy.
    root = answer.get("root")
    own_root = str(Path(__file__).resolve().parent)
    elsewhere = isinstance(root, str) and root != own_root and Path(root).is_dir()
    newest, _count = code_stamp_parts(Path(root) if elsewhere else None)
    try:
        disk = code_stamp(Path(root) if elsewhere else None)
    except OSError:
        return {**out, "ok": False, "reason": "installed code fingerprint unavailable"}
    differs = answer.get("code_stamp") != disk or (
        not elsewhere and answer.get("version") != __version__
    )
    now = time.time_ns() if now_ns is None else now_ns
    if differs and now - newest > 60 * 1_000_000_000:
        out["ok"] = False
        out["reason"] = (
            "the running service is on different code than the disk; "
            "it restarts by itself, or run `khipu jobs refresh`"
        )
    return out
