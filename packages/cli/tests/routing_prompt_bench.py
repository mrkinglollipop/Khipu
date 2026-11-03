"""Prompt-path latency against a 10,000-line synthetic routing log while another
process writes every few hundred milliseconds. No timing assertions; run from
``packages/cli`` with the checkout under test first on the path:

    python -m tests.routing_prompt_bench [prompts] [write_every_ms]
    python -m tests.routing_prompt_bench pre-ready [lines ...]

Each prompt is a forked child of this process (as the per-prompt child is of the
routing helper, which keeps its caches and its alias table fresh between requests) that calls ``routing.prompt_push``; the time is the
child's own clock from the call to its answer (what the hook's budget counts). A second process writes a
conversation report every ``write_every_ms``; its call times are reported as the
write path's latency."""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

END = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
KEYS = ["claude|", "codex|"]


def _env(root: Path) -> None:
    os.environ.update(HOME=str(root), KHIPU_DATA_DIR=str(root / "data"), KHIPU_CAPTURE_HOME=str(root / "capture"),
                      KHIPU_KEYCHAIN="0", KHIPU_SESSION_ID="bench-author", KHIPU_HARNESS="codex")


def _percentile(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def _writer(root: Path, every_ms: int, stop: Path, out: Path) -> None:
    _env(root)
    from khipu import routing

    times = []
    while not stop.exists():
        start = time.perf_counter()
        routing.conversation_write({"action": "report", "state": "open", "account": "claude"},
                                   author={"kind": "conversation", "harness": "codex", "session": "w"})
        times.append((time.perf_counter() - start) * 1000)
        for _ in range(max(1, every_ms // 50)):
            if stop.exists():
                break
            time.sleep(0.05)
    out.write_text(json.dumps(times))


def run(prompts: int = 100, every_ms: int = 300) -> dict:
    root = Path(tempfile.mkdtemp(prefix="khipu-prompt-bench-"))
    _env(root)
    (root / "data").mkdir()
    (root / "data" / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}],
        "routing_reset_check": False}))
    from khipu import routing
    from tests import routing_compact_synth as synth

    now = datetime.now(timezone.utc)
    synth.write(routing.events_file(), synth.real_shape(KEYS, now - timedelta(minutes=5)))
    routing.read_view()
    routing.prompt_imports()
    from khipu import agent_view

    agent_view.prepare_routing_table()  # the routing helper keeps its table fresh between requests
    refreshed = time.monotonic()
    stop, wrote = root / "stop", root / "writes.json"
    writer = subprocess.Popen([sys.executable, "-c",
                               "import sys; from tests import routing_prompt_bench as b; from pathlib import Path; "
                               "b._writer(Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))",
                               str(root), str(every_ms), str(stop), str(wrote)])
    time.sleep(1.0)
    times, kinds = [], []
    for n in range(prompts):
        if time.monotonic() - refreshed > 1.0:
            agent_view.prepare_routing_table(refresh=True)
            refreshed = time.monotonic()
        read, write = os.pipe()
        start = time.perf_counter()
        pid = os.fork()
        if pid == 0:
            os.close(read)
            try:
                refolds = []
                extra = {"kick": refolds.append} if "kick" in inspect.signature(routing.prompt_push).parameters else {}
                began = time.perf_counter()
                out = routing.prompt_push(None, budget=1.0, session_id=f"bench-{n % 5}", **extra)
                took = (time.perf_counter() - began) * 1000
                os.write(write, f"{out.get('kind') or '?'} {took}".encode())
                if refolds:
                    os.close(write)
                    routing.refold_in_background()  # what the per-prompt child does after its answer
            except BaseException as exc:  # noqa: BLE001
                os.write(write, f"{type(exc).__name__} 0".encode())
            os._exit(0)
        os.close(write)
        kind, _, took = os.read(read, 64).decode().partition(" ")
        kinds.append(kind)
        times.append(float(took))
        os.close(read)
        os.waitpid(pid, 0)
        time.sleep(0.11)
    stop.write_text("1")
    writer.wait(timeout=60)
    writes = json.loads(wrote.read_text()) if wrote.exists() else []
    lines = sum(1 for _ in routing.events_file().open())
    return {"lines": lines, "prompts": prompts,
            "prompt_p50_ms": round(_percentile(times, 50), 1), "prompt_p95_ms": round(_percentile(times, 95), 1),
            "prompt_p99_ms": round(_percentile(times, 99), 1), "prompt_max_ms": round(max(times), 1),
            "over_50ms": sum(t > 50 for t in times), "answers": {k: kinds.count(k) for k in set(kinds)},
            "writes": len(writes), "write_p50_ms": round(_percentile(writes, 50), 1) if writes else None,
            "write_p95_ms": round(_percentile(writes, 95), 1) if writes else None}


_PRE_READY = r"""
import json, os, statistics, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
root, lines, runs = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
os.environ.update(HOME=str(root), KHIPU_DATA_DIR=str(root / "data"), KHIPU_CAPTURE_HOME=str(root / "capture"),
                  KHIPU_KEYCHAIN="0")
(root / "data").mkdir(parents=True)
(root / "data" / "config.json").write_text(json.dumps({"usage_accounts": [
    {"label": "Claude", "harness": "claude"}, {"label": "Work", "harness": "claude", "home": str(root / ".claude-work")},
    {"label": "Codex", "harness": "codex"}], "routing_reset_check": False}))
from khipu import routing
from tests import routing_compact_synth as synth
keys = ["claude|", "claude|" + str(root / ".claude-work"), "codex|"]
synth.write(routing.events_file(), synth.real_shape(keys, datetime.now(timezone.utc) - timedelta(minutes=5), total=lines))
routing.read_view()
routing.prompt_imports()
from khipu import agent_view
times, parses = [], 0
for _ in range(runs):
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        routing._ENTRY_INPUTS.clear()
        routing._PARSED_LINE_CACHE.clear()
        agent_view._TABLE.clear()
        agent_view._TABLE.update(sig=None, at=0.0, table=None)
        calls = []
        real = routing._read_all_events
        routing._read_all_events = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        began = time.perf_counter()
        # What ``recall_prompt._routing_child`` does before it writes "ready".
        agent_view.prepare_routing_table(cold=True)
        if not routing.push_off("prompt", environ=dict(os.environ)):
            routing.prompt_budget()
        took = (time.perf_counter() - began) * 1000
        os.write(write, json.dumps([took, len(calls)]).encode())
        os._exit(0)
    os.close(write)
    took, n = json.loads(os.read(read, 256))
    os.close(read)
    os.waitpid(pid, 0)
    times.append(took)
    parses = max(parses, n)
began = time.perf_counter()
routing._ENTRY_INPUTS.clear()
routing._PARSED_LINE_CACHE.clear()
routing._entry_cache()
parse_ms = (time.perf_counter() - began) * 1000
print(json.dumps({"lines": sum(1 for _ in routing.events_file().open()), "runs": runs,
                  "pre_ready_ms": round(statistics.median(times), 2), "pre_ready_max_ms": round(max(times), 2),
                  "parses": parses, "full_parse_ms": round(parse_ms, 1)}))
"""


def pre_ready(lines: int = 10_000, runs: int = 5, root: Path | None = None) -> dict:
    """The per-prompt child's routing work before it says it is ready (the
    alias inventory build and the push budget), median of ``runs`` cold
    children forked from a process that has imported what the helper
    imports, against a ``lines``-line log with a saved view. ``parses``: the
    most whole-log parses any child made; ``full_parse_ms``: what one costs
    here, which the child paid before its inventory came from the view. A
    fresh interpreter, so the environment it sets stays its own."""
    root = Path(tempfile.mkdtemp(prefix="khipu-pre-ready-")) if root is None else root
    cwd = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", _PRE_READY, str(root), str(lines), str(runs)], cwd=cwd,
                         env=dict(os.environ, PYTHONPATH=str(cwd)), capture_output=True, text=True, timeout=300,
                         check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    if sys.argv[1:2] == ["pre-ready"]:
        for n in [int(a) for a in sys.argv[2:]] or [1_000, 10_000]:
            print(json.dumps(pre_ready(n), sort_keys=True))
        sys.exit(0)
    args = [int(a) for a in sys.argv[1:]]
    print(json.dumps(run(*args), sort_keys=True))
