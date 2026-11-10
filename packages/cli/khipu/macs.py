"""One status row per Mac, shared through the hub (``khipu macs --json``).

Each Mac writes only its own row of ``mac_status`` (migration 0052): when ``khipu health`` runs and on the
five-minute snapshot heal timer. A reader gets this Mac live plus every other Mac's row; a row the hub last
heard from more than ``STALE_AFTER_S`` ago is marked ``stale``, never shown as current.

The row is a closed list of fields (``project``), built the way the routing copy builds its payload: only the
fields named here are copied, each in its named shape, and everything else is dropped. It never holds a path, a
login home, transcript text, a prompt or a key value. The same projection runs on write and again on read, so
a row written by a newer or older build can never put an unlisted field in front of the app.

Nothing here raises to a caller. The other Mac may run an older build: no table, no row or an unknown field is
"this Mac only", labelled. A write that fails (hub down) is reported in the result and never fails ``health``
or the heal timer.

Older Mac-name helpers to fold into ``mac_label`` / ``name_of`` later (their files are being rewritten, so this
slice leaves them alone): ``routing_replication.mac_label`` and ``health_five``'s old own copy.
"""
from __future__ import annotations

import json
import math
import re
import socket
from datetime import datetime, timezone
from typing import Any

STALE_AFTER_S = 15 * 60
HUB_TIMEOUT_S = 5
OTHER_MAC = "Your other Mac"

# The closed list. Anything not named here is dropped, on write and on read.
ROW_FIELDS = ("name", "version", "saving", "health", "running", "signins")
HEALTH_FIELDS = ("checked_at", "state", "areas", "routing_version", "free_gb", "last_backup_at")
AREA_FIELDS = ("key", "state")
RUNNING_FIELDS = ("count", "silent")
SILENT_FIELDS = ("name", "since")
SIGNIN_FIELDS = ("harness", "label", "signed_in")

STATES = ("ok", "warn", "err", "unknown")
AREA_KEYS = ("saving", "finding", "routing", "database", "jobs")
_SHORT = re.compile(r"[0-9a-f]{12}")
_NAME = re.compile(r"[^\x00-\x1f/\\~][^\x00-\x1f/\\]{0,59}")
_RUNNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,39}")
_HARNESS = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+()_ -]{0,59}")
_MAX_LIST = 20


# ---- the closed projection -------------------------------------------------------------------------------------


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value) -> str | None:
    moment = _parse(value)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _text(value, pattern: re.Pattern) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if pattern.fullmatch(value) else None


def _count(value) -> int | None:
    return value if type(value) is int and 0 <= value <= 100000 else None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 1) if 0 <= value <= 1e7 else None


def _health(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    areas = []
    for item in (raw.get("areas") if isinstance(raw.get("areas"), list) else [])[:len(AREA_KEYS)]:
        if isinstance(item, dict) and item.get("key") in AREA_KEYS and item.get("state") in STATES:
            areas.append({"key": item["key"], "state": item["state"]})
    out = {"checked_at": _iso(raw.get("checked_at")),
           "state": raw["state"] if raw.get("state") in STATES else None,
           "areas": areas,
           "routing_version": _text(raw.get("routing_version"), _SHORT),
           "free_gb": _number(raw.get("free_gb")),
           "last_backup_at": _iso(raw.get("last_backup_at"))}
    return {k: out[k] for k in HEALTH_FIELDS}


def _running(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    silent = []
    for item in (raw.get("silent") if isinstance(raw.get("silent"), list) else [])[:_MAX_LIST]:
        name = _text(item.get("name"), _RUNNER) if isinstance(item, dict) else None
        if name:
            silent.append({"name": name, "since": _iso(item.get("since"))})
    return {"count": _count(raw.get("count")), "silent": silent}


def _signins(raw) -> list | None:
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw[:_MAX_LIST]:
        if not isinstance(item, dict) or not isinstance(item.get("signed_in"), bool):
            continue
        harness, label = _text(item.get("harness"), _HARNESS), _text(item.get("label"), _NAME)
        if harness and label:
            out.append({"harness": harness, "label": label, "signed_in": item["signed_in"]})
    return out


def project(raw) -> dict:
    """The closed row: exactly ``ROW_FIELDS``, each in its named shape or ``None``. Unlisted fields are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    return {"name": _text(raw.get("name"), _NAME), "version": _text(raw.get("version"), _VERSION),
            "saving": raw["saving"] if isinstance(raw.get("saving"), bool) else None,
            "health": _health(raw.get("health")), "running": _running(raw.get("running")),
            "signins": _signins(raw.get("signins"))}


# ---- this Mac's names and identity ------------------------------------------------------------------------------


def mac_label() -> str:
    """The name the owner gave this Mac, else its host name."""
    try:
        from khipu import routing

        given = (routing.settings().get("routing_mac_names") or {}).get(routing._local_mac())
        if isinstance(given, str) and given.strip():
            return given.strip()
    except Exception:  # noqa: BLE001 - a label is never worth failing a report
        pass
    return socket.gethostname().split(".")[0] or "This Mac"


def name_of(mac: str | None, row_name: str | None = None) -> str:
    """What the owner calls another Mac: their own name for it, else the name its row carries, else 'Your other Mac'."""
    try:
        from khipu import routing

        given = (routing.settings().get("routing_mac_names") or {}).get(mac) if isinstance(mac, str) else None
        if isinstance(given, str) and given.strip():
            return given.strip()
    except Exception:  # noqa: BLE001
        pass
    return row_name or OTHER_MAC


def local_mac_id() -> str | None:
    try:
        from khipu import routing

        return routing._local_mac()
    except Exception:  # noqa: BLE001
        return None


def routing_version() -> str | None:
    """A short digest of this Mac's routing record that another Mac can compare: the rule ids with their
    revisions and the priority revision. (The record's own ``version`` is a per-Mac fold count, so two Macs
    holding the same record can show different numbers.) ``None`` when there is no saved record."""
    try:
        import hashlib

        from khipu import routing

        view = routing._load_view()
        if not view or not view.get("record"):
            return None
        shown = routing.public_view(view, datetime.now(timezone.utc))
        rules = sorted((str(r.get("id")), str(r.get("revision"))) for r in shown.get("rules") or [] if isinstance(r, dict))
        priority = (shown.get("priority") or {}).get("revision") if isinstance(shown.get("priority"), dict) else None
        body = json.dumps({"rules": rules, "priority": priority}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:12]
    except Exception:  # noqa: BLE001 - no digest is "unknown", never an error
        return None


def sign_ins() -> list[dict] | None:
    """Each tracked account and whether it reads as signed in on this Mac, from what is cached: no probe, no path."""
    try:
        from khipu import usage

        cache, accounts = usage._load_cache(), usage.all_accounts()
        out = []
        for account in accounts:
            if account.get("invalid"):
                continue
            row = cache.get(usage._key(account))
            failed = isinstance(row, dict) and (row.get("error") if row.get("ok") is False else row.get("live_error")) == "not_signed_in"
            out.append({"harness": account.get("harness"), "label": account.get("label"), "signed_in": not failed})
        return out
    except Exception:  # noqa: BLE001
        return None


def version_stamp() -> str | None:
    try:
        from khipu import build_info

        return build_info.stamp()
    except Exception:  # noqa: BLE001
        return None


# ---- what health last said (so the heal timer can report without running doctor) ---------------------------------


def _cache_path():
    from khipu.paths import data_dir

    return data_dir() / "mac-status.json"


def _from_health(doc: dict) -> dict:
    areas = [{"key": a.get("key"), "state": a.get("state")} for a in doc.get("areas") or [] if isinstance(a, dict)]
    states = [a["state"] for a in areas]
    worst = next((s for s in ("err", "warn", "unknown") if s in states), "ok" if states else None)
    saving_area = next((a for a in areas if a["key"] == "saving"), None)
    silent = [{"name": s.get("name"), "since": s.get("since")} for s in doc.get("silent_runners") or []
              if isinstance(s, dict)]
    return {"saving": None if not saving_area or saving_area["state"] == "unknown" else saving_area["state"] != "err",
            "health": {"checked_at": doc.get("checked_at"), "state": worst, "areas": areas,
                       "routing_version": routing_version(), "free_gb": doc.get("free_gb"),
                       "last_backup_at": doc.get("last_backup_at")},
            "running": {"count": doc.get("jobs_running"), "silent": silent}}


def remember_health(doc: dict) -> dict:
    """Keep the closed part of the latest health document on this Mac for the timer; returns it."""
    kept = project(_from_health(doc))
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: kept[k] for k in ("saving", "health", "running")}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return kept


def _remembered() -> dict:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def local_row(doc: dict | None = None) -> dict:
    """This Mac's closed row now. With a health document, that is what it reports; without one (the timer), the
    last health this Mac kept; with neither, saving, health and running are ``None`` (not known)."""
    base = _from_health(doc) if doc else _remembered()
    row = project(dict(base, name=mac_label(), version=version_stamp(), signins=sign_ins()))
    if row["health"] is not None and not doc:
        row["health"]["routing_version"] = routing_version()
    return row


# ---- the hub ------------------------------------------------------------------------------------------------------


def publish(row: dict | None = None, *, mac: str | None = None) -> dict:
    """Write this Mac's own row. Never raises: ``{"ok": False, "why": ...}`` when it could not."""
    try:
        mac = mac or local_mac_id()
        if not mac:
            return {"ok": False, "why": "no_mac_identity"}
        from khipu import hub_reach

        if hub_reach.known_unreachable():
            return {"ok": False, "why": "hub_unreachable"}
        row = project(row if row is not None else local_row())
        from khipu import db

        with db.connect(statement_timeout_s=HUB_TIMEOUT_S) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mac_status (mac, name, version, seen_at, saving, health, running, signins) "
                    "VALUES (%s, %s, %s, clock_timestamp(), %s, %s::jsonb, %s::jsonb, %s::jsonb) "
                    "ON CONFLICT (mac) DO UPDATE SET name = EXCLUDED.name, version = EXCLUDED.version, "
                    "seen_at = EXCLUDED.seen_at, saving = EXCLUDED.saving, health = EXCLUDED.health, "
                    "running = EXCLUDED.running, signins = EXCLUDED.signins",
                    (mac, row["name"], row["version"], row["saving"],
                     *(None if row[k] is None else json.dumps(row[k]) for k in ("health", "running", "signins"))))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - a missing table or a hub that is down is not a failure of the caller
        return {"ok": False, "why": type(exc).__name__}


def read_rows() -> tuple[list[dict], str]:
    """(every row the hub holds, projected, with ``mac`` and ``age_s`` by the hub's own clock), and how it went:
    ``ok`` | ``unreachable`` | ``no_table``. Never raises."""
    try:
        from khipu import hub_reach

        if hub_reach.known_unreachable():
            return [], "unreachable"
        from khipu import db

        with db.connect(statement_timeout_s=HUB_TIMEOUT_S) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT mac, name, version, seen_at, saving, health, running, signins, "
                            "extract(epoch FROM (clock_timestamp() - seen_at)) FROM mac_status ORDER BY mac")
                fetched = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        return [], "no_table" if type(exc).__name__ == "UndefinedTable" else "unreachable"
    rows = []
    for mac, name, version, seen_at, saving, health, running, signins, age in fetched:
        shown = project({"name": name, "version": version, "saving": saving, "health": health,
                         "running": running, "signins": signins})
        shown.update(mac=str(mac), seen_at=_iso(seen_at), age_s=max(0, int(age)) if age is not None else None)
        rows.append(shown)
    return rows, "ok"


# ---- the answer ---------------------------------------------------------------------------------------------------


def _entry(mac: str | None, row: dict, *, this: bool, seen_at: str | None, age_s: int | None) -> dict:
    stale = age_s is None or age_s > STALE_AFTER_S
    return {"mac": mac, "name": row["name"] if this else name_of(mac, row["name"]), "this_mac": this,
            "version": row["version"], "seen_at": seen_at, "age_s": age_s, "stale": stale, "saving": row["saving"],
            "health": row["health"], "running": row["running"], "signins": row["signins"]}


def others(*, rows: list[dict] | None = None, mine: str | None = None) -> tuple[list[dict], str]:
    """The other Macs' entries (not this Mac) and how the hub read went."""
    if rows is None:
        rows, how = read_rows()
    else:
        how = "ok"
    mine = mine or local_mac_id()
    return [_entry(r["mac"], r, this=False, seen_at=r["seen_at"], age_s=r["age_s"]) for r in rows if r["mac"] != mine], how


def view(*, now: datetime | None = None) -> dict:
    """``khipu macs --json``: this Mac live, then every other Mac's row. Never an error."""
    now = now or datetime.now(timezone.utc)
    mine = local_mac_id()
    here = _entry(mine, local_row(), this=True, seen_at=_iso(now), age_s=0)
    rest, how = others(mine=mine)
    out = {"checked_at": _iso(now), "macs": [here, *rest], "shared": how == "ok" and mine is not None,
           "stale_after_s": STALE_AFTER_S}
    if mine is None:
        out["note"] = "This Mac has no routing identity yet, so it shares no status."
    elif how != "ok":
        out["note"] = ("The shared status table is not on the hub yet (run khipu migrate), so only this Mac is shown."
                       if how == "no_table" else "The hub did not answer, so only this Mac is shown.")
    elif not rest:
        out["note"] = "No other Mac has reported yet, so only this Mac is shown."
    return out


def cmd_macs(args) -> int:
    print(json.dumps(view(), indent=2, ensure_ascii=False))
    return 0
