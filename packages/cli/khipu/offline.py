"""Settings for working with the hub away (offline and self-healing scope,
docs/plans/2026-10-16-offline-and-self-healing.md, part C).

Each is the owner's in the app (Settings), with ``khipu config --set KEY
VALUE`` / ``--unset KEY`` as the secondary path, stored in config.json like
the ledger's and the work view's settings.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SETTINGS: dict[str, tuple[str, Any, str]] = {
    "hub_statement_timeout": (
        "duration", 30,
        "Longest an owed or decision read or write waits on the hub before it uses this Mac's copy (0: no limit)"),
    "hub_dead_connection_after": (
        "duration", 60,
        "How soon a hub connection that went silent (a network change) is given up (0: wait for the system)"),
}


def _coerce(kind: str, value: Any, key: str) -> Any:
    from khipu.routing import _coerce as routing_coerce

    return routing_coerce(kind, value, key)


def settings(config: dict | None = None) -> dict[str, Any]:
    """Each setting's value: the stored one when valid, else the default."""
    if config is None:
        from khipu.config import load_config

        config = load_config()
    out: dict[str, Any] = {}
    for key, (kind, default, _words) in SETTINGS.items():
        value = default
        if config.get(key) is not None:
            try:
                value = _coerce(kind, config[key], key)
            except (ValueError, TypeError):
                value = default
        out[key] = value
    return out


def settings_status() -> dict[str, dict]:
    from khipu.config import load_config

    config = load_config()
    values = settings(config)
    out = {}
    for key, (kind, default, words) in SETTINGS.items():
        source = "default"
        if config.get(key) is not None:
            try:
                _coerce(kind, config[key], key)
                source = "file"
            except (ValueError, TypeError):
                source = "invalid"
        out[key] = {"value": values[key], "default": default, "kind": kind, "source": source, "words": words}
    return out


def set_setting(key: str, raw: Any) -> Path:
    """Store a setting (``None`` removes it: the default again)."""
    from khipu.config import config_file, mutate_config

    if key not in SETTINGS:
        raise KeyError(f"unknown offline setting {key!r}")
    with mutate_config() as data:
        if raw is None:
            data.pop(key, None)
        else:
            data[key] = _coerce(SETTINGS[key][0], raw, key)
    return config_file()


def setting(key: str) -> Any:
    """One value; the default when the config cannot be read."""
    try:
        return settings()[key]
    except Exception:  # noqa: BLE001 - a connection must never fail on a config read
        return SETTINGS[key][1]
