"""Portable install doctor contract — no legacy sync/producer plists by default."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from khipu import git_sync_health, graph_sync
from khipu.drift import backup_health


class PortableProducerSyncHostTest(unittest.TestCase):
    def test_sync_host_false_without_scheduled_jobs(self):
        with mock.patch.dict(os.environ, {"KHIPU_GIT_SYNC_HOST": ""}, clear=False), mock.patch(
            "khipu.components_matrix.read_versions", return_value={}
        ):
            st = git_sync_health.status()
            self.assertTrue(st["ok"])
            self.assertFalse(st["applicable"])

    def test_sync_host_true_when_scheduled_nightly(self):
        versions = {"scheduled_jobs": {"nightly": True}}
        with mock.patch.dict(os.environ, {"KHIPU_GIT_SYNC_HOST": ""}, clear=False), mock.patch(
            "khipu.components_matrix.read_versions", return_value=versions
        ):
            self.assertTrue(git_sync_health.is_sync_host())

    def test_graph_producer_false_without_opt_in(self):
        with mock.patch.dict(os.environ, {"KHIPU_GRAPH_PRODUCER": ""}, clear=False), mock.patch(
            "khipu.components_matrix.read_versions", return_value={}
        ):
            self.assertFalse(graph_sync.is_graph_producer())

    def test_graph_producer_true_when_scheduled_graph_build(self):
        versions = {"scheduled_jobs": {"graph_build": True}}
        with mock.patch.dict(os.environ, {"KHIPU_GRAPH_PRODUCER": ""}, clear=False), mock.patch(
            "khipu.components_matrix.read_versions", return_value=versions
        ):
            self.assertTrue(graph_sync.is_graph_producer())


class LocalBackupHealthTest(unittest.TestCase):
    def test_local_docker_uses_pg_dump_not_walg_only_message(self):
        events = {
            "pg_dump": {
                "status": "ok",
                "detail": {"path": "/tmp/x.dump"},
                "created_at": "2026-08-21T12:00:00+00:00",
                "age_seconds": 60,
            },
            "restore_drill": {"status": "ok", "created_at": "2026-08-21T12:00:00+00:00", "age_seconds": 60},
        }

        def fake_latest(cur, kind):
            return events.get(kind)

        with mock.patch("khipu.drift.connect") as conn_ctx, mock.patch(
            "khipu.drift._latest_ops_event", side_effect=fake_latest
        ), mock.patch("khipu.drift._postgres_backup_mode", return_value="local_docker"):
            conn = conn_ctx.return_value.__enter__.return_value
            cur = conn.cursor.return_value.__enter__.return_value
            cur.fetchone.return_value = (True,)
            out = backup_health()
        self.assertTrue(out["ok"])
        self.assertEqual(out["postgres_mode"], "local_docker")
        self.assertIn("pg_dump", json.dumps(out))


class LaunchdGenTest(unittest.TestCase):
    def test_render_plist_uses_application_support_not_cloud_storage(self):
        from khipu import launchd_gen

        with mock.patch.object(launchd_gen, "render_context") as ctx:
            ctx.return_value = {
                "KHIPU_ROOT": "/Applications/Khipu.app/Contents/Resources/khipu",
                "KHIPU_PYTHON": "/Applications/Khipu.app/Contents/Resources/khipu/python/bin/python3.11",
                "PYTHONPATH": "/Applications/Khipu.app/Contents/Resources/khipu/packages/cli",
                "WORKING_DIRECTORY": "/Users/you/Library/Application Support/Khipu",
            }
            raw = launchd_gen.render_plist("nightly").decode("utf-8")
        self.assertIn("Application Support/Khipu", raw)
        self.assertNotIn("/Volumes/Cloud Storage", raw)
        self.assertNotIn("{{KHIPU_ROOT}}", raw)
