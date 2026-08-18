"""Tests for khipu.outbox — the durability leg behind hub-as-default.

Pure parts use a temp KHIPU_OUTBOX. The live part (skipped when PG is
unreachable) simulates a real PG outage with a bogus DSN, asserts the capture
was queued and still returned 0, then restores the DSN, drains, verifies the
row by identity, and cleans it up.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from khipu import outbox


def _pg_available() -> bool:
    try:
        from khipu.db import connect
        with connect() as c, c.cursor() as cur:
            cur.execute("select 1")
        return True
    except Exception:  # noqa: BLE001
        return False


class OutboxUnitTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="khipu-outbox-")
        self.env = mock.patch.dict(os.environ, {"KHIPU_OUTBOX": self.td})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_enqueue_is_idempotent_by_identity_and_status_counts(self):
        p = {"ts": "2026-08-17T18:00:00Z", "summary": "hello", "topics": []}
        a = outbox.enqueue(p, reason="test")
        b = outbox.enqueue(p, reason="test again")          # same identity -> same file
        self.assertEqual(a, b)
        self.assertEqual(outbox.status()["pending"], 1)
        outbox.enqueue({"ts": "2026-08-17T18:00:01Z", "summary": "hello"})
        self.assertEqual(outbox.status()["pending"], 2)
        self.assertIsNotNone(outbox.status()["oldest_age_s"])
        job = json.loads(a.read_text())
        self.assertEqual(job["payload"]["summary"], "hello")
        self.assertIn("queued_at", job)

    def test_drain_deletes_on_success_keeps_on_failure_and_stops_early_on_connection_loss(self):
        for i in range(3):
            outbox.enqueue({"ts": f"2026-08-17T18:00:0{i}Z", "summary": f"s{i}"})
        calls = []

        def fake_write_pg(payload):
            calls.append(payload["summary"])
            if payload["summary"] == "s1":
                raise RuntimeError("constraint-ish failure")   # not a connection error
            return {"episode_inserted": True, "topics_written": 0}

        with mock.patch("khipu.capture.write_pg", fake_write_pg), \
                mock.patch("khipu.embed.embed_on_capture", lambda p: True):
            out = outbox.drain()
        self.assertEqual((out["replayed"], out["failed"], out["stopped_early"]), (2, 1, False))
        self.assertEqual(outbox.status()["pending"], 1)
        kept = json.loads(outbox.jobs()[0].read_text())
        self.assertEqual((kept["payload"]["summary"], kept["attempts"]), ("s1", 1))
        self.assertIn("RuntimeError", kept["last_error"])

        class OperationalError(Exception):
            pass

        def down(payload):
            raise OperationalError("connection failed")

        outbox.enqueue({"ts": "2026-08-17T18:00:09Z", "summary": "s9"})
        with mock.patch("khipu.capture.write_pg", down):
            out = outbox.drain()
        self.assertTrue(out["stopped_early"])
        self.assertEqual(outbox.status()["pending"], 2)      # nothing lost

    def test_capture_queues_on_pg_failure_and_still_writes_the_file_leg(self):
        from khipu import capture as cap

        payload = {"ts": "2026-08-17T18:00:00Z", "summary": "queued one", "topics": []}
        ran = []
        with mock.patch.object(cap, "write_pg", side_effect=RuntimeError("pg down")), \
                mock.patch.object(cap, "run_capture_v2", lambda p, suppress_mirror: ran.append(suppress_mirror) or 0):
            self.assertEqual(cap.capture(payload, mode="dual"), 0)
            self.assertEqual(cap.capture(payload, mode="hub"), 0)
        self.assertEqual(ran, [False, False])              # file leg ran, mirror left ON
        self.assertEqual(outbox.status()["pending"], 1)      # one identity, queued once

    def test_hub_reverse_mirrors_to_file_by_default(self):
        from khipu import capture as cap

        ran = []
        with mock.patch.object(cap, "write_pg", return_value={"episode_inserted": False, "topics_written": 0}), \
                mock.patch.object(cap, "run_capture_v2", lambda p, suppress_mirror: ran.append(suppress_mirror) or 0):
            self.assertEqual(cap.capture({"summary": "x", "ts": "t"}, mode="hub"), 0)
            with mock.patch.dict(os.environ, {"KHIPU_HUB_FILE_MIRROR": "0"}):
                self.assertEqual(cap.capture({"summary": "x", "ts": "t"}, mode="hub"), 0)
        self.assertEqual(ran, [True])                       # once with mirror suppressed; off when disabled

    def test_legacy_mirror_leg_queues_instead_of_losing(self):
        from khipu import mirror

        with mock.patch.object(mirror, "mirror_episode", side_effect=RuntimeError("no keychain")):
            self.assertIsNone(mirror.mirror_after_capture({"ts": "2026-08-17T18:00:00Z", "summary": "m"}))
        self.assertEqual(outbox.status()["pending"], 1)


@unittest.skipUnless(_pg_available(), "Postgres unreachable")
class OutboxDurabilityTest(unittest.TestCase):
    """Regressions from the 2026-08-17 audit. The outbox IS the durability leg,
    so a torn write or a misreported age here is worse than elsewhere."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="khipu-outbox-dur-")
        self.env = mock.patch.dict(os.environ, {"KHIPU_OUTBOX": self.td})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_the_retry_path_writes_atomically(self):
        """It used to write the job in place on failure. A crash mid-write left
        an unreadable job: never replayed, never deleted, doctor red forever."""
        outbox.enqueue({"ts": "2026-08-17T18:00:00Z", "summary": "s"}, reason="test")
        jp = outbox.jobs()[0]

        seen = {}
        real = pathlib.Path.write_text

        def spy(self, data, *a, **kw):
            seen.setdefault("first_target", self.name)
            return real(self, data, *a, **kw)

        class Boom(Exception):
            pass

        with mock.patch.object(pathlib.Path, "write_text", spy), \
             mock.patch("khipu.capture.write_pg", side_effect=Boom("nope")), \
             mock.patch("khipu.embed.embed_on_capture", return_value=False):
            outbox.drain()
        self.assertTrue(seen["first_target"].endswith(".tmp"),
                        f"retry wrote directly to {seen['first_target']}")
        # And the job survived, readable, with the attempt recorded.
        job = json.loads(jp.read_text())
        self.assertEqual(job["attempts"], 1)
        self.assertIn("Boom", job["last_error"])

    def test_oldest_is_the_longest_waiting_job_not_the_earliest_episode(self):
        """jobs() sorts by filename, i.e. by episode ts, so status() used to
        report the age of whichever EPISODE was oldest."""
        outbox.enqueue({"ts": "2020-01-01T00:00:00Z", "summary": "ancient episode"}, reason="a")
        outbox.enqueue({"ts": "2030-01-01T00:00:00Z", "summary": "future episode"}, reason="b")
        by_name = {p.name: p for p in outbox.jobs()}
        # The FUTURE-dated episode is the one that has actually been waiting.
        future = next(n for n in by_name if n.startswith("20300101"))
        j = json.loads(by_name[future].read_text())
        j["queued_at"] = "2026-08-01T00:00:00+00:00"
        by_name[future].write_text(json.dumps(j))
        st = outbox.status()
        self.assertEqual(st["oldest_job"], future)
        self.assertGreater(st["oldest_age_s"], 86400)

    def test_an_unreadable_job_is_counted_not_crashed_on(self):
        outbox.enqueue({"ts": "2026-08-17T18:00:00Z", "summary": "s"}, reason="test")
        outbox.jobs()[0].write_text("{ torn")
        st = outbox.status()
        self.assertEqual(st["pending"], 1)
        self.assertEqual(st["unreadable"], 1)
        self.assertIsNone(st["oldest_age_s"])


class LiveOutageTest(unittest.TestCase):
    def test_pg_outage_then_recovery_lands_exactly_one_row(self):
        from khipu import capture as cap
        from khipu.db import connect

        summary = f"khipu-test-probe outbox {os.getpid()} — safe to delete"
        payload = cap.load_payload(json.dumps({"summary": summary, "scope": "general"}))
        md = hashlib.md5(summary.encode()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="khipu-outbox-live-") as td, \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": td}):
            try:
                with mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL":
                                                  "postgresql://nobody@127.0.0.1:1/none?connect_timeout=2"}), \
                        mock.patch.object(cap, "run_capture_v2", lambda p, suppress_mirror: 0):
                    self.assertEqual(cap.capture(payload, mode="hub"), 0)   # outage: queued, not failed
                self.assertEqual(outbox.status()["pending"], 1)
                out = outbox.drain()                                          # PG is back
                self.assertEqual((out["replayed"], out["failed"]), (1, 0))
                self.assertEqual(outbox.status()["pending"], 0)
                with connect() as conn, conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s",
                                (payload["ts"], md))
                    self.assertEqual(cur.fetchone()[0], 1)
                # A second drain of the same identity must not add a row.
                outbox.enqueue(payload)
                outbox.drain()
                with connect() as conn, conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM episodes WHERE md5(summary) = %s", (md,))
                    self.assertEqual(cur.fetchone()[0], 1)
            finally:
                with connect() as conn, conn.cursor() as cur:
                    cur.execute("DELETE FROM memory_embeddings WHERE kind='episode' AND ref IN "
                                "(SELECT id::text FROM episodes WHERE md5(summary) = %s)", (md,))
                    cur.execute("DELETE FROM episodes WHERE md5(summary) = %s", (md,))
                    conn.commit()


if __name__ == "__main__":
    unittest.main()
