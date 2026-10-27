"""The graph nightly fits the driver's timeout and survives a hung export."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from khipu import jobs

GRAPH = Path(__file__).resolve().parents[3] / "third_party" / "graphify"


def _graph_nightly():
    sys.path.insert(0, str(GRAPH))
    spec = importlib.util.spec_from_file_location("budget_graphify_nightly", GRAPH / "graphify_nightly.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GraphNightlyBudgetTest(unittest.TestCase):
    def test_a_hung_sources_export_is_non_fatal(self):
        gn = _graph_nightly()
        with (
            mock.patch.object(gn, "_khipu_root", return_value=Path("/nonexistent-khipu-root")),
            mock.patch.object(gn, "log"),
            mock.patch.object(gn.subprocess, "run", side_effect=subprocess.TimeoutExpired(["khipu"], 60)),
        ):
            self.assertIsNone(gn._export_sources({}))

    def test_step_caps_plus_the_export_fit_inside_the_driver_timeout(self):
        text = (GRAPH / "graphify_nightly.py").read_text()
        export = text.split("def _export_sources", 1)[1].split("\ndef ", 1)[0]
        main = text.split("def main", 1)[1]
        caps = sum(int(x) for part in (export, main) for x in re.findall(r"timeout=(\d+)", part))
        self.assertLessEqual(caps, jobs.DRIVER_TIMEOUT_SECONDS)
