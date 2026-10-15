"""Gateway provenance, phase 1: a queued capture keeps the origin it was
stamped with when it was queued.

The outbox job envelope carries the server-assigned origin next to the
payload, never inside it, and every replay path (``khipu outbox drain``, a
drain inside the gateway process, the scheduled session drain, the Stop hook)
writes the row with that origin instead of recomputing it from the drainer's
own context. Hermetic: fake cursor, fake machine id, a temporary outbox.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from unittest import mock

from khipu import capture as cap
from khipu import db as khipu_db
from khipu import outbox
from khipu import request_context as rc
from tests.test_capture import _FakeConn
from tests.test_gateway_provenance import _OriginFakeCursor, _ReplayCursor

QUEUER = "local:aaaaaaaaaaaa"
DRAINER = "local:bbbbbbbbbbbb"


def _payload(summary: str = "gateway outbox provenance") -> dict:
    return {"ts": "2026-10-15T12:00:00Z", "summary": summary,
            "session_id": "grokbot:review", "t3_thread_id": "already-resolved"}


class _OutboxOriginBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = {k: v for k, v in os.environ.items() if k != rc.GATEWAY_ACTIVE_ENV}
        env.update(KHIPU_OUTBOX=os.path.join(self.tmp, "outbox"), KHIPU_HUB_FILE_MIRROR="0")
        p = mock.patch.dict("os.environ", env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        khipu_db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(khipu_db._TABLE_COLUMNS_CACHE.clear)
        outbox.outbox_dir().mkdir(parents=True)

    def _queue(self, payload=None, *, label: str | None = "caller-a", gateway_process=False,
               origin=QUEUER):
        """Run capture() with the database down so the payload is queued."""
        env = {rc.GATEWAY_ACTIVE_ENV: "1"} if gateway_process else {}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(cap, "write_pg", side_effect=ConnectionError("down")), \
                mock.patch("khipu.machine_identity.local_origin", return_value=origin):
            if label:
                with rc.gateway_request(label):
                    rc_ = cap.capture(payload or _payload(), mode="hub")
            else:
                rc_ = cap.capture(payload or _payload(), mode="hub")
        self.assertEqual(rc_, cap.EX_SOFTWARE)
        jobs = outbox.jobs()
        self.assertEqual(len(jobs), 1)
        return outbox._read_job(jobs[0])

    def _replay(self, run, *, gateway_process=False, cur=None):
        """Run one replay path against a fake hub; return the INSERTs it made."""
        cur = cur or _OriginFakeCursor()
        env = {rc.GATEWAY_ACTIVE_ENV: "1"} if gateway_process else {}
        with mock.patch.dict("os.environ", env), \
                mock.patch("psycopg.connect", side_effect=AssertionError("real database forbidden")), \
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.machine_identity.local_origin", return_value=DRAINER), \
                mock.patch("khipu.embed.embed_on_capture", return_value={}), \
                mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}), \
                mock.patch("khipu.config.path_setting", return_value=None), \
                mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key")), \
                mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            run()
        self.assertEqual(outbox.jobs(), [], "the job must replay and leave the queue")
        return cur.inserts


def _cli_drain():
    from khipu.cli import cmd_outbox

    with mock.patch("builtins.print"):
        assert cmd_outbox(argparse.Namespace(outbox_cmd="drain", limit=None)) == 0


def _scheduled_drain():
    from khipu import session_capture as sc

    with mock.patch.object(sc, "sweep_idle", return_value={}), \
            mock.patch.object(sc, "_reclaim_stale", return_value=0), \
            mock.patch.object(sc, "queued_jobs", return_value=[]):
        out = sc.drain(sweep=True)
    assert out["outbox"]["replayed"] == 1, out


def _stop_hook_drain():
    from khipu import hook_background as hb

    with mock.patch.object(hb, "_drain"), \
            mock.patch("khipu.mirror.sync_recent_episodes", return_value={}), \
            mock.patch("khipu.notes.reconcile", return_value={}), \
            mock.patch.object(hb, "_say"):
        hb.run("", "claude_code", "s1")


class EnqueueRecordsOriginTest(_OutboxOriginBase):
    def test_a_gateway_capture_queues_its_label_beside_the_payload(self):
        job = self._queue()
        self.assertEqual(job["origin"], "gateway:caller-a")
        self.assertNotIn("origin", job["payload"])

    def test_a_payload_origin_never_becomes_the_queued_origin(self):
        job = self._queue({**_payload(), "origin": "gateway:someone-else"}, label=None)
        self.assertEqual(job["origin"], QUEUER)
        self.assertNotIn("origin", job["payload"])

    def test_a_gateway_process_capture_outside_a_request_queues_unknown(self):
        job = self._queue(label=None, gateway_process=True)
        self.assertIn("origin", job)
        self.assertIsNone(job["origin"])

    def test_unreadable_identity_queues_no_origin_and_still_queues(self):
        from khipu.machine_identity import MachineIdentityError

        with mock.patch("khipu.request_context.request_origin",
                        side_effect=MachineIdentityError("no id")):
            job = self._queue(label=None)
        self.assertNotIn("origin", job)

    def test_the_same_payload_from_two_callers_queues_twice(self):
        with rc.gateway_request("caller-a"):
            first = outbox.enqueue(_payload())
        with rc.gateway_request("caller-b"):
            second = outbox.enqueue(_payload())
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(outbox._read_job(p)["origin"] for p in (first, second)),
                         ["gateway:caller-a", "gateway:caller-b"])

    def test_a_failed_mirror_write_queues_with_its_caller_origin(self):
        from khipu import mirror

        with mock.patch.object(mirror, "mirror_episode", side_effect=ConnectionError("down")):
            with rc.gateway_request("caller-a"):
                mirror.mirror_after_capture(_payload())
        job = outbox._read_job(outbox.jobs()[0])
        self.assertEqual(job["origin"], "gateway:caller-a")


class ReplayRestoresOriginTest(_OutboxOriginBase):
    """One test per replay path; each drains a gateway caller's queued capture."""

    def _assert_replayed_as_caller(self, run, **kw):
        self._queue()
        inserts = self._replay(run, **kw)
        self.assertEqual([i.get("origin") for i in inserts], ["gateway:caller-a"])

    def test_cli_outbox_drain_keeps_the_gateway_caller(self):
        self._assert_replayed_as_caller(_cli_drain)

    def test_drain_inside_the_gateway_process_keeps_the_gateway_caller(self):
        self._assert_replayed_as_caller(outbox.drain, gateway_process=True)

    def test_scheduled_session_drain_keeps_the_gateway_caller(self):
        self._assert_replayed_as_caller(_scheduled_drain)

    def test_stop_hook_drain_keeps_the_gateway_caller(self):
        self._assert_replayed_as_caller(_stop_hook_drain)

    def test_a_local_capture_replays_as_the_queuing_machine(self):
        self._queue(label=None)
        inserts = self._replay(_cli_drain)
        self.assertEqual([i.get("origin") for i in inserts], [QUEUER])

    def test_an_unknown_origin_stays_unknown_when_a_local_drain_replays_it(self):
        self._queue(label=None, gateway_process=True)
        inserts = self._replay(_cli_drain)
        self.assertEqual([i.get("origin") for i in inserts], [None])

    def test_a_payload_origin_in_the_job_file_is_still_dropped(self):
        self._queue()
        jp = outbox.jobs()[0]
        job = json.loads(jp.read_text())
        job["payload"]["origin"] = "gateway:spoof"
        jp.write_text(json.dumps(job))
        inserts = self._replay(_cli_drain)
        self.assertEqual([i.get("origin") for i in inserts], ["gateway:caller-a"])

    def test_a_job_queued_before_this_change_takes_the_drainers_origin(self):
        outbox._atomic_write(outbox.outbox_dir() / "legacy.json",
                             {"payload": _payload(), "reason": "x", "queued_at": "2026-10-01T00:00:00+00:00",
                              "attempts": 0})
        inserts = self._replay(_cli_drain)
        self.assertEqual([i.get("origin") for i in inserts], [DRAINER])

    def test_a_malformed_origin_quarantines_the_job(self):
        outbox._atomic_write(outbox.outbox_dir() / "bad.json",
                             {"payload": _payload(), "origin": "root", "reason": "x",
                              "queued_at": "2026-10-01T00:00:00+00:00", "attempts": 0})
        with mock.patch("khipu.capture.write_pg", side_effect=AssertionError("must not replay")):
            out = outbox.drain()
        self.assertEqual(out.get("quarantined"), 1)

    def test_a_queued_gateway_capture_claims_the_row_a_file_sync_wrote_first(self):
        # capture() also writes the file line, and the Stop hook's file sync
        # runs before its outbox drain, so the row can already exist unowned.
        self._queue({"ts": _ReplayCursor.TS, "session_id": "s1", "summary": _ReplayCursor.SUMMARY})
        cur = _ReplayCursor()
        self._replay(_cli_drain, cur=cur)
        self.assertEqual([params for _, params in cur.claims], [("gateway:caller-a", 1)])
        self.assertIn("origin IS NULL", cur.claims[0][0])

    def test_the_replay_origin_does_not_outlive_the_drain(self):
        self._queue()
        self._replay(_cli_drain)
        with mock.patch("khipu.machine_identity.local_origin", return_value=DRAINER):
            self.assertEqual(rc.request_origin(), DRAINER)


if __name__ == "__main__":
    unittest.main()
