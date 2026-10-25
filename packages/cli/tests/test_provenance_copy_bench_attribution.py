# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""Where the copy benchmark's slow unchanged pulls spend their time, under
synthetic load (measurement only; opt-in with PROVENANCE_ATTRIBUTION=1, and
the benchmark's bounds are its own, unchanged).

The benchmark (``test_provenance_copy_bench``) runs while CPU-bound
processes saturate the host and pgbench drives the same hermetic cluster
(a TPC-B workload on its own database, and full scans of the hub's vector
table on the hub's). Each pull's hub session is named, so the cluster's log
gives every statement's server-side duration, and a sampler reads its wait
events from ``pg_stat_activity``. Beside them the client records each
pull's elapsed time, its connect time and the interpreter's GC pauses.
Everything goes to the JSON file PROVENANCE_ATTRIBUTION_OUT.

Knobs: PROVENANCE_ATTRIBUTION_CPU (busy processes, default the CPU count),
PROVENANCE_ATTRIBUTION_PG (pgbench clients, default 8; 0 for none),
PROVENANCE_ATTRIBUTION_HEAP (millions of live objects kept, default 0).
"""
from __future__ import annotations

import gc
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest

from khipu import hub_snapshot as hs, provenance
from tests import test_provenance_copy_bench as bench
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import replica  # noqa: F401
from tests.test_provenance_forget import env, pg  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get("PROVENANCE_ATTRIBUTION"),
                                reason="opt-in measurement: PROVENANCE_ATTRIBUTION=1")

_SAMPLER = r"""
import json, sys, time, psycopg
conn = psycopg.Connection.connect(sys.argv[1], autocommit=True, application_name="attr-sampler")
with open(sys.argv[2], "w") as out:
    while True:
        rows = conn.execute(
            "SELECT extract(epoch FROM clock_timestamp())::float8, application_name, state, "
            "coalesce(wait_event_type, ''), coalesce(wait_event, '') FROM pg_stat_activity "
            "WHERE application_name LIKE 'bench-p%' AND state = 'active'").fetchall()
        for row in rows:
            out.write(json.dumps(row) + "\n")
        out.flush()
        time.sleep(0.002)
"""
# One line per simple query or extended-protocol execute (each one round
# trip); parse and bind lines are left out.
_LOG_LINE = re.compile(r"app=(bench-p\d+) .*?duration: ([0-9.]+) ms\s+(?:statement|execute)")


def _pgbench(cluster, *args) -> list[str]:
    return [shutil.which("pgbench"), "-h", "127.0.0.1", "-p", str(cluster.port), "-U", "khipu", *args]


def _attribute(pull: dict, statements: list[float], samples: list[list], pauses: list[tuple]) -> dict:
    gc_s = sum(max(0.0, min(e, pull["end"]) - max(s, pull["start"])) for s, e in pauses)
    waits: dict[str, int] = {}
    for _t, _app, _state, kind, event in samples:
        key = f"{kind}:{event}" if kind else "CPU (no wait event)"
        waits[key] = waits.get(key, 0) + 1
    server = sum(statements) / 1000
    # Every simple query crosses the delay line once each way.
    proxy = len(statements) * 2 * bench.ONE_WAY_S
    return {
        "label": pull["label"], "elapsed_s": round(pull["elapsed"], 4), "connect_s": round(pull["connect"], 4),
        "hub_call_s": round(pull["hub_call"], 4), "exchanges": len(statements),
        "server_statement_s": round(server, 4), "server_statements_ms": statements,
        "server_wait_samples": waits, "injected_proxy_delay_s": round(proxy, 4), "gc_pause_s": round(gc_s, 4),
        "client_and_proxy_scheduling_s": round(pull["elapsed"] - pull["connect"] - server - proxy - gc_s, 4),
    }


def test_attribute_the_unchanged_pull_tail_under_load(pg, replica, pg_cluster, monkeypatch, tmp_path):
    out_path = Path(os.environ.get("PROVENANCE_ATTRIBUTION_OUT") or tmp_path / "attribution.json")
    log = pg_cluster.root / "log"
    with pg_cluster.admin() as admin:
        admin.execute("ALTER SYSTEM SET log_line_prefix = '%m [%p] app=%a '")
        admin.execute("SELECT pg_reload_conf()")
        admin.execute(f"ALTER DATABASE {pg.name} SET log_min_duration_statement = 0")
        admin.execute(f"ALTER DATABASE {pg.name} SET track_io_timing = on")
        admin.execute("DROP DATABASE IF EXISTS attr_load")
        admin.execute("CREATE DATABASE attr_load")
    procs: list[subprocess.Popen] = []
    cpu = int(os.environ.get("PROVENANCE_ATTRIBUTION_CPU", str(os.cpu_count() or 4)))
    clients = int(os.environ.get("PROVENANCE_ATTRIBUTION_PG", "8"))
    heap = [object() for _ in range(int(float(os.environ.get("PROVENANCE_ATTRIBUTION_HEAP", "0")) * 1e6))]
    scan = tmp_path / "scan.sql"
    scan.write_text("SELECT count(*), sum(length(chunk_text)) FROM memory_embeddings;\n")
    pulls: list[dict] = []
    pauses: list[tuple] = []
    label: list[str | None] = [None]
    gc_started: list[float] = [0.0]

    def on_gc(phase, info):
        if phase == "start":
            gc_started[0] = time.time()
        else:
            pauses.append((gc_started[0], time.time()))

    real_connect = psycopg.Connection.connect

    def connect(conninfo="", **kw):
        if label[0] is None:
            return real_connect(conninfo, **kw)
        started = time.monotonic()
        try:
            return real_connect(conninfo, **{**kw, "application_name": label[0]})
        finally:
            pulls[-1]["connect"] += time.monotonic() - started

    real_pull, real_hub_call = hs.pull_changes, provenance.hub_call

    def pull(*a, **kw):
        label[0] = f"bench-p{len(pulls) + 1}"
        pulls.append({"label": label[0], "connect": 0.0, "hub_call": 0.0, "start": time.time()})
        started = time.monotonic()
        try:
            result = real_pull(*a, **kw)
        finally:
            pulls[-1]["elapsed"] = time.monotonic() - started
            pulls[-1]["end"] = time.time()
            label[0] = None
        pulls[-1]["unchanged"] = bool(result.get("ok") and result.get("facts", {}).get("skipped"))
        return result

    def hub_call(*a, **kw):
        started = time.monotonic()
        try:
            return real_hub_call(*a, **kw)
        finally:
            if label[0] is not None:
                pulls[-1]["hub_call"] += time.monotonic() - started

    monkeypatch.setattr(psycopg.Connection, "connect", staticmethod(connect))
    monkeypatch.setattr(hs, "pull_changes", pull)
    monkeypatch.setattr(provenance, "hub_call", hub_call)
    samples_path = tmp_path / "samples.jsonl"
    offset = log.stat().st_size if log.exists() else 0
    bound: str | None = None
    load_before = os.getloadavg()
    try:
        if clients:
            subprocess.run(_pgbench(pg_cluster, "-i", "-q", "-s", "10", "attr_load"), check=True,
                           capture_output=True, timeout=300)
            procs.append(subprocess.Popen(_pgbench(pg_cluster, "-n", "-c", str(clients), "-j", "4", "-T", "100000",
                                                   "attr_load"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            procs.append(subprocess.Popen(_pgbench(pg_cluster, "-n", "-c", "2", "-T", "100000", "-f", str(scan),
                                                   pg.name), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs += [subprocess.Popen([sys.executable, "-I", "-c", "while True: pass"]) for _ in range(cpu)]
        procs.append(subprocess.Popen([sys.executable, "-c", _SAMPLER, pg_cluster.dsn(pg.name), str(samples_path)],
                                      env=os.environ.copy()))
        gc.callbacks.append(on_gc)
        try:
            bench.test_the_copy_under_a_sustained_workload_meets_its_bounds(pg, replica, pg_cluster, monkeypatch)
        except AssertionError as exc:  # a bound missed under load is a finding, reported below
            bound = str(exc)[:2000]
        finally:
            gc.callbacks.remove(on_gc)
    finally:
        load_after = os.getloadavg()
        for proc in procs:
            proc.kill()
        for proc in procs:
            proc.wait(30)
        del heap
    statements: dict[str, list[float]] = {}
    with log.open("r", errors="replace") as fh:
        fh.seek(offset)
        for line in fh:
            if m := _LOG_LINE.search(line):
                statements.setdefault(m.group(1), []).append(float(m.group(2)))
    samples: dict[str, list] = {}
    for line in samples_path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        samples.setdefault(row[1], []).append(row)
    unchanged = [p for p in pulls if p.get("unchanged")]
    unchanged.sort(key=lambda p: p["elapsed"], reverse=True)
    rows = [_attribute(p, statements.get(p["label"], []), samples.get(p["label"], []), pauses)
            for p in unchanged]
    times = sorted(p["elapsed"] for p in unchanged)
    report = {
        "load": {"cpu_processes": cpu, "pgbench_clients": clients, "hub_scan_clients": 2 if clients else 0,
                 "extra_heap_objects": int(float(os.environ.get("PROVENANCE_ATTRIBUTION_HEAP", "0")) * 1e6),
                 "loadavg_before": load_before, "loadavg_after": load_after},
        "bound_missed": bound,
        "unchanged_pulls": len(unchanged),
        "unchanged_median_s": round(statistics.median(times), 4) if times else None,
        "unchanged_p99_s": round(times[min(len(times) - 1, int(len(times) * 0.99))], 4) if times else None,
        "unchanged_max_s": round(times[-1], 4) if times else None,
        "median_row": rows[len(rows) // 2] if rows else None,
        "slowest": rows[:10],
        "gc_pauses": len(pauses), "gc_pause_max_s": round(max((e - s for s, e in pauses), default=0.0), 4),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=1))
    print("attribution: " + json.dumps({k: report[k] for k in (
        "unchanged_median_s", "unchanged_p99_s", "unchanged_max_s", "gc_pause_max_s")}))
    assert unchanged and statements, "no unchanged pull or no server timing was recorded"
