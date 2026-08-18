"""Tests for khipu.graph_sync — graph.sqlite → PG mirror + drift.

Pure parts run against a temp SQLite. The live parts (skipped when PG or the
real graph.sqlite is unreachable) run the sync in dry-run mode — one transaction,
rolled back — and assert the drift check is zero, i.e. what the nightly leaves
behind is what doctor calls ok.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from khipu import graph_sync as gs


def _pg_and_sqlite_available() -> bool:
    from khipu.config import path_setting
    sqlite = path_setting("graph_sqlite")
    if sqlite is None or not sqlite.is_file():
        return False
    try:
        from khipu.db import connect
        with connect() as c, c.cursor() as cur:
            cur.execute("select 1")
        return True
    except Exception:  # noqa: BLE001
        return False


class ReadSqliteTest(unittest.TestCase):
    def test_reads_and_normalises_like_the_p1_export(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.sqlite"
            con = sqlite3.connect(p)
            con.executescript(
                """
                CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, bucket TEXT, name TEXT,
                                    payload TEXT, source_path TEXT, built_at TEXT, frozen INTEGER);
                CREATE TABLE edges (src TEXT, dst TEXT, type TEXT, weight REAL, payload TEXT, built_at TEXT);
                INSERT INTO nodes VALUES ('a','concept','code','A','{"k":1}','x.py','2026-08-17T06:17:39+00:00',0);
                INSERT INTO nodes VALUES ('b','concept','',  '', 'not json', '', '', 1);
                INSERT INTO nodes VALUES ('c','concept',NULL,NULL,NULL,NULL,NULL,0);
                INSERT INTO edges VALUES ('a','b','uses',NULL,NULL,'2026-08-17T06:17:39+00:00');
                """
            )
            con.commit()
            con.close()
            nodes, edges = gs._read_sqlite(p)
            self.assertEqual(len(nodes), 3)
            a = dict(zip(("id", "type", "bucket", "name", "payload", "source_path", "built_at", "frozen"), nodes[0]))
            self.assertEqual((a["bucket"], a["payload"], a["frozen"]), ("code", '{"k":1}', False))
            b = dict(zip(("id", "type", "bucket", "name", "payload", "source_path", "built_at", "frozen"), nodes[1]))
            self.assertIsNone(b["bucket"])                          # '' -> NULL, as P1 did
            self.assertEqual(json.loads(b["payload"]), {"_raw": "not json"})
            self.assertTrue(b["frozen"])
            self.assertIsNone(b["built_at"])
            self.assertEqual(edges[0][:3], ("a", "b", "uses"))

    def test_json_or_wrapped(self):
        self.assertIsNone(gs._json_or_wrapped(None))
        self.assertIsNone(gs._json_or_wrapped(""))
        self.assertEqual(gs._json_or_wrapped('{"a":1}'), '{"a":1}')
        self.assertEqual(json.loads(gs._json_or_wrapped("plain")), {"_raw": "plain"})

    def test_ownership_sql_is_null_safe(self):
        # The rule must be a real boolean for rows with NULL bucket / source_path
        # (6,611 graphify nodes have NULL source_path — see module docstring).
        self.assertIn("COALESCE(n.source_path", gs.KHIPU_OWNED_NODE_SQL)
        self.assertIn("COALESCE(n.bucket", gs.KHIPU_OWNED_NODE_SQL)

    def test_missing_sqlite_is_red_on_the_producer_and_n_a_elsewhere(self):
        """Audit 2026-08-17: an unconditional red made `khipu doctor` permanently
        failing on the second Mac, which never builds the graph."""
        with mock.patch.object(gs, "is_graph_producer", return_value=True):
            d = gs.graph_drift(Path("/nonexistent/graph.sqlite"))
            self.assertFalse(d["ok"])
            self.assertIn("error", d)
        with mock.patch.object(gs, "is_graph_producer", return_value=False):
            d = gs.graph_drift(Path("/nonexistent/graph.sqlite"))
            self.assertTrue(d["ok"])
            self.assertIn("skipped", d)
        with self.assertRaises(FileNotFoundError):
            gs.sync_from_sqlite(Path("/nonexistent/graph.sqlite"), dry_run=True)


@unittest.skipUnless(_pg_and_sqlite_available(), "PG or graph.sqlite unreachable")
class LiveGraphMirrorTest(unittest.TestCase):
    def test_dry_run_is_transactional_and_live_drift_is_zero(self):
        before = gs.graph_drift()
        out = gs.sync_from_sqlite(dry_run=True)          # rolls back
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["sqlite_dangling_edges"], 0)
        after = gs.graph_drift()
        for k in ("pg_graphify_nodes", "pg_graphify_edges", "pg_khipu_nodes"):
            self.assertEqual(before[k], after[k], k)      # dry run changed nothing
        # The mirror is wired into graphify's nightly; between nightlies the live
        # PG must match graph.sqlite exactly. Not a soft check.
        self.assertTrue(after["ok"], {k: v for k, v in after.items() if not k.startswith("sample")})

    def test_cli_check_exits_zero_on_zero_drift(self):
        import subprocess
        import sys
        r = subprocess.run([sys.executable, "-m", "khipu.cli", "graph-sync", "--check"],
                           capture_output=True, text=True, timeout=300,
                           env=dict(os.environ, PYTHONPATH=os.environ.get("PYTHONPATH", "")))
        self.assertEqual(r.returncode, 0, r.stdout[-400:] + r.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
