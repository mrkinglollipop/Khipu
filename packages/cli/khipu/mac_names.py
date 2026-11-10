"""What the owner calls a Mac: his own name for it, never an id, a path or an address.

One small place for the app's reads (``route --json``'s ``this_mac``, the run-ahead picker, the owner-actions
list).  Nothing here raises: a name that can't be read is ``None`` (or the host's short name for this Mac), so a
label is never worth failing a read.
"""
from __future__ import annotations

import socket


def _given(mac) -> str | None:
    if not isinstance(mac, str) or not mac:
        return None
    try:
        from khipu import routing

        given = (routing.settings().get("routing_mac_names") or {}).get(mac)
    except Exception:  # noqa: BLE001 - a label is never worth failing a read
        return None
    return given.strip() if isinstance(given, str) and given.strip() else None


def this_mac() -> str | None:
    """This Mac's name: the one the owner gave it (``routing_mac_names``), else its host name; ``None`` when
    neither can be read."""
    try:
        from khipu import routing

        given = _given(routing._local_mac())
    except Exception:  # noqa: BLE001
        given = None
    if given:
        return given
    try:
        return socket.gethostname().split(".")[0] or None
    except OSError:
        return None


def name_of(mac) -> str | None:
    """Another Mac's name by its id: the owner's own name for it, else ``None``."""
    return _given(mac)
