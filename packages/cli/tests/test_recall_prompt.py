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


class QueryEmbedCacheTest(unittest.TestCase):
    """R1 follow-up: the local query-embedding cache — a repeated prompt
    must cost zero API calls."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="khipu-qembed-"))
        self._patch = mock.patch.object(rp, "_query_embed_cache_path", return_value=self.tmp / "c.json")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_a_repeated_prompt_never_calls_embed_again(self):
        with mock.patch("khipu.embed.embed_one", return_value=[0.1, 0.2, 0.3]) as m:
            first = rp._cached_query_embed("what is the status", "p1")
            second = rp._cached_query_embed("What Is The Status", "p1")  # case/whitespace differ
        self.assertEqual(first, [0.1, 0.2, 0.3])
        self.assertEqual(second, [0.1, 0.2, 0.3])
        m.assert_called_once()

    def test_different_profiles_do_not_share_a_cache_entry(self):
        with mock.patch("khipu.embed.embed_one", side_effect=[[0.1], [0.2]]) as m:
            a = rp._cached_query_embed("q", "profile-a")
            b = rp._cached_query_embed("q", "profile-b")
        self.assertNotEqual(a, b)
        self.assertEqual(m.call_count, 2)

    def test_cache_survives_a_fresh_load(self):
        with mock.patch("khipu.embed.embed_one", return_value=[9.0]) as m:
            rp._cached_query_embed("persisted", "p1")
        with mock.patch("khipu.embed.embed_one") as m2:
            out = rp._cached_query_embed("persisted", "p1")
        self.assertEqual(out, [9.0])
        m2.assert_not_called()


class SnapshotSearchHitsTest(unittest.TestCase):
    """R1 follow-up: local-snapshot-first, hub-only-on-fallback."""

    def test_stale_or_missing_snapshot_raises_snapshot_unusable(self):
        with mock.patch("khipu.hub_snapshot.snapshot_is_fresh", return_value=(False, {"exists": False})):
            with self.assertRaises(rp._SnapshotUnusable):
                rp._snapshot_search_hits("a topical prompt", project=None)

    def test_fresh_snapshot_fuses_cosine_and_lexical_and_attaches_metadata(self):
        cosine_row = {"kind": "topic", "id": "t1", "chunk_idx": 0, "score": 0.9,
                       "label": "T1", "snippet": "topic body", "rank_text": "topic body"}
        lexical_row = {"kind": "episode", "id": "5", "label": "ep", "snippet": "episode text"}
        with mock.patch("khipu.hub_snapshot.snapshot_is_fresh", return_value=(True, {"exists": True})), \
                mock.patch("khipu.hub_snapshot.search_snapshot", return_value=[lexical_row]), \
                mock.patch("khipu.hub_snapshot.active_snapshot_profile", return_value="p1"), \
                mock.patch.object(rp, "_cached_query_embed", return_value=[1.0]), \
                mock.patch("khipu.hub_snapshot.cosine_candidates_snapshot", return_value=[cosine_row]), \
                mock.patch("khipu.hub_snapshot.open_snapshot", return_value=object()), \
                mock.patch("khipu.hub_snapshot.snapshot_row_metadata", side_effect=lambda con, rows: rows):
            out = rp._snapshot_search_hits("a topical prompt", project=None)
        ids = {(r["kind"], r["id"]) for r in out}
        self.assertIn(("topic", "t1"), ids)
        self.assertIn(("episode", "5"), ids)

    def test_a_cosine_leg_failure_degrades_to_lexical_only_not_a_raise(self):
        lexical_row = {"kind": "episode", "id": "5", "label": "ep", "snippet": "episode text"}
        with mock.patch("khipu.hub_snapshot.snapshot_is_fresh", return_value=(True, {"exists": True})), \
                mock.patch("khipu.hub_snapshot.search_snapshot", return_value=[lexical_row]), \
                mock.patch("khipu.hub_snapshot.active_snapshot_profile", return_value="p1"), \
                mock.patch.object(rp, "_cached_query_embed", side_effect=RuntimeError("embed down")), \
                mock.patch("khipu.hub_snapshot.open_snapshot", return_value=object()), \
                mock.patch("khipu.hub_snapshot.snapshot_row_metadata", side_effect=lambda con, rows: rows):
            out = rp._snapshot_search_hits("a topical prompt", project=None)
        self.assertEqual([r["id"] for r in out], ["5"])

    def test_search_hits_falls_back_to_the_hub_when_the_snapshot_is_unusable(self):
        hub_hit = {"kind": "episode", "id": "1", "score": 0.5, "label": "x", "snippet": "x"}
        with mock.patch.object(rp, "_snapshot_search_hits", side_effect=rp._SnapshotUnusable("missing")), \
                mock.patch("khipu.embed.hybrid_search", return_value={"results": [hub_hit]}) as m_hub:
            out = rp._search_hits("a topical prompt", cwd=None)
        m_hub.assert_called_once()
        self.assertEqual(out[0]["id"], "1")

    def test_search_hits_uses_the_snapshot_without_touching_the_hub_when_it_works(self):
        snap_hit = {"kind": "episode", "id": "9", "score": 0.5, "label": "x", "snippet": "x"}
        with mock.patch.object(rp, "_snapshot_search_hits", return_value=[snap_hit]), \
                mock.patch("khipu.embed.hybrid_search") as m_hub:
            out = rp._search_hits("a topical prompt", cwd=None)
        m_hub.assert_not_called()
        self.assertEqual(out[0]["id"], "9")


if __name__ == "__main__":
    unittest.main()
