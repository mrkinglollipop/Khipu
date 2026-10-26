"""Credentials (the hub DSN, the gateway token) stay out of error text, a
restore never swaps them, and the legacy DSN is not read past an explicit
data folder."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SECRET = "s3cr3t-hub-password"


class PreflightErrorTextTest(unittest.TestCase):
    def test_unparseable_dsn_detail_does_not_quote_the_password(self):
        from khipu.setup import connect_database

        out = connect_database(f"postgres://u:{SECRET}%zz@h/d", store=False, install_jobs=False, prove=False)
        self.assertNotIn(SECRET, json.dumps(out), json.dumps(out)[:300])


class RestoreKeepsCurrentCredentialsTest(unittest.TestCase):
    def _restore(self, merge: bool) -> str:
        from khipu import paths

        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            data.mkdir()
            (data / "dsn").write_text("postgres://u:current@h/d\n")
            archive = Path(td) / "b.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("dsn", "postgres://u:from-archive@evil/d\n")
            with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data)}):
                paths.import_local(source=archive, merge=merge)
            return (data / "dsn").read_text()

    def test_merge_does_not_replace_an_existing_dsn(self):
        self.assertIn("current", self._restore(merge=True))

    def test_replace_does_not_replace_an_existing_dsn(self):
        self.assertIn("current", self._restore(merge=False))


class LegacyDsnFallbackTest(unittest.TestCase):
    def test_an_explicit_empty_data_dir_does_not_resolve_the_legacy_dsn(self):
        from khipu import db, keychain

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_cfg, cfg = root / "alzy", root / "khipu"
            legacy_cfg.mkdir()
            (legacy_cfg / "dsn").write_text("postgres://legacy-user:pw@legacy-host/legacy\n")
            empty = root / "empty"
            empty.mkdir()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"KHIPU_KEYCHAIN": "0", "KHIPU_DATA_DIR": str(empty)}))
                for name in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL", "KHIPU_ISOLATED_DIR", "KHIPU_DSN_FILE", "ALZY_DSN_FILE"):
                    os.environ.pop(name, None)
                stack.enter_context(mock.patch.object(keychain, "CONFIG_DIR", cfg))
                stack.enter_context(mock.patch.object(keychain, "LEGACY_CONFIG_DIR", legacy_cfg))
                stack.enter_context(mock.patch.object(db, "LEGACY_DSN_FILE", legacy_cfg / "dsn"))
                stack.enter_context(mock.patch.object(db, "_MIGRATION_DONE", False))
                with self.assertRaises(RuntimeError):
                    got = db.resolve_dsn()
                    self.fail(f"resolved {got.split('@')[-1]!r} from the legacy file")


class DbStatusErrorTextTest(unittest.TestCase):
    def test_status_error_does_not_quote_the_password(self):
        import io
        from argparse import Namespace

        from khipu import cli, db

        dsn = f"postgres://u:{SECRET}@h/d"
        buf = io.StringIO()
        with mock.patch.object(db, "dsn_configured", return_value=True), \
                mock.patch.object(db, "resolve_dsn", return_value=dsn), \
                mock.patch.object(db, "connect", side_effect=RuntimeError(f'bad token: "{SECRET}"')), \
                contextlib.redirect_stdout(buf):
            rc = cli.cmd_db(Namespace(db_cmd="status"))
        self.assertEqual(rc, 1)
        self.assertNotIn(SECRET, buf.getvalue())


class BackupCarriesNoCredentialsTest(unittest.TestCase):
    def test_backup_archive_leaves_out_the_dsn_and_gateway_token(self):
        from khipu import paths

        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            data.mkdir()
            (data / "dsn").write_text(f"postgres://u:{SECRET}@h/d\n")
            (data / "gateway-token").write_text(SECRET)
            (data / "root.crt").write_text("cert")
            with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data)}):
                out = paths.backup_local(dest=Path(td) / "out")
            with zipfile.ZipFile(out["archive"]) as zf:
                self.assertEqual(zf.namelist(), ["root.crt"])


class DataDirPointerDoesNotSeedDsnTest(unittest.TestCase):
    def test_setting_the_data_dir_copies_the_cert_but_not_the_dsn(self):
        from khipu import paths

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            default, legacy, chosen = root / "khipu", root / "alzy", root / "chosen"
            default.mkdir()
            (default / "dsn").write_text(f"postgres://u:{SECRET}@h/d\n")
            (default / "root.crt").write_text("cert")
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {}))
                for name in ("KHIPU_ISOLATED_DIR", "KHIPU_DATA_DIR", "ALZY_DATA_DIR"):
                    os.environ.pop(name, None)
                stack.enter_context(mock.patch.object(paths, "DEFAULT_DIR", default))
                stack.enter_context(mock.patch.object(paths, "LEGACY_DIR", legacy))
                paths.set_data_dir(chosen)
            self.assertFalse((chosen / "dsn").exists(), "dsn copied into the chosen folder")
            self.assertEqual((chosen / "root.crt").read_text(), "cert")


class SnapshotLegacyDsnFallbackTest(unittest.TestCase):
    def test_file_dsn_ignores_the_legacy_file_past_an_explicit_data_dir(self):
        from khipu import hub_snapshot

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "legacy-dsn").write_text("postgres://legacy-user:pw@legacy-host/legacy\n")
            empty = root / "empty"
            empty.mkdir()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"KHIPU_KEYCHAIN": "0", "KHIPU_DATA_DIR": str(empty)}))
                for name in ("KHIPU_ISOLATED_DIR", "KHIPU_DSN_FILE", "ALZY_DSN_FILE"):
                    os.environ.pop(name, None)
                stack.enter_context(mock.patch.object(hub_snapshot, "_LEGACY_DSN_FILE", root / "legacy-dsn"))
                self.assertIsNone(hub_snapshot._file_dsn())


class LegacyMigrationPastExplicitDataDirTest(unittest.TestCase):
    def test_a_read_does_not_copy_the_legacy_dsn_into_the_config_dir(self):
        from khipu import keychain

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_cfg, cfg, data = root / "alzy", root / "khipu", root / "data"
            legacy_cfg.mkdir()
            data.mkdir()
            (legacy_cfg / "dsn").write_text("postgres://legacy-user:pw@legacy-host/legacy\n")
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"KHIPU_KEYCHAIN": "0", "KHIPU_DATA_DIR": str(data)}))
                for name in ("KHIPU_ISOLATED_DIR", "KHIPU_DSN_FILE", "ALZY_DSN_FILE"):
                    os.environ.pop(name, None)
                stack.enter_context(mock.patch.object(keychain, "CONFIG_DIR", cfg))
                stack.enter_context(mock.patch.object(keychain, "LEGACY_CONFIG_DIR", legacy_cfg))
                keychain.migrate_legacy_secrets()
            self.assertFalse((cfg / "dsn").exists(), "legacy dsn copied into the config dir")
