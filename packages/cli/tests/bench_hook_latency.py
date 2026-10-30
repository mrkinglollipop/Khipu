"""Wall-clock micro-benchmark of the Claude Code hooks under a throwaway HOME.

Run from packages/cli (never against the real HOME or hub):

  env -u KHIPU_DATABASE_URL HOME=$(mktemp -d) KHIPU_KEYCHAIN=0 \\
    PYTHONPATH="$PWD:<repo>/.python_libs" python3.11 tests/bench_hook_latency.py --runs 15 --rtt-ms 30

It starts a hermetic PostgreSQL (tests/hermetic_pg.py) on loopback, seeds a
synthetic project, and runs the real hook launchers (bin/khipu-recall-hook,
bin/khipu-prompt-recall with the warm service off, bin/khipu-delegation-hook)
as the harness would: a new process per call, the payload on stdin. With
``--rtt-ms`` the hub is reached through a local proxy that holds every reply
for that long, so round trips cost what they cost against a remote hub.
``--profile`` also prints the top cumulative entries of one in-process
SessionStart under cProfile.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE.parent
ROOT = CLI.parent.parent
sys.path.insert(0, str(CLI))

from tests import hermetic_pg  # noqa: E402

PROJECT = "synthetic/bench"


class DelayProxy:
    """TCP proxy on loopback that delays each server-to-client chunk by ``rtt``."""

    def __init__(self, target_port: int, rtt: float) -> None:
        self.target_port, self.rtt = target_port, rtt
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            client, _ = self.sock.accept()
            server = socket.create_connection(("127.0.0.1", self.target_port))
            threading.Thread(target=self._pipe, args=(client, server, 0.0), daemon=True).start()
            threading.Thread(target=self._pipe, args=(server, client, self.rtt), daemon=True).start()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket, delay: float) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if delay:
                    time.sleep(delay)
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def seed(hub) -> None:
    from tests import fact_seed

    with hub.connect() as conn:
        cur = conn.cursor()
        for i in range(300):
            fact_seed.announce(cur)
            cur.execute(
                "INSERT INTO episodes (ts, summary, project, session_id, topics) "
                "VALUES (now() - make_interval(mins => %s), %s, %s, %s, %s)",
                (i, f"synthetic episode {i} about the recall hook latency", PROJECT if i % 3 else "other/x",
                 f"claude_code:s{i}", json.dumps([f"topic-{i % 7}", f"note:bench-{i % 5}"])))
        for i in range(12):
            conn.execute(
                "INSERT INTO topics (slug, title, body, status) VALUES (%s, %s, 'synthetic body', 'active')",
                (f"topic-{i}", f"Topic {i}"))
        for i in range(12):
            conn.execute(
                "INSERT INTO commitments (text, project, content_hash, owner, kind, status) "
                "VALUES (%s, %s, %s, %s, %s, 'open')",
                (f"synthetic follow-up {i}", PROJECT, f"bench-{i}", ("user", "assistant", "agent")[i % 3],
                 ("followup", "blocker", "question")[i % 3]))
        fact_seed.adopt_all(cur, "commitment")
        conn.commit()


def git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    f"git@github.com:{PROJECT}.git"], check=True)
    return repo


def run(argv: list[str], payload: dict, env: dict, runs: int) -> tuple[list[float], str]:
    samples, out = [], ""
    data = json.dumps(payload).encode()
    for _ in range(runs):
        t0 = time.perf_counter()
        # From the session's folder, as the harness runs a hook: ``python -c``
        # puts its cwd first on sys.path, so a cwd holding a ``khipu`` package
        # would run that code instead of the tree under test.
        proc = subprocess.run(argv, input=data, capture_output=True, env=env, timeout=60, cwd=payload["cwd"])
        samples.append((time.perf_counter() - t0) * 1000)
        out = proc.stdout.decode()
    return samples, out


def summary(name: str, samples: list[float], out: str) -> dict:
    s = sorted(samples)
    return {"hook": name, "runs": len(s), "p50_ms": round(statistics.median(s), 1),
            "p90_ms": round(s[max(0, int(len(s) * 0.9) - 1)], 1), "max_ms": round(s[-1], 1),
            "out_chars": len(out)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=15)
    ap.add_argument("--rtt-ms", type=float, default=0.0)
    ap.add_argument("--root", default=str(ROOT), help="the checkout whose hooks run (default: this one)")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--trace", action="store_true", help="print each SQL statement one SessionStart sends")
    args = ap.parse_args()
    home = os.environ.get("HOME", "")
    if not home.startswith(("/var/folders/", "/tmp/", "/private/")):
        sys.exit(f"refusing: HOME={home!r} is not a throwaway folder")
    work = Path(tempfile.mkdtemp(prefix="khipu-bench-"))
    cluster = hermetic_pg.Cluster(work / "pg", hermetic_pg._free_port())
    (work / "pg").mkdir()
    cluster.start()
    try:
        cluster.build_template()
        hub = cluster.new_database()
        hub.migrate()
        seed(hub)
        port = cluster.port
        if args.rtt_ms:
            port = DelayProxy(cluster.port, args.rtt_ms / 1000).port
        dsn = f"postgresql://khipu@127.0.0.1:{port}/{hub.name}"
        repo = git_repo(work)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "KHIPU_", "GROK_"))}
        root = Path(args.root).resolve()
        env.update(KHIPU_DATABASE_URL=dsn, KHIPU_KEYCHAIN="0", KHIPU_ROOT=str(root),
                   KHIPU_PYTHON=sys.executable, KHIPU_RECALL_DAEMON="0",
                   PYTHONPATH=str(root / "packages" / "cli") + os.pathsep + os.environ.get("PYTHONPATH", ""))
        bin_dir = root / "packages" / "cli" / "bin"
        if args.trace:
            # Count the statements the launched hook itself sends.
            tracer = work / "trace"
            tracer.mkdir()
            (tracer / "sitecustomize.py").write_text(
                "import os, psycopg\n"
                "_o = psycopg.Cursor.execute\n"
                "def _ex(self, q, *a, **k):\n"
                "    with open(os.environ['KHIPU_BENCH_TRACE'], 'a') as f:\n"
                "        f.write(' '.join(str(q).split())[:100] + '\\n')\n"
                "    return _o(self, q, *a, **k)\n"
                "psycopg.Cursor.execute = _ex\n")
            env["KHIPU_BENCH_TRACE"] = str(work / "trace.txt")
            env["PYTHONPATH"] = f"{tracer}{os.pathsep}{env['PYTHONPATH']}"
        results = []
        start = {"session_id": "bench-start", "cwd": str(repo), "source": "startup",
                 "hook_event_name": "SessionStart"}
        results.append(summary("session_start", *run(["/bin/sh", str(bin_dir / "khipu-recall-hook")],
                                                     start, env, args.runs)))
        prompt = {"session_id": "bench-prompt", "cwd": str(repo), "hook_event_name": "UserPromptSubmit",
                  "prompt": "what did we decide about the recall hook latency budget"}
        results.append(summary("prompt_cold", *run(["/bin/sh", str(bin_dir / "khipu-prompt-recall")],
                                                   prompt, env, args.runs)))
        tool = {"session_id": "bench-tool", "cwd": str(repo), "hook_event_name": "PreToolUse",
                "tool_name": "Bash", "tool_input": {"command": "ls"}}
        results.append(summary("delegation_bash", *run(["/bin/sh", str(bin_dir / "khipu-delegation-hook")],
                                                       tool, env, args.runs)))
        log = Path(os.environ["HOME"]) / ".grok" / "khipu" / "logs" / "prompt-recall.log"
        inner = [float(line.rsplit("ms=", 1)[1]) for line in (log.read_text().splitlines() if log.exists() else [])
                 if "[khipu-session-start]" in line and "ms=" in line]
        if inner:
            results[0]["logged_p50_ms"] = round(statistics.median(inner), 1)
        for r in results:
            print(json.dumps(r))
        if args.trace:
            sent = (work / "trace.txt").read_text().splitlines() if (work / "trace.txt").exists() else []
            print(f"launched hooks sent {len(sent)} statements in all")
            code = ("import psycopg,sys,time;from khipu import recall_rule;o=psycopg.Cursor.execute\n"
                    "def ex(self,q,*a,**k):\n t=time.perf_counter();r=o(self,q,*a,**k)\n"
                    " sys.stderr.write('%6.1f ms  %s\\n'%((time.perf_counter()-t)*1000,' '.join(str(q).split())[:110]));return r\n"
                    "psycopg.Cursor.execute=ex;recall_rule.session_start_main(shape=None)")
            proc = subprocess.run([sys.executable, "-c", code], input=json.dumps(start).encode(),
                                  capture_output=True, env=env, cwd=str(root / "packages" / "cli"), timeout=60)
            print(proc.stderr.decode()[-9000:])
        if args.profile:
            code = ("import cProfile,pstats,io,sys;from khipu import recall_rule;"
                    "p=cProfile.Profile();p.enable();recall_rule.session_start_main(shape=None);p.disable();"
                    "s=io.StringIO();pstats.Stats(p,stream=s).sort_stats('cumulative').print_stats(45);"
                    "sys.stderr.write(s.getvalue())")
            proc = subprocess.run([sys.executable, "-c", code], input=json.dumps(start).encode(),
                                  capture_output=True, env=env, cwd=str(root / "packages" / "cli"), timeout=60)
            print(proc.stderr.decode()[-9000:])
    finally:
        cluster.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
