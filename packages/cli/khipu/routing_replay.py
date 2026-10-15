"""khipu.routing_replay: the routing push's stop-condition, measured.

Routing truth, phase 3c (docs/plans/2026-10-14-routing-truth.md): "if, over
at least 200 replayed prompts, timeouts with routing on exceed those with
routing off by more than 2 in 100 prompts, switch push off and stop." This
replays prompts through a warm recall service (``recall_daemon.RecallService``
with its routing helper, as ``serve`` runs it), each prompt once with the
push off and once with it on, interleaved, and reports the timeouts per 100
prompts for each.

It never touches the live data dir: it makes its own isolated dir (its own
log, dedup state and routing record), and ``--live-replica`` only links the
live replica read only, as the promotion gate's replay does. The routing
push is on for the helper as on a Mac that uses it (each prompt's own
request still says on or off), so the helper polls and refolds as it would
there. Before the first prompt the helper must refold the seeded record at
a first limit's end with no prompt asking (else the replay stops: a helper
that never polls would leave its cost out), and a second limit ends during
the run, so the helper's clock-driven refold happens while prompts arrive. Run it as
``python -m khipu.routing_replay --prompts 200``; it prints JSON and exits 1
when the stop-condition is met.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

MIN_PROMPTS = 200
# The most timeouts per 100 prompts routing on may add before the push stops.
STOP_PER_100 = 2.0
# As bin/khipu-recall-client.py: the hook's own deadline for an answer.
ANSWER_TIMEOUT_S = 1.6
SESSIONS = 10
PROMPTS = (
    "what did we decide about the recall hook deadline",
    "where is the promotion gate's replay step",
    "how does the routing helper fork each prompt's child",
    "which account was limited yesterday afternoon",
    "the queue drain retry and the python shim",
    "why did the nightly embed backfill stop",
    "summarise the plan usage probes for codex",
    "what is the capture hook's turn threshold",
    "how do we roll back a bad promotion",
    "the hub snapshot schema version and the replica",
    "where does the session start summary get its budget",
    "the doctor block for the routing record",
    "what broke in the bundle seal on macOS",
    "how is the graph backup copied offsite",
    "which settings does the desktop app show for routing",
    "the lane map from runner lanes to accounts",
    "what does the reset check do after a limit ends",
    "how are holds from a conversation expired",
    "the redaction of runner lines before they are pushed",
    "why the daemon never forks itself for routing",
)
_AUTHOR = {"kind": "conversation", "harness": "claude_code", "project": "routing-replay", "session": "replay"}
_ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]
_ISOLATION_VARS = ("KHIPU_ISOLATED_DIR", "KHIPU_CAPTURE_HOME", "KHIPU_FEATURE_ROUTING_PUSH", "ROUTING_NOW_OFF")
# The first limit's end, which the helper must refold at before any prompt
# is asked, and how long the replay waits for that refold.
_CHECK_LIMIT = "2s"
_CHECK_WAIT_S = 20.0


@contextlib.contextmanager
def _isolated(directory: Path) -> Iterator[None]:
    """Point this process's data dir and capture home at ``directory`` for
    the run, with the routing push on (the helper takes this environment),
    and back afterwards."""
    before = {k: os.environ.get(k) for k in _ISOLATION_VARS}
    os.environ["KHIPU_ISOLATED_DIR"] = str(directory)
    os.environ["KHIPU_CAPTURE_HOME"] = str(directory / "capture")
    os.environ["KHIPU_FEATURE_ROUTING_PUSH"] = "1"
    os.environ.pop("ROUTING_NOW_OFF", None)
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _seed(directory: Path, limit_ends_in: str) -> None:
    """The isolated dir's settings (the routing push on, as on a Mac that
    uses it) and a small routing record: one account limited until
    ``limit_ends_in`` from now (its end is a clock-driven change), one open,
    and one hold."""
    from khipu import routing

    (directory / "capture" / "logs").mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"usage_accounts": _ACCOUNTS,
                                                       "features": {"routing_push": True}}), encoding="utf-8")
    routing.report(account="Codex", state="limited", until=limit_ends_in, author=_AUTHOR)
    routing.report(account="Claude", state="open", author=_AUTHOR)
    routing.add_hold("hold new launches while the replay runs", reason="keep", scope="machine", author=_AUTHOR)


def _helper_refolds(wait: float | None = None) -> float:
    """Wait, with no prompt asking, for the routing helper to refold the
    record at its ``valid_until`` (the seeded first limit's end), and return
    how many seconds after that end it saved the view. Only reads view.json
    (``routing._load_view``): nothing in this process folds. Raises when it
    does not, naming why the push is off when it is."""
    from khipu import routing

    seeded = routing._load_view() or {}
    end = routing._parse(seeded.get("valid_until"))
    if end is None:
        raise RuntimeError("the seeded routing record has no limit end for the helper to refold at")
    wait = _CHECK_WAIT_S if wait is None else wait
    until = time.monotonic() + max(0.0, (end - routing._now()).total_seconds()) + wait
    while time.monotonic() < until:
        view = routing._load_view() or {}
        folded = routing._parse(view.get("folded_at"))
        if view.get("version") != seeded.get("version") and folded is not None and folded >= end:
            return round((folded - end).total_seconds(), 2)
        time.sleep(0.05)
    off = routing.push_off("prompt")
    raise RuntimeError("the routing helper did not refold the record at its limit's end with no prompt"
                       + (f" (its push is off: {off})" if off else ""))


def _ask(path: Path, prompt: str, session: str, routing_on: bool) -> dict[str, Any]:
    """One prompt as the client sends it: request, answer, ack."""
    from khipu import recall_daemon

    raw = json.dumps({"session_id": session, "prompt": prompt, "cwd": "/replay/project",
                      "hook_event_name": "UserPromptSubmit"})
    began = time.monotonic()
    deadline = began + ANSWER_TIMEOUT_S
    env = {"KHIPU_FEATURE_ROUTING_PUSH": "1" if routing_on else "0"}
    req = {"op": "recall", "raw": raw, "shape": "claude", "protocol": 1, "deadline": deadline, "env": env}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(ANSWER_TIMEOUT_S + recall_daemon.CONN_READ_TIMEOUT_S)
    try:
        sock.connect(str(path))
        sock.sendall(json.dumps(req).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        answer = json.loads(buf.split(b"\n", 1)[0] or b"{}")
        took = time.monotonic() - began
        if answer.get("ok") is True and time.monotonic() < deadline:
            sock.sendall(b'{"op":"ack"}\n')
        return {"ok": answer.get("ok") is True, "error": answer.get("error"), "ms": round(took * 1000, 1)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"client: {type(exc).__name__}", "ms": round((time.monotonic() - began) * 1000, 1)}
    finally:
        sock.close()


def _log_rows(count: int, wait: float = 10.0) -> dict[str, list[dict[str, Any]]]:
    """The summary lines by session, once ``count`` of them are written (the
    service writes each after the client's ack, on its own thread)."""
    from khipu import recall_prompt

    path = recall_prompt._log_path()
    until = time.monotonic() + wait
    while True:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        rows = [line for line in lines if recall_prompt._LOG_LINE_RE.match(line)]
        if len(rows) >= count or time.monotonic() >= until:
            break
        time.sleep(0.05)
    out: dict[str, list[dict[str, Any]]] = {}
    for line in rows:
        m = recall_prompt._LOG_LINE_RE.match(line)
        session = line.split(" session=", 1)[1].split(" ", 1)[0]
        out.setdefault(session, []).append({"reason": m.group("reason"), "ms": float(m.group("ms")),
                                            "routing": recall_prompt.routing_status_of(line)})
    return out


# The synthetic search (``--search synthetic``): each prompt's search takes a
# time drawn once from a log-normal around ``_SYNTH_MEDIAN_S`` (about 2 in 100
# past the hook's 1.2 s), part waiting (as the embedding call does, without
# the GIL) and part Python work holding the GIL (as scoring does), so a
# Mac with no replica can still measure what routing adds to a real load.
# The same prompt gets the same time with routing on and off.
_SYNTH_MEDIAN_S = 0.6
_SYNTH_SIGMA = 0.35
_SYNTH_CPU_SHARE = 0.4
_SYNTH_SEED = 3


def _synthetic_times(n: int) -> list[float]:
    import math
    import random

    rng = random.Random(_SYNTH_SEED)
    return [math.exp(rng.gauss(math.log(_SYNTH_MEDIAN_S), _SYNTH_SIGMA)) for _ in range(n)]


@contextlib.contextmanager
def _synthetic_search(times: dict[str, float]) -> Iterator[None]:
    """Stand the search in for one that takes ``times[prompt]`` seconds."""
    from khipu import recall_prompt

    real = recall_prompt._search_hits

    def search(prompt, *_a, **_k):
        seconds = times.get(prompt, _SYNTH_MEDIAN_S)
        time.sleep(seconds * (1 - _SYNTH_CPU_SHARE))
        until = time.monotonic() + seconds * _SYNTH_CPU_SHARE
        n = 0
        while time.monotonic() < until:
            n += sum(i * i for i in range(200))
        return {"hits": [{"kind": "episode", "id": str(abs(hash(prompt)) % 9973), "score": 0.9,
                          "snippet": "a synthetic hit", "label": "a synthetic hit"}],
                "legs": ["lexical", "cosine"], "degraded": None}

    recall_prompt._search_hits = search
    try:
        yield
    finally:
        recall_prompt._search_hits = real


def verdict(on_per_100: float, off_per_100: float, *, most: float = STOP_PER_100) -> bool:
    """True when the push must stop: timeouts per 100 prompts with routing
    on exceed those with it off by more than ``most``."""
    return on_per_100 - off_per_100 > most


def _summary(asked: list[dict[str, Any]], rows: list[dict[str, Any]], n: int) -> dict[str, Any]:
    timeouts = sum(1 for r in rows if r["reason"].startswith("timeout"))
    # A prompt whose answer never reached the client is a timeout too, logged or not.
    unlogged = max(0, sum(1 for a in asked if not a["ok"] and str(a.get("error") or "").startswith("timeout"))
                   - timeouts)
    timeouts += unlogged
    statuses: dict[str, int] = {}
    for r in rows:
        if r["routing"]:
            key = r["routing"].split(":", 2)[0] if not r["routing"].startswith("skipped") else r["routing"]
            statuses[key] = statuses.get(key, 0) + 1
    reasons: dict[str, int] = {}
    for r in rows:
        key = r["reason"].split(":", 1)[0].split(">", 1)[0].split(" (", 1)[0]
        reasons[key] = reasons.get(key, 0) + 1
    ms = [a["ms"] for a in asked]
    return {"prompts": n, "timeouts": timeouts, "reasons": reasons, "per_100": round(100.0 * timeouts / n, 2) if n else 0.0,
            "median_ms": round(statistics.median(ms), 1) if ms else None,
            "p95_ms": round(sorted(ms)[int(0.95 * (len(ms) - 1))], 1) if ms else None,
            "routing": statuses}


def replay(n: int = MIN_PROMPTS, *, prompts: tuple[str, ...] | list[str] = PROMPTS, live_replica: bool = False,
           limit_ends_in: str = "20s", workdir: Path | None = None, search: str = "real") -> dict[str, Any]:
    """Replay ``n`` prompts with routing on and ``n`` with it off through
    one warm recall service, interleaved (on first, then off first, in
    turn), and report timeouts per 100 for each and whether the
    stop-condition is met (``stop``). Everything it writes is under
    ``workdir`` (a new temp dir by default, removed afterwards). ``search``
    is ``real`` (the hook's own search) or ``synthetic`` (a stand-in with a
    realistic spread of times, for a Mac or a test with no replica)."""
    from khipu import embed, recall_daemon

    texts = [f"{prompts[i % len(prompts)]} (prompt {i + 1})" for i in range(n)]
    synthetic = dict(zip(texts, _synthetic_times(n))) if search == "synthetic" else None

    own = workdir is None
    directory = Path(tempfile.mkdtemp(prefix="krr-", dir="/tmp")) if own else Path(workdir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if live_replica:
            from khipu import promote

            promote._link_replica(directory)  # before isolation: the live replica's path
        stand_in = _synthetic_search(synthetic) if synthetic is not None else contextlib.nullcontext()
        with _isolated(directory), stand_in:
            _seed(directory, _CHECK_LIMIT)
            recall_daemon._warm_imports()
            recall_daemon._warm_caches()
            transport = recall_daemon.KeepAliveTransport()
            embed.set_transport(transport.send)
            svc = recall_daemon.RecallService(directory / "capture" / "recall.sock", stamp_fn=lambda: "replay",
                                              idle_check=False)
            if not svc.bind():
                raise RuntimeError("another recall service holds the replay's socket")
            thread = threading.Thread(target=svc.run, daemon=True)
            thread.start()
            try:
                svc.routing_forkserver.prepare()
                if not svc.routing_forkserver.wait_ready(30):
                    raise RuntimeError("the routing helper did not start")
                helper_refold_s = _helper_refolds()
                # The limit that ends while the prompts arrive.
                from khipu import routing

                routing.report(account="Codex", state="limited", until=limit_ends_in, author=_AUTHOR)
                asked: dict[bool, list[dict[str, Any]]] = {True: [], False: []}
                for i in range(n):
                    prompt = texts[i]
                    for routing_on in ((True, False) if i % 2 == 0 else (False, True)):
                        mode = "on" if routing_on else "off"
                        asked[routing_on].append(_ask(svc.path, prompt, f"replay-{mode}-{i % SESSIONS}", routing_on))
                rows = _log_rows(2 * n)
            finally:
                svc.stop()
                thread.join(5)
                svc.close()
                embed.set_transport(None)
                transport.close()
        by_mode = {mode: [r for s, rs in rows.items() if s.startswith(f"replay-{mode}-") for r in rs]
                   for mode in ("on", "off")}
        on = _summary(asked[True], by_mode["on"], n)
        off = _summary(asked[False], by_mode["off"], n)
        return {"prompts": n, "on": on, "off": off, "added_per_100": round(on["per_100"] - off["per_100"], 2),
                "stop": verdict(on["per_100"], off["per_100"]), "stop_per_100": STOP_PER_100,
                "enough": n >= MIN_PROMPTS, "live_replica": live_replica, "search": search,
                "helper_refold_s": helper_refold_s}
    finally:
        if own:
            shutil.rmtree(directory, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m khipu.routing_replay", description=__doc__.split("\n\n")[1])
    parser.add_argument("--prompts", type=int, default=MIN_PROMPTS, help="prompts each way (at least 200 to count)")
    parser.add_argument("--live-replica", action="store_true", help="search the live replica, read only")
    parser.add_argument("--search", choices=("real", "synthetic"), default="real",
                        help="the hook's own search, or a stand-in with a realistic spread of times")
    args = parser.parse_args(argv)
    out = replay(max(1, args.prompts), live_replica=args.live_replica, search=args.search)
    print(json.dumps(out, indent=1, sort_keys=True))
    return 1 if out["stop"] else 0


if __name__ == "__main__":
    sys.exit(main())
