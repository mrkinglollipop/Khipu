"""khipu.extract turns a transcript window into an episode. Aegis has no legacy
extractor, so for that harness this module decides what memory exists at all —
and it had no tests until the 2026-08-17 audit.

The model call is always mocked; nothing here reaches Gemini.
"""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
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

    def test_cwd_basename_is_no_longer_forced_into_topics(self):
        """memory reliability W1.3: the cwd basename used to be force-appended
        as a topic (the single largest source of dangling topic nodes);
        project identity now belongs in the capture payload's `project`
        field, set by the hook via khipu.identity — not minted as a topic."""
        out = self._run(_reply(topics=["something-real"]), cwd="/srv/checkouts/Khipu")
        self.assertEqual(out["topics"], ["something-real"])
        self.assertNotIn("khipu", out["topics"])

    def test_cwd_with_trailing_slash_still_does_not_leak_into_topics(self):
        out = self._run(_reply(), cwd="/a/b/Khipu/")
        self.assertNotIn("khipu", out["topics"])

    def test_people_parsed_from_the_model_reply(self):
        out = self._run(_reply(people=["Matt", " Alice "]))
        self.assertEqual(out["people"], ["Matt", "Alice"])

    def test_people_defaults_to_empty_list(self):
        out = self._run(_reply())
        self.assertEqual(out["people"], [])

    def test_open_loops_normalized_from_objects(self):
        """owner/future_trigger ride through as the model sent them (or None);
        khipu.commitments decides the STORED values deterministically."""
        out = self._run(_reply(open_loops=[
            {"text": "follow up with Matt", "kind": "FOLLOWUP", "due_after": "2026-09-10",
             "owner": "assistant", "future_trigger": True},
            {"text": "  "},
            "bare string loop",
            {"text": "bad kind", "kind": "nonsense"},
        ]))
        self.assertEqual(out["open_loops"], [
            {"text": "follow up with Matt", "kind": "followup", "due_after": "2026-09-10",
             "owner": "assistant", "future_trigger": True},
            {"text": "bare string loop", "kind": "followup", "due_after": None,
             "owner": None, "future_trigger": None},
            {"text": "bad kind", "kind": "followup", "due_after": None,
             "owner": None, "future_trigger": None},
        ])

    def test_open_loops_defaults_to_empty_list(self):
        out = self._run(_reply())
        self.assertEqual(out["open_loops"], [])

    def test_closed_loops_normalized(self):
        out = self._run(_reply(closed_loops=[{"text": "shipped the fix"}, "merged PR", {"text": ""}]))
        self.assertEqual(out["closed_loops"], [{"text": "shipped the fix"}, {"text": "merged PR"}])

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
             mock.patch("khipu.models.synth_settings", return_value={
                 "provider": "cloud", "endpoint": "", "model_id": "gemini-2.5-flash",
             }), \
             mock.patch("khipu.models.cloud_model_id", return_value="gemini-2.5-flash"), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            extract._generate("hi")
        self.assertNotIn("SECRET-KEY-VALUE", seen["url"])
        self.assertNotIn("key=", seen["url"])
        self.assertEqual(seen["headers"].get("x-goog-api-key"), "SECRET-KEY-VALUE")

    def test_local_path_hits_chat_completions_and_never_calls_key(self):
        seen = {}

        class _Resp:
            status = 200

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": '{"summary":"x"}'}}]
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, **kw):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = req.data
            return _Resp()

        with mock.patch.object(extract, "_key", side_effect=AssertionError("_key")) as key_mock, \
             mock.patch("khipu.models.synth_settings", return_value={
                 "provider": "local",
                 "endpoint": "http://127.0.0.1:11434",
                 "model_id": "llama3",
             }), \
             mock.patch("khipu.keychain.get_openai_compat_key", return_value="LOCAL-SECRET"), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            out = extract._generate("hi")
        self.assertIn("/v1/chat/completions", seen["url"])
        self.assertNotIn("generativelanguage.googleapis.com", seen["url"])
        self.assertEqual(seen["headers"].get("authorization"), "Bearer LOCAL-SECRET")
        self.assertNotIn(b"LOCAL-SECRET", seen["body"] or b"")
        self.assertNotIn("LOCAL-SECRET", seen["url"])
        key_mock.assert_not_called()
        self.assertIn("summary", out)

    def test_empty_local_endpoint_raises_without_gemini_fallback(self):
        with mock.patch("khipu.models.synth_settings", return_value={
                 "provider": "local", "endpoint": "", "model_id": "x",
             }), \
             mock.patch.object(extract, "_key", side_effect=AssertionError("_key")) as key_mock, \
             mock.patch.object(extract, "_generate_cloud") as cloud:
            with self.assertRaises(RuntimeError):
                extract._generate("hi")
        key_mock.assert_not_called()
        cloud.assert_not_called()

    def test_json_object_retries_once_on_400_then_succeeds(self):
        calls = []

        class _Resp:
            def __init__(self, status, body):
                self.status = status
                self._body = body.encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, **kw):
            body = json.loads(req.data.decode())
            calls.append(body)
            if "response_format" in body:
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad", hdrs=None, fp=io.BytesIO(b"no json mode")
                )
            return _Resp(200, json.dumps({
                "choices": [{"message": {"content": '{"ok":true}'}}]
            }))

        with mock.patch("khipu.models.synth_settings", return_value={
                 "provider": "local",
                 "endpoint": "http://127.0.0.1:11434",
                 "model_id": "llama3",
             }), \
             mock.patch("khipu.keychain.get_openai_compat_key", return_value=None), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            out = extract._generate("hi")
        self.assertEqual(len(calls), 2)
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])
        self.assertIn("ok", out)

    def test_json_object_retries_once_on_415_then_succeeds(self):
        calls = []

        class _Resp:
            def __init__(self, status, body):
                self.status = status
                self._body = body.encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, **kw):
            body = json.loads(req.data.decode())
            calls.append(body)
            if "response_format" in body:
                raise urllib.error.HTTPError(
                    req.full_url,
                    415,
                    "Unsupported Media Type",
                    hdrs=None,
                    fp=io.BytesIO(b"unsupported"),
                )
            return _Resp(200, json.dumps({
                "choices": [{"message": {"content": '{"ok":true}'}}]
            }))

        with mock.patch("khipu.models.synth_settings", return_value={
                 "provider": "local",
                 "endpoint": "http://127.0.0.1:11434",
                 "model_id": "llama3",
             }), \
             mock.patch("khipu.keychain.get_openai_compat_key", return_value=None), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            out = extract._generate("hi")
        self.assertEqual(len(calls), 2)
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])
        self.assertIn("ok", out)

    def test_json_object_does_not_retry_on_401(self):
        calls = []

        def fake_urlopen(req, **kw):
            body = json.loads(req.data.decode())
            calls.append(body)
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b"nope")
            )

        with mock.patch("khipu.models.synth_settings", return_value={
                 "provider": "local",
                 "endpoint": "http://127.0.0.1:11434",
                 "model_id": "llama3",
             }), \
             mock.patch("khipu.keychain.get_openai_compat_key", return_value=None), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError):
                extract._generate("hi")
        self.assertEqual(len(calls), 1)
        self.assertIn("response_format", calls[0])

    def test_khipu_extract_model_ignored_when_provider_is_local(self):
        seen = {}

        class _Resp:
            status = 200

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "{}"}}]
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, **kw):
            seen["body"] = json.loads(req.data.decode())
            return _Resp()

        with mock.patch.dict(os.environ, {"KHIPU_EXTRACT_MODEL": "should-not-appear"}), \
             mock.patch("khipu.models.synth_settings", return_value={
                 "provider": "local",
                 "endpoint": "http://127.0.0.1:11434",
                 "model_id": "llama3",
             }), \
             mock.patch("khipu.keychain.get_openai_compat_key", return_value=None), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            extract._generate("hi")
        self.assertEqual(seen["body"]["model"], "llama3")

    def test_module_has_no_import_time_model_constant(self):
        self.assertFalse(hasattr(extract, "MODEL"))


if __name__ == "__main__":
    unittest.main()
