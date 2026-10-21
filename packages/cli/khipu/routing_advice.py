"""Launch advice for one account (slice 17c, section 13 A).

Advice is advisory only: it never blocks, refuses, permits or launches
anything, and it never feeds a gate. A runner or conversation may choose
to act on it; that is its own launch policy. Unknown evidence answers
``go`` with that evidence status, so the consumer applies its own policy.
"""
from __future__ import annotations

import os
from datetime import timedelta

PURPOSES = ("job", "orchestration")
TIERS = ("opus", "sonnet", "haiku")
KINDS = ("build", "fix", "check")
_KIND_CLASS = {"build": "build", "fix": "fix-round", "check": "tests"}
# Cap kinds that hold a job back, by purpose. Orchestration is not a
# runner job, so pace and concurrency caps do not hold it; share and
# spend caps hold every purpose.
_APPLIES = {
    "pace_target": {"job"},
    "per_day": {"job"},
    "concurrency": {"job"},
    "share": {"job", "orchestration"},
    "spend": {"job", "orchestration"},
}
_HOLD_VERDICTS = {
    "pace_target": {"ahead", "recovering"},
    "per_day": {"over"},
    "concurrency": {"full"},
    "share": {"cap_reached"},
    "spend": {"over", "reached"},
}


def is_dispatched(environ=None) -> bool:
    """Whether this attachment is a dispatched job, from capture lineage.

    The markers are the ones capture already trusts: ``KHIPU_JOB``, a
    parent session, ``CLAUDE_CODE_CHILD_SESSION=1``, the runner's
    dispatched-lane marker, and a Claude Code host session id. Detection
    fails closed: a host id with no own id is a child (only a host id that
    matches this session's own id is not), so a caller can never talk its
    way into orchestration advice from a dispatched context. A job that
    claims ``orchestration`` is answered as a job.
    """
    env = os.environ if environ is None else environ

    def _get(name):
        value = env.get(name)
        return value.strip() if isinstance(value, str) else ""

    if (_get("KHIPU_JOB") == "1" or _get("CLAUDE_DISPATCHED_LANE")
            or _get("KHIPU_PARENT_SESSION") or _get("CLAUDE_CODE_CHILD_SESSION") == "1"):
        return True
    host, own = _get("CLAUDE_CODE_HOST_SESSION_ID"), _get("CLAUDE_CODE_SESSION_ID")
    if not host:
        return False
    return not own or host != own


def effective_purpose(purpose, dispatched: bool) -> str:
    if purpose not in PURPOSES:
        raise ValueError("purpose must be job or orchestration")
    return "job" if dispatched and purpose == "orchestration" else purpose


def _provider_for(view: dict, entry: dict) -> dict | None:
    providers = (view.get("definitions") or {}).get("providers") or {}
    if isinstance(providers, dict):
        rows = list(providers.values())
        if entry.get("harness") in providers:
            return providers[entry["harness"]]
    else:
        rows = list(providers)
    for row in rows:
        if isinstance(row, dict) and row.get("id") == entry.get("harness"):
            return row
    return None


def _tier_of(model) -> str | None:
    from khipu.routing_advisory import model_tier

    return model_tier(model)


def eligibility(view: dict, entry: dict, *, tier=None, kind=None, privacy=False,
                explicit: bool = False) -> tuple[bool, str]:
    """Whether this account may take work of this tier/kind, from facts.

    Only structured facts decide: a provider's ``models_allowed`` /
    ``models_never`` and its ``use``. Privacy is never read from prose —
    no provider fact carries a privacy flag, so ``privacy`` excludes only
    ``on_request`` providers the owner has not explicitly named.
    """
    provider = _provider_for(view, entry)
    if provider is None:
        return True, "no provider fact restricts this account"
    if provider.get("use") == "on_request" and not explicit:
        return False, "provider is on request only"
    if tier is not None:
        allowed = provider.get("models_allowed")
        if allowed is not None and not any(_tier_of(m) == tier for m in allowed):
            return False, f"no {tier} model is allowed for this provider"
        if any(m in (provider.get("models_never") or []) and _tier_of(m) == tier
               for m in provider.get("models_never") or []):
            return False, f"{tier} models are never used for this provider"
    return True, "provider facts allow this work"


def _pace_reading_unknown(pace: dict) -> bool:
    """Whether one pace object's usage reading cannot be placed at now.

    ``buckets`` counts the readings in the trailing window and ``why`` says
    why the pace cannot be computed. A fresh reading that is merely too
    thin for a rate ("only N readings ...") still says where usage stands;
    every other reason, and no reading in the window at all, does not.
    """
    if pace.get("used_pct") is None or not pace.get("buckets"):
        return True
    why = pace.get("why") or ""
    return bool(why) and not why.startswith("only ")


def _evidence(view: dict, entry: dict, now, statuses: list[dict]) -> str:
    from khipu import routing

    if any(s.get("verdict") == "unknown" for s in statuses if s.get("kind") in _APPLIES):
        # An expired or uncovered cap reading is unknown evidence, never a
        # figure invented from partial coverage.
        if entry.get("state") not in (routing.PAUSED,):
            return "unknown"
    # Judge freshness from the usage readings themselves, never from the
    # account report. A pace object carries its own coverage and its newest
    # reading's age in ``why``: an old or otherwise unplaceable reading is
    # unknown evidence rather than a figure carried over from the report.
    paces = entry.get("pace") or []
    if paces:
        return "unknown" if any(_pace_reading_unknown(p) for p in paces) else "fresh"
    witness = entry.get("witness") or {}
    at = routing._parse(witness.get("at"))
    if entry.get("state") == routing.UNKNOWN or at is None or entry.get("silent"):
        return "unknown"
    stale_after = (routing.settings().get("routing_stale_after") or 0)
    if stale_after and (now - at).total_seconds() >= stale_after:
        return "stale"
    return "fresh"


def _crosses_within_hour(entry: dict, now) -> list[str]:
    from khipu import routing

    found = []
    for pace in entry.get("pace") or []:
        for name in ("plan", "share_cap", "day_cap"):
            obj = pace.get(name) or {}
            eta = routing._parse(obj.get("runs_out_at") or obj.get("reaches_cap_at"))
            if eta is not None and now < eta <= now + timedelta(hours=1):
                found.append(name)
    return found


_FIGURE_FIELDS = ("allowance_pct", "used_pct", "usd", "events", "jobs")


def _without_figures(status: dict) -> dict:
    """A cap status with no reading figures left, for unknown evidence."""
    return {k: (None if k in _FIGURE_FIELDS else v) for k, v in status.items()}


def account_advice(view: dict, entry: dict, now, *, purpose: str = "job",
                   tier=None, kind=None, privacy: bool = False,
                   explicit: bool = False) -> dict:
    """One account's advice at ``now``. Never a permission decision."""
    from khipu import routing

    statuses = routing.cap_statuses(view, entry, now)
    evidence = _evidence(view, entry, now, statuses)
    # An unplaceable pace reading has no figures: never show a number the
    # reading cannot support. Other readings (spend, concurrency) keep
    # their own figures; their status is already unknown without one.
    unknown_windows = {p.get("window") for p in entry.get("pace") or []
                       if _pace_reading_unknown(p)}
    statuses = [_without_figures(status)
                if status["kind"] == "pace_target" and status.get("window") in unknown_windows
                else status for status in statuses]
    numbers: dict = {}
    for status in statuses:
        if status["kind"] == "pace_target":
            numbers.update(allowance_pct=status.get("allowance_pct"),
                           used_pct=status.get("used_pct"))
        elif status["kind"] == "spend":
            numbers.update(spend_usd=status.get("usd"), spend_cap_usd=status.get("cap_usd"))
        elif status["kind"] == "concurrency":
            numbers.update(active_jobs=status.get("jobs"), concurrency_cap=status.get("cap_n"))
    numbers = {k: v for k, v in numbers.items() if v is not None}
    eligible, why_eligible = eligibility(view, entry, tier=tier, kind=kind,
                                         privacy=privacy, explicit=explicit)
    held = [h for h in view.get("holds") or []
            if (h.get("scope") or {}).get("kind") in ("account", "all", "machine")
            and ((h.get("scope") or {}).get("account") in (None, entry["key"])
                 if (h.get("scope") or {}).get("kind") == "account" else True)]
    pace_plan_closed = any((p.get("plan") or {}).get("verdict") == "limit_reached"
                           for p in entry.get("pace") or [])
    verdict, reason = "go", why_eligible
    if held:
        verdict, reason = "closed", "held"
    elif entry.get("state") == routing.PAUSED:
        verdict, reason = "closed", "paused by the owner"
    elif entry.get("state") == routing.LIMITED:
        verdict, reason = "closed", "limited"
    elif entry.get("state") == routing.DOWN:
        verdict, reason = "closed", "down"
    elif pace_plan_closed:
        verdict, reason = "closed", "plan limit reached"
    elif not eligible:
        # Facts do not allow this work for this account: never a ``go``.
        verdict, reason = "not_eligible", why_eligible
    else:
        holding = [s for s in statuses
                   if purpose in _APPLIES.get(s.get("kind"), set())
                   and s.get("verdict") in _HOLD_VERDICTS.get(s.get("kind"), set())]
        crossing = _crosses_within_hour(entry, now) if purpose == "job" else []
        if holding:
            verdict = "hold_back"
            reason = "; ".join(f"{s['kind']} {s['verdict']}" for s in holding)
        elif crossing:
            verdict = "hold_back"
            reason = "a cap crosses within the hour at the trailing average"
    return {"account": routing.account_id(entry["key"]), "label": entry.get("label"),
            "purpose": purpose, "verdict": verdict, "reason": reason,
            "evidence": evidence, "numbers": numbers,
            "caps": [{**s, "applies": purpose in _APPLIES.get(s.get("kind"), set())}
                     for s in statuses],
            "eligible": eligible, "why_eligible": why_eligible, "advisory": True}


def advice_line(advice: dict) -> str:
    purpose = "orchestration" if advice.get("purpose") == "orchestration" else "jobs"
    return (f"Advice for {purpose}: {advice['label']}: "
            f"{advice['verdict'].replace('_', ' ')} ({advice['reason']})")


def rank(view: dict, now, *, purpose: str = "job", tier=None, kind=None,
         privacy: bool = False, target_key: str | None = None) -> list[dict]:
    """Every account's advice, ranked for work of this kind.

    Eligible ``go`` accounts come first in the owner's priority order
    (accounts he did not rank follow, soonest pace first is not invented:
    they keep the record's order), then ``hold_back``, then ``closed``,
    then accounts the facts do not allow for this tier or kind. Each row
    says why it stands where it does.
    """
    if tier is None and kind is not None:
        table = view.get("tier_table") or {}
        tiers = table.get("tiers") if table.get("confirmed") is True else None
        if isinstance(tiers, dict):
            tier = tiers.get(_KIND_CLASS.get(kind or "", ""))
    priority = view.get("priority") or {}
    order = {e.get("account"): i for i, e in enumerate(priority.get("entries") or [])}
    rows = []
    entries = [e for e in view.get("accounts") or []
               if target_key is None or e["key"] == target_key]
    for entry in entries:
        advice = account_advice(view, entry, now, purpose=purpose, tier=tier, kind=kind,
                                privacy=privacy, explicit=target_key is not None)
        if not advice["eligible"]:
            group = 3
            why = advice["why_eligible"]
        elif advice["verdict"] == "go":
            group = 0
            why = (f"priority {order[entry['key']] + 1}" if entry["key"] in order
                   else "open; not in the owner's priority")
        elif advice["verdict"] == "hold_back":
            group = 1
            why = advice["reason"]
        else:
            group = 2
            why = advice["reason"]
        # The owner's order comes first. Among accounts the owner did not
        # rank, one that may go with no fresh reading ranks below one whose
        # evidence is known.
        unknown = 1 if group == 0 and advice["evidence"] == "unknown" and entry["key"] not in order else 0
        rows.append({**advice, "why_ranked": why,
                     "_rank": (group, order.get(entry["key"], len(order) + 1000), unknown)})
    rows.sort(key=lambda r: r["_rank"])
    for i, row in enumerate(rows, 1):
        row["rank"] = i
        del row["_rank"]
    return rows
