"""Every line the Stop hook writes must be attributable to a run of a session.

Audit 2026-09-04: `[khipu-stop-hook] capture ...` carried neither a timestamp
nor a session id, so a quiet session in ~/Library/Logs/khipu/stop-hook.log
could not be told apart from a hook that never fired for it at all — which is
exactly the question liveness/doctor exist to answer.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import session_capture as sc

SHIM = Path(__file__).resolve().parents[1] / "bin" / "khipu-stop-hook"


class StopHookShimLoggingTest(unittest.TestCase):
    def setUp(self):
        self.text = SHIM.read_text(encoding="utf-8")

    def test_the_shim_stays_posix_sh(self):
        self.assertTrue(self.text.startswith("#!/bin/sh\n"))

    def test_the_pycache_redirect_survives(self):
        self.assertIn('PYTHONPYCACHEPREFIX="${HOME}/Library/Caches/Khipu/pycache"', self.text)
        self.assertIn('mkdir -p "$PYTHONPYCACHEPREFIX"', self.text)
        self.assertIn("export PYTHONPYCACHEPREFIX", self.text)

    def _body(self) -> str:
        """The Python heredoc, minus the shell's own trailing redirect line."""
        after = self.text.split("<<'PY'", 1)[1]
        return after.split("\n", 1)[1].split("\nPY\n", 1)[0]

    def test_every_log_line_goes_through_the_stamped_helper(self):
        body = self._body()
        self.assertIn('def _say(msg):', body)
        self.assertIn('%Y-%m-%dT%H:%M:%SZ', body, "ISO-8601 UTC stamp")
        self.assertIn("session={SID or '?'}", body)
        # No bare print() of a [khipu-stop-hook] line may remain.
        stray = [ln for ln in body.splitlines()
                 if "[khipu-stop-hook]" in ln and not ln.strip().startswith("#")]
        # Exactly one: the format string inside _say itself.
        self.assertEqual(len(stray), 1, stray)
        self.assertIn("_stamp()", stray[0])
        self.assertIn("session=", stray[0])

    def test_the_shim_body_compiles(self):
        compile(self._body(), "khipu-stop-hook", "exec")


class CaptureLogStampTest(unittest.TestCase):
    def test_log_lines_are_iso_8601_utc(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "capture.log"
            with mock.patch.object(sc, "log_path", return_value=p):
                sc._log("claude_code:abc123: Stop due (5 turns) -> queued j.json")
            line = p.read_text(encoding="utf-8").strip()
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[khipu-capture\] ")
        self.assertIn("claude_code:abc123", line, "the session id must ride the line")


class HookMainLogsCarryTheSessionIdTest(unittest.TestCase):
    def test_a_hook_error_names_the_session(self):
        seen: list[str] = []
        with mock.patch.object(sc, "_log", side_effect=seen.append), \
                mock.patch.object(sc, "_heartbeat"), \
                mock.patch.object(sc, "transcript_path", side_effect=RuntimeError("boom")):
            out = sc.hook_main('{"session_id": "sx", "hook_event_name": "Stop"}', "claude_code")
        self.assertIn("RuntimeError", out["error"])
        self.assertTrue(seen)
        self.assertIn("claude_code:sx", seen[0])
