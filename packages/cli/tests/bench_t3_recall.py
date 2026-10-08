"""Compare parent/current recall under the oracle's isolated HOME/PYTHONPATH.

Run: python3.11 tests/bench_t3_recall.py --baseline-ref 7147cca
SQLite executes fixture queries, including thread-reader SQL via a dialect
adapter. This measures local work, not production hub/network latency.
"""
import argparse
import json
import statistics
import subprocess
import time
import types
from unittest import mock

from khipu import recall_prompt
from tests.fixtures.t3 import handoff_wrapper
from tests.fixtures.t3_slice_c import memory_fixture


def measure(module, con, connection):
    def search(*args, **kwargs):
        rows = con.execute(
            "SELECT id, summary FROM episodes WHERE summary LIKE '%recall%' ORDER BY id DESC LIMIT 3"
        ).fetchall()
        return {"hits": [{"kind": "episode", "id": str(i), "snippet": s} for i, s in rows],
                "legs": ["fixture-sqlite"], "degraded": None}

    with mock.patch.object(module, "_search_hits", side_effect=search), \
         mock.patch.object(module, "_deliverable_context_line", return_value=""), \
         mock.patch("khipu.hub_snapshot.try_hub_connect", return_value=connection()), \
         mock.patch("khipu.db.has_columns", return_value=True):
        prompt = handoff_wrapper("what did we decide about the recall hook")
        for _ in range(30):
            module.prior_work_for_prompt(prompt)
        samples = []
        for _ in range(500):
            start = time.perf_counter()
            result = module.prior_work_for_prompt(prompt)
            samples.append((time.perf_counter() - start) * 1000)
    return {"iterations": len(samples), "episodes": 1000,
            "median_ms": round(statistics.median(samples), 3),
            "p95_ms": round(sorted(samples)[474], 3),
            "mean_ms": round(statistics.mean(samples), 3),
            "context_chars": len(result["context"]),
            "kinds": [h["kind"] for h in result["hits"]]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", required=True)
    args = parser.parse_args()
    source = subprocess.run(
        ["git", "show", f"{args.baseline_ref}:packages/cli/khipu/recall_prompt.py"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    baseline = types.ModuleType("recall_prompt_baseline")
    exec(compile(source, "recall_prompt_baseline.py", "exec"), baseline.__dict__)
    con, connection = memory_fixture()
    try:
        before = measure(baseline, con, connection)
        after = measure(recall_prompt, con, connection)
        print(json.dumps({"baseline_ref": args.baseline_ref, "before": before, "after": after}))
    finally:
        con.close()


if __name__ == "__main__":
    main()
