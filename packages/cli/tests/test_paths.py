"""khipu.paths decides where every local write lands — the DSN, the root cert,
the backups. It had no tests until the 2026-08-17 audit, which found two real
bugs in it (both regression-guarded below).

Everything here runs under a temp KHIPU_DATA_DIR / patched DEFAULT_DIR; nothing
touches the real ~/.config/khipu.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from khipu import paths


class DataDirResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.default = self.root / "config" / "khipu"
        self.default.mkdir(parents=True)
        self._p = mock.patch.object(paths, "DEFAULT_DIR", self.default)
        self._p.start()
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": "", "ALZY_DATA_DIR": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._p.stop()
        self.tmp.cleanup()

    def _pointer(self, payload):
        (self.default / paths.POINTER_NAME).write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )

    def test_no_pointer_no_env_uses_the_default(self):
        self.assertEqual(paths.data_dir(), self.default)

    def test_env_override_wins_over_pointer(self):
        self._pointer({"path": str(self.root / "from-pointer")})
        with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.root / "from-env")}):
            self.assertEqual(paths.data_dir(), self.root / "from-env")

    def test_legacy_alzy_env_still_honored(self):
        with mock.patch.dict(os.environ, {"ALZY_DATA_DIR": str(self.root / "legacy")}):
            self.assertEqual(paths.data_dir(), self.root / "legacy")

    def test_pointer_is_followed(self):
        self._pointer({"path": str(self.root / "elsewhere")})
        self.assertEqual(paths.data_dir(), self.root / "elsewhere")

    def test_pointer_with_tilde_is_expanded(self):
        self._pointer({"path": "~/khipu-data"})
        self.assertEqual(paths.data_dir(), Path.home() / "khipu-data")

    # --- regression: audit 2026-08-17 -------------------------------------------

    def test_pointer_missing_its_path_key_falls_back_and_never_returns_cwd(self):
        """Path("") is PosixPath("."), whose str() is truthy — so a malformed
        pointer used to resolve the data dir to the CURRENT WORKING DIRECTORY,
        which is where the `dsn` file (Postgres credentials) would be written."""
        self._pointer({"updated_at": "2026-08-17T00:00:00Z"})
        self.assertEqual(paths.data_dir(), self.default)
        self.assertNotEqual(paths.data_dir(), Path("."))

    def test_pointer_with_empty_or_blank_path_falls_back(self):
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                self._pointer({"path": bad})
                self.assertEqual(paths.data_dir(), self.default)

    def test_corrupt_pointer_falls_back(self):
        self._pointer("{not json")
        self.assertEqual(paths.data_dir(), self.default)


class ImportLocalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "data"
        self.target.mkdir()
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.target)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_directory_import_copies_files_and_locks_down_the_dsn(self):
        src = self.root / "src"
        (src / "sub").mkdir(parents=True)
        (src / "dsn").write_text("postgres://x")
        (src / "sub" / "a.txt").write_text("a")
        out = paths.import_local(source=src)
        self.assertEqual(out["imported"], 2)
        self.assertTrue((self.target / "dsn").is_file())
        self.assertEqual(oct((self.target / "dsn").stat().st_mode)[-3:], "600")

    def test_zip_import_copies_files(self):
        z = self.root / "b.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("root.crt", "cert")
        self.assertEqual(paths.import_local(source=z)["imported"], 1)
        self.assertTrue((self.target / "root.crt").is_file())

    def test_merge_false_does_not_overwrite(self):
        (self.target / "dsn").write_text("original")
        src = self.root / "src"
        src.mkdir()
        (src / "dsn").write_text("replacement")
        paths.import_local(source=src, merge=False)
        self.assertEqual((self.target / "dsn").read_text(), "original")

    def test_bad_source_raises(self):
        with self.assertRaises(RuntimeError):
            paths.import_local(source=self.root / "nope.tar")

    # --- regression: audit 2026-08-17 -------------------------------------------

    def test_zip_slip_parent_escape_is_refused(self):
        z = self.root / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../escaped.txt", "pwned")
        with self.assertRaises(RuntimeError):
            paths.import_local(source=z)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_zip_slip_sibling_prefix_escape_is_refused(self):
        """The old guard was a string prefix check: with target /a/b, the path
        "/a/bc/evil" startswith("/a/b") and sailed straight through."""
        sibling = self.root / "datacache"          # shares the "data" prefix
        z = self.root / "evil2.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../datacache/evil.txt", "pwned")
        with self.assertRaises(RuntimeError):
            paths.import_local(source=z)
        self.assertFalse((sibling / "evil.txt").exists())


class BackupAndStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "dsn").write_text("postgres://x")
        (self.data / "root.crt").write_text("cert")
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.data)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_backup_to_a_directory_names_the_archive(self):
        out = paths.backup_local(dest=self.root / "backups")
        self.assertTrue(out["ok"])
        self.assertEqual(out["files"], 2)
        z = Path(out["archive"])
        self.assertTrue(z.is_file())
        self.assertTrue(z.name.startswith("khipu-local-") and z.suffix == ".zip")
        with zipfile.ZipFile(z) as zf:
            self.assertEqual(sorted(zf.namelist()), ["dsn", "root.crt"])

    def test_backup_roundtrips_into_a_fresh_data_dir(self):
        archive = Path(paths.backup_local(dest=self.root / "b")["archive"])
        fresh = self.root / "fresh"
        fresh.mkdir()
        with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(fresh)}):
            self.assertEqual(paths.import_local(source=archive)["imported"], 2)
            self.assertEqual((fresh / "dsn").read_text(), "postgres://x")

    def test_status_reports_the_resolved_dir_and_files(self):
        st = paths.paths_status()
        self.assertEqual(st["data_dir"], str(self.data))
        self.assertTrue(st["override_env"])
        self.assertEqual(sorted(f["path"] for f in st["files"]), ["dsn", "root.crt"])


if __name__ == "__main__":
    unittest.main()
