"""Offline and self-healing, slice 1 (docs/plans/2026-10-16-offline-and-self-healing.md, D):
doctor's rate checks judge a time window the owner sets, and calls made while
the hub could not be reached are reported apart, never as failures."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from khipu import promote, query_log
from khipu import recall_prompt as rp


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class DegradedRateWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="khipu-qlog-"))
        self.path = self.tmp / "query_log.jsonl"
        patch = mock.patch.object(query_log, "log_path", return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)
        self.now = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)

    def _write(self, entries: list[dict]) -> None:
        self.path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    def test_an_old_burst_ages_out(self) -> None:
        old = _ts(self.now - timedelta(hours=3))
        self._write([{"ts": old, "degraded": "embed-unavailable"}] * 50)
        out = query_log.degraded_rate(now=self.now, window_minutes=60)
        self.assertTrue(out["ok"])
        self.assertTrue(out["quiet"])
        self.assertEqual(out["sampled"], 0)

    def test_a_recent_burst_stays_red(self) -> None:
        recent = _ts(self.now - timedelta(minutes=5))
        self._write([{"ts": recent, "degraded": "embed-unavailable"}] * 50)
        out = query_log.degraded_rate(now=self.now, window_minutes=60)
        self.assertFalse(out["ok"])
        self.assertEqual(out["sampled"], 50)

    def test_offline_searches_are_not_counted_as_degraded(self) -> None:
        recent = _ts(self.now - timedelta(minutes=5))
        entries = ([{"ts": recent, "degraded": "hub unreachable", "offline": True}] * 40
                   + [{"ts": recent}] * 10)
        self._write(entries)
        out = query_log.degraded_rate(now=self.now, window_minutes=60)
        self.assertTrue(out["ok"])
        self.assertEqual(out["offline"], 40)
        self.assertEqual(out["sampled"], 10)
        self.assertEqual(out["degraded"], 0)

    def test_window_defaults_to_the_owner_setting(self) -> None:
        old = _ts(self.now - timedelta(hours=3))
        self._write([{"ts": old, "degraded": "embed-unavailable"}] * 50)
        with mock.patch("khipu.config.minute_setting", return_value=240):
            out = query_log.degraded_rate(now=self.now)
        self.assertFalse(out["ok"])
        self.assertEqual(out["window_minutes"], 240)

    def test_log_query_records_offline(self) -> None:
        with mock.patch("khipu.paths.ensure_data_dir"):
            query_log.log_query("q", mode="hybrid", result_count=0, offline=True)
            query_log.log_query("q", mode="hybrid", result_count=0)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertIs(rows[0].get("offline"), True)
        self.assertNotIn("offline", rows[1])


class OfflineSummaryLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lines: list[str] = []
        patch = mock.patch.object(rp, "_log", side_effect=self.lines.append)
        patch.start()
        self.addCleanup(patch.stop)

    def test_finish_delivery_tags_an_offline_call(self) -> None:
        result = {"reason": "error: OperationalError: connection refused", "hits": [], "ms": 5.0,
                  "legs": [], "degraded": None, "offline": True}
        rp.finish_delivery({"result": result, "raw": "{}"}, delivered=True)
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].endswith(" offline=1"), self.lines[0])

    def test_a_tagged_line_parses_for_doctor_and_the_gate(self) -> None:
        line = ("2026-10-16T13:00:00Z [khipu-prompt-recall] session=s reason=timeout>1.2s ms=1200.0 "
                "hits=[] legs=[] degraded=timeout stage=hub offline=1 routing=ok")
        self.assertIsNotNone(rp._LOG_LINE_RE.match(line))
        self.assertIsNotNone(rp._OFFLINE_FIELD_RE.search(line))
        parsed = promote._parse_summary(line)
        self.assertEqual(parsed["degraded"], "timeout")
        self.assertEqual(parsed["routing"], "ok")
        bare = "2026-10-16T13:00:00Z [khipu-prompt-recall] session=s reason=ok ms=3.0 hits=[] legs=[] degraded=None offline=1"
        self.assertEqual(promote._parse_summary(bare)["degraded"], "None")

    def test_a_connection_failure_is_offline_and_a_bug_is_not(self) -> None:
        self.assertTrue(rp._hub_unreachable_error(ConnectionRefusedError("connection refused")))
        self.assertFalse(rp._hub_unreachable_error(KeyError("hits")))


class RateWindowSettingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="khipu-cfg-"))
        patch = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def _config(self, *argv: str) -> tuple[int, dict]:
        from khipu import cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["config", *argv])
        return rc, json.loads(buf.getvalue())

    def test_set_show_and_unset(self) -> None:
        from khipu import config

        self.assertEqual(config.config_file().parent, self.home)
        self.assertEqual(config.minute_setting("doctor_rate_window_minutes"), 60)
        rc, out = self._config("--set", "doctor_rate_window_minutes", "90")
        self.assertEqual(rc, 0)
        self.assertEqual(out["doctor_rate_window_minutes"]["value"], 90)
        self.assertEqual(config.minute_settings_status()["doctor_rate_window_minutes"]["source"], "file")
        rc, out = self._config("--unset", "doctor_rate_window_minutes")
        self.assertEqual(out["doctor_rate_window_minutes"]["value"], 60)
        self.assertNotIn("doctor_rate_window_minutes", config.load_config())

    def test_out_of_range_is_refused(self) -> None:
        rc, out = self._config("--set", "doctor_rate_window_minutes", "0")
        self.assertEqual(rc, 2)
        self.assertIn("between 5 and", out["error"])


if __name__ == "__main__":
    unittest.main()
