"""khipu.recall_rule is the prompt-time text that tells a model Khipu exists.

It ships from one source (RULE_MD) into Claude/Codex SessionStart hooks, a Cursor
sessionStart push (additional_context), and a Cursor .mdc pull rule; these tests
exist to keep the shapes from drifting and to keep the rule honest about what it
promises, since nothing else checks it.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from khipu import recall_rule as rr


class RuleTextTest(unittest.TestCase):
    def test_it_names_every_tool_the_mcp_server_exposes(self):
        from khipu.mcp_server import TOOLS

        for tool in (t["name"] for t in TOOLS):
            with self.subTest(tool=tool):
                self.assertIn(tool, rr.RULE_MD)

    def test_it_states_the_cadence_rather_than_demanding_recall_every_turn(self):
        self.assertIn("on demand", rr.RULE_MD)

    def test_it_warns_that_capture_declines_where_a_hook_already_runs(self):
        """Without this a model reads a declined khipu_capture as a failure and
        retries it every turn."""
        self.assertIn("declines", rr.RULE_MD)
        self.assertIn("dual", rr.RULE_MD)

    def test_it_explains_the_semantic_flag_both_ways(self):
        self.assertIn("semantic", rr.RULE_MD)

    def test_it_says_digit_ids_are_episodes_not_graph_nodes(self):
        self.assertIn("Digit ids are episodes", rr.RULE_MD)
        self.assertIn("fused by reciprocal-rank fusion", rr.RULE_MD)

    def test_it_states_the_default_mode_and_filters(self):
        self.assertIn("mode` is `hybrid`", rr.RULE_MD)
        self.assertIn("mode: \"literal\"", rr.RULE_MD)
        for f in ("kind", "project", "since", "session_id", "harness"):
            with self.subTest(filter=f):
                self.assertIn(f, rr.RULE_MD)


class ClaudeShapeTest(unittest.TestCase):
    def test_the_hook_context_is_the_rule_with_no_frontmatter(self):
        out = rr.claude_additional_context()
        self.assertFalse(out.startswith("---"))
        self.assertTrue(out.startswith("# Khipu memory"))

    def test_it_is_stripped_so_the_harness_does_not_pad_the_prompt(self):
        out = rr.claude_additional_context()
        self.assertEqual(out, out.strip())


class CursorShapeTest(unittest.TestCase):
    def test_the_mdc_opens_with_frontmatter_cursor_can_parse(self):
        lines = rr.cursor_mdc().splitlines()
        self.assertEqual(lines[0], "---")
        self.assertIn("alwaysApply: true", lines[:5])

    def test_the_frontmatter_closes_before_the_body(self):
        body = rr.cursor_mdc().split("---", 2)[2]
        self.assertIn("# Khipu memory", body)

    def test_both_shapes_carry_the_identical_rule_body(self):
        """One source of truth: a fix to the rule must reach Cursor and Claude
        Code without either being edited separately."""
        self.assertIn(rr.RULE_MD.strip(), rr.cursor_mdc())
        self.assertIn(rr.claude_additional_context(), rr.cursor_mdc())

    def test_session_start_cursor_shape_uses_additional_context(self):
        with mock.patch.object(rr, "session_start_context", return_value="# Khipu memory\nkhipu_search"):
            with mock.patch.object(rr, "_session_start_cwd", return_value=None):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rr.session_start_main(shape="cursor")
        d = json.loads(buf.getvalue())
        self.assertIn("additional_context", d)
        self.assertNotIn("hookSpecificOutput", d)
        self.assertIn("khipu_search", d["additional_context"])

    def test_session_start_default_shape_is_claude_nested(self):
        with mock.patch.object(rr, "session_start_context", return_value="# Khipu memory\nkhipu_search"):
            with mock.patch.object(rr, "_session_start_cwd", return_value=None):
                with mock.patch.object(rr.sys, "argv", ["khipu-recall-hook"]):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rr.session_start_main()
        d = json.loads(buf.getvalue())
        self.assertIn("hookSpecificOutput", d)
        self.assertIn("khipu_search", d["hookSpecificOutput"]["additionalContext"])

    def test_session_start_ignores_env_shape_without_argv(self):
        """A shell-exported KHIPU_RECALL_SHAPE must not reshape Claude SessionStart."""
        with mock.patch.object(rr, "session_start_context", return_value="# Khipu memory\nkhipu_search"):
            with mock.patch.object(rr, "_session_start_cwd", return_value=None):
                with mock.patch.object(rr.sys, "argv", ["khipu-recall-hook"]):
                    with mock.patch.dict("os.environ", {"KHIPU_RECALL_SHAPE": "cursor"}):
                        buf = io.StringIO()
                        with redirect_stdout(buf):
                            rr.session_start_main()
        d = json.loads(buf.getvalue())
        self.assertIn("hookSpecificOutput", d)
        self.assertNotIn("additional_context", d)

    def test_session_start_cwd_reads_cursor_workspace_roots(self):
        payload = json.dumps({"workspace_roots": ["/Volumes/Example/Code/Khipu/packages/cli"]})
        with mock.patch.object(rr.sys, "stdin", io.StringIO(payload)):
            self.assertEqual(
                rr._session_start_cwd(),
                "/Volumes/Example/Code/Khipu/packages/cli",
            )

    def test_session_start_cwd_prefers_explicit_cwd_over_roots(self):
        payload = json.dumps({
            "cwd": "/tmp/explicit",
            "workspace_roots": ["/tmp/root"],
        })
        with mock.patch.object(rr.sys, "stdin", io.StringIO(payload)):
            self.assertEqual(rr._session_start_cwd(), "/tmp/explicit")


class AegisTest(unittest.TestCase):
    def test_the_module_documents_why_aegis_gets_no_rule(self):
        """Aegis's SessionStart is an Observe gate that discards stdout, so the
        absence of a third shape is a verified fact and not an oversight."""
        self.assertIn("Aegis", rr.__doc__)
        self.assertIn("Observe", rr.__doc__)


class PushSliceTest(unittest.TestCase):
    def test_cwd_walks_off_packages_cli(self):
        self.assertEqual(
            rr.cwd_search_term("/Volumes/Example/Code/Khipu/packages/cli"),
            "Khipu",
        )

    def test_empty_cwd(self):
        self.assertEqual(rr.cwd_search_term(None), "")
        self.assertEqual(rr.cwd_search_term(""), "")

    def test_session_start_appends_slice(self):
        with mock.patch.object(
            rr, "_pushed_memory_slice", return_value="## Pushed slice\n- episode `1`: hi"
        ):
            out = rr.session_start_context("/tmp/Khipu")
        self.assertTrue(out.startswith("# Khipu memory"))
        self.assertIn("## Pushed slice", out)
        self.assertNotEqual(out, rr.claude_additional_context())

    def test_session_start_falls_back_when_slice_raises(self):
        with mock.patch.object(
            rr, "_pushed_memory_slice", side_effect=RuntimeError("nope")
        ):
            out = rr.session_start_context("/tmp")
        self.assertEqual(out, rr.RULE_MD.strip())


class RepoScopedSliceTest(unittest.TestCase):
    """W4: _pushed_memory_slice resolves repo_root/project first and prefers
    a repo-scoped slice (commitments -> episodes -> topics) over the
    cwd-token search, which stays the fallback for no-project or empty
    results, and hub_snapshot.open_snapshot backs a `stale` degrade on a PG
    failure. identity.resolve_repo_root/activity.project_slice/query_log/
    hub_snapshot are all mocked — no real DB or filesystem git call."""

    def setUp(self):
        os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)

    def test_repo_scoped_slice_wins_over_cwd_token_search_when_populated(self):
        with mock.patch(
            "khipu.identity.resolve_repo_root",
            return_value={"repo_root": "/repo/khipu", "project": "acme/khipu"},
        ), mock.patch(
            "khipu.activity.project_slice",
            return_value={
                "commitments": [{"id": 1, "text": "ship it", "kind": "followup"}],
                "episodes": [{"id": 5, "summary": "did the thing"}],
                "topics": [{"slug": "khipu", "title": "Khipu", "age_days": 2}],
            },
        ) as m_slice, mock.patch("khipu.cli._search_query") as m_search:
            out = rr._pushed_memory_slice("/repo/khipu")
        m_search.assert_not_called()
        self.assertIn("project: `acme/khipu`", out)
        self.assertIn("Open commitments", out)
        self.assertIn("ship it", out)
        self.assertIn("Recent episodes", out)
        self.assertIn("Linked topics", out)
        self.assertIn("2d old", out)
        self.assertEqual(m_slice.call_args.kwargs["project"], "acme/khipu")
        self.assertEqual(m_slice.call_args.kwargs["repo_root"], "/repo/khipu")

    def test_empty_project_slice_falls_through_to_cwd_token_search(self):
        with mock.patch(
            "khipu.identity.resolve_repo_root",
            return_value={"repo_root": "/repo/khipu", "project": "acme/khipu"},
        ), mock.patch(
            "khipu.activity.project_slice",
            return_value={"commitments": [], "episodes": [], "topics": []},
        ), mock.patch("khipu.cli._search_query", return_value=[]), mock.patch(
            "khipu.activity.recent_episodes",
            return_value=[{"id": 9, "summary": "fallback episode"}],
        ), mock.patch("khipu.db.connect"):
            out = rr._pushed_memory_slice("/repo/khipu")
        self.assertNotIn("project: `acme/khipu`", out)
        self.assertIn("fallback episode", out)

    def test_pg_failure_degrades_to_stale_snapshot_and_logs_the_miss(self):
        class _FakeSnapshotCursor:
            def fetchall(self_inner):
                return [(11, "snapshot episode", json.dumps({"project": "acme/khipu"}))]

        class _FakeSnapshotCon:
            def execute(self_inner, sql, params=None):
                return _FakeSnapshotCursor()

            def close(self_inner):
                pass

        with mock.patch(
            "khipu.identity.resolve_repo_root",
            return_value={"repo_root": "/repo/khipu", "project": "acme/khipu"},
        ), mock.patch(
            "khipu.activity.project_slice", side_effect=RuntimeError("hub unreachable")
        ), mock.patch(
            "khipu.hub_snapshot.open_snapshot", return_value=_FakeSnapshotCon()
        ), mock.patch("khipu.query_log.log_query") as m_log:
            out = rr._pushed_memory_slice("/repo/khipu")
        self.assertIn("stale", out)
        self.assertIn("snapshot episode", out)
        m_log.assert_called_once()
        self.assertEqual(m_log.call_args.kwargs["mode"], "slice")
        self.assertEqual(m_log.call_args.kwargs["result_count"], 0)

    def test_pg_failure_with_no_snapshot_falls_through_to_cwd_token_search(self):
        with mock.patch(
            "khipu.identity.resolve_repo_root",
            return_value={"repo_root": "/repo/khipu", "project": "acme/khipu"},
        ), mock.patch(
            "khipu.activity.project_slice", side_effect=RuntimeError("hub unreachable")
        ), mock.patch(
            "khipu.hub_snapshot.open_snapshot", side_effect=FileNotFoundError("no snapshot")
        ), mock.patch("khipu.query_log.log_query"), mock.patch(
            "khipu.cli._search_query", return_value=[]
        ), mock.patch(
            "khipu.activity.recent_episodes",
            return_value=[{"id": 3, "summary": "last resort episode"}],
        ), mock.patch("khipu.db.connect"):
            out = rr._pushed_memory_slice("/repo/khipu")
        self.assertIn("last resort episode", out)

    def test_host_session_id_used_when_no_project_resolves(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_HOST_SESSION_ID": "host-9"}), mock.patch(
            "khipu.identity.resolve_repo_root",
            return_value={"repo_root": None, "project": None},
        ), mock.patch(
            "khipu.activity.project_slice",
            return_value={"commitments": [], "episodes": [{"id": 1, "summary": "sibling work"}], "topics": []},
        ) as m_slice, mock.patch("khipu.cli._search_query") as m_search:
            out = rr._pushed_memory_slice("/tmp/scratchpad")
        m_search.assert_not_called()
        self.assertEqual(m_slice.call_args.kwargs["host_session_id"], "claude_code:host-9")
        self.assertIn("sibling work", out)

    def test_no_project_and_no_host_id_goes_straight_to_cwd_token_search(self):
        with mock.patch(
            "khipu.identity.resolve_repo_root", return_value={"repo_root": None, "project": None}
        ), mock.patch("khipu.activity.project_slice") as m_slice, mock.patch(
            "khipu.cli._search_query", return_value=[]
        ), mock.patch(
            "khipu.activity.recent_episodes", return_value=[{"id": 4, "summary": "recents only"}]
        ), mock.patch("khipu.db.connect"):
            out = rr._pushed_memory_slice("/tmp")
        m_slice.assert_not_called()
        self.assertIn("recents only", out)


class SliceBudgetTest(unittest.TestCase):
    def test_fit_budget_drops_whole_trailing_lines_not_mid_word(self):
        lines = ["a" * 100 for _ in range(100)]
        out = rr._fit_budget(lines, budget=550)
        self.assertLess(len(out), len(lines))
        self.assertTrue(all(line == "a" * 100 for line in out))

    def test_fit_budget_always_keeps_at_least_the_first_line(self):
        lines = ["x" * 10000]
        out = rr._fit_budget(lines, budget=10)
        self.assertEqual(out, lines)


if __name__ == "__main__":
    unittest.main()
