# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""The replica fact copy under a sustained workload (design section 8, phase
7B; owner answer 3). Its bounds are the build's acceptance and are not tuned.

A hermetic hub holds 50,000 facts (commitments, decisions and deliverables in
a 2:2:1 mix, each claimed by its own anchor episode) and 20,000 commitment
vectors, and is reached through a proxy that delays every byte 25 ms each
way (50 ms round trip). After the first dump, 200 rounds each make one
capture, then a changed pull, then an unchanged pull, while one thread reads
the replica's fact readers and another makes live patches.

Acceptance: an unchanged pull writes nothing to SQLite and finishes under
0.5 s; a changed pull holds ``write_gate`` (its publication) under 2 s; the
incremental peak RSS over the workload stays under 200 MiB.
"""
from __future__ import annotations

import ctypes
import hashlib
import heapq
import itertools
import json
import os
import random
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager

import psycopg
import pytest

from khipu import db, hub_snapshot as hs, offline_reads, provenance
from tests.fact_seed import announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import mark_ready, replica  # noqa: F401
from tests.test_provenance_forget import PROJECT, _capture, env, pg  # noqa: F401

FACTS, VECTORS = 50_000, 20_000
ROUNDS = int(os.environ.get("PROVENANCE_COPY_BENCH_ROUNDS", "200"))
ONE_WAY_S = 0.025


def _seed(h, n: int) -> None:
    episodes = n // 10
    commitments, decisions = (2 * n) // 5, (2 * n) // 5
    deliverables = n - commitments - decisions
    # Each episode insert takes its identities' advisory locks until commit,
    # so the episodes go in batches small enough for the lock table.
    for start in range(1, episodes + 1, 200):
        with h.connect() as conn:
            announce(conn)
            conn.execute(
                "INSERT INTO episodes (ts, session_id, summary, raw, harness, project, transcript_range) "
                "SELECT now(), 'codex:bench-' || g, 'bench episode ' || g, "
                "jsonb_build_object('ts', now()::text, 'summary', 'bench episode ' || g, "
                "'session_id', 'codex:bench-' || g, 'harness', 'codex', 'transcript_range', '0:1'), "
                "'codex', %s, '0:1' FROM generate_series(%s::int, %s::int) g",
                (PROJECT, start, min(episodes, start + 199)))
    with h.connect() as conn:
        first = conn.execute("SELECT min(id) FROM episodes").fetchone()[0]
        conn.execute(
            "INSERT INTO commitments (text, project, owner, kind, opened_episode, content_hash) "
            "SELECT 'bench commitment ' || g, %s, 'agent', 'followup', %s + (g %% %s), 'bench-c-' || g "
            "FROM generate_series(1, %s) g", (PROJECT, first, episodes, commitments))
        conn.execute(
            "INSERT INTO decisions (project, text, episode_id) "
            "SELECT %s, 'bench decision ' || g, %s + (g %% %s) FROM generate_series(1, %s) g",
            (PROJECT, first, episodes, decisions))
        conn.execute(
            "INSERT INTO deliverables (project, kind, path, title, episode_id) "
            "SELECT %s, 'file', 'docs/bench-' || g || '.md', 'bench deliverable ' || g, %s + (g %% %s) "
            "FROM generate_series(1, %s) g", (PROJECT, first, episodes, deliverables))
        for kind, table, field, col, anchor in (("commitment", "commitments", "text", "text", "opened_episode"),
                                                ("decision", "decisions", "text", "text", "episode_id"),
                                                ("deliverable", "deliverables", "title", "title", "episode_id")):
            conn.execute(f"INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id) "
                         f"SELECT '{kind}', id, '{field}', {col}, 'episode', {anchor} FROM {table}")
            conn.execute(f"SELECT count(khipu_project_fact('{kind}', id)) FROM {table}")
        profile, dim = conn.execute("SELECT id, dim FROM embedding_profiles ORDER BY id LIMIT 1").fetchone()
        # One vector per commitment, of float noise as wide as a provider's
        # (each value 8 to 9 characters as text).
        rng = random.Random(7)
        vector = "[" + ",".join(f"{rng.uniform(-0.5, 0.5):.6f}" for _ in range(int(dim))) + "]"
        conn.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, content_hash, embedding) "
            "SELECT %s, 'commitment', c.id::text, 0, c.text, md5(c.text), %s::vector "
            "FROM commitments c ORDER BY c.id LIMIT %s", (profile, vector, VECTORS))


class DelayProxy:
    """A TCP proxy that holds every chunk ``ONE_WAY_S`` before forwarding it,
    in each direction, without limiting throughput (a delay line)."""

    def __init__(self, port: int):
        self.upstream = ("127.0.0.1", port)
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.stopping = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self.stopping:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            up = socket.create_connection(self.upstream)
            for a, b in ((client, up), (up, client)):
                self._pipe(a, b)

    def _pipe(self, src: socket.socket, dst: socket.socket) -> None:
        line: list = []
        ready = threading.Condition()
        order = itertools.count()

        def read() -> None:
            while True:
                try:
                    data = src.recv(1 << 16)
                except OSError:
                    data = b""
                with ready:
                    heapq.heappush(line, (time.monotonic() + ONE_WAY_S, next(order), data))
                    ready.notify()
                if not data:
                    return

        def write() -> None:
            while True:
                with ready:
                    while not line:
                        ready.wait()
                    due, _n, data = line[0]
                    wait = due - time.monotonic()
                    if wait > 0:
                        ready.wait(wait)
                        continue
                    heapq.heappop(line)
                try:
                    if not data:
                        dst.shutdown(socket.SHUT_WR)
                        return
                    dst.sendall(data)
                except OSError:
                    return

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=write, daemon=True).start()

    def close(self) -> None:
        self.stopping = True
        self.server.close()


def _rss() -> int:
    """This process's resident set size now, in bytes."""
    if sys.platform == "darwin":
        class Info(ctypes.Structure):
            _fields_ = [("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
                        ("resident_size_max", ctypes.c_uint64), ("user_time", ctypes.c_int32 * 2),
                        ("system_time", ctypes.c_int32 * 2), ("policy", ctypes.c_int32),
                        ("suspend_count", ctypes.c_int32)]
        libc = ctypes.CDLL(None)
        info, count = Info(), ctypes.c_uint(ctypes.sizeof(Info) // 4)
        task = ctypes.c_uint.in_dll(libc, "mach_task_self_")
        assert libc.task_info(task, 20, ctypes.byref(info), ctypes.byref(count)) == 0  # MACH_TASK_BASIC_INFO
        return int(info.resident_size)
    with open("/proc/self/statm") as statm:
        return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


class PhaseTimer:
    """Where one thread's calls spend their time (measurement only): only
    calls on the timed thread count, and only a name's outermost call, so
    ``copy_facts(session=...)``, which calls itself, is timed once. The
    copy lock's acquisition and hold and a publication's admission and hold
    are recorded apart from the calls around them."""

    def __init__(self) -> None:
        self.phases: dict[str, float] = {}
        self.thread: int | None = None
        self._depth = threading.local()

    def start(self) -> None:
        self.phases.clear()
        self.thread = threading.get_ident()

    def stop(self) -> None:
        self.thread = None

    def mine(self) -> bool:
        return threading.get_ident() == self.thread

    def add(self, name: str, secs: float) -> None:
        if self.mine():
            self.phases[name] = round(self.phases.get(name, 0.0) + secs, 4)

    def wrap(self, name, fn):
        def run(*args, **kw):
            depth = getattr(self._depth, name, 0)
            setattr(self._depth, name, depth + 1)
            started = time.monotonic()
            try:
                return fn(*args, **kw)
            finally:
                setattr(self._depth, name, depth)
                if depth == 0:
                    self.add(name, time.monotonic() - started)
        return run

    def held(self, name, real):
        """``real``, a context manager, timed as ``name_wait`` until it
        yields and ``name_held`` until it exits."""
        @contextmanager
        def run(*args, **kw):
            started = time.monotonic()
            with real(*args, **kw) as got:
                entered = time.monotonic()
                self.add(f"{name}_wait", entered - started)
                try:
                    yield got
                finally:
                    self.add(f"{name}_held", time.monotonic() - entered)
        return run


def _file_hash() -> tuple[str, int]:
    # Streamed, so the measured RSS is the product's, not a file-sized buffer.
    path = hs.snapshot_path()
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_mtime_ns


def test_the_copy_under_a_sustained_workload_meets_its_bounds(pg, replica, pg_cluster, monkeypatch):
    replica(pg, ready=False)
    _seed(pg, FACTS)
    mark_ready(pg)
    proxy = DelayProxy(pg_cluster.port)
    remote = pg.dsn.replace(f":{pg_cluster.port}/", f":{proxy.port}/")
    monkeypatch.setattr(hs, "try_hub_connect", lambda **kw: psycopg.Connection.connect(remote, **kw))
    monkeypatch.setattr(db, "resolve_dsn", lambda: remote)
    # The seed wrote 5,000 episodes a moment before the dump; without this
    # the pull's overlap window would read them all again.
    monkeypatch.setattr(hs, "FRESHNESS_OVERLAP_S", 0)
    holds: list[float] = []
    real_admitted = hs._admitted

    @contextmanager
    def timed(path, deadline):
        # From gate entry (the locks and BEGIN IMMEDIATE) to their release.
        started = time.monotonic()
        with real_admitted(path, deadline) as got:
            yield got
        holds.append(time.monotonic() - started)

    monkeypatch.setattr(hs, "_admitted", timed)
    try:
        first = hs.refresh()
        assert first["ok"] and first["facts"].get("copied"), first
        assert first["facts"]["vectors"] == VECTORS, first["facts"]
        holds.clear()
        with pg.connect() as conn:
            live_rows = [r[0] for r in conn.execute(
                "SELECT to_jsonb(e) FROM episodes e ORDER BY id LIMIT 50").fetchall()]
        stop = threading.Event()
        side = {"reads": 0, "read_errors": [], "patches": 0, "patch_errors": [], "rss": []}

        def read_loop() -> None:
            # What recall and the MCP tools read: a page of decisions and the
            # recent deliverables, through the fact fence.
            while not stop.is_set():
                try:
                    offline_reads.decisions(PROJECT, None, "all", 50)
                    with sqlite3.connect(f"file:{hs.snapshot_path()}?mode=ro", uri=True) as con:
                        hs.recent_deliverables_snapshot(con, project=PROJECT)
                    side["reads"] += 1
                except Exception as exc:  # noqa: BLE001 - asserted below
                    side["read_errors"].append(repr(exc))
                time.sleep(0.05)

        quiet = threading.Event()
        quiet.set()

        def patch_loop() -> None:
            for row in itertools.cycle(live_rows):
                if stop.wait(0.2):
                    return
                quiet.wait()
                out = hs.upsert_episode(row, [])
                side["patches"] += 1
                if not (out.get("ok") or out.get("deferred") or "in progress" in str(out.get("error"))):
                    side["patch_errors"].append(out)

        def rss_loop() -> None:
            while not stop.wait(0.01):
                side["rss"].append(_rss())

        base = _rss()
        threads = [threading.Thread(target=f) for f in (read_loop, patch_loop, rss_loop)]
        for t in threads:
            t.start()
        changed, unchanged, unchanged_copy, wrote, unchanged_phases = [], [], [], [], []
        # Where an unchanged pull's time goes, on the pull's own thread
        # (measurement only; the bounds are unchanged).
        timer = PhaseTimer()
        for name in ("try_hub_connect", "_one_exchange", "_pull_apply", "_pull_finish", "copy_facts",
                     "_local_copy_state"):
            monkeypatch.setattr(hs, name, timer.wrap(name, getattr(hs, name)))
        # The copy's hub work (it waits on its worker from the pull's thread).
        monkeypatch.setattr(provenance, "hub_call", timer.wrap("hub_call", provenance.hub_call))
        monkeypatch.setattr(hs, "_fact_copy_lock", timer.held("copy_lock", hs._fact_copy_lock))
        monkeypatch.setattr(hs, "_admitted", timer.held("publication", hs._admitted))
        try:
            for i in range(ROUNDS):
                _capture(loop=None, decision=f"bench capture decision {i}")
                started = time.monotonic()
                out = hs.pull_changes(include_ledger=False)
                changed.append(time.monotonic() - started)
                assert out["ok"] and out["facts"].get("copied"), out
                # Live patches wait while the unchanged pull runs, so any
                # write to the file is the pull's own.
                quiet.clear()
                time.sleep(0.25)
                before = _file_hash()
                timer.start()
                started = time.monotonic()
                out = hs.pull_changes(include_ledger=False)
                unchanged.append(time.monotonic() - started)
                timer.stop()
                unchanged_phases.append(dict(timer.phases))
                unchanged_copy.append(out["facts"]["seconds"])
                assert out["ok"] and out["facts"].get("skipped"), out
                # A pull that re-read an episode (a deferred live patch's
                # missed marker) was not an unchanged one.
                if _file_hash() != before and not any(out.get(k) for k in (
                        "episodes", "tombstones", "vectors", "missed_retried")):
                    wrote.append({k: out.get(k) for k in ("episodes", "tombstones", "vectors", "missed_retried",
                                                          "kept_newer", "topics", "project_aliases")})
                quiet.set()
        finally:
            stop.set()
            quiet.set()
            for t in threads:
                t.join(30)
        peak = max(side["rss"] or [base]) - base
        report = {
            "rounds": ROUNDS, "facts": FACTS, "vectors": VECTORS, "round_trip_s": 2 * ONE_WAY_S,
            "unchanged_pull_max_s": round(max(unchanged), 3), "unchanged_copy_max_s": round(max(unchanged_copy), 3),
            "unchanged_pull_median_s": round(sorted(unchanged)[len(unchanged) // 2], 3),
            "unchanged_pull_p99_s": round(sorted(unchanged)[min(len(unchanged) - 1, int(len(unchanged) * 0.99))], 3),
            "slowest_unchanged_phases_s": unchanged_phases[unchanged.index(max(unchanged))],
            "changed_pull_max_s": round(max(changed), 3), "gate_hold_max_s": round(max(holds), 3),
            "gate_holds": len(holds), "incremental_peak_rss_mib": round(peak / 2**20, 1),
            "unchanged_pulls_that_wrote": len(wrote), "reads": side["reads"], "patches": side["patches"],
        }
        print("copy benchmark: " + json.dumps(report))
        assert not side["read_errors"] and not side["patch_errors"], side
        assert side["reads"] > 0 and side["patches"] > 0
        assert not wrote, report
        assert max(unchanged) < 0.5, report
        assert max(holds) < 2.0, report
        assert peak < 200 * 2**20, report
    finally:
        proxy.close()
