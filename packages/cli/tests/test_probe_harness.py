"""khipu.probe — each harness keeps its own last-probe file (audit
2026-09-04 §1.8: one shared file hid the pack-level signal)."""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from khipu import probe


class HarnessResultsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = pathlib.Path(self.tmp.name)
        self._patches = [
            mock.patch("khipu.paths.data_dir", return_value=self.data_dir),
            mock.patch("khipu.paths.ensure_data_dir", return_value=self.data_dir),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_harness_keeps_its_own_last_probe(self):
        probe._write_state({"ts": probe._now_iso(), "harness": "cursor", "ok": True, "seconds": 3.2})
        probe._write_state({"ts": "2020-01-01T00:00:00Z", "harness": "codex", "ok": True, "seconds": 9.0})
        probe._write_state({"ts": probe._now_iso(), "harness": "claude_code", "ok": False, "error": "never found"})

        st = probe.status()
        self.assertEqual(st["last_probe"]["harness"], "claude_code")
        self.assertEqual(set(st["harnesses"]), {"cursor", "codex", "claude_code"})
        self.assertTrue(st["harnesses"]["cursor"]["ok"])
        self.assertFalse(st["harnesses"]["cursor"]["stale"])
        self.assertTrue(st["harnesses"]["codex"]["stale"])
        self.assertFalse(st["harnesses"]["claude_code"]["ok"])
        self.assertEqual(st["harnesses"]["claude_code"]["error"], "never found")


if __name__ == "__main__":
    unittest.main()
