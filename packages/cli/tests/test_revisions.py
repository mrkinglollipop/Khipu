"""khipu.revisions is the conflict report the desktop Revisions pane shows and
the exit code `khipu revisions` returns. Untested until the 2026-08-17 audit,
which found it clearing 622 topics after comparing the first 40 of them.

Postgres is always faked here — no test opens a real connection.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from khipu import revisions


class FakeCursor:
    """Records every statement and replays canned rows in order.

    ``commitments_columns`` answers the ONE schema-introspection query
    ``khipu.db.table_columns(cur, "commitments")`` makes (commitments gained
    last_seen_at/seen_count in migration 0012, and every reader gates on them)
    out of band: it is not recorded and it does not consume a canned result,
    so tests that assert on the ORDER of a module's real queries keep working
    on both a migrated and a pre-migration fake hub. Set it to a column list
    to simulate the migrated schema.
    """

    commitments_columns = (
        "id", "text", "project", "owner", "kind", "opened_episode", "opened_at",
        "due_after", "status", "closed_episode", "closed_at", "close_reason",
        "content_hash",
    )

    def __init__(self, results):
        self._results = list(results)
        self.statements: list[str] = []
        self.params: list[tuple] = []
        self._current: list = []
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.pop("commitments", None)

    def execute(self, sql, params=None):
        if "information_schema.columns" in sql and tuple(params or ()) == ("commitments",):
            self._current = [(c,) for c in self.commitments_columns]
            return
        self.statements.append(" ".join(sql.split()))
        self.params.append(params)
        self._current = self._results.pop(0) if self._results else []

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _pg_only_results():
    """The three PG-only queries conflict_report always runs, in order."""
    return [[(7,)], [], [(3,)]]


class ConflictReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "topics").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _topic(self, slug: str, body: str = "hello\n") -> str:
        path = self.root / "topics" / f"{slug}.md"
        path.write_text(body, encoding="utf-8")
        from khipu.mirror import topic_content_hash

        return topic_content_hash(body)

    def _run(self, results, **kw):
        cur = FakeCursor(results)
        with mock.patch.object(revisions, "connect", return_value=FakeConn(cur)) as c:
            out = revisions.conflict_report(self.root, **kw)
        return out, cur, c

    # --- regression: audit 2026-08-17 -----------------------------------------

    def test_every_topic_is_compared_by_default(self):
        """sample defaulted to 40, so `ok` spoke for a corpus it had not read.
        622 topics, 40 checked, green — the drift check's defect, missed here."""
        for i in range(50):
            self._topic(f"topic-{i:03d}")
        rows = [(f"topic-{i:03d}", self._topic(f"topic-{i:03d}"), None, None) for i in range(50)]
        out, _, _ = self._run([rows, *_pg_only_results()])
        self.assertEqual(out["topics_checked"], 50)
        self.assertTrue(out["ok"])

    def test_one_query_for_every_slug_not_one_per_slug(self):
        for i in range(30):
            self._topic(f"t{i:02d}")
        rows = [(f"t{i:02d}", self._topic(f"t{i:02d}"), None, None) for i in range(30)]
        _, cur, conn = self._run([rows, *_pg_only_results()])
        topic_queries = [s for s in cur.statements if "FROM topics" in s]
        self.assertEqual(len(topic_queries), 1)
        self.assertIn("ANY(%s)", topic_queries[0])

    def test_a_single_connection_serves_the_whole_report(self):
        self._topic("a")
        _, _, conn = self._run([[("a", self._topic("a"), None, None)], *_pg_only_results()])
        self.assertEqual(conn.call_count, 1)

    def test_an_unreadable_topic_fails_the_report_rather_than_raising(self):
        """It used to raise UnicodeDecodeError straight out of `khipu revisions`;
        reporting it green would have been worse."""
        (self.root / "topics" / "bad.md").write_bytes(b"\xff\xfe not utf-8")
        # Nothing hashed, so the slug lookup is skipped and only the three
        # PG-only queries run.
        out, _, _ = self._run(_pg_only_results())
        self.assertEqual(out["topic_files_unreadable"], ["bad.md"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["open_file_vs_pg"], 0)

    def test_hashes_agree_with_the_writer_on_crlf(self):
        """revisions, drift and the mirror each had their own sha256; the drift
        one hashed raw bytes, so a CRLF topic file would have reported a
        mismatch no reconcile could ever clear."""
        from khipu import drift
        from khipu.mirror import parse_topic_file

        path = self.root / "topics" / "crlf.md"
        path.write_bytes(b"line one\r\nline two\r\n")
        written = parse_topic_file(path)["digest"]
        walked, _ = drift.file_topic_hashes(self.root)
        self.assertEqual(revisions.file_topic_hash(path), written)
        self.assertEqual(walked["crlf"], written)

    # --- the three conflict shapes --------------------------------------------

    def test_a_file_with_no_pg_row_is_missing_in_pg(self):
        self._topic("orphan")
        out, _, _ = self._run([[], *_pg_only_results()])
        self.assertEqual(out["file_vs_pg"][0]["issue"], "missing_in_pg")
        self.assertFalse(out["ok"])

    def test_a_tombstoned_row_with_a_live_file_is_flagged(self):
        digest = self._topic("zombie")
        rows = [("zombie", digest, None, "2026-08-17")]
        out, _, _ = self._run([rows, *_pg_only_results()])
        self.assertEqual(out["file_vs_pg"][0]["issue"], "tombstoned_in_pg")

    def test_a_differing_hash_is_a_mismatch_and_never_leaks_the_full_digest(self):
        self._topic("changed")
        rows = [("changed", "0" * 64, None, None)]
        out, _, _ = self._run([rows, *_pg_only_results()])
        entry = out["file_vs_pg"][0]
        self.assertEqual(entry["issue"], "hash_mismatch")
        self.assertEqual(len(entry["file_hash"]), 12)
        self.assertEqual(entry["pg_hash"], "0" * 12)

    def test_matching_hashes_produce_no_conflict(self):
        digest = self._topic("clean")
        out, _, _ = self._run([[("clean", digest, None, None)], *_pg_only_results()])
        self.assertEqual(out["open_file_vs_pg"], 0)
        self.assertTrue(out["ok"])

    # --- sampling knobs --------------------------------------------------------

    def test_sample_zero_skips_the_filesystem_walk_entirely(self):
        self._topic("ignored")
        out, cur, _ = self._run(_pg_only_results(), sample=0)
        self.assertEqual(out["topics_checked"], 0)
        self.assertEqual([s for s in cur.statements if "FROM topics " in s], [])

    def test_an_explicit_sample_still_caps_the_pass(self):
        for i in range(10):
            self._topic(f"s{i}")
        out, _, _ = self._run([[], *_pg_only_results()], sample=3)
        self.assertEqual(out["topics_checked"], 3)

    def test_no_memory_root_means_pg_only(self):
        cur = FakeCursor(_pg_only_results())
        with mock.patch.object(revisions, "connect", return_value=FakeConn(cur)):
            out = revisions.conflict_report(None)
        self.assertEqual(out["topics_checked"], 0)
        self.assertEqual(out["revision_row_count"], 7)

    def test_the_mismatch_list_is_capped_but_the_count_is_not(self):
        for i in range(45):
            self._topic(f"m{i:02d}")
        out, _, _ = self._run([[], *_pg_only_results()])
        self.assertEqual(out["open_file_vs_pg"], 45)
        self.assertEqual(len(out["file_vs_pg"]), 40)


class FileTopicHashTest(unittest.TestCase):
    def test_a_missing_file_is_none(self):
        with TemporaryDirectory() as d:
            self.assertIsNone(revisions.file_topic_hash(Path(d) / "nope.md"))

    def test_undecodable_bytes_are_none_not_an_exception(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "bad.md"
            p.write_bytes(b"\xff\xfe")
            self.assertIsNone(revisions.file_topic_hash(p))


class RecentRevisionsTest(unittest.TestCase):
    def _rows(self):
        import datetime as dt

        return [(1, "slug-a", dt.datetime(2026, 8, 17, 12, 0), "capture", "note", "abc", "preview")]

    def test_the_slug_filter_reaches_the_query(self):
        cur = FakeCursor([self._rows()])
        with mock.patch.object(revisions, "connect", return_value=FakeConn(cur)):
            revisions.recent_revisions(limit=5, slug="slug-a")
        self.assertIn("WHERE slug = %s", cur.statements[0])
        self.assertEqual(cur.params[0], ("slug-a", 5))

    def test_without_a_slug_the_query_is_unfiltered(self):
        cur = FakeCursor([self._rows()])
        with mock.patch.object(revisions, "connect", return_value=FakeConn(cur)):
            revisions.recent_revisions(limit=5)
        self.assertNotIn("WHERE slug", cur.statements[0])
        self.assertEqual(cur.params[0], (5,))

    def test_timestamps_come_back_as_iso_strings(self):
        cur = FakeCursor([self._rows()])
        with mock.patch.object(revisions, "connect", return_value=FakeConn(cur)):
            out = revisions.recent_revisions()
        self.assertEqual(out[0]["revised_at"], "2026-08-17T12:00:00")


class RevisionForIdTest(unittest.TestCase):
    def test_a_missing_id_is_none_not_an_error(self):
        cur = FakeCursor([[]])
        with mock.patch.object(revisions, "connect", return_value=FakeConn(cur)):
            self.assertIsNone(revisions.revision_for_id(999))

    def test_a_found_revision_carries_the_full_body(self):
        cur = FakeCursor([[(4, "s", None, "capture", "n", "h", "the whole body")]])
        with mock.patch.object(revisions, "connect", return_value=FakeConn(cur)):
            out = revisions.revision_for_id(4)
        self.assertEqual(out["body"], "the whole body")
        self.assertIsNone(out["revised_at"])


if __name__ == "__main__":
    unittest.main()
