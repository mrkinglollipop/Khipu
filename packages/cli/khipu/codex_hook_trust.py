"""Ask Codex whether it trusts Khipu's hooks (read-only).

Codex runs a hook only after its owner has reviewed it in Codex. Khipu's hook
file can be right and Codex still never run it, and only Codex knows. Its
app-server answers ``hooks/list`` over JSON-RPC on stdio: ``initialize``, the
``initialized`` notification, then ``hooks/list`` with ``{cwds: [folder]}``;
each hook comes back with ``key``, ``currentHash`` and ``trustStatus``
(``untrusted``, ``trusted``, ``modified`` or ``managed``).

This only reads: it never writes Codex's config and never marks a hook
trusted. It is asked by the full ``khipu doctor`` alone, with a short time
limit; ``--quick`` and every hook path skip it. Never raises.
"""
from __future__ import annotations

import contextlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
TIMEOUT_S = 4.0

TRUSTED = "trusted"
WAITING = "waiting"
UNKNOWN = "unknown"


def codex_binary() -> str | None:
    if os.access(CODEX_BIN, os.X_OK):
        return CODEX_BIN
    return shutil.which("codex")


def _kill(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=1)


def _hooks_list(binary: str, folder: str, timeout: float) -> list[dict]:
    """The hooks Codex lists for ``folder``. Raises on a timeout, a bad answer
    or an error reply; the process is always killed."""
    proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + timeout
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        buf = b""

        def send(message: dict) -> None:
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()

        def reply(ident: int) -> dict:
            nonlocal buf
            while True:
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(message, dict) and message.get("id") == ident and "method" not in message:
                        if message.get("error") is not None:
                            raise RuntimeError("Codex answered with an error")
                        return message.get("result")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("Codex did not answer in time")
                if not sel.select(left):
                    raise TimeoutError("Codex did not answer in time")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex closed before answering")
                buf += chunk

        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "khipu-doctor", "version": "1"}}})
        reply(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "hooks/list", "params": {"cwds": [folder]}})
        result = reply(2)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise ValueError("unexpected answer")
        hooks = []
        for entry in data:
            for hook in (entry.get("hooks") if isinstance(entry, dict) else None) or []:
                if isinstance(hook, dict):
                    hooks.append(hook)
        return hooks
    finally:
        _kill(proc)


def ask(*, binary: str | None = None, folder: str | None = None, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """``{"state": "trusted" | "waiting" | "unknown", "words": ..., ...}`` for
    Khipu's hooks as Codex sees them. Khipu's hooks are the listed command
    hooks whose command is one of Khipu's launchers."""
    from khipu.integrations import _cursor_is_khipu

    binary = binary or codex_binary()
    if not binary:
        return {"state": UNKNOWN, "words": "installed; could not ask Codex (Codex was not found)"}
    try:
        hooks = _hooks_list(binary, folder or str(Path.home()), timeout)
        mine = [h for h in hooks if isinstance(h.get("command"), str) and _cursor_is_khipu(h["command"])]
        statuses = [h.get("trustStatus") for h in mine]
        if not mine or any(s not in ("trusted", "managed", "untrusted", "modified") for s in statuses):
            return {"state": UNKNOWN, "words": "installed; could not ask Codex (its answer was not understood)"}
    except TimeoutError:
        return {"state": UNKNOWN, "words": "installed; could not ask Codex (it did not answer in time)"}
    except Exception as exc:  # noqa: BLE001 - a question that fails is "could not ask", never a crash
        return {"state": UNKNOWN, "words": f"installed; could not ask Codex ({type(exc).__name__})"}
    waiting = sum(1 for s in statuses if s in ("untrusted", "modified"))
    if waiting:
        return {"state": WAITING, "hooks": len(mine), "waiting": waiting,
                "words": "installed, waiting for your review in Codex"}
    return {"state": TRUSTED, "hooks": len(mine), "words": "installed and trusted"}
