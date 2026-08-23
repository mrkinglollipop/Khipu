"""Local Postgres installer — versions.json image persist."""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

import khipu.db  # noqa: F401 — load submodule so mock.patch("khipu.db...") resolves

from khipu.components_postgres import (
    DOCKER_BUILD_TIMEOUT_S,
    DOCKER_PULL_TIMEOUT_S,
    _local_dsn,
    _read_dsn_password_port,
    components_status,
    install_local_postgres,
    upgrade_postgres,
)

IMAGE = "ghcr.io/mrkinglollipop/khipu-postgres:19beta3-pgvector"


class InstallLocalPostgresImageTest(unittest.TestCase):
    def test_build_timeout_is_longer_than_pull(self):
        self.assertEqual(DOCKER_PULL_TIMEOUT_S, 600)
        self.assertEqual(DOCKER_BUILD_TIMEOUT_S, 1800)
        self.assertGreater(DOCKER_BUILD_TIMEOUT_S, DOCKER_PULL_TIMEOUT_S)

    @mock.patch(
        "khipu.components_postgres._run_migrations",
        return_value={"ok": True, "ran": []},
    )
    @mock.patch("khipu.components_postgres.set_dsn")
    @mock.patch(
        "khipu.components_postgres._probe_local_cluster",
        return_value={"ok": True, "server_version": "19beta3", "pgvector": "0.8.6"},
    )
    @mock.patch("khipu.components_postgres._wait_pg_ready", return_value=True)
    @mock.patch("khipu.components_postgres._docker")
    @mock.patch("khipu.components_postgres.choose_host_port", return_value=54329)
    @mock.patch("khipu.components_postgres.ensure_postgres_image")
    @mock.patch("khipu.components_postgres.disk_headroom_warning", return_value=None)
    @mock.patch(
        "khipu.components_postgres._verify_pending_row",
        return_value={"ok": True, "row": {}},
    )
    @mock.patch("khipu.components_postgres.write_versions")
    @mock.patch("khipu.components_postgres.read_versions")
    @mock.patch("khipu.components_postgres.docker_available", return_value={"ok": True})
    def test_install_writes_postgres_image(
        self,
        _docker_available,
        read_versions,
        write_versions,
        _verify,
        _disk,
        ensure_image,
        _port,
        docker,
        _ready,
        _probe,
        _dsn,
        _migrate,
    ):
        read_versions.return_value = {
            "pending": {
                "postgres_image": IMAGE,
                "pgvector_min": "0.8.6",
                "graphify_semver": "1.0.0",
                "graphify_tarball_url": "https://example.invalid/g.tar.gz",
            }
        }
        ensure_image.return_value = {"ok": True, "image": IMAGE, "source": "pull"}
        proc = mock.Mock()
        proc.returncode = 0
        proc.stderr = ""
        proc.stdout = ""
        docker.return_value = proc

        result = install_local_postgres()
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["image"], IMAGE)
        self.assertTrue(write_versions.called)
        first = write_versions.call_args_list[0][0][0]
        self.assertEqual(first["postgres"]["image"], IMAGE)
        last = write_versions.call_args_list[-1][0][0]
        self.assertEqual(last["postgres"]["image"], IMAGE)
        self.assertEqual(last["postgres"]["mode"], "local_docker")


class LocalDsnPasswordTest(unittest.TestCase):
    def test_local_dsn_quotes_password_roundtrip(self):
        dsn = _local_dsn(54329, "p@ss:word")
        self.assertIn("p%40ss%3Aword", dsn)
        self.assertNotIn("p@ss:word", dsn)
        with mock.patch("khipu.db.resolve_dsn", return_value=dsn):
            result = _read_dsn_password_port({"postgres": {"port": 54329}})
        self.assertEqual(result, {"ok": True, "password": "p@ss:word", "port": 54329})

    def test_read_dsn_unquotes_percent_encoded_password(self):
        dsn = "postgresql://khipu:p%40ss@127.0.0.1:54329/khipu?sslmode=disable"
        with mock.patch("khipu.db.resolve_dsn", return_value=dsn):
            result = _read_dsn_password_port({"postgres": {"port": 54329}})
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["password"], "p@ss")
        self.assertEqual(result["port"], 54329)

    def test_read_dsn_empty_password_does_not_generate(self):
        dsn = "postgresql://khipu:@127.0.0.1:54329/khipu?sslmode=disable"
        with (
            mock.patch("khipu.db.resolve_dsn", return_value=dsn),
            mock.patch("khipu.components_postgres._generate_password") as gen,
        ):
            result = _read_dsn_password_port({"postgres": {"port": 54329}})
        self.assertEqual(result, {"ok": False, "error": "dsn_password_missing"})
        gen.assert_not_called()

    def test_upgrade_postgres_surfaces_missing_dsn_password(self):
        versions = {
            "postgres": {
                "mode": "local_docker",
                "image": IMAGE,
                "port": 54329,
            },
            "graphify": {"semver": "1.0.0"},
        }
        row = {
            "postgres_image": "ghcr.io/mrkinglollipop/khipu-postgres:19beta4-pgvector",
            "graphify_semver": "1.0.0",
            "graphify_tarball_url": "https://example.invalid/g.tar.gz",
            "pgvector_min": "0.8.6",
        }
        with (
            mock.patch(
                "khipu.components_postgres.docker_available", return_value={"ok": True}
            ),
            mock.patch("khipu.components_matrix.refresh_matrix_cache"),
            mock.patch(
                "khipu.components_postgres.read_versions", return_value=versions
            ),
            mock.patch(
                "khipu.components_matrix.effective_matrix", return_value=([row], {})
            ),
            mock.patch("khipu.components_matrix.find_full_row", return_value=row),
            mock.patch(
                "khipu.components_matrix.khipu_app_version", return_value="0.3.0"
            ),
            mock.patch(
                "khipu.components_postgres._read_dsn_password_port",
                return_value={"ok": False, "error": "dsn_password_missing"},
            ),
        ):
            result = upgrade_postgres()
        self.assertEqual(result, {"ok": False, "error": "dsn_password_missing"})


class ComponentsStatusFallbackTest(unittest.TestCase):
    def _base_stack(self, stack, versions):
        stack.enter_context(
            mock.patch("khipu.components_postgres.read_versions", return_value=versions)
        )
        stack.enter_context(
            mock.patch(
                "khipu.components_postgres.docker_available", return_value={"ok": True}
            )
        )
        stack.enter_context(mock.patch("khipu.components_matrix.refresh_matrix_cache"))
        stack.enter_context(
            mock.patch("khipu.components_matrix.effective_matrix", return_value=([], {}))
        )
        stack.enter_context(
            mock.patch("khipu.components_matrix.khipu_app_version", return_value="0.3.1")
        )

    def test_probes_remote_dsn_when_versions_json_has_no_postgres(self):
        versions = {"graphify": {"semver": "1.0.0", "path": "/opt/graphify"}}
        with contextlib.ExitStack() as stack:
            self._base_stack(stack, versions)
            probe = stack.enter_context(
                mock.patch(
                    "khipu.components_postgres.check_remote_postgres",
                    return_value={"ok": True, "server_version": "19.1"},
                )
            )
            result = components_status()
        probe.assert_called_once_with(full=True)
        self.assertEqual(result["postgres"]["mode"], "remote")
        self.assertEqual(result["postgres"]["source"], "dsn")
        self.assertEqual(result["postgres"]["server_version"], "19.1")
        self.assertIsNone(result["postgres_upgrade"])
        self.assertIsNone(result["postgres_probe"])

    def test_records_probe_failure_without_writing_postgres_block(self):
        versions = {"graphify": {"semver": "1.0.0", "path": "/opt/graphify"}}
        with contextlib.ExitStack() as stack:
            self._base_stack(stack, versions)
            stack.enter_context(
                mock.patch(
                    "khipu.components_postgres.check_remote_postgres",
                    return_value={"ok": False, "error": "connection_refused"},
                )
            )
            result = components_status()
        self.assertEqual(result["postgres"], {})
        self.assertEqual(
            result["postgres_probe"], {"ok": False, "error": "connection_refused"}
        )

    def test_falls_back_to_external_graphify_script(self):
        versions = {"postgres": {"mode": "remote", "source": "dsn"}}
        script = mock.Mock()
        script.parent = "/opt/khipu-ops/graphify"
        with contextlib.ExitStack() as stack:
            self._base_stack(stack, versions)
            nightly = stack.enter_context(
                mock.patch("khipu.jobs.graphify_nightly_path", return_value=script)
            )
            result = components_status()
        nightly.assert_called_once_with()
        self.assertEqual(result["graphify"]["semver"], "external")
        self.assertEqual(result["graphify"]["source"], "env")
        self.assertEqual(result["graphify"]["path"], "/opt/khipu-ops/graphify")
        self.assertIsNone(result["graphify_upgrade"])

    def test_no_external_graphify_script_leaves_graphify_empty(self):
        versions = {"postgres": {"mode": "remote", "source": "dsn"}}
        with contextlib.ExitStack() as stack:
            self._base_stack(stack, versions)
            stack.enter_context(
                mock.patch("khipu.jobs.graphify_nightly_path", return_value=None)
            )
            result = components_status()
        self.assertEqual(result["graphify"], {})
        self.assertIsNone(result["graphify_upgrade"])
