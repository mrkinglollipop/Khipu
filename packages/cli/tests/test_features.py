# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Tests for khipu.features — the switch registry and capability signal
(Phase 0, session B; docs/plans/2026-09-27-memory-reasoning-scope.md,
"Engineering rules added in review" § Switches).

Every test that reads or writes config.json isolates it under a temp dir via
KHIPU_DATA_DIR (same convention as test_integrations.py's _TempHomeCase) so
none of this ever touches a real machine's Khipu config.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import features


def _isolated_data_dir():
    """A temp dir for config.json, via the same env var khipu.paths.data_dir()
    honors. Returns (patch, path); the caller enters the patch itself so it
    can layer more mock.patch.dict calls (e.g. KHIPU_FEATURE_*) on top."""
    tmp = tempfile.mkdtemp(prefix="khipu-features-")
    return mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": tmp}), Path(tmp)


class PrecedenceTest(unittest.TestCase):
    NAME = "rerank"

    def test_default_is_off(self):
        patch, _ = _isolated_data_dir()
        with patch:
            self.assertFalse(features.enabled(self.NAME))

    def test_file_overrides_default(self):
        patch, _ = _isolated_data_dir()
        with patch:
            features.set_enabled(self.NAME, True)
            self.assertTrue(features.enabled(self.NAME))

    def test_env_overrides_file(self):
        patch, _ = _isolated_data_dir()
        with patch:
            features.set_enabled(self.NAME, True)
            with mock.patch.dict(os.environ, {"KHIPU_FEATURE_RERANK": "off"}):
                self.assertFalse(features.enabled(self.NAME))


class BoolParsingTest(unittest.TestCase):
    def test_truthy_words_case_insensitive(self):
        for word in ("1", "true", "TRUE", "True", "on", "On", "yes", "YES"):
            self.assertIs(features.parse_bool(word), True, word)

    def test_falsy_words_case_insensitive(self):
        for word in ("0", "false", "FALSE", "False", "off", "Off", "no", "NO"):
            self.assertIs(features.parse_bool(word), False, word)

    def test_anything_else_is_ignored(self):
        for word in ("", "maybe", "2", "enabled", "   "):
            self.assertIsNone(features.parse_bool(word))

    def test_an_unparseable_env_value_falls_through_to_file_then_default(self):
        patch, _ = _isolated_data_dir()
        with patch, mock.patch.dict(os.environ, {"KHIPU_FEATURE_REFLECT": "maybe"}):
            self.assertFalse(features.enabled("reflect"))


class UnknownNameTest(unittest.TestCase):
    def test_enabled_raises_key_error_for_an_unknown_name(self):
        with self.assertRaises(KeyError):
            features.enabled("not_a_real_switch")

    def test_set_enabled_raises_key_error_for_an_unknown_name(self):
        patch, _ = _isolated_data_dir()
        with patch:
            with self.assertRaises(KeyError):
                features.set_enabled("not_a_real_switch", True)

    def test_unknown_name_in_config_file_is_ignored_and_reported(self):
        patch, tmp = _isolated_data_dir()
        with patch:
            (tmp / "config.json").write_text(
                json.dumps({"features": {"not_a_real_switch": True}}))
            out = features.states()
        self.assertNotIn("not_a_real_switch", out["features"])
        self.assertIn("not_a_real_switch", out["unknown"])

    def test_unknown_name_in_environment_is_ignored_and_reported(self):
        patch, _ = _isolated_data_dir()
        with patch, mock.patch.dict(os.environ, {"KHIPU_FEATURE_NOT_A_REAL_SWITCH": "on"}):
            out = features.states()
        self.assertNotIn("not_a_real_switch", out["features"])
        self.assertIn("not_a_real_switch", out["unknown"])


class SetEnabledRoundTripTest(unittest.TestCase):
    def test_round_trip_under_a_temp_data_dir(self):
        patch, tmp = _isolated_data_dir()
        with patch:
            features.set_enabled("briefs", True)
            self.assertTrue(features.enabled("briefs"))
            on_disk = json.loads((tmp / "config.json").read_text())
            self.assertEqual(on_disk["features"]["briefs"], True)
            features.set_enabled("briefs", False)
            self.assertFalse(features.enabled("briefs"))

    def test_states_reports_the_source(self):
        patch, _ = _isolated_data_dir()
        with patch:
            out = features.states()
            self.assertEqual(out["features"]["briefs"]["source"], "default")
            features.set_enabled("briefs", True)
            out = features.states()
            self.assertEqual(out["features"]["briefs"], {"enabled": True, "source": "file"})
            with mock.patch.dict(os.environ, {"KHIPU_FEATURE_BRIEFS": "off"}):
                out = features.states()
                self.assertEqual(out["features"]["briefs"], {"enabled": False, "source": "env"})


class ContractShapeTest(unittest.TestCase):
    def test_contract_shape_and_content(self):
        from khipu import __version__ as khipu_version

        patch, _ = _isolated_data_dir()
        with patch:
            c = features.contract()
        self.assertEqual(set(c), {"version", "capabilities", "features"})
        self.assertEqual(c["version"], khipu_version)
        self.assertEqual(c["capabilities"], sorted(c["capabilities"]))
        self.assertIn("search.hybrid", c["capabilities"])
        self.assertIn("launchers.read_only", c["capabilities"])
        self.assertEqual(set(c["features"]), set(features.FEATURES))
        self.assertTrue(all(isinstance(v, bool) for v in c["features"].values()))


class VersionSyncTest(unittest.TestCase):
    """khipu.__version__, tauri.conf.json and package.json must never
    drift apart — skips when the desktop app files are absent (e.g. a
    checkout of the CLI package alone)."""

    def test_version_matches_tauri_and_package_json(self):
        from khipu import __version__ as khipu_version
        from khipu.paths import repo_root

        root = repo_root()
        tauri = root / "apps" / "desktop" / "src-tauri" / "tauri.conf.json"
        pkg = root / "apps" / "desktop" / "package.json"
        if not tauri.is_file() or not pkg.is_file():
            self.skipTest("apps/desktop files not present in this checkout")
        tauri_version = json.loads(tauri.read_text())["version"]
        pkg_version = json.loads(pkg.read_text())["version"]
        self.assertEqual(khipu_version, tauri_version,
                          "khipu.__version__ has drifted from tauri.conf.json")
        self.assertEqual(khipu_version, pkg_version,
                          "khipu.__version__ has drifted from package.json")


if __name__ == "__main__":
    unittest.main()
