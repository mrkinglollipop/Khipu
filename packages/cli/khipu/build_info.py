"""Which commit this build is.

Two different builds can carry the same ``khipu.__version__`` (a release is
tagged once, then main moves on), so the version alone cannot say whether a
Mac runs the build you think it does. ``stamp()`` is the version plus the short
commit, for ``khipu --version``, ``khipu doctor`` and the promote's version line:

    0.4.15 (25718b62)

Where the commit comes from, in order:

1. ``BUILD_COMMIT`` at the root of the code (``<root>/BUILD_COMMIT``): the
   desktop bundle has no git, so ``apps/desktop/scripts/bundle_cli.sh`` writes
   the commit it was built from there, before the bundle is signed.
2. ``git rev-parse`` in the root, only when the root itself is a checkout
   (has its own ``.git``): a live slot or a developer checkout. A folder that
   merely sits inside some other repository does not count, or a bundle
   staged inside the repo would report that repo's HEAD.

Neither: ``commit unknown``. Never a guess.
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from khipu import __version__

STAMP_FILE = "BUILD_COMMIT"
SHORT = 8
UNKNOWN = "commit unknown"
_HEX = re.compile(r"[0-9a-f]{7,40}")


def _root() -> Path:
    """The root of the code this module runs from: the checkout, the slot or
    the bundle (``<root>/packages/cli/khipu/build_info.py``)."""
    return Path(__file__).resolve().parents[3]


def _from_stamp(root: Path) -> str | None:
    try:
        text = (root / STAMP_FILE).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return text[:SHORT] if _HEX.fullmatch(text) else None


def _from_git(root: Path) -> str | None:
    if not (root / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", f"--short={SHORT}", "HEAD"],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip().lower()
    return out if r.returncode == 0 and _HEX.fullmatch(out) else None


@lru_cache(maxsize=1)
def commit() -> str | None:
    """The short commit this build is, or None when it cannot be told."""
    root = _root()
    return _from_stamp(root) or _from_git(root)


def stamp() -> str:
    """``0.4.15 (25718b62)``, or ``0.4.15 (commit unknown)``."""
    return f"{__version__} ({commit() or UNKNOWN})"
