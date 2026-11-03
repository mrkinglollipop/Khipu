"""Prompt-path latency against a 10,000-line synthetic routing log while another
process writes every few hundred milliseconds. No timing assertions; run from
``packages/cli`` with the checkout under test first on the path:

    python -m tests.routing_prompt_bench [prompts] [write_every_ms]

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


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]]
    print(json.dumps(run(*args), sort_keys=True))
