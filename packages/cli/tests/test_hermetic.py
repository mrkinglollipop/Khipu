# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Proves conftest.py's hermetic-by-default environment actually took effect
(Phase 0 session A; B1 in docs/research/hindsight-plan-review-2026-09-28.md).

Skipped under KHIPU_LIVE_TESTS=1, where conftest deliberately leaves the
environment exactly as found and these assertions would not hold.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path


@unittest.skipIf(os.environ.get("KHIPU_LIVE_TESTS") == "1",
                  "KHIPU_LIVE_TESTS=1 leaves the real environment untouched")
class HermeticEnvironmentTest(unittest.TestCase):
    def test_home_is_not_the_real_home(self):
        home = os.environ.get("HOME", "")
        self.assertIn("khipu-hermetic-home-", home)
        self.assertEqual(str(Path.home()), home)

    def test_integrations_shim_dir_resolves_under_the_temp_home(self):
        from khipu import integrations

        self.assertTrue(integrations._shim_dir().is_relative_to(Path.home()))

    def test_paths_data_dir_resolves_under_the_temp_home(self):
        from khipu import paths

        self.assertTrue(paths.data_dir().is_relative_to(Path.home()))

    def test_session_capture_khipu_home_resolves_under_the_temp_home(self):
        from khipu import session_capture

        self.assertTrue(session_capture.khipu_home().is_relative_to(Path.home()))

    def test_resolve_dsn_raises_with_nothing_configured(self):
        from khipu import db

        with self.assertRaises(RuntimeError):
            db.resolve_dsn()

    def test_resolve_gemini_key_raises_with_nothing_configured(self):
        from khipu import keychain

        with self.assertRaises(RuntimeError):
            keychain.resolve_gemini_key()


if __name__ == "__main__":
    unittest.main()
