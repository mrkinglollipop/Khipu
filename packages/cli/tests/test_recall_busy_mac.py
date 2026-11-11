"""Per-prompt recall on a busy Mac (2026-11-09): the warm service's client
waits longer when the load average is at or over the core count, the search
uses that time (and only then), and the service reloads what a replica write
made stale between prompts instead of during the next one.

Everything is faked as in tests/test_prompt_lane_provisional.py: no replica,
no embedding call, and the limits are patched down so the sleeps stay short.
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from khipu import integrations, recall_daemon
from khipu import recall_prompt as rp
from tests.test_prompt_lane_provisional import _lane, _slow, PROMPT, LIMIT_S, DEADLINE_S

CLIENT = Path(__file__).resolve().parents[1] / "bin" / "khipu-recall-client.py"
# The patched lane: TIMEOUT_S 0.4 and LOCAL_LANE_DEADLINE_S 0.25 (from the
# provisional tests), and a busy limit 0.5 s past the usual one.
BUSY_S = LIMIT_S + 0.5


def _client():
    spec = importlib.util.spec_from_file_location("khipu_recall_client_busy", CLIENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _busy_lane(**kw):
    with _lane(**kw), mock.patch.object(rp, "BUSY_TIMEOUT_S", BUSY_S):
        yield


def _client_deadline(busy: bool) -> float:
    """What the client sends: its own start plus its answer timeout, which is
    the lane's limit plus ANSWER_MARGIN_S."""
    return time.monotonic() + (BUSY_S if busy else LIMIT_S) + rp.ANSWER_MARGIN_S


class ClientAnswerTimeoutTest(unittest.TestCase):
    def test_an_idle_mac_waits_the_usual_time(self) -> None:
        client = _client()
        with mock.patch.object(client.os, "getloadavg", return_value=(17.9, 0, 0)), \
                mock.patch.object(client.os, "cpu_count", return_value=18):
            self.assertEqual(client.answer_timeout(), client.ANSWER_TIMEOUT_S)

    def test_a_load_at_the_core_count_waits_longer(self) -> None:
        client = _client()
        for load in (18.0, 60.0, 400.0):
            with self.subTest(load=load), mock.patch.object(client.os, "getloadavg", return_value=(load, 0, 0)), \
                    mock.patch.object(client.os, "cpu_count", return_value=18):
                self.assertEqual(client.answer_timeout(), client.BUSY_ANSWER_TIMEOUT_S)

    def test_an_unreadable_load_waits_the_usual_time(self) -> None:
        client = _client()
        with mock.patch.object(client.os, "getloadavg", side_effect=OSError):
            self.assertEqual(client.answer_timeout(), client.ANSWER_TIMEOUT_S)

    def test_the_budgets_line_up_and_leave_room_under_the_harness(self) -> None:
        client = _client()
        self.assertAlmostEqual(client.ANSWER_TIMEOUT_S - rp.ANSWER_MARGIN_S, rp.TIMEOUT_S)
        self.assertAlmostEqual(client.BUSY_ANSWER_TIMEOUT_S - rp.ANSWER_MARGIN_S, rp.BUSY_TIMEOUT_S)
        # The harness stops the hook at 3 s; 0.4 s at least stays for the
        # shell, the interpreter's start and the output.
        self.assertLessEqual(client.BUSY_ANSWER_TIMEOUT_S + 0.4, integrations.PROMPT_RECALL_TIMEOUT)

    def test_the_promotion_gate_still_measures_the_one_shot_limit(self) -> None:
        from khipu import promote

        self.assertEqual(promote.RECALL_BUDGET_MS, rp.TIMEOUT_S * 1000)


class BusyDeadlineTest(unittest.TestCase):
    def test_a_usual_deadline_answers_keyword_only_as_before(self) -> None:
        with _busy_lane(embed=_slow(0.5, [1.0])):
            t0 = time.monotonic()
            out = rp.prior_work_for_prompt(PROMPT, deadline=_client_deadline(busy=False))
            elapsed = time.monotonic() - t0
        self.assertEqual(out["legs"], ["lexical"])
        self.assertEqual(out["degraded"], "embedding late")
        self.assertLess(elapsed, DEADLINE_S + 0.12)

    def test_a_busy_deadline_waits_for_the_meaning_based_leg(self) -> None:
        with _busy_lane(embed=_slow(0.5, [1.0])):
            out = rp.prior_work_for_prompt(PROMPT, deadline=_client_deadline(busy=True))
        self.assertIn("cosine", out["legs"])
        self.assertIsNone(out["degraded"])
        self.assertEqual({h["id"] for h in out["hits"]}, {"5", "t1"})

    def test_a_busy_deadline_never_makes_a_quick_search_wait(self) -> None:
        with _busy_lane():
            t0 = time.monotonic()
            out = rp.prior_work_for_prompt(PROMPT, deadline=_client_deadline(busy=True))
            elapsed = time.monotonic() - t0
        self.assertIn("cosine", out["legs"])
        self.assertLess(elapsed, DEADLINE_S)

    def test_the_one_shot_path_with_no_deadline_is_never_stretched(self) -> None:
        """The one-shot hook (and the promotion gate, which replays it) gives
        no deadline: its limits stay TIMEOUT_S and LOCAL_LANE_DEADLINE_S."""
        with _busy_lane(embed=_slow(0.5, [1.0])):
            t0 = time.monotonic()
            out = rp.prior_work_for_prompt(PROMPT)
            elapsed = time.monotonic() - t0
        self.assertEqual(out["legs"], ["lexical"])
        self.assertLess(elapsed, DEADLINE_S + 0.12)

    def test_the_stretch_is_capped_at_the_busy_limit(self) -> None:
        with _busy_lane(keyword=_slow(3.0, lambda *_a: [])):
            t0 = time.monotonic()
            out = rp.prior_work_for_prompt(PROMPT, deadline=time.monotonic() + 10.0)
            elapsed = time.monotonic() - t0
        self.assertEqual(out["reason"], f"timeout>{round(BUSY_S, 1)}s")
        self.assertLess(elapsed, BUSY_S + 0.15)
        self.assertGreaterEqual(elapsed, BUSY_S - 0.05)

    def test_a_usual_timeout_keeps_its_reason(self) -> None:
        with _busy_lane(keyword=_slow(3.0, lambda *_a: [])):
            out = rp.prior_work_for_prompt(PROMPT, deadline=_client_deadline(busy=False))
        self.assertEqual(out["reason"], f"timeout>{LIMIT_S}s")

    def test_a_budgeted_call_is_never_stretched(self) -> None:
        """khipu_status's budgeted lane names its own budget; a far deadline
        does not lengthen it."""
        seen: dict = {}

        def budgeted(*_a, **kw):
            seen.update(kw)
            return {"hits": [], "legs": [], "degraded": None}

        with mock.patch.object(rp, "_search_hits_budgeted", side_effect=budgeted):
            t0 = time.monotonic()
            rp.prior_work_for_prompt(PROMPT, budget_ms=300, deadline=t0 + 10.0)
        self.assertLess(seen["deadline"] - t0, 0.3 + rp._BUDGET_SAFETY_SLACK_S + 0.05)


class KeepCachesWarmTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kbm-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.replica = self.dir / "hub_snapshot.sqlite"
        self.replica.write_bytes(b"one")
        self.now = [100.0]
        self.svc = recall_daemon.RecallService(self.dir / "recall.sock", stamp_fn=lambda: "s",
                                               clock=lambda: self.now[0], routing_forkserver=False)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch("khipu.hub_snapshot.snapshot_path", return_value=self.replica))
        self.warm = stack.enter_context(mock.patch.object(recall_daemon, "_warm_caches"))

    def _write(self, data: bytes) -> None:
        self.replica.write_bytes(data)
        st = self.replica.stat()
        os.utime(self.replica, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def test_it_reloads_once_after_the_replica_is_still(self) -> None:
        self.assertFalse(self.svc.keep_caches_warm())  # first look
        self.now[0] += recall_daemon.REPLICA_QUIET_S / 2
        self.assertFalse(self.svc.keep_caches_warm())
        self.now[0] += recall_daemon.REPLICA_QUIET_S
        self.assertTrue(self.svc.keep_caches_warm())
        self.now[0] += 60
        self.assertFalse(self.svc.keep_caches_warm())  # the same file: already warm
        self.assertEqual(self.warm.call_count, 1)

    def test_a_write_waits_for_the_burst_to_end_then_reloads_again(self) -> None:
        self.svc.keep_caches_warm()
        self.now[0] += 5
        self.assertTrue(self.svc.keep_caches_warm())
        self._write(b"two two")
        self.now[0] += 5
        self.assertFalse(self.svc.keep_caches_warm())  # just moved
        self._write(b"three three three")
        self.now[0] += 0.5
        self.assertFalse(self.svc.keep_caches_warm())  # moved again inside the burst
        self.now[0] += recall_daemon.REPLICA_QUIET_S
        self.assertTrue(self.svc.keep_caches_warm())
        self.assertEqual(self.warm.call_count, 2)

    def test_never_while_a_prompt_is_being_answered(self) -> None:
        self.svc.keep_caches_warm()
        self.now[0] += 5
        self.svc._in_flight = 1
        self.assertFalse(self.svc.keep_caches_warm())
        self.svc._in_flight = 0
        self.assertTrue(self.svc.keep_caches_warm())

    def test_a_request_counts_as_in_flight_while_it_is_answered(self) -> None:
        during: list[int] = []

        def handler(*_a, **_k):
            during.append(self.svc._in_flight)
            return {}

        with mock.patch("khipu.recall_prompt.hook_main", side_effect=handler):
            self.svc._answer(b'{"op": "recall", "raw": "{}"}')
        self.assertEqual(during, [1])
        self.assertEqual(self.svc._in_flight, 0)

    def test_a_request_that_raises_still_leaves_the_count_at_zero(self) -> None:
        with mock.patch.object(self.svc, "dispatch", side_effect=RuntimeError("boom")), \
                mock.patch("khipu.recall_prompt.log_terminal_outcome"):
            self.svc._answer(b'{"op": "recall", "raw": "{}"}')
        self.assertEqual(self.svc._in_flight, 0)

    def test_no_replica_means_nothing_to_warm(self) -> None:
        self.replica.unlink()
        self.svc.keep_caches_warm()
        self.now[0] += 60
        self.assertFalse(self.svc.keep_caches_warm())
        self.warm.assert_not_called()

    def test_a_stopping_service_does_not_reload(self) -> None:
        self.svc.keep_caches_warm()
        self.now[0] += 5
        self.svc.stop()
        self.assertFalse(self.svc.keep_caches_warm())


class WarmCachesTest(unittest.TestCase):
    def test_it_also_counts_word_rarity(self) -> None:
        with mock.patch("khipu.hub_snapshot.active_snapshot_profile", return_value=None), \
                mock.patch("khipu.hub_snapshot.term_document_frequency") as counts:
            recall_daemon._warm_caches()
        counts.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
