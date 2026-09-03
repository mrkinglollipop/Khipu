"""Unit tests for khipu.drift — P2a / audit F2 (mirror-lag arithmetic).

Pure-arithmetic tests need no database. The one payload-shape test connects
read-only to the live Khipu Postgres (same DSN resolution as the CLI) and
skips cleanly if it's unreachable — never a hard failure, never a write.
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from khipu.drift import _lag_seconds, file_topic_hashes


def _pg_available() -> bool:
    try:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception:
        return False
    return True


PG_AVAILABLE = _pg_available()


class LagSecondsTest(unittest.TestCase):
    """`_lag_seconds` is the arithmetic behind both mirror_lag_seconds and
    time_since_last_capture_seconds — same helper, different (later, earlier)
    timestamp pairs. Covering it here covers both fields' math."""

    def test_positive_gap(self) -> None:
        earlier = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        later = earlier + timedelta(seconds=42)
        self.assertEqual(_lag_seconds(later, earlier), 42.0)

    def test_fractional_seconds(self) -> None:
        earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later = earlier + timedelta(milliseconds=1500)
        self.assertEqual(_lag_seconds(later, earlier), 1.5)

    def test_later_before_earlier_is_negative(self) -> None:
        # Clock skew / bad data should surface as a negative number, not crash
        # or silently clamp — truthful signals over comfortable ones.
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 - timedelta(seconds=5)
        self.assertEqual(_lag_seconds(t1, t0), -5.0)

    def test_none_later_returns_none(self) -> None:
        self.assertIsNone(_lag_seconds(None, datetime.now(timezone.utc)))

    def test_none_earlier_returns_none(self) -> None:
        self.assertIsNone(_lag_seconds(datetime.now(timezone.utc), None))

    def test_both_none_returns_none(self) -> None:
        self.assertIsNone(_lag_seconds(None, None))


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live status_payload check")
class StatusPayloadFieldsTest(unittest.TestCase):
    """F2: mirror_lag_seconds and time_since_last_capture_seconds must be
    separate, differently-named fields; the old conflated ingest_lag_seconds
    name must be gone."""

    def test_mirror_lag_and_capture_idle_are_separate_fields(self) -> None:
        from khipu.drift import status_payload

        payload = status_payload(None)
        self.assertIn("mirror_lag_seconds", payload)
        self.assertIn("time_since_last_capture_seconds", payload)
        self.assertNotIn("ingest_lag_seconds", payload)
        for key in ("mirror_lag_seconds", "time_since_last_capture_seconds"):
            value = payload[key]
            self.assertTrue(value is None or isinstance(value, float))


if __name__ == "__main__":
    unittest.main()


class EpisodeFilesTest(unittest.TestCase):
    """2026-08-17: the reconcile and drift must read every episodes*.jsonl, not
    just the main file, and must skip the 2020 'smoke' artifact by content."""

    def test_enumerates_main_and_siblings_but_not_bak(self):
        import tempfile
        from pathlib import Path

        from khipu.drift import episode_files, file_episode_keys

        root = Path(tempfile.mkdtemp())
        (root / "episodes.jsonl").write_text('{"ts":"2026-06-01T00:00:00Z","summary":"a"}\n')
        (root / "episodes_2026.jsonl").write_text('{"ts":"2026-05-01T00:00:00Z","summary":"b"}\n')
        (root / "episodes_2020.jsonl").write_text('{"ts":"2020-01-01T00:00:00+00:00","summary":"smoke"}\n')
        (root / "episodes.jsonl.bak-20260611").write_text('{"ts":"2026-01-01T00:00:00Z","summary":"old"}\n')
        names = [p.name for p in episode_files(root)]
        self.assertEqual(names[0], "episodes.jsonl")
        self.assertIn("episodes_2026.jsonl", names)
        self.assertIn("episodes_2020.jsonl", names)
        self.assertFalse(any(".bak" in n for n in names))
        keys = file_episode_keys(root)
        self.assertEqual(len(keys), 2)                       # smoke row excluded by content
        self.assertEqual({k[0][:4] for k in keys}, {"2026"})


class TopicHashCoverageTest(unittest.TestCase):
    """Regression: the topic drift pass defaulted to limit=25 over an
    alphabetical walk, so doctor compared the same first 4% of 622 topics on
    every run and the other 597 were never checked at all (audit 2026-08-17)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "topics").mkdir()
        for i in range(40):
            (self.root / "topics" / f"topic-{i:03d}.md").write_text(f"body {i}")
        (self.root / "topics" / "_retired.md").write_text("skip me")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_hashes_every_topic(self):
        hashes, unreadable = file_topic_hashes(self.root)
        self.assertEqual(len(hashes), 40)
        self.assertEqual(unreadable, [])
        self.assertIn("topic-039", hashes, "the alphabetical tail must be covered")

    def test_underscore_files_are_still_skipped(self):
        hashes, _ = file_topic_hashes(self.root)
        self.assertNotIn("_retired", hashes)

    def test_an_explicit_limit_still_caps_the_pass(self):
        hashes, _ = file_topic_hashes(self.root, limit=5)
        self.assertEqual(len(hashes), 5)

    def test_an_unreadable_topic_is_reported_not_raised(self):
        """One bad file used to abort the whole health report with a traceback."""
        bad = self.root / "topics" / "topic-007.md"

        real = pathlib.Path.read_text

        def boom(self, *a, **kw):
            if self.name == "topic-007.md":
                raise OSError("permission denied")
            return real(self, *a, **kw)

        with mock.patch.object(pathlib.Path, "read_text", boom):
            hashes, unreadable = file_topic_hashes(self.root)
        self.assertEqual(unreadable, [bad.name])
        self.assertEqual(len(hashes), 39)
        self.assertNotIn("topic-007", hashes)

    def test_a_topic_that_is_not_valid_utf8_is_reported_not_raised(self):
        """The hash moved to text mode to agree with the writer, which makes a
        decode error a real failure mode and not just a permissions one."""
        (self.root / "topics" / "topic-007.md").write_bytes(b"\xff\xfe binary")
        hashes, unreadable = file_topic_hashes(self.root)
        self.assertEqual(unreadable, ["topic-007.md"])
        self.assertNotIn("topic-007", hashes)

    def test_the_walk_hashes_exactly_what_the_writer_stores(self):
        """drift, revisions and mirror each had their own sha256. PR #43 moved
        this one to raw bytes, so a CRLF topic would have drifted from PG
        forever — the writer stores the newline-translated hash."""
        from khipu.mirror import parse_topic_file

        path = self.root / "topics" / "topic-007.md"
        path.write_bytes(b"---\ntitle: CRLF\n---\r\nbody line\r\n")
        hashes, _ = file_topic_hashes(self.root)
        self.assertEqual(hashes["topic-007"], parse_topic_file(path)["digest"])


# ---------------------------------------------------------------------------
# W6.2 recall_quality() — no live DB needed, a small prefix-matching fake
# cursor stands in (same shape as tests/test_cli_memory_reliability.py's).
# ---------------------------------------------------------------------------

class _FakeCur:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self._last = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.calls.append((s, params))
        for i, (prefix, result) in enumerate(self.script):
            if s.startswith(prefix):
                self._last = result
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


class RecallQualityMetricHelpersTest(unittest.TestCase):
    def test_metric_shape(self):
        from khipu.drift import _metric

        row = _metric(0.5, 0.8, ok=True, note="n=10")
        self.assertEqual(row, {"value": 0.5, "threshold": 0.8, "ok": True, "note": "n=10"})

    def test_has_column_true(self):
        from khipu.drift import _has_column

        cur = _FakeCur([("SELECT column_name FROM information_schema.columns",
                          {"rows": [("tags",)]})])
        self.assertTrue(_has_column(cur, "episodes", "tags"))

    def test_has_column_false_when_no_row(self):
        from khipu.drift import _has_column

        cur = _FakeCur([("SELECT column_name FROM information_schema.columns", {"rows": []})])
        self.assertFalse(_has_column(cur, "episodes", "tags"))

    def test_table_exists_true(self):
        from khipu.drift import _table_exists

        cur = _FakeCur([("SELECT to_regclass", {"row": (True,)})])
        self.assertTrue(_table_exists(cur, "commitments"))

    def test_table_exists_false(self):
        from khipu.drift import _table_exists

        cur = _FakeCur([("SELECT to_regclass", {"row": (False,)})])
        self.assertFalse(_table_exists(cur, "commitments"))

    def test_one_episode_session_ratio_computes(self):
        from khipu.drift import _one_episode_session_ratio

        cur = _FakeCur([("WITH s AS", {"row": (8, 10)})])
        row = _one_episode_session_ratio(cur)
        self.assertEqual(row["value"], 0.8)
        self.assertTrue(row["ok"])  # 0.8 <= 0.85 threshold

    def test_one_episode_session_ratio_no_data_is_ok(self):
        from khipu.drift import _one_episode_session_ratio

        cur = _FakeCur([("WITH s AS", {"row": (0, 0)})])
        row = _one_episode_session_ratio(cur)
        self.assertIsNone(row["value"])
        self.assertTrue(row["ok"])

    def test_cross_session_pairs_skips_when_columns_missing(self):
        from khipu.drift import _cross_session_pairs_5min

        cur = _FakeCur([("SELECT column_name FROM information_schema.columns", {"rows": []})])
        row = _cross_session_pairs_5min(cur)
        self.assertIsNone(row["value"])
        self.assertTrue(row["ok"])
        self.assertIn("pre-0008", row["note"])

    def test_cross_session_pairs_computes_ratio_below_threshold(self):
        from khipu.drift import _cross_session_pairs_5min

        # _has_column(project) and _has_column(parent_session_id) both read
        # episodes' columns via db.table_columns — one query, per-process
        # cached (fix 13 consolidation), not two.
        cur = _FakeCur([
            ("SELECT column_name FROM information_schema.columns",
             {"rows": [("project",), ("parent_session_id",)]}),
            ("WITH recent AS", {"row": (1,)}),
            ("SELECT COUNT(*) FROM episodes WHERE ts", {"row": (1000,)}),
        ])
        row = _cross_session_pairs_5min(cur)
        self.assertEqual(row["value"], 1)
        self.assertTrue(row["ok"])  # 1/1000 = 0.001 <= 0.01 threshold

    def test_cross_session_pairs_over_threshold_warns(self):
        from khipu.drift import _cross_session_pairs_5min

        cur = _FakeCur([
            ("SELECT column_name FROM information_schema.columns",
             {"rows": [("project",), ("parent_session_id",)]}),
            ("WITH recent AS", {"row": (50,)}),
            ("SELECT COUNT(*) FROM episodes WHERE ts", {"row": (1000,)}),
        ])
        row = _cross_session_pairs_5min(cur)
        self.assertEqual(row["value"], 50)
        self.assertFalse(row["ok"])  # 50/1000 = 0.05 > 0.01 threshold

    def test_dangling_topic_ratio_computes(self):
        from khipu.drift import _dangling_topic_ratio

        cur = _FakeCur([
            ("SELECT column_name FROM information_schema.columns", {"rows": [("tags",)]}),
            ("SELECT COALESCE(SUM", {"row": (2, 8)}),
        ])
        row = _dangling_topic_ratio(cur)
        self.assertEqual(row["value"], 0.2)
        self.assertFalse(row["ok"])  # 0.2 > 0.05 threshold

    def test_dangling_topic_ratio_missing_column_skips(self):
        from khipu.drift import _dangling_topic_ratio

        cur = _FakeCur([("SELECT column_name FROM information_schema.columns", {"rows": []})])
        row = _dangling_topic_ratio(cur)
        self.assertIsNone(row["value"])
        self.assertTrue(row["ok"])

    def test_junk_path_ratio_reuses_hygiene(self):
        from khipu.drift import _junk_path_ratio

        cur = _FakeCur([("SELECT id FROM nodes WHERE type", {"rows": [("path:a/b",)]})])
        row = _junk_path_ratio(cur)
        self.assertEqual(row["value"], 1.0)
        self.assertFalse(row["ok"])

    def test_commitments_counts_missing_table(self):
        from khipu.drift import _commitments_counts

        cur = _FakeCur([("SELECT to_regclass", {"row": (False,)})])
        open_m, stale_m = _commitments_counts(cur)
        self.assertIsNone(open_m["value"])
        self.assertIsNone(stale_m["value"])

    def test_commitments_counts_present(self):
        from khipu.drift import _commitments_counts

        cur = _FakeCur([
            ("SELECT to_regclass", {"row": (True,)}),
            ("SELECT status, COUNT(*) FROM commitments", {"rows": [("open", 5), ("stale", 2)]}),
        ])
        open_m, stale_m = _commitments_counts(cur)
        self.assertEqual(open_m["value"], 5)
        self.assertEqual(stale_m["value"], 2)
        self.assertTrue(stale_m["ok"])  # 2 <= max(5,1)

    def test_commitments_counts_more_stale_than_open_warns(self):
        from khipu.drift import _commitments_counts

        cur = _FakeCur([
            ("SELECT to_regclass", {"row": (True,)}),
            ("SELECT status, COUNT(*) FROM commitments", {"rows": [("open", 1), ("stale", 9)]}),
        ])
        _open_m, stale_m = _commitments_counts(cur)
        self.assertFalse(stale_m["ok"])


class QueryLogMetricsTest(unittest.TestCase):
    """`_query_log_window`/`_query_log_metrics` are a read-only pass over the
    query_log.jsonl format khipu.query_log already documents — write one by
    hand rather than depend on that module's writer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "query_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows):
        with self.path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(__import__("json").dumps(row) + "\n")

    def test_zero_result_rate_and_slice_errors(self):
        from khipu import drift

        now = datetime.now(timezone.utc)
        self._write([
            {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "hybrid", "result_count": 3},
            {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "hybrid", "result_count": 0},
            {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "slice", "result_count": 0},
        ])
        with mock.patch("khipu.query_log.log_path", return_value=self.path):
            zero_row, slice_row = drift._query_log_metrics()
        self.assertAlmostEqual(zero_row["value"], 2 / 3)
        self.assertEqual(slice_row["value"], 1)
        self.assertFalse(slice_row["ok"])

    def test_old_entries_outside_window_are_excluded(self):
        from khipu import drift

        old = datetime.now(timezone.utc) - timedelta(days=60)
        self._write([{"ts": old.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "hybrid", "result_count": 0}])
        with mock.patch("khipu.query_log.log_path", return_value=self.path):
            zero_row, _slice_row = drift._query_log_metrics()
        self.assertIsNone(zero_row["value"])  # no entries in-window -> no rate

    def test_missing_log_file_is_empty_not_an_error(self):
        from khipu import drift

        with mock.patch("khipu.query_log.log_path", return_value=self.path):
            zero_row, slice_row = drift._query_log_metrics()
        self.assertIsNone(zero_row["value"])
        self.assertEqual(slice_row["value"], 0)


class RecallQualityIntegrationTest(unittest.TestCase):
    """recall_quality() opens its own connection (backup_health()'s style);
    patch khipu.drift.connect so this exercises the real call sequence
    end-to-end without a live database."""

    def test_returns_full_block_and_never_raises(self):
        from khipu import drift

        cur = _FakeCur([
            ("WITH s AS", {"row": (8, 10)}),
            # cross_session_pairs_5min's _has_column(project)/_has_column(parent)
            # and dangling_topic_ratio's _has_column(tags) all read episodes'
            # columns via db.table_columns — one cached query per table (fix 13
            # consolidation), so every column any check in this run needs must
            # be in this single "episodes" row set.
            ("SELECT column_name FROM information_schema.columns",
             {"rows": [("project",), ("parent_session_id",), ("tags",)]}),
            ("WITH recent AS", {"row": (0,)}),
            ("SELECT COUNT(*) FROM episodes WHERE ts", {"row": (10,)}),
            ("SELECT COALESCE(SUM", {"row": (1, 9)}),
            ("SELECT id FROM nodes WHERE type", {"rows": []}),
            ("SELECT to_regclass", {"row": (False,)}),
        ])
        conn = _FakeConn(cur)
        with mock.patch("khipu.drift.connect", return_value=conn):
            with mock.patch("khipu.query_log.log_path", return_value=pathlib.Path("/nonexistent-khipu-probe")):
                block = drift.recall_quality(hub_snapshot={"ok": True, "behind_ingest_seconds": 5})
        for key in (
            "one_episode_session_ratio", "cross_session_pairs_5min",
            "dangling_topic_ratio", "junk_path_ratio", "commitments_open",
            "commitments_stale", "query_zero_result_rate", "slice_error_count",
            "snapshot_behind_ingest_seconds",
        ):
            self.assertIn(key, block)
        self.assertTrue(block["snapshot_behind_ingest_seconds"]["ok"])

    def test_a_db_failure_reports_error_not_a_crash(self):
        from khipu import drift

        with mock.patch("khipu.drift.connect", side_effect=RuntimeError("no hub")):
            block = drift.recall_quality()
        self.assertIn("error", block)
        self.assertFalse(block["one_episode_session_ratio"]["ok"])

