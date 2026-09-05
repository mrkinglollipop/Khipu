"""khipu.outbox — dead-letter handling (audit 2026-09-04: attempts was
written and never read, so a permanently-failing job retried forever and
kept doctor red; a connection failure must never bury a job that would have
succeeded once PG came back)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from khipu import outbox


class OutboxDeadTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="khipu-outbox-dead-")
        self.env = mock.patch.dict(os.environ, {"KHIPU_OUTBOX": self.td})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_a_job_past_max_attempts_is_buried_and_can_be_retried(self):
        jp = outbox.enqueue(
            {"ts": "2026-09-05T12:00:00Z", "summary": "never lands", "session_id": "codex:x"},
            reason="test",
        )
        job = json.loads(jp.read_text())
        job["attempts"] = outbox.MAX_ATTEMPTS - 1
        outbox._atomic_write(jp, job)

        with mock.patch("khipu.capture.write_pg", side_effect=ValueError("payload rejected")):
            out = outbox.drain()
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out.get("buried"), 1)

        st = outbox.status()
        self.assertEqual(st["pending"], 0)
        self.assertEqual(st["dead"], 1)

        self.assertEqual(outbox.retry_dead()["moved"], 1)

        st = outbox.status()
        self.assertEqual(st["pending"], 1)
        self.assertEqual(st["dead"], 0)
        requeued = json.loads(outbox.jobs()[0].read_text())
        self.assertEqual(requeued["attempts"], 0)

    def test_a_connection_failure_never_buries(self):
        jp = outbox.enqueue(
            {"ts": "2026-09-05T12:00:00Z", "summary": "temporarily unreachable", "session_id": "codex:y"},
            reason="test",
        )
        job = json.loads(jp.read_text())
        job["attempts"] = outbox.MAX_ATTEMPTS + 5
        outbox._atomic_write(jp, job)

        class OperationalError(Exception):
            pass

        def down(payload):
            raise OperationalError("connection refused")

        with mock.patch("khipu.capture.write_pg", down):
            out = outbox.drain()
        self.assertTrue(out["stopped_early"])
        self.assertEqual(outbox.status()["dead"], 0)


if __name__ == "__main__":
    unittest.main()
