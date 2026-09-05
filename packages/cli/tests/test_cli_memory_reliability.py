"""Tests for the memory-reliability CLI additions — `khipu owed`, `khipu
episode forget`, `khipu topic purge`, `khipu backfill identity`, `khipu
hygiene paths`. New subcommands only (khipu.cli's shared search/status/doctor
internals are a concurrent change's territory); a separate test file keeps
this from colliding with edits to test_cli.py.

No live database: khipu.db.connect is stubbed with a small fake connection/
cursor per test.
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from khipu import cli


class _FakeCur:
    def __init__(self, script):
        """script: list of (sql_prefix, result) pairs consumed in order for
        fetchone/fetchall; execute just records the call."""
        self.script = list(script)
        self.calls = []
        self.rowcount = 0
        self._last = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.calls.append((s, params))
        for i, (prefix, result) in enumerate(self.script):
            if s.startswith(prefix):
                self._last = result
                if isinstance(result, dict) and "rowcount" in result:
                    self.rowcount = result["rowcount"]
                del self.script[i]
                return
        self._last = None

    def fetchone(self):
        if isinstance(self._last, dict):
            return self._last.get("row")
        return self._last

    def fetchall(self):
        if isinstance(self._last, dict):
            return self._last.get("rows", [])
        return self._last or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(argv, cur):
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = args.func(args)
    return rc, json.loads(out.getvalue())


class OwedCommandTest(unittest.TestCase):
    def test_list_default_open(self):
        cur = _FakeCur([("SELECT id, text, project", {"rows": [
            (1, "follow up", "acme/widget", None, "followup", 10, "t0", None,
             "open", None, None, None),
        ]})])
        rc, out = _run(["owed"], cur)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "follow up")
        sql, params = cur.calls[-1]
        self.assertIn("status = %s", sql)
        self.assertEqual(params[0], "open")

    def test_close_by_id(self):
        cur = _FakeCur([("UPDATE commitments SET status = %s", {"rowcount": 1})])
        rc, out = _run(["owed", "--close", "7"], cur)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "closed")

    def test_reopen_missing_id_reports_not_ok(self):
        cur = _FakeCur([("UPDATE commitments SET status = %s", {"rowcount": 0})])
        rc, out = _run(["owed", "--reopen", "99"], cur)
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])


class OwedSnoozeCommandTest(unittest.TestCase):
    """`khipu owed --snooze ID --until …` (desktop overhaul phase 4)."""

    def test_compact_window_becomes_a_sql_interval(self):
        cur = _FakeCur([("UPDATE commitments SET due_after", {"rowcount": 1})])
        rc, out = _run(["owed", "--snooze", "5", "--until", "7d"], cur)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        sql, params = cur.calls[-1]
        self.assertIn("now() + interval '7 days'", sql)
        # Only the id is bound: the interval is computed in SQL, on the
        # transaction's clock, never spliced from user text.
        self.assertEqual(tuple(params), (5,))

    def test_iso_date_is_bound_as_a_parameter(self):
        cur = _FakeCur([("UPDATE commitments SET due_after", {"rowcount": 1})])
        rc, out = _run(["owed", "--snooze", "9", "--until", "2026-09-30"], cur)
        self.assertEqual(rc, 0)
        sql, params = cur.calls[-1]
        self.assertIn("%s::timestamptz", sql)
        self.assertEqual(tuple(params), ("2026-09-30", 9))

    def test_free_text_is_refused_rather_than_clearing_the_due_date(self):
        cur = _FakeCur([])
        rc, out = _run(["owed", "--snooze", "5", "--until", "after the release"], cur)
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertEqual(cur.calls, [])

    def test_missing_id_reports_not_ok(self):
        cur = _FakeCur([("UPDATE commitments SET due_after", {"rowcount": 0})])
        rc, out = _run(["owed", "--snooze", "404", "--until", "2 weeks"], cur)
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])


class EpisodeEditCommandTest(unittest.TestCase):
    """`khipu episode edit ID --summary TEXT` (desktop overhaul phase 3)."""

    def test_edit_updates_the_summary_and_reembeds(self):
        cur = _FakeCur([("UPDATE episodes SET summary", {"rowcount": 1})])
        with mock.patch.object(cli, "_reembed_episode", return_value=True) as m:
            rc, out = _run(["episode", "edit", "42", "--summary", "corrected"], cur)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["reembedded"])
        m.assert_called_once()
        sql, params = cur.calls[0]
        self.assertTrue(sql.startswith("UPDATE episodes SET summary"))
        self.assertEqual(params, ("corrected", 42))

    def test_reembed_failure_still_keeps_the_correction(self):
        cur = _FakeCur([("UPDATE episodes SET summary", {"rowcount": 1})])
        with mock.patch.object(cli, "_reembed_episode", side_effect=RuntimeError("no key")):
            rc, out = _run(["episode", "edit", "42", "--summary", "corrected"], cur)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertFalse(out["reembedded"])

    def test_unknown_episode_reports_not_ok(self):
        cur = _FakeCur([("UPDATE episodes SET summary", {"rowcount": 0})])
        with mock.patch.object(cli, "_reembed_episode", return_value=True) as m:
            rc, out = _run(["episode", "edit", "404", "--summary", "corrected"], cur)
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        m.assert_not_called()

    def test_blank_summary_is_refused(self):
        cur = _FakeCur([])
        rc, out = _run(["episode", "edit", "42", "--summary", "   "], cur)
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertEqual(cur.calls, [])


class EpisodeForgetCommandTest(unittest.TestCase):
    def test_forget_soft_deletes_and_removes_embeddings(self):
        cur = _FakeCur([
            ("UPDATE episodes SET deleted_at", {"rowcount": 1}),
            ("DELETE FROM memory_embeddings WHERE kind = 'episode'", {"rowcount": 3}),
        ])
        rc, out = _run(["episode", "forget", "42"], cur)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["soft_deleted"])
        self.assertEqual(out["embeddings_removed"], 3)

    def test_already_deleted_episode_reports_false_but_ok(self):
        cur = _FakeCur([
            ("UPDATE episodes SET deleted_at", {"rowcount": 0}),
            ("DELETE FROM memory_embeddings WHERE kind = 'episode'", {"rowcount": 0}),
        ])
        rc, out = _run(["episode", "forget", "42"], cur)
        self.assertEqual(rc, 0)
        self.assertFalse(out["soft_deleted"])


class TopicPurgeCommandTest(unittest.TestCase):
    def test_refuses_without_yes(self):
        parser = cli.build_parser()
        args = parser.parse_args(["topic", "purge", "some-slug"])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = args.func(args)
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_refuses_a_topic_that_is_not_tombstoned(self):
        cur = _FakeCur([("SELECT deleted_at FROM topics", {"row": (None,)})])
        rc, out = _run(["topic", "purge", "live-topic", "--yes"], cur)
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("not tombstoned", out["error"])

    def test_refuses_a_missing_topic(self):
        cur = _FakeCur([("SELECT deleted_at FROM topics", {"row": None})])
        rc, out = _run(["topic", "purge", "no-such-slug", "--yes"], cur)
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])

    def test_purges_a_tombstoned_topic(self):
        cur = _FakeCur([
            ("SELECT deleted_at FROM topics", {"row": ("2026-08-01T00:00:00Z",)}),
            ("DELETE FROM memory_embeddings WHERE kind = 'topic'", {"rowcount": 2}),
            ("DELETE FROM topic_revisions", {"rowcount": 5}),
            ("DELETE FROM topics WHERE slug", {"rowcount": 1}),
        ])
        rc, out = _run(["topic", "purge", "dead-topic", "--yes"], cur)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["revisions_removed"], 5)
        self.assertEqual(out["embeddings_removed"], 2)


class BackfillIdentityCommandTest(unittest.TestCase):
    def test_dry_run_is_the_default(self):
        cur = _FakeCur([
            ("SELECT id, scope, session_id FROM episodes", {"rows": [(1, "/abs/x", "s1")]}),
            ("SELECT COUNT(*) FROM episodes", {"row": (1,)}),
        ])
        rc, out = _run(["backfill", "identity"], cur)
        self.assertEqual(rc, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["would_backfill"], 1)
        self.assertFalse(cur.calls and any("UPDATE" in s for s, _ in cur.calls))

    def test_apply_calls_the_destructive_path(self):
        cur = _FakeCur([
            ("SELECT id, scope FROM episodes", {"rows": []}),
        ])
        rc, out = _run(["backfill", "identity", "--apply"], cur)
        self.assertEqual(rc, 0)
        self.assertFalse(out["dry_run"])
        self.assertEqual(out["scanned"], 0)


class HygienePathsCommandTest(unittest.TestCase):
    def test_dry_run_reports_without_deleting(self):
        cur = _FakeCur([("SELECT id FROM nodes WHERE type = 'path'", {"rows": [("path:a/b",)]})])
        rc, out = _run(["hygiene", "paths"], cur)
        self.assertEqual(rc, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["failing"], 1)
        self.assertFalse(any("DELETE" in s for s, _ in cur.calls))

    def test_apply_deletes_failing_nodes(self):
        cur = _FakeCur([
            ("SELECT id FROM nodes WHERE type = 'path'", {"rows": [("path:a/b",)]}),
            ("DELETE FROM edges", {"rowcount": 0}),
            ("DELETE FROM nodes", {"rowcount": 1}),
        ])
        rc, out = _run(["hygiene", "paths", "--apply"], cur)
        self.assertEqual(rc, 0)
        self.assertFalse(out["dry_run"])
        self.assertEqual(out["deleted_nodes"], 1)


if __name__ == "__main__":
    unittest.main()
