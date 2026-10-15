"""`khipu doctor` speed work (2026-10-15): each rewrite gives the same answer
as before and fails here in its old form.

  * episodes_missing_in_pg asks in one statement, not one per 500 keys.
  * a passing snapshot integrity check is remembered for that exact file.
  * doctor plans the memory space once (coverage keeps the gaps, the topic
    lag reuses them) and from the fingerprint cache.
  * the slow hub checks run at the same time, and --probe waits for them.
  * graph_drift reads nodes and edges on two connections at once.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from khipu import cli, drift, embed, graph_backup


class _RecordingCursor:
    def __init__(self, rows=()):
        self.calls: list[tuple[str, object]] = []
        self.rows = list(rows)

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return list(self.rows)


class EpisodesMissingOneStatementTest(unittest.TestCase):
    def test_1200_keys_are_one_statement_in_input_order(self):
        keys = [(f"2026-10-15T00:{i // 60:02d}:{i % 60:02d}Z", f"{i:032x}") for i in range(1200)]
        cur = _RecordingCursor(rows=[keys[7], keys[3]])
        # Pre-0027 shape; the episode_sources probe is its own statement.
        with mock.patch("khipu.capture_identity.sources_ready", return_value=False):
            out = drift.episodes_missing_in_pg(cur, keys)
        self.assertEqual(len(cur.calls), 1, "one round trip, not one per 500 keys")
        sql, params = cur.calls[0]
        self.assertIn("unnest(%s::text[], %s::text[]) WITH ORDINALITY", sql)
        self.assertIn("e.ts = v.ts::timestamptz AND md5(e.summary) = v.md", sql)
        self.assertTrue(sql.endswith("ORDER BY v.ord"))
        self.assertEqual(params, ([k[0] for k in keys], [k[1] for k in keys]))
        self.assertEqual(out, [keys[7], keys[3]])

    def test_no_keys_asks_nothing(self):
        cur = _RecordingCursor()
        self.assertEqual(drift.episodes_missing_in_pg(cur, []), [])
        self.assertEqual(cur.calls, [])


class IntegrityVerdictIsRememberedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap = self.root / "graph-20261015T000000Z.sqlite"
        con = sqlite3.connect(self.snap)
        con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE edges (src TEXT, dst TEXT, type TEXT)")
        con.commit()
        con.close()
        cache = self.root / "cache" / "integrity.json"
        p = mock.patch.object(graph_backup, "_integrity_cache_file", return_value=cache)
        p.start()
        self.addCleanup(p.stop)

    def _health(self):
        return graph_backup.local_health(
            producer=True, snapshot_dir=self.root, now=datetime.now(timezone.utc)
        )

    def test_unchanged_snapshot_is_checked_once(self):
        with mock.patch.object(graph_backup, "_integrity_check", return_value="ok") as check:
            first, second = self._health(), self._health()
        self.assertEqual(check.call_count, 1)
        self.assertTrue(first["ok"] and second["ok"], (first, second))
        self.assertEqual(first["integrity_checked_at"], second["integrity_checked_at"])

    def test_a_changed_snapshot_is_checked_again(self):
        with mock.patch.object(graph_backup, "_integrity_check", return_value="ok") as check:
            self._health()
            time.sleep(0.01)
            con = sqlite3.connect(self.snap)
            con.execute("INSERT INTO nodes VALUES ('x')")
            con.commit()
            con.close()
            self._health()
        self.assertEqual(check.call_count, 2)

    def test_a_failing_verdict_is_never_remembered(self):
        with mock.patch.object(graph_backup, "_integrity_check", return_value="error: malformed") as check:
            first, second = self._health(), self._health()
        self.assertEqual(check.call_count, 2)
        self.assertFalse(first["ok"] or second["ok"])

    def test_a_day_old_verdict_is_checked_again(self):
        with mock.patch.object(graph_backup, "_integrity_check", return_value="ok") as check:
            self._health()
            later = datetime.now(timezone.utc) + timedelta(hours=25)
            with mock.patch.object(graph_backup, "_utcnow", return_value=later):
                self._health()
        self.assertEqual(check.call_count, 2)


    def test_a_write_during_the_check_is_not_remembered_as_checked(self):
        # Cross-family review: the identity was read after the check, so a
        # file changed while it was checked kept the old pass.
        writes = []

        def check_then_write(path):
            writes.append(1)
            con = sqlite3.connect(path)
            con.execute("INSERT INTO nodes VALUES (?)", (f"written during check {len(writes)}",))
            con.commit()
            con.close()
            return "ok"

        with mock.patch.object(graph_backup, "_integrity_check", side_effect=check_then_write) as check:
            self._health()
            self._health()
        self.assertEqual(check.call_count, 2)

    def test_a_snapshot_with_a_wal_beside_it_is_never_remembered(self):
        # Committed WAL pages change the database without touching the main
        # file's size or times, so a verdict for it cannot be remembered.
        Path(f"{self.snap}-wal").write_bytes(b"x")
        with mock.patch.object(graph_backup, "_integrity_check", return_value="ok") as check:
            self._health()
            self._health()
        self.assertEqual(check.call_count, 2)

class MemoryPlannedOnceTest(unittest.TestCase):
    def test_memory_gaps_can_use_the_fingerprint_cache(self):
        plan = {("topic", "a", 0): ("h", 1)}
        with mock.patch.object(embed, "memory_chunk_plan", return_value=plan) as planned, \
                mock.patch.object(embed, "_existing_hashes", return_value={}):
            _plan, missing, stale = embed.memory_gaps(None, "p@1", cached=True)
        planned.assert_called_once_with(None, cached=True)
        self.assertEqual((missing, stale), ([("topic", "a", 0)], []))

    def test_topic_lag_reuses_the_gaps_coverage_kept(self):
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = (None,)
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        kept = {"profile": "p@1", "missing": [("topic", "t1", 0), ("episode", "9", 0)], "stale": []}
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(embed, "_resolve_profile", return_value="p@1"), \
                mock.patch.object(embed, "memory_gaps", side_effect=AssertionError("planned twice")):
            out = embed.topics_embed_lag_minutes(gaps=kept)
        self.assertTrue(out["ok"])
        sql, params = cur.execute.call_args[0]
        self.assertEqual(params, (["t1"], "p@1"))

    def test_gaps_for_another_profile_are_not_reused(self):
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = (None,)
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        kept = {"profile": "other@1", "missing": [("topic", "t1", 0)], "stale": []}
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(embed, "_resolve_profile", return_value="p@1"), \
                mock.patch.object(embed, "memory_gaps", return_value=({}, [], [])) as gaps:
            embed.topics_embed_lag_minutes(gaps=kept)
        gaps.assert_called_once()

    def test_doctor_embed_checks_plan_once_from_the_cache(self):
        seen = {}

        def fake_coverage(**kw):
            seen.update(kw)
            kw["keep_gaps"].update(profile="p@1", missing=[], stale=[])
            return {"episodes": {"missing": 0}, "topics": {"missing": 0}, "missing": 0, "stale": 0}

        with mock.patch("khipu.embed.coverage", side_effect=fake_coverage), \
                mock.patch("khipu.embed.topics_embed_lag_minutes",
                           return_value={"ok": True, "lag_minutes": 0}) as lag:
            cov, cov_ok, topics = cli._doctor_embed_checks()
        self.assertTrue(cov_ok)
        self.assertTrue(seen["detail"] and seen["cached_plan"])
        lag.assert_called_once_with(gaps={"profile": "p@1", "missing": [], "stale": []})


class DoctorHubChecksRunTogetherTest(unittest.TestCase):
    def setUp(self):
        from tests.test_cli_recall_quality import _green_doctor_patches

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [p for p in _green_doctor_patches() if p.attribute != "path_setting"]
        patches += [
            mock.patch("khipu.config.path_setting",
                       side_effect=lambda key, *a, **k: Path("/x/graph.sqlite") if key == "graph_sqlite" else None),
            mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}),
            mock.patch("khipu.drift.recall_quality", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _doctor(self, argv):
        args = cli.build_parser().parse_args(["doctor", "--memory-root", self.tmp.name, *argv])
        with redirect_stdout(io.StringIO()) as out:
            cli.cmd_doctor(args)
        return json.loads(out.getvalue())

    def test_drift_graph_and_coverage_are_waited_on_together(self):
        together = threading.Barrier(3, timeout=5)

        def meet(result):
            def run(*_a, **_k):
                together.wait()
                return result
            return run

        clean_drift = {"episodes_missing_in_pg": 0, "topic_mismatches": [], "topic_files_unreadable": []}
        coverage = {"episodes": {"missing": 0}, "topics": {"missing": 0}}
        with mock.patch("khipu.drift.sample_drift", side_effect=meet(clean_drift)), \
                mock.patch("khipu.graph_sync.graph_drift", side_effect=meet({"ok": True})), \
                mock.patch("khipu.embed.coverage", side_effect=meet(coverage)):
            out = self._doctor([])
        # Run one after another, the first to arrive waits out the barrier and
        # every one of the three comes back as an error.
        self.assertEqual(out["drift"], clean_drift)
        self.assertEqual(out["graph_drift"], {"ok": True})
        self.assertEqual(out["embed_coverage"], coverage)
        self.assertTrue(out["drift_ok"] and out["graph_drift_ok"] and out["embed_coverage_ok"])

    def test_probe_waits_for_the_hub_checks(self):
        done = {}

        def slow(name, result):
            def run(*_a, **_k):
                time.sleep(0.3)
                done[name] = time.monotonic()
                return result
            return run

        started = {}

        def probe(_harness):
            started["probe"] = time.monotonic()
            return {"ok": True}

        with mock.patch("khipu.drift.sample_drift", side_effect=slow("drift", {
                    "episodes_missing_in_pg": 0, "topic_mismatches": [], "topic_files_unreadable": []})), \
                mock.patch("khipu.graph_sync.graph_drift", side_effect=slow("graph", {"ok": True})), \
                mock.patch("khipu.embed.coverage", side_effect=slow("coverage", {
                    "episodes": {"missing": 0}, "topics": {"missing": 0}})), \
                mock.patch("khipu.probe.run_probe", side_effect=probe), \
                mock.patch("khipu.probe.harness_results", return_value={}):
            self._doctor(["--probe"])
        self.assertEqual(set(done), {"drift", "graph", "coverage"})
        self.assertGreaterEqual(started["probe"], max(done.values()))


class GraphDriftTwoConnectionsTest(unittest.TestCase):
    def test_nodes_and_edges_are_read_at_the_same_time(self):
        from khipu import graph_sync

        together = threading.Barrier(2, timeout=5)

        class Cur:
            def __init__(self):
                self.rows = []

            def execute(self, sql, params=None):
                flat = " ".join(sql.split())
                if "SELECT n.id, n.type" in flat:
                    together.wait()
                    self.rows = [("n1", "concept", "code", "Name", {"a": 1}, "x.py",
                                  datetime(2026, 1, 1, tzinfo=timezone.utc), False, None)]
                elif "SELECT COUNT(*)" in flat:
                    self.rows = [(0,)]
                elif "SELECT e.src" in flat:
                    together.wait()
                    self.rows = [("n1", "n1", "uses", 1.0, {"z": 1}, None)]

            def fetchall(self):
                return list(self.rows)

            def fetchone(self):
                return self.rows[0]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Conn:
            def __init__(self):
                self.cur = Cur()

            def cursor(self):
                return self.cur

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "graph.sqlite"
            con = sqlite3.connect(p)
            con.executescript("""
            CREATE TABLE nodes(id TEXT,type TEXT,bucket TEXT,name TEXT,payload TEXT,source_path TEXT,built_at TEXT,frozen INTEGER);
            CREATE TABLE edges(src TEXT,dst TEXT,type TEXT,weight REAL,payload TEXT,built_at TEXT);
            INSERT INTO nodes VALUES('n1','concept','code','Name','{"a":1}','x.py','2026-01-01T00:00:00Z',0);
            INSERT INTO edges VALUES('n1','n1','uses',1,'{"z":1}',NULL);
            """)
            con.commit()
            con.close()
            with mock.patch.object(graph_sync, "is_graph_producer", return_value=True), \
                    mock.patch.object(graph_sync, "disabled_or_unreachable_ids", return_value=set()), \
                    mock.patch.object(graph_sync, "load_sources", return_value={"sources": []}), \
                    mock.patch.object(graph_sync, "owned_source_ids", return_value=set()), \
                    mock.patch.object(graph_sync, "_changed_since_last_mirror", return_value=False), \
                    mock.patch("khipu.db.connect", side_effect=lambda **_k: Conn()):
                out = graph_sync.graph_drift(p)
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["pg_graphify_nodes"], out["pg_graphify_edges"]), (1, 1))


class SecretsMigrationRunsOnceTest(unittest.TestCase):
    def test_two_threads_connecting_at_once_migrate_once(self):
        from khipu import db

        calls = []

        def migrate():
            calls.append(1)
            time.sleep(0.2)

        with mock.patch.object(db, "_MIGRATION_DONE", False), \
                mock.patch.dict("os.environ", {"KHIPU_DATABASE_URL": "", "ALZY_DATABASE_URL": ""}), \
                mock.patch("khipu.paths.isolated_dir", return_value=None), \
                mock.patch("khipu.keychain.migrate_legacy_secrets", side_effect=migrate), \
                mock.patch("khipu.keychain.get_dsn", return_value="postgresql://x/y"):
            threads = [threading.Thread(target=db.resolve_dsn) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
