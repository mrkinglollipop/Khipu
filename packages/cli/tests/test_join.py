"""Unit tests for khipu.join — encrypt roundtrip, localhost refuse, DSN rewrite."""

from __future__ import annotations

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
        self.assertEqual(unquote(qs["sslrootcert"][0]), "/tmp/khipu/root.crt")
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
        self.assertIn("not a join-kit file problem", out["error"])


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
            self.assertEqual(unquote(qs["sslrootcert"][0]), str(cert))
            self.assertNotIn("/old/mac/root.crt", rewritten)
            set_gemini.assert_called_once_with("g-secret")
            set_mode.assert_called_once_with("hub")
            set_models.assert_called_once()
            self.assertNotIn("database_url", summary)
            self.assertNotIn("gemini_api_key", summary)
            self.assertTrue(summary["has_gemini_api_key"])
            self.assertEqual((data / "dsn").read_text().strip(), rewritten)


if __name__ == "__main__":
    unittest.main()
