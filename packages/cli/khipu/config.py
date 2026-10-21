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

import contextlib
import hashlib
import json
import os
import re
import threading
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


# An account change (``accounts._transact``) writes this key in the same
# write as the account list it makes, and clears it once the routing record
# says the change is done. Every read of the config (``load_config``,
# ``mutate_config``) that finds it finishes or rolls back that change first
# (``accounts.recover``), so no reader sees a list the record and the usage
# cache do not yet agree with.
ACCOUNT_CHANGE_OPEN = "account_change_open"


def _read_config() -> dict:
    path = config_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _settle_account_change(data: dict) -> bool:
    """Settle an unfinished account change ``data`` names; True when one was
    settled (read the document again). Skipped while this thread holds the
    config lock (the routing lock is taken first, never after it) or is
    settling one already."""
    if not data.get(ACCOUNT_CHANGE_OPEN) or getattr(_held, "depth", 0) or getattr(_held, "settling", False):
        return False
    from khipu import accounts

    _held.settling = True
    try:
        accounts.recover()
    except accounts.AccountsError:
        # Not settled yet: the document as stored. Every account write is
        # refused until it is (``accounts._locked``).
        return False
    finally:
        _held.settling = False
    return True


@secret_result("config")
def load_config() -> dict:
    data = _read_config()
    if _settle_account_change(data):
        data = _read_config()
    return data


def read_config_unsettled() -> dict:
    """The document as stored, an unfinished account change and all: for a
    reader that holds a lock an account change takes after the routing lock
    (the usage cache's), which must not wait on that lock here."""
    return _read_config()


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


def check_config_readable() -> None:
    """Raise ``InvalidConfigDocument`` when config.json is there but can't be
    read or parsed (or isn't an object): for a reader that must not lean on
    the empty document ``load_config`` returns then. Writes nothing."""
    _load_config_for_mutation()


# The one write path for config.json. Every writer in the package changes the
# document inside ``mutate_config()``: it takes ``config_write_lock``, reads the
# file as it is now, hands the writer that copy and writes it back when the
# block ends. No module writes a whole-file copy it read earlier, so a save of
# one setting can never undo another writer's save that landed in between
# (``tests/test_config_write_path.py`` keeps every other module off the file).
_held = threading.local()


def _write_config(data: dict) -> Path:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _body(data: dict) -> str:
    return json.dumps(data, sort_keys=True, default=str)


@contextlib.contextmanager
def mutate_config():
    """Read, change and write config.json under the one lock. Yields the
    document as stored now; the block changes it in place and it is written
    when the block ends without an error (only if it changed). A nested
    block in the same thread shares the outer one's document."""
    if not getattr(_held, "depth", 0):
        _settle_account_change(_read_config())
    with config_write_lock():
        if getattr(_held, "data", None) is not None:
            yield _held.data
            if _body(_held.data) != _held.written:
                _write_config(_held.data)
                _held.written = _body(_held.data)
            return
        data = _load_config_for_mutation()
        _held.data, _held.written = data, _body(data)
        try:
            yield data
            if _body(data) != _held.written:
                _write_config(data)
        finally:
            _held.data = _held.written = None


def save_config(data: dict) -> Path:
    """Write the document ``mutate_config`` handed this thread now, before
    its block ends (a writer that must land before it does something else).
    Any other dict is refused: it would be a stale whole-file copy."""
    if data is not getattr(_held, "data", None):
        raise RuntimeError("config.json is written only inside config.mutate_config()")
    path = _write_config(data)
    _held.written = _body(data)
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
    with mutate_config() as data:
        data["capture_mode"] = mode
        return config_file()


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
    with mutate_config() as data:
        if url:
            data["gateway_url"] = url
        else:
            data.pop("gateway_url", None)
        return config_file()


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
    # z.ai GLM Coding Plan key (a file holding only the key), read for plan usage
    "glm_key_file": ("KHIPU_GLM_KEY_FILE",),
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
    with mutate_config() as data:
        if value is None or not str(value).strip():
            data.pop(key, None)
        else:
            data[key] = str(Path(str(value)).expanduser())
        return config_file()


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
    with mutate_config() as data:
        data[key] = float(value)
        return config_file()


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
    with mutate_config() as data:
        if cleaned:
            data[key] = cleaned
        else:
            data.pop(key, None)
        return config_file()


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
    with mutate_config() as data:
        if value is None:
            data.pop(key, None)
        else:
            data[key] = parse_int_setting(key, value)
        return config_file()


# ---------------------------------------------------------------------------
# Lengths of time in whole minutes, each with its default and its allowed
# range. `doctor_rate_window_minutes` is how far back doctor's rate checks
# (prompt-time recall outcomes, search fallbacks) look, so a past outage ages
# out on its own (docs/plans/2026-10-16-offline-and-self-healing.md, D).
# ---------------------------------------------------------------------------

MINUTE_SETTINGS: dict[str, tuple[int, int, int]] = {
    "doctor_rate_window_minutes": (60, 5, 7 * 24 * 60),
    # How long the promotion gate waits for an unreachable hub before it
    # queues the promotion to run again once the hub answers (E). 0: no wait.
    "promote_hub_wait_minutes": (10, 0, 4 * 60),
}


def parse_minute_setting(key: str, raw: "str | int") -> int:
    """``raw`` as whole minutes inside the key's range; ValueError names the
    key and the range otherwise."""
    _default, low, high = MINUTE_SETTINGS[key]
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a whole number of minutes, got {raw!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high} minutes, got {value}")
    return value


def minute_setting(key: str) -> int:
    """The stored minutes for ``key``, or its default when unset or invalid."""
    default, _low, _high = MINUTE_SETTINGS[key]
    stored = load_config().get(key)
    if isinstance(stored, bool) or not isinstance(stored, int):
        return default
    try:
        return parse_minute_setting(key, stored)
    except ValueError:
        return default


def set_minute_setting(key: str, value: "str | int | None") -> Path:
    if key not in MINUTE_SETTINGS:
        raise KeyError(f"unknown minutes setting {key!r}; known: {sorted(MINUTE_SETTINGS)}")
    with mutate_config() as data:
        if value is None:
            data.pop(key, None)
        else:
            data[key] = parse_minute_setting(key, value)
        return config_file()


def minute_settings_status() -> dict:
    """Every minutes setting with its value, source and range, for
    ``khipu config``."""
    out = {}
    stored = load_config()
    for key, (default, low, high) in MINUTE_SETTINGS.items():
        value = minute_setting(key)
        source = "file" if key in stored and stored.get(key) == value else "default"
        out[key] = {"value": value, "source": source, "default": default, "min": low, "max": high}
    return out


# ---------------------------------------------------------------------------
# Counts: how many of something to show or take at once, whole numbers >= 0,
# 0 = no limit (every limit Khipu enforces is liftable on the spot).
# `owed_orchestrator_slice_max` is how many items for the orchestrator (owner
# `agent`, from its jobs) a conversation's session-start list shows
# (docs/plans/2026-10-16-owed-and-ledger-ssot.md, decision 2).
# `owed_page_max` is the most items one `khipu_owed` page returns, and
# `owed_bulk_max` the most ids one `khipu_owed_update` call changes (the same
# scope, design (c)). `owed_tidy_calls` is the most model calls one `khipu
# owed tidy` plan makes for the items its rules cannot sort (design (d),
# decision 4); past it those items are kept as they are and counted.
# ---------------------------------------------------------------------------

COUNT_SETTINGS: dict[str, int] = {
    "owed_orchestrator_slice_max": 3,
    "owed_page_max": 200,
    "owed_bulk_max": 500,
    "owed_tidy_calls": 25,
}


def parse_count_setting(key: str, raw: "str | int") -> int:
    """``raw`` as a whole number >= 0; ValueError names the key otherwise."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a whole number >= 0 (0 for no limit), got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{key} must be a whole number >= 0 (0 for no limit), got {value}")
    return value


def count_setting(key: str) -> int:
    """The stored count for ``key``, or its default when unset or invalid.
    0 means no limit."""
    if key not in COUNT_SETTINGS:
        raise KeyError(f"unknown count setting {key!r}; known: {sorted(COUNT_SETTINGS)}")
    stored = load_config().get(key)
    if isinstance(stored, int) and not isinstance(stored, bool) and stored >= 0:
        return stored
    return COUNT_SETTINGS[key]


def set_count_setting(key: str, value: "str | int | None") -> Path:
    if key not in COUNT_SETTINGS:
        raise KeyError(f"unknown count setting {key!r}; known: {sorted(COUNT_SETTINGS)}")
    with mutate_config() as data:
        if value is None:
            data.pop(key, None)
        else:
            data[key] = parse_count_setting(key, value)
        return config_file()


def count_settings_status() -> dict:
    """Every count setting with its value, source and default, for
    ``khipu config``."""
    stored = load_config()
    out = {}
    for key, default in COUNT_SETTINGS.items():
        value = count_setting(key)
        source = "file" if key in stored and stored.get(key) == value else "default"
        out[key] = {"value": value, "source": source, "default": default, "no_limit": 0}
    return out


# ---------------------------------------------------------------------------
# Choice settings: one of a fixed set of words. `synth_fallback` says what
# happens when a summariser provider other than Gemini fails a window
# (account limit, not logged in, transport error, a reply that is not the
# capture JSON): `gemini` retries that window on Gemini Flash when a Gemini
# key resolves, `none` leaves the job queued for a later retry. Unset = gemini.
# `owed_pr_check_github`: whether the owed close rules ask GitHub, read-only
# through `gh pr view`, whether a pull request an item names has merged
# (docs/plans/2026-10-16-owed-and-ledger-ssot.md, decision 1). Unset = on.
# ---------------------------------------------------------------------------

CHOICE_SETTINGS: dict[str, tuple[str, ...]] = {
    "synth_fallback": ("gemini", "none"),
    "owed_pr_check_github": ("on", "off"),
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
    with mutate_config() as data:
        if value is None:
            data.pop(key, None)
        else:
            word = str(value).strip().lower()
            if word not in choices:
                raise ValueError(f"{key} must be one of {', '.join(choices)}, got {value!r}")
            data[key] = word
        return config_file()


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
    with mutate_config() as data:
        if not (value or "").strip():
            data.pop("graph_backup_remote", None)
        else:
            data["graph_backup_remote"] = check_graph_backup_remote(str(value))
        return config_file()


CONFIG_LOCK_NAME = ".config.lock"
# What a version of the account list is made of: the list, "Read no
# accounts", and a conversation's request still waiting for the owner.
_ACCOUNT_STATE_KEYS = ("usage_accounts", "usage_accounts_none", "usage_accounts_request")


@contextlib.contextmanager
def config_write_lock():
    """One read-compare-write of config.json at a time across processes
    (flock; released if the holder dies). ``mutate_config`` holds it from
    its read to its save; it may be taken again by the thread holding it."""
    import fcntl  # here, not at the top: config is imported where fcntl isn't (the Windows lock backend)

    if getattr(_held, "depth", 0):
        # Already held by this thread (flock is per open file, so taking it
        # again here would wait on itself).
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    path = config_file().with_name(CONFIG_LOCK_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _held.depth = 1
        yield
    finally:
        _held.depth = 0
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# The one format of a version a write hands back (``--expect``), defined
# here and nowhere else in the package: ASCII letters and digits in parts
# joined by single dots, at most ``VERSION_MAX`` characters. The account
# list's is "a" and 16 hex digits; the priority's is "p" and its event's
# mark, then "." and the account list's version when there is one
# (``p12.a4def5fc278cfc609``). The desktop app checks the same format
# (``checked_expect`` in apps/desktop/src-tauri/src/lib.rs), and
# tests/test_accounts.py runs that Rust check on versions this code makes.
VERSION_MAX = 64
_VERSION_PART = re.compile(r"[A-Za-z0-9]+")


def is_version(text: str) -> bool:
    return 0 < len(text) <= VERSION_MAX and all(_VERSION_PART.fullmatch(p) for p in text.split("."))


def version_part(raw: str) -> str:
    """``raw`` as one part of a version: as it is when it is letters and
    digits, else a short digest of it (an id appended by hand)."""
    if _VERSION_PART.fullmatch(raw) and len(raw) <= 24:
        return raw
    return "h" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def accounts_version(data: dict | None = None) -> str:
    """The version of the stored account list: a short digest of what
    ``_ACCOUNT_STATE_KEYS`` hold. A write that carries the version it read
    (``expect``) is refused once it moved."""
    data = load_config() if data is None else data
    body = {k: data.get(k) for k in _ACCOUNT_STATE_KEYS}
    text = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "a" + hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


def check_accounts_version(data: dict, expect: "str | None") -> None:
    """Refuse (``AccountsError`` ``version_moved``) when ``expect`` is given
    and the stored list is no longer the one it names."""
    from khipu.usage import AccountsError

    if expect is not None and str(expect).strip() != accounts_version(data):
        raise AccountsError("the account list changed after it was read; read it again and redo the change",
                            "version_moved")


AUTO_TRACK_KEY = "usage_auto_track"
AUTO_TRACK_MIGRATION_KEY = "usage_auto_track_default_migrated"


def auto_track() -> bool:
    """Whether found accounts a conversation used are tracked without asking
    (settings clarity phase 9; per Mac). On by default only for a genuinely
    new install — no config file at all (decision 15). An install that
    already had a config file when this default arrived, with no stored
    key, may have turned it off under the old delete-the-key behaviour:
    the migration below stores an explicit ``false`` plus its marker once,
    so it stays off. Any stored value other than literal ``true`` — false,
    null, 0, a string, a malformed document — reads as off."""
    data = load_config()
    if AUTO_TRACK_KEY in data:
        return data[AUTO_TRACK_KEY] is True
    if not config_file().is_file():
        return True
    try:
        with mutate_config() as stored:
            if AUTO_TRACK_KEY not in stored:
                stored[AUTO_TRACK_KEY] = False
                stored[AUTO_TRACK_MIGRATION_KEY] = True
    except Exception:  # noqa: BLE001 — a malformed document reads as off, never on
        return False
    return False


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
    with mutate_config() as data:
        if value is None or not str(value).strip():
            data.pop(key, None)
            return config_file()
        path = Path(str(value).strip()).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{key} must be a full path")
        if key == "node_folder":
            if not _executable_file(path / "node"):
                raise ValueError("node_folder must be a folder holding an executable node")
        elif not _executable_file(path):
            raise ValueError(f"{key} must be an executable file")
        data[key] = str(path)
        return config_file()


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
