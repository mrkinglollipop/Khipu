"""Unit tests for khipu.join — encrypt roundtrip, localhost refuse, DSN rewrite."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import join as j


class EncryptRoundtripTest(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self) -> None:
        payload = {
            "format": j.FORMAT_VERSION,
            "database_url": "postgres://user:secret@hub.example/db?sslmode=verify-full",
            "capture_mode": "dual",
            "models": {
                "synth": {
                    "provider": "cloud",
                    "endpoint": "",
                    "model_id": "gemini-2.5-flash",
                },
                "embed": {"provider": "cloud", "endpoint": "", "model_id": ""},
                "vision": {"provider": "off", "endpoint": "", "model_id": ""},
            },
            "expected": {"episodes": 10, "topics": 5, "nodes": 100},
            "created_at": "2026-08-27T00:00:00+00:00",
        }
        blob = j.encrypt_payload(payload, "hunter2")
        out = j.decrypt_payload(blob, "hunter2")
        self.assertEqual(out["database_url"], payload["database_url"])
        self.assertEqual(out["expected"], payload["expected"])

    def test_wrong_passphrase_raises(self) -> None:
        blob = j.encrypt_payload({"database_url": "postgres://x"}, "right")
        with self.assertRaises(ValueError):
            j.decrypt_payload(blob, "wrong")

    def test_plaintext_kit_roundtrip_without_passphrase(self) -> None:
        payload = {
            "format": j.FORMAT_VERSION,
            "database_url": "postgres://u:p@hub.example/db",
            "expected": {"episodes": 1, "topics": 1, "nodes": 1},
        }
        blob = json.dumps(payload).encode("utf-8")
        out = j.decrypt_payload(blob, "")
        self.assertEqual(out["database_url"], payload["database_url"])

    def test_encrypted_kit_without_passphrase_explains(self) -> None:
        blob = j.encrypt_payload({"format": j.FORMAT_VERSION, "database_url": "postgres://x"}, "secret")
        with self.assertRaises(ValueError) as ctx:
            j.decrypt_payload(blob, "")
        self.assertIn("passphrase-protected", str(ctx.exception))

    def test_write_kit_file_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "kit.khipujoin"
            j.write_kit_file(dest, b'{"format":"khipu-join-v1"}')
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)

    def test_resolve_passphrase_empty_is_ok(self) -> None:
        with mock.patch.dict(os.environ, {"KHIPU_JOIN_PASSPHRASE": ""}, clear=False):
            self.assertEqual(j.resolve_passphrase(None), "")
            self.assertEqual(j.resolve_passphrase("  "), "")

    def _export_patches(self):
        return (
            mock.patch("khipu.keychain.get_dsn", return_value="postgres://u:p@hub/db"),
            mock.patch("khipu.config.capture_mode", return_value="hub"),
            mock.patch("khipu.config.gateway_url", return_value=None),
            mock.patch("khipu.keychain.get_gemini_key", return_value="gem-key"),
            mock.patch("khipu.keychain.get_openai_compat_key", return_value=None),
            mock.patch.object(j, "_models_export_blob", return_value={}),
            mock.patch.object(
                j, "_fetch_live_counts", return_value={"episodes": 1, "topics": 1, "nodes": 1}
            ),
            mock.patch("khipu.paths.root_cert_file", return_value=Path("/no/such/root.crt")),
        )

    def _export(self, passphrase: str, **kw) -> bytes:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in self._export_patches():
                stack.enter_context(p)
            return j.export_kit(passphrase, **kw)

    def test_export_kit_refuses_without_passphrase(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._export("")
        self.assertIn("needs a passphrase", str(ctx.exception))
        self.assertIn("--allow-unencrypted", str(ctx.exception))

    def test_export_kit_plaintext_only_with_explicit_opt_out(self) -> None:
        blob = self._export("", allow_unencrypted=True)
        data = json.loads(blob.decode("utf-8"))
        self.assertEqual(data["database_url"], "postgres://u:p@hub/db")
        self.assertNotIn("ciphertext_b64", data)
        # Reading an existing unencrypted kit keeps working without a passphrase.
        self.assertEqual(j.decrypt_payload(blob, ""), data)

    def test_export_kit_encrypts_when_passphrase_set(self) -> None:
        blob = self._export("secret")
        self.assertNotIn(b"postgres://", blob)
        self.assertNotIn(b"gem-key", blob)
        envelope = json.loads(blob.decode("utf-8"))
        self.assertIn("ciphertext_b64", envelope)
        out = j.decrypt_payload(blob, "secret")
        self.assertEqual(out["database_url"], "postgres://u:p@hub/db")
        self.assertEqual(out["gemini_api_key"], "gem-key")
        with self.assertRaises(ValueError) as ctx:
            j.decrypt_payload(blob, "")
        self.assertIn("passphrase-protected", str(ctx.exception))


class JoinCliPassphraseTest(unittest.TestCase):
    def _run(self, argv: list[str], env: dict[str, str] | None = None):
        import contextlib
        import io

        from khipu import cli

        buf = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"KHIPU_JOIN_PASSPHRASE": "", **(env or {})}),
            mock.patch("khipu.join.export_kit", return_value=b"kit") as export,
            mock.patch("khipu.join.write_kit_file", side_effect=lambda p, b: p) as write,
            mock.patch("khipu.join.decrypt_payload", return_value={"expected": {}}),
            contextlib.redirect_stdout(buf),
        ):
            rc = cli.main(argv)
        return rc, buf.getvalue(), export, write

    def test_export_without_passphrase_is_refused_in_plain_words(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "kit.khipujoin")
            rc, stdout, export, write = self._run(["join", "export", "--out", out])
        self.assertEqual(rc, 2)
        body = json.loads(stdout)
        self.assertFalse(body["ok"])
        self.assertIn("needs a passphrase", body["error"])
        self.assertIn("database password", body["error"])
        export.assert_not_called()
        write.assert_not_called()

    def test_advertise_without_passphrase_is_refused(self) -> None:
        rc, stdout, export, _ = self._run(["join", "advertise"])
        self.assertEqual(rc, 2)
        self.assertIn("needs a passphrase", json.loads(stdout)["error"])
        export.assert_not_called()

    def test_export_opt_out_flag_makes_unencrypted_kit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "kit.khipujoin")
            rc, stdout, export, _ = self._run(
                ["join", "export", "--out", out, "--allow-unencrypted"]
            )
        self.assertEqual(rc, 0)
        export.assert_called_once_with("", allow_unencrypted=True)

    def test_export_with_passphrase_never_prints_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "kit.khipujoin")
            rc, stdout, export, _ = self._run(
                ["join", "export", "--out", out], env={"KHIPU_JOIN_PASSPHRASE": "s3cret-phrase"}
            )
        self.assertEqual(rc, 0)
        export.assert_called_once_with("s3cret-phrase", allow_unencrypted=False)
        self.assertNotIn("s3cret-phrase", stdout)



class MalformedDsnTest(unittest.TestCase):
    def test_malformed_database_url_error_never_echoes_kit_content(self) -> None:
        kit = json.dumps({
            "format": j.FORMAT_VERSION,
            "database_url": "host=hub password=hunter2secret hunter2tail",
        }).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"KHIPU_DATA_DIR": tmp}
        ):
            with self.assertRaises(ValueError) as ctx:
                j.import_kit(kit, "")
        msg = str(ctx.exception)
        self.assertNotIn("hunter2", msg)
        self.assertNotIn("host=hub", msg)
        self.assertIn("database address", msg)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)


class PassphraseInputTest(unittest.TestCase):
    """A passphrase never travels as a literal argument: env, stdin, a file
    descriptor or the terminal prompt."""

    def _run(self, argv, *, stdin_text=None, tty=False, getpass_value=None):
        import contextlib
        import io

        from khipu import cli

        buf = io.StringIO()
        stdin = io.StringIO(stdin_text or "")
        stdin.isatty = lambda: tty  # type: ignore[method-assign]
        with (
            mock.patch.dict(os.environ, {"KHIPU_JOIN_PASSPHRASE": ""}),
            mock.patch("sys.stdin", stdin),
            mock.patch("getpass.getpass", return_value=getpass_value or "") as gp,
            mock.patch("khipu.join.export_kit", return_value=b"kit") as export,
            mock.patch("khipu.join.write_kit_file", side_effect=lambda p, b: p),
            mock.patch("khipu.join.decrypt_payload", return_value={"expected": {}}),
            contextlib.redirect_stdout(buf),
        ):
            rc = cli.main(argv)
        return rc, buf.getvalue(), export, gp

    def test_literal_passphrase_argument_is_refused_and_steers_to_safe_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "kit.khipujoin")
            rc, stdout, export, _ = self._run(["join", "export", "--out", out, "--passphrase", "s3cret"])
        self.assertEqual(rc, 2)
        body = json.loads(stdout)
        self.assertIn("visible to other processes", body["error"])
        self.assertIn("KHIPU_JOIN_PASSPHRASE", body["error"])
        self.assertNotIn("s3cret", stdout)
        export.assert_not_called()

    def test_passphrase_stdin_reads_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "kit.khipujoin")
            rc, stdout, export, _ = self._run(
                ["join", "export", "--out", out, "--passphrase-stdin"], stdin_text="from stdin\nextra\n"
            )
        self.assertEqual(rc, 0)
        export.assert_called_once_with("from stdin", allow_unencrypted=False)
        self.assertNotIn("from stdin", stdout)

    def test_passphrase_fd_reads_from_the_descriptor(self) -> None:
        r, w = os.pipe()
        os.write(w, b"from fd\n")
        os.close(w)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "kit.khipujoin")
                rc, _, export, _ = self._run(["join", "export", "--out", out, "--passphrase-fd", str(r)])
        finally:
            try:
                os.close(r)
            except OSError:
                pass
        self.assertEqual(rc, 0)
        export.assert_called_once_with("from fd", allow_unencrypted=False)

    def test_interactive_export_prompts_for_the_passphrase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "kit.khipujoin")
            rc, _, export, gp = self._run(
                ["join", "export", "--out", out], tty=True, getpass_value="typed"
            )
        self.assertEqual(rc, 0)
        gp.assert_called()
        export.assert_called_once_with("typed", allow_unencrypted=False)


class LocalhostRefuseTest(unittest.TestCase):
    def test_is_localhost_dsn(self) -> None:
        for dsn in (
            "postgres://u:p@127.0.0.1/db",
            "postgres://u:p@localhost/db",
            "postgres://u:p@[::1]/db",
        ):
            with self.subTest(dsn=dsn):
                self.assertTrue(j.is_localhost_dsn(dsn))

    def test_import_kit_refuses_localhost(self) -> None:
        blob = j.encrypt_payload(
            {"database_url": "postgres://u:p@127.0.0.1/khipu"},
            "pw",
        )
        with self.assertRaises(ValueError) as ctx:
            j.import_kit(blob, "pw")
        self.assertIn("localhost", str(ctx.exception).lower())


class SslrootcertRewriteTest(unittest.TestCase):
    def test_rewrite_sets_sslrootcert(self) -> None:
        from urllib.parse import parse_qs, unquote, urlsplit

        dsn = "postgres://u:p@hub.example/khipu?sslmode=verify-full"
        out = j.rewrite_dsn_sslrootcert(dsn, "/tmp/khipu/root.crt")
        self.assertIn("sslrootcert=", out)
        qs = parse_qs(urlsplit(out).query)
        self.assertEqual(unquote(qs["sslrootcert"][0]), str(Path("/tmp/khipu/root.crt").resolve()))
        self.assertIn("sslmode=verify-full", out)


class VerifyLiveCountsTest(unittest.TestCase):
    def test_expected_episodes_gt_zero_live_zero_fails(self) -> None:
        with mock.patch.object(
            j,
            "_fetch_live_counts",
            return_value={"episodes": 0, "topics": 0, "nodes": 0},
        ):
            out = j.verify_live_counts({"episodes": 42, "topics": 1, "nodes": 9})
        self.assertFalse(out["ok"])
        self.assertTrue(any("episodes" in m for m in out["mismatches"]))

    def test_matching_counts_ok(self) -> None:
        expected = {"episodes": 3, "topics": 2, "nodes": 7}
        with mock.patch.object(j, "_fetch_live_counts", return_value=dict(expected)):
            out = j.verify_live_counts(expected)
        self.assertTrue(out["ok"])
        self.assertEqual(out["mismatches"], [])

    def test_nonzero_delta_is_warning_not_hard_stop(self) -> None:
        with mock.patch.object(
            j,
            "_fetch_live_counts",
            return_value={"episodes": 43, "topics": 1, "nodes": 9},
        ):
            out = j.verify_live_counts({"episodes": 42, "topics": 1, "nodes": 9})
        self.assertTrue(out["ok"])
        self.assertTrue(any("episodes" in m for m in out["mismatches"]))

    def test_connect_failure_names_host_port(self) -> None:
        with (
            mock.patch.object(
                j, "_fetch_live_counts", side_effect=RuntimeError("connection refused")
            ),
            mock.patch.object(j, "_dsn_host_port", return_value="hub.example:5432"),
        ):
            out = j.verify_live_counts({"episodes": 42, "topics": 1, "nodes": 9})
        self.assertFalse(out["ok"])
        self.assertIn("hub.example:5432", out["error"])
        self.assertIn("unreachable", out["error"])

    def test_cert_missing_error_not_blamed_on_tailscale(self) -> None:
        err = RuntimeError(
            'root certificate file "/Users/matthewsc" does not exist'
        )
        with (
            mock.patch.object(j, "_fetch_live_counts", side_effect=err),
            mock.patch.object(j, "_dsn_host_port", return_value="100.1.2.3:5433"),
        ):
            out = j.verify_live_counts({"episodes": 1, "topics": 1, "nodes": 1})
        self.assertFalse(out["ok"])
        self.assertIn("TLS/certificate", out["error"])
        self.assertIn("not a Tailscale routing failure", out["error"])
        self.assertNotIn("hub must be reachable", out["error"])

    def test_ssl_syscall_timeout_is_network_not_cert(self) -> None:
        err = RuntimeError("SSL SYSCALL error: Operation timed out")
        with (
            mock.patch.object(j, "_fetch_live_counts", side_effect=err),
            mock.patch.object(j, "_dsn_host_port", return_value="100.1.2.3:5433"),
        ):
            out = j.verify_live_counts({"episodes": 1, "topics": 1, "nodes": 1})
        self.assertFalse(out["ok"])
        self.assertIn("unreachable", out["error"])
        self.assertNotIn("TLS/certificate", out["error"])
        self.assertIn("hub must be reachable", out["error"])


class ImportKitApplyTest(unittest.TestCase):
    def test_import_writes_cert_and_dsn_without_secrets_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            cert = data / "root.crt"
            payload = {
                "format": j.FORMAT_VERSION,
                "database_url": (
                    "postgres://u:pw@remote.example/khipu?sslmode=verify-full"
                    "&sslrootcert=/old/mac/root.crt"
                ),
                "root_crt_pem": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
                "capture_mode": "hub",
                "models": {
                    "synth": {
                        "provider": "cloud",
                        "endpoint": "",
                        "model_id": "gemini-2.5-flash",
                    },
                    "embed": {"provider": "cloud", "endpoint": "", "model_id": ""},
                    "vision": {"provider": "off", "endpoint": "", "model_id": ""},
                },
                "expected": {"episodes": 1, "topics": 1, "nodes": 1},
                "created_at": "2026-08-27T00:00:00+00:00",
                "gemini_api_key": "g-secret",
            }
            blob = j.encrypt_payload(payload, "join-pass")
            with (
                mock.patch("khipu.paths.data_dir", return_value=data),
                mock.patch("khipu.paths.ensure_data_dir", return_value=data),
                mock.patch("khipu.paths.dsn_file", return_value=data / "dsn"),
                mock.patch("khipu.paths.root_cert_file", return_value=cert),
                mock.patch("khipu.keychain.set_dsn") as set_dsn,
                mock.patch("khipu.keychain.set_gemini_key") as set_gemini,
                mock.patch("khipu.config.set_capture_mode") as set_mode,
                mock.patch("khipu.models.set_models_replace") as set_models,
            ):
                summary = j.import_kit(blob, "join-pass")
            self.assertTrue(cert.is_file())
            set_dsn.assert_called_once()
            rewritten = set_dsn.call_args.args[0]
            from urllib.parse import parse_qs, unquote, urlsplit

            qs = parse_qs(urlsplit(rewritten).query)
            self.assertEqual(unquote(qs["sslrootcert"][0]), str(cert.resolve()))
            self.assertNotIn("/old/mac/root.crt", rewritten)
            set_gemini.assert_called_once_with("g-secret")
            set_mode.assert_called_once_with("hub")
            set_models.assert_called_once()
            self.assertNotIn("database_url", summary)
            self.assertNotIn("gemini_api_key", summary)
            self.assertTrue(summary["has_gemini_api_key"])
            self.assertEqual((data / "dsn").read_text().strip(), rewritten)

    def test_import_refuses_verify_full_without_pem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            payload = {
                "format": j.FORMAT_VERSION,
                "database_url": (
                    "postgres://u:pw@remote.example/khipu?sslmode=verify-full"
                    "&sslrootcert=/old/mac/root.crt"
                ),
                "capture_mode": "hub",
                "expected": {"episodes": 1, "topics": 1, "nodes": 1},
                "created_at": "2026-08-27T00:00:00+00:00",
            }
            blob = j.encrypt_payload(payload, "join-pass")
            with (
                mock.patch("khipu.paths.data_dir", return_value=data),
                mock.patch("khipu.paths.ensure_data_dir", return_value=data),
                mock.patch("khipu.paths.dsn_file", return_value=data / "dsn"),
                mock.patch(
                    "khipu.paths.root_cert_file", return_value=data / "root.crt"
                ),
            ):
                with self.assertRaises(ValueError) as ctx:
                    j.import_kit(blob, "join-pass")
            self.assertIn("root_crt_pem", str(ctx.exception))

    def test_rewrite_percent_encodes_slashes(self) -> None:
        dsn = "postgres://u:p@h/db?sslmode=verify-full"
        out = j.rewrite_dsn_sslrootcert(
            dsn, "/Users/example/.config/khipu/root.crt"
        )
        self.assertIn("sslrootcert=%2FUsers%2F", out)
        self.assertNotIn("sslrootcert=/Users/", out)


if __name__ == "__main__":
    unittest.main()
