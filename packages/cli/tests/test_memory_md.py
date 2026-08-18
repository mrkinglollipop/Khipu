"""khipu.memory_md regenerates the human-readable topic index from Postgres.
Untested until the 2026-08-17 audit, which found it writing the file in place —
a crash partway through would truncate the index someone reads to find a topic.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from khipu import memory_md
from tests.test_revisions import FakeConn, FakeCursor


def _rows():
    return [
        ("khipu-audit", "Khipu Audit", "active", "2026-08-17"),
        ("phase-f", "Phase F", "active", "2026-08-16"),
    ]


class RegenMemoryMdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.out = Path(self.tmp.name) / "nested" / "MEMORY.md"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, rows, **kw):
        cur = FakeCursor([rows])
        with mock.patch.object(memory_md, "connect", return_value=FakeConn(cur)):
            n = memory_md.regen_memory_md(self.out, **kw)
        return n, cur

    def test_it_writes_one_line_per_topic_and_returns_the_count(self):
        n, _ = self._run(_rows())
        self.assertEqual(n, 2)
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("[Khipu Audit](topics/khipu-audit.md)", text)
        self.assertIn("[Phase F](topics/phase-f.md)", text)

    def test_missing_parent_directories_are_created(self):
        self._run(_rows())
        self.assertTrue(self.out.is_file())

    def test_tombstoned_topics_are_excluded_by_the_query(self):
        _, cur = self._run(_rows())
        self.assertIn("deleted_at IS NULL", cur.statements[0])

    def test_the_limit_reaches_the_query(self):
        _, cur = self._run(_rows(), limit=7)
        self.assertEqual(cur.params[0], (7,))

    def test_an_empty_corpus_still_writes_a_valid_header(self):
        n, _ = self._run([])
        self.assertEqual(n, 0)
        self.assertTrue(self.out.read_text(encoding="utf-8").startswith("# Memory"))

    # --- regression: audit 2026-08-17 -----------------------------------------

    def test_a_failed_write_leaves_the_previous_index_intact(self):
        """A direct write() truncates before it fills; a reader arriving mid-run
        would have seen a half-empty index, or an empty one if it died."""
        self.out.parent.mkdir(parents=True)
        self.out.write_text("# the good index\n", encoding="utf-8")
        cur = FakeCursor([_rows()])
        with mock.patch.object(memory_md, "connect", return_value=FakeConn(cur)), \
             mock.patch.object(Path, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                memory_md.regen_memory_md(self.out)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "# the good index\n")

    def test_no_temp_file_is_left_behind_on_success(self):
        self._run(_rows())
        leftovers = list(self.out.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
