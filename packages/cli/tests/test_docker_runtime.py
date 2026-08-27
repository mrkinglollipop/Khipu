"""Docker CLI discovery + ensure_docker (no live Docker Desktop download)."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import docker_runtime as dr


def _fake_docker_bin(root: Path) -> Path:
    path = root / "docker"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class DockerCliTest(unittest.TestCase):
    def test_finds_executable_from_which(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _fake_docker_bin(Path(tmp))
            with mock.patch("khipu.docker_runtime.shutil.which", return_value=str(fake)):
                self.assertEqual(dr.docker_cli(), fake)

    def test_missing_cli_is_docker_not_found(self):
        with mock.patch("khipu.docker_runtime.shutil.which", return_value=None), mock.patch.object(
            dr, "DOCKER_APP", Path("/tmp/khipu-no-docker.app")
        ), mock.patch("khipu.docker_runtime.os.access", return_value=False), mock.patch(
            "khipu.docker_runtime.Path.is_file", return_value=False
        ):
            out = dr.docker_available()
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "docker_not_found")
        self.assertEqual(out["error"], "docker_not_found")


class DockerAvailableTest(unittest.TestCase):
    def test_daemon_stopped_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _fake_docker_bin(Path(tmp))
            proc = mock.Mock()
            proc.returncode = 1
            proc.stderr = "Cannot connect to the Docker daemon. Is the docker daemon running?"
            proc.stdout = ""
            with mock.patch("khipu.docker_runtime.docker_cli", return_value=fake), mock.patch(
                "khipu.docker_runtime.docker_app_installed", return_value=True
            ), mock.patch("khipu.docker_runtime._run", return_value=proc):
                out = dr.docker_available()
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "docker_daemon_stopped")
        self.assertTrue(out["app_installed"])


class EnsureDockerTest(unittest.TestCase):
    def test_already_ok(self):
        with mock.patch(
            "khipu.docker_runtime.docker_available",
            return_value={"ok": True, "app_installed": True, "cli": "/usr/local/bin/docker"},
        ):
            out = dr.ensure_docker(install=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "already_ok")

    def test_need_install_when_no_runtime(self):
        missing = {
            "ok": False,
            "error": "docker_not_found",
            "code": "docker_not_found",
            "app_installed": False,
            "cli": None,
        }
        with mock.patch("khipu.docker_runtime.docker_available", return_value=missing), mock.patch(
            "khipu.docker_runtime._start_container_runtime", return_value=None
        ):
            out = dr.ensure_docker(install=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["action"], "need_install")
        self.assertEqual(out["code"], "docker_not_found")

    def test_starts_existing_app(self):
        stopped = {
            "ok": False,
            "code": "docker_daemon_stopped",
            "error": "daemon down",
            "app_installed": True,
            "cli": "/usr/local/bin/docker",
        }
        ready = {**stopped, "ok": True, "error": None, "code": None}
        with mock.patch(
            "khipu.docker_runtime.docker_available",
            side_effect=[stopped, ready],
        ), mock.patch(
            "khipu.docker_runtime._start_container_runtime", return_value="Docker"
        ), mock.patch.dict(os.environ, {"KHIPU_DOCKER_WAIT_S": "0"}):
            out = dr.ensure_docker(install=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "started_docker")

    def test_install_downloads_and_copies(self):
        missing = {
            "ok": False,
            "error": "docker_not_found",
            "code": "docker_not_found",
            "app_installed": False,
            "cli": None,
        }
        ready = {"ok": True, "app_installed": True, "cli": "/usr/local/bin/docker"}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with mock.patch(
                "khipu.docker_runtime.docker_available",
                side_effect=[missing, ready],
            ), mock.patch(
                "khipu.docker_runtime._start_container_runtime",
                side_effect=[None, "Docker"],
            ), mock.patch(
                "khipu.docker_runtime._download_docker_dmg",
                return_value={"ok": True, "path": str(cache / "Docker.dmg"), "bytes": 12},
            ), mock.patch(
                "khipu.docker_runtime._install_docker_app_from_dmg",
                return_value={"ok": True, "path": "/Applications/Docker.app"},
            ), mock.patch(
                "khipu.docker_runtime.application_support_dir", return_value=cache
            ), mock.patch.dict(os.environ, {"KHIPU_DOCKER_WAIT_S": "0"}):
                (cache / "cache").mkdir()
                (cache / "cache" / "Docker.dmg").write_bytes(b"x")
                out = dr.ensure_docker(install=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "installed_docker_desktop")

    def test_official_url_is_docker_cdn(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KHIPU_DOCKER_DMG_URL", None)
            url = dr.docker_desktop_dmg_url()
        self.assertTrue(url.startswith("https://desktop.docker.com/mac/main/"))
        self.assertTrue(url.endswith("/Docker.dmg"))


if __name__ == "__main__":
    unittest.main()
