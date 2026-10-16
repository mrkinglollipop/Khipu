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
import re
from pathlib import Path

from khipu.output_guard import secret_result

CAPTURE_MODES = ("legacy", "dual", "hub")
DEFAULT_CAPTURE_MODE = "dual"
CONFIG_NAME = "config.json"


class InvalidConfigDocument(ValueError):
    """An existing config document cannot safely be changed."""


def config_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / CONFIG_NAME


@secret_result("config")
def load_config() -> dict:
    path = config_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@secret_result("config")
def _load_config_for_mutation() -> dict:
    """Load an existing document without overwriting malformed bytes."""
    path = config_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidConfigDocument(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidConfigDocument(f"cannot modify malformed config: {path}")
    return data


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
    data = _load_config_for_mutation()
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
    data = _load_config_for_mutation()
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
    "anthropic_key_file": ("KHIPU_ANTHROPIC_KEY_FILE",),
    "openai_key_file": ("KHIPU_OPENAI_KEY_FILE",),
    "openrouter_key_file": ("KHIPU_OPENROUTER_KEY_FILE",),
}


# The file wiki and the graph source: live trees an isolated run
# (``paths.ISOLATED_ENV``, the promotion gate) must never write, so they read
# as unset there whatever the environment or config says.
ISOLATED_UNSET = frozenset({"memory_root", "memory_repo", "capture_v2", "graph_sqlite"})


def path_setting(key: str) -> Path | None:
    if key not in PATH_SETTINGS:
        raise KeyError(f"unknown path setting {key!r}; known: {sorted(PATH_SETTINGS)}")
    if key in ISOLATED_UNSET:
        from khipu.paths import isolated_dir

        if isolated_dir() is not None:
            return None
    for env in PATH_SETTINGS[key]:
        v = (os.environ.get(env) or "").strip()
        if v:
            return Path(v).expanduser()
    stored = str(load_config().get(key, "") or "").strip()
    return Path(stored).expanduser() if stored else None


def set_path_setting(key: str, value: str | None) -> Path:
    if key not in PATH_SETTINGS:
        raise KeyError(f"unknown path setting {key!r}; known: {sorted(PATH_SETTINGS)}")
    data = _load_config_for_mutation()
    if value is None or not str(value).strip():
        data.pop(key, None)
    else:
        data[key] = str(Path(str(value)).expanduser())
    return save_config(data)


# ---------------------------------------------------------------------------
# Tunable float knobs (memory reliability W1.4 / W3.3).
#
# (env override, default) — same precedence as everything else here: env wins,
# then config.json, then the default. Never hardcoded at the call site so an
# operator can tune dedup/close aggressiveness without a code change.
# ---------------------------------------------------------------------------

FLOAT_SETTINGS: dict[str, tuple[str, float]] = {
    # W1.4 ingest dedup: cosine (or Jaccard fallback) threshold to merge two
    # captures in the same project within a 5-minute window.
    "dedup_similarity": ("KHIPU_DEDUP_SIMILARITY", 0.92),
    # W3.3 commitments auto-close: cosine threshold for a closed_loop/decision
    # to close an open commitment in the same project.
    "commitment_close_similarity": ("KHIPU_COMMITMENT_CLOSE_SIMILARITY", 0.85),
}


def float_setting(key: str) -> float:
    if key not in FLOAT_SETTINGS:
        raise KeyError(f"unknown float setting {key!r}; known: {sorted(FLOAT_SETTINGS)}")
    env_name, default = FLOAT_SETTINGS[key]
    env = (os.environ.get(env_name) or "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    stored = load_config().get(key)
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        return float(stored)
    return default


def set_float_setting(key: str, value: float) -> Path:
    if key not in FLOAT_SETTINGS:
        raise KeyError(f"unknown float setting {key!r}; known: {sorted(FLOAT_SETTINGS)}")
    data = _load_config_for_mutation()
    data[key] = float(value)
    return save_config(data)


# ---------------------------------------------------------------------------
# List knobs (user identity, W3 owner rule — 2026-09-05).
#
# Same env → config.json → default precedence as everything else here. The
# only consumer today is ``khipu.commitments.user_aliases`` (every name that
# means "the user" on this hub, never a hardcoded name — this is a public
# product).
# ---------------------------------------------------------------------------

LIST_SETTINGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "user_aliases": ("KHIPU_USER_ALIASES", ()),
}


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def list_setting(key: str) -> tuple[str, ...]:
    if key not in LIST_SETTINGS:
        raise KeyError(f"unknown list setting {key!r}; known: {sorted(LIST_SETTINGS)}")
    env_name, default = LIST_SETTINGS[key]
    env = (os.environ.get(env_name) or "").strip()
    if env:
        return _split_csv(env)
    stored = load_config().get(key)
    if isinstance(stored, list):
        return tuple(str(v).strip() for v in stored if str(v).strip())
    if isinstance(stored, str) and stored.strip():
        return _split_csv(stored)
    return tuple(default)


def set_list_setting(key: str, values: "str | list[str] | tuple[str, ...]") -> Path:
    if key not in LIST_SETTINGS:
        raise KeyError(f"unknown list setting {key!r}; known: {sorted(LIST_SETTINGS)}")
    if isinstance(values, str):
        values = _split_csv(values)
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    data = _load_config_for_mutation()
    if cleaned:
        data[key] = cleaned
    else:
        data.pop(key, None)
    return save_config(data)


# ---------------------------------------------------------------------------
# Daily call ceilings (khipu.embed budget scopes), whole numbers >= 0, 0 =
# unlimited. Unlike the knobs above, the stored value wins over the env: the
# owner lifts a ceiling with `khipu budget lift` / `khipu config --set` and a
# launchd plist's env must not silently override that. Read by
# khipu.embed._budget_cap; unset means "use the env, else the default".
# ---------------------------------------------------------------------------

INT_SETTINGS: dict[str, str] = {
    "synth_daily_calls": "synth",
    "embed_daily_calls": "memory",
    "library_embed_daily_calls": "library",
}


def int_setting(key: str) -> int | None:
    """The stored whole number for ``key``, or None when unset or invalid."""
    if key not in INT_SETTINGS:
        raise KeyError(f"unknown integer setting {key!r}; known: {sorted(INT_SETTINGS)}")
    stored = load_config().get(key)
    if isinstance(stored, int) and not isinstance(stored, bool) and stored >= 0:
        return stored
    return None


def parse_int_setting(key: str, raw: "str | int") -> int:
    """``raw`` as a whole number >= 0; ValueError names the key otherwise."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a whole number >= 0, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{key} must be a whole number >= 0, got {value}")
    return value


def set_int_setting(key: str, value: "int | None") -> Path:
    if key not in INT_SETTINGS:
        raise KeyError(f"unknown integer setting {key!r}; known: {sorted(INT_SETTINGS)}")
    data = _load_config_for_mutation()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = parse_int_setting(key, value)
    return save_config(data)


# ---------------------------------------------------------------------------
# Choice settings: one of a fixed set of words. `synth_fallback` says what
# happens when a summariser provider other than Gemini fails a window
# (account limit, not logged in, transport error, a reply that is not the
# capture JSON): `gemini` retries that window on Gemini Flash when a Gemini
# key resolves, `none` leaves the job queued for a later retry. Unset = gemini.
# ---------------------------------------------------------------------------

CHOICE_SETTINGS: dict[str, tuple[str, ...]] = {
    "synth_fallback": ("gemini", "none"),
}


def choice_setting(key: str) -> tuple[str, str]:
    """``(value, source)``: the stored word and ``config``, or the first
    choice and ``default`` when unset or invalid."""
    choices = CHOICE_SETTINGS[key]
    stored = load_config().get(key)
    if isinstance(stored, str) and stored.strip().lower() in choices:
        return stored.strip().lower(), "config"
    return choices[0], "default"


def set_choice_setting(key: str, value: "str | None") -> Path:
    choices = CHOICE_SETTINGS[key]
    data = _load_config_for_mutation()
    if value is None:
        data.pop(key, None)
    else:
        word = str(value).strip().lower()
        if word not in choices:
            raise ValueError(f"{key} must be one of {', '.join(choices)}, got {value!r}")
        data[key] = word
    return save_config(data)


# ---------------------------------------------------------------------------
# Graph backup destination: the rclone `remote:path` the offsite copy of the
# graph snapshot goes to. Env KHIPU_GRAPH_BACKUP_REMOTE → config.json →
# default. The remote must be named `r2` (graph_backup.has_r2_remote).
# ---------------------------------------------------------------------------

DEFAULT_GRAPH_BACKUP_REMOTE = "r2:khipu-backups/khipu-graph"
GRAPH_BACKUP_REMOTE_ENV = "KHIPU_GRAPH_BACKUP_REMOTE"

# Same rule as the app's Tauri validator (`checked_backup_remote`): a named
# rclone remote, then a path. rclone reads anything with a slash before the
# colon as a local path, and a leading dash as a flag.
_BACKUP_REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def check_graph_backup_remote(value: str) -> str:
    """``value`` trimmed, or ValueError in plain words."""
    text = (value or "").strip().rstrip("/")
    remote, sep, path = text.partition(":")
    ok = (
        len(text) <= 512
        and bool(sep)
        and bool(path)
        and _BACKUP_REMOTE_NAME.fullmatch(remote) is not None
        and not any(c.isspace() or not c.isprintable() for c in text)
    )
    if not ok:
        raise ValueError(
            "graph_backup_remote must be a named rclone remote and folder, such as "
            "r2:my-bucket/khipu-graph (not a local path, and not starting with a dash)"
        )
    return text


def _graph_backup_remote_raw() -> tuple[str, str]:
    env = (os.environ.get(GRAPH_BACKUP_REMOTE_ENV) or "").strip()
    if env:
        return env, "env"
    stored = str(load_config().get("graph_backup_remote", "") or "").strip()
    if stored:
        return stored, "config"
    return DEFAULT_GRAPH_BACKUP_REMOTE, "default"


def graph_backup_remote() -> str:
    """The resolved destination, checked whatever it came from."""
    raw, source = _graph_backup_remote_raw()
    try:
        return check_graph_backup_remote(raw)
    except ValueError as exc:
        where = {"env": f"the environment variable {GRAPH_BACKUP_REMOTE_ENV}",
                 "config": "config.json"}.get(source, "the default")
        raise ValueError(f"{exc}; the value in {where} does not qualify") from None


def graph_backup_remote_status() -> dict:
    """The destination and where it came from: env, config or default.
    An invalid value is reported as ``error``, never passed on."""
    raw, source = _graph_backup_remote_raw()
    out = {"value": raw, "source": source, "default": DEFAULT_GRAPH_BACKUP_REMOTE}
    try:
        out["value"] = graph_backup_remote()
    except ValueError as exc:
        out["error"] = str(exc)
    return out


def set_graph_backup_remote(value: "str | None") -> Path:
    data = _load_config_for_mutation()
    if not (value or "").strip():
        data.pop("graph_backup_remote", None)
    else:
        data["graph_backup_remote"] = check_graph_backup_remote(str(value))
    return save_config(data)


def set_usage_accounts(raw: "str | list | None") -> Path:
    """Store ``usage_accounts``, the accounts ``khipu usage`` reads (JSON text
    or a list of ``{label, harness, home?}``, checked by
    ``khipu.usage.parse_usage_accounts``). None or an empty list removes the
    key: every installed CLI with its default login."""
    from khipu.usage import parse_usage_accounts

    entries = [] if raw is None else parse_usage_accounts(raw)
    data = _load_config_for_mutation()
    if entries:
        data["usage_accounts"] = entries
    else:
        data.pop("usage_accounts", None)
    return save_config(data)


# ---------------------------------------------------------------------------
# Where a subscription CLI and its Node live, per Mac (settings clarity phase
# 9, decision 10). The app starts under launchd's bare PATH, where a CLI under
# a Node version manager is either not found or found without its node; these
# let the owner repair that in the app. ``synth_cli.binary`` reads
# ``cli_path.<cli>`` after ``KHIPU_<NAME>_CLI``; ``usage._probe_path`` puts
# ``node_folder`` on the probe PATH. Only an executable file (a CLI) or a
# folder holding an executable ``node`` is stored.
# ---------------------------------------------------------------------------

CLI_SETTINGS = ("cli_path.claude", "cli_path.codex", "cli_path.grok", "cli_path.muse", "node_folder")


def _executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def cli_setting(key: str) -> str | None:
    """The stored path for ``key`` (one of ``CLI_SETTINGS``), or None when
    unset or no longer usable (a moved CLI, a removed Node)."""
    if key not in CLI_SETTINGS:
        raise KeyError(f"unknown CLI setting {key!r}; known: {list(CLI_SETTINGS)}")
    stored = load_config().get(key)
    if not isinstance(stored, str) or not stored.strip():
        return None
    try:
        path = Path(stored.strip()).expanduser()
    except RuntimeError:
        return None
    if not path.is_absolute():
        # A hand-edited relative value would resolve against whatever the
        # caller's working folder is: the writer refuses it, so does this.
        return None
    usable = _executable_file(path / "node") if key == "node_folder" else _executable_file(path)
    return str(path) if usable else None


def set_cli_setting(key: str, value: "str | None") -> Path:
    if key not in CLI_SETTINGS:
        raise KeyError(f"unknown CLI setting {key!r}; known: {list(CLI_SETTINGS)}")
    data = _load_config_for_mutation()
    if value is None or not str(value).strip():
        data.pop(key, None)
        return save_config(data)
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{key} must be a full path")
    if key == "node_folder":
        if not _executable_file(path / "node"):
            raise ValueError("node_folder must be a folder holding an executable node")
    elif not _executable_file(path):
        raise ValueError(f"{key} must be an executable file")
    data[key] = str(path)
    return save_config(data)


def cli_settings_status() -> dict:
    """Each CLI setting as stored and whether it is usable now."""
    stored = load_config()
    return {key: {"value": stored.get(key) if isinstance(stored.get(key), str) else None,
                  "usable": cli_setting(key) is not None} for key in CLI_SETTINGS}


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


def float_settings_status() -> dict:
    """Every 0-1 knob with its effective value, source (env / file / default)
    and default, for ``khipu config``."""
    out = {}
    for key, (env_name, default) in FLOAT_SETTINGS.items():
        env = (os.environ.get(env_name) or "").strip()
        src = "default"
        if env:
            try:
                float(env)
                src = "env"
            except ValueError:
                pass
        if src == "default":
            stored = load_config().get(key)
            if isinstance(stored, (int, float)) and not isinstance(stored, bool):
                src = "file"
        out[key] = {"value": float_setting(key), "source": src, "default": default}
    return out
