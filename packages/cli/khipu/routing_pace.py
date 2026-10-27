"""Pace plumbing: samples, canonical bucket merge, caps and push forecasts.

The calculation here is the plumbing layer for slice 4Pa. It turns stored
``usage_sample`` observations into per-window pace objects, keeps owner caps
apart from conversation proposals, and decides whether a forecast differs
enough from the last acknowledged one to push. It never chooses work.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

WINDOWS = {
    "weekly": {"trailing_s": 86400, "bucket_s": 900, "sample_s": 1800, "gap_s": 7200, "trailing_h": 24,
               "window_s": 604800},
    "five_hour": {"trailing_s": 3600, "bucket_s": 300, "sample_s": 600, "gap_s": 900, "trailing_h": 1,
                  "window_s": 18000},
}
# Cap kinds scoped to a usage window; the rest are account-scoped facts.
WINDOW_CAP_KINDS = ("share", "per_day", "pace_target")
CAP_KINDS = WINDOW_CAP_KINDS + ("spend", "concurrency")
CAP_PURPOSES = ("job", "check")
_REUSABLE_CYCLES = {"weekly", "five_hour", "five-hour", "five hour", "daily", "day"}


def parse(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if value else None


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


_CAP_FIELDS = {"share": {"pct"}, "per_day": {"pct"}, "pace_target": {"pct", "slack"},
               "spend": {"usd", "days"}, "concurrency": {"n"}}


def cap_scope_error(kind, purpose="job", *, tier=None, sensitive_only=None):
    """The scope shared by owner writes, proposals and stored facts."""
    if purpose not in CAP_PURPOSES:
        return "cap purpose must be job or check"
    if purpose == "check" and kind != "pace_target":
        return "check purpose is only supported for pace_target caps"
    if (tier is not None or sensitive_only is not None) and (kind != "pace_target" or purpose != "check"):
        return "cap qualifiers are only supported for check pace_target caps"
    if tier is not None and tier not in ("opus", "sonnet", "haiku"):
        return "cap tier must be opus, sonnet or haiku"
    if sensitive_only is not None and not isinstance(sensitive_only, bool):
        return "sensitive_only must be true or false"
    return None


def checked_cap(value):
    """One owner cap fact, checked by kind. Runner numbers never enter
    here: a cap holds only what the owner confirmed. A field the kind
    does not use refuses the cap, never drops silently."""
    if not isinstance(value, dict) or set(value) - {"kind", "pct", "slack", "usd", "days", "n", "revision", "purpose", "tier", "sensitive_only"}:
        return None
    if any(k in value and value[k] is None for k in ("purpose", "tier", "sensitive_only")):
        return None
    if cap_scope_error(value.get("kind"), value.get("purpose", "job"),
                       tier=value.get("tier"), sensitive_only=value.get("sensitive_only")):
        return None
    scope = {k: value[k] for k in ("purpose", "tier", "sensitive_only") if k in value}
    kind = value.get("kind")
    revision = value.get("revision")
    if kind not in CAP_KINDS or not isinstance(revision, str) or not revision.strip():
        return None
    if {k for k, v in value.items() if v is not None} - {"kind", "revision", "purpose", "tier", "sensitive_only"} - _CAP_FIELDS[kind]:
        return None
    revision = revision.strip()
    if kind in ("share", "per_day"):
        pct = _num(value.get("pct"))
        if pct is None or not 0 < pct <= 100:
            return None
        return {"kind": kind, "pct": pct, "revision": revision, **scope}
    if kind == "pace_target":
        pct, slack = _num(value.get("pct")), _num(value.get("slack"))
        if pct is None or not 0 < pct <= 100 or slack is None or not 0 <= slack <= 100:
            return None
        return {"kind": kind, "pct": pct, "slack": slack, "revision": revision, **scope}
    if kind == "spend":
        usd, days = _num(value.get("usd")), _num(value.get("days"))
        if usd is None or not 0 < usd <= 1e9 or days is None or not 0 < days <= 366:
            return None
        return {"kind": kind, "usd": usd, "days": days, "revision": revision, **scope}
    n = value.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 100000:
        return None
    return {"kind": kind, "n": n, "revision": revision, **scope}


def spend_verdict(total: dict, usd) -> str:
    """A spend cap's verdict from a 17t total: unknown stays unknown,
    never a figure invented from partial coverage."""
    if not isinstance(total, dict) or total.get("status") != "known" or total.get("usd") is None:
        return "unknown"
    if total["usd"] > usd:
        return "over"
    if total["usd"] >= usd:
        return "reached"
    return "near" if total["usd"] >= 0.85 * usd else "under"


def concurrency_verdict(active: dict, n) -> str:
    """A concurrency cap's verdict from a 17t active snapshot: an expired
    record is unknown, never zero jobs."""
    if not isinstance(active, dict) or active.get("status") != "known" or active.get("jobs") is None:
        return "unknown"
    return "full" if active["jobs"] >= n else "under"


def _clock(dt, now):
    d, n = dt.astimezone(timezone.utc), now.astimezone(timezone.utc)
    if d.date() == n.date():
        return d.strftime("%H:%MZ")
    if abs((d.date() - n.date()).days) < 7:
        return d.strftime("%a %H:%MZ")
    return d.strftime("%b %d %H:%MZ")


def _span(seconds):
    minutes = int(abs(seconds) // 60)
    if minutes < 1:
        return "less than a minute"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {mins} min" if mins else f"{hours} h"
    return f"{mins} min"


def _rate_words(rate):
    if rate >= 10:
        return f"{rate:.0f}"
    if rate >= 0.1:
        return f"{round(rate, 1):g}"
    return f"{round(rate, 2):g}" if rate >= 0.01 else "less than 0.01"


def _hours(value):
    return f"{value:g}" if abs(value - round(value)) < 1e-9 else f"{value:.1f}".rstrip("0").rstrip(".")


def _filter(samples, params):
    out, last_by, last_identical = [], {}, {}
    for s in sorted(samples, key=lambda x: (x["t"], x.get("mac") or "", x.get("source") or "", x["used"])):
        if s.get("t_err", 0) > params["sample_s"]:
            continue
        key = (s.get("mac"), s.get("source"))
        if key in last_by and s["t"] <= last_by[key]:
            continue
        # Only an identical reading from the same Mac and source, carrying
        # the same reset, is a duplicate; a disagreeing witness is evidence.
        ident = (s.get("mac"), s.get("source"), s.get("reset"), s["used"])
        kept_at = last_identical.get(ident)
        if kept_at is not None and 0 <= (s["t"] - kept_at).total_seconds() <= s.get("t_err", 0):
            continue
        out.append(dict(s))
        last_by[key] = s["t"]
        last_identical[ident] = s["t"]
    return out


def _cycles(samples, window):
    for s in samples:
        raw = s.get("cycle")
        s["_cycle_id"] = raw if isinstance(raw, str) and raw and raw not in _REUSABLE_CYCLES and raw != window else None
    id_groups = {}
    for s in samples:
        if s["_cycle_id"]:
            id_groups.setdefault(s["_cycle_id"], []).append(s)
    idless = [s for s in samples if not s["_cycle_id"]]
    # Single-linkage components of id-less reset estimates.
    comps = []
    for s in sorted(idless, key=lambda x: x["reset"]):
        if comps and (s["reset"] - comps[-1][-1]["reset"]).total_seconds() <= 3600:
            comps[-1].append(s)
        else:
            comps.append([s])
    for cid, group in id_groups.items():
        for s in group:
            s["_cycle_key"] = ("id", cid)
    for i, comp in enumerate(comps):
        target = None
        for cid, group in id_groups.items():
            if min(abs((s["reset"] - g["reset"]).total_seconds()) for s in comp for g in group) <= 3600:
                target = ("id", cid)
                break
        for s in comp:
            s["_cycle_key"] = target or ("reset", i, iso(comp[0]["reset"]))


# A view holding pace figures is folded again at least this often. Its
# allowances, needed rates and trailing averages drift with the clock between
# readings; the step changes (a reading going stale, a reset passing, a
# reading leaving the trailing window) are scheduled at their own second
# (``next_change``). The prompt words the clock-relative parts at prompt time.
REFOLD_S = 30


def next_change(samples, now, window, *, day=False):
    """The earliest moment after ``now`` at which the clock alone changes
    what ``compute_pace`` reads from ``samples`` by a step: the newest
    reading turning stale, a reset passing, a reading leaving the trailing
    window (or the rolling day, ``day``), or a reading witnessed ahead of the
    clock coming due. None when there is none. Continuous drift (allowances,
    needed rates) is bounded by ``REFOLD_S`` instead."""
    params = WINDOWS.get(window)
    if params is None:
        return None
    best = newest = None
    trailing = timedelta(seconds=params["trailing_s"])
    for s in samples:
        t, reset = s.get("t"), s.get("reset")
        if not isinstance(t, datetime):
            continue
        newest = t if newest is None or t > newest else newest
        for moment in (reset, t, t + trailing, t + timedelta(days=1) if day else None):
            if isinstance(moment, datetime) and moment > now and (best is None or moment < best):
                best = moment
    if newest is not None:
        stale = min(newest, now) + timedelta(seconds=2 * params["sample_s"])
        if stale > now and (best is None or stale < best):
            best = stale
    return best


def at(pace, now):
    """``pace`` (folded at some earlier moment of the same readings) with its
    clock-driven figures as of ``now``: each pace target's allowance and
    verdict, and the needed rate and unused-at-reset share of the plan and
    the share cap. Everything else is as folded; a step change (a reading
    going stale, a reset passing) refolds the view itself (``next_change``),
    and the trailing average's drift is bounded by ``REFOLD_S``. A prompt's
    pace words are built from this, so a cached view says what a fold at
    the prompt would."""
    params = WINDOWS.get(pace.get("window"))
    reset = parse(pace.get("cycle_resets_at"))
    used = _num(pace.get("used_pct"))
    if params is None or reset is None or used is None:
        return pace
    secs = (reset - now).total_seconds()
    if secs <= 0:
        return pace
    out = dict(pace)
    rate = pace.get("rate_pct_per_h")
    for field, ceiling in (("plan", 100.0), ("share_cap", None)):
        obj = pace.get(field)
        if not isinstance(obj, dict) or _num(obj.get("needed_pct_per_h")) is None:
            continue
        top = ceiling if ceiling is not None else _num(obj.get("pct"))
        if top is None:
            continue
        headroom = top - used
        obj = dict(obj, needed_pct_per_h=headroom / (secs / 3600))
        if obj.get("verdict") == "underpacing" and obj.get("unused_at_reset_pct") is not None:
            obj["unused_at_reset_pct"] = headroom if not rate or rate < 0.01 else headroom - rate * secs / 3600
        out[field] = obj
    for field in ("pace_target", "check_pace_target"):
        target = pace.get(field)
        if not isinstance(target, dict) or _num(target.get("allowance_pct")) is None \
                or target.get("verdict") not in ("ahead", "within", "recovering"):
            continue
        elapsed = min(max(params["window_s"] - secs, 0.0), float(params["window_s"]))
        allowance = target["pct"] * elapsed / params["window_s"] + target["slack"]
        # The allowance only grows within a cycle: ahead can become
        # recovering (it was ahead) or within; within stays within.
        verdict = ("ahead" if used > allowance else
                   "within" if used <= allowance - 1 or target["verdict"] == "within" else "recovering")
        out[field] = dict(target, allowance_pct=allowance, verdict=verdict)
    return out


def compute_pace(samples, caps, now, window):
    """One window's pace object from sanitized sample dicts.

    Samples carry ``t``/``reset`` datetimes, ``used``, ``mac``, ``source``,
    ``cycle`` and ``t_err``. Caps are checked cap dicts for this window.
    """
    params = WINDOWS[window]
    trailing = params["trailing_s"]
    pace = {"window": window, "cycle_resets_at": None, "trailing_h": params["trailing_h"], "buckets": 0,
            "valid_pct": 0.0, "observed_pct": 0.0, "gappy": False, "rate_pct_per_h": None, "why": None,
            "plan": {"verdict": "unknown", "needed_pct_per_h": None, "runs_out_at": None,
                     "before_reset_s": None, "unused_at_reset_pct": None},
            "share_cap": None, "day_cap": None, "pace_target": None, "headline": "plan"}
    samples = [s for s in samples if s.get("reset") is not None and _num(s.get("used")) is not None]
    targets = {("check_pace_target" if c.get("purpose", "job") == "check" else "pace_target"): c
               for c in caps if c.get("kind") == "pace_target"}
    for field, target_cap in targets.items():
        pace[field] = {"revision": target_cap["revision"], "pct": target_cap["pct"],
                               "slack": target_cap["slack"], "allowance_pct": None, "used_pct": None,
                               "verdict": "unknown", "why": "no reading"}
    if not samples:
        pace["why"] = "this account can't be probed"
        return pace
    samples = _filter(samples, params)
    # A witness ahead of now is a skewed clock: within its own uncertainty
    # it is a reading for now; beyond it, it cannot be placed at all.
    samples = [dict(s, t=min(s["t"], now)) for s in samples
               if s["t"] <= now + timedelta(seconds=s.get("t_err") or 0)]
    if not samples:
        pace["why"] = "this account can't be probed"
        return pace
    _cycles(samples, window)
    # Only the windows the calculations read can matter: the trailing window
    # (a rolling day for a per-day cap), plus the newest reading before it
    # per witness and cycle, which anchors the interval straddling its start.
    horizon = 86400 if any(c.get("kind") == "per_day" for c in caps) else trailing
    horizon_start = now - timedelta(seconds=horizon)
    before: dict = {}
    for s in samples:
        if s["t"] < horizon_start:
            ident = (s.get("mac"), s.get("source"), s["_cycle_key"])
            if ident not in before or s["t"] > before[ident]["t"]:
                before[ident] = s
    samples = [s for s in samples if s["t"] >= horizon_start] + list(before.values())
    # Raw possible resets, judged before any bucket maximum can hide them.
    ordered = sorted(samples, key=lambda x: (x["t"], x.get("mac") or "", x.get("source") or "", x["used"]))
    for i, s in enumerate(ordered):
        s["_i"] = i
        s["_correction"] = False
    pending = False
    segments = {}
    for key in {s["_cycle_key"] for s in ordered}:
        group = [s for s in ordered if s["_cycle_key"] == key]
        # The highest later reading in this same cycle, for refute checks.
        suffix_high: list = [None] * (len(group) + 1)
        for pos in range(len(group) - 1, -1, -1):
            suffix_high[pos] = group[pos]["used"] if suffix_high[pos + 1] is None \
                else max(suffix_high[pos + 1], group[pos]["used"])
        high, seg = None, 0
        for pos, s in enumerate(group):
            s["_segment"] = (key, seg)
            if high is None or s["used"] >= high:
                high = s["used"] if high is None else max(high, s["used"])
                continue
            if suffix_high[pos + 1] is not None and suffix_high[pos + 1] >= high:
                # A later reading in this cycle returns to the high: the
                # whole dip is a correction, rebound included.
                s["_correction"] = True
                continue
            if s["used"] < high - 5:
                nxt = ordered[s["_i"] + 1:s["_i"] + 3]
                if (nxt and nxt[0]["_cycle_key"] != key) or (len(nxt) == 2 and all(x["used"] < high - 5 for x in nxt)):
                    seg += 1
                    s["_segment"] = (key, seg)
                    high = s["used"]
                    continue
                pending = True
            # A smaller unrecovered drop stays a raw point; negative bucket
            # gains are excluded below.
        segments[key] = seg
    newest = max(samples, key=lambda x: x["t"])
    bucket_no = int(newest["t"].timestamp() // params["bucket_s"])
    disagrees = len({s["_cycle_key"] for s in samples if int(s["t"].timestamp() // params["bucket_s"]) == bucket_no}) > 1
    current_key = min((s for s in samples if s["t"] == newest["t"]), key=lambda x: x["reset"])["_cycle_key"]
    current = [s for s in samples if s["_cycle_key"] == current_key]
    representative = min((s for s in current if s["t"] == max(x["t"] for x in current)), key=lambda x: x["reset"])["reset"]
    pace["cycle_resets_at"] = iso(representative)
    confidence = [s for s in current if s["t"] >= now - timedelta(seconds=trailing)]
    span = ((max(s["reset"] for s in confidence) - min(s["reset"] for s in confidence)).total_seconds()
            if confidence else 0)
    ambiguous = span > 3600
    # Canonical fixed-epoch buckets per settled segment.
    buckets = {}
    for s in samples:
        bkey = (s.get("_segment", (s["_cycle_key"], 0)), int(s["t"].timestamp() // params["bucket_s"]))
        b = buckets.setdefault(bkey, {"segment": bkey[0], "bucket": bkey[1], "value": s["used"], "low": s["used"],
                                      "time": s["t"], "macs": set(), "correction": False})
        b["value"] = max(b["value"], s["used"])
        b["low"] = min(b["low"], s["used"])
        b["time"] = max(b["time"], s["t"])
        b["macs"].add(s.get("mac"))
        b["correction"] = b["correction"] or s.get("_correction", False)
    blist = sorted(buckets.values(), key=lambda b: (b["time"], b["bucket"]))
    start = now - timedelta(seconds=trailing)
    in_window = [b for b in blist if start <= b["time"] <= now]
    pace["buckets"] = len(in_window)

    def clipped(intervals_start, intervals_end):
        gains = durations = gap = disagree = 0.0
        for a, b in zip(blist, blist[1:]):
            dur = (b["time"] - a["time"]).total_seconds()
            if dur <= 0:
                continue
            lo, hi = max(a["time"], intervals_start), min(b["time"], intervals_end)
            inside = (hi - lo).total_seconds()
            if inside <= 0:
                continue
            if a["segment"] != b["segment"]:
                disagree += inside
                continue
            if a["correction"] or b["correction"]:
                continue
            gain = b["value"] - a["value"]
            if gain < 0:
                continue
            gains += gain * inside / dur
            durations += inside
            if dur > params["gap_s"]:
                gap += inside
        return gains, durations, gap, disagree

    gains, valid_dur, gap_time, disagree_time = clipped(start, now)
    rate = gains / (valid_dur / 3600) if valid_dur > 0 else None
    pace["rate_pct_per_h"] = rate
    pace["valid_pct"] = valid_dur / trailing * 100
    pace["observed_pct"] = max(0.0, (valid_dur - gap_time) / trailing * 100)
    pace["gappy"] = gap_time / trailing > 0.25
    used = blist[-1]["value"] if blist else None
    secs_to_reset = (representative - now).total_seconds() if representative else None
    needed = None
    if used is not None and secs_to_reset and secs_to_reset > 0:
        needed = (100 - used) / (secs_to_reset / 3600)
    pace["plan"]["needed_pct_per_h"] = needed
    why = None
    if pending:
        why = "possible reset; waiting for the next reading"
    elif disagrees:
        why = "reset time disagrees between readings"
    elif ambiguous:
        why = "reset time spread over more than an hour"
    elif secs_to_reset is not None and secs_to_reset <= 0:
        why = "reset passed; waiting for a new reading"
    elif blist and (now - blist[-1]["time"]).total_seconds() > 2 * params["sample_s"]:
        age_h = (now - blist[-1]["time"]).total_seconds() / 3600
        why = f"last reading {_hours(age_h)} h old"
    elif len(in_window) < 3:
        why = f"only {len(in_window)} readings in the last {params['trailing_h']} h"
    elif pace["valid_pct"] < 50 or pace["observed_pct"] < 50:
        if disagree_time > 0:
            why = f"readings disagree for {_hours(disagree_time / 3600)} of the last {params['trailing_h']} h"
        else:
            why = f"readings cover only {_hours(max(0.0, (valid_dur - gap_time) / 3600))} of the last {params['trailing_h']} h"
    pace["why"] = why
    allowed = why is None
    # Gaps and thin coverage invalidate the rate, not a fresh usage level.
    # Unsettled, expired or stale readings cannot establish a reached ceiling.
    level_known = (bool(in_window) and not (pending or disagrees or ambiguous)
                   and secs_to_reset is not None and secs_to_reset > 0
                   and (now - blist[-1]["time"]).total_seconds() <= 2 * params["sample_s"])
    if not level_known:
        pace["plan"]["needed_pct_per_h"] = None

    def target(headroom, cap_pct=None):
        out = {"verdict": "unknown", "needed_pct_per_h": None, "runs_out_at": None,
               "before_reset_s": None, "unused_at_reset_pct": None}
        if cap_pct is not None:
            out = {"revision": None, "pct": cap_pct, **out}
        if not level_known or secs_to_reset is None or secs_to_reset <= 0:
            return out
        n = headroom / (secs_to_reset / 3600)
        out["needed_pct_per_h"] = n
        if headroom <= 0:
            out["verdict"] = "cap_reached" if cap_pct is not None else "limit_reached"
            return out
        if not allowed or rate is None:
            return out
        if rate < 0.01:
            out.update(verdict="underpacing", unused_at_reset_pct=headroom)
            return out
        if rate > 1.15 * n:
            run_s = headroom / rate * 3600
            out.update(verdict="outpacing", runs_out_at=iso(now + timedelta(seconds=run_s)),
                       before_reset_s=int(secs_to_reset - run_s))
        elif rate < 0.85 * n:
            out.update(verdict="underpacing", unused_at_reset_pct=headroom - rate * secs_to_reset / 3600)
        else:
            out["verdict"] = "on_pace"
        return out

    if used is not None and level_known:
        if secs_to_reset is not None and secs_to_reset <= 0:
            pace["plan"]["verdict"] = "unknown"
        elif 100 - used <= 0:
            pace["plan"].update(verdict="limit_reached", needed_pct_per_h=needed)
        elif allowed:
            pace["plan"] = target(100 - used)
    share = min((c for c in caps if c["kind"] == "share"), key=lambda c: c["pct"], default=None)
    if share:
        sc = target(share["pct"] - used, share["pct"]) if used is not None else {"verdict": "unknown"}
        sc["revision"], sc["pct"] = share["revision"], share["pct"]
        # Keep the closed schema's key order and fields.
        pace["share_cap"] = {"revision": sc["revision"], "pct": sc["pct"], "verdict": sc["verdict"],
                             "needed_pct_per_h": sc.get("needed_pct_per_h"), "runs_out_at": sc.get("runs_out_at"),
                             "before_reset_s": sc.get("before_reset_s"), "unused_at_reset_pct": sc.get("unused_at_reset_pct")}
    day = min((c for c in caps if c["kind"] == "per_day"), key=lambda c: c["pct"], default=None)
    if day:
        day_start = now - timedelta(hours=24)
        dgain, ddur, dgap, _ddis = clipped(day_start, now)
        dobs = max(0.0, (ddur - dgap) / 86400 * 100)
        verdict = "unknown"
        day_why = None
        if ddur < 20 * 3600 or dobs < 50:
            day_why = f"the last 24 h are covered for only {_hours(max(0.0, (ddur - dgap) / 3600))} h"
        reaches = peak = peak_at = None
        settles = 24 * rate if rate is not None else None
        if day_why is None:
            verdict = "over" if dgain > day["pct"] else "near" if dgain >= 0.85 * day["pct"] else "under"
            # Exact walk of S(delta) = G + r*delta - A(delta), breakpoints at
            # each bucket time's age-out moment. A is swept once: every valid
            # interval adds its gain rate to A's slope between the moments
            # its start and its end age out, so the walk is linear.
            points = sorted({b["time"] for b in blist if day_start <= b["time"] <= now})
            breaks = sorted({0.0, 86400.0, *[(t - day_start).total_seconds() for t in points]})
            breaks = [b for b in breaks if 0 <= b <= 86400]
            slope_at: dict[float, float] = {}
            for a, b in zip(blist, blist[1:]):
                if a["segment"] != b["segment"] or a["correction"] or b["correction"] or b["value"] < a["value"]:
                    continue
                dur = (b["time"] - a["time"]).total_seconds()
                if dur <= 0:
                    continue
                start_age = max(0.0, (max(a["time"], day_start) - day_start).total_seconds())
                end_age = min(86400.0, (b["time"] - day_start).total_seconds())
                if end_age <= start_age:
                    continue
                per_s = (b["value"] - a["value"]) / dur
                slope_at[start_age] = slope_at.get(start_age, 0.0) + per_s
                slope_at[end_age] = slope_at.get(end_age, 0.0) - per_s

            aged, slope = 0.0, slope_at.get(0.0, 0.0)

            def s_of(delta, aged_value):
                return dgain + (rate or 0) * delta / 3600 - aged_value

            peak, peak_delta = s_of(0.0, 0.0), 0.0
            reaches = None
            for a, b in zip(breaks, breaks[1:]):
                sa = s_of(a, aged)
                aged_b = aged + slope * (b - a)
                sb = s_of(b, aged_b)
                for d, value in ((a, sa), (b, sb)):
                    if value > peak:
                        peak, peak_delta = value, d
                if verdict != "over" and reaches is None:
                    if sa >= day["pct"]:
                        reaches = a
                    elif sa < day["pct"] <= sb and sb > sa:
                        reaches = a + (day["pct"] - sa) / (sb - sa) * (b - a)
                aged = aged_b
                slope += slope_at.get(b, 0.0)
            peak_at = iso(now + timedelta(seconds=peak_delta))
            if verdict == "over":
                reaches = 0.0
            reaches = iso(now + timedelta(seconds=reaches)) if reaches is not None else None
        pace["day_cap"] = {"revision": day["revision"], "pct": day["pct"], "verdict": verdict,
                           "used_24h_pct": dgain, "observed_pct": dobs, "settles_at_pct": settles,
                           "peak_pct": peak, "peak_at": peak_at, "reaches_cap_at": reaches,
                           "why": day_why}
    if any(c.get("purpose") == "check" and c["kind"] in ("share", "per_day") for c in caps):
        # The headline uses the strictest ceiling, but each fact retains
        # its own verdict even when another purpose has a lower ceiling.
        ceiling_verdicts = []
        for cap in caps:
            if cap["kind"] == "share":
                verdict = target(cap["pct"] - used, cap["pct"])["verdict"] if used is not None else "unknown"
            elif cap["kind"] == "per_day":
                daily = pace["day_cap"] or {}
                gain = daily.get("used_24h_pct")
                verdict = ("unknown" if daily.get("verdict") == "unknown" or gain is None else
                           "over" if gain > cap["pct"] else "near" if gain >= .85 * cap["pct"] else "under")
            else:
                continue
            ceiling_verdicts.append({"kind": cap["kind"], "purpose": cap.get("purpose", "job"),
                                     "verdict": verdict})
        pace["ceiling_verdicts"] = ceiling_verdicts
    pace["used_pct"] = used
    for field, target_cap in targets.items():
        # Ahead of an even pace toward the target, apart from the forecast:
        # allowance grows linearly with the elapsed share of the window.
        target = {"revision": target_cap["revision"], "pct": target_cap["pct"],
                  "slack": target_cap["slack"], "allowance_pct": None, "used_pct": used,
                  "verdict": "unknown", "why": None}
        if used is None:
            target["why"] = "no reading"
        elif secs_to_reset is None or secs_to_reset <= 0:
            target["why"] = "no reset time"
        elif not level_known:
            target["why"] = why or "no current reading"
        else:
            elapsed = min(max(params["window_s"] - secs_to_reset, 0.0), float(params["window_s"]))
            allowance = target_cap["pct"] * elapsed / params["window_s"] + target_cap["slack"]
            target["allowance_pct"] = allowance
            # Recovery needs a full point under the allowance (hysteresis),
            # and only after the account was ahead in this cycle: the band
            # between reads "recovering" then, never for one never ahead.
            was_ahead = any(
                b["value"] > target_cap["pct"] * min(max(
                    params["window_s"] - (representative - b["time"]).total_seconds(), 0.0),
                    float(params["window_s"])) / params["window_s"] + target_cap["slack"]
                for b in blist if b["segment"][0] == current_key)
            target["verdict"] = ("ahead" if used > allowance else
                                 "within" if used <= allowance - 1 or not was_ahead else "recovering")
        pace[field] = target
    # Headline: reached/over, then earliest crossing, then near, then plan.
    ranked = []
    if pace["plan"]["verdict"] in ("limit_reached",):
        ranked.append((0, 0, "plan"))
    if pace["share_cap"] and pace["share_cap"]["verdict"] == "cap_reached":
        ranked.append((0, 0, "share_cap"))
    if pace["day_cap"] and pace["day_cap"]["verdict"] == "over":
        ranked.append((0, 0, "day_cap"))
    for name, obj, key in (("plan", pace["plan"], "runs_out_at"),
                           ("share_cap", pace["share_cap"], "runs_out_at"),
                           ("day_cap", pace["day_cap"], "reaches_cap_at")):
        if obj and obj.get(key):
            ranked.append((1, (parse(obj[key]) - now).total_seconds(), name))
    if pace["day_cap"] and pace["day_cap"]["verdict"] == "near":
        ranked.append((2, 0, "day_cap"))
    if pace["share_cap"] and pace["share_cap"]["verdict"] == "near":
        ranked.append((2, 0, "share_cap"))
    pace["headline"] = min(ranked)[2] if ranked else "plan"
    return pace


def pace_line(label, pace, now):
    word = "weekly" if pace["window"] == "weekly" else "five-hour"
    rate = pace.get("rate_pct_per_h")
    rate_text = f"{_rate_words(rate)}%/h" if rate is not None else "no rate"
    headline = pace.get("headline") or "plan"
    obj = pace.get(headline) or pace["plan"]
    verdict = obj.get("verdict", "unknown")
    target = pace.get("pace_target") or {}
    if verdict in ("limit_reached", "cap_reached"):
        return f"{label} {word}: {verdict.replace('_', ' ')} at the trailing average ({rate_text})"
    if target.get("verdict") in ("ahead", "recovering"):
        allowance = target.get("allowance_pct")
        allowance_text = f"{allowance:.2f}%" if isinstance(allowance, (int, float)) else "its allowance"
        return (f"{label} {word}: {target['verdict']} of its pace target "
                f"({target.get('used_pct'):g}% used against a {allowance_text} allowance)")[:160]
    if verdict == "unknown" or pace.get("why"):
        return f"{label} {word}: pace unknown at the trailing average: {pace.get('why') or 'not enough readings'}"
    needed = obj.get("needed_pct_per_h")
    needed_text = f"{_rate_words(needed)}%/h" if needed is not None else "no rate"
    lands = f"{needed_text} lands on {obj.get('pct') or 100:g}%" if isinstance(obj.get('pct') or 100, (int, float)) \
        else f"{needed_text} lands on 100%"
    gappy = " (gappy readings)" if pace.get("gappy") else ""
    lines = []
    if obj.get("runs_out_at"):
        runs = parse(obj["runs_out_at"])
        before = obj.get("before_reset_s")
        tail = f", {_span(before)} before reset" if before is not None else ""
        lines.append(f"{label} {word}: {verdict} at the trailing average ({rate_text}); runs out "
                     f"{_clock(runs, now)}{tail}; {lands}{gappy}")
        lines.append(f"{label} {word}: {verdict} at the trailing average ({rate_text}); runs out "
                     f"{_clock(runs, now)}; {lands}{gappy}")
    elif obj.get("reaches_cap_at"):
        lines.append(f"{label} {word}: {verdict} at the trailing average ({rate_text}); reaches its cap "
                     f"{_clock(parse(obj['reaches_cap_at']), now)}; {lands}{gappy}")
    elif verdict == "underpacing" and obj.get("unused_at_reset_pct") is not None:
        lines.append(f"{label} {word}: underpacing at the trailing average ({rate_text}); about "
                     f"{obj['unused_at_reset_pct']:g}% unused at the reset; {lands}{gappy}")
    lines.append(f"{label} {word}: {verdict} at the trailing average ({rate_text}); {lands}{gappy}")
    lines.append(f"{label} {word}: {verdict} ({rate_text}); {lands}{gappy}")
    for line in lines:
        if len(line) <= 160:
            return line
    return lines[-1][:160]


def forecast(pace, account, label, now=None):
    headline = pace.get("headline") or "plan"
    obj = pace.get(headline) or pace["plan"]
    eta = obj.get("runs_out_at") or obj.get("reaches_cap_at")
    if eta is None and now is not None and pace.get("cycle_resets_at"):
        # A verdict that is not outpacing still projects a run-out time at
        # the trailing rate; without it the forecast's eta would appear and
        # disappear as the verdict crosses its threshold.
        rate, needed = pace.get("rate_pct_per_h"), obj.get("needed_pct_per_h")
        secs = (parse(pace["cycle_resets_at"]) - now).total_seconds()
        if rate and needed and secs > 0:
            eta = iso(now + timedelta(seconds=needed / rate * secs))
    eta_s = None
    if eta and now is not None:
        eta_s = (parse(eta) - now).total_seconds()
    return {"account": account, "label": label, "window": pace["window"], "headline": headline,
            "verdict": obj.get("verdict", "unknown"), "revision": obj.get("revision") or "plan",
            "cycle": pace.get("cycle_resets_at"), "availability": "unknown" if pace.get("why") else "known",
            "eta_at": eta, "eta_s": eta_s, "rate_pct_per_h": pace.get("rate_pct_per_h"),
            "needed_pct_per_h": obj.get("needed_pct_per_h"),
            "line_verdict": obj.get("verdict", "unknown")}


def effective_forecast(acked, current, now):
    """Current forecast with verdict hysteresis held from the acknowledged one."""
    out = dict(current)
    if not acked or acked.get("account") != current["account"]:
        return out
    rate, needed = current.get("rate_pct_per_h"), current.get("needed_pct_per_h")
    if rate is None or not needed:
        return out
    if acked.get("verdict") == "outpacing" and rate > 1.10 * needed:
        out["verdict"] = "outpacing"
    elif acked.get("verdict") == "underpacing" and rate < 0.90 * needed:
        out["verdict"] = "underpacing"
    return out


def should_push(acked, current, now):
    if acked is None:
        return True
    for key in ("verdict", "window", "headline", "revision", "cycle", "availability"):
        if acked.get(key) != current.get(key):
            return True
    old_eta, new_eta = acked.get("eta_at"), current.get("eta_at")
    if (old_eta is None) != (new_eta is None):
        return True
    if old_eta and new_eta:
        # Bands compare remaining time, but movement compares the absolute
        # projected run-out times: a forecast that has not moved does not
        # push merely because the clock advanced.
        old_s = acked.get("eta_s")
        new_s = current.get("eta_s")
        if old_s is None:
            old_s = (parse(old_eta) - now).total_seconds()
        if new_s is None:
            new_s = (parse(new_eta) - now).total_seconds()
        for band in (86400, 21600, 3600, 900):
            if old_s >= band > new_s:
                return True
        moved_s = abs((parse(new_eta) - parse(old_eta)).total_seconds())
        if moved_s > max(900, 0.25 * abs(old_s)):
            return True
    return False


def should_sample(existing, candidate, *, costly=False):
    """Sampling cadence for one account window from its stored samples."""
    window = candidate["window"]
    interval = 1800 if costly or window == "weekly" else 600
    prior = [s for s in existing if s["window"] == window and s.get("mac") == candidate.get("mac")
             and s.get("source") == candidate.get("source")]
    if not prior:
        return True
    last = max(prior, key=lambda s: s["t"])
    age = (candidate["t"] - last["t"]).total_seconds()
    if age <= 0:
        return False
    if age >= 3600:
        return True
    if abs(candidate["used"] - last["used"]) >= 0.5 and age >= 120:
        return True
    # The fast cadence applies only while usage moved in the last hour;
    # an idle window is sampled hourly (the age check above).
    recent = [s for s in prior if (candidate["t"] - s["t"]).total_seconds() <= 3600]
    moved_recently = len(recent) > 1 and max(s["used"] for s in recent) > min(s["used"] for s in recent)
    return moved_recently and age >= interval
