"""This machine's stable, opaque identity for row provenance.

``local:<first 12 hex chars of sha256(hardware id)>``. The hardware id is the
IOPlatformUUID on macOS (``ioreg``) and ``/etc/machine-id`` on Linux. Both
survive a rename and a reinstall of Khipu, and the hash keeps the raw hardware
id out of the data. When neither is readable this raises instead of inventing
an id: a made-up value would change on the next run and orphan every row
stamped with it. Scope: docs/plans/2026-10-11-gateway-provenance.md, decision 1.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

LINUX_MACHINE_ID = Path("/etc/machine-id")
_IOREG_UUID_RE = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')
_ORIGIN_RE = re.compile(r"^local:[0-9a-f]{12}$")
_MACHINE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_CACHED: str | None = None


class MachineIdentityError(RuntimeError):
    """Neither the macOS hardware UUID nor the Linux machine-id is readable."""


def _macos_hardware_uuid() -> str | None:
    try:
        out = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = _IOREG_UUID_RE.search(out or "")
    return m.group(1).strip() if m and m.group(1).strip() else None


def _linux_machine_id() -> str | None:
    try:
        value = LINUX_MACHINE_ID.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # systemd writes a placeholder such as "uninitialized" until first boot
    # completes; hashing it would give an id that changes later.
    return value if _MACHINE_ID_RE.match(value) else None


def hardware_id() -> str:
    raw = _macos_hardware_uuid() if sys.platform == "darwin" else _linux_machine_id()
    if not raw:
        where = ("the IOPlatformUUID from ioreg" if sys.platform == "darwin"
                 else str(LINUX_MACHINE_ID))
        raise MachineIdentityError(
            f"cannot read this machine's identity ({where} is missing or unreadable); "
            "Khipu will not invent one, because rows stamped with it would be orphaned "
            "when it changed"
        )
    return raw


def local_origin() -> str:
    """``local:<12 hex>`` for this machine, computed once per process."""
    global _CACHED
    if _CACHED is None:
        digest = hashlib.sha256(hardware_id().encode("utf-8")).hexdigest()
        _CACHED = f"local:{digest[:12]}"
    return _CACHED


def is_local_origin(value: str) -> bool:
    return bool(_ORIGIN_RE.match(value or ""))
