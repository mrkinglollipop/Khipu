"""Tests for khipu.capture + khipu.config — P3 step 2 (dual-write entrypoint).

Pure tests cover config precedence, payload validation, ts minting, and mode
routing (capture_v2 and PG are stubbed). One live test performs a real
hub-mode write against Postgres with an unmistakable probe summary and deletes
it afterwards; it skips when PG is unreachable.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import os
import tempfile
import unittest
from unittest import mock

from khipu import capture as cap
from khipu import config as cfg


def _pg_available() -> bool:
    try:
        from khipu.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        return False
    return True


PG_AVAILABLE = _pg_available()


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khipu-cfg-")
        self._env = dict(os.environ)
        os.environ["KHIPU_DATA_DIR"] = self.tmp
        os.environ.pop("KHIPU_CAPTURE_MODE", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_default_is_dual(self):
        self.assertEqual(cfg.capture_mode(), "dual")

    def test_set_persists_and_reads_back(self):
        path = cfg.set_capture_mode("hub")
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text())["capture_mode"], "hub")
        self.assertEqual(cfg.capture_mode(), "hub")

    def test_env_overrides_file(self):
        cfg.set_capture_mode("hub")
        os.environ["KHIPU_CAPTURE_MODE"] = "legacy"
        self.assertEqual(cfg.capture_mode(), "legacy")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            cfg.set_capture_mode("yolo")

    def test_garbage_file_falls_back(self):
        cfg.config_file().parent.mkdir(parents=True, exist_ok=True)
        cfg.config_file().write_text("{not json")
        self.assertEqual(cfg.capture_mode(), "dual")


class LoadPayloadTest(unittest.TestCase):
    def test_empty_is_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cap.load_payload("")
        self.assertEqual(ctx.exception.code, cap.EX_USAGE)

    def test_bad_json_is_data_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cap.load_payload("{nope")
        self.assertEqual(ctx.exception.code, cap.EX_DATAERR)

    def test_missing_summary_is_data_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cap.load_payload(json.dumps({"topics": ["x"]}))
        self.assertEqual(ctx.exception.code, cap.EX_DATAERR)

    def test_ts_minted_once_seconds_z(self):
        p = cap.load_payload(json.dumps({"summary": "hi"}))
        self.assertRegex(p["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_existing_ts_preserved(self):
        p = cap.load_payload(json.dumps({"summary": "hi", "ts": "2026-01-01T00:00:00Z"}))
        self.assertEqual(p["ts"], "2026-01-01T00:00:00Z")


class RoutingTest(unittest.TestCase):
    """capture() routing with both legs stubbed — asserts WHO gets called and
    whether the mirror-suppression flag is set. Contract since 2026-08-17
    (outbox): a PG failure is queued, the file leg still runs, and the call
    returns 0 because the capture is durable in two places."""

    def setUp(self):
        import tempfile
        self.calls: list[tuple[str, dict]] = []
        self.p_pg = mock.patch.object(
            cap, "write_pg",
            side_effect=lambda payload: self.calls.append(("pg", {})) or {"episode_inserted": True, "topics_written": 0},
        )
        self.p_v2 = mock.patch.object(
            cap, "run_capture_v2",
            side_effect=lambda payload, *, suppress_mirror: self.calls.append(("v2", {"suppress": suppress_mirror})) or 0,
        )
        self.p_pg.start()
        self.p_v2.start()
        # Never let a routing test touch the real outbox directory.
        self.outbox_dir = tempfile.mkdtemp(prefix="khipu-routing-outbox-")
        self.p_env = mock.patch.dict(os.environ, {"KHIPU_OUTBOX": self.outbox_dir})
        self.p_env.start()

    def tearDown(self):
        self.p_pg.stop()
        self.p_v2.stop()
        self.p_env.stop()

    def _pending(self) -> int:
        from khipu.outbox import status
        return status()["pending"]

    def test_trivial_skips_everything(self):
        self.assertEqual(cap.capture({"summary": "x", "scope": "trivial"}, mode="dual"), 0)
        self.assertEqual(self.calls, [])

    def test_legacy_only_v2_mirror_allowed(self):
        cap.capture({"summary": "x"}, mode="legacy")
        self.assertEqual(self.calls, [("v2", {"suppress": False})])

    def test_dual_pg_first_then_v2_with_mirror_suppressed(self):
        cap.capture({"summary": "x"}, mode="dual")
        self.assertEqual(self.calls, [("pg", {}), ("v2", {"suppress": True})])

    def test_hub_pg_first_then_reverse_mirror_to_file(self):
        # hub = PG is the record; the file is its reverse mirror (plan wording),
        # so the legacy consumers keep working. Off only via KHIPU_HUB_FILE_MIRROR=0.
        cap.capture({"summary": "x"}, mode="hub")
        self.assertEqual(self.calls, [("pg", {}), ("v2", {"suppress": True})])
        self.calls.clear()
        with mock.patch.dict(os.environ, {"KHIPU_HUB_FILE_MIRROR": "0"}):
            cap.capture({"summary": "x"}, mode="hub")
        self.assertEqual(self.calls, [("pg", {})])

    def test_pg_failure_is_queued_file_still_written_and_returns_zero(self):
        for mode in ("dual", "hub"):
            self.calls.clear()
            self.p_pg.stop()
            with mock.patch.object(cap, "write_pg", side_effect=RuntimeError("pg down")):
                rc = cap.capture({"summary": "x", "ts": "2026-08-17T18:00:00Z"}, mode=mode)
            self.p_pg.start()
            self.assertEqual(rc, 0, mode)                                   # durable: outbox + file
            self.assertEqual(self.calls, [("v2", {"suppress": False})], mode)  # file leg, own mirror ON
        self.assertEqual(self._pending(), 1)                                # same identity -> one job

    def test_pg_failure_with_unwritable_outbox_is_a_real_exit(self):
        self.p_pg.stop()
        with mock.patch.object(cap, "write_pg", side_effect=RuntimeError("pg down")), \
                mock.patch("khipu.outbox.enqueue", side_effect=OSError("disk full")):
            rc = cap.capture({"summary": "x"}, mode="hub")
        self.p_pg.start()
        self.assertEqual(rc, cap.EX_SOFTWARE)                               # nowhere durable but the file
        self.assertEqual(self.calls, [("v2", {"suppress": False})])


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live capture round-trip")
class LiveHubWriteTest(unittest.TestCase):
    """Real PG write in hub mode with a probe row, verified by identity, then deleted."""

    def test_hub_write_lands_and_is_idempotent(self):
        from khipu.db import connect

        summary = f"khipu-test-probe {os.getpid()} — safe to delete"
        payload = cap.load_payload(json.dumps({"summary": summary, "scope": "general"}))
        md = hashlib.md5(summary.encode()).hexdigest()
        # hub now reverse-mirrors to the legacy file (2026-08-17); a test must
        # never write a probe line into the real episodes.jsonl, so stub the leg.
        file_leg = mock.patch.object(cap, "run_capture_v2", lambda p, suppress_mirror: 0)
        file_leg.start()
        self.addCleanup(file_leg.stop)
        try:
            self.assertEqual(cap.capture(payload, mode="hub"), 0)
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s",
                    (payload["ts"], md),
                )
                self.assertEqual(cur.fetchone()[0], 1)
            # Same identity again → no second row (uq index + ON CONFLICT DO NOTHING).
            self.assertEqual(cap.capture(payload, mode="hub"), 0)
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM episodes WHERE md5(summary) = %s", (md,))
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            with connect() as conn, conn.cursor() as cur:
                # Vectors ride the capture now (P3 step 3): remove the probe's
                # embedding too, or coverage over-reports until the next sweep.
                cur.execute(
                    "DELETE FROM memory_embeddings WHERE kind='episode' AND ref IN "
                    "(SELECT id::text FROM episodes WHERE md5(summary) = %s)",
                    (md,),
                )
                cur.execute("DELETE FROM episodes WHERE md5(summary) = %s", (md,))
                conn.commit()



class PayloadRobustnessTest(unittest.TestCase):
    """Regressions from the 2026-08-17 audit: a malformed payload must exit with
    the documented code, and a hung file leg must not throw out of the hook."""

    def test_non_string_summary_exits_dataerr_instead_of_crashing(self):
        for bad in (123, ["a"], {"x": 1}, None):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as e:
                cap.load_payload(json.dumps({"summary": bad, "ts": "2026-08-17T18:00:00Z"}))
            self.assertEqual(e.exception.code, cap.EX_DATAERR)

    def test_non_string_session_id_is_ignored_not_fatal(self):
        out = cap.load_payload(json.dumps({"summary": "real", "session_id": 42}))
        self.assertEqual(out["summary"], "real")

    def test_blank_input_and_bad_json_keep_their_documented_codes(self):
        with self.assertRaises(SystemExit) as e:
            cap.load_payload("   ")
        self.assertEqual(e.exception.code, cap.EX_USAGE)
        with self.assertRaises(SystemExit) as e:
            cap.load_payload("{not json")
        self.assertEqual(e.exception.code, cap.EX_DATAERR)

    def test_a_hung_capture_v2_returns_a_code_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            fake = pathlib.Path(td) / "capture_v2.py"
            fake.write_text("import time\ntime.sleep(30)\n")
            with mock.patch.object(cap, "_capture_v2", return_value=fake), \
                 mock.patch("subprocess.run",
                            side_effect=subprocess.TimeoutExpired(cmd="capture_v2", timeout=300)):
                rc = cap.run_capture_v2({"summary": "s"}, suppress_mirror=True)
        self.assertEqual(rc, cap.EX_SOFTWARE)

    def test_an_unrunnable_capture_v2_returns_a_code_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            fake = pathlib.Path(td) / "capture_v2.py"
            fake.write_text("x")
            with mock.patch.object(cap, "_capture_v2", return_value=fake), \
                 mock.patch("subprocess.run", side_effect=OSError("exec format error")):
                rc = cap.run_capture_v2({"summary": "s"}, suppress_mirror=True)
        self.assertEqual(rc, cap.EX_SOFTWARE)

if __name__ == "__main__":
    unittest.main()
