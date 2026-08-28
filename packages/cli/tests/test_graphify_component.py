"""Graphify component install + empty-sources smoke tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import jobs
from khipu.components_graphify import _extract_tarball, install_graphify


ROOT = Path(__file__).resolve().parents[3]
GRAPHIFY_SRC = ROOT / "third_party" / "graphify"


class GraphifyTarballTest(unittest.TestCase):
    def test_graphify_scripts_compile(self):
        for name in (
            "graphify_nightly.py",
            "build_graph.py",
            "code_ast_extractor.py",
            "code_semantic_extractor.py",
            "embed_corpus.py",
        ):
            path = GRAPHIFY_SRC / name
            self.assertTrue(path.is_file(), msg=f"missing {name}")
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_version_file(self):
        version = (GRAPHIFY_SRC / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "1.0.0")


class GraphifyInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app_support = Path(self.tmp.name) / "Application Support" / "Khipu"
        self.app_support.mkdir(parents=True)
        self.archive = self._build_tarball()

    def tearDown(self):
        self.tmp.cleanup()

    def _build_tarball(self) -> Path:
        out = Path(self.tmp.name) / "khipu-graphify-1.0.0.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            tar.add(GRAPHIFY_SRC, arcname="graphify")
        return out

    @mock.patch("khipu.components_graphify._download")
    @mock.patch("khipu.components_graphify.match_row_for_install")
    @mock.patch("khipu.components_graphify._ensure_empty_sources")
    @mock.patch("khipu.components_graphify.application_support_dir")
    @mock.patch("khipu.components_graphify.read_versions")
    @mock.patch("khipu.components_graphify.write_versions")
    def test_install_from_pending(
        self,
        write_versions,
        read_versions,
        support_dir,
        ensure_sources,
        match_row,
        download,
    ):
        support_dir.return_value = self.app_support
        pending = {
            "graphify_semver": "1.0.0",
            "graphify_tarball_url": "https://example.invalid/khipu-graphify-1.0.0.tar.gz",
            "postgres_image": "ghcr.io/mrkinglollipop/khipu-postgres:19beta3-pgvector",
            "pgvector_min": "0.8.6",
        }
        read_versions.return_value = {
            "pending": pending,
            "postgres": {"mode": "local_docker", "image": pending["postgres_image"]},
        }
        match_row.return_value = dict(pending, khipu_app_min="0.3.0")

        def fake_download(url: str, dest: Path) -> None:
            dest.write_bytes(self.archive.read_bytes())

        download.side_effect = fake_download

        result = install_graphify(first_run=True)
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["semver"], "1.0.0")
        script = self.app_support / "graphify" / "1.0.0" / "graphify_nightly.py"
        self.assertTrue(script.is_file())
        write_versions.assert_called()
        saved = write_versions.call_args[0][0]
        self.assertIn("graphify", saved)
        self.assertNotIn("pending", saved)

    @mock.patch("khipu.components_matrix.select_compat_row")
    @mock.patch("khipu.components_graphify._download")
    @mock.patch("khipu.components_graphify.match_row_for_install")
    @mock.patch("khipu.components_graphify._ensure_empty_sources")
    @mock.patch("khipu.components_graphify.application_support_dir")
    @mock.patch("khipu.components_graphify.read_versions")
    @mock.patch("khipu.components_graphify.write_versions")
    def test_install_selects_compat_when_pending_missing(
        self,
        write_versions,
        read_versions,
        support_dir,
        ensure_sources,
        match_row,
        download,
        select_row,
    ):
        del ensure_sources
        support_dir.return_value = self.app_support
        pending = {
            "graphify_semver": "1.0.0",
            "graphify_tarball_url": "https://example.invalid/khipu-graphify-1.0.0.tar.gz",
            "pgvector_min": "0.8.6",
        }
        read_versions.side_effect = [
            {"postgres": {"mode": "remote"}},
            {"pending": pending, "postgres": {"mode": "remote"}},
        ]
        select_row.return_value = {"ok": True, "pending": pending}
        match_row.return_value = dict(pending, khipu_app_min="0.3.0")

        def fake_download(url: str, dest: Path) -> None:
            dest.write_bytes(self.archive.read_bytes())

        download.side_effect = fake_download

        result = install_graphify(first_run=True)
        self.assertTrue(result["ok"], msg=result)
        select_row.assert_called_once()
        self.assertEqual(select_row.call_args.args[0], "remote")

    @mock.patch("khipu.components_matrix.select_compat_row")
    @mock.patch("khipu.components_graphify.read_versions")
    def test_install_still_errors_when_select_cannot_write_pending(
        self, read_versions, select_row
    ):
        read_versions.return_value = {}
        select_row.return_value = {"ok": False, "error": "matrix_no_matching_row"}
        result = install_graphify(first_run=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_pending_graphify")
        select_row.assert_called_once()
        self.assertEqual(select_row.call_args.args[0], "remote")


class GraphifyEmptySourcesSmokeTest(unittest.TestCase):
    def test_graphify_nightly_skips_with_empty_sources(self):
        with tempfile.TemporaryDirectory() as td:
            app_support = Path(td) / "Application Support" / "Khipu"
            state = app_support / "state"
            state.mkdir(parents=True)
            resolved = {
                "schema_version": 2,
                "collectors": {key: False for key in (
                    "tickers", "skills", "agents", "reports", "memory_topics",
                    "predictive_gates", "frozen_tell", "hardcoded_data_sources",
                    "hardcoded_notion_dbs", "biblical", "model_call_log",
                    "code_ast", "code_semantic",
                )},
                "code_roots": [],
            }
            resolved_path = app_support / "graph_sources.resolved.json"
            resolved_path.write_text(json.dumps(resolved), encoding="utf-8")
            env = os.environ.copy()
            env["KHIPU_GRAPH_SOURCES_RESOLVED"] = str(resolved_path)
            env["KHIPU_ROOT"] = str(ROOT)
            env["PYTHONPATH"] = str(ROOT / "packages" / "cli")
            proc = subprocess.run(
                [sys.executable, str(GRAPHIFY_SRC / "graphify_nightly.py")],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            json_lines = [
                line for line in proc.stdout.splitlines() if line.strip().startswith("{")
            ]
            self.assertTrue(json_lines, msg=proc.stdout)
            payload = json.loads(json_lines[-1])
            self.assertTrue(payload.get("ok"))
            self.assertEqual(payload.get("skipped"), "no_sources")


class TarSlipTest(unittest.TestCase):
    def test_extract_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            archive = td_path / "evil.tar.gz"
            dest = td_path / "out"
            payload = b"boom"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo(name="../evil.txt")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            with self.assertRaises(tarfile.TarError):
                _extract_tarball(archive, dest)
            self.assertFalse((td_path / "evil.txt").exists())

    def test_extract_rejects_absolute_path(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            archive = td_path / "evil.tar.gz"
            dest = td_path / "out"
            payload = b"nope"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo(name="/tmp/khipu-tar-slip.txt")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            with self.assertRaises(tarfile.TarError):
                _extract_tarball(archive, dest)


class GraphifyPathResolutionTest(unittest.TestCase):
    @mock.patch.object(jobs, "_application_support_dir")
    def test_jobs_reads_installed_graphify(self, support_dir):
        with tempfile.TemporaryDirectory() as td:
            app_support = Path(td)
            support_dir.return_value = app_support
            graphify_root = app_support / "graphify" / "1.0.0"
            graphify_root.mkdir(parents=True)
            script = graphify_root / "graphify_nightly.py"
            script.write_text("# graphify\n", encoding="utf-8")
            (app_support / "versions.json").write_text(
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


if __name__ == "__main__":
    unittest.main()
