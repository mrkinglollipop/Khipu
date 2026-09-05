# --bypass-harness (sonnet lane) — already running as a dispatched on-sub
# Sonnet build agent (khipu-memory-system-53e7a0 worktree task); nothing to
# route further.
"""Every khipu launcher must redirect CPython bytecode caching OUTSIDE any
signed .app bundle — see khipu.paths.pycache_dir. Release 0.3.15 skipped this:
running the bundled Python wrote __pycache__/*.pyc inside Contents/Resources/
khipu, and the next in-app update's Gatekeeper re-validation saw the added
files and reported "Khipu is damaged" (confirmed with `codesign -vvv --deep
--strict`; release withdrawn).

Three of the four bin wrappers export PYTHONPYCACHEPREFIX to
~/Library/Caches/Khipu/pycache. khipu-aegis-capture cannot: it runs inside
Aegis's sandbox, which DENIES ~/Library entirely (see the header comment in
that script), so it disables bytecode writing outright instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parents[1] / "bin"

PYCACHE_WRAPPERS = ("khipu-stop-hook", "khipu-mcp", "khipu-recall-hook")
DONTWRITE_WRAPPERS = ("khipu-aegis-capture",)


@pytest.mark.parametrize("name", PYCACHE_WRAPPERS)
def test_wrapper_exports_pythonpycacheprefix(name):
    text = (BIN_DIR / name).read_text(encoding="utf-8")
    assert "PYTHONPYCACHEPREFIX" in text, name
    assert "export PYTHONPYCACHEPREFIX" in text, name
    # Points at the Caches dir, not the bundle or a sandbox-denied path.
    assert "Library/Caches/Khipu/pycache" in text, name
    # Created before use, and space-safe (quoted).
    assert 'mkdir -p "$PYTHONPYCACHEPREFIX"' in text, name


@pytest.mark.parametrize("name", DONTWRITE_WRAPPERS)
def test_sandboxed_wrapper_disables_bytecode_instead(name):
    text = (BIN_DIR / name).read_text(encoding="utf-8")
    assert "export PYTHONDONTWRITEBYTECODE" in text, name
    # Must NOT actually set PYTHONPYCACHEPREFIX — the sandbox denies
    # ~/Library, so a Caches dir under it is not writable either. (The
    # comment explaining why is allowed to mention the variable name.)
    assert "PYTHONPYCACHEPREFIX=" not in text, name
    assert "export PYTHONPYCACHEPREFIX" not in text, name


def test_every_wrapper_covered():
    """Guards against a new bin/khipu-* launcher shipping without either
    mechanism — extend PYCACHE_WRAPPERS or DONTWRITE_WRAPPERS above."""
    all_wrappers = {p.name for p in BIN_DIR.iterdir() if p.is_file()}
    known = set(PYCACHE_WRAPPERS) | set(DONTWRITE_WRAPPERS)
    unaccounted = all_wrappers - known
    # No allowlist. khipu-capture-hook was the last entry and had no reference
    # anywhere in the repo (audit 2026-09-04) — it was deleted rather than
    # exempted. Do not grow this back: every launcher gets one mechanism.
    assert not unaccounted, unaccounted


def test_the_bundled_cli_wrapper_redirects_the_cache_too():
    """apps/desktop/scripts/bundle_cli.sh writes its own `khipu` launcher into
    Contents/Resources; it shipped WITHOUT PYTHONPYCACHEPREFIX, so running the
    bundled CLI wrote .pyc files inside the signed bundle — the exact 0.3.15
    seal break these wrappers exist to prevent."""
    script = Path(__file__).resolve().parents[3] / "apps" / "desktop" / "scripts" / "bundle_cli.sh"
    text = script.read_text(encoding="utf-8")
    heredoc = text.split('cat >"$OUT/bin/khipu" <<', 1)[1].split("EOF", 2)[1]
    assert "PYTHONPYCACHEPREFIX" in heredoc
    assert "export PYTHONPYCACHEPREFIX" in heredoc
    assert "Library/Caches/Khipu/pycache" in heredoc
    assert 'mkdir -p "$PYTHONPYCACHEPREFIX"' in heredoc
