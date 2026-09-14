"""Phase 6 (honesty) contract tests — doctor and Home must never stay green
because evidence never arrived. One test per gap the brief names: nightly
step evidence (D1/D2), outbox/embed/search-degrade age (D6), gateway
per-token liveness (K9), an unrecognised harness's heartbeat (K8), the
Aegis queue-drain LaunchAgent (K10), and khipu_status's new fields (D4/R10).
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class NightlyStepHealthTest(unittest.TestCase):
    """D1/D2: doctor reads nightly-last.json per step instead of trusting
    only the legacy driver's exit code."""

    def _write_last(self, tmp: Path, steps: list[dict]) -> None:
        (tmp / "nightly-last.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")

    def test_not_applicable_off_the_sync_host_reads_green(self):
        from khipu import jobs

        with mock.patch.object(jobs, "_is_index_sync_host", return_value=False):
            out = jobs.nightly_step_health()
        for key in ("notes_reconcile_ok", "embed_provider_ok", "commitments_hygiene_ok", "mark_stale_ok"):
            self.assertTrue(out[key]["ok"])
            self.assertFalse(out[key]["applicable"])

    def test_missing_evidence_on_the_sync_host_is_skipped_not_red(self):
        """A step that has never been recorded is a false red on every fresh
        install and every Mac the day it updates (maintainer, 2026-09-14:
        "especially after they update") — skipped, and must not flip `ok`."""
        from khipu import jobs

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                    mock.patch.object(jobs, "ensure_data_dir", return_value=Path(td)):
                out = jobs.nightly_step_health()
        for key in ("notes_reconcile_ok", "embed_provider_ok", "commitments_hygiene_ok", "mark_stale_ok"):
            self.assertTrue(out[key]["ok"], key)
            self.assertTrue(out[key]["skipped"], key)
            self.assertEqual(out[key]["reason"], "not checked yet — the nightly runs at 02:05")
        self.assertEqual(
            jobs.skipped_step_names(out),
            ["notes_reconcile", "embed_provider", "commitments_hygiene", "mark_stale"],
        )

    def test_a_stale_recording_over_36h_old_is_red_even_if_it_was_ok(self):
        """A nightly that stopped running altogether must not hide behind a
        week-old `ok: true` — this is a different failure mode from a step
        that ran and failed, so it gets its own message."""
        from datetime import datetime, timedelta, timezone

        from khipu import jobs

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_last(tmp, [
                {"name": "notes_reconcile", "ok": True, "counts": {}, "error": None, "ts": old_ts},
            ])
            with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                    mock.patch.object(jobs, "ensure_data_dir", return_value=tmp):
                out = jobs.nightly_step_health()
        self.assertFalse(out["notes_reconcile_ok"]["ok"])
        self.assertNotIn("skipped", out["notes_reconcile_ok"])
        self.assertIn("the nightly has not run since", out["notes_reconcile_ok"]["error"])
        self.assertIn("khipu jobs status", out["notes_reconcile_ok"]["fix"])
        # Stale, not skipped: it must not show up in the skipped/not_configured list.
        self.assertNotIn("notes_reconcile", jobs.skipped_step_names(out))

    def test_a_recent_recording_under_36h_old_is_judged_on_its_own_ok(self):
        from datetime import datetime, timedelta, timezone

        from khipu import jobs

        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_last(tmp, [
                {"name": "notes_reconcile", "ok": True, "counts": {}, "error": None, "ts": recent_ts},
            ])
            with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                    mock.patch.object(jobs, "ensure_data_dir", return_value=tmp):
                out = jobs.nightly_step_health()
        self.assertTrue(out["notes_reconcile_ok"]["ok"])
        self.assertNotIn("skipped", out["notes_reconcile_ok"])

    def test_a_failed_step_is_red_with_its_own_error_and_fix(self):
        from khipu import jobs

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_last(tmp, [
                {"name": "notes_reconcile", "ok": True, "counts": {}, "error": None, "ts": "t1"},
                {"name": "embed_backfill", "ok": False, "counts": {}, "error": "missing key", "ts": "t2"},
                {"name": "commitments_mark_stale", "ok": True, "counts": {}, "error": None, "ts": "t3"},
                {"name": "commitments_hygiene", "ok": True, "counts": {}, "error": None, "ts": "t4"},
            ])
            with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                    mock.patch.object(jobs, "ensure_data_dir", return_value=tmp):
                out = jobs.nightly_step_health()
        self.assertTrue(out["notes_reconcile_ok"]["ok"])
        self.assertFalse(out["embed_provider_ok"]["ok"])
        self.assertEqual(out["embed_provider_ok"]["error"], "missing key")
        self.assertIn("embedding provider", out["embed_provider_ok"]["fix"])
        self.assertTrue(out["mark_stale_ok"]["ok"])
        self.assertTrue(out["commitments_hygiene_ok"]["ok"])

    def test_a_later_step_entry_for_the_same_name_wins(self):
        """Steps are appended every nightly run; the LAST entry for a step
        name is tonight's outcome, not last week's."""
        from khipu import jobs

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_last(tmp, [
                {"name": "notes_reconcile", "ok": False, "counts": {}, "error": "old failure", "ts": "t1"},
                {"name": "notes_reconcile", "ok": True, "counts": {}, "error": None, "ts": "t2"},
            ])
            with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                    mock.patch.object(jobs, "ensure_data_dir", return_value=tmp):
                out = jobs.nightly_step_health()
        self.assertTrue(out["notes_reconcile_ok"]["ok"])


class NightlyLogDualWriteTest(unittest.TestCase):
    """D2: structured nightly evidence goes to the Khipu log dir ALWAYS and
    additionally to the legacy dir when configured — never instead."""

    def test_writes_both_locations_when_legacy_is_configured(self):
        from khipu import jobs

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            khipu_log = tmp / "khipu.out.log"
            legacy_dir = tmp / "legacy"
            legacy_dir.mkdir()
            with mock.patch.object(jobs, "_log_paths", return_value=(khipu_log, tmp / "khipu.err.log")), \
                    mock.patch.object(jobs, "LOG_DIR_LEGACY", legacy_dir):
                jobs._nightly_log("notes-reconcile: {}")
            self.assertIn("notes-reconcile", khipu_log.read_text())
            self.assertIn("notes-reconcile", (legacy_dir / "khipu-nightly.out.log").read_text())

    def test_writes_only_khipu_dir_when_no_legacy_configured(self):
        from khipu import jobs

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            khipu_log = tmp / "khipu.out.log"
            with mock.patch.object(jobs, "_log_paths", return_value=(khipu_log, tmp / "khipu.err.log")), \
                    mock.patch.object(jobs, "LOG_DIR_LEGACY", None):
                jobs._nightly_log("notes-reconcile: {}")
            self.assertIn("notes-reconcile", khipu_log.read_text())


class TopicsEmbedLagTest(unittest.TestCase):
    """D6/F2: how long has the oldest unembedded topic been waiting."""

    def _mock_connect(self, row):
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchone.return_value = row
        conn.cursor.return_value = cur
        return conn, cur

    def test_nothing_unembedded_is_green(self):
        from khipu import embed

        conn, cur = self._mock_connect((None,))
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(embed, "_resolve_profile", return_value="p1"):
            out = embed.topics_embed_lag_minutes()
        self.assertTrue(out["ok"])
        self.assertEqual(out["lag_minutes"], 0)

    def test_over_an_hour_is_red(self):
        from datetime import datetime, timedelta, timezone

        from khipu import embed

        oldest = datetime.now(timezone.utc) - timedelta(minutes=90)
        conn, cur = self._mock_connect((oldest,))
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(embed, "_resolve_profile", return_value="p1"):
            out = embed.topics_embed_lag_minutes()
        self.assertFalse(out["ok"])
        self.assertGreaterEqual(out["lag_minutes"], 89)

    def test_under_an_hour_is_green(self):
        from datetime import datetime, timedelta, timezone

        from khipu import embed

        oldest = datetime.now(timezone.utc) - timedelta(minutes=10)
        conn, cur = self._mock_connect((oldest,))
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(embed, "_resolve_profile", return_value="p1"):
            out = embed.topics_embed_lag_minutes()
        self.assertTrue(out["ok"])


class DegradedRateTest(unittest.TestCase):
    """D6/F4: fraction of the last N searches that degraded, not just a raw
    24h count — a rate makes a burst show on a high-volume day."""

    def test_no_searches_yet_is_green_not_red_on_idleness(self):
        from khipu import query_log

        with mock.patch.object(query_log, "tail", return_value=[]):
            out = query_log.degraded_rate()
        self.assertTrue(out["ok"])
        self.assertEqual(out["sampled"], 0)

    def test_over_20_percent_degraded_is_red(self):
        from khipu import query_log

        entries = [{"degraded": "no-embedding"}] * 3 + [{"degraded": None}] * 7
        with mock.patch.object(query_log, "tail", return_value=entries):
            out = query_log.degraded_rate(n=10)
        self.assertFalse(out["ok"])
        self.assertAlmostEqual(out["rate"], 0.3)
        self.assertEqual(out["degraded"], 3)

    def test_under_20_percent_degraded_is_green(self):
        from khipu import query_log

        entries = [{"degraded": "no-embedding"}] + [{"degraded": None}] * 9
        with mock.patch.object(query_log, "tail", return_value=entries):
            out = query_log.degraded_rate(n=10)
        self.assertTrue(out["ok"])


class OutboxAgeTest(unittest.TestCase):
    """D6: outbox_ok used to test pending==0 only, never age — a job stuck
    failing-then-retrying could sit at pending==0 between attempts."""

    def test_status_carries_oldest_age_for_doctor_to_threshold(self):
        from khipu import outbox

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            job = d / "20260101T000000Z-abcdef0123.json"
            old_ts = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 7200)
            )
            job.write_text(json.dumps({"queued_at": old_ts, "attempts": 1}), encoding="utf-8")
            with mock.patch.object(outbox, "outbox_dir", return_value=d), \
                    mock.patch.object(outbox, "dead_dir", return_value=d / "dead"):
                st = outbox.status()
        self.assertEqual(st["pending"], 1)
        self.assertGreaterEqual(st["oldest_age_s"], 7100)


class GatewayLivenessRoundTripTest(unittest.TestCase):
    """K9: per-token last_ok_at/last_error_at, read back from the same file
    `/healthz` serves — the round trip doctor and `integrations verify`
    both depend on."""

    def test_record_then_read_round_trips(self):
        from khipu import gateway

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gateway-liveness.json"
            with mock.patch.object(gateway, "_liveness_path", return_value=path):
                gateway._record_liveness("grokbot", ok=True)
                gateway._record_liveness("grokbot", ok=False, error="401 unauthorized")
                out = gateway.gateway_liveness()
        self.assertIn("grokbot", out["tokens"])
        self.assertIn("last_ok_at", out["tokens"]["grokbot"])
        self.assertEqual(out["tokens"]["grokbot"]["last_error"], "401 unauthorized")

    def test_read_with_no_file_yet_is_empty_not_a_crash(self):
        from khipu import gateway

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "does-not-exist.json"
            with mock.patch.object(gateway, "_liveness_path", return_value=path):
                out = gateway.gateway_liveness()
        self.assertEqual(out["tokens"], {})


class GatewayLivenessCheckTest(unittest.TestCase):
    """integrations.gateway_liveness_check() reads the gateway's own
    /healthz over the network — doctor runs on a different Mac than the
    gateway, so this can never be a local file read."""

    def test_no_gateway_url_is_not_applicable(self):
        from khipu import integrations as integ

        with mock.patch("khipu.config.gateway_url", return_value=""):
            out = integ.gateway_liveness_check()
        self.assertTrue(out["ok"])
        self.assertFalse(out["applicable"])

    def test_recent_error_with_no_success_since_is_red(self):
        from khipu import integrations as integ

        body = {
            "gateway_liveness": {
                "tokens": {
                    "default": {
                        "last_error_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "last_error": "429 token rate limit",
                    }
                }
            }
        }
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.read.return_value = json.dumps(body).encode()
        with mock.patch("khipu.config.gateway_url", return_value="https://gw.example.test"), \
                mock.patch.object(integ, "_gateway_token", return_value="t" * 30), \
                mock.patch("urllib.request.urlopen", return_value=resp):
            out = integ.gateway_liveness_check()
        self.assertFalse(out["ok"])
        self.assertTrue(out["reasons"])

    def test_no_traffic_yet_is_green_info(self):
        from khipu import integrations as integ

        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.read.return_value = json.dumps({"gateway_liveness": {"tokens": {}}}).encode()
        with mock.patch("khipu.config.gateway_url", return_value="https://gw.example.test"), \
                mock.patch.object(integ, "_gateway_token", return_value="t" * 30), \
                mock.patch("urllib.request.urlopen", return_value=resp):
            out = integ.gateway_liveness_check()
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("note"), "no traffic recorded yet")


class UnknownHarnessTest(unittest.TestCase):
    """K8: a heartbeat file from a harness not in HARNESSES is a warning,
    never a hard red — the hook may be fine, it's just unidentified."""

    def test_recognised_harness_files_are_not_flagged(self):
        from khipu import session_capture as sc

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "claude_code.json").write_text(json.dumps({"dispatches": 5}), encoding="utf-8")
            with mock.patch.object(sc, "dispatch_dir", return_value=d):
                out = sc.unknown_harness_heartbeats()
        self.assertEqual(out["warnings"], [])

    def test_an_unrecognised_harness_file_is_warned(self):
        from khipu import session_capture as sc

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "unknown.json").write_text(
                json.dumps({"dispatches": 147, "last_error": "no transcript path"}), encoding="utf-8"
            )
            with mock.patch.object(sc, "dispatch_dir", return_value=d):
                out = sc.unknown_harness_heartbeats()
        self.assertEqual(len(out["warnings"]), 1)
        w = out["warnings"][0]
        self.assertEqual(w["harness"], "unknown")
        self.assertEqual(w["dispatches"], 147)
        self.assertEqual(w["reason"], "no transcript path")


class CapturedTodayTest(unittest.TestCase):
    """D3: a day-bucketed counter, summed across harnesses, backs Home's
    'captured today' figure."""

    def test_sums_todays_captures_across_harnesses(self):
        from datetime import datetime, timezone

        from khipu import session_capture as sc

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        beats = {
            "claude_code": {"captures_today_date": today, "captures_today": 3},
            "cursor": {"captures_today_date": "2000-01-01", "captures_today": 9},
            "codex": {},
            "aegis": {"captures_today_date": today, "captures_today": 2},
        }
        with mock.patch.object(sc, "_read_beat", side_effect=lambda h: beats.get(h, {})):
            self.assertEqual(sc.captured_today(), 5)


class QueueDrainPlistTest(unittest.TestCase):
    """K10: the Aegis capture queue had no drainer of its own."""

    def test_queue_drain_job_is_registered_and_renders(self):
        from khipu import jobs, launchd_gen

        self.assertIn("queue_drain", jobs._JOB_SPECS)
        self.assertIn("queue_drain", launchd_gen._JOB_TEMPLATE)
        data = launchd_gen.render_plist("queue_drain")
        self.assertIn(b"com.khipu.queue-drain", data)
        self.assertIn(b"drain", data)


class KhipuStatusFieldsTest(unittest.TestCase):
    """D4/R10: khipu_status surfaces snapshot_age_seconds and pending_turns
    on top of Phase 1/4's notes_freshness / search_degraded_last_24h /
    prior_work(prompt)."""

    def test_snapshot_age_and_pending_turns_are_present(self):
        from khipu import mcp_server as ms

        with mock.patch(
            "khipu.drift.status_payload",
            return_value={"counts": {"episodes": 1}, "notes_freshness": {}, "search_degraded_last_24h": 0},
        ), mock.patch(
            "khipu.hub_snapshot.snapshot_freshness", return_value={"ok": True, "age_seconds": 42}
        ), mock.patch(
            "khipu.session_capture.liveness_all",
            return_value={"harnesses": {"claude_code": {"pending_turns": 2}, "aegis": {"pending_turns": 1}}},
        ):
            out = ms._tool_status({})
        self.assertEqual(out["snapshot_age_seconds"], 42)
        self.assertEqual(out["pending_turns"], 3)


if __name__ == "__main__":
    unittest.main()
