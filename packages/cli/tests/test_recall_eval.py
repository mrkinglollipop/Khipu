"""Tests for khipu.recall_eval (W6.3) — golden-query hit@k scoring.
khipu.embed.hybrid_search is mocked throughout; no live database."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from khipu import recall_eval


class LoadGoldenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "golden.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_parses_valid_lines_skips_blanks_and_comments(self):
        self.path.write_text(
            '# a comment\n'
            '\n'
            '{"query": "a", "expect": ["1"], "k": 3, "note": "n"}\n'
            '{"query": "b", "expect": ["2"]}\n'
        )
        entries = recall_eval.load_golden(self.path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["query"], "a")
        self.assertEqual(entries[1]["query"], "b")

    def test_invalid_json_raises_with_line_number(self):
        self.path.write_text('{"query": "a", "expect": ["1"]}\nnot json\n')
        with self.assertRaises(ValueError) as ctx:
            recall_eval.load_golden(self.path)
        self.assertIn(":2:", str(ctx.exception))

    def test_missing_required_fields_raises(self):
        self.path.write_text('{"query": "a"}\n')
        with self.assertRaises(ValueError):
            recall_eval.load_golden(self.path)


class EvalOneTest(unittest.TestCase):
    def test_hit_when_expected_id_in_top_k(self):
        entry = {"query": "q", "expect": ["42"], "k": 3}
        results = {"results": [
            {"kind": "episode", "id": "1", "score": 0.5},
            {"kind": "episode", "id": "42", "score": 0.4},
            {"kind": "topic", "id": "some-topic", "score": 0.9},
        ]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results) as m:
            row = recall_eval.eval_one(entry)
        self.assertTrue(row["hit"])
        # Every kind is scored (audit 2026-09-04): the top-k slice is what the
        # caller actually sees, so dropping non-episode rows from `got` both
        # made a topic-slug golden line unhittable and mis-reported the slice.
        self.assertEqual(row["got"], ["1", "42", "some-topic"])
        m.assert_called_once_with("q", mode="hybrid", limit=3)

    def test_a_topic_slug_can_be_a_golden_expectation(self):
        entry = {"query": "q", "expect": ["some-topic"], "k": 3}
        results = {"results": [
            {"kind": "episode", "id": "1", "score": 0.5},
            {"kind": "topic", "id": "some-topic", "score": 0.4},
        ]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            row = recall_eval.eval_one(entry)
        self.assertTrue(row["hit"], row)
        self.assertEqual(row["got"], ["1", "some-topic"])

    def test_miss_when_expected_id_absent(self):
        entry = {"query": "q", "expect": ["999"], "k": 3}
        results = {"results": [{"kind": "episode", "id": "1", "score": 0.5}]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            row = recall_eval.eval_one(entry)
        self.assertFalse(row["hit"])

    def test_search_failure_is_a_miss_not_a_crash(self):
        entry = {"query": "q", "expect": ["1"], "k": 3}
        with mock.patch("khipu.embed.hybrid_search", side_effect=RuntimeError("hub down")):
            row = recall_eval.eval_one(entry)
        self.assertFalse(row["hit"])
        self.assertIn("hub down", row["error"])

    def test_custom_mode_and_k_are_forwarded(self):
        entry = {"query": "q", "mode": "semantic", "expect": ["1"], "k": 5}
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": []}) as m:
            recall_eval.eval_one(entry)
        m.assert_called_once_with("q", mode="semantic", limit=5)


class RunEvalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "golden.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, entries):
        with self.path.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def test_overall_hit_rate(self):
        self._write([
            {"query": "hit-one", "expect": ["1"], "k": 3},
            {"query": "hit-two", "expect": ["2"], "k": 3},
            {"query": "miss-one", "expect": ["999"], "k": 3},
            {"query": "miss-two", "expect": ["998"], "k": 3},
        ])

        def fake_search(query, *, mode="hybrid", limit=3):
            mapping = {
                "hit-one": ["1"], "hit-two": ["2"],
                "miss-one": ["1"], "miss-two": ["1"],
            }
            return {"results": [{"kind": "episode", "id": i, "score": 1.0}
                                 for i in mapping[query]]}

        with mock.patch("khipu.embed.hybrid_search", side_effect=fake_search):
            report = recall_eval.run_eval(self.path)
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["hits"], 2)
        self.assertEqual(report["overall_hit_rate"], 0.5)
        self.assertEqual(len(report["rows"]), 4)

    def test_empty_golden_file_is_zero_not_a_crash(self):
        self.path.write_text("")
        report = recall_eval.run_eval(self.path)
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["overall_hit_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
