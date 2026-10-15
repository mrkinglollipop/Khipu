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
import contextlib
import hashlib
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
        self.assertIn("origin", job)
        self.assertIsNone(job["origin"], "unknown is recorded, never left for the drainer to fill")

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

    def test_an_unreadable_identity_at_queue_time_stays_unknown_on_another_drainer(self):
        from khipu.machine_identity import MachineIdentityError

        with mock.patch("khipu.request_context.request_origin",
                        side_effect=MachineIdentityError("no id")):
            self._queue(label=None)
        inserts = self._replay(_cli_drain)
        self.assertEqual([i.get("origin") for i in inserts], [None])

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


class _DedupClaimCursor(_OriginFakeCursor):
    """The origin fake seeded with one unowned row a file sync wrote, applying
    origin claims under their NULL guard, with an optional window_id column."""

    def __init__(self, row: dict, *, window_col: bool = False):
        super().__init__()
        self.episodes[1] = {"ts": "2026-10-15T11:00:00Z", "summary": "written by a file sync",
                            "topics": [], "decisions": [], "preferences": [], "raw": {},
                            "tags": [], "origin": None, **row}
        self._existing_keys = {(e["ts"], hashlib.md5(e["summary"].encode()).hexdigest())
                               for e in self.episodes.values()}
        self.next_id = 2
        self.window_col = window_col
        self.claims: list[tuple] = []
        self.guarded = False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("UPDATE episodes SET origin"):
            self.guarded = "origin IS NULL" in s
            self.claims.append(params)
            origin, eid = params
            if self.guarded and self.episodes[eid].get("origin") is None:
                self.episodes[eid]["origin"] = origin
            return
        if s.startswith("SELECT id FROM episodes WHERE harness") and "window_id" in s:
            key = tuple(params)
            self._result = [(eid,) for eid, e in self.episodes.items()
                            if (e.get("harness"), e.get("session_id"), e.get("transcript_range"),
                                e.get("window_id"), (e.get("raw") or {}).get("part")) == key][:1]
            return
        super().execute(sql, params)
        if self.window_col and s.startswith("SELECT column_name FROM information_schema.columns") \
                and params == ("episodes",):
            self._result = list(self._result) + [("window_id",)]


_WINDOW = {"harness": "claude_code", "session_id": "claude_code:abc", "transcript_range": "0:100"}
_SIMILAR = "user experiencing slow performance from oracle processes in a worktree"
_MERGE_TEXT = {"_dedup_text": lambda p: p.get("summary") or ""}


def _merge_target():
    return _DedupClaimCursor({**_WINDOW, "summary": _SIMILAR, "project": "acme/widget"})


def _merge_payload():
    return {"ts": "2026-10-15T12:00:00Z", "summary": _SIMILAR, "harness": "claude_code",
            "session_id": "claude_code:other", "transcript_range": "50:200",
            "project": "acme/widget", "decisions": ["a new decision"]}


def _window_payload(**extra):
    return {"ts": "2026-10-15T12:00:00Z", "summary": "the same window, captured again",
            **_WINDOW, **extra}


class DedupEarlyReturnClaimsTest(_OutboxOriginBase):
    """write_pg returns early when a capture lands on an existing row (an
    exact-window or receipt skip, or a merge) and the outbox then deletes the
    job: on a skip, a trusted queued or local origin must claim that row first,
    under the NULL guard; a similarity merge never claims, and a live gateway
    caller never claims."""

    def _drain_onto(self, cur, payload, *, label="caller-a", patches=None):
        self._queue(payload, label=label)
        with mock.patch.multiple(cap, **patches) if patches else contextlib.nullcontext():
            self._replay(_cli_drain, cur=cur)
        self.assertEqual(cur.inserts, [], "the capture must land on the existing row")
        return cur.episodes[1].get("origin")

    def test_exact_window_skip_claims_for_the_queued_gateway_caller(self):
        cur = _DedupClaimCursor(dict(_WINDOW))
        self.assertEqual(self._drain_onto(cur, _window_payload()), "gateway:caller-a")
        self.assertTrue(cur.guarded)

    def test_exact_window_skip_claims_for_the_queuing_machine(self):
        cur = _DedupClaimCursor(dict(_WINDOW))
        self.assertEqual(self._drain_onto(cur, _window_payload(), label=None), QUEUER)

    def test_window_id_skip_claims_for_the_queued_owner(self):
        cur = _DedupClaimCursor({**_WINDOW, "window_id": "w1", "raw": {"part": "2"}},
                                window_col=True)
        payload = _window_payload(window_id="w1", part="2")
        self.assertEqual(self._drain_onto(cur, payload), "gateway:caller-a")

    def test_a_receipt_from_before_match_kinds_skips_without_claiming(self):
        from khipu.capture_identity import source_identity

        payload = {"ts": "2026-10-15T12:00:00Z", "summary": "folded in earlier",
                   "session_id": "grokbot:review"}
        job = self._queue(payload)
        receipt = source_identity(dict(job["payload"]))["payload_sha256"]
        cur = _DedupClaimCursor({"raw": {"merged_from": [{"source_id": receipt}]}})
        self._replay(_cli_drain, cur=cur)
        self.assertEqual(cur.inserts, [])
        self.assertEqual(cur.claims, [])
        self.assertIsNone(cur.episodes[1]["origin"])

    def test_a_similarity_merge_never_changes_the_target_origin(self):
        # Origin is used for authorization; a text resemblance is not identity.
        for name, owner in (("unowned", None), ("owned", "local:cccccccccccc")):
            with self.subTest(name):
                for job in outbox.jobs():
                    job.unlink()
                khipu_db._TABLE_COLUMNS_CACHE.clear()
                cur = _merge_target()
                cur.episodes[1]["origin"] = owner
                origin = self._drain_onto(cur, _merge_payload(), patches=_MERGE_TEXT)
                self.assertEqual(cur.updated_ids, [1], "the capture merged into the existing row")
                self.assertEqual(origin, owner)
                self.assertEqual(cur.claims, [])

    def test_an_owned_row_keeps_its_owner(self):
        cur = _DedupClaimCursor({**_WINDOW, "origin": "local:cccccccccccc"})
        self.assertEqual(self._drain_onto(cur, _window_payload()), "local:cccccccccccc")

    def test_an_unknown_queued_origin_claims_nothing(self):
        self._queue(_window_payload(), label=None, gateway_process=True)
        cur = _DedupClaimCursor(dict(_WINDOW))
        self._replay(_cli_drain, cur=cur)
        self.assertEqual(cur.claims, [])
        self.assertIsNone(cur.episodes[1]["origin"])

    def test_a_failed_required_claim_keeps_the_job_pending(self):
        # A lock timeout (or any error) on the claim of a trusted owner must not
        # let the outbox delete the only record of who queued the capture.
        class LockNotAvailable(Exception):
            pass

        for name, label, owner in (("gateway caller", "caller-a", "gateway:caller-a"),
                                   ("queuing machine", None, QUEUER)):
            with self.subTest(name):
                for job in outbox.jobs():
                    job.unlink()
                khipu_db._TABLE_COLUMNS_CACHE.clear()
                cur = _DedupClaimCursor(dict(_WINDOW))
                self._queue(_window_payload(), label=label)
                locked = mock.patch.object(cur, "execute", side_effect=lambda sql, params=None: (
                    (_ for _ in ()).throw(LockNotAvailable("lock timeout"))
                    if sql.lstrip().startswith("UPDATE episodes SET origin")
                    else _DedupClaimCursor.execute(cur, sql, params)))
                with locked, mock.patch.object(outbox, "MAX_ATTEMPTS", 1), \
                        mock.patch("psycopg.connect", side_effect=AssertionError("real database")), \
                        mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                        mock.patch("khipu.machine_identity.local_origin", return_value=DRAINER), \
                        mock.patch("khipu.config.path_setting", return_value=None):
                    first = outbox.drain()
                    second = outbox.drain()
                for out in (first, second):
                    self.assertEqual((out["replayed"], out["failed"], out.get("buried", 0)), (0, 1, 0))
                self.assertEqual(len(outbox.jobs()), 1, "the job stays pending, never buried")
                self.assertIsNone(cur.episodes[1]["origin"])
                self.assertIn("ROLLBACK TO SAVEPOINT capture_claim", [c for c, _ in cur.calls])
                # Once the lock clears, the retry claims for the queued owner.
                self._replay(_cli_drain, cur=cur)
                self.assertEqual(cur.episodes[1]["origin"], owner)

    def test_a_deferred_claim_never_starves_the_jobs_behind_it(self):
        class LockNotAvailable(Exception):
            pass

        cur = _DedupClaimCursor(dict(_WINDOW))
        self._queue(_window_payload())
        stuck = outbox.jobs()[0]
        outbox.enqueue({"ts": "2026-10-15T13:00:00Z", "summary": "a later capture",
                                           "session_id": "grokbot:later"})
        locked = mock.patch.object(cur, "execute", side_effect=lambda sql, params=None: (
            (_ for _ in ()).throw(LockNotAvailable("lock timeout"))
            if sql.lstrip().startswith("UPDATE episodes SET origin")
            else _DedupClaimCursor.execute(cur, sql, params)))
        with locked, mock.patch("psycopg.connect", side_effect=AssertionError("real database")), \
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.machine_identity.local_origin", return_value=DRAINER), \
                mock.patch("khipu.embed.embed_on_capture", return_value={}), \
                mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}), \
                mock.patch("khipu.config.path_setting", return_value=None), \
                mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            first = outbox.drain(limit=1)
            second = outbox.drain(limit=1)
        self.assertEqual(first.get("deferred"), 1)
        self.assertEqual(second["replayed"], 1, "the later job runs before the deferred one")
        self.assertEqual(outbox.jobs(), [stuck])

    def test_every_deferred_claim_is_reached_by_bounded_drains(self):
        # Two deferred claims: the first stays locked, the second's lock
        # clears. Drains of one job each must rotate through both, not retry
        # the first job forever in filename order.
        class LockNotAvailable(Exception):
            pass

        other = {**_WINDOW, "session_id": "claude_code:def"}
        cur = _DedupClaimCursor(dict(_WINDOW))
        cur.episodes[2] = {**cur.episodes[1], **other, "ts": "2026-10-15T11:30:00Z"}
        self._queue(_window_payload())
        with rc.gateway_request("caller-a"):
            outbox.enqueue({**_window_payload(), **other, "ts": "2026-10-15T12:30:00Z"})
        locked_rows = {1, 2}

        def execute(sql, params=None):
            if sql.lstrip().startswith("UPDATE episodes SET origin") and params[1] in locked_rows:
                raise LockNotAvailable("lock timeout")
            return _DedupClaimCursor.execute(cur, sql, params)

        with mock.patch.object(cur, "execute", side_effect=execute), \
                mock.patch("psycopg.connect", side_effect=AssertionError("real database")), \
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.machine_identity.local_origin", return_value=DRAINER), \
                mock.patch("khipu.config.path_setting", return_value=None):
            self.assertEqual(outbox.drain().get("deferred"), 2)
            locked_rows.discard(2)
            for _ in range(3):
                outbox.drain(limit=1)
        self.assertEqual(cur.episodes[2]["origin"], "gateway:caller-a")
        self.assertIsNone(cur.episodes[1]["origin"])
        self.assertEqual(len(outbox.jobs()), 1)

    def test_a_failing_job_retries_after_jobs_not_yet_tried(self):
        # Any retry, deferred or not, goes behind work that has not been tried,
        # so a bounded drain never spends its whole budget on old failures.
        first = outbox.enqueue({"ts": "2026-10-15T10:00:00Z", "summary": "fails", "session_id": "s"})
        fresh = outbox.enqueue({"ts": "2026-10-15T11:00:00Z", "summary": "new", "session_id": "s"})
        with mock.patch("khipu.capture.write_pg", side_effect=ValueError("bad row")):
            outbox.drain(limit=1)
        self.assertEqual(outbox._read_job(first)["attempts"], 1)
        with mock.patch("khipu.capture.write_pg", side_effect=ValueError("bad row")):
            outbox.drain(limit=1)
        self.assertEqual(outbox._read_job(fresh)["attempts"], 1)
        self.assertEqual(outbox._read_job(first)["attempts"], 1)

    def test_a_claim_that_is_not_required_never_blocks(self):
        # No trusted origin (a gateway-process capture outside a request), so
        # no claim is owed: a failing probe must not hold the job.
        self._queue(_window_payload(), label=None, gateway_process=True)
        cur = _DedupClaimCursor(dict(_WINDOW))
        with mock.patch.object(cap, "_stamp_origin", side_effect=RuntimeError("probe")):
            self._replay(_cli_drain, cur=cur)
        self.assertEqual(cur.inserts, [])
        self.assertIsNone(cur.episodes[1]["origin"])

    def test_a_merge_never_attempts_a_claim_so_a_failing_probe_never_blocks_it(self):
        self._queue(_merge_payload())
        cur = _merge_target()
        with mock.patch.object(cap, "_stamp_origin", side_effect=RuntimeError("probe")), \
                mock.patch.multiple(cap, **_MERGE_TEXT):
            self._replay(_cli_drain, cur=cur)
        self.assertEqual(cur.updated_ids, [1])
        self.assertEqual(cur.claims, [])

    def test_a_similarity_receipt_never_claims_on_a_retry(self):
        # A merge commits, then the drain dies before it deletes the job: the
        # retry finds the merge receipt, which records a text resemblance, not
        # an identity, so it must skip without claiming. Receipts written
        # before receipts recorded their match count as similarity.
        for name, label in (("gateway caller", "caller-a"), ("queuing machine", None)):
            with self.subTest(name):
                for job in outbox.jobs():
                    job.unlink()
                khipu_db._TABLE_COLUMNS_CACHE.clear()
                cur = _merge_target()
                self._queue(_merge_payload(), label=label)
                jp = outbox.jobs()[0]
                interrupted = jp.read_bytes()
                with mock.patch.multiple(cap, **_MERGE_TEXT):
                    self._replay(_cli_drain, cur=cur)
                    self.assertEqual(cur.updated_ids, [1], "the first drain merged")
                    (receipt,) = cur.episodes[1]["raw"]["merged_from"]
                    jp.write_bytes(interrupted)
                    self._replay(_cli_drain, cur=cur)
                    self.assertIsNone(cur.episodes[1]["origin"])
                    self.assertEqual(receipt.pop("match", None), "similarity")
                    jp.write_bytes(interrupted)
                    self._replay(_cli_drain, cur=cur)
                self.assertEqual(cur.inserts, [])
                self.assertEqual(cur.claims, [])
                self.assertIsNone(cur.episodes[1]["origin"])

    def test_an_identity_receipt_still_claims_on_a_retry(self):
        from khipu.capture_identity import source_identity

        payload = {"ts": "2026-10-15T12:00:00Z", "summary": "folded in by identity",
                   "session_id": "grokbot:review"}
        job = self._queue(payload)
        receipt = source_identity(dict(job["payload"]))["payload_sha256"]
        cur = _DedupClaimCursor({"raw": {"merged_from": [{"source_id": receipt,
                                                          "match": "identity"}]}})
        self._replay(_cli_drain, cur=cur)
        self.assertEqual(cur.episodes[1]["origin"], "gateway:caller-a")

    def test_a_planted_receipt_in_a_payload_never_reaches_the_row(self):
        # A file line becomes an unowned row; a merged_from it carried would
        # pose as an identity receipt and let a matching capture claim it.
        from khipu import mirror

        cur = _OriginFakeCursor()
        forged = {"ts": "2026-10-15T12:00:00Z", "summary": "planted", "session_id": "s1",
                  "merged_from": [{"source_id": "0" * 64, "match": "identity"}]}
        with mock.patch("khipu.capture_identity.record_source"):
            self.assertTrue(mirror._upsert_episode(cur, forged))
        self.assertNotIn("merged_from", json.loads(cur.inserts[0]["raw"]))

    def test_a_live_gateway_caller_never_claims_on_any_early_return(self):
        cases = (("exact window", _DedupClaimCursor(dict(_WINDOW)), _window_payload(), None),
                 ("merge", _merge_target(), _merge_payload(), _MERGE_TEXT))
        for name, cur, payload, patches in cases:
            with self.subTest(name), \
                    mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                    mock.patch("khipu.machine_identity.local_origin", return_value=DRAINER), \
                    mock.patch("khipu.embed.embed_on_capture", return_value={}), \
                    mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}), \
                    mock.patch("khipu.config.path_setting", return_value=None), \
                    mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key")), \
                    mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)), \
                    mock.patch.multiple(cap, **patches) if patches else contextlib.nullcontext(), \
                    rc.gateway_request("caller-b"):
                stats = cap.write_pg(payload)
                self.assertIn(stats["dedup"]["action"], ("skip", "merge"))
                self.assertEqual(cur.claims, [])
                self.assertIsNone(cur.episodes[1]["origin"])


if __name__ == "__main__":
    unittest.main()
