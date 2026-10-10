"""Hermetic regressions for cited synthesis, brief freshness and outcomes."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from khipu import briefs, reflect
from tests.test_briefs import _answer, _build, _claim, _seeded
from tests.test_reflect import (
    _ReflectCur, _ReflectHub, _answer as _reflection_answer,
    _claim as _reflection_claim, _rows, _run,
)


class CitedProseTest(unittest.TestCase):
    def test_brief_stores_only_claims_with_citations(self):
        hub = _seeded()
        out, _ = _build(hub, answer=_answer(
            [_claim("Release was planned.", 1)],
            body="Release shipped. Production deployment is approved.",
        ))
        self.assertEqual(out["status"], "built")
        body = hub.current("alpha")["body"]
        self.assertNotIn("Release shipped", body)
        self.assertNotIn("Production deployment is approved", body)
        self.assertEqual(body, "- Release was planned. [episode:1]")

    def test_reflection_returns_only_claims_with_citations(self):
        hub = _ReflectHub()
        hub.episode(1, "Release was planned.")
        out, _ = _run(hub, _reflection_answer(
            [_reflection_claim("Release was planned.", "episode:1")],
            answer="Release shipped. Production deployment is approved.",
        ), rows=_rows(1))
        self.assertFalse(out["abstained"])
        self.assertNotIn("Release shipped", out["answer"])
        self.assertNotIn("Production deployment is approved", out["answer"])
        self.assertEqual(out["answer"], "- Release was planned. [episode:1]")

    def test_rendering_keeps_whole_claims_and_their_citations_at_the_cap(self):
        claims = [{"text": "x" * 500, "episode_ids": [1], "sources": ["episode:1"]}]
        for render, cap in ((briefs._final_body, "MAX_BODY_CHARS"),
                            (reflect._final_answer, "MAX_ANSWER_CHARS")):
            with self.subTest(render=render.__name__), mock.patch.object(
                briefs if render is briefs._final_body else reflect, cap, 520,
            ):
                result = render("UNSUPPORTED", claims * 2, 0)
                self.assertEqual(result, "- " + "x" * 500 + " [episode:1]")


class LiveBriefFreshnessTest(unittest.TestCase):
    def test_read_detects_edited_source_without_planning_or_rebuilding(self):
        hub = _seeded()
        _build(hub)
        hub.episodes[1]["summary"] = "The pagination decision was corrected."
        with hub.cursor() as cur:
            out = briefs.read_brief(cur, "alpha")
        self.assertEqual(out["state"], "stale")

    def test_read_detects_new_source_without_planning_or_rebuilding(self):
        hub = _seeded()
        _build(hub)
        hub.episode(3, "New decision: roll back the release.", minutes=9)
        with hub.cursor() as cur:
            out = briefs.read_brief(cur, "alpha")
        self.assertEqual(out["state"], "stale")

    def test_read_detects_changed_decision_and_old_derivation(self):
        for change in ("decision", "version"):
            with self.subTest(change=change):
                hub = _seeded()
                hub.decision(10, 1, "Use cursor pagination")
                _build(hub)
                if change == "decision":
                    hub.decisions[10]["retracted_at"] = "2026-10-10"
                else:
                    hub.current("alpha")["derivation_version"] -= 1
                with hub.cursor() as cur:
                    self.assertEqual(briefs.read_brief(cur, "alpha")["state"], "stale")

    def test_unrelated_capture_keeps_brief_current_and_reads_do_not_write(self):
        hub = _seeded()
        _build(hub)
        hub.episode(3, "Other topic.", topics=("beta",))
        hub.statements.clear()
        with hub.cursor() as cur:
            self.assertEqual(briefs.read_brief(cur, "alpha")["state"], "current")
        self.assertTrue(all(sql.startswith("SELECT") for sql in hub.statements))

    def test_reflection_does_not_prefer_a_brief_after_its_sources_change(self):
        hub = _ReflectHub()
        hub.episode(1, "Release planned.")
        hub.bodies["alpha"] = "PAGE: release delayed."
        _build(hub, answer=_answer([_claim("BRIEF: release planned.", 1)], body="BRIEF: release planned."))
        hub.episode(2, "Release delayed.", minutes=9)
        with mock.patch("khipu.features.enabled", return_value=True):
            _, prompts = _run(hub, _reflection_answer(
                [_reflection_claim("Release delayed.", "topic:alpha")],
            ), rows=_rows(topics=("alpha",)))
        self.assertIn("PAGE: release delayed", prompts[0])
        self.assertNotIn("BRIEF: release planned", prompts[0])


class BriefBuildOutcomeTest(unittest.TestCase):
    def test_cli_reports_invalid_json_and_zero_claims_as_failure(self):
        for answer in ("not json", _answer([_claim("No citation.")])):
            with self.subTest(answer=answer):
                hub = _seeded()
                stdout = io.StringIO()
                with mock.patch("khipu.features.enabled", return_value=True), \
                        mock.patch("khipu.db.connect", return_value=hub), \
                        mock.patch("khipu.extract._generate", return_value=answer), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    rc = briefs.cli_main(SimpleNamespace(briefs_cmd="build", topic=None, limit=5))
                out = json.loads(stdout.getvalue())
                self.assertEqual(out["results"][0]["status"], "failed")
                self.assertFalse(out["ok"])
                self.assertEqual(rc, 1)

    def test_cli_reports_per_topic_unavailability_as_failure(self):
        hub = _seeded()
        stdout = io.StringIO()
        with mock.patch("khipu.features.enabled", return_value=True), \
                mock.patch("khipu.db.connect", return_value=hub), \
                mock.patch("khipu.briefs.build_many", return_value=[
                    {"topic": "alpha", "status": "unavailable"},
                ]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            rc = briefs.cli_main(SimpleNamespace(briefs_cmd="build", topic=None, limit=5))
        self.assertFalse(json.loads(stdout.getvalue())["ok"])
        self.assertEqual(rc, 1)


class LegacyDecisionValidityTest(unittest.TestCase):
    def test_repeated_legacy_decision_cannot_bypass_registry_lifecycle(self):
        for state in ("retracted", "superseded"):
            with self.subTest(state=state):
                hub = _ReflectHub()
                hub.episode(1, "Original note.", decisions=["Use offset pagination"])
                hub.episode(2, "Later note.", decisions=["Use offset pagination"])
                hub.decision(10, 1, "Use offset pagination",
                             retracted=state == "retracted",
                             superseded_by=11 if state == "superseded" else None)
                _, gen = _build(hub, answer=_answer([_claim("Later note.", 2)]))
                _, prompts = _run(hub, _reflection_answer(
                    [_reflection_claim("Later note.", "episode:2")],
                ), rows=_rows(2))
                brief_source = gen.call_args.args[0].split("[episode 2,", 1)[1].split("\n\n", 1)[0]
                self.assertNotIn("- Use offset pagination", brief_source)
                self.assertNotIn("Use offset pagination", prompts[0])

    def test_legacy_fallback_is_retained_without_a_registry(self):
        hub = _ReflectHub(decisions_table=False)
        hub.episode(1, "Legacy note.", decisions=["Use cursor pagination"])
        _, gen = _build(hub, answer=_answer([_claim("Legacy note.", 1)]))
        _, prompts = _run(hub, _reflection_answer(
            [_reflection_claim("Legacy note.", "episode:1")],
        ), rows=_rows(1))
        self.assertIn("- Use cursor pagination", gen.call_args.args[0])
        self.assertIn("- Use cursor pagination", prompts[0])


class _CacheCur(_ReflectCur):
    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        h = self.hub
        if "to_regclass('public.memory_query_cache')" in s:
            self._rows = [(True,)]
        elif s.startswith("SELECT embedding::text FROM memory_query_cache"):
            self._rows = [(h.vector,)] if h.vector else []
        elif s.startswith(("UPDATE memory_query_cache", "INSERT INTO memory_query_cache")):
            h.statements.append(s)
            h.cache_writes += 1
            if s.startswith("INSERT"):
                h.vector = params[2]
            self._rows = []
        elif "to_regclass('public.media_assets')" in s:
            self._rows = [(False,)]
        elif s.startswith("SELECT m.kind, m.ref, m.chunk_idx,"):
            self._rows = [("episode", "1", 0, 0.99, "Cursor pagination.",
                           "Cursor pagination.", "Cursor pagination.")]
        else:
            super().execute(sql, params)


class _CacheHub(_ReflectHub):
    def __init__(self, *, hit):
        super().__init__()
        self.vector = "[1.0,0.0]" if hit else None
        self.cache_writes = 0
        self.episode(1, "Cursor pagination.")

    def cursor(self):
        return _CacheCur(self)


class ReflectionCacheContractTest(unittest.TestCase):
    def test_real_retrieval_cache_hit_and_miss_do_not_write_memory_content(self):
        for hit in (True, False):
            with self.subTest(hit=hit):
                hub = _CacheHub(hit=hit)
                with mock.patch("khipu.db.connect", return_value=hub), \
                        mock.patch("khipu.hub_snapshot.try_hub_connect", return_value=hub), \
                        mock.patch("khipu.embed._active_profile", return_value="test"), \
                        mock.patch("khipu.embed.uses_task_prefixes", return_value=False), \
                        mock.patch("khipu.embed.embed_one", return_value=[1.0, 0.0]) as transport, \
                        mock.patch("khipu.cli._literal_candidates", return_value=[]), \
                        mock.patch("khipu.embed._apply_search_filters", side_effect=lambda cur, rows, **kw: rows), \
                        mock.patch("khipu.topic_graph.enrich_search_results", side_effect=lambda cur, rows: rows), \
                        mock.patch("khipu.decisions.enrich_search_results", side_effect=lambda cur, rows: rows), \
                        mock.patch("khipu.features.enabled", return_value=False), \
                        mock.patch("khipu.relevance.enabled", return_value=False), \
                        mock.patch("khipu.rerank._call_model", return_value=(
                            _reflection_answer([_reflection_claim("Cursor pagination.", "episode:1")]), "stub",
                        )):
                    out = reflect.reflect("Cursor pagination")
                self.assertFalse(out["abstained"], out)
                self.assertEqual(hub.cache_writes, 1)
                self.assertEqual(hub.commits, 1)
                self.assertEqual(transport.call_count, 0 if hit else 1)
                mutations = [sql for sql in hub.statements if sql.startswith(("INSERT", "UPDATE", "DELETE"))]
                self.assertTrue(mutations)
                self.assertTrue(all("memory_query_cache" in sql for sql in mutations))
                self.assertEqual(hub.briefs, [])
                self.assertEqual(hub.decisions, {})
                self.assertIn("retrieval caches", reflect.__doc__)
