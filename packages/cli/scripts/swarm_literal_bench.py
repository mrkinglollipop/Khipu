"""Swarm-ready slice 4 measurement: the literal (ILIKE) episode leg at hub size,
with and without the trigram indexes migration 0041 adds.

Hermetic only: starts a throwaway PostgreSQL through tests/hermetic_pg.py,
loads synthetic episodes of the live hub's shape (23,600 rows, 7,080 in one
project) and prints literal-search p95 plus capture insert latency. Never
touches a real hub. Run from packages/cli:

    PYTHONPATH="$PWD" python3.11 scripts/swarm_literal_bench.py
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

N_EPISODES = 23_600
N_BIG_PROJECT = 7_080
OTHER_PROJECTS = 40
INSERTS = 400
REPS = 3

NEW_INDEXES = (
    "CREATE INDEX idx_episodes_topics_text_trgm ON episodes USING gin ((COALESCE(topics::text, '')) gin_trgm_ops)",
    "CREATE INDEX idx_episodes_decisions_text_trgm ON episodes USING gin ((COALESCE(decisions::text, '')) gin_trgm_ops)",
    "CREATE INDEX idx_episodes_preferences_text_trgm ON episodes USING gin ((COALESCE(preferences::text, '')) gin_trgm_ops)",
    "CREATE INDEX idx_episodes_people_text_trgm ON episodes USING gin ((COALESCE(people::text, '')) gin_trgm_ops)",
)
NEW_INDEX_NAMES = (
    "idx_episodes_topics_text_trgm", "idx_episodes_decisions_text_trgm",
    "idx_episodes_preferences_text_trgm", "idx_episodes_people_text_trgm",
)


def vocab(rng: random.Random, n: int = 6000) -> list[str]:
    letters = "abcdefghijklmnopqrstuvwxyz"
    return ["".join(rng.choice(letters) for _ in range(rng.randint(3, 10))) for _ in range(n)]


def words(rng, v, k):
    # Zipf-ish: low indexes are common words.
    return [v[min(len(v) - 1, int(rng.paretovariate(0.9)) - 1 + rng.randint(0, 40))] for _ in range(k)]


def episode_row(rng, v, i):
    proj = "church-harness" if i < N_BIG_PROJECT else f"project-{i % OTHER_PROJECTS}"
    summary = " ".join(words(rng, v, rng.randint(120, 260)))
    topics = [" ".join(words(rng, v, 2)) for _ in range(rng.randint(2, 8))]
    decisions = [" ".join(words(rng, v, 14)) for _ in range(rng.randint(0, 4))]
    prefs = [" ".join(words(rng, v, 10)) for _ in range(rng.randint(0, 2))]
    people = [" ".join(words(rng, v, 2)) for _ in range(rng.randint(0, 2))]
    return (f"codex:s{i}", summary, json.dumps(topics), json.dumps(decisions),
            json.dumps(prefs), json.dumps(people), proj)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def main() -> int:
    from khipu import cli
    from tests.hermetic_pg import Cluster, chain_dir, _free_port
    from khipu import migrate
    import psycopg

    root = Path(tempfile.mkdtemp(prefix="khipu-literal-bench-"))
    cluster = Cluster(root, _free_port())
    cluster.start()
    try:
        with cluster.admin() as a:
            a.execute("CREATE DATABASE bench")
        conn = psycopg.Connection.connect(cluster.dsn("bench"))
        res = migrate.run(conn=conn, directory=chain_dir(root, "0040"))
        assert not res["pending"], res
        rng = random.Random(11)
        v = vocab(rng)
        cur = conn.cursor()
        with cur.copy("COPY episodes (ts, session_id, summary, topics, decisions, preferences, people, "
                      "raw, harness, project) FROM STDIN") as cp:
            for i in range(N_EPISODES):
                s, summ, t, d, p, pe, proj = episode_row(rng, v, i)
                cp.write_row(("2026-01-01 00:00:00+00", s, summ, t, d, p, pe, "{}", "codex", proj))
        conn.commit()
        cur.execute("ANALYZE episodes")
        conn.commit()
        print("rows", N_EPISODES)
        # Queries: a mix of selectivities drawn from the vocabulary.
        qrng = random.Random(5)
        terms = [v[qrng.randint(0, 3000)] for _ in range(30)] + \
                [f"{v[qrng.randint(0, 200)]} {v[qrng.randint(0, 3000)]}" for _ in range(20)] + \
                [f"{v[qrng.randint(0, 50)]} {v[qrng.randint(0, 800)]} {v[qrng.randint(0, 3000)]}" for _ in range(10)]

        def literal_times():
            ts = []
            for _ in range(REPS):
                for t in terms:
                    c = conn.cursor()
                    t0 = time.perf_counter()
                    cli._search_query(c, t, 10, kind="episode")
                    ts.append((time.perf_counter() - t0) * 1000)
                    conn.rollback()
            return ts

        def insert_times():
            ts = []
            c = conn.cursor()
            for i in range(INSERTS):
                s, summ, t, d, p, pe, proj = episode_row(rng, v, i)
                t0 = time.perf_counter()
                c.execute("INSERT INTO episodes (ts, session_id, summary, topics, decisions, preferences, "
                          "people, raw, harness, project) VALUES (now(), %s, %s, %s::jsonb, %s::jsonb, "
                          "%s::jsonb, %s::jsonb, '{}'::jsonb, 'codex', %s)",
                          (s, summ, t, d, p, pe, proj))
                conn.commit()
                ts.append((time.perf_counter() - t0) * 1000)
            return ts

        def report(label, lt, it):
            print(f"{label}: literal p50 {pct(lt, .5):.1f} ms  p95 {pct(lt, .95):.1f} ms  max {max(lt):.1f} | "
                  f"insert mean {statistics.mean(it):.2f} ms  p50 {pct(it, .5):.2f}  p95 {pct(it, .95):.2f}  "
                  f"max {max(it):.1f}")

        # Project filter materialised CTE cost (slice 3's shape).
        c = conn.cursor()
        t0 = time.perf_counter()
        for _ in range(10):
            c.execute("SELECT count(*) FROM (SELECT id FROM episodes WHERE COALESCE(project, scope) "
                      "ILIKE '%church-harness%') x")
            c.fetchone()
        print(f"project substring CTE: {(time.perf_counter() - t0) * 100:.1f} ms avg")
        conn.rollback()

        base_l, base_i = literal_times(), insert_times()
        report("without indexes", base_l, base_i)
        for sql in NEW_INDEXES:
            t0 = time.perf_counter()
            conn.autocommit = True
            conn.execute(sql.replace("CREATE INDEX", "CREATE INDEX CONCURRENTLY"))
            conn.autocommit = False
            print(f"  built in {(time.perf_counter() - t0):.2f}s")
        conn.execute("ANALYZE episodes")
        conn.commit()
        idx_l, idx_i = literal_times(), insert_times()
        report("with indexes   ", idx_l, idx_i)
        c.execute("EXPLAIN SELECT id FROM episodes WHERE deleted_at IS NULL AND ("
                  "summary ILIKE '%abc%' OR COALESCE(topics::text,'') ILIKE '%abc%' OR "
                  "COALESCE(decisions::text,'') ILIKE '%abc%' OR COALESCE(preferences::text,'') ILIKE '%abc%' "
                  "OR COALESCE(people::text,'') ILIKE '%abc%')")
        print("\n".join(r[0] for r in c.fetchall()))
        print("insert mean delta ms:", round(statistics.mean(idx_i) - statistics.mean(base_i), 2))
        return 0
    finally:
        cluster.stop()


if __name__ == "__main__":
    sys.exit(main())
