"""Keeps this Mac's copy of the hub (the replica) current between nightlies.

The ``replica_refresh`` background job runs ``khipu snapshot heal`` every
``replica_check_every`` seconds (docs/plans/2026-10-16-offline-and-self-healing.md,
design A). Each run decides from this Mac's own evidence whether the copy needs
work, and does it only when the hub answers:

- no copy, or the last full dump failed or is overdue: a full dump;
- a measurement made after the last update found the copy behind: a full dump;
- the hub answers again after a run found it unreachable: an update at once;
- the last update is ``replica_refresh_after`` old or more: an update.

An update is an incremental pull (``hub_snapshot.pull_changes``: what other
machines and the gateway wrote, missed patches, re-built vectors, decisions);
a full dump is its fallback, when the pull has too much to catch up or fails
while the hub answers.

Otherwise it does nothing and opens no connection. It never runs from a hook,
a status read or a tab click. Every dump goes through ``hub_snapshot.refresh``
and its lock, so this job, the nightly and ``khipu snapshot refresh`` never
dump at once; a run that finds a dump in progress skips.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# Matt, 2026-10-16 (decision 2): within 15 minutes of the hub while it answers,
# changeable in Settings. The check interval is rendered into the job's plist.
SETTINGS: dict[str, tuple[str, Any, str]] = {
    "replica_refresh_after": ("duration", 15 * 60,
                              "Bring this Mac's copy up to date when its last update is this old (0: at every check)"),
    "replica_check_every": ("duration", 5 * 60, "How often the background job checks this Mac's copy"),
}
CHECK_EVERY_MIN_S = 60
STATE_NAME = "replica-heal.json"
WAKE_NAME = "replica-wake"


def _coerce(kind: str, value: Any, key: str) -> Any:
    from khipu import routing

    out = routing._coerce(kind, value, key)
    if key == "replica_check_every" and out < CHECK_EVERY_MIN_S:
        raise ValueError(f"{key} must be at least {CHECK_EVERY_MIN_S} seconds")
    return out


def settings(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
        raise KeyError(f"unknown replica setting {key!r}")
    with mutate_config() as data:
        if raw is None:
            data.pop(key, None)
        else:
            data[key] = _coerce(SETTINGS[key][0], raw, key)
    return config_file()


def check_every_s() -> int:
    return int(settings()["replica_check_every"])


def schedule_words() -> str:
    seconds = check_every_s()
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"every {minutes} min"
    return f"every {seconds} s"


# ---- state --------------------------------------------------------------------


def state_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / STATE_NAME


def wake_path() -> Path:
    """The file the job's plist watches: touching it starts a check now."""
    from khipu.paths import data_dir

    return data_dir() / "state" / WAKE_NAME


def ensure_wake_file() -> Path:
    path = wake_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return path


def read_state() -> dict[str, Any]:
    try:
        loaded = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_state(state: Mapping[str, Any]) -> None:
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        return
    dest = state_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".replica-heal.", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(name, dest)
    finally:
        Path(name).unlink(missing_ok=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(val: Any) -> datetime | None:
    from khipu.hub_snapshot import _utc as snapshot_utc

    return snapshot_utc(val)


def _age(now: datetime, when: Any) -> float | None:
    at = _utc(when)
    return None if at is None else (now - at).total_seconds()


# ---- the decision -----------------------------------------------------------


def last_update_at(state: Mapping[str, Any], health: Mapping[str, Any]) -> datetime | None:
    """The newest of this job's last good update and the last full dump (the
    nightly's or a manual one): either brings the copy level with the hub."""
    stamps = [_utc(state.get("last_synced_at")), _utc(health.get("refreshed_at"))]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def plan(
    *,
    now: datetime,
    state: Mapping[str, Any],
    health: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    last_known: Mapping[str, Any] | None,
    refresh_after: float,
) -> tuple[str, str]:
    """``(action, reason)``: ``dump`` (a full dump), ``sync`` (bring the copy
    up to date) or ``none``. Local evidence only: no connection is opened."""
    from khipu.hub_snapshot import SNAPSHOT_MAX_AGE_S, _ts_not_before

    if state.get("hub_unreachable_since"):
        return "sync", "hub_back"
    if not health.get("exists") or health.get("age_seconds") is None:
        action, reason = "dump", "missing"
    elif health["age_seconds"] > SNAPSHOT_MAX_AGE_S:
        action, reason = "dump", "overdue"
    elif isinstance(sidecar.get("refresh_error"), dict) and _ts_not_before(
            sidecar["refresh_error"].get("at"), health.get("refreshed_at")):
        action, reason = "dump", "refresh_failed"
    elif (last_known and last_known.get("state") == "behind"
          and last_known.get("refreshed_at") == health.get("refreshed_at")
          and _ts_not_before(last_known.get("checked_at"), state.get("last_synced_at") or health.get("refreshed_at"))):
        action, reason = "dump", "behind"
    else:
        since = last_update_at(state, health)
        age = None if since is None else (now - since).total_seconds()
        if age is not None and 0 <= age < refresh_after:
            return "none", "fresh"
        action, reason = "sync", "due"
    # A failed attempt is not repeated sooner than the refresh window: a dump
    # that keeps failing while the hub answers must not run every check.
    tried = _age(now, state.get("last_failed_at"))
    if tried is not None and 0 <= tried < refresh_after:
        return "none", "recently_failed"
    return action, reason


# ---- the run ------------------------------------------------------------------


def _hub_reachable() -> tuple[bool, str | None]:
    from khipu.hub_snapshot import HUB_CONNECT_TIMEOUT_S, try_hub_connect

    try:
        with try_hub_connect(connect_timeout=HUB_CONNECT_TIMEOUT_S) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True, None
    except Exception as exc:  # noqa: BLE001 - any failure to connect is "not reachable"
        return False, f"{type(exc).__name__}: {exc}"[:300]


def _full_dump() -> dict[str, Any]:
    from khipu.hub_snapshot import refresh

    try:
        out = refresh()
    except Exception as exc:  # noqa: BLE001 - reported in the run's result
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {k: v for k, v in out.items() if k != "path"}


def _sync() -> dict[str, Any]:
    from khipu import hub_snapshot

    try:
        pulled = hub_snapshot.pull_changes()
    except Exception as exc:  # noqa: BLE001 - a pull that cannot run is a dump's job
        pulled = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    if pulled.get("ok"):
        return pulled
    # Waiting out a dump in progress or an unreachable hub is the caller's job;
    # anything else means the pull cannot bring the copy level.
    if pulled.get("hub_unreachable") or "in progress" in str(pulled.get("error") or ""):
        return pulled
    return {**_full_dump(), "fallback_from_pull": pulled.get("error")}


def run() -> dict[str, Any]:
    """One check. ``ok`` is false only when the hub answered and the work
    failed; an unreachable hub is waited out, not a failure."""
    from khipu import hub_snapshot

    now = _now()
    state = read_state()
    health = hub_snapshot.snapshot_health()
    action, reason = plan(
        now=now, state=state, health=health, sidecar=hub_snapshot._read_sidecar(),
        last_known=hub_snapshot._read_last_known(), refresh_after=float(settings()["replica_refresh_after"]),
    )
    state["last_run_at"] = now.isoformat()
    out: dict[str, Any] = {"ok": True, "action": action, "reason": reason}
    if action == "none":
        _write_state(state)
        return out
    reachable, error = _hub_reachable()
    if not reachable:
        state.setdefault("hub_unreachable_since", now.isoformat())
        state["last_error"] = error
        _write_state(state)
        return {**out, "action": "waiting", "planned": action, "reason": "hub_unreachable",
                "since": state["hub_unreachable_since"], "error": error}
    state.pop("hub_unreachable_since", None)
    result = _full_dump() if action == "dump" else _sync()
    out["result"] = result
    if result.get("ok"):
        state["last_synced_at"] = _now().isoformat()
        state.pop("last_failed_at", None)
        state.pop("last_error", None)
    elif "in progress" in str(result.get("error") or ""):
        out["action"] = "skipped"
        out["reason"] = "refresh_in_progress"
    elif result.get("hub_unreachable"):
        # Lost between the probe and the pull: waited out like a failed probe.
        state["hub_unreachable_since"] = now.isoformat()
        state["last_error"] = result.get("error")
        out["action"] = "waiting"
        out["reason"] = "hub_unreachable"
    else:
        state["last_failed_at"] = _now().isoformat()
        state["last_error"] = result.get("error")
        out["ok"] = False
    _write_state(state)
    return out
