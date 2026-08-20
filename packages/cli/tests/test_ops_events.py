"""ops_events.record — parameterized INSERT only (no string-built SQL)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from khipu import ops_events


class OpsEventsTest(unittest.TestCase):
    def test_record_uses_parameterized_insert_and_returns_kind(self):
        created = datetime(2026, 8, 19, tzinfo=timezone.utc)
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = (42, created)
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        conn.cursor.return_value.__exit__.return_value = False
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False

        with mock.patch("khipu.db.connect", return_value=conn):
            out = ops_events.record("graph_snapshot", "ok", {"path": "/tmp/x.sqlite"})

        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        self.assertIn("%s", sql)
        self.assertNotIn("/tmp/x.sqlite", sql)
        self.assertEqual(params[0], "graph_snapshot")
        self.assertEqual(params[1], "ok")
        self.assertEqual(json.loads(params[2]), {"path": "/tmp/x.sqlite"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], "graph_snapshot")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["id"], 42)
        conn.commit.assert_called_once()

    def test_invalid_status_raises(self):
        with self.assertRaises(ValueError):
            ops_events.record("graph_snapshot", "banana")


if __name__ == "__main__":
    unittest.main()
