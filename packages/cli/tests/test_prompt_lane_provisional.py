# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this change (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""The per-prompt lane's keyword-only result: published as soon as the keyword
leg finishes, returned when the outer limit passes first, and never a reason
to change what a search that finishes in time returns.

Everything is faked: no replica, no embedding call, no real clock beyond the
short sleeps that stand in for a slow leg (TIMEOUT_S and LOCAL_LANE_DEADLINE_S
are patched down so they stay short).
"""
from __future__ import annotations

import contextlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from khipu import recall_prompt as rp
from tests.test_prompt_lane import _fresh_cosine_row, _reference_snapshot_search_hits

PROMPT = "recall hook decision"
LIMIT_S = 0.4
DEADLINE_S = 0.25


def _lexical_row(rid: str = "5", text: str = "recall hook decision text") -> dict:
    return {"kind": "episode", "id": rid, "label": "ep", "snippet": text}


@contextlib.contextmanager
def _lane(*, lexical=None, embed=None, cosine=None, profile="p1", metadata=None,
          keyword=None, counts=None, project=None):
    """A snapshot lane whose every collaborator is fake. ``embed`` and
    ``metadata`` are callables standing in for the query embedding and the row
    metadata pass; ``keyword`` replaces the keyword leg outright."""
    rows = [_lexical_row()] if lexical is None else lexical
    if keyword is None:
        def keyword(*_a, **_k):
            return [dict(r) for r in rows]
    if embed is None:
        def embed(*_a, **_k):
            return [1.0]
    if metadata is None:
        def metadata(_con, fused):
            return fused
    if cosine is None:
        def cosine(*_a, **_k):
            return [_fresh_cosine_row()]
    with contextlib.ExitStack() as stack:
        for target, kw in (
            ("khipu.hub_snapshot.snapshot_is_fresh", {"return_value": (True, {"exists": True})}),
            ("khipu.hub_snapshot.search_snapshot", {"side_effect": keyword}),
            ("khipu.hub_snapshot.active_snapshot_profile", {"return_value": profile}),
            ("khipu.hub_snapshot.cosine_candidates_snapshot", {"side_effect": cosine}),
            ("khipu.hub_snapshot.open_snapshot", {"return_value": object()}),
            ("khipu.hub_snapshot.snapshot_row_metadata", {"side_effect": metadata}),
            ("khipu.hub_snapshot.decision_counts_snapshot", {"return_value": counts or {}}),
        ):
            stack.enter_context(mock.patch(target, **kw))
        stack.enter_context(mock.patch.object(rp, "_cached_query_embed", side_effect=embed))
        stack.enter_context(mock.patch.object(rp, "_project_for_cwd", side_effect=lambda _c: project))
        stack.enter_context(mock.patch.object(rp, "TIMEOUT_S", LIMIT_S))
        stack.enter_context(mock.patch.object(rp, "LOCAL_LANE_DEADLINE_S", DEADLINE_S))
        yield


def _slow(seconds: float, value):
    def _fn(*args, **_k):
        time.sleep(seconds)
        return value(*args) if callable(value) else value
    return _fn


def _second_call_slow():
    """A row-metadata stand-in that is quick the first time (the keyword-only
    pass) and slow every time after (the full pass)."""
    calls: list[int] = []

    def _metadata(_con, fused):
        calls.append(1)
        if len(calls) > 1:
            time.sleep(2.0)
        return fused

    return _metadata


def _strip_validity(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "validity"} for r in rows]


class BothLegsOnTimeTest(unittest.TestCase):
    def test_output_matches_the_reference_and_the_unpublished_run(self) -> None:
        with _lane():
            progress: dict = {}
            published = rp._search_hits(PROMPT, cwd=None, limit=8, progress=progress)
            plain = rp._search_hits(PROMPT, cwd=None, limit=8)
            reference = _reference_snapshot_search_hits(PROMPT, project=None)
        self.assertEqual(published, plain)
        self.assertEqual(_strip_validity(published["hits"]), rp._apply_score_floor(reference)[:8])
        self.assertIsNone(published["degraded"])
        self.assertEqual(set(published["legs"]), {"lexical", "cosine"})
        # The keyword-only result was built from copies: it did not disturb the full one.
        self.assertEqual(progress["provisional"]["legs"], ["lexical"])
        self.assertEqual(progress["provisional"]["degraded"], "embedding late")
        self.assertEqual(progress["stage"], "finish")

    def test_through_the_public_entry_point_nothing_changes(self) -> None:
        with _lane():
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertEqual(out["reason"], "ok (no session_id: dedup skipped)")
        self.assertIsNone(out["degraded"])
        self.assertNotIn("stage", out)
        self.assertEqual({h["id"] for h in out["hits"]}, {"5", "t1"})


class LimitPassesAfterTheKeywordLegTest(unittest.TestCase):
    def test_slow_post_processing_returns_the_keyword_rows_not_nothing(self) -> None:
        with _lane(metadata=_second_call_slow()):
            t0 = time.monotonic()
            out = rp.prior_work_for_prompt(PROMPT)
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, LIMIT_S + 0.4)
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])
        self.assertEqual(out["reason"], "ok (no session_id: dedup skipped)")
        self.assertEqual(out["legs"], ["lexical"])
        self.assertEqual(out["degraded"], "embedding late (limit)")
        self.assertNotIn("stage", out)
        self.assertIn("recall hook decision", out["context"])

    def test_the_reason_is_plain_ok_when_there_is_a_session(self) -> None:
        with _lane(metadata=_second_call_slow()), \
                mock.patch.object(rp, "_load_recent_batches", return_value=[]), \
                mock.patch.object(rp, "_save_recent_batches"):
            out = rp.prior_work_for_prompt(PROMPT, session_id="s1")
        self.assertEqual(out["reason"], "ok")
        self.assertEqual(out["degraded"], "embedding late (limit)")

    def test_a_late_embedding_alone_still_reports_embedding_late(self) -> None:
        with _lane(embed=_slow(2.0, [1.0])):
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertEqual(out["degraded"], "embedding late")
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])

    def test_an_embedding_error_reports_embedding_error(self) -> None:
        def _boom(*_a, **_k):
            raise RuntimeError("embed API down")

        with _lane(embed=_boom):
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertEqual(out["degraded"], "embedding error")
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])


class NothingPublishedIsATimeoutTest(unittest.TestCase):
    def test_a_keyword_leg_slower_than_the_limit_names_the_keyword_stage(self) -> None:
        with _lane(keyword=_slow(2.0, [_lexical_row()])):
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertEqual(out["hits"], [])
        self.assertTrue(out["reason"].startswith("timeout>"))
        self.assertEqual(out["stage"], "keyword")
        self.assertEqual(out["degraded"], "timeout")

    def test_a_slow_project_lookup_names_the_project_stage(self) -> None:
        with _lane(), mock.patch.object(rp, "_project_for_cwd", side_effect=_slow(2.0, None)):
            out = rp.prior_work_for_prompt(PROMPT, cwd="/repo")
        self.assertTrue(out["reason"].startswith("timeout>"))
        self.assertEqual(out["stage"], "project")

    def test_without_an_embedding_leg_slow_post_processing_names_the_finish_stage(self) -> None:
        with _lane(profile="", metadata=_slow(2.0, lambda _con, fused: fused)):
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertTrue(out["reason"].startswith("timeout>"))
        self.assertEqual(out["stage"], "finish")

    def test_the_budgeted_path_is_untouched(self) -> None:
        def _slow_budgeted(*_a, **_k):
            time.sleep(2.0)
            return {"hits": [_lexical_row()], "legs": ["lexical"], "degraded": None}

        with mock.patch.object(rp, "_search_hits_budgeted", side_effect=_slow_budgeted), \
                mock.patch.object(rp, "_BUDGET_SAFETY_SLACK_S", 0.0):
            out = rp.prior_work_for_prompt(PROMPT, budget_ms=50)
        self.assertTrue(out["reason"].startswith("timeout>"))
        self.assertEqual(out["hits"], [])
        self.assertNotIn("stage", out)
        self.assertEqual(out["prior_work_meta"]["outcome"], "timeout")


class KeywordOnlyResultGetsTheSameSteps(unittest.TestCase):
    def test_the_relevance_gate_applies_to_the_keyword_only_result(self) -> None:
        unrelated = _lexical_row("9", "spreadsheet macro freeze pane")
        with _lane(lexical=[unrelated], embed=_slow(2.0, [1.0])), \
                mock.patch.dict("os.environ", {"KHIPU_FEATURE_RELEVANCE_FLOOR": "1"}):
            progress: dict = {}
            out = rp._search_hits(PROMPT, cwd=None, progress=progress)
        self.assertEqual(progress["provisional"]["hits"], [])
        self.assertEqual(out["hits"], [])
        self.assertEqual(out["degraded"], "embedding late")

    def test_the_gate_keeps_a_row_the_keywords_support(self) -> None:
        with _lane(embed=_slow(2.0, [1.0])), \
                mock.patch.dict("os.environ", {"KHIPU_FEATURE_RELEVANCE_FLOOR": "1"}):
            progress: dict = {}
            rp._search_hits(PROMPT, cwd=None, progress=progress)
        self.assertEqual([h["id"] for h in progress["provisional"]["hits"]], ["5"])

    def test_validity_is_annotated_on_the_keyword_only_result(self) -> None:
        with _lane(embed=_slow(2.0, [1.0]), counts={"5": (0, 1, 0)}):
            progress: dict = {}
            out = rp._search_hits(PROMPT, cwd=None, progress=progress)
        published = progress["provisional"]["hits"][0]
        self.assertEqual(published["validity"]["state"], "superseded")
        self.assertEqual(out["hits"][0]["validity"], published["validity"])

    def test_the_score_floor_and_the_limit_apply_when_the_limit_passes(self) -> None:
        rows = [dict(_lexical_row(str(i)), score=score) for i, score in enumerate([1.0, 0.9, 0.1])]
        with _lane(lexical=rows, metadata=_second_call_slow()):
            out = rp.prior_work_for_prompt(PROMPT, limit=2)
        ids = [h["id"] for h in out["hits"]]
        self.assertLessEqual(len(ids), 2)
        self.assertNotIn("2", ids)


class LogLineTest(unittest.TestCase):
    @staticmethod
    def _summary_line(log: Path, before: int) -> str:
        text = log.read_text(encoding="utf-8")[before:]
        summary = [ln for ln in text.splitlines() if " reason=" in ln]
        assert len(summary) == 1, text
        return summary[0]

    def _run_hook(self, log: Path) -> str:
        before = len(log.read_text(encoding="utf-8")) if log.exists() else 0
        rp.hook_main(json.dumps({"prompt": PROMPT, "session_id": "s-log"}))
        return self._summary_line(log, before)

    def test_ok_and_timeout_lines_still_parse_and_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "prompt-recall.log"
            with mock.patch.object(rp, "_log_path", return_value=log), \
                    mock.patch.object(rp, "_load_recent_batches", return_value=[]), \
                    mock.patch.object(rp, "_save_recent_batches"):
                with _lane(metadata=_second_call_slow()):
                    limit_line = self._run_hook(log)
                with _lane(keyword=_slow(2.0, [_lexical_row()])):
                    timeout_line = self._run_hook(log)
                with _lane():
                    ok_line = self._run_hook(log)
                lines = {"limit": limit_line, "timeout": timeout_line, "ok": ok_line}
                for line in lines.values():
                    self.assertIsNotNone(rp._LOG_LINE_RE.match(line), line)
                self.assertIn("degraded=embedding late (limit)", lines["limit"])
                self.assertNotIn("stage=", lines["limit"])
                self.assertTrue(lines["timeout"].endswith(" stage=keyword"), lines["timeout"])
                self.assertNotIn("stage=", lines["ok"])
                classified = {
                    name: rp._classify_call_reason(rp._LOG_LINE_RE.match(line).group("reason"))
                    for name, line in lines.items()
                }
                self.assertEqual(classified, {"limit": "ok", "timeout": "timeout", "ok": "ok"})

                # The doctor's own reader counts the timeouts among the ok calls.
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                body = "\n".join(
                    f"{stamp} [khipu-prompt-recall] " + lines[name].split("] ", 1)[1]
                    for name in ["ok", "timeout"] * 12
                )
                log.write_text(body + "\n", encoding="utf-8")
                out = rp.prompt_recall_outcomes()
            self.assertEqual(out["count"], 24)
            self.assertEqual(out["timeout_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
