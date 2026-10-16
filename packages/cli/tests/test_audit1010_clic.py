"""Hermetic regressions for the audit1010 CLI repair slice."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from khipu import config, join, jobs, launchd_gen, setup, sources


class SourceBoundaryRegressionTest(unittest.TestCase):
    def test_code_root_does_not_claim_a_sibling_prefix(self) -> None:
        doc = {
            "sources": [
                {"id": "code:app", "kind": "code_ast", "root": "/code/app"},
            ]
        }
        self.assertIsNone(sources._code_source_id_for_path("/code/app-backup/main.py", doc))
        self.assertEqual(
            sources._code_source_id_for_path("/code/app/main.py", doc), "code:app"
        )


class CorruptDocumentMutationRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.dir)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_config_setter_preserves_invalid_existing_bytes(self) -> None:
        path = config.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = b"{ this is corrupt"
        path.write_bytes(raw)
        with self.assertRaises(ValueError):
            config.set_capture_mode("hub")
        self.assertEqual(path.read_bytes(), raw)

    def test_sources_setter_preserves_wrong_shape_existing_bytes(self) -> None:
        path = sources.sources_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = b'{"sources": {}}'
        path.write_bytes(raw)
        with self.assertRaises(ValueError):
            sources.add_code_root(self.dir / "code")
        self.assertEqual(path.read_bytes(), raw)


class JoinConninfoRegressionTest(unittest.TestCase):
    def test_keyword_conninfo_retargets_cert_and_refuses_localhost(self) -> None:
        dsn = "host=remote.example dbname=khipu user=alice sslmode=verify-full sslrootcert=/old/root.crt"
        rewritten = join.rewrite_dsn_sslrootcert(dsn, "/tmp/new root.crt")
        from psycopg.conninfo import conninfo_to_dict

        params = conninfo_to_dict(rewritten)
        self.assertEqual(params["host"], "remote.example")
        self.assertEqual(params["sslrootcert"], str(Path("/tmp/new root.crt").resolve()))
        self.assertTrue(join.dsn_requires_sslrootcert(rewritten))
        self.assertTrue(join.is_localhost_dsn("host=localhost dbname=khipu"))


class EmbedBackfillRegressionTest(unittest.TestCase):
    def test_failed_or_budget_exhausted_embed_is_not_healthy(self) -> None:
        with (
            mock.patch("khipu.embed.backfill", return_value={"failed_chunks": 1}),
            mock.patch.object(jobs, "_library_backfill", return_value=None),
            mock.patch.object(jobs, "_nightly_log"),
        ):
            self.assertFalse(jobs._embed_backfill()["ok"])
        with (
            mock.patch("khipu.embed.backfill", return_value={"time_budget_exhausted": True}),
            mock.patch.object(jobs, "_library_backfill", return_value=None),
            mock.patch.object(jobs, "_nightly_log"),
        ):
            self.assertFalse(jobs._embed_backfill()["ok"])

    def test_drain_failure_overrides_and_persists_driver_success(self) -> None:
        with (
            mock.patch.object(jobs, "_drain_sessions", return_value={"ok": False, "failed": 1}),
            mock.patch.object(jobs, "_run_script", return_value=0),
            mock.patch.object(jobs, "_record_nightly_step"),
            mock.patch.object(jobs, "_reconcile_notes_if_due", return_value={"ok": True}),
            mock.patch.object(jobs, "_embed_backfill", return_value={"ok": True}),
            mock.patch.object(jobs, "_prune_query_cache", return_value={"ok": True}),
            mock.patch.object(jobs, "_mark_stale_commitments", return_value={"ok": True}),
            mock.patch.object(jobs, "_hygiene_commitments", return_value={"ok": True}),
            mock.patch.object(jobs, "_briefs_build_if_on", return_value=None),
            mock.patch.object(jobs, "_write_job_state") as write_state,
        ):
            self.assertEqual(jobs.run_nightly(), 1)
        write_state.assert_called_once_with("nightly", 1)


class EmbedProviderIntegrationRegressionTest(unittest.TestCase):
    def test_real_backfill_provider_failure_makes_nightly_producer_unhealthy(self) -> None:
        from khipu import embed
        from tests.test_embed import _BackfillConn, _BackfillCursor

        cur = _BackfillCursor([("note:a", "Title", "short body")])
        conn = _BackfillConn(cur)
        with (
            mock.patch("khipu.db.connect", return_value=conn),
            mock.patch.object(
                embed,
                "embed_batch",
                side_effect=RuntimeError("provider unavailable"),
            ),
            mock.patch.object(embed.time, "sleep", lambda _seconds: None),
            mock.patch.object(jobs, "_library_backfill", return_value=None),
            mock.patch.object(jobs, "_nightly_log"),
        ):
            out = jobs._embed_backfill()
        self.assertEqual(out["failed_chunks"], 1)
        self.assertFalse(out["ok"])

    def test_drain_failure_return_and_persisted_receipt_agree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(jobs, "ensure_data_dir", return_value=Path(tmp)),
                mock.patch.object(jobs, "_drain_sessions", return_value={"ok": False, "failed": 1}),
                mock.patch.object(jobs, "_run_script", return_value=0),
                mock.patch.object(jobs, "_reconcile_notes_if_due", return_value={"ok": True}),
                mock.patch.object(jobs, "_embed_backfill", return_value={"ok": True}),
                mock.patch.object(jobs, "_prune_query_cache", return_value={"ok": True}),
                mock.patch.object(jobs, "_mark_stale_commitments", return_value={"ok": True}),
                mock.patch.object(jobs, "_hygiene_commitments", return_value={"ok": True}),
                mock.patch.object(jobs, "_briefs_build_if_on", return_value=None),
            ):
                rc = jobs.run_nightly()
                state = jobs._read_job_state("nightly")
        self.assertEqual(rc, 1)
        self.assertEqual(state["exit"], rc)


class SetupPreflightRegressionTest(unittest.TestCase):
    def test_no_store_no_jobs_no_prove_uses_read_only_stage_modes(self) -> None:
        conn = mock.MagicMock()
        conn.close = mock.Mock()
        stage = {"id": "stage", "ok": True, "title": "ok", "detail": "ok"}
        with (
            mock.patch.object(setup, "_connect", return_value=conn),
            mock.patch.object(setup, "_stage_version", return_value=stage),
            mock.patch.object(setup, "_stage_privileges", return_value=stage) as privileges,
            mock.patch.object(setup, "_stage_schema", return_value=stage) as schema,
            mock.patch.object(setup, "_stage_graph", return_value=stage),
            mock.patch.object(setup, "_stage_summary", return_value=(stage, {})),
        ):
            out = setup.connect_database(
                "host=remote.example dbname=khipu", store=False, install_jobs=False, prove=False
            )
        self.assertTrue(out["ok"])
        privileges.assert_called_once_with(conn, read_only=True)
        schema.assert_called_once_with(conn, read_only=True)


class LaunchdXmlRegressionTest(unittest.TestCase):
    def test_every_job_parses_with_metacharacter_paths(self) -> None:
        import plistlib

        special = "/tmp/a&b<quote>'double\"/path"
        context = {
            "KHIPU_ROOT": special,
            "KHIPU_PYTHON": special,
            "PYTHONPATH": special,
            "WORKING_DIRECTORY": special,
            "PYTHONPYCACHEPREFIX": special,
        }
        with (
            mock.patch.object(launchd_gen, "render_context", return_value=context),
            mock.patch.object(launchd_gen, "_log_paths", return_value=(Path(special), Path(special))),
        ):
            for job in launchd_gen._JOB_TEMPLATE:
                with self.subTest(job=job):
                    plistlib.loads(launchd_gen.render_plist(job))


class JoinPrivateOutputRegressionTest(unittest.TestCase):
    def test_private_writer_replaces_existing_file_owner_only(self) -> None:
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "secret"
            dest.write_bytes(b"old")
            os.chmod(dest, 0o644)
            join.write_private_file(dest, b"new")
            self.assertEqual(dest.read_bytes(), b"new")
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_private_writer_has_no_permissive_window_and_failure_preserves_old_bytes(self) -> None:
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "secret"
            dest.write_bytes(b"old")
            old_umask = os.umask(0o022)
            try:
                with mock.patch.object(join.os, "replace", side_effect=OSError("injected")):
                    with self.assertRaises(OSError):
                        join.write_private_file(dest, b"new")
            finally:
                os.umask(old_umask)
            self.assertEqual(dest.read_bytes(), b"old")
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o644)
            self.assertEqual(list(Path(tmp).glob(".*.tmp")), [])

    def test_setup_stores_same_private_dsn_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dsn"
            with (
                mock.patch("khipu.keychain.set_dsn"),
                mock.patch("khipu.paths.dsn_file", return_value=path),
            ):
                out = setup._stage_store("host=remote.example dbname=khipu", None)
            self.assertTrue(out["ok"])
            self.assertEqual(path.read_text(), "host=remote.example dbname=khipu\n")


class JoinImportPrevalidationRegressionTest(unittest.TestCase):
    def test_invalid_capture_mode_fails_before_any_credential_setter(self) -> None:
        payload = {"format": join.FORMAT_VERSION, "database_url": "host=remote.example dbname=khipu", "capture_mode": "bogus"}
        blob = json.dumps(payload).encode("utf-8")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("khipu.paths.ensure_data_dir", return_value=Path(tmp)),
            mock.patch("khipu.paths.root_cert_file", return_value=Path("/no/such/cert")),
            mock.patch("khipu.keychain.set_dsn") as set_dsn,
        ):
            with self.assertRaises(ValueError):
                join.import_kit(blob, "")
            self.assertEqual(os.listdir(tmp), [".memory.lock"])
        set_dsn.assert_not_called()


class LaunchdUninstallRegressionTest(unittest.TestCase):
    def test_uninstall_attempts_label_unload_when_plist_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp)
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            absent = mock.Mock(returncode=1, stdout="", stderr="Could not find service")
            with (
                mock.patch.object(launchd_gen, "_launchagents_dir", return_value=agents),
                mock.patch.object(launchd_gen, "read_versions", return_value={}),
                mock.patch.object(launchd_gen, "write_versions"),
                mock.patch.object(launchd_gen.subprocess, "run", side_effect=[completed, absent]) as run,
            ):
                out = launchd_gen.uninstall_job("nightly")
            self.assertTrue(out["ok"])
            self.assertIn("gui/", " ".join(run.call_args_list[0].args[0]))


class LaunchdDriverRegressionTest(unittest.TestCase):
    def test_nightly_refuses_schedule_without_a_real_driver(self) -> None:
        with mock.patch.object(launchd_gen, "_launchctl_load") as load:
            out = launchd_gen.install_job("nightly", environ={})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "required_driver_unavailable")
        load.assert_not_called()


class PairReadinessRegressionTest(unittest.TestCase):
    def test_export_failure_emits_no_advertising_banner(self) -> None:
        with (
            mock.patch.object(__import__("khipu.join_pair", fromlist=["advertise"]), "advertise", side_effect=RuntimeError("export failed")),
            mock.patch("builtins.print") as printed,
        ):
            with self.assertRaises(RuntimeError):
                __import__("khipu.join_pair", fromlist=["advertise_join_kit"]).advertise_join_kit("")
        printed.assert_not_called()


class PairDeadlineRegressionTest(unittest.TestCase):
    def test_browse_partial_line_cannot_defeat_deadline(self) -> None:
        from khipu import join_pair

        read_fd, write_fd = os.pipe()
        stdout = os.fdopen(read_fd, "r", encoding="utf-8")
        os.write(write_fd, b"partial without newline")
        proc = mock.Mock(stdout=stdout)
        result: list[object] = []
        thread = threading.Thread(
            target=lambda: result.append(join_pair._browse_service(timeout_s=0.05)),
            daemon=True,
        )
        try:
            with mock.patch.object(join_pair.subprocess, "Popen", return_value=proc):
                thread.start()
                thread.join(0.3)
            self.assertFalse(thread.is_alive(), "partial Bonjour output blocked past deadline")
            self.assertEqual(result, [[]])
        finally:
            os.close(write_fd)
            stdout.close()

    def test_accepted_socket_is_bounded_before_tls_handshake(self) -> None:
        from khipu import join_pair

        client = mock.Mock()
        server = mock.Mock()
        server.accept.side_effect = [(client, ("127.0.0.1", 1)), socket.timeout()]
        context = mock.Mock()
        context.wrap_socket.side_effect = socket.timeout("stalled TLS")
        dns = mock.Mock()
        dns.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            (cert_dir / "cert.pem").write_text("cert")
            (cert_dir / "key.pem").write_text("key")
            with (
                mock.patch("khipu.join.export_kit", return_value=b"kit"),
                mock.patch.object(join_pair, "_self_signed_cert_dir", return_value=cert_dir),
                mock.patch.object(join_pair.ssl, "SSLContext", return_value=context),
                mock.patch.object(join_pair.socket, "socket", return_value=server),
                mock.patch.object(join_pair.subprocess, "Popen", return_value=dns),
                mock.patch.object(join_pair, "lan_ipv4", return_value=None),
                mock.patch.object(join_pair.time, "time", side_effect=[0, 0, 31, 31]),
            ):
                join_pair.advertise("", timeout_s=1, pin="123456")
        client.settimeout.assert_called()

    def test_dead_bonjour_process_never_announces_ready(self) -> None:
        from khipu import join_pair

        server = mock.Mock()
        dns = mock.Mock()
        dns.poll.return_value = 1
        ready = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            (cert_dir / "cert.pem").write_text("cert")
            (cert_dir / "key.pem").write_text("key")
            with (
                mock.patch("khipu.join.export_kit", return_value=b"kit"),
                mock.patch.object(join_pair, "_self_signed_cert_dir", return_value=cert_dir),
                mock.patch.object(join_pair.ssl, "SSLContext", return_value=mock.Mock()),
                mock.patch.object(join_pair.socket, "socket", return_value=server),
                mock.patch.object(join_pair.subprocess, "Popen", return_value=dns),
                mock.patch.object(join_pair, "lan_ipv4", return_value=None),
            ):
                with self.assertRaises(RuntimeError):
                    join_pair.advertise("", timeout_s=1, pin="123456", on_ready=ready)
        ready.assert_not_called()


class NightlyHealthRegressionTest(unittest.TestCase):
    def test_malformed_receipt_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "nightly-last.json"
            receipt.write_text("{broken")
            with (
                mock.patch.object(jobs, "_nightly_last_path", return_value=receipt),
                mock.patch.object(jobs, "_is_index_sync_host", return_value=True),
                mock.patch.object(jobs, "_job_entry", return_value={"plist_loaded": True}),
            ):
                health = jobs.nightly_step_health()
        self.assertTrue(any(not row["ok"] for row in health.values()))
        self.assertTrue(any("malformed" in str(row.get("error")) for row in health.values()))

    def test_scheduled_overdue_missing_receipt_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nightly-last.json"
            old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            with (
                mock.patch.object(jobs, "_nightly_last_path", return_value=missing),
                mock.patch.object(jobs, "_is_index_sync_host", return_value=True),
                mock.patch.object(
                    jobs,
                    "_job_entry",
                    return_value={"plist_loaded": True, "last_run_iso": old, "last_exit": 0, "last_exit_ts": old},
                ),
            ):
                health = jobs.nightly_step_health()
        self.assertTrue(any(not row["ok"] for row in health.values()))

    def test_invalid_success_timestamp_and_failed_exit_are_unhealthy(self) -> None:
        receipt = {
            "steps": [
                {"name": name, "ok": True, "ts": "not-a-time"}
                for name, _fix in jobs._NIGHTLY_STEP_CHECKS.values()
            ]
        }
        with (
            mock.patch.object(jobs, "nightly_last", return_value=receipt),
            mock.patch.object(jobs, "_is_index_sync_host", return_value=True),
            mock.patch.object(
                jobs,
                "_job_entry",
                return_value={"plist_loaded": True, "last_exit": 7, "last_exit_ts": datetime.now(timezone.utc).isoformat()},
            ),
        ):
            health = jobs.nightly_step_health()
        self.assertFalse(health["nightly_job_ok"]["ok"])
        self.assertTrue(any(not row["ok"] for key, row in health.items() if key != "nightly_job_ok"))


class NightlyAggregateRegressionTest(unittest.TestCase):
    def test_rv4_07_failed_brief_reaches_nightly_state_and_health(self) -> None:
        from khipu import briefs
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(jobs, "ensure_data_dir", return_value=Path(tmp)), \
                    mock.patch.object(jobs, "_drain_sessions", return_value={"ok": True}), \
                    mock.patch.object(jobs, "_run_script", return_value=0), \
                    mock.patch.object(jobs, "_reconcile_notes_if_due", return_value={"ok": True}), \
                    mock.patch.object(jobs, "_embed_backfill", return_value={"ok": True}), \
                    mock.patch.object(jobs, "_prune_query_cache", return_value={"ok": True}), \
                    mock.patch.object(jobs, "_mark_stale_commitments", return_value={"ok": True}), \
                    mock.patch.object(jobs, "_hygiene_commitments", return_value={"ok": True}), \
                    mock.patch("khipu.features.enabled", return_value=True), \
                    mock.patch("khipu.db.connect", return_value=mock.MagicMock()), \
                    mock.patch.object(briefs, "acquire_build_lock", return_value=True), \
                    mock.patch.object(briefs, "release_build_lock"), \
                    mock.patch.object(briefs, "select_for_build", return_value={"available": True, "topics": ["a"]}), \
                    mock.patch.object(briefs, "build_many", return_value=[{"status": "failed", "error": "provider"}]), \
                    mock.patch.object(jobs, "_nightly_log"):
                rc = jobs.run_nightly()
                self.assertEqual(rc, 1)
                self.assertEqual(jobs._read_job_state("nightly")["exit"], 1)
                with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                        mock.patch.object(jobs, "_job_entry", return_value={"last_exit": rc,
                            "last_exit_ts": datetime.now(timezone.utc).isoformat(), "plist_loaded": True}):
                    health = jobs.nightly_step_health()
                self.assertFalse(health["briefs_build_ok"]["ok"])
                self.assertTrue(health["briefs_build_ok"]["applicable"])

    def test_rv4_07_disabled_briefs_do_not_gate_health(self) -> None:
        with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                mock.patch.object(jobs, "_job_entry", return_value={}), \
                mock.patch.object(jobs, "nightly_last", return_value={"steps": [
                    {"name": "briefs_build", "ok": False, "error": "old provider failure"}]}), \
                mock.patch("khipu.features.enabled", return_value=False):
            health = jobs.nightly_step_health()
        self.assertTrue(health["briefs_build_ok"]["ok"])
        self.assertFalse(health["briefs_build_ok"]["applicable"])

    def test_briefs_error_or_unavailable_result_is_unhealthy(self) -> None:
        # A provider or table error for a topic is status "error"/"unavailable".
        # A recorded per-topic "failed" brief beside built ones is not a failed
        # night (tests/test_jobs_briefs.py covers that side).
        from khipu import briefs, features

        chosen = {"available": True, "topics": ["a", "b"]}
        results = [
            {"status": "ok"},
            {"status": "error", "error": "provider"},
        ]
        conn = mock.MagicMock()
        with (
            mock.patch.object(briefs, "acquire_build_lock", return_value=True),
            mock.patch.object(briefs, "release_build_lock"),
            mock.patch.object(briefs, "select_for_build", return_value=chosen),
            mock.patch.object(briefs, "build_many", return_value=results),
            mock.patch.object(features, "enabled", return_value=True),
            mock.patch("khipu.db.connect") as connect,
            mock.patch.object(jobs, "_nightly_log"),
        ):
            connect.return_value.__enter__.return_value = conn
            out = jobs._briefs_build_if_on()
        self.assertFalse(out["ok"])

    def test_library_nested_failure_overrides_top_level_ok(self) -> None:
        fake_library = mock.Mock()
        fake_library.nightly.return_value = {
            "ok": True,
            "libraries": 1,
            "results": [{"name": "docs", "failed": 1, "remaining": 2}],
        }
        with (
            mock.patch("khipu.db.dsn_configured", return_value=True),
            mock.patch.dict("sys.modules", {"khipu.library": fake_library}),
            mock.patch.object(jobs, "_nightly_log"),
        ):
            out = jobs._library_backfill()
        self.assertFalse(out["ok"])


class JoinTransactionRegressionTest(unittest.TestCase):
    def test_rv4_02_rollback_failure_is_reported(self) -> None:
        from khipu import keychain
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": tmp}), \
                mock.patch.object(keychain, "keychain_available", return_value=True), \
                mock.patch.object(keychain, "_security", return_value=mock.Mock(returncode=0, stdout="old dsn")), \
                mock.patch.object(keychain, "set_dsn"), \
                mock.patch.object(keychain, "set_password", side_effect=RuntimeError("locked")), \
                mock.patch("khipu.models.set_models_replace", side_effect=RuntimeError("late failure")):
            with self.assertRaisesRegex(RuntimeError, "rollback failed for credential database_url") as caught:
                join.import_kit(self._payload(), "")
            self.assertEqual(str(caught.exception.__cause__), "late failure")

    def test_rv4_02_late_failure_restores_keychain_and_files(self) -> None:
        from khipu import keychain
        for existing in (True, False):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "dsn").write_text("host=old.example dbname=khipu\n")
                (root / "root.crt").write_text("old cert\n")
                (root / "config.json").write_text('{"capture_mode":"dual"}\n')
                original = {keychain.DSN_ACCOUNT: "host=old.example dbname=khipu",
                            keychain.GEMINI_ACCOUNT: "old-gemini"} if existing else {}
                stored = dict(original)

                def security(*args, **kwargs):
                    account = args[args.index("-a") + 1]
                    if args[0] == "find-generic-password":
                        return mock.Mock(returncode=0 if account in stored else 44,
                                         stdout=stored.get(account, ""), stderr="")
                    if args[0] == "delete-generic-password":
                        stored.pop(account, None)
                        return mock.Mock(returncode=0)
                    stored[account] = kwargs["stdin"].splitlines()[0]
                    return mock.Mock(returncode=0)

                with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": tmp}), \
                        mock.patch.object(keychain, "keychain_available", return_value=True), \
                        mock.patch.object(keychain, "_security", side_effect=security), \
                        mock.patch("khipu.models.set_models_replace", side_effect=RuntimeError("late failure")):
                    with self.assertRaisesRegex(RuntimeError, "late failure"):
                        join.import_kit(self._payload(root_crt_pem="new cert", gemini_api_key="new-gemini",
                                                      openai_compat_api_key="new-openai"), "")
                self.assertEqual(stored, original)
                self.assertEqual((root / "dsn").read_text(), "host=old.example dbname=khipu\n")
                self.assertEqual((root / "root.crt").read_text(), "old cert\n")

    def _payload(self, **extra: object) -> bytes:
        models = {
            "synth": {"provider": "cloud", "endpoint": "", "model_id": "x"},
            "embed": {"provider": "cloud", "endpoint": "", "model_id": ""},
            "vision": {"provider": "off", "endpoint": "", "model_id": ""},
        }
        return json.dumps({
            "format": join.FORMAT_VERSION,
            "database_url": "host=remote.example dbname=khipu",
            "capture_mode": "hub",
            "models": models,
            **extra,
        }).encode()

    def test_all_fields_validate_before_any_mutation(self) -> None:
        setters = [
            mock.patch("khipu.keychain.set_dsn"),
            mock.patch("khipu.keychain.set_gemini_key"),
            mock.patch("khipu.config.set_capture_mode"),
        ]
        mocks = [patcher.start() for patcher in setters]
        try:
            with self.assertRaises(ValueError):
                join.import_kit(self._payload(gateway_url="http://remote.example"), "")
            for setter in mocks:
                setter.assert_not_called()
        finally:
            for patcher in reversed(setters):
                patcher.stop()

    def test_owned_files_roll_back_when_late_setter_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dsn_path = root / "dsn"
            cert_path = root / "root.crt"
            cfg_path = root / "config.json"
            dsn_path.write_bytes(b"old dsn\n")
            cert_path.write_bytes(b"old cert\n")
            cfg_path.write_bytes(b'{"capture_mode":"dual"}\n')
            with (
                mock.patch("khipu.paths.ensure_data_dir", return_value=root),
                mock.patch("khipu.paths.dsn_file", return_value=dsn_path),
                mock.patch("khipu.paths.root_cert_file", return_value=cert_path),
                mock.patch.object(config, "config_file", return_value=cfg_path),
                mock.patch("khipu.keychain.set_dsn"),
                mock.patch("khipu.models.set_models_replace", side_effect=RuntimeError("late failure")),
            ):
                with self.assertRaises(RuntimeError):
                    join.import_kit(self._payload(root_crt_pem="new cert"), "")
            self.assertEqual(dsn_path.read_bytes(), b"old dsn\n")
            self.assertEqual(cert_path.read_bytes(), b"old cert\n")
            # Put back setting by setting, never as a whole-file copy.
            self.assertEqual(json.loads(cfg_path.read_bytes()), {"capture_mode": "dual"})

    def test_existing_malformed_config_refuses_before_dsn_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_bytes(b"{broken")
            with (
                mock.patch.object(config, "config_file", return_value=cfg),
                mock.patch("khipu.keychain.set_dsn") as set_dsn,
            ):
                with self.assertRaises(ValueError):
                    join.import_kit(self._payload(), "")
            set_dsn.assert_not_called()
            self.assertEqual(cfg.read_bytes(), b"{broken")


class StoreFallbackRegressionTest(unittest.TestCase):
    def test_keychain_disabled_fallback_resolves_and_is_private(self) -> None:
        import stat
        from khipu import db

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": tmp, "KHIPU_KEYCHAIN": "0"}, clear=False):
                out = setup._stage_store("host=remote.example dbname=khipu", None)
                path = Path(tmp) / "dsn"
                self.assertTrue(out["ok"])
                self.assertEqual(db.resolve_dsn(), "host=remote.example dbname=khipu")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_available_keychain_failure_restores_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dsn = root / "dsn"
            cert = root / "root.crt"
            source = root / "incoming.crt"
            dsn.write_bytes(b"old dsn\n")
            cert.write_bytes(b"old cert\n")
            source.write_bytes(b"new cert\n")
            with (
                mock.patch("khipu.paths.dsn_file", return_value=dsn),
                mock.patch("khipu.paths.root_cert_file", return_value=cert),
                mock.patch("khipu.keychain.keychain_available", return_value=True),
                mock.patch("khipu.keychain.set_dsn", side_effect=RuntimeError("denied")),
            ):
                out = setup._stage_store("host=remote.example dbname=khipu", str(source))
            self.assertFalse(out["ok"])
            self.assertEqual(dsn.read_bytes(), b"old dsn\n")
            self.assertEqual(cert.read_bytes(), b"old cert\n")


class LaunchdUncertaintyRegressionTest(unittest.TestCase):
    def test_permission_denied_print_is_not_proof_of_absence(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="permission denied")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(launchd_gen.subprocess, "run", side_effect=[failed, failed]):
                out = launchd_gen._launchctl_unload("com.test", Path(tmp) / "missing.plist")
        self.assertFalse(out["ok"])

    def test_known_absent_print_allows_idempotent_uninstall(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="Could not find service")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(launchd_gen.subprocess, "run", side_effect=[failed, failed]):
                out = launchd_gen._launchctl_unload("com.test", Path(tmp) / "missing.plist")
        self.assertTrue(out["ok"])

    def test_failed_bootout_and_still_loaded_is_unhealthy(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="permission denied")
        loaded = mock.Mock(returncode=0, stdout="state = running", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(launchd_gen.subprocess, "run", side_effect=[failed, loaded]):
                out = launchd_gen._launchctl_unload("com.test", Path(tmp) / "missing.plist")
        self.assertFalse(out["ok"])


class ScheduledDriverCurrentRegressionTest(unittest.TestCase):
    def test_current_app_plist_without_required_driver_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "nightly.plist"
            plist.write_bytes(b"plist")
            with (
                mock.patch.object(launchd_gen, "_plist_path", return_value=plist),
                mock.patch.object(launchd_gen, "plist_external", return_value=False),
                mock.patch.object(launchd_gen, "plist_current", return_value=True),
                mock.patch.object(launchd_gen, "_installed_plist", return_value={"EnvironmentVariables": {}}),
                mock.patch.object(launchd_gen, "install_job") as install,
            ):
                out = launchd_gen.ensure_scheduled_jobs(["nightly"])
        self.assertFalse(out["ok"])
        install.assert_not_called()


class SetupReadOnlyRegressionTest(unittest.TestCase):
    def test_read_only_privilege_probe_executes_no_create_and_never_commits(self) -> None:
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [("db",), (True,), ("vector",)]
        out = setup._stage_privileges(conn, read_only=True)
        self.assertTrue(out["ok"])
        sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
        self.assertNotIn("CREATE ", sql.upper())
        conn.commit.assert_not_called()

    def test_read_only_pending_schema_reports_preparation_needed(self) -> None:
        with (
            mock.patch("khipu.migrate.available", return_value=["001.sql"]),
            mock.patch("khipu.migrate.run", return_value={"pending": ["001.sql"], "ran": []}),
        ):
            out = setup._stage_schema(mock.Mock(), read_only=True)
        self.assertFalse(out["ok"])
        self.assertIn("needs preparation", out["title"].lower())

    def test_pending_schema_stops_before_graph_probe(self) -> None:
        conn = mock.MagicMock()
        ok = {"id": "ok", "ok": True, "title": "ok", "detail": "ok"}
        pending = {"id": "schema", "ok": False, "title": "needs preparation", "detail": "pending"}
        with (
            mock.patch.object(setup, "_connect", return_value=conn),
            mock.patch.object(setup, "_stage_version", return_value=ok),
            mock.patch.object(setup, "_stage_privileges", return_value=ok),
            mock.patch.object(setup, "_stage_schema", return_value=pending),
            mock.patch.object(setup, "_stage_graph") as graph,
        ):
            out = setup.connect_database(
                "host=remote.example dbname=khipu",
                store=False,
                install_jobs=False,
                prove=False,
            )
        self.assertFalse(out["ok"])
        graph.assert_not_called()


class SourceRegistryBoundaryRegressionTest(unittest.TestCase):
    def test_existing_directory_registry_is_explicit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "graph_sources.json"
            registry.mkdir()
            with mock.patch.object(sources, "sources_file", return_value=registry):
                with self.assertRaises(sources.InvalidSourcesDocument):
                    sources.load_sources()

    def test_graph_sync_refuses_corrupt_registry_before_pg_mutation(self) -> None:
        from khipu import graph_sync

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "graph.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, bucket TEXT, name TEXT, "
                "payload TEXT, source_path TEXT, built_at TEXT, frozen INTEGER);"
                "CREATE TABLE edges (src TEXT, dst TEXT, type TEXT, weight REAL, payload TEXT, built_at TEXT);"
                "INSERT INTO nodes VALUES ('pg-only','concept','code','x',NULL,'/code/a.py',NULL,0);"
            )
            con.commit()
            con.close()
            registry = root / "graph_sources.json"
            registry.write_text("{broken")
            with (
                mock.patch.object(sources, "sources_file", return_value=registry),
                mock.patch("khipu.db.connect") as connect,
            ):
                with self.assertRaises(sources.InvalidSourcesDocument):
                    graph_sync.sync_from_sqlite(db)
            connect.assert_not_called()


class LocalMoveGuardRegressionTest(unittest.TestCase):
    def test_existing_local_source_is_refused_before_destructive_installer(self) -> None:
        with (
            mock.patch("khipu.db.resolve_dsn", return_value="host=localhost dbname=khipu"),
            mock.patch("khipu.components_postgres.install_local_postgres") as install,
        ):
            out = setup.provision_local_move_target()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "existing_local_installation")
        install.assert_not_called()


class PairAbsoluteDeadlineRegressionTest(unittest.TestCase):
    def test_resolve_partial_txt_line_obeys_overall_deadline(self) -> None:
        from khipu import join_pair

        read_fd, write_fd = os.pipe()
        stdout = os.fdopen(read_fd, "r", encoding="utf-8")
        writer = os.fdopen(write_fd, "w", encoding="utf-8")
        proc = mock.Mock(stdout=stdout)
        proc.wait.return_value = 0
        result: list[object] = []
        errors: list[BaseException] = []

        def emit() -> None:
            import time

            writer.write("Lookup can be reached at nearby.local.:8788 (interface 4)\n")
            writer.flush()
            time.sleep(0.03)
            writer.write("ipv4=192.168.1.23")
            writer.flush()

        def resolve() -> None:
            try:
                result.append(join_pair._resolve_service("Nearby", timeout_s=0.10))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        emit_thread = threading.Thread(target=emit, daemon=True)
        resolve_thread = threading.Thread(target=resolve, daemon=True)
        try:
            with mock.patch.object(join_pair.subprocess, "Popen", return_value=proc):
                emit_thread.start()
                resolve_thread.start()
                resolve_thread.join(0.30)
            self.assertFalse(
                resolve_thread.is_alive(), "partial TXT output blocked past the resolve deadline"
            )
            self.assertEqual(errors, [])
            self.assertEqual(result, [("nearby.local", 8788, None)])
        finally:
            writer.close()
            resolve_thread.join(0.50)
            stdout.close()

    def test_advertiser_stops_slowloris_read_at_pin_deadline(self) -> None:
        from khipu import join_pair

        class Clock:
            now = 0.0

            def time(self) -> float:
                return self.now

        class DribbleTLS:
            def __init__(self, clock: Clock) -> None:
                self.clock = clock
                self.data = bytearray(
                    json.dumps({"protocol": join_pair._PROTOCOL, "pin": "123456"}).encode()
                    + b"\n"
                )
                self.recv_calls = 0
                self.sent: list[bytes] = []

            def settimeout(self, _timeout: float) -> None:
                return None

            def recv(self, _size: int) -> bytes:
                self.recv_calls += 1
                self.clock.now += 0.60
                return bytes([self.data.pop(0)]) if self.data else b""

            def sendall(self, data: bytes) -> None:
                self.sent.append(data)

            def close(self) -> None:
                return None

        clock = Clock()
        tls = DribbleTLS(clock)
        client = mock.Mock()
        server = mock.Mock()
        server.accept.return_value = (client, ("127.0.0.1", 1))
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        dns = mock.Mock()
        dns.poll.return_value = None
        dns.wait.return_value = 0
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            (cert_dir / "cert.pem").write_text("cert")
            (cert_dir / "key.pem").write_text("key")
            with (
                mock.patch("khipu.join.export_kit", return_value=b"kit"),
                mock.patch.object(join_pair, "_self_signed_cert_dir", return_value=cert_dir),
                mock.patch.object(join_pair.ssl, "SSLContext", return_value=context),
                mock.patch.object(join_pair.socket, "socket", return_value=server),
                mock.patch.object(join_pair.subprocess, "Popen", return_value=dns),
                mock.patch.object(join_pair, "lan_ipv4", return_value=None),
                mock.patch.object(join_pair.time, "time", side_effect=clock.time),
                mock.patch.object(join_pair.time, "sleep", return_value=None),
            ):
                out = join_pair.advertise("", timeout_s=1, pin="123456")
        self.assertFalse(out["ok"])
        self.assertLessEqual(tls.recv_calls, 2)
        self.assertEqual(tls.sent, [])


class JoinStoredModelsPrevalidationRegressionTest(unittest.TestCase):
    def test_invalid_stored_models_refuse_before_credentials_or_files_change(self) -> None:
        models = {
            "synth": {"provider": "cloud", "endpoint": "", "model_id": "x"},
            "embed": {"provider": "cloud", "endpoint": "", "model_id": ""},
            "vision": {"provider": "off", "endpoint": "", "model_id": ""},
        }
        payload = json.dumps({
            "format": join.FORMAT_VERSION,
            "database_url": "host=remote.example dbname=khipu",
            "capture_mode": "hub",
            "models": models,
            "root_crt_pem": "new cert",
            "gemini_api_key": "new secret",
        }).encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dsn_path = root / "dsn"
            cert_path = root / "root.crt"
            cfg_path = root / "config.json"
            dsn_path.write_bytes(b"old dsn\n")
            cert_path.write_bytes(b"old cert\n")
            cfg_path.write_bytes(b'{"capture_mode":"dual","models":{"synth":"bad"}}\n')
            before = {p: p.read_bytes() for p in (dsn_path, cert_path, cfg_path)}
            with (
                mock.patch("khipu.paths.ensure_data_dir", return_value=root),
                mock.patch("khipu.paths.dsn_file", return_value=dsn_path),
                mock.patch("khipu.paths.root_cert_file", return_value=cert_path),
                mock.patch.object(config, "config_file", return_value=cfg_path),
                mock.patch("khipu.keychain.set_dsn") as set_dsn,
                mock.patch("khipu.keychain.set_gemini_key") as set_gemini,
            ):
                with self.assertRaises(ValueError):
                    join.import_kit(payload, "")
            set_dsn.assert_not_called()
            set_gemini.assert_not_called()
            self.assertEqual({p: p.read_bytes() for p in before}, before)


class NightlyUnknownEvidenceRegressionTest(unittest.TestCase):
    def test_success_exit_without_timestamp_and_unknown_missing_steps_are_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nightly-last.json"
            with (
                mock.patch.object(jobs, "_nightly_last_path", return_value=missing),
                mock.patch("khipu.features.enabled", return_value=True),
                mock.patch.object(jobs, "_is_index_sync_host", return_value=True),
                mock.patch.object(
                    jobs,
                    "_job_entry",
                    return_value={"plist_loaded": False, "last_exit": 0, "last_exit_ts": None},
                ),
            ):
                health = jobs.nightly_step_health()
        self.assertFalse(health["nightly_job_ok"]["ok"])
        self.assertTrue(all(not row["ok"] for key, row in health.items() if key != "nightly_job_ok"))

    def test_missing_expected_nightly_log_with_unknown_age_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MEMORY.md").write_text("# Memory\n")
            with (
                mock.patch.object(jobs, "_is_index_sync_host", return_value=True),
                mock.patch.object(jobs, "nightly_log_path", return_value=root / "missing.log"),
                mock.patch.object(
                    jobs,
                    "_job_entry",
                    return_value={"plist_loaded": False, "last_exit_ts": None, "plist_mtime": None},
                ),
            ):
                health = jobs.index_freshness(memory_root=root)
        self.assertFalse(health["ok"])
        self.assertTrue(any("missing" in reason for reason in health["reasons"]))
