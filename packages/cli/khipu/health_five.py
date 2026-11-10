"""``khipu health --json``: the five checks the app shows, as plain rows.

A view over ``khipu doctor``: it reads the same report and deletes none of its checks. Every check key that
feeds doctor's overall ``ok`` (``cli.OK_GATES``) lands in exactly one of five areas (``area_of``); one nobody
wrote words for lands in the fifth, named. ``you_do`` is plain words and never names a command; commands and
check keys go into ``agent_note``, which passes through ``redact``.

The Routing row does not read the delivery ledger or the old subagent summary (pull request #534 deletes
them): it reads whether this Mac's routing record exists and was folded recently.
"""
from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

from khipu.doctor_summary import AREAS, OTHER, WORDS, failing_keyed

KEYS = ("saving", "finding", "routing", "database", "jobs")
LINKS = ("where-memory-is-kept", "search", "accounts", "backups", "running-now")
# Not read: routing delivery is evidence that #534 removes. It still has an area so no check key is homeless.
UNREAD = ("routing_delivery_ok",)
# A failure of these is an error (red); every other failing check is a warning (amber).
SEVERE = {"hub_ok", "outbox_ok", "ledger_outbox_ok", "disk_space_ok"}
ROUTING_STALE = timedelta(hours=24)
SILENT_QUIET = timedelta(days=7)
RANK = {"ok": 0, "unknown": 0, "warn": 1, "err": 2}

OK_LINES = (
    "Captures are being saved.",
    "Search is up to date.",
    "Your routing record is fresh on this Mac.",
    "The database answers, the backup is recent and there is room on the disk.",
    "Background jobs are running.",
)
DOING = (
    "Khipu keeps what it could not save safe on this Mac and keeps trying.",
    "Khipu is catching the search index up in the background.",
    "Khipu rebuilds the routing record on its next read.",
    "Khipu keeps trying to reach the database and keeps a copy on this Mac meanwhile.",
    "Khipu runs its jobs again on their next turn.",
)
NOTHING = "Nothing."
LATER = "Nothing for now. If it is still here tomorrow, give the note below to an agent."
# What the owner can do, in plain words, for the few problems that are theirs; everything else is "Nothing."
YOU_DO = {
    "disk_space_ok": "Free up some space on this Mac.",
    "hub_ok": "Check this Mac's internet connection. If it is fine, there is nothing more to do.",
    "outbox_ok": LATER, "launchers_ok": LATER, "bundle_seal_ok": LATER, "backup_ok": LATER,
}
UNNAMED_YOU_DO = "Nothing yet. If it is still here tomorrow, give the note below to an agent."


def area_of(key: str) -> int:
    """The one area a check key lands in; a key with no wording lands in the last."""
    return WORDS.get(key, (OTHER,))[0]


def every_key() -> tuple[str, ...]:
    from khipu.cli import OK_GATES

    return (*OK_GATES, *UNREAD)


def _cap(text: str) -> str:
    text = text.strip().rstrip(".")
    return (text[:1].upper() + text[1:] + ".") if text else ""


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mac_label() -> str:
    """The name the owner gave this Mac, else its host name."""
    try:
        from khipu import routing

        names = routing.settings().get("routing_mac_names") or {}
        given = names.get(routing._local_mac())
        if isinstance(given, str) and given.strip():
            return given.strip()
    except Exception:  # noqa: BLE001 - a label is never worth failing the report
        pass
    return socket.gethostname().split(".")[0] or "This Mac"


def _routing_problem(report: dict, now: datetime) -> tuple[str, str, str] | None:
    """(state, line, agent detail) when this Mac's routing record is missing, unreadable or old."""
    block = report.get("routing")
    if not isinstance(block, dict) or block.get("error"):
        return "err", "Khipu can't read this Mac's routing record.", \
            "routing record unreadable: " + str((block or {}).get("error") or "no routing block in the report")
    if not block.get("record"):
        return "warn", "This Mac has no routing record yet.", "routing record absent on this Mac"
    folded = _parse(block.get("folded_at"))
    if folded is None or now - folded > ROUTING_STALE:
        return "warn", "This Mac's routing record is out of date.", f"routing record folded_at={block.get('folded_at')}"
    return None


def _runners(report: dict) -> list[dict] | None:
    runners = ((report.get("routing") or {}).get("sources") or {}).get("runners")
    return runners if isinstance(runners, list) else None


def _silent_runners(report: dict, mac: str) -> list[dict]:
    return [{"name": r.get("name"), "mac": mac, "since": r.get("heartbeat_at")}
            for r in _runners(report) or [] if r.get("silent") or r.get("unreadable")]


def _jobs_running(report: dict) -> int | None:
    runners = _runners(report)
    return None if runners is None else sum(1 for r in runners if not (r.get("silent") or r.get("unreadable")))


def _free_gb(report: dict) -> float | None:
    from khipu.storage import GB

    volumes = (report.get("disk_space") or {}).get("volumes") or []
    free = [v["free_bytes"] for v in volumes if isinstance(v.get("free_bytes"), (int, float))]
    return round(min(free) / GB, 1) if free else None


def _last_backup_at(report: dict, now: datetime) -> str | None:
    backup = report.get("backup") or {}
    stamps = [_parse((backup.get(k) or {}).get("created_at"))
              for k in ("walg_basebackup", "pg_dump") if (backup.get(k) or {}).get("status") == "ok"]
    stamps = [s for s in stamps if s]
    if stamps:
        return _iso(max(stamps))
    age = backup.get("freshest_backup_age_seconds")
    return _iso(now - timedelta(seconds=age)) if isinstance(age, (int, float)) else None


def build(report: dict, *, now: datetime | None = None, mac: str | None = None, why: str = "") -> dict:
    """The health document for one doctor report (an empty report means the checks could not run)."""
    now = now or datetime.now(timezone.utc)
    mac = mac or mac_label()
    ran = bool(report)
    rows = [{"key": KEYS[i], "name": AREAS[i], "state": "ok" if ran else "unknown",
             "line": OK_LINES[i] if ran else "Khipu could not run its checks.",
             "khipu_doing": "" if ran else "Khipu tries again on the next check.",
             "you_do": NOTHING, "link": LINKS[i]} for i in range(5)]
    notes: list[str] = []
    problems: list[list[tuple[str, str, str, str]]] = [[] for _ in range(5)]  # (state, line, doing, you_do)
    silent: list[dict] = []
    if ran:
        for key, area, what, fix in failing_keyed(report, skip=UNREAD):
            problems[area].append(("err" if key in SEVERE else "warn", _cap(what), DOING[area],
                                   YOU_DO.get(key, NOTHING) if key in WORDS else UNNAMED_YOU_DO))
            notes.append(f"[{AREAS[area]}] {key}: {what}. Fix: {fix}")
        found = _routing_problem(report, now)
        if found:
            problems[2].append((found[0], found[1], DOING[2], NOTHING))
            notes.append(f"[{AREAS[2]}] {found[2]}")
        silent = _silent_runners(report, mac)
        for item in silent:
            since = _parse(item["since"])
            if since is None or now - since <= SILENT_QUIET:
                problems[4].append(("warn", f"A job runner, {item['name']}, has stopped reporting.", DOING[4], NOTHING))
                notes.append(f"[{AREAS[4]}] runner {item['name']} silent since {item['since']}")
            else:
                notes.append(f"[{AREAS[4]}] runner {item['name']} silent for more than a week (quiet, not a problem)")
        for i, items in enumerate(problems):
            if not items:
                continue
            rows[i]["state"] = max((s for s, *_ in items), key=RANK.get)
            rows[i]["line"] = " ".join(dict.fromkeys(line for _s, line, _d, _y in items))
            rows[i]["khipu_doing"] = " ".join(dict.fromkeys(d for _s, _l, d, _y in items))
            rows[i]["you_do"] = " ".join(dict.fromkeys(y for _s, _l, _d, y in items if y != NOTHING)) or NOTHING
    else:
        notes.append("doctor produced no report, so no check ran" + (f": {why}" if why else ""))
    running = _jobs_running(report) if ran else None
    if ran and not problems[4]:
        rows[4]["line"] = ("No job is running." if not running else
                           f"{running} job{'s are' if running != 1 else ' is'} running.")
    lead = next((r for s in ("err", "warn", "unknown") for r in rows if r["state"] == s), None)
    headline = ("Everything is working." if lead is None
                else f"{lead['name']}: {lead['line']} {lead['khipu_doing']}".strip())
    header = f"Khipu health on {mac} at {_iso(now)}. " + (
        "Nothing is failing." if lead is None else f"{sum(r['state'] != 'ok' for r in rows)} of 5 areas need a look.")
    from khipu.redact import redact_secrets

    note, _count = redact_secrets("\n".join([header, *notes]), force=True)
    return {"checked_at": _iso(now), "mac": mac, "headline": headline, "areas": rows,
            "jobs_running": running, "silent_runners": silent,
            "free_gb": _free_gb(report) if ran else None,
            "last_backup_at": _last_backup_at(report, now) if ran else None, "agent_note": note}


def run_doctor() -> tuple[dict, str]:
    """(the full doctor report, why there is none): read the way an agent reads it, its JSON on standard output."""
    import contextlib
    import io
    import json

    from khipu import cli

    args = cli.build_parser().parse_args(["doctor"])
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            args.func(args)
        parsed = json.loads(out.getvalue())
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - no report is a finding, shown as "could not run"
        return {}, f"{type(exc).__name__}: {str(exc)[:300]}"
    return (parsed, "") if isinstance(parsed, dict) else ({}, "doctor printed something other than a report")


def cmd_health(args) -> int:
    import json

    report, why = run_doctor()
    print(json.dumps(build(report, why=why), indent=2, ensure_ascii=False))
    return 0
