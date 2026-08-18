"""Hub config — the small set of switches the desktop Settings pane owns.

Today this is one key, ``capture_mode``, stored as JSON in the Khipu data dir
(``~/.config/khipu/config.json`` by default; honors the same ``KHIPU_DATA_DIR``
override and pointer file as everything else in ``paths.py``).

``capture_mode`` is the SSOT for *who writes* (agent-integration note,
"capture_mode vs KHIPU_MIRROR"): env flags never substitute for it. Precedence
for reads is env ``KHIPU_CAPTURE_MODE`` (tests / one-off overrides) → config
file → default ``dual``. Writes only ever go to the file.

Modes:
  legacy  capture_v2 writes files; Khipu writes nothing (KHIPU_MIRROR aside)
  dual    capture_v2 writes files AND Khipu writes PG — both durable
  hub     Khipu writes PG; file wiki maintained by reverse-mirror (P3 end-state)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CAPTURE_MODES = ("legacy", "dual", "hub")
DEFAULT_CAPTURE_MODE = "dual"
CONFIG_NAME = "config.json"


def config_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / CONFIG_NAME


def load_config() -> dict:
    path = config_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> Path:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def capture_mode() -> str:
    env = (os.environ.get("KHIPU_CAPTURE_MODE") or "").strip().lower()
    if env in CAPTURE_MODES:
        return env
    stored = str(load_config().get("capture_mode", "")).strip().lower()
    return stored if stored in CAPTURE_MODES else DEFAULT_CAPTURE_MODE


def set_capture_mode(mode: str) -> Path:
    mode = (mode or "").strip().lower()
    if mode not in CAPTURE_MODES:
        raise ValueError(f"capture_mode must be one of {CAPTURE_MODES}, got {mode!r}")
    data = load_config()
    data["capture_mode"] = mode
    return save_config(data)


def gateway_url() -> str:
    """Public HTTPS base URL of the Khipu gateway (MCP over HTTPS, for cloud
    harnesses). Env KHIPU_GATEWAY_URL wins; else Hub config; else empty."""
    env = (os.environ.get("KHIPU_GATEWAY_URL") or "").strip()
    if env:
        return env.rstrip("/")
    return str(load_config().get("gateway_url", "")).strip().rstrip("/")


def set_gateway_url(url: str) -> Path:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith("https://"):
        raise ValueError("gateway_url must be an https:// URL (it carries a bearer token)")
    data = load_config()
    if url:
        data["gateway_url"] = url
    else:
        data.pop("gateway_url", None)
    return save_config(data)


# ---------------------------------------------------------------------------
# Machine-specific paths.
#
# These used to be hardcoded defaults pointing at one developer's disk layout,
# which made the code run only on that Mac. They now live in the same config
# file as capture_mode: env override → config.json → None. None means "not
# configured", and every consumer must say so rather than assume a location.
# ---------------------------------------------------------------------------

PATH_SETTINGS: dict[str, tuple[str, ...]] = {
    # legacy file wiki that dual/legacy capture writes and reconcile reads
    "memory_root": ("KHIPU_MEMORY_ROOT", "ALZY_MEMORY_ROOT"),
    # git repo that git_sync.py pushes (usually memory_root's parent)
    "memory_repo": ("KHIPU_MEMORY_REPO",),
    # legacy capture_v2.py — the file-wiki writer dual mode chains to
    "capture_v2": ("KHIPU_CAPTURE_V2",),
    # legacy graphify SQLite that graph-sync mirrors from
    "graph_sqlite": ("KHIPU_GRAPH_SQLITE", "ALZY_GRAPH_SQLITE"),
    # last-resort file the Gemini key may be read from
    "gemini_key_file": ("KHIPU_GEMINI_KEY_FILE",),
}


def path_setting(key: str) -> Path | None:
    if key not in PATH_SETTINGS:
        raise KeyError(f"unknown path setting {key!r}; known: {sorted(PATH_SETTINGS)}")
    for env in PATH_SETTINGS[key]:
        v = (os.environ.get(env) or "").strip()
        if v:
            return Path(v).expanduser()
    stored = str(load_config().get(key, "") or "").strip()
    return Path(stored).expanduser() if stored else None


def set_path_setting(key: str, value: str | None) -> Path:
    if key not in PATH_SETTINGS:
        raise KeyError(f"unknown path setting {key!r}; known: {sorted(PATH_SETTINGS)}")
    data = load_config()
    if value is None or not str(value).strip():
        data.pop(key, None)
    else:
        data[key] = str(Path(str(value)).expanduser())
    return save_config(data)


def path_settings_status() -> dict:
    """Every path setting with its resolved value and where it came from."""
    out = {}
    for key, envs in PATH_SETTINGS.items():
        src = "unset"
        for env in envs:
            if (os.environ.get(env) or "").strip():
                src = f"env:{env}"
                break
        else:
            if str(load_config().get(key, "") or "").strip():
                src = "file"
        val = path_setting(key)
        out[key] = {
            "value": str(val) if val else None,
            "source": src,
            "exists": val.exists() if val else False,
        }
    return out
