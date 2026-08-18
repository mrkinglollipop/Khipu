"""khipu.extract turns a transcript window into an episode. Aegis has no legacy
extractor, so for that harness this module decides what memory exists at all —
and it had no tests until the 2026-08-17 audit.

The model call is always mocked; nothing here reaches Gemini.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from khipu import extract


def _reply(**kw) -> str:
    base = {"summary": "did a thing", "topics": [], "decisions": [],
            "preferences": [], "scope": "repo"}
    base.update(kw)
    return json.dumps(base)


class SlugifyTest(unittest.TestCase):
    def test_spaces_and_case_become_a_slug(self):
        self.assertEqual(extract.slugify("Phase F Calibration"), "phase-f-calibration")

    def test_punctuation_collapses_and_edges_are_trimmed(self):
        self.assertEqual(extract.slugify("  --Khipu/Audit!!  "), "khipu-audit")

    def test_runs_of_separators_collapse(self):
        self.assertEqual(extract.slugify("a   ---   b"), "a-b")

    def test_a_slug_with_nothing_usable_is_empty(self):
        self.assertEqual(extract.slugify("!!!"), "")


class ParseModelJsonTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(extract.parse_model_json('{"a": 1}'), {"a": 1})

    def test_markdown_fences_and_leading_prose_are_tolerated(self):
        raw = 'Sure!\n```json\n{"a": 1}\n```'
        self.assertEqual(extract.parse_model_json(raw), {"a": 1})

    def test_nested_braces_survive(self):
        self.assertEqual(extract.parse_model_json('{"a": {"b": 2}}'), {"a": {"b": 2}})

    def test_non_json_and_non_object_are_none(self):
        for raw in ("no json here", "", '["a"]', '{not json}'):
            with self.subTest(raw=raw):
                self.assertIsNone(extract.parse_model_json(raw))


class AsStrListTest(unittest.TestCase):
    def test_non_list_is_empty(self):
        for v in (None, "a", 5, {"a": 1}):
            self.assertEqual(extract._as_str_list(v), [])

    def test_blanks_dropped_and_values_stringified(self):
        self.assertEqual(extract._as_str_list(["a", "  ", 7, ""]), ["a", "7"])

    def test_lower_flag(self):
        self.assertEqual(extract._as_str_list(["AbC"], lower=True), ["abc"])


class ExtractMemoryTest(unittest.TestCase):
    def _run(self, reply, **kw):
        with mock.patch.object(extract, "_generate", return_value=reply):
            return extract.extract_memory("some transcript", **kw)

    def test_empty_transcript_never_calls_the_model(self):
        with mock.patch.object(extract, "_generate") as g:
            self.assertIsNone(extract.extract_memory("   "))
        g.assert_not_called()

    def test_an_empty_summary_means_nothing_durable(self):
        self.assertIsNone(self._run(_reply(summary="")))
        self.assertIsNone(self._run(_reply(summary="   ")))

    def test_topics_are_slugified_and_deduped(self):
        out = self._run(_reply(topics=["Phase F", "phase f", "Khipu Audit"]))
        self.assertEqual(out["topics"], ["phase-f", "khipu-audit"])

    def test_the_project_dir_is_added_as_a_topic_once(self):
        out = self._run(_reply(topics=["khipu"]), cwd="/srv/checkouts/Khipu")
        self.assertEqual(out["topics"].count("khipu"), 1)

    def test_a_trailing_slash_on_cwd_still_yields_the_dir_name(self):
        out = self._run(_reply(), cwd="/a/b/Khipu/")
        self.assertIn("khipu", out["topics"])

    def test_non_json_from_the_model_raises_rather_than_losing_the_window(self):
        """The caller must be able to retry; a silent None would mark the
        transcript window consumed and drop the session."""
        with mock.patch.object(extract, "_generate", return_value="I'm sorry Dave"):
            with self.assertRaises(RuntimeError):
                extract.extract_memory("t")

    def test_transport_failure_propagates(self):
        with mock.patch.object(extract, "_generate", side_effect=RuntimeError("HTTP 500")):
            with self.assertRaises(RuntimeError):
                extract.extract_memory("t")

    # --- regression: audit 2026-08-17 -------------------------------------------

    def test_a_list_summary_is_joined_not_stringified(self):
        """str(["did", "a thing"]) produced the literal "['did', 'a thing']" as
        the episode summary — garbage that then gets embedded and searched."""
        out = self._run(_reply(summary=["did", "a thing"]))
        self.assertEqual(out["summary"], "did a thing")
        self.assertNotIn("[", out["summary"])

    def test_a_nonsense_summary_type_is_treated_as_nothing_durable(self):
        for bad in (5, {"a": 1}, None):
            with self.subTest(bad=bad):
                self.assertIsNone(self._run(_reply(summary=bad)))

    def test_the_api_key_is_sent_as_a_header_not_in_the_url(self):
        seen = {}

        class _Resp:
            def read(self):
                return json.dumps(
                    {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, **kw):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _Resp()

        with mock.patch.object(extract, "_key", return_value="SECRET-KEY-VALUE"), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            extract._generate("hi")
        self.assertNotIn("SECRET-KEY-VALUE", seen["url"])
        self.assertNotIn("key=", seen["url"])
        self.assertEqual(seen["headers"].get("x-goog-api-key"), "SECRET-KEY-VALUE")


if __name__ == "__main__":
    unittest.main()
