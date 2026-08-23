"""Install-default path resolution — no maintainer Cloud Storage defaults."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import jobs, sources


class GraphifyPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app_support = Path(self.tmp.name) / "Application Support" / "Khipu"
        self.app_support.mkdir(parents=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "KHIPU_GRAPHIFY_NIGHTLY": "",
                "KHIPU_CONSOLIDATE_NIGHTLY": "",
                "KHIPU_CONSOLIDATE_MONTHLY": "",
                "KHIPU_BUILD_INDEX": "",
            },
            clear=False,
        )
        self._env.start()
        for key in list(os.environ):
            if key.startswith("KHIPU_") and key not in {
                "KHIPU_GRAPHIFY_NIGHTLY",
                "KHIPU_CONSOLIDATE_NIGHTLY",
                "KHIPU_CONSOLIDATE_MONTHLY",
                "KHIPU_BUILD_INDEX",
            }:
                os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    @mock.patch.object(jobs, "_plist_env_path", return_value=None)
    @mock.patch.object(jobs, "_application_support_dir")
    def test_graphify_nightly_path_none_without_env_or_versions(self, support_dir, _plist):
        # The installed launchd plist is the third resolution source; a fresh
        # machine has none, which is what this test models.
        support_dir.return_value = self.app_support
        self.assertIsNone(jobs.graphify_nightly_path())

    @mock.patch.object(jobs, "_application_support_dir")
    def test_graphify_nightly_path_reads_versions_json(self, support_dir):
        support_dir.return_value = self.app_support
        graphify_root = self.app_support / "graphify" / "1.0.0"
        graphify_root.mkdir(parents=True)
        script = graphify_root / "graphify_nightly.py"
        script.write_text("# graphify\n", encoding="utf-8")
        (self.app_support / "versions.json").write_text(
            json.dumps(
                {
                    "graphify": {
                        "semver": "1.0.0",
                        "path": str(graphify_root),
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(jobs.graphify_nightly_path(), script)

    @mock.patch.object(jobs, "_application_support_dir")
    def test_graphify_nightly_path_falls_back_to_env(self, support_dir):
        support_dir.return_value = self.app_support
        script = self.app_support / "graphify_nightly.py"
        script.write_text("# graphify\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"KHIPU_GRAPHIFY_NIGHTLY": str(script)}):
            self.assertEqual(jobs.graphify_nightly_path(), script)


class GraphBuildCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app_support = self.dir / "Application Support" / "Khipu"
        self.app_support.mkdir(parents=True)
        self._support = mock.patch.object(
            jobs, "_application_support_dir", return_value=self.app_support
        )
        self._support.start()

    def tearDown(self):
        self._support.stop()
        self.tmp.cleanup()

    def _run_graph_build(self) -> subprocess.CompletedProcess[str]:
        cli_root = Path(__file__).resolve().parents[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith("KHIPU_")}
        env["PYTHONPATH"] = str(cli_root)
        env["KHIPU_DATA_DIR"] = str(self.dir / "data")
        env["HOME"] = str(self.dir)  # no installed LaunchAgents plist to fall back to
        return subprocess.run(
            [sys.executable, "-m", "khipu.cli", "graph-build"],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_graph_build_json_error_when_graphify_missing(self):
        jobs.GRAPHIFY_NIGHTLY = None
        result = self._run_graph_build()
        self.assertEqual(result.returncode, 2, msg=result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "graphify_not_installed")
        self.assertEqual(payload["fix"], "khipu components install graphify")
        self.assertNotIn("/Volumes/Cloud Storage", result.stdout)


class InstallDefaultPathsTest(unittest.TestCase):
    def test_jobs_constants_have_no_cloud_storage_defaults(self):
        for name in (
            "CONSOLIDATE_NIGHTLY",
            "CONSOLIDATE_MONTHLY",
            "BUILD_INDEX",
        ):
            value = getattr(jobs, name)
            if value is not None:
                self.assertNotIn(
                    "/Volumes/Cloud Storage",
                    str(value),
                    msg=f"{name} still embeds maintainer path",
                )
        self.assertIsNone(jobs.GRAPHIFY_NIGHTLY)

    def test_sources_default_document_is_empty(self):
        doc = sources.default_document()
        self.assertEqual(doc["sources"], [])

    def test_sources_resolved_default_under_data_dir(self):
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.dict(
                os.environ,
                {"KHIPU_DATA_DIR": td},
                clear=False,
            ),
        ):
            resolved = sources.resolved_path()
            self.assertEqual(
                resolved,
                Path(td) / "graph_sources.resolved.json",
            )
            self.assertNotIn("/Volumes/Cloud Storage", str(resolved))

    def test_no_cloud_storage_in_install_defaults_except_topic_graph(self):
        """Grep gate: Cloud Storage strings are not install defaults.

        Exempt ``topic_graph.py`` ``VOLUME_ROOT`` — wiki-path peeler, not an
        install default. Do not rename it in the portable-DMG slice.
        """
        root = Path(__file__).resolve().parents[1] / "khipu"
        hits: list[str] = []
        for path in root.rglob("*.py"):
            if path.name == "topic_graph.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "/Volumes/Cloud Storage" in text:
                hits.append(str(path.relative_to(root)))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
