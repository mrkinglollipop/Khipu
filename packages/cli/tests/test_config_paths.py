"""Machine-specific paths (legacy memory tree, graph.sqlite, capture_v2, key
file) come from env → config.json → None. They used to be hardcoded to one
developer's disk layout, so the code only ran there.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import cli, config


class PathSettingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"KHIPU_DATA_DIR": self.tmp.name}
        for envs in config.PATH_SETTINGS.values():
            for e in envs:
                env[e] = ""
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_unset_is_none_not_a_guess(self):
        for key in config.PATH_SETTINGS:
            with self.subTest(key=key):
                self.assertIsNone(config.path_setting(key))

    def test_env_beats_file(self):
        config.set_path_setting("memory_root", "/from/file")
        with mock.patch.dict(os.environ, {"KHIPU_MEMORY_ROOT": "/from/env"}):
            self.assertEqual(config.path_setting("memory_root"), Path("/from/env"))
        self.assertEqual(config.path_setting("memory_root"), Path("/from/file"))

    def test_legacy_env_names_still_work(self):
        with mock.patch.dict(os.environ, {"ALZY_MEMORY_ROOT": "/legacy"}):
            self.assertEqual(config.path_setting("memory_root"), Path("/legacy"))

    def test_set_and_unset_round_trip_through_the_file(self):
        p = config.set_path_setting("graph_sqlite", "~/g.sqlite")
        stored = json.loads(p.read_text())["graph_sqlite"]
        self.assertFalse(stored.startswith("~"), "tilde must be expanded on write")
        config.set_path_setting("graph_sqlite", None)
        self.assertNotIn("graph_sqlite", json.loads(p.read_text()))
        self.assertIsNone(config.path_setting("graph_sqlite"))

    def test_unknown_key_is_refused(self):
        with self.assertRaises(KeyError):
            config.path_setting("dsn")
        with self.assertRaises(KeyError):
            config.set_path_setting("dsn", "/x")

    def test_status_names_the_source(self):
        config.set_path_setting("capture_v2", self.tmp.name)
        st = config.path_settings_status()
        self.assertEqual(st["capture_v2"]["source"], "file")
        self.assertTrue(st["capture_v2"]["exists"])
        self.assertEqual(st["memory_root"]["source"], "unset")
        with mock.patch.dict(os.environ, {"KHIPU_CAPTURE_V2": "/nope"}):
            st = config.path_settings_status()
            self.assertEqual(st["capture_v2"]["source"], "env:KHIPU_CAPTURE_V2")
            self.assertFalse(st["capture_v2"]["exists"])


class RepoRootTest(unittest.TestCase):
    def test_derived_from_the_package_location_when_env_is_unset(self):
        from khipu import paths
        with mock.patch.dict(os.environ, {"KHIPU_ROOT": "", "ALZY_ROOT": ""}):
            root = paths.repo_root()
        self.assertTrue((root / "packages" / "cli" / "khipu" / "paths.py").is_file(), root)

    def test_env_wins(self):
        from khipu import paths
        with mock.patch.dict(os.environ, {"KHIPU_ROOT": "/elsewhere"}):
            self.assertEqual(paths.repo_root(), Path("/elsewhere"))


class UnconfiguredCommandsTest(unittest.TestCase):
    """Commands that need the file wiki fail loudly, with the fix, exit 2."""

    def _run(self, fn, **kw):
        args = mock.Mock(**kw)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = fn(args)
        return rc, json.loads(out.getvalue())

    def test_reconcile_without_memory_root(self):
        rc, payload = self._run(cli.cmd_reconcile, memory_root=None)
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("khipu config --set memory_root", payload["fix"])

    def test_regen_memory_without_out_or_root(self):
        rc, payload = self._run(cli.cmd_regen_memory, out=None, memory_root=None, limit=5)
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])


class ConfigCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"KHIPU_DATA_DIR": self.tmp.name}
        for envs in config.PATH_SETTINGS.values():
            for e in envs:
                env[e] = ""
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _cfg(self, **kw):
        base = dict(set_capture_mode=None, set_gateway_url=None, set=None, unset=None)
        base.update(kw)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_config(mock.Mock(**base))
        return rc, json.loads(out.getvalue())

    def test_set_then_show_then_unset(self):
        rc, p = self._cfg(set=["memory_root", self.tmp.name])
        self.assertEqual(rc, 0)
        self.assertEqual(p["memory_root"]["source"], "file")
        rc, p = self._cfg()
        self.assertEqual(p["paths"]["memory_root"]["value"], self.tmp.name)
        rc, p = self._cfg(unset="memory_root")
        self.assertEqual(rc, 0)
        self.assertEqual(p["memory_root"]["source"], "unset")

    def test_unknown_key_exits_2(self):
        rc, p = self._cfg(set=["dsn", "/x"])
        self.assertEqual(rc, 2)
        self.assertFalse(p["ok"])
