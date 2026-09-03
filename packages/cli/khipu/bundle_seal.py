"""Verify the running app bundle's code signature is intact.

Release 0.3.15 bundled the Python CLI under Contents/Resources/khipu. Running
that Python — hook wrappers, the desktop app's own shell-out
(`run_khipu_cli`), launchd jobs — wrote `__pycache__/*.pyc` INSIDE the signed
bundle. Gatekeeper re-validates a bundle's signature the first time it
launches after an in-app update, saw the added files, and reported "Khipu is
damaged" (confirmed with `codesign -vvv --deep --strict`; release withdrawn).

Every khipu launcher now redirects bytecode outside the bundle
(PYTHONPYCACHEPREFIX / PYTHONDONTWRITEBYTECODE — see khipu.paths.pycache_dir).
This check is the tripwire that would have caught the incident before a user
did: on a machine actually running from inside a .app, doctor/status run
`codesign --verify --deep --strict` against it and go red the moment anything
adds or modifies a file under it, regardless of cause.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

TIMEOUT_SECONDS = 20


def _app_bundle_root(root: Path) -> Path | None:
    """Walk up from KHIPU_ROOT looking for the enclosing `.app` directory.

    A release build's KHIPU_ROOT is .../Khipu.app/Contents/Resources/khipu —
    three levels below the bundle root, but that layout is not asserted here;
    any ancestor named `*.app` counts.
    """
    for parent in (root, *root.parents):
        if parent.suffix == ".app":
            return parent
    return None


def _first_relevant_line(stderr: str) -> str:
    for line in (stderr or "").splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if "file added" in low or "file modified" in low or "invalid signature" in low:
            return line
    # Fall back to the last non-empty line — still more useful than nothing.
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else "codesign verify failed"


def check(root: Path | None = None) -> dict:
    """codesign --verify --deep --strict on the enclosing .app, if any.

    Fail-open to ``{"ok": True, "na": True, ...}`` when the running CLI is not
    inside a .app bundle (a maintainer checkout, a second Mac running from a
    plain repo, Linux) or when `codesign` itself is unavailable — the check
    only means something on a signed macOS app.
    """
    if root is None:
        from khipu.paths import repo_root

        root = repo_root()
    app = _app_bundle_root(Path(root))
    if app is None:
        return {"ok": True, "na": True, "reason": "KHIPU_ROOT is not inside a .app bundle"}
    try:
        r = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {"ok": True, "na": True, "reason": "codesign not available", "app": str(app)}
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"codesign timed out after {TIMEOUT_SECONDS}s",
            "app": str(app),
        }
    except OSError as exc:  # noqa: BLE001 — a broken check must not look like a pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "app": str(app)}
    if r.returncode == 0:
        return {"ok": True, "app": str(app)}
    return {"ok": False, "error": _first_relevant_line(r.stderr), "app": str(app)}
