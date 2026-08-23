"""Every CLI module must import with ONLY the vendored libs on the path.

The desktop bundle ships a bare interpreter plus packages/cli/requirements.txt;
a module that quietly leans on a Homebrew site-package (packaging, 0.3.0-0.3.1:
broke `components status-json` and flipped graph_producer to False in the app)
passes every normal test and fails only inside the .app.
"""

import pkgutil
import subprocess
import sys
import unittest
from pathlib import Path

import khipu

CLI = Path(khipu.__file__).resolve().parents[1]
VENDORED = CLI.parents[1] / ".python_libs"


@unittest.skipUnless(VENDORED.is_dir(), "no .python_libs on this machine")
class VendoredImportTest(unittest.TestCase):
    def test_every_module_imports_without_site_packages(self):
        names = [m.name for m in pkgutil.iter_modules(khipu.__path__, "khipu.") if not m.name.endswith("__main__")]
        # -I ignores PYTHONPATH, so seed sys.path explicitly.
        code = (
            f"import importlib,sys\nsys.path[:0] = [{str(CLI)!r}, {str(VENDORED)!r}]\n"
        ) + "".join(
            f"importlib.import_module({n!r})\n" for n in names
        )
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            env={"HOME": str(Path.home())},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
