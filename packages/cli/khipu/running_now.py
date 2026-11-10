"""Which jobs are running, on which Mac, and which have gone quiet (``khipu running --json``, app slice B4).

This Mac's part is the work view's jobs joined, by runner name, with the routing runners' heartbeat. Each Mac
publishes that part inside its own ``mac_status`` row (``running.runners``); the other Mac's part is read from
there. Pull requests, release trains and the ledger are not here.

What travels in the row is a closed list (``RUNNER_FIELDS``, ``JOB_FIELDS``), projected on write and again on
read, like the rest of the row (``macs.project``). A job's one-line summary and its "now" line are the owner's
own task text, so they travel, clipped to ``SUMMARY_CHARS`` / ``NOW_CHARS`` and cut of any path by the work
view's own text cleaner. A path, a login home, a prompt, transcript text or a key value never does.

Nothing here raises to a caller and the command always exits 0: the other Mac may run an older build, so a row
without the running part, a missing row or a missing table is "this Mac only", labelled.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

RUNNER_FIELDS = ("name", "heartbeat_at", "silent", "silent_since", "last_finished_at", "jobs")
JOB_FIELDS = ("summary", "now_line", "now_at", "state", "quiet", "quiet_for_s", "account_key", "account_label",
              "model", "started_at", "updated_at", "ended_at")
STATES = ("running", "waiting", "queued", "unknown", "done", "failed", "stopped")
LIVE = ("running", "waiting", "queued")
SUMMARY_CHARS = 120
NOW_CHARS = 90
UNNAMED = "Unnamed runner"
MAX_RUNNERS = 20
MAX_JOBS = 40

_RUNNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,39}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ +-]{0,39}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


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
    moment = _parse(value) if value is not None else None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _word(value, pattern: re.Pattern) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if pattern.fullmatch(value) else None


def _words(value, cap: int) -> str | None:
    """The owner's own one line, cleaned by the work view's text rules (paths cut, secrets refused), then clipped."""
    try:
        from khipu import work

        return work.clean_text(value, cap)
    except Exception:  # noqa: BLE001 - no cleaner, no text
        return None


def _seconds(value) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**8 else None


# ---- the closed projection (write and read) ------------------------------------------------------------------------


def _job(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    summary, state = _words(raw.get("summary"), SUMMARY_CHARS), raw.get("state")
    if summary is None or state not in STATES:
        return None
    out = {"summary": summary, "now_line": _words(raw.get("now_line"), NOW_CHARS), "now_at": _iso(raw.get("now_at")),
           "state": state, "quiet": raw["quiet"] if isinstance(raw.get("quiet"), bool) else False,
           "quiet_for_s": _seconds(raw.get("quiet_for_s")), "account_key": _word(raw.get("account_key"), _KEY),
           "account_label": _word(raw.get("account_label"), _LABEL), "model": _word(raw.get("model"), _LABEL),
           "started_at": _iso(raw.get("started_at")), "updated_at": _iso(raw.get("updated_at")),
           "ended_at": _iso(raw.get("ended_at"))}
    if out["now_line"] is None:
        out["now_at"] = None
    return {k: out[k] for k in JOB_FIELDS}


def _runner(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = _word(raw.get("name"), _RUNNER)
    if not name:
        return None
    jobs = [j for j in (_job(x) for x in (raw.get("jobs") if isinstance(raw.get("jobs"), list) else [])[:MAX_JOBS]) if j]
    out = {"name": name, "heartbeat_at": _iso(raw.get("heartbeat_at")),
           "silent": raw["silent"] if isinstance(raw.get("silent"), bool) else False,
           "silent_since": _iso(raw.get("silent_since")), "last_finished_at": _iso(raw.get("last_finished_at")),
           "jobs": jobs}
    return {k: out[k] for k in RUNNER_FIELDS}


def project_runners(raw) -> list[dict] | None:
    """The closed runner list, or ``None`` when ``raw`` is not a list (a row from a build that has no running part)."""
    if not isinstance(raw, list):
        return None
    return [r for r in (_runner(x) for x in raw[:MAX_RUNNERS]) if r]


# ---- this Mac ------------------------------------------------------------------------------------------------------


def _heartbeats(now: datetime) -> list[dict] | None:
    """The routing runners' status (name, heartbeat_at, silent, unreadable), or ``None`` when it can't be read."""
    try:
        from khipu import routing

        with routing.lock_budget(2.0):
            view = routing.read_view(now)
        runners = (view.get("sources") or {}).get("runners")
        return [r for r in runners if isinstance(r, dict) and isinstance(r.get("name"), str)] if isinstance(runners, list) else None
    except Exception:  # noqa: BLE001
        return None


def _one_job(raw: dict, state: str, now: datetime) -> dict:
    lines = [x for x in raw.get("now") or [] if isinstance(x, dict) and x.get("text")]
    last = lines[-1] if lines else {}
    updated = _parse(raw.get("updated_at"))
    quiet = bool(raw.get("quiet")) and state == "running"
    ref = raw.get("account_ref") if isinstance(raw.get("account_ref"), dict) else {}
    return {"summary": raw.get("summary"), "now_line": last.get("text"), "now_at": last.get("at"), "state": state,
            "quiet": quiet, "quiet_for_s": max(0, int((now - updated).total_seconds())) if quiet and updated else None,
            "account_key": ref.get("id") if ref.get("matched") else None, "account_label": ref.get("label"),
            "model": raw.get("model"), "started_at": raw.get("started_at"), "updated_at": raw.get("updated_at"),
            "ended_at": raw.get("ended_at")}


def build_runners(view: dict, statuses: list[dict] | None, now: datetime) -> list[dict]:
    """This Mac's runners from a work view and the routing runners' status. A runner that stopped reporting (or
    whose source the view no longer counts as live) has its live jobs shown as ``unknown``; their last words and
    the time of them stay, so the app can say "last said"."""
    beats = {r["name"]: r for r in statuses or []}
    groups: dict[str, list[dict]] = {}
    still_live = {(j.get("project"), j.get("id")) for j in view.get("live") or [] if isinstance(j, dict)}
    for board in view.get("projects") or []:
        for job in board.get("jobs") or []:
            groups.setdefault(job.get("runner") or UNNAMED, []).append(job)
    for name in beats:
        groups.setdefault(name, [])
    runners = []
    for name in sorted(groups):
        beat = beats.get(name)
        silent = bool(beat and (beat.get("silent") or beat.get("unreadable")))
        jobs, finished = [], []
        for raw in groups[name]:
            state = raw.get("state")
            lost = state in LIVE and (silent or (raw.get("project"), raw.get("id")) not in still_live)
            if state in STATES or lost:
                jobs.append(_one_job(raw, "unknown" if lost else state, now))
            if state not in LIVE:
                finished.append(raw.get("ended_at") or raw.get("updated_at"))
        order = {s: i for i, s in enumerate(STATES)}
        jobs.sort(key=lambda j: (order.get(j["state"], 9), -(_parse(j["updated_at"]) or now).timestamp()))
        runners.append({"name": name, "heartbeat_at": beat.get("heartbeat_at") if beat else None, "silent": silent,
                        "silent_since": beat.get("heartbeat_at") if silent and beat else None,
                        "last_finished_at": max((t for t in finished if _parse(t)), key=_parse, default=None),
                        "jobs": jobs})
    return project_runners(runners) or []


def this_mac_runners(now: datetime | None = None, *, view: dict | None = None,
                     statuses: list[dict] | None = None) -> list[dict] | None:
    """This Mac's runners (closed shape), or ``None`` when the work view could not be read."""
    now = now or datetime.now(timezone.utc)
    try:
        if view is None:
            from khipu import work

            view = work.build_view(now)
        return build_runners(view, statuses if statuses is not None else _heartbeats(now), now)
    except Exception:  # noqa: BLE001
        return None


# ---- the answer ------------------------------------------------------------------------------------------------------


def _counts(entries: list[dict]) -> dict:
    jobs = [j for e in entries if not e["stale"] for r in e["runners"] for j in r["jobs"]]
    return {"running": sum(j["state"] == "running" for j in jobs),
            "waiting": sum(j["state"] in ("waiting", "queued") for j in jobs)}


def view(*, now: datetime | None = None) -> dict:
    """``khipu running --json``: this Mac live, then the other Mac as it last reported. Never an error."""
    from khipu import macs

    now = now or datetime.now(timezone.utc)
    mine = macs.local_mac_id()
    notes: list[str] = []
    runners = this_mac_runners(now)
    if runners is None:
        runners = []
        notes.append("This Mac's running jobs could not be read right now.")
    entries = [{"mac": mine, "name": macs.mac_label(), "this_mac": True, "as_of": _iso(now), "stale": False,
                "runners": runners}]
    rest, how = macs.others(mine=mine)
    for other in rest:
        part = (other.get("running") or {}).get("runners")
        if part is None:
            notes.append(f"{other['name']} runs an older Khipu that does not share its running jobs yet.")
            continue
        entries.append({"mac": other["mac"], "name": other["name"], "this_mac": False, "as_of": other["seen_at"],
                        "stale": other["stale"], "runners": part})
    if mine is None:
        notes.append("This Mac has no routing identity yet, so it shares no running jobs.")
    elif how == "no_table":
        notes.append("The shared status table is not on the hub yet (run khipu migrate), so only this Mac is shown.")
    elif how != "ok":
        notes.append("The hub did not answer, so only this Mac is shown.")
    elif not rest:
        notes.append("No other Mac has reported yet, so only this Mac is shown.")
    out = {"updated_at": _iso(now), "macs": entries, "counts": _counts(entries),
           "shared": how == "ok" and mine is not None and len(entries) > 1}
    if notes:
        out["note"] = " ".join(notes)
    return out


def cmd_running(args) -> int:
    try:
        doc: Any = view()
    except Exception as exc:  # noqa: BLE001 - the command always answers
        doc = {"updated_at": _iso(datetime.now(timezone.utc)), "macs": [], "counts": {"running": 0, "waiting": 0},
               "shared": False, "note": f"Running jobs could not be read ({type(exc).__name__})."}
    print(json.dumps(doc, indent=2, ensure_ascii=False))
    return 0
