"""Unit tests for khipu.mirror + drift's directional gate — P2b (audit F1/F3).

Pure tests (topic parsing, episode-key extraction) need no database. Live tests
connect to the Khipu Postgres read-only — the anti-join and md5-parity checks
never write — and skip cleanly when it's unreachable.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import pathlib
import os
from unittest import mock
import unittest
from pathlib import Path

from khipu import mirror
from khipu.drift import file_episode_keys
from khipu.mirror import parse_topic_file


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


class ParseTopicFileTest(unittest.TestCase):
    """F3: one canonical topic shape for mirror AND reconcile. These pin the
    frontmatter handling both writers now share."""

    def _write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="topic-", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_frontmatter_parsed_and_stripped(self) -> None:
        text = '---\ntitle: "My Topic"\nstatus: archived\n---\n\nBody here.\n'
        path = self._write(text)
        parsed = parse_topic_file(path)
        assert parsed is not None
        self.assertEqual(parsed["title"], "My Topic")
        self.assertEqual(parsed["status"], "archived")
        self.assertEqual(parsed["body"], "Body here.\n")
        # Hash covers the FULL text (frontmatter included) so it stays
        # comparable with drift/conflict checks that hash the file as-is.
        self.assertEqual(
            parsed["digest"], hashlib.sha256(text.encode("utf-8")).hexdigest()
        )

    def test_no_frontmatter_defaults(self) -> None:
        path = self._write("Just a body.\n")
        parsed = parse_topic_file(path)
        assert parsed is not None
        self.assertEqual(parsed["title"], path.stem)
        self.assertEqual(parsed["status"], "active")
        self.assertEqual(parsed["body"], "Just a body.\n")

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(parse_topic_file(Path("/nonexistent/topic.md")))


class FileEpisodeKeysTest(unittest.TestCase):
    """The drift gate's file-side identity extraction: md5 of the exact summary
    string the mirror inserts, malformed/blank/ts-less lines skipped."""

    def _write_jsonl(self, lines: list[str]) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_keys_and_skips(self) -> None:
        good = {"ts": "2026-08-10T12:00:00+00:00", "summary": "hello"}
        lines = [
            json.dumps(good),
            "",  # blank
            "not json {",  # malformed
            json.dumps({"summary": "no ts"}),  # ts-less
        ]
        path = self._write_jsonl(lines)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "episodes.jsonl").write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            keys = file_episode_keys(root)
        self.assertEqual(len(keys), 1)
        ts, md = keys[0]
        self.assertEqual(ts, good["ts"])
        self.assertEqual(md, hashlib.md5(b"hello").hexdigest())

    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(file_episode_keys(Path(d)), [])


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live drift checks")
class DirectionalDriftLiveTest(unittest.TestCase):
    """P2b exit AC: the gate must be able to FAIL. A fabricated episode key
    must come back missing; a real PG episode's key must not. Read-only."""

    def test_python_md5_matches_pg_md5(self) -> None:
        # The anti-join compares hashlib.md5 (file side) with PG md5() (row
        # side); both must hash identical UTF-8 bytes, unicode included.
        from khipu.db import connect

        for probe in ("plain ascii", "unicodé ✓ — khipu"):
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT md5(%s)", (probe,))
                    pg_md5 = cur.fetchone()[0]
            self.assertEqual(pg_md5, hashlib.md5(probe.encode("utf-8")).hexdigest())

    def test_fabricated_key_is_missing(self) -> None:
        from khipu.db import connect
        from khipu.drift import episodes_missing_in_pg

        fake = [("1999-01-01T00:00:00+00:00", hashlib.md5(b"no such episode").hexdigest())]
        with connect() as conn:
            with conn.cursor() as cur:
                missing = episodes_missing_in_pg(cur, fake)
        self.assertEqual(missing, fake)

    def test_real_episode_key_is_present(self) -> None:
        from khipu.db import connect
        from khipu.drift import episodes_missing_in_pg

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts::text, md5(summary) FROM episodes ORDER BY id LIMIT 1"
                )
                row = cur.fetchone()
                if row is None:
                    self.skipTest("episodes table empty")
                missing = episodes_missing_in_pg(cur, [(row[0], row[1])])
        self.assertEqual(missing, [])

    def test_empty_keys_is_green(self) -> None:
        from khipu.db import connect
        from khipu.drift import episodes_missing_in_pg

        with connect() as conn:
            with conn.cursor() as cur:
                self.assertEqual(episodes_missing_in_pg(cur, []), [])


if __name__ == "__main__":
    unittest.main()


class _FakeCursor:
    """Records SQL. Answers the tombstone sweep's COUNT with `live_count`."""

    def __init__(self, live_count):
        self.live_count = live_count
        self.sql = []
        self._last = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self._last = self.sql[-1]

    def fetchone(self):
        if self._last and self._last.startswith("SELECT COUNT(*) FROM topics"):
            return (self.live_count,)
        return (0,)

    def fetchall(self):
        return []

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


class TombstoneCircuitBreakerTest(unittest.TestCase):
    """`topics_dir.is_dir()` was the only guard on the tombstone sweep, but a
    volume that mounts with an EMPTY topics/ passes it — `seen` comes back empty
    and `NOT (slug = ANY('{}'))` is true for every row, tombstoning the whole
    corpus. Every read filters deleted_at IS NULL and embed.backfill then
    DELETES the vectors, so recovery is a paid re-embed (audit 2026-08-17).
    No live database is touched here.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "topics").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, live_count, env=None):
        cur = _FakeCursor(live_count)
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
             mock.patch.dict(os.environ, env or {}):
            stats = mirror.reconcile_memory_root(self.root)
        return stats, cur

    def _updates(self, cur):
        return [q for q in cur.sql if q.startswith("UPDATE topics SET deleted_at")]

    def test_an_empty_topics_dir_never_tombstones(self):
        stats, cur = self._run(live_count=622)
        self.assertEqual(self._updates(cur), [], "the sweep must not run on an empty read")
        self.assertEqual(stats["tombstoned"], 0)
        self.assertIn("refusing", stats["tombstone_skipped"])

    def test_an_empty_root_with_an_empty_pg_is_a_no_op_not_an_alarm(self):
        stats, cur = self._run(live_count=0)
        self.assertEqual(stats["tombstoned"], 0)
        self.assertNotIn("tombstone_skipped", stats)

    def test_a_plausible_deletion_still_sweeps(self):
        for i in range(20):
            (self.root / "topics" / f"t{i:02d}.md").write_text(f"# T{i}\n\nbody")
        stats, cur = self._run(live_count=21)          # one topic genuinely gone
        self.assertEqual(len(self._updates(cur)), 1, "a normal deletion must still sweep")
        self.assertNotIn("tombstone_skipped", stats)

    def test_an_implausibly_large_deletion_is_refused(self):
        for i in range(5):
            (self.root / "topics" / f"t{i:02d}.md").write_text(f"# T{i}\n\nbody")
        stats, cur = self._run(live_count=600)         # 595 would vanish
        self.assertEqual(self._updates(cur), [])
        self.assertIn("KHIPU_ALLOW_MASS_TOMBSTONE", stats["tombstone_skipped"])

    def test_the_override_allows_a_real_bulk_retirement(self):
        for i in range(5):
            (self.root / "topics" / f"t{i:02d}.md").write_text(f"# T{i}\n\nbody")
        stats, cur = self._run(live_count=600, env={"KHIPU_ALLOW_MASS_TOMBSTONE": "1"})
        self.assertEqual(len(self._updates(cur)), 1)

