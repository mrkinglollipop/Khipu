"""components_matrix select_compat_row and row picking."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import components_matrix as cm


SAMPLE_ROWS = [
    {
        "khipu_app_min": "0.3.0",
        "graphify_semver": "1.0.0",
        "graphify_tarball_url": "https://example.com/g1.tar.gz",
        "postgres_image": "ghcr.io/mrkinglollipop/khipu-postgres:19beta3-pgvector",
        "pgvector_min": "0.8.6",
    },
    {
        "khipu_app_min": "0.3.0",
        "graphify_semver": "1.1.0",
        "graphify_tarball_url": "https://example.com/g2.tar.gz",
        "postgres_image": "ghcr.io/mrkinglollipop/khipu-postgres:19beta4-pgvector",
        "pgvector_min": "0.8.6",
    },
]


class SelectCompatRowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app_support = Path(self.tmp.name) / "Application Support" / "Khipu"
        self.app_support.mkdir(parents=True)
        self.bundled = Path(self.tmp.name) / "info.json"
        self.bundled.write_text(json.dumps({"matrix": SAMPLE_ROWS}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    @mock.patch.object(cm, "application_support_dir")
    def test_local_picks_newest_major19_image(self, app_support):
        app_support.return_value = self.app_support

        bundled_path = Path(self.tmp.name) / "info.json"
        bundled_path.write_text(json.dumps({"matrix": SAMPLE_ROWS}), encoding="utf-8")

        with mock.patch.object(cm, "bundled_matrix_path", return_value=bundled_path), mock.patch.object(
            cm, "refresh_matrix_cache"
        ), mock.patch.object(cm, "khipu_app_version", return_value="0.3.0"):
            out = cm.select_compat_row("local_docker", refresh=False)
        self.assertTrue(out["ok"])
        self.assertEqual(
            out["pending"]["postgres_image"],
            "ghcr.io/mrkinglollipop/khipu-postgres:19beta4-pgvector",
        )
        self.assertEqual(out["pending"]["graphify_semver"], "1.1.0")
        saved = json.loads(
            (self.app_support / "versions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["pending"]["pgvector_min"], "0.8.6")

    @mock.patch.object(cm, "application_support_dir")
    def test_remote_ignores_postgres_image(self, app_support):
        app_support.return_value = self.app_support

        bundled_path = Path(self.tmp.name) / "info.json"
        bundled_path.write_text(json.dumps({"matrix": SAMPLE_ROWS}), encoding="utf-8")

        with mock.patch.object(cm, "bundled_matrix_path", return_value=bundled_path), mock.patch.object(
            cm, "refresh_matrix_cache"
        ), mock.patch.object(cm, "khipu_app_version", return_value="0.3.0"):
            out = cm.select_compat_row(
                "remote",
                pgvector_extversion="0.8.6",
                refresh=False,
            )
        self.assertTrue(out["ok"])
        self.assertNotIn("postgres_image", out["pending"])
        self.assertEqual(out["pending"]["graphify_semver"], "1.1.0")

    @mock.patch.object(cm, "application_support_dir")
    def test_remote_mode_recorded_even_without_server_version(self, app_support):
        """2026-09-05 root cause: a Mac whose database was connected before
        the app recorded a mode had NO `postgres` dict at all, because this
        only wrote `postgres.mode = "remote"` when `server_version` was
        known. The mode must be recorded whenever mode is remote, even with
        server_version and pgvector still unknown."""
        app_support.return_value = self.app_support

        bundled_path = Path(self.tmp.name) / "info.json"
        bundled_path.write_text(json.dumps({"matrix": SAMPLE_ROWS}), encoding="utf-8")

        with mock.patch.object(cm, "bundled_matrix_path", return_value=bundled_path), mock.patch.object(
            cm, "refresh_matrix_cache"
        ), mock.patch.object(cm, "khipu_app_version", return_value="0.3.0"):
            out = cm.select_compat_row("remote", refresh=False)
        self.assertTrue(out["ok"])
        saved = json.loads(
            (self.app_support / "versions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["postgres"]["mode"], "remote")
        self.assertNotIn("server_version", saved["postgres"])
        self.assertNotIn("pgvector", saved["postgres"])

    @mock.patch.object(cm, "khipu_app_version", return_value="0.3.0")
    def test_forbidden_image_rejected(self, _app_ver):
        self.assertTrue(
            cm.is_forbidden_postgres_image("docker.io/library/postgres:latest")
        )
        self.assertTrue(
            cm.is_forbidden_postgres_image("alzy/postgres:19beta3-pgvector")
        )


if __name__ == "__main__":
    unittest.main()


class AppVersionFallbackTest(unittest.TestCase):
    """Audit 2026-09-04: the fallback hardcoded "0.3.14" and kept reporting a
    shipped release long after 0.3.16. The desktop app passes KHIPU_APP_VERSION;
    with nothing to read, the honest answer is "unknown", not a stale number."""

    def test_env_wins(self):
        with mock.patch.dict("os.environ", {"KHIPU_APP_VERSION": "0.4.1"}):
            self.assertEqual(cm.khipu_app_version(), "0.4.1")

    def test_versions_json_is_next(self):
        with mock.patch.dict("os.environ", {"KHIPU_APP_VERSION": "", "KHIPU_VERSION": ""}), \
                mock.patch.object(cm, "read_versions", return_value={"khipu_app": "0.3.16"}):
            self.assertEqual(cm.khipu_app_version(), "0.3.16")

    def test_unknown_when_nothing_reports_a_version(self):
        with mock.patch.dict("os.environ", {"KHIPU_APP_VERSION": "", "KHIPU_VERSION": ""}), \
                mock.patch.object(cm, "read_versions", return_value={}):
            self.assertEqual(cm.khipu_app_version(), "unknown")
