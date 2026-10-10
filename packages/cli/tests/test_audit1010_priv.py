"""Hermetic privacy regressions for the 2026-10-10 audit."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from khipu import forget, keychain, outbox, paths, redact


def _concurrent_drain(outbox_path, mode, started, attempted, acknowledged, result):
    """Runs in a separate process so flock has real process ownership."""
    os.environ["KHIPU_OUTBOX"] = outbox_path
    from khipu import capture, embed, outbox as worker_outbox

    def write_pg(_payload):
        if mode == "fail":
            if not acknowledged.wait(30):
                raise RuntimeError("successful replay did not acknowledge")
            raise RuntimeError("retryable test failure")
        started.set()
        if not attempted.wait(30):
            raise RuntimeError("second drainer never attempted replay")
        return {"episode_inserted": False}

    capture.write_pg = write_pg
    embed.embed_on_capture = lambda _payload: None
    if mode == "fail":
        attempted.set()
    outcome = worker_outbox.drain()
    if mode == "success":
        acknowledged.set()
    result.put(outcome)


class RedactionPrivacyTest(unittest.TestCase):
    def test_quoted_json_and_yaml_secret_names_are_redacted(self):
        for text in ('"password": "QuotedSecret987654"', "'api_key': 'YamlSecret987654'"):
            with self.subTest(text=text):
                cleaned, count = redact.redact_secrets(text)
                self.assertGreater(count, 0)
                self.assertNotIn("Secret987654", cleaned)

    def test_decision_details_nested_text_is_redacted_without_losing_shape(self):
        payload = {
            "decision_details": [{
                "text": "keep matching text",
                "rationale": {"nested": "api_key: NestedSecret987654"},
                "reverses": "password: ReverseSecret987654",
            }],
        }
        redact.redact_payload(payload)
        blob = json.dumps(payload)
        for secret in ("NestedSecret987654", "ReverseSecret987654"):
            self.assertNotIn(secret, blob)
        self.assertEqual(payload["decision_details"][0]["text"], "keep matching text")

    def test_capture_sql_recording_stub_receives_redacted_structured_payload(self):
        from khipu import capture

        seen = []
        cur = mock.MagicMock()
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        payload = {"ts": "2026-10-10T00:00:00Z", "summary": "s", "decision_details": [
            {"text": "decision", "rationale": "api_key: SqlSecret987654"}
        ]}
        with mock.patch("khipu.db.connect", return_value=conn), \
             mock.patch("khipu.db.has_columns", return_value=False), \
             mock.patch("khipu.capture._inherit_project"), \
             mock.patch("khipu.capture.dedup_before_insert", return_value={"action": "new"}), \
             mock.patch("khipu.mirror._upsert_episode", side_effect=lambda _c, p: seen.append(p) or False), \
             mock.patch("khipu.topic_graph.persist_capture_graph"), \
             mock.patch("khipu.config.path_setting", return_value=None):
            capture.write_pg(payload)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("SqlSecret987654", json.dumps(seen[0]))

    def test_raw_nested_values_and_matching_decision_text_reach_sql_and_queue_redacted(self):
        from khipu import mirror

        decision = "rotate api_key: MatchingSecret987654"
        payload = {
            "ts": "2026-10-10T00:00:00Z", "summary": "s", "raw": {"nested": [
                {"value": "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"}
            ]}, "decisions": [decision], "decision_details": [{"text": decision}],
        }
        redact.redact_payload(payload)

        class RecordingCursor:
            rowcount = 1
            def execute(self, _sql, params): self.params = params

        cur = RecordingCursor()
        with mock.patch("khipu.db.has_columns", return_value=False):
            mirror._upsert_episode(cur, payload)
        sql_blob = json.dumps(cur.params, default=str)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", sql_blob)
        self.assertEqual(payload["decisions"][0], payload["decision_details"][0]["text"])
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_OUTBOX": td}):
            queued = outbox.enqueue(payload)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", queued.read_text())

    def test_claude_hook_queue_to_extract_provider_request_masks_quoted_secrets(self):
        """The hook redacts before durable queueing and the real cloud request."""
        from khipu import session_capture as sc

        class Response:
            status = 200
            def read(self):
                return json.dumps({"candidates": [{"content": {"parts": [{"text":
                    '{"summary":"safe","topics":[],"people":[],"decisions":[],"preferences":[]}'
                }]}}]}).encode("utf-8")
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "claude.jsonl"
            quoted_password = "QuotedPassword987654"
            quoted_api_key = "QuotedApiKey987654"
            transcript.write_text("\n".join(json.dumps(row) for row in [
                {"type": "user", "message": {"role": "user", "content":
                    f'{{"password": "{quoted_password}", "api_key": "{quoted_api_key}"}}'}},
                {"type": "assistant", "message": {"role": "assistant", "content":
                    "record this " + "x" * sc.MIN_CHARS}},
            ]) + "\n", encoding="utf-8")
            requests, captured = [], []
            def record_request(req, **_kwargs):
                requests.append(req.data.decode("utf-8"))
                return Response()
            env = {"hook_event_name": "SessionEnd", "session_id": "redact-route",
                   "cwd": td, "transcript_path": str(transcript)}
            with mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(root / "capture"),
                                               "CODEX_HOME": "", "CODEX_SANDBOX": ""}), \
                 mock.patch("khipu.models.synth_settings", return_value={"provider": "cloud"}), \
                 mock.patch("khipu.models.cloud_model_id", return_value="recording-model"), \
                 mock.patch("khipu.extract._key", return_value="test-key"), \
                 mock.patch("khipu.extract.urllib.request.urlopen", side_effect=record_request), \
                 mock.patch("khipu.capture.capture", side_effect=lambda payload, **_kw: captured.append(payload) or 0), \
                 mock.patch("khipu.config.capture_mode", return_value="hub"), \
                 mock.patch("khipu.hub_snapshot.sync_decision_changes", return_value={"ok": True}):
                queued = sc.hook_main(json.dumps(env), harness="claude_code")
                drained = sc.drain()
            self.assertTrue(queued["due"], queued)
            self.assertEqual(drained["captured"], 1)
            self.assertEqual(len(requests), 1)
            self.assertEqual(len(captured), 1)
            provider_body = requests[0]
            self.assertNotIn(quoted_password, provider_body)
            self.assertNotIn(quoted_api_key, provider_body)
            self.assertIn(redact.MASK, provider_body)


class OutboxPrivacyTest(unittest.TestCase):
    def test_quarantined_jobs_keep_the_existing_dead_work_health_signal_red(self):
        (Path(self.tmp.name) / "broken.json").write_bytes(b"null")
        with mock.patch("khipu.capture.write_pg"), mock.patch("khipu.embed.embed_on_capture"):
            outbox.drain()
        result = outbox.status()
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["dead"], 1)

    def test_naive_queue_timestamp_is_unreadable_without_aborting_status(self):
        (Path(self.tmp.name) / "naive.json").write_text(json.dumps({
            "queued_at": "2026-10-10T00:00:00", "payload": {}, "attempts": 0,
        }))
        result = outbox.status()
        self.assertEqual(result["unreadable"], 1)
        self.assertEqual(result["pending"], 1)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"KHIPU_OUTBOX": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_hostile_timestamp_is_contained(self):
        path = outbox.enqueue({"ts": "../../escape", "summary": "safe"})
        self.assertTrue(path.is_relative_to(Path(self.tmp.name)))
        self.assertEqual(len(outbox.jobs()), 1)

    def test_invalid_timestamp_identity_is_bounded_and_contains_nonascii_paths(self):
        path = outbox.enqueue({"ts": "/绝对/../../" + "x" * 1000, "summary": "safe"})
        self.assertTrue(path.is_relative_to(Path(self.tmp.name)))
        self.assertLessEqual(len(path.name), 100)

    def test_bad_envelopes_are_quarantined_and_healthy_job_replays(self):
        root = Path(self.tmp.name)
        raw = b'{"payload": null, "attempts": "not-a-number"}'
        (root / "bad.json").write_bytes(raw)
        outbox.enqueue({"ts": "2026-10-10T00:00:00Z", "summary": "healthy"})
        with mock.patch("khipu.capture.write_pg", return_value={"episode_inserted": False}), \
             mock.patch("khipu.embed.embed_on_capture"):
            result = outbox.drain()
        self.assertEqual(result["replayed"], 1)
        self.assertEqual(result["quarantined"], 1)
        self.assertFalse((root / "bad.json").exists())
        quarantined = list((root / "quarantine").glob("*.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), raw)
        self.assertEqual(outbox.status()["pending"], 0)

    def test_all_bad_envelope_shapes_quarantine_and_retry_dead_stays_healthy(self):
        root = Path(self.tmp.name)
        for name, raw in {
            "broken": b"{", "missing": b"{}", "null": b'{"payload": null}',
            "list": b'{"payload": []}',
        }.items():
            (root / f"{name}.json").write_bytes(raw)
        dead = root / "dead"
        dead.mkdir()
        (dead / "dead-list.json").write_bytes(b'{"payload": []}')
        outbox.enqueue({"ts": "2026-10-10T00:00:01Z", "summary": "healthy later"})
        with mock.patch("khipu.capture.write_pg", return_value={"episode_inserted": False}), \
             mock.patch("khipu.embed.embed_on_capture"):
            drained = outbox.drain()
        retried = outbox.retry_dead()
        self.assertEqual((drained["quarantined"], drained["replayed"]), (4, 1))
        self.assertEqual(retried["quarantined"], 1)
        self.assertEqual(outbox.status()["pending"], 0)

    def test_concurrent_failed_and_successful_drains_cannot_resurrect_acknowledged_job(self):
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("requires process-owned flock")
        outbox.enqueue({"ts": "2026-10-10T00:00:00Z", "summary": "race"})
        ctx = multiprocessing.get_context("fork")
        started, attempted, acknowledged = ctx.Event(), ctx.Event(), ctx.Event()
        results = ctx.Queue()
        first = ctx.Process(target=_concurrent_drain,
                            args=(self.tmp.name, "success", started, attempted, acknowledged, results))
        second = ctx.Process(target=_concurrent_drain,
                             args=(self.tmp.name, "fail", started, attempted, acknowledged, results))
        try:
            first.start()
            self.assertTrue(started.wait(30))
            second.start()
            first.join(30)
            second.join(30)
            self.assertEqual((first.exitcode, second.exitcode), (0, 0))
            self.assertTrue(acknowledged.is_set())
            self.assertEqual(outbox.status()["pending"], 0)
            self.assertEqual(outbox.status()["dead"], 0)
        finally:
            for process in (first, second):
                if process.is_alive():
                    process.terminate()
                    process.join(5)


class KeychainPrivacyTest(unittest.TestCase):
    def test_failed_upsert_does_not_delete_an_existing_credential(self):
        calls = []

        def fake_security(*args, **_kwargs):
            calls.append(args[0])
            return mock.Mock(returncode=1, stdout="", stderr="denied")

        with mock.patch.object(keychain, "keychain_available", return_value=True), \
             mock.patch.object(keychain, "_security", side_effect=fake_security):
            with self.assertRaises(RuntimeError):
                keychain.set_password("account", "new-value")
        self.assertEqual(calls, ["add-generic-password"])

    def test_failed_upsert_preserves_stateful_existing_credential_for_followup_read(self):
        stored = {"account": "old-value"}

        def fake_security(command, *args, **_kwargs):
            account = args[args.index("-a") + 1]
            if command == "find-generic-password":
                value = stored.get(account)
                return mock.Mock(returncode=0 if value else 44, stdout=(value or "") + "\n", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="denied")

        with mock.patch.object(keychain, "keychain_available", return_value=True), \
             mock.patch.object(keychain, "_security", side_effect=fake_security):
            with self.assertRaises(RuntimeError):
                keychain.set_password("account", "replacement")
            self.assertEqual(keychain.get_password("account"), "old-value")

    def test_invalid_utf8_dsn_is_honest_status_not_a_crash(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "dsn").write_bytes(b"\xff\xfe")
            with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data)}), \
                 mock.patch.object(keychain, "get_dsn", return_value=None), \
                 mock.patch.object(keychain, "get_gemini_key", return_value=None), \
                 mock.patch.object(keychain, "get_openai_compat_key", return_value=None), \
                 mock.patch.object(keychain, "get_voyage_key", return_value=None):
                result = keychain.secrets_status()
        self.assertFalse(result["dsn_file"]["ok"])
        self.assertIn("UnicodeDecodeError", result["dsn_file"]["reasons"][0])


class PathsPrivacyTest(unittest.TestCase):
    def test_sensitive_archive_and_restored_dsn_are_private_under_umask_022(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.mkdir()
            (source / "dsn").write_text("postgres://private")
            with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(source)}):
                old = os.umask(0o022)
                try:
                    archive = Path(paths.backup_local(dest=root / "backup.zip")["archive"])
                finally:
                    os.umask(old)
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            restored = root / "restored"
            restored.mkdir()
            with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(restored)}):
                old = os.umask(0o022)
                try:
                    paths.import_local(source=archive)
                finally:
                    os.umask(old)
            self.assertEqual(stat.S_IMODE((restored / "dsn").stat().st_mode), 0o600)

    def test_directory_and_zip_gateway_token_restore_use_sensitive_writer_before_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.mkdir()
            (source / "dsn").write_text("postgres://private")
            (source / "gateway-token").write_text("bearer")
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(source / "dsn", "dsn")
                zf.write(source / "gateway-token", "gateway-token")
            for incoming in (source, archive):
                target = root / f"target-{incoming.suffix or 'dir'}"
                target.mkdir()
                observed = []
                original = paths.copy_sensitive
                def spy(reader, destination):
                    observed.append(destination.name)
                    return original(reader, destination)
                with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(target)}), \
                     mock.patch.object(paths, "copy_sensitive", side_effect=spy):
                    old = os.umask(0o022)
                    try:
                        paths.import_local(source=incoming)
                    finally:
                        os.umask(old)
                self.assertEqual(set(observed), {"dsn", "gateway-token"})
                self.assertEqual(stat.S_IMODE((target / "gateway-token").stat().st_mode), 0o600)

    def test_gateway_token_is_created_private_before_its_bytes_are_written(self):
        from khipu import integrations

        with tempfile.TemporaryDirectory() as td:
            token_path = Path(td) / "gateway-token"
            observed = []
            original_write = integrations.os.write

            def write_spy(fd, data):
                observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
                return original_write(fd, data)

            with mock.patch("khipu.integrations.gateway_token_file", return_value=token_path), \
                 mock.patch.object(integrations.os, "write", side_effect=write_spy):
                old = os.umask(0o022)
                try:
                    integrations.set_gateway_token("GatewaySecret987654")
                finally:
                    os.umask(old)
            self.assertEqual(observed, [0o600])
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)


class ForgetPrivacyTest(unittest.TestCase):
    def test_absent_unconfigured_stores_are_complete_but_existing_failed_replica_is_partial(self):
        ts = datetime(2026, 10, 10, tzinfo=timezone.utc)
        cur = mock.Mock()
        cur.fetchone.return_value = (ts, "forget me", "codex:session")
        cur.rowcount = 1
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        with tempfile.TemporaryDirectory() as td:
            replica = Path(td) / "missing.sqlite"
            with mock.patch("khipu.db.connect", return_value=conn), \
                 mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot", return_value={
                     "ok": False, "error": "hub snapshot missing",
                 }), \
                 mock.patch("khipu.hub_snapshot.snapshot_path", return_value=replica), \
                 mock.patch("khipu.config.path_setting", return_value=None), \
                 mock.patch("khipu.forget.forget_from_outbox", return_value={"ok": True, "removed": 0}):
                absent = forget.forget_everywhere(1)
                replica.write_bytes(b"private replica content")
                existing = forget.forget_everywhere(1)
        self.assertTrue(absent["ok"])
        self.assertTrue(absent["complete"])
        self.assertFalse(existing["ok"])
        self.assertIn("snapshot", existing["partial"])
        self.assertIn("snapshot", existing["error"])

    def test_unidentifiable_legacy_records_withhold_complete_forget(self):
        ts = datetime(2026, 10, 10, tzinfo=timezone.utc)
        cur = mock.Mock()
        cur.fetchone.return_value = (ts, "forget me", "codex:session")
        cur.rowcount = 1
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "episodes.jsonl").write_text('{"summary":"unidentified private text"')
            with mock.patch("khipu.db.connect", return_value=conn), \
                 mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot", return_value={"ok": True}), \
                 mock.patch("khipu.forget.forget_from_outbox", return_value={"ok": True, "removed": 0}):
                result = forget.forget_everywhere(1, memory_root=root)
        self.assertFalse(result["complete"])
        self.assertFalse(result["ok"])
        self.assertIn("legacy_file", result["partial"])

    def test_backup_is_sanitized_and_outbox_copies_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gone = {"ts": "2026-10-10T00:00:00Z", "summary": "forget me"}
            keep = {"ts": "2026-10-10T00:01:00Z", "summary": "keep me"}
            (root / "episodes.jsonl").write_text(json.dumps(gone) + "\n" + json.dumps(keep) + "\n")
            with mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(root / "outbox")}):
                outbox.enqueue(gone)
                result = forget.forget_in_legacy_file(
                    root, gone["ts"], hashlib.md5(gone["summary"].encode()).hexdigest()
                )
                erased = forget.forget_from_outbox(gone["ts"], result["identity"]["summary_md5"])
            self.assertNotIn("forget me", Path(result["backup"]).read_text())
            self.assertEqual(erased["removed"], 1)
            self.assertEqual(outbox.status()["pending"], 0)

    def test_old_backups_and_all_outbox_stores_are_scrubbed_or_reported_partial(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gone = {"ts": "2026-10-10T00:00:00Z", "summary": "forget me"}
            backup_dir = root / ".khipu-forget-backups"
            backup_dir.mkdir()
            old = backup_dir / "episodes.jsonl.old"
            old.write_text(json.dumps(gone) + "\n")
            digest = hashlib.md5(b"forget me").hexdigest()
            with mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(root / "outbox")}):
                outbox.enqueue(gone)
                outbox.dead_dir().mkdir(parents=True)
                (outbox.dead_dir() / "dead.json").write_text(json.dumps({"payload": gone, "attempts": 0}))
                outbox.quarantine_dir().mkdir(parents=True)
                (outbox.quarantine_dir() / "quarantine.json").write_text(json.dumps({"payload": gone}))
                # There is no active legacy log: old retained copies still
                # need erasure when the hub identity is forgotten.
                forget.forget_in_legacy_file(root, gone["ts"], digest)
                erased = forget.forget_from_outbox(gone["ts"], digest)
            self.assertNotIn("forget me", old.read_text())
            self.assertEqual(erased["removed"], 3)

    def test_raw_original_identity_is_retained_for_edited_summary_cleanup(self):
        ts = datetime(2026, 10, 10, tzinfo=timezone.utc)

        class Cur:
            rowcount = 1

            def execute(self, sql, _params=None):
                self.sql = sql

            def fetchone(self):
                if "SELECT raw" in self.sql:
                    return ({"ts": "2026-10-10T00:00:00Z", "summary": "original"},)
                return (ts, "edited", "codex:session")

        with mock.patch.object(forget, "_deliverables_ready", return_value=False), \
             mock.patch.object(forget, "_decision_evidence_ready", return_value=False), \
             mock.patch.object(forget, "_briefs_ready", return_value=False):
            result = forget.forget_episode(Cur(), 1)
        hashes = {item["summary_md5"] for item in result["legacy_identities"]}
        self.assertEqual(hashes, {
            hashlib.md5(b"edited").hexdigest(), hashlib.md5(b"original").hexdigest(),
        })

    def test_forget_everywhere_erases_edited_and_original_legacy_and_outbox_copies(self):
        ts = datetime(2026, 10, 10, tzinfo=timezone.utc)

        class Cur:
            rowcount = 1
            sql = ""
            def execute(self, sql, _params=None): self.sql = sql
            def fetchone(self):
                if "SELECT raw" in self.sql:
                    return ({"ts": "2026-10-10T00:00:00Z", "summary": "original"},)
                return (ts, "edited", "codex:session")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = {"ts": "2026-10-10T00:00:00Z", "summary": "original"}
            edited = {"ts": "2026-10-10T00:00:00+00:00", "summary": "edited"}
            (root / "episodes.jsonl").write_text(json.dumps(original) + "\n" + json.dumps(edited) + "\n")
            conn = mock.MagicMock()
            conn.__enter__.return_value = conn
            conn.cursor.return_value.__enter__.return_value = Cur()
            with mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(root / "outbox")}):
                outbox.enqueue(original)
                outbox.enqueue(edited)
                with mock.patch("khipu.db.connect", return_value=conn), \
                     mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot", return_value={"ok": True}), \
                     mock.patch("khipu.forget.time.strftime", side_effect=["first", "second"]):
                    result = forget.forget_everywhere(1, memory_root=root)
            self.assertTrue(result["complete"])
            self.assertEqual(outbox.status()["pending"], 0)
            self.assertEqual((root / "episodes.jsonl").read_text(), "")
            retained = "".join(p.read_text() for p in (root / ".khipu-forget-backups").iterdir())
            self.assertNotIn("original", retained)
            self.assertNotIn("edited", retained)

    def test_snapshot_or_unconfigured_legacy_makes_completion_partial(self):
        ts = datetime(2026, 10, 10, tzinfo=timezone.utc)
        cur = mock.Mock()
        cur.fetchone.return_value = (ts, "forget me", "codex:session")
        cur.rowcount = 1
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        with mock.patch("khipu.db.connect", return_value=conn), \
             mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot", return_value={"ok": False, "error": "locked"}), \
             mock.patch("khipu.config.path_setting", side_effect=RuntimeError("no root")), \
             mock.patch("khipu.forget.forget_from_outbox", return_value={"ok": True, "removed": 0}):
            result = forget.forget_everywhere(1)
        self.assertFalse(result["ok"])
        self.assertFalse(result["complete"])
        self.assertIn("snapshot", result["partial"])
        self.assertIn("legacy_file", result["partial"])

