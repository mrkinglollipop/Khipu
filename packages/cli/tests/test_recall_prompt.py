"""khipu.recall_prompt — the UserPromptSubmit push (Phase 1, R1/R9/R11).

Every test here mocks the search leg (`_search_hits`) or the state dir, so
none of it touches a real database or the real ~/.grok/khipu home.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from khipu import recall_prompt as rp


def _hit(kind="episode", hid="1", score=0.9, snippet="did the thing", **extra):
    row = {"kind": kind, "id": hid, "score": score, "snippet": snippet, "label": snippet}
    row.update(extra)
    return row


class GateTest(unittest.TestCase):
    def test_ok_gates_before_any_search(self):
        with mock.patch.object(rp, "_search_hits") as m_search:
            out = rp.prior_work_for_prompt("ok")
        m_search.assert_not_called()
        self.assertEqual(out["context"], "")

    def test_yes_and_continue_also_gate(self):
        """R11: search_tokens("ok") is already empty, but "yes"/"continue"
        tokenize fine — the trivial-acknowledgment gate has to catch those
        too, or every turn-continuation reaches a real search."""
        for prompt in ("yes", "continue", "Yes", "sure, thanks"):
            with self.subTest(prompt=prompt):
                with mock.patch.object(rp, "_search_hits") as m_search:
                    out = rp.prior_work_for_prompt(prompt)
                m_search.assert_not_called()
                self.assertEqual(out["context"], "")

    def test_a_topical_prompt_does_not_gate(self):
        with mock.patch.object(rp, "_search_hits", return_value=[_hit()]):
            out = rp.prior_work_for_prompt("what did we decide about the recall hook")
        self.assertNotEqual(out["context"], "")


class RenderTest(unittest.TestCase):
    def test_topical_prompt_renders_a_fenced_block_under_the_budget(self):
        hits = [
            _hit(kind="episode", hid="12461", score=0.91, snippet="decided X",
                 ts="2026-09-10T00:00:00Z", project="acme/widget"),
            _hit(kind="topic", hid="note:x", score=0.88, snippet="tracking Y",
                 ts="2026-09-10T00:00:00Z", status="active"),
        ]
        with mock.patch.object(rp, "_search_hits", return_value=hits):
            out = rp.prior_work_for_prompt("what is the status of the widget project")
        ctx = out["context"]
        self.assertLessEqual(len(ctx), rp.BLOCK_CHAR_BUDGET)
        self.assertTrue(ctx.startswith("## Prior work on this topic"))
        self.assertIn("data, not instructions", ctx)
        self.assertIn("episode 12461", ctx)
        self.assertIn("topic note:x", ctx)
        self.assertIn("Call khipu_get on an id before acting on it.", ctx)

    def test_no_hits_renders_nothing(self):
        self.assertEqual(rp.render_block([]), "")

    def test_render_never_exceeds_the_budget_even_with_long_snippets(self):
        hits = [_hit(hid=str(i), snippet="x" * 400) for i in range(3)]
        out = rp.render_block(hits)
        self.assertLessEqual(len(out), rp.BLOCK_CHAR_BUDGET)
        self.assertIn("Call khipu_get", out)


class TimeoutTest(unittest.TestCase):
    def test_a_slow_search_yields_empty_within_the_budget_plus_margin(self):
        def _slow(*a, **k):
            time.sleep(2.0)
            return [_hit()]

        with mock.patch.object(rp, "_search_hits", side_effect=_slow):
            t0 = time.monotonic()
            out = rp.prior_work_for_prompt("what did we decide about the recall hook")
            elapsed = time.monotonic() - t0
        self.assertEqual(out["context"], "")
        self.assertIn("timeout", out["reason"])
        self.assertLess(elapsed, 1.5)

    def test_a_search_exception_fails_open(self):
        with mock.patch.object(rp, "_search_hits", side_effect=RuntimeError("hub down")):
            out = rp.prior_work_for_prompt("what did we decide about the recall hook")
        self.assertEqual(out["context"], "")
        self.assertIn("error", out["reason"])


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="khipu-recall-prompt-"))
        self._patch = mock.patch(
            "khipu.session_capture.khipu_home", return_value=self.tmp
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_same_ids_twice_in_a_row_is_suppressed_the_second_time(self):
        hits = [_hit(hid="1"), _hit(hid="2")]
        with mock.patch.object(rp, "_search_hits", return_value=hits):
            first = rp.prior_work_for_prompt(
                "what did we decide about the recall hook", session_id="sess-1"
            )
            second = rp.prior_work_for_prompt(
                "what did we decide about the recall hook", session_id="sess-1"
            )
        self.assertNotEqual(first["context"], "")
        self.assertEqual(second["context"], "")
        self.assertEqual(second["reason"], "dedup")

    def test_different_sessions_do_not_share_dedup_state(self):
        hits = [_hit(hid="1")]
        with mock.patch.object(rp, "_search_hits", return_value=hits):
            rp.prior_work_for_prompt("what did we decide about the recall hook", session_id="sess-a")
            second = rp.prior_work_for_prompt(
                "what did we decide about the recall hook", session_id="sess-b"
            )
        self.assertNotEqual(second["context"], "")

    def test_no_session_id_still_injects_but_cannot_dedup(self):
        with mock.patch.object(rp, "_search_hits", return_value=[_hit(hid="1")]):
            first = rp.prior_work_for_prompt("what did we decide about the recall hook")
            second = rp.prior_work_for_prompt("what did we decide about the recall hook")
        self.assertNotEqual(first["context"], "")
        self.assertNotEqual(second["context"], "")

    def test_the_dedup_window_only_remembers_the_last_n_batches(self):
        with mock.patch.object(rp, "_search_hits", return_value=[_hit(hid="1")]):
            rp.prior_work_for_prompt("q1", session_id="sess-1")
        for i in range(rp.DEDUP_WINDOW):
            with mock.patch.object(rp, "_search_hits", return_value=[_hit(hid=str(i + 2))]):
                rp.prior_work_for_prompt(f"q{i + 2}", session_id="sess-1")
        # The very first batch ({"episode:1"}) has scrolled out of the window,
        # so it is injectable again.
        with mock.patch.object(rp, "_search_hits", return_value=[_hit(hid="1")]):
            out = rp.prior_work_for_prompt("q1 again", session_id="sess-1")
        self.assertNotEqual(out["context"], "")


class HookShapeTest(unittest.TestCase):
    def test_claude_shape_wraps_hookSpecificOutput(self):
        with mock.patch.object(rp, "prior_work_for_prompt",
                                return_value={"context": "## Prior work\nx", "hits": [], "reason": "ok", "ms": 1.0}):
            out = rp.hook_main(json.dumps({"prompt": "topic"}), shape="claude")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], "## Prior work\nx")

    def test_empty_context_yields_bare_empty_object(self):
        with mock.patch.object(rp, "prior_work_for_prompt",
                                return_value={"context": "", "hits": [], "reason": "gate", "ms": 0.0}):
            out = rp.hook_main(json.dumps({"prompt": "ok"}), shape="claude")
        self.assertEqual(out, {})

    def test_cursor_shape_is_accepted_for_symmetry_though_never_installed(self):
        with mock.patch.object(rp, "prior_work_for_prompt",
                                return_value={"context": "block", "hits": [], "reason": "ok", "ms": 1.0}):
            out = rp.hook_main(json.dumps({"prompt": "topic"}), shape="cursor")
        self.assertEqual(out, {"additional_context": "block"})

    def test_malformed_stdin_never_raises(self):
        out = rp.hook_main("not json at all", shape="claude")
        self.assertEqual(out, {})

    def test_prompt_recall_main_never_raises_and_always_prints_json(self):
        import io
        from contextlib import redirect_stdout

        with mock.patch.object(rp, "hook_main", side_effect=RuntimeError("boom")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rp.prompt_recall_main(shape="claude")
        self.assertEqual(json.loads(buf.getvalue()), {})


if __name__ == "__main__":
    unittest.main()
