"""khipu.query_log — the search query log (W2.5). Append-only JSONL under the
state dir; every test points ``data_dir()`` at a tmpdir so nothing touches the
real one.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import query_log as ql


class _TmpDataDir:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self._patches = [
            mock.patch("khipu.paths.data_dir", return_value=self.path),
            mock.patch("khipu.paths.ensure_data_dir", return_value=self.path),
        ]
        for p in self._patches:
            p.start()
        return self.path

    def __exit__(self, *a):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class LogQueryTest(unittest.TestCase):
    def test_writes_one_json_line(self) -> None:
        with _TmpDataDir() as data:
            ql.log_query(
                "mobile oracle follow-up",
                mode="hybrid",
                filters={"kind": None, "project": "khipu"},
                result_count=3,
                top=[{"kind": "episode", "id": "11287", "score": 0.9}],
            )
            lines = (data / ql.LOG_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["query"], "mobile oracle follow-up")
        self.assertEqual(entry["mode"], "hybrid")
        self.assertEqual(entry["result_count"], 3)
        self.assertEqual(entry["top"][0]["id"], "11287")

    def test_empty_filter_values_are_dropped(self) -> None:
        with _TmpDataDir() as data:
            ql.log_query(
                "x", mode="literal", filters={"kind": None, "project": ""},
                result_count=0, top=[],
            )
            entry = json.loads((data / ql.LOG_NAME).read_text(encoding="utf-8").strip())
        self.assertEqual(entry["filters"], {})

    def test_harness_env_default(self) -> None:
        with _TmpDataDir() as data:
            with mock.patch.dict("os.environ", {"KHIPU_HARNESS": "aegis"}):
                ql.log_query("x", mode="hybrid", result_count=0, top=[])
            entry = json.loads((data / ql.LOG_NAME).read_text(encoding="utf-8").strip())
        self.assertEqual(entry["harness"], "aegis")

    def test_defaults_to_mcp_when_no_harness_env(self) -> None:
        with _TmpDataDir() as data:
            with mock.patch.dict("os.environ", {}, clear=False):
                import os as _os

                _os.environ.pop("KHIPU_HARNESS", None)
                ql.log_query("x", mode="hybrid", result_count=0, top=[])
            entry = json.loads((data / ql.LOG_NAME).read_text(encoding="utf-8").strip())
        self.assertEqual(entry["harness"], "mcp")

    def test_never_raises_on_a_broken_data_dir(self) -> None:
        with mock.patch("khipu.paths.ensure_data_dir", side_effect=OSError("nope")):
            ql.log_query("x", mode="hybrid", result_count=0, top=[])  # must not raise


class TailTest(unittest.TestCase):
    def test_tail_returns_last_n_newest_last(self) -> None:
        with _TmpDataDir() as data:
            for i in range(5):
                ql.log_query(f"q{i}", mode="hybrid", result_count=i, top=[])
            out = ql.tail(2)
        self.assertEqual([e["query"] for e in out], ["q3", "q4"])

    def test_tail_on_missing_file_is_empty(self) -> None:
        with _TmpDataDir():
            self.assertEqual(ql.tail(10), [])

    def test_tail_skips_corrupt_lines(self) -> None:
        with _TmpDataDir() as data:
            path = data / ql.LOG_NAME
            path.write_text("not json\n" + json.dumps({"query": "ok"}) + "\n", encoding="utf-8")
            out = ql.tail(10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["query"], "ok")


class ZeroResultsTest(unittest.TestCase):
    def test_only_zero_result_recent_entries(self) -> None:
        with _TmpDataDir() as data:
            ql.log_query("has-hits", mode="hybrid", result_count=3, top=[])
            ql.log_query("no-hits", mode="hybrid", result_count=0, top=[])
            out = ql.zero_results(7)
        self.assertEqual([e["query"] for e in out], ["no-hits"])

    def test_old_zero_result_entries_are_excluded(self) -> None:
        with _TmpDataDir() as data:
            path = data / ql.LOG_NAME
            old = {
                "ts": "2020-01-01T00:00:00Z", "query": "ancient", "mode": "hybrid",
                "filters": {}, "result_count": 0, "top": [], "harness": "mcp",
            }
            path.write_text(json.dumps(old) + "\n", encoding="utf-8")
            out = ql.zero_results(7)
        self.assertEqual(out, [])


class RotationTest(unittest.TestCase):
    def test_rotates_when_over_max_bytes(self) -> None:
        with _TmpDataDir() as data:
            path = data / ql.LOG_NAME
            path.write_text("x" * (ql.MAX_BYTES + 1), encoding="utf-8")
            ql.log_query("after-rotation", mode="hybrid", result_count=1, top=[])
            rotated = data / (ql.LOG_NAME + ql.ROTATED_SUFFIX)
            self.assertTrue(rotated.is_file())
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["query"], "after-rotation")


if __name__ == "__main__":
    unittest.main()
