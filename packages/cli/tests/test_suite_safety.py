"""Hermetic checks for the suite's live-resource authorization boundaries."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock


def _load(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("safety_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveResourceSafetyTest(unittest.TestCase):
    def test_bundled_semantic_behavior_tests_are_not_skipped(self):
        module = _load("test_code_semantic_extractor")
        self.assertFalse(getattr(module.CodeRootsFromResolvedTest, "__unittest_skip__", False))
        self.assertEqual(module.EXTRACTOR_PATH.resolve(),
                         Path(__file__).resolve().parents[3] / "third_party" / "graphify" / "code_semantic_extractor.py")

    def test_pure_outbox_durability_tests_execute_without_a_database(self):
        with mock.patch("khipu.db.connect", side_effect=AssertionError("database consulted")) as connect:
            module = _load("test_outbox")
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.OutboxDurabilityTest)
            result = unittest.TestResult()
            suite.run(result)
        self.assertEqual(result.testsRun, 3)
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.errors + result.failures, [])
        connect.assert_not_called()

    def test_live_drill_uses_unique_names_and_refuses_existing_resources(self):
        from khipu import components_backup as backup

        with mock.patch.dict(os.environ, {}, clear=True):
            module = _load("test_setup_live")
        names = []
        with mock.patch.dict(os.environ, {"KHIPU_LIVE_TESTS": "1"}), mock.patch.object(
            backup, "_docker", return_value=mock.Mock(returncode=1)
        ) as docker:
            for _ in range(2):
                with module._test_drill_resources():
                    names.append((backup.DRILL_CONTAINER, backup.DRILL_VOLUME))
            self.assertNotEqual(names[0], names[1])
            for container, volume in names:
                self.assertTrue(container.startswith("khipu-test-drill-"))
                self.assertTrue(volume.startswith("khipu-test-pgdata-"))
                self.assertIn(mock.call(["container", "inspect", container], check=False), docker.mock_calls)
            docker.reset_mock()
            docker.return_value.returncode = 0
            with self.assertRaisesRegex(RuntimeError, "refusing to reuse"):
                with module._test_drill_resources():
                    self.fail("existing resources were accepted")
            self.assertEqual(docker.call_count, 1)

    def test_scratch_requires_matching_sentinel_and_accepts_provisioned_database(self):
        from tests import connect_scratch

        nonce = "a" * 32
        name = "khipu_scratch_" + nonce
        with mock.patch.dict(os.environ, {"KHIPU_LIVE_TESTS": "1", "KHIPU_SCRATCH_NONCE": nonce}), \
             mock.patch("psycopg.connect") as connect:
            conn = connect.return_value
            cur = conn.cursor.return_value.__enter__.return_value
            for rows in ([], [(name, "wrong")], [("production_scratch_archive", nonce)]):
                cur.fetchall.return_value = rows
                with self.assertRaisesRegex(ValueError, "sentinel"):
                    connect_scratch("postgresql://u@127.0.0.1/" + name)
            cur.fetchall.side_effect = RuntimeError("missing sentinel table")
            with self.assertRaisesRegex(RuntimeError, "missing sentinel"):
                connect_scratch("postgresql://u@127.0.0.1/" + name)
            cur.fetchall.side_effect = None
            cur.fetchall.return_value = [(name, nonce)]
            self.assertIs(connect_scratch("postgresql://u@127.0.0.1/" + name), conn)
            conn.rollback.assert_called()
            self.assertEqual(conn.close.call_count, 4)

    def test_default_collection_never_probes_docker(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "khipu.components_postgres.docker_available", return_value={"ok": True}
        ) as docker:
            module = _load("test_setup_live")
        docker.assert_not_called()
        self.assertIsNotNone(module._SKIP_REASON)

    def test_scratch_requires_opt_in_before_connecting(self):
        for name in ("test_pg_scratch", "test_briefs_scratch"):
            with self.subTest(module=name), mock.patch.dict(os.environ, {
                "KHIPU_SCRATCH_DSN": "postgresql://u@127.0.0.1/production_scratch_archive",
            }, clear=True), mock.patch("psycopg.connect") as connect:
                module = _load(name)
                with self.assertRaises((ValueError, RuntimeError)):
                    module._direct_connect()
                connect.assert_not_called()

    def test_scratch_rejects_remote_and_substring_names_before_connecting(self):
        for dsn in ("postgresql://u@remote.example/khipu_scratch_" + "a" * 32,
                    "postgresql://u@127.0.0.1/production_scratch_archive"):
            with self.subTest(dsn=dsn), mock.patch.dict(os.environ, {
                "KHIPU_LIVE_TESTS": "1", "KHIPU_SCRATCH_DSN": dsn,
                "KHIPU_SCRATCH_NONCE": "a" * 32,
            }, clear=True), mock.patch("psycopg.connect") as connect:
                with self.assertRaises((ValueError, RuntimeError)):
                    _load("test_pg_scratch")._direct_connect()
                connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
