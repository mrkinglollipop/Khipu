# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Phase 0 session C (finding B10, docs/research/hindsight-plan-review-
2026-09-28.md): the deadline-aware local recall lane, its sqlite
query-embedding cache, and the doctor check on its real outcome rate.

``test_recall_prompt.py`` covers the gate/render/dedup/timeout plumbing this
phase did not change; this file covers what it DID change: the lane no
longer waits unboundedly for the embedding leg, the query-vector cache is a
keyed sqlite table instead of a whole-file JSON blob, and `khipu doctor` now
has real evidence of the lane's own outcome rate instead of only replica
freshness.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from khipu import recall_prompt as rp


def _fresh_lexical_row() -> dict:
    return {"kind": "episode", "id": "5", "label": "ep", "snippet": "episode text"}


def _fresh_cosine_row() -> dict:
    return {
        "kind": "topic", "id": "t1", "chunk_idx": 0, "score": 0.9,
        "label": "T1", "snippet": "topic body", "rank_text": "topic body",
    }


def _reference_snapshot_search_hits(prompt: str, *, project: str | None) -> list:
    """Verbatim copy of ``khipu.recall_prompt._snapshot_search_hits`` as it
    existed before Phase 0 session C (finding B10) — sequential lexical then
    cosine, no deadline, returning a plain list of fused rows. Reference for
    the "embedding on time" case: the new deadline-aware, threaded version
    must fuse to the exact same rows when the cosine leg lands before the
    deadline."""
    from khipu import hub_snapshot
    from khipu.recency import apply_project_and_status
    from khipu.search_text import fuse_ranked_lists, search_tokens, token_hit_count

    fresh, health = hub_snapshot.snapshot_is_fresh()
    if not fresh:
        raise rp._SnapshotUnusable(
            "missing" if not health.get("exists") else f"stale ({health.get('age_seconds')}s)"
        )
    tokens = search_tokens(prompt)
    lexical_rows = hub_snapshot.search_snapshot(prompt, rp._SEARCH_LIMIT, kind=None)
    for r in lexical_rows:
        r["rank_text"] = f"{r.get('label') or ''} {r.get('snippet') or ''}"
    if tokens:
        lexical_rows.sort(key=lambda r: -token_hit_count(r.get("rank_text") or "", tokens))
    lists = [lexical_rows] if lexical_rows else []
    cosine_rows: list = []
    profile = hub_snapshot.active_snapshot_profile()
    if profile:
        try:
            vec = rp._cached_query_embed(prompt, profile)
            cosine_rows = hub_snapshot.cosine_candidates_snapshot(
                vec, profile, limit=rp._SEARCH_LIMIT
            )
        except Exception:  # noqa: BLE001
            cosine_rows = []
    if cosine_rows:
        for r in cosine_rows:
            r["cosine"] = r.get("score")
        lists.insert(0, list(cosine_rows))
        if tokens:
            union = {(r["kind"], str(r["id"])): r for r in cosine_rows}
            for r in lexical_rows:
                union.setdefault((r["kind"], str(r["id"])), r)
            lex_rows = sorted(
                union.values(), key=lambda r: -token_hit_count(r.get("rank_text") or "", tokens)
            )
            lists.append(lex_rows)
    _scored_rows: set = set()
    for row_list in lists:
        for r in row_list:
            if id(r) in _scored_rows:
                continue
            _scored_rows.add(id(r))
            if tokens:
                r["lexical_hits"] = token_hit_count(r.get("rank_text") or "", tokens)
            r.pop("rank_text", None)
    if not lists:
        return []
    fused = fuse_ranked_lists(lists, limit=rp._SEARCH_LIMIT)
    con = hub_snapshot.open_snapshot()
    fused = hub_snapshot.snapshot_row_metadata(con, fused)
    fused = apply_project_and_status(fused, project=project)
    return fused


class EmbeddingOnTimeTest(unittest.TestCase):
    """When the embedding leg lands well before LOCAL_LANE_DEADLINE_S, the
    new threaded/deadline-aware fusion must match the old sequential one
    exactly — the deadline machinery must not change results, only timing
    and failure behavior."""

    def test_matches_the_pre_change_sequential_fusion(self) -> None:
        with mock.patch("khipu.hub_snapshot.snapshot_is_fresh", return_value=(True, {"exists": True})), \
                mock.patch("khipu.hub_snapshot.search_snapshot",
                            side_effect=lambda *a, **k: [_fresh_lexical_row()]), \
                mock.patch("khipu.hub_snapshot.active_snapshot_profile", return_value="p1"), \
                mock.patch.object(rp, "_cached_query_embed", return_value=[1.0]), \
                mock.patch("khipu.hub_snapshot.cosine_candidates_snapshot",
                            side_effect=lambda *a, **k: [_fresh_cosine_row()]), \
                mock.patch("khipu.hub_snapshot.open_snapshot", return_value=object()), \
                mock.patch("khipu.hub_snapshot.snapshot_row_metadata", side_effect=lambda con, rows: rows):
            new_out = rp._snapshot_search_hits("a topical prompt", project=None)
            old_out = _reference_snapshot_search_hits("a topical prompt", project=None)
        # Phase 2, session B: `open_snapshot` here is a plain `object()` (no
        # real sqlite connection), so validity annotation degrades to
        # "unknown" and adds a `validity` key to every row — additive, but it
        # breaks a strict dict comparison against the pre-phase reference,
        # which never adds it. Strip it before comparing; the deadline
        # machinery under test is unaffected either way.
        new_hits = [{k: v for k, v in r.items() if k != "validity"} for r in new_out["hits"]]
        self.assertEqual(new_hits, old_out)
        self.assertIsNone(new_out["degraded"])
        self.assertEqual(set(new_out["legs"]), {"lexical", "cosine"})


class EmbeddingLateTest(unittest.TestCase):
    """finding B10: the embedding leg missing its deadline must never again
    throw away a keyword match that was ready long before it."""

    def test_keyword_only_rows_come_back_degraded_within_timeout_s(self) -> None:
        def _slow_embed(*_a, **_k):
            time.sleep(5.0)
            return [1.0]

        with mock.patch("khipu.hub_snapshot.snapshot_is_fresh", return_value=(True, {"exists": True})), \
                mock.patch("khipu.hub_snapshot.search_snapshot",
                            return_value=[_fresh_lexical_row()]), \
                mock.patch("khipu.hub_snapshot.active_snapshot_profile", return_value="p1"), \
                mock.patch.object(rp, "_cached_query_embed", side_effect=_slow_embed), \
                mock.patch("khipu.hub_snapshot.open_snapshot", return_value=object()), \
                mock.patch("khipu.hub_snapshot.snapshot_row_metadata", side_effect=lambda con, rows: rows):
            t0 = time.monotonic()
            out = rp._search_hits("a topical prompt", cwd=None)
            elapsed = time.monotonic() - t0
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])
        self.assertEqual(out["degraded"], "embedding late")
        # The whole point of the deadline: never wait for the 5s sleep.
        self.assertLess(elapsed, rp.TIMEOUT_S)


class DeadlineCountsFromTheCallersStartTest(unittest.TestCase):
    """The embedding wait and TIMEOUT_S must share one clock. When the wait
    started after the project lookup, a slow lookup plus a late embedding
    overran TIMEOUT_S and the keyword rows were thrown away with it."""

    def test_a_slow_project_lookup_shortens_the_embedding_wait(self) -> None:
        def _slow_embed(*_a, **_k):
            time.sleep(5.0)
            return [1.0]

        def _slow_project(_cwd):
            time.sleep(0.3)
            return None

        with mock.patch("khipu.hub_snapshot.snapshot_is_fresh", return_value=(True, {"exists": True})), \
                mock.patch("khipu.hub_snapshot.search_snapshot",
                            return_value=[_fresh_lexical_row()]), \
                mock.patch("khipu.hub_snapshot.active_snapshot_profile", return_value="p1"), \
                mock.patch.object(rp, "_project_for_cwd", side_effect=_slow_project), \
                mock.patch.object(rp, "_cached_query_embed", side_effect=_slow_embed), \
                mock.patch("khipu.hub_snapshot.open_snapshot", return_value=object()), \
                mock.patch("khipu.hub_snapshot.snapshot_row_metadata", side_effect=lambda con, rows: rows):
            t0 = time.monotonic()
            out = rp._search_hits("a topical prompt", cwd="/repo")
            elapsed = time.monotonic() - t0
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])
        self.assertEqual(out["degraded"], "embedding late")
        self.assertLess(elapsed, rp.LOCAL_LANE_DEADLINE_S + 0.15)


class DeliverableLineIsBoundedTest(unittest.TestCase):
    def test_a_slow_hub_costs_the_line_not_the_prompt(self) -> None:
        def _slow_line(*_a, **_k):
            time.sleep(5.0)
            return "You produced x.py on 2026-09-14 (episode 42)"

        hits = {"hits": [_fresh_lexical_row()], "legs": ["lexical"], "degraded": None}
        with mock.patch.object(rp, "_search_hits", return_value=hits), \
                mock.patch.object(rp, "_deliverable_context_line", side_effect=_slow_line), \
                mock.patch.object(rp, "_log"):
            t0 = time.monotonic()
            out = rp.prior_work_for_prompt("what did we build for decisions", cwd="/repo")
            elapsed = time.monotonic() - t0
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])
        self.assertNotIn("You produced", out["context"])
        self.assertLess(elapsed, rp.DELIVERABLE_LINE_TIMEOUT_S + 0.2)


class EmbeddingErrorTest(unittest.TestCase):
    def test_keyword_only_rows_come_back_degraded_embedding_error(self) -> None:
        with mock.patch("khipu.hub_snapshot.snapshot_is_fresh", return_value=(True, {"exists": True})), \
                mock.patch("khipu.hub_snapshot.search_snapshot",
                            return_value=[_fresh_lexical_row()]), \
                mock.patch("khipu.hub_snapshot.active_snapshot_profile", return_value="p1"), \
                mock.patch.object(rp, "_cached_query_embed", side_effect=RuntimeError("embed API down")), \
                mock.patch("khipu.hub_snapshot.open_snapshot", return_value=object()), \
                mock.patch("khipu.hub_snapshot.snapshot_row_metadata", side_effect=lambda con, rows: rows):
            out = rp._search_hits("a topical prompt", cwd=None)
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])
        self.assertEqual(out["degraded"], "embedding error")


class QueryEmbedSqliteCacheTest(unittest.TestCase):
    """Phase 0 session C: the query-vector cache is now a keyed sqlite
    table (was a whole-file JSON blob parsed on every prompt)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="khipu-qembed-sqlite-"))
        self.path = self.tmp / "cache.sqlite3"
        self._patch = mock.patch.object(rp, "_query_embed_cache_path", return_value=self.path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_round_trip_is_bit_identical(self) -> None:
        vec = [0.1, -2.5, 3.333333333333333, 1e-300, -1e300, 0.0, 123456789.987654321]
        rp._query_embed_cache_put("k1", vec)
        got = rp._query_embed_cache_get("k1")
        # Exact equality, not almost-equal: float64 round trip through the
        # BLOB column must be bit-identical, no lossy text step anywhere.
        self.assertEqual(got, vec)

    def test_miss_returns_none(self) -> None:
        self.assertIsNone(rp._query_embed_cache_get("nope"))

    def test_pruning_keeps_at_most_the_max_and_drops_oldest(self) -> None:
        with mock.patch.object(rp, "QUERY_EMBED_CACHE_MAX", 3):
            for i in range(5):
                with mock.patch("time.time", return_value=float(i)):
                    rp._query_embed_cache_put(f"k{i}", [float(i)])
        con = sqlite3.connect(str(self.path))
        rows = con.execute("SELECT key FROM query_embed_cache ORDER BY created").fetchall()
        con.close()
        self.assertEqual([r[0] for r in rows], ["k2", "k3", "k4"])

    def test_corrupt_cache_file_degrades_to_a_miss_without_raising(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"not a sqlite file at all, just garbage bytes")
        self.assertIsNone(rp._query_embed_cache_get("k1"))
        rp._query_embed_cache_put("k1", [1.0])  # must not raise either

    def test_locked_cache_file_degrades_to_a_miss_without_raising(self) -> None:
        # Simulates "another hook process holds the write lock past our
        # connect timeout" — the same OperationalError a real lock timeout
        # produces.
        with mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertIsNone(rp._query_embed_cache_get("k1"))
            rp._query_embed_cache_put("k1", [1.0])  # must not raise

    def test_cached_query_embed_end_to_end_hits_the_new_cache(self) -> None:
        with mock.patch("khipu.embed.embed_one", return_value=[9.0, 8.0]) as m:
            first = rp._cached_query_embed("persisted prompt", "p1")
        with mock.patch("khipu.embed.embed_one") as m2:
            second = rp._cached_query_embed("persisted prompt", "p1")
        self.assertEqual(first, [9.0, 8.0])
        self.assertEqual(second, [9.0, 8.0])
        m.assert_called_once()
        m2.assert_not_called()


def _log_line(ts: str, reason: str, ms: float, *, legs: str = "[]", degraded: str = "None") -> str:
    return (
        f"{ts} [khipu-prompt-recall] session=s reason={reason} ms={ms} "
        f"hits=[] legs={legs} degraded={degraded}"
    )


class DoctorOutcomesTest(unittest.TestCase):
    """``recall_prompt.prompt_recall_outcomes`` — the doctor check reading
    the hook's own log for finding B10's real outcome rate."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="khipu-outcomes-"))
        self.log_path = self.tmp / "prompt-recall.log"
        self._patch = mock.patch.object(rp, "_log_path", return_value=self.log_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def _write(self, lines: list[str]) -> None:
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_healthy_log(self) -> None:
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        lines = []
        for i in range(25):
            ts = (now - timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            reason = "timeout>1.2s" if i % 10 == 0 else "ok"  # ~12% timeouts
            lines.append(_log_line(ts, reason, 800.0 + i))
        self._write(lines)
        out = rp.prompt_recall_outcomes(now=now)
        self.assertTrue(out["ok"])
        self.assertFalse(out["insufficient"])
        self.assertEqual(out["count"], 25)
        self.assertLess(out["timeout_rate"], 0.5)

    def test_unhealthy_log(self) -> None:
        """The production shape finding B10 measured: most calls time out."""
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        lines = []
        for i in range(25):
            ts = (now - timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            reason = "ok" if i % 10 == 0 else "timeout>1.2s"  # ~88% timeouts
            lines.append(_log_line(ts, reason, 1200.0))
        self._write(lines)
        out = rp.prompt_recall_outcomes(now=now)
        self.assertFalse(out["ok"])
        self.assertFalse(out["insufficient"])
        self.assertGreaterEqual(out["timeout_rate"], 0.5)

    def test_short_log_is_insufficient(self) -> None:
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [_log_line(ts, "ok", 500.0) for _ in range(5)]
        self._write(lines)
        out = rp.prompt_recall_outcomes(now=now)
        self.assertTrue(out["ok"])
        self.assertTrue(out["insufficient"])
        self.assertEqual(out["count"], 5)

    def test_missing_log_is_insufficient(self) -> None:
        out = rp.prompt_recall_outcomes()
        self.assertTrue(out["ok"])
        self.assertTrue(out["insufficient"])
        self.assertEqual(out["count"], 0)

    def test_garbage_lines_are_skipped_not_raised(self) -> None:
        lines = [
            "not a log line at all",
            "2026-09-28T00:00:00Z [khipu-prompt-recall] snapshot unusable "
            "(missing) — falling back to hub",
            "",
            "\tweird whitespace line ??",
            "2026-09-28T00:00:00Z [khipu-prompt-recall] hook_main crashed: RuntimeError: boom",
        ]
        self._write(lines)
        out = rp.prompt_recall_outcomes()
        self.assertTrue(out["ok"])
        self.assertTrue(out["insufficient"])
        self.assertEqual(out["count"], 0)

    def test_calls_outside_the_7_day_window_are_excluded(self) -> None:
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        old = now - timedelta(days=10)
        ts = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [_log_line(ts, "ok", 500.0) for _ in range(25)]
        self._write(lines)
        out = rp.prompt_recall_outcomes(now=now)
        self.assertEqual(out["count"], 0)
        self.assertTrue(out["insufficient"])

    def test_gated_reasons_do_not_count_as_searched_calls(self) -> None:
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            _log_line(ts, "no content tokens", 0.0),
            _log_line(ts, "trivial acknowledgment", 0.0),
            _log_line(ts, "gate error: boom", 0.0),
        ] * 10
        self._write(lines)
        out = rp.prompt_recall_outcomes(now=now)
        self.assertEqual(out["count"], 0)
        self.assertTrue(out["insufficient"])

    def test_median_ms_even_and_odd_counts(self) -> None:
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        # 21 calls (odd), ms values 0..20 sorted already -> median is the
        # 11th value (index 10) = 10.0.
        lines = [_log_line(ts, "ok", float(i)) for i in range(21)]
        self._write(lines)
        out = rp.prompt_recall_outcomes(now=now)
        self.assertEqual(out["count"], 21)
        self.assertEqual(out["median_ms"], 10.0)

    def test_only_the_most_recent_calls_are_judged(self) -> None:
        """A long run of old timeouts followed by a run of recent successes
        reads healthy: the check reports what the lane does now."""
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        lines = []
        for i in range(300):  # oldest first, all inside the window
            ts = (now - timedelta(minutes=5000 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(_log_line(ts, "timeout>1.2s", 1200.0))
        for i in range(rp._OUTCOMES_MAX_CALLS):
            ts = (now - timedelta(minutes=200 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(_log_line(ts, "ok", 500.0))
        self._write(lines)
        out = rp.prompt_recall_outcomes(now=now)
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], rp._OUTCOMES_MAX_CALLS)
        self.assertEqual(out["timeout_rate"], 0.0)

    def test_a_recent_breakage_is_red_despite_older_successes(self) -> None:
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        lines = []
        for i in range(300):
            ts = (now - timedelta(minutes=5000 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(_log_line(ts, "ok", 500.0))
        for i in range(rp._OUTCOMES_MAX_CALLS):
            ts = (now - timedelta(minutes=200 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(_log_line(ts, "timeout>1.2s", 1200.0))
        self._write(lines)
        out = rp.prompt_recall_outcomes(now=now)
        self.assertFalse(out["ok"])
        self.assertEqual(out["timeout_rate"], 1.0)
        self.assertIn("deadline", out["reason"])
        self.assertIn("khipu snapshot refresh", out["fix"])

    def test_doctor_never_raises_on_an_unreadable_directory(self) -> None:
        # _log_path pointed at a path whose parent does not exist at all —
        # is_file() is False, so this degrades the same as "missing".
        missing = self.tmp / "nope" / "prompt-recall.log"
        with mock.patch.object(rp, "_log_path", return_value=missing):
            out = rp.prompt_recall_outcomes()
        self.assertTrue(out["ok"])
        self.assertTrue(out["insufficient"])


if __name__ == "__main__":
    unittest.main()
