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

