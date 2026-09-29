"""khipu.forget — a forget reaches the row, its vectors, its commitments,
its decisions/deliverables, the local replica and the legacy file (audit
2026-09-04: it used to stop at the vectors; Phase 2, session A: extended
the cascade to decisions/deliverables/the sqlite replica, B3 in
docs/research/hindsight-plan-review-2026-09-28.md)."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from khipu import forget


class _Cur:
    """``columns`` maps a table name to the columns ``khipu.db.has_columns``
    should see for it (default {}: nothing migrated, i.e. every cascade step
    gated on a readiness probe is skipped — the pre-existing behavior this
    fixture always had). ``fetchall()`` only ever answers a readiness probe;
    every other call in ``forget_episode`` uses ``execute``/``rowcount`` or
    the fixed ``fetchone`` row, exactly as before."""

    def __init__(self, row, *, columns: dict[str, tuple[str, ...]] | None = None):
        self.row = row
        self.sql: list[str] = []
        self.rowcount = 0
        self.columns = columns or {}
        self._current: list[tuple] = []
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.clear()

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "information_schema.columns" in s:
            table = (params or (None,))[0]
            self._current = [(c,) for c in self.columns.get(table, ())]
            return
        self.sql.append(s)
        self.rowcount = 1 if "UPDATE" in sql or "DELETE" in sql else 0
        self._current = []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self._current)


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

    def test_pre_migration_hub_reports_zero_deliverables_and_decisions(self):
        """No deliverables/decisions columns on this fake hub (the default) —
        the cascade steps are skipped, not attempted and swallowed."""
        ts = datetime(2026, 9, 5, 12, 20, 17, tzinfo=timezone.utc)
        cur = _Cur((ts, "Shipped 0.4.0", "claude_code:abc"))
        out = forget.forget_episode(cur, 11617)
        self.assertEqual(out["deliverables_removed"], 0)
        self.assertEqual(out["decisions_retracted"], 0)
        self.assertFalse(any("DELETE FROM deliverables" in s for s in cur.sql))
        self.assertFalse(any("retract_reason = 'forgotten'" in s for s in cur.sql))

    def test_cascade_removes_deliverables_and_retracts_decisions(self):
        ts = datetime(2026, 9, 5, 12, 20, 17, tzinfo=timezone.utc)
        cur = _Cur(
            (ts, "Shipped 0.4.0", "claude_code:abc"),
            columns={
                "deliverables": ("id", "project", "kind"),
                "decisions": ("retracted_at", "retract_reason"),
            },
        )
        out = forget.forget_episode(cur, 11617)
        self.assertEqual(out["deliverables_removed"], 1)
        self.assertEqual(out["decisions_retracted"], 1)
        self.assertTrue(any("DELETE FROM deliverables" in s for s in cur.sql))
        self.assertTrue(any("retract_reason = 'forgotten'" in s for s in cur.sql))
        self.assertTrue(any("episode_id = %s" in s and "DELETE FROM deliverables" in s for s in cur.sql))


class ForgetEverywhereTest(unittest.TestCase):
    def test_touches_the_local_snapshot_too(self):
        ts = datetime(2026, 9, 5, 12, 20, 17, tzinfo=timezone.utc)
        hub_cur = _Cur((ts, "Shipped 0.4.0", "claude_code:abc"))
        hub_conn = mock.MagicMock()
        hub_conn.__enter__.return_value = hub_conn
        hub_conn.cursor.return_value.__enter__.return_value = hub_cur
        snapshot_out = {"ok": True, "episode_id": 11617, "updated": True}
        with mock.patch("khipu.db.connect", return_value=hub_conn), \
             mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot",
                         return_value=snapshot_out) as snap_fn, \
             mock.patch("khipu.config.path_setting", side_effect=Exception("no config")):
            out = forget.forget_everywhere(11617)
        snap_fn.assert_called_once_with(11617)
        self.assertEqual(out["snapshot"], snapshot_out)

    def test_a_failed_snapshot_write_does_not_fail_the_forget(self):
        """The hub write is already durable; the replica catching up later
        (or never, if this Mac has no snapshot) is not a forget failure."""
        ts = datetime(2026, 9, 5, 12, 20, 17, tzinfo=timezone.utc)
        hub_cur = _Cur((ts, "Shipped 0.4.0", "claude_code:abc"))
        hub_conn = mock.MagicMock()
        hub_conn.__enter__.return_value = hub_conn
        hub_conn.cursor.return_value.__enter__.return_value = hub_cur
        with mock.patch("khipu.db.connect", return_value=hub_conn), \
             mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot",
                         side_effect=RuntimeError("boom")), \
             mock.patch("khipu.config.path_setting", side_effect=Exception("no config")):
            out = forget.forget_everywhere(11617)
        self.assertTrue(out["ok"])
        self.assertFalse(out["snapshot"]["ok"])

    def test_unknown_episode_never_reaches_the_snapshot(self):
        hub_cur = _Cur(None)
        hub_conn = mock.MagicMock()
        hub_conn.__enter__.return_value = hub_conn
        hub_conn.cursor.return_value.__enter__.return_value = hub_cur
        with mock.patch("khipu.db.connect", return_value=hub_conn), \
             mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot") as snap_fn:
            out = forget.forget_everywhere(1)
        self.assertFalse(out["ok"])
        snap_fn.assert_not_called()


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
