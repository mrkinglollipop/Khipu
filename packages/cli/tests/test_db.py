"""khipu.db resolves the DSN for every other module. Untested until the
2026-08-17 audit, which found the legacy-secret migration running inside every
single connect().

No test opens a real connection or touches the real Keychain.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from khipu import db

_CLEAR = {
    "KHIPU_DATABASE_URL": "",
    "ALZY_DATABASE_URL": "",
    "KHIPU_DSN_FILE": "",
    "ALZY_DSN_FILE": "",
}


class ResolveDsnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db._MIGRATION_DONE = False

    def tearDown(self):
        db._MIGRATION_DONE = False
        self.tmp.cleanup()

    def _no_keychain(self):
        return mock.patch.dict(
            "sys.modules",
            {"khipu.keychain": mock.MagicMock(get_dsn=lambda: None,
                                              migrate_legacy_secrets=lambda: {})},
        )

    def test_env_wins_and_short_circuits_everything(self):
        with mock.patch.dict(os.environ, {**_CLEAR, "KHIPU_DATABASE_URL": "postgres://env"}):
            with mock.patch("khipu.keychain.get_dsn") as kc:
                self.assertEqual(db.resolve_dsn(), "postgres://env")
            kc.assert_not_called()

    def test_the_legacy_env_name_still_works(self):
        with mock.patch.dict(os.environ, {**_CLEAR, "ALZY_DATABASE_URL": "postgres://legacy"}):
            self.assertEqual(db.resolve_dsn(), "postgres://legacy")

    def test_whitespace_only_env_is_not_a_dsn(self):
        f = self.root / "dsn"
        f.write_text("postgres://file\n")
        with mock.patch.dict(os.environ, {**_CLEAR, "KHIPU_DATABASE_URL": "   ",
                                          "KHIPU_DSN_FILE": str(f)}), \
             mock.patch("khipu.keychain.get_dsn", return_value=None), \
             mock.patch("khipu.keychain.migrate_legacy_secrets", return_value={}):
            self.assertEqual(db.resolve_dsn(), "postgres://file")

    def test_keychain_comes_before_the_file(self):
        f = self.root / "dsn"
        f.write_text("postgres://file")
        with mock.patch.dict(os.environ, {**_CLEAR, "KHIPU_DSN_FILE": str(f)}), \
             mock.patch("khipu.keychain.get_dsn", return_value="postgres://kc"), \
             mock.patch("khipu.keychain.migrate_legacy_secrets", return_value={}):
            self.assertEqual(db.resolve_dsn(), "postgres://kc")

    def test_a_flaky_keychain_never_blocks_the_file_fallback(self):
        f = self.root / "dsn"
        f.write_text("postgres://file")
        with mock.patch.dict(os.environ, {**_CLEAR, "KHIPU_DSN_FILE": str(f)}), \
             mock.patch("khipu.keychain.migrate_legacy_secrets", side_effect=RuntimeError("locked")):
            self.assertEqual(db.resolve_dsn(), "postgres://file")

    def test_the_file_value_is_trimmed(self):
        f = self.root / "dsn"
        f.write_text("  postgres://file\n\n")
        with mock.patch.dict(os.environ, {**_CLEAR, "KHIPU_DSN_FILE": str(f)}), \
             mock.patch("khipu.keychain.get_dsn", return_value=None), \
             mock.patch("khipu.keychain.migrate_legacy_secrets", return_value={}):
            self.assertEqual(db.resolve_dsn(), "postgres://file")

    def test_nothing_anywhere_raises_a_pointed_message(self):
        with mock.patch.dict(os.environ, {**_CLEAR, "KHIPU_DSN_FILE": str(self.root / "absent")}), \
             mock.patch("khipu.keychain.get_dsn", return_value=None), \
             mock.patch("khipu.keychain.migrate_legacy_secrets", return_value={}), \
             mock.patch.object(db, "LEGACY_DSN_FILE", self.root / "also-absent"):
            with self.assertRaises(RuntimeError) as e:
                db.resolve_dsn()
        self.assertIn("No Khipu DSN", str(e.exception))

    # --- regression: audit 2026-08-17 -----------------------------------------

    def test_the_legacy_migration_runs_once_per_process_not_once_per_connect(self):
        """It ran inside resolve_dsn(), so every connect() paid up to four
        `security` subprocess spawns before a single query."""
        f = self.root / "dsn"
        f.write_text("postgres://file")
        with mock.patch.dict(os.environ, {**_CLEAR, "KHIPU_DSN_FILE": str(f)}), \
             mock.patch("khipu.keychain.get_dsn", return_value=None), \
             mock.patch("khipu.keychain.migrate_legacy_secrets", return_value={}) as mig:
            for _ in range(5):
                db.resolve_dsn()
        self.assertEqual(mig.call_count, 1)

    def test_a_failed_migration_is_retried_rather_than_marked_done(self):
        f = self.root / "dsn"
        f.write_text("postgres://file")
        with mock.patch.dict(os.environ, {**_CLEAR, "KHIPU_DSN_FILE": str(f)}), \
             mock.patch("khipu.keychain.migrate_legacy_secrets",
                        side_effect=RuntimeError("locked")) as mig:
            db.resolve_dsn()
            db.resolve_dsn()
        self.assertEqual(mig.call_count, 2)


class DsnConfiguredTest(unittest.TestCase):
    def test_true_when_a_dsn_resolves(self):
        with mock.patch.object(db, "resolve_dsn", return_value="postgres://x"):
            self.assertTrue(db.dsn_configured())

    def test_false_when_none_does(self):
        with mock.patch.object(db, "resolve_dsn", side_effect=RuntimeError("no dsn")):
            self.assertFalse(db.dsn_configured())


class ConnectTest(unittest.TestCase):
    def test_connect_passes_the_resolved_dsn_and_autocommit_through(self):
        with mock.patch.object(db, "resolve_dsn", return_value="postgres://x"), \
             mock.patch.object(db, "conninfo_with_local_root_cert", side_effect=lambda d: d), \
             mock.patch("psycopg.connect") as pg:
            db.connect(autocommit=True)
        pg.assert_called_once_with("postgres://x", autocommit=True)


class ConninfoLocalRootCertTest(unittest.TestCase):
    def test_overwrites_uri_sslrootcert_with_this_macs_file(self):
        from psycopg.conninfo import conninfo_to_dict

        with TemporaryDirectory() as tmp:
            cert = Path(tmp) / "root.crt"
            cert.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
            dsn = (
                "postgresql://u:secret@100.114.233.88:5433/alzy"
                "?sslmode=verify-full"
                "&sslrootcert=/Users/matthewschwartz/.config/khipu/root.crt"
            )
            with mock.patch("khipu.paths.root_cert_file", return_value=cert):
                out = db.conninfo_with_local_root_cert(dsn)
        parsed = conninfo_to_dict(out)
        self.assertEqual(parsed.get("sslrootcert"), str(cert.resolve()))
        self.assertEqual(parsed.get("host"), "100.114.233.88")
        self.assertEqual(parsed.get("port"), "5433")
        self.assertNotIn("://", out)
        self.assertNotIn("secret", repr(parsed.get("sslrootcert")))

    def test_overwrites_percent_encoded_uri_sslrootcert_too(self):
        from urllib.parse import quote

        from psycopg.conninfo import conninfo_to_dict

        with TemporaryDirectory() as tmp:
            cert = Path(tmp) / "root.crt"
            cert.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
            foreign = "/Users/matthewschwartz/.config/khipu/root.crt"
            dsn = (
                "postgresql://u:secret@100.114.233.88:5433/alzy"
                f"?sslmode=verify-full&sslrootcert={quote(foreign, safe='')}"
            )
            with mock.patch("khipu.paths.root_cert_file", return_value=cert):
                out = db.conninfo_with_local_root_cert(dsn)
            self.assertEqual(
                conninfo_to_dict(out).get("sslrootcert"), str(cert.resolve())
            )

    def test_keyword_conninfo_without_cert_file_omits_sslrootcert(self):
        from psycopg.conninfo import conninfo_to_dict

        missing = Path("/no/such/khipu-root.crt")
        dsn = "postgresql://u:p@hub:5433/db?sslmode=verify-full"
        with mock.patch("khipu.paths.root_cert_file", return_value=missing):
            out = db.conninfo_with_local_root_cert(dsn)
        parsed = conninfo_to_dict(out)
        self.assertEqual(parsed.get("host"), "hub")
        self.assertIsNone(parsed.get("sslrootcert"))


if __name__ == "__main__":
    unittest.main()
