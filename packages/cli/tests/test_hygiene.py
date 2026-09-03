"""Tests for khipu.hygiene — W5.1 (topics vs tags) and W5.2 (path filter).

No live database: classify_topics and report_junk_paths/backfill_identity_
report are exercised against a small fake cursor that answers exactly the
SQL shapes hygiene.py issues.
"""
from __future__ import annotations

import unittest

from khipu import hygiene


class _FakeCursor:
    def __init__(self, topics: set[str] | None = None, aliases: dict[str, str] | None = None,
                 raise_on_execute: bool = False):
        self.topics = topics or set()
        self.aliases = aliases or {}
        self.raise_on_execute = raise_on_execute
        self._result: list[tuple] = []

    def execute(self, sql, params=None):
        if self.raise_on_execute:
            raise RuntimeError("connection lost")
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("SELECT slug FROM topics WHERE slug = ANY"):
            candidates = params[0]
            self._result = [(c,) for c in candidates if c in self.topics]
        elif s.startswith("SELECT alias, slug FROM topic_aliases"):
            candidates = params[0]
            self._result = [(c, self.aliases[c]) for c in candidates if c in self.aliases]
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)


class IsNoiseSlugTest(unittest.TestCase):
    def test_worktree_hex_suffix(self):
        self.assertTrue(hygiene.is_noise_slug("left-panel-ux-97b929"))

    def test_harness_names(self):
        for name in ("claude", "cursor", "codex", "aegis", "grok", "claude-code"):
            self.assertTrue(hygiene.is_noise_slug(name), name)

    def test_generic_list(self):
        for name in ("tmp", "general", "misc", "chat", "test"):
            self.assertTrue(hygiene.is_noise_slug(name), name)

    def test_real_topic_is_not_noise(self):
        self.assertFalse(hygiene.is_noise_slug("khipu-memory-reliability"))

    def test_blank_is_noise(self):
        self.assertTrue(hygiene.is_noise_slug(""))
        self.assertTrue(hygiene.is_noise_slug(None))


class ClassifyTopicsTest(unittest.TestCase):
    def test_exact_match_resolves(self):
        cur = _FakeCursor(topics={"khipu-memory-reliability"})
        resolved, tags, unresolved = hygiene.classify_topics(cur, ["khipu-memory-reliability"])
        self.assertEqual(resolved, ["khipu-memory-reliability"])
        self.assertEqual(tags, [])
        self.assertFalse(unresolved)

    def test_alias_resolves_to_canonical_slug(self):
        cur = _FakeCursor(topics={"khipu-memory-reliability"},
                          aliases={"memory-reliability": "khipu-memory-reliability"})
        resolved, tags, unresolved = hygiene.classify_topics(cur, ["memory-reliability"])
        self.assertEqual(resolved, ["khipu-memory-reliability"])
        self.assertEqual(tags, [])

    def test_previously_unseen_slug_stays_a_topic_and_gets_minted(self):
        """fix 8 (regression fix, not a test-to-pass edit): a clean,
        never-before-seen slug is NOT demoted to a tag just because no page
        exists for it yet — hub/cloud captures never see the file wiki, so
        the old "has no page yet -> tag" rule tagged every novel topic."""
        cur = _FakeCursor(topics=set())
        resolved, tags, unresolved = hygiene.classify_topics(cur, ["some-invented-slug"])
        self.assertEqual(resolved, ["some-invented-slug"])
        self.assertEqual(tags, [])
        self.assertFalse(unresolved)

    def test_noise_slugs_become_tags_not_topics(self):
        cur = _FakeCursor(topics=set())
        resolved, tags, unresolved = hygiene.classify_topics(
            cur, ["aegis", "left-panel-ux-97b929", "tmp", "real-tag"]
        )
        self.assertEqual(resolved, ["real-tag"])
        self.assertEqual(sorted(tags), sorted(["aegis", "left-panel-ux-97b929", "tmp"]))
        self.assertFalse(unresolved)

    def test_dedup_and_order_preserved(self):
        cur = _FakeCursor(topics={"a"})
        resolved, tags, _ = hygiene.classify_topics(cur, ["a", "A", "b", "a"])
        # "a" is an exact match; "b" is non-noise and previously unseen —
        # both stay topics under the new rule, neither is tagged.
        self.assertEqual(resolved, ["a", "b"])
        self.assertEqual(tags, [])

    def test_empty_input(self):
        cur = _FakeCursor()
        self.assertEqual(hygiene.classify_topics(cur, []), ([], [], False))
        self.assertEqual(hygiene.classify_topics(cur, None), ([], [], False))

    def test_db_error_degrades_to_keep_everything_and_flags_unresolved(self):
        cur = _FakeCursor(raise_on_execute=True)
        resolved, tags, unresolved = hygiene.classify_topics(cur, ["some-slug", "another"])
        self.assertEqual(sorted(resolved), ["another", "some-slug"])
        self.assertEqual(tags, [])
        self.assertTrue(unresolved)

    def test_no_cursor_at_all_degrades_the_same_way(self):
        resolved, tags, unresolved = hygiene.classify_topics(None, ["some-slug"])
        self.assertEqual(resolved, ["some-slug"])
        self.assertTrue(unresolved)


class IsRealPathTest(unittest.TestCase):
    def test_extension_counts(self):
        self.assertTrue(hygiene.is_real_path("packages/cli/khipu/capture.py"))

    def test_leading_markers_count(self):
        self.assertTrue(hygiene.is_real_path("/absolute/path"))
        self.assertTrue(hygiene.is_real_path("~/home/path"))
        self.assertTrue(hygiene.is_real_path("./relative/path"))

    def test_junk_shape_without_repo_root_fails(self):
        self.assertFalse(hygiene.is_real_path("a/b"))
        self.assertFalse(hygiene.is_real_path("foo/bar"))

    def test_exists_under_repo_root_counts(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "sub").mkdir()
            (Path(td) / "sub" / "dir").mkdir()
            self.assertTrue(hygiene.is_real_path("sub/dir", repo_root=td))

    def test_blank_is_never_a_path(self):
        self.assertFalse(hygiene.is_real_path(""))
        self.assertFalse(hygiene.is_real_path(None))

    def test_filter_real_paths(self):
        out = hygiene.filter_real_paths(["a/b", "foo.py", "c/d/e"])
        self.assertEqual(out, ["foo.py"])


class ReportJunkPathsTest(unittest.TestCase):
    class _NodesCursor:
        def __init__(self, ids):
            self.ids = ids

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [(i,) for i in self.ids]

    def test_counts_and_samples_failing_nodes(self):
        cur = self._NodesCursor(["path:a/b", "path:foo.py", "path:c/d"])
        report = hygiene.report_junk_paths(cur)
        self.assertEqual(report["total_path_nodes"], 3)
        self.assertEqual(report["failing"], 2)
        self.assertEqual(sorted(report["sample"]), ["path:a/b", "path:c/d"])

    def test_all_real_paths_report_zero_failing(self):
        cur = self._NodesCursor(["path:foo.py", "path:/abs/bar"])
        report = hygiene.report_junk_paths(cur)
        self.assertEqual(report["failing"], 0)


class ApplyPurgeJunkPathsTest(unittest.TestCase):
    def test_deletes_only_the_failing_nodes(self):
        class _Cur:
            def __init__(self):
                self.deletes = []

            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if s.startswith("SELECT id FROM nodes"):
                    self._rows = [("path:a/b",), ("path:foo.py",)]
                elif s.startswith("DELETE FROM edges") or s.startswith("DELETE FROM nodes"):
                    self.deletes.append((s, params))

            def fetchall(self):
                return self._rows

        cur = _Cur()
        out = hygiene.apply_purge_junk_paths(cur)
        self.assertEqual(out["deleted_nodes"], 1)
        self.assertEqual(out["sample"], ["path:a/b"])
        self.assertEqual(len(cur.deletes), 2)  # edges then nodes


class BackfillIdentityReportTest(unittest.TestCase):
    def test_reports_count_and_sample(self):
        class _Cur:
            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if s.startswith("SELECT id, scope, session_id"):
                    self._rows = [(1, "/abs/path", "sid1"), (2, "/other/path", "sid2")]
                elif s.startswith("SELECT COUNT(*)"):
                    self._count = (2,)

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._count

        cur = _Cur()
        report = hygiene.backfill_identity_report(cur)
        self.assertEqual(report["would_backfill"], 2)
        self.assertEqual(len(report["sample"]), 2)
        self.assertEqual(report["sample"][0]["scope"], "/abs/path")


if __name__ == "__main__":
    unittest.main()


class RealPathDirectoryShapesTest(unittest.TestCase):
    """Regression: real multi-segment directories were rejected while the rule
    only accepted an extension or a leading marker (the live fill-dir probe
    lost its path: node). Junk shapes must still be rejected."""

    def test_trailing_slash_directory_is_a_path(self):
        from khipu.hygiene import is_real_path
        self.assertTrue(is_real_path("sojourn/art-samples/uw-intro-acut-fill-2026-07-26/"))
        self.assertTrue(is_real_path("sojourn_art/nephilim/klein_style_lora/corpus/nphlm_style/"))

    def test_three_lowercase_segments_with_separator_is_a_path(self):
        from khipu.hygiene import is_real_path
        self.assertTrue(is_real_path("Code/aegis/.claude/worktrees/w59-1015-perf"))

    def test_junk_shapes_still_rejected(self):
        from khipu.hygiene import is_real_path
        for junk in ("UI/jobs", "add/remove", "push-channel/latency", "ideas/mac-hands",
                     "SPY/QQQ/IWM", "IC/PCS/CCS/COMBO", "60/90/120", "VoiceTyper/LazyType/dictate",
                     "TheHindSight/biblestudy", "0.118/leg"):
            self.assertFalse(is_real_path(junk), junk)
