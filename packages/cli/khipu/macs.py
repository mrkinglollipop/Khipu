"""One status row per Mac, shared through the hub (``khipu macs --json``).

Each Mac writes only its own row of ``mac_status`` (migration 0052): when ``khipu health`` runs and as the last
stage of the five-minute snapshot heal timer. Neither builds the work view: the running part of the row is the one
``khipu running`` last kept on this Mac (``remember_runners``), published while it is under ``STALE_AFTER_S`` old.
The sweep's last stage refreshes that part itself (``refresh_running``) inside its own time cap, so a Mac's running
jobs reach the other Mac without anyone opening ``khipu running``. A reader gets this Mac live plus every other Mac's row; a row the hub last
heard from more than ``STALE_AFTER_S`` ago is marked ``stale``, never shown as current.

The row is a closed list of fields (``project``), built the way the routing copy builds its payload: only the
fields named here are copied, each in its named shape, and everything else is dropped. It never holds a path, a
login home, transcript text, a prompt or a key value. The same projection runs on write and again on read, so
a row written by a newer or older build can never put an unlisted field in front of the app.

Nothing here raises to a caller. The other Mac may run an older build: no table, no row or an unknown field is
"this Mac only", labelled. A write that fails (hub down) is reported in the result and never fails ``health``
or the heal timer.

``mac_label`` / ``name_of`` are the one place a Mac gets a name: search and activity rows (``memory_rows``), the
health document and ``khipu macs`` all call them. (``routing_replication.mac_label`` belongs to the routing
pages and is left alone.)
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
RUNNING_FIELDS = ("count", "silent", "runners")  # "runners" is the closed list in running_now.RUNNER_FIELDS
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
    from khipu import running_now

    return {"count": _count(raw.get("count")), "silent": silent, "runners": running_now.project_runners(raw.get("runners"))}


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


def _given_name(mac: Any) -> str | None:
    """The owner's name for Mac id ``mac`` (routing_mac_names), else None."""
    try:
        from khipu import routing

        given = (routing.settings().get("routing_mac_names") or {}).get(mac) if isinstance(mac, str) else None
        return given.strip() if isinstance(given, str) and given.strip() else None
    except Exception:  # noqa: BLE001 - a label is never worth failing a report
        return None


def mac_label() -> str:
    """This Mac's name: the one the owner gave it, else its host name (else 'This Mac'). The one place any
    screen or search row gets a name for this Mac."""
    return _given_name(local_mac_id()) or socket.gethostname().split(".")[0] or "This Mac"


def name_of(mac: str | None, row_name: str | None = None) -> str:
    """What the owner calls the Mac with id ``mac``: this Mac's name when it is this Mac; else their own name for
    that Mac, else the name its status row carries, else 'Your other Mac'. ``mac`` None is a Mac whose id is not
    known (a search row carries a hardware origin, not a Mac id): the fallback."""
    if isinstance(mac, str) and mac == local_mac_id():
        return mac_label()
    return _given_name(mac) or row_name or OTHER_MAC


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


def _write_cache(data: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _remembered() -> dict:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def remember_health(doc: dict) -> dict:
    """Keep the closed part of the latest health document on this Mac for the timer; returns it. The runners
    ``khipu running`` kept (and when) stay."""
    kept = project(_from_health(doc))
    old = _remembered()
    data = {k: kept[k] for k in ("saving", "health", "running")}
    runners = (old.get("running") or {}).get("runners") if isinstance(old.get("running"), dict) else None
    if isinstance(runners, list) and old.get("runners_at"):
        data["running"] = dict(data["running"] or {"count": None, "silent": []}, runners=runners)
        data["runners_at"] = old["runners_at"]
    _write_cache(data)
    return kept


def remember_runners(runners: list | None, *, now: datetime | None = None) -> None:
    """Keep this Mac's running jobs as ``khipu running`` just read them, for the row the timer and health publish.
    ``None`` (the work view could not be read) keeps what was there."""
    if not isinstance(runners, list):
        return
    old = _remembered()
    running = old.get("running") if isinstance(old.get("running"), dict) else {"count": None, "silent": []}
    _write_cache(dict(old, running=dict(running, runners=runners), runners_at=_iso(now or datetime.now(timezone.utc))))


def refresh_running(now: datetime | None = None) -> bool | None:
    """Build this Mac's running jobs the way ``khipu running`` does and keep them (the sweep's last stage, before it
    publishes). ``None``: nothing was built (no Mac identity, or the hub is already known to be down, so the row
    cannot be written). ``False``: the work view could not be read, so what was kept stays. ``True``: kept fresh."""
    if not local_mac_id():
        return None
    from khipu import hub_reach

    if hub_reach.known_unreachable():
        return None
    from khipu import running_now

    runners = running_now.this_mac_runners(now)
    if runners is None:
        return False
    remember_runners(runners, now=now)
    return True


def _kept_runners(now: datetime | None = None) -> list | None:
    """The runners ``khipu running`` kept, while they are fresh enough to publish; else ``None``."""
    old = _remembered()
    at = _parse(old.get("runners_at"))
    runners = (old.get("running") or {}).get("runners") if isinstance(old.get("running"), dict) else None
    if at is None or not isinstance(runners, list):
        return None
    age = ((now or datetime.now(timezone.utc)) - at).total_seconds()
    return runners if 0 <= age <= STALE_AFTER_S else None


def local_row(doc: dict | None = None) -> dict:
    """This Mac's closed row now. With a health document, that is what it reports; without one (the timer), the
    last health this Mac kept; with neither, saving, health and running are ``None`` (not known). The running jobs
    are the ones ``khipu running`` last kept, when that was under 15 minutes ago. Reads no hub and builds no work
    view."""
    base = _from_health(doc) if doc else _remembered()
    runners = _kept_runners()
    if base.get("running") is not None or runners is not None:
        running = {k: v for k, v in (base.get("running") or {"count": None, "silent": []}).items() if k != "runners"}
        base = dict(base, running=dict(running, runners=runners) if runners is not None else running)
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
