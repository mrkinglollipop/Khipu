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
        rc, payload = self._run(
            cli.cmd_regen_memory, out=None, memory_root=None, limit=5, index=False,
        )
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])


class RegenMemoryIndexTest(unittest.TestCase):
    def test_index_refuses_memory_root_mismatch(self):
        engine_script = Path("/tmp/khipu-engine-root/conversations/scripts/build_index.py")
        args = mock.Mock(index=True, memory_root="/somewhere/else")
        with mock.patch("khipu.jobs.BUILD_INDEX", engine_script), \
             mock.patch("khipu.jobs.run_build_index") as run_idx, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = cli.cmd_regen_memory(args)
        self.assertEqual(rc, 2)
        run_idx.assert_not_called()
        self.assertIn("does not match", err.getvalue())

    def test_index_runs_when_memory_root_matches_engine(self):
        with tempfile.TemporaryDirectory() as td:
            engine_root = Path(td) / "conversations"
            engine_script = engine_root / "scripts" / "build_index.py"
            engine_script.parent.mkdir(parents=True)
            engine_script.write_text("# stub\n", encoding="utf-8")
            args = mock.Mock(index=True, memory_root=str(engine_root))
            with mock.patch("khipu.jobs.BUILD_INDEX", engine_script), \
                 mock.patch("khipu.jobs.run_build_index", return_value=0) as run_idx, \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = cli.cmd_regen_memory(args)
            self.assertEqual(rc, 0)
            run_idx.assert_called_once_with()
            self.assertIn(str(engine_root.resolve()), out.getvalue())


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


class RelevanceCosineFloorConfigTest(unittest.TestCase):
    """`khipu config --set relevance.cosine_floor N`: stored as the nested
    ``relevance.cosine_floor`` object that ``relevance.cosine_floor()`` reads,
    so the Settings number field changes what search actually uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": self.tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _cfg(self, **kw):
        base = dict(set_capture_mode=None, set_gateway_url=None, set=None, unset=None)
        base.update(kw)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_config(mock.Mock(**base))
        return rc, json.loads(out.getvalue())

    def _stored(self):
        return json.loads((Path(self.tmp.name) / "config.json").read_text())

    def test_set_writes_the_nested_key_relevance_reads(self):
        from khipu import relevance

        rc, p = self._cfg(set=["relevance.cosine_floor", "0.7"])
        self.assertEqual(rc, 0)
        self.assertTrue(p["ok"])
        self.assertEqual(self._stored()["relevance"], {"cosine_floor": 0.7})
        self.assertNotIn("relevance.cosine_floor", self._stored())
        self.assertEqual(relevance.cosine_floor(), 0.7)
        self.assertEqual(p["relevance_cosine_floor"]["source"], "file")

    def test_show_reports_default_then_file(self):
        rc, p = self._cfg()
        self.assertEqual(p["relevance_cosine_floor"],
                         {"value": 0.65, "source": "default", "default": 0.65})
        self._cfg(set=["relevance.cosine_floor", "1"])
        rc, p = self._cfg()
        self.assertEqual(p["relevance_cosine_floor"]["value"], 1.0)
        self.assertEqual(p["relevance_cosine_floor"]["source"], "file")

    def test_unset_restores_the_default_and_drops_the_empty_object(self):
        self._cfg(set=["relevance.cosine_floor", "0.7"])
        rc, p = self._cfg(unset="relevance.cosine_floor")
        self.assertEqual(rc, 0)
        self.assertNotIn("relevance", self._stored())
        self.assertEqual(p["relevance_cosine_floor"]["value"], 0.65)
        self.assertEqual(p["relevance_cosine_floor"]["source"], "default")

    def test_unset_keeps_sibling_relevance_keys(self):
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"relevance": {"cosine_floor": 0.7, "other": 1}}))
        self._cfg(unset="relevance.cosine_floor")
        self.assertEqual(self._stored()["relevance"], {"other": 1})

    def test_other_config_is_left_alone(self):
        config.set_capture_mode("hub")
        self._cfg(set=["relevance.cosine_floor", "0.5"])
        self.assertEqual(self._stored()["capture_mode"], "hub")

    def test_out_of_range_or_non_numbers_are_refused_and_nothing_is_written(self):
        for bad in ("0", "-0.1", "1.01", "2", "abc", "", "nan", "inf"):
            with self.subTest(value=bad):
                rc, p = self._cfg(set=["relevance.cosine_floor", bad])
                self.assertEqual(rc, 2)
                self.assertFalse(p["ok"])
        self.assertFalse((Path(self.tmp.name) / "config.json").exists())

    def test_the_status_helper_ignores_an_unusable_stored_value(self):
        from khipu import relevance

        cfg = Path(self.tmp.name) / "config.json"
        for raw in ("0.9", True, 5, 0):
            with self.subTest(raw=raw):
                cfg.write_text(json.dumps({"relevance": {"cosine_floor": raw}}))
                self.assertEqual(relevance.cosine_floor_status()["source"], "default")
                self.assertEqual(relevance.cosine_floor(), 0.65)


class ConfigShowFloatSettingsTest(unittest.TestCase):
    def test_show_names_value_source_and_default_for_each_knob(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": td, "KHIPU_DEDUP_SIMILARITY": ""}):
            config.set_float_setting("commitment_close_similarity", 0.9)
            base = dict(set_capture_mode=None, set_gateway_url=None, set=None, unset=None)
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                cli.cmd_config(mock.Mock(**base))
            shown = json.loads(out.getvalue())["float_settings"]
        self.assertEqual(shown["dedup_similarity"],
                         {"value": 0.92, "source": "default", "default": 0.92})
        self.assertEqual(shown["commitment_close_similarity"]["source"], "file")
        self.assertEqual(shown["commitment_close_similarity"]["value"], 0.9)
