"""khipu.forget — a forget reaches the row, its vectors, its commitments and
the legacy file (audit 2026-09-04: it used to stop at the vectors)."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from khipu import forget


class _Cur:
    def __init__(self, row):
        self.row = row
        self.sql: list[str] = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.rowcount = 1 if "UPDATE" in sql or "DELETE" in sql else 0

    def fetchone(self):
        return self.row


class ForgetEpisodeTest(unittest.TestCase):
    def test_touches_row_vectors_commitments_and_reports_identity(self):
        ts = datetime(2026, 9, 5, 12, 20, 17, tzinfo=timezone.utc)
        cur = _Cur((ts, "Shipped 0.4.0", "claude_code:abc"))
        out = forget.forget_episode(cur, 11617)
        self.assertTrue(out["ok"])
        self.assertTrue(out["soft_deleted"])
        self.assertEqual(out["commitments_closed"], 1)
        self.assertTrue(any("UPDATE episodes SET deleted_at" in s for s in cur.sql))
        self.assertTrue(any("kind = 'episode'" in s for s in cur.sql))
        self.assertTrue(any("close_reason = 'forgotten'" in s for s in cur.sql))
        self.assertTrue(any("kind = 'commitment'" in s for s in cur.sql))
        self.assertEqual(out["identity"]["summary_md5"], hashlib.md5(b"Shipped 0.4.0").hexdigest())
        self.assertEqual(out["session_id"], "claude_code:abc")

    def test_unknown_episode_is_reported_not_raised(self):
        cur = _Cur(None)
        out = forget.forget_episode(cur, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(len(cur.sql), 1)


class LegacyFileTest(unittest.TestCase):
    def test_removes_the_line_with_a_backup_and_leaves_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = {"ts": "2026-09-05T11:00:00Z", "summary": "keep me"}
            gone = {"ts": "2026-09-05T12:20:17Z", "summary": "forget me"}
            (root / "episodes.jsonl").write_text(
                json.dumps(keep) + "\n" + json.dumps(gone) + "\n", encoding="utf-8"
            )
            md5 = hashlib.md5(b"forget me").hexdigest()
            out = forget.forget_in_legacy_file(root, "2026-09-05T12:20:17+00:00", md5)
            self.assertEqual(out["removed"], 1)
            self.assertTrue(Path(out["backup"]).is_file())
            self.assertIn("forget me", Path(out["backup"]).read_text())
            left = (root / "episodes.jsonl").read_text()
            self.assertIn("keep me", left)
            self.assertNotIn("forget me", left)
            again = forget.forget_in_legacy_file(root, "2026-09-05T12:20:17+00:00", md5)
            self.assertEqual(again["removed"], 0)
            self.assertEqual(len(list((root / ".khipu-forget-backups").iterdir())), 1)

    def test_unconfigured_root_is_a_no_op(self):
        self.assertEqual(forget.forget_in_legacy_file(None, "x", "y")["removed"], 0)
