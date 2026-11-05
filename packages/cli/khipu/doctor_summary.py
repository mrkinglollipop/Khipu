"""The five plain lines at the top of ``khipu doctor``: for each part of Khipu,
fine, or what is wrong and what would fix it. Read from the report's own
``*_ok`` flags, so a line goes red only when the check it names does."""
from __future__ import annotations


def _outbox(d: dict) -> str:
    box = d.get("outbox") or {}
    if box.get("error"):
        return "the queue of captures can't be read"
    parts = []
    waiting = box.get("dead_waiting", box.get("dead")) or 0
    if waiting:
        parts.append(f"{waiting} capture{' sits' if waiting == 1 else 's sit'} held on this Mac, safe but not in "
                     "memory, and Khipu will not retry " + ("it" if waiting == 1 else "them") + " by itself")
    if box.get("unreadable"):
        parts.append(f"{box['unreadable']} queued capture file{'' if box['unreadable'] == 1 else 's'} can't be read")
    if (box.get("oldest_pending_seconds") or 0) > 3600:
        parts.append(f"{box.get('pending')} queued, the oldest for {box['oldest_pending_seconds'] // 3600} hours: "
                     "safe on this Mac, not yet in the database")
    old = box.get("dead", 0) - waiting
    if old > 0:
        parts.append(f"{old} older ones are held too (kept whole, nothing newer has died)")
    return "; ".join(parts) or "captures are waiting past the limit"


def _outbox_fix(d: dict) -> str:
    fix = (d.get("outbox") or {}).get("fix") or "run `khipu outbox status`"
    return fix.split(". ")[0].rstrip(".")


def _embed(d: dict) -> str:
    cov = d.get("embed_coverage") or {}
    if cov.get("error"):
        return "the search index can't be checked"
    gaps = (cov.get("missing") or 0) + (cov.get("stale") or 0)
    return f"{gaps} search chunk{'' if gaps == 1 else 's'} unembedded for over an hour"


def _delivery(d: dict) -> str:
    block = d.get("routing_delivery") or {}
    prompts = block.get("prompts") or {}
    if "prompt-skips" in (block.get("reasons") or []):
        return (f"routing lines were left out of {prompts.get('out_of_time')} of the last hour's "
                f"{prompts.get('count')} prompts")
    return "routing changes may not be reaching conversations (" + ", ".join(block.get("reasons") or ["no reason given"]) + ")"


def _capture(d: dict) -> str:
    red = (d.get("capture_liveness") or {}).get("red") or []
    return "no recent capture from " + ", ".join(red) if red else "a harness is not being recorded"


def _jobs(d: dict) -> str:
    bad = list(d.get("job_failures") or []) + [k.removesuffix("_ok") for k, v in (d.get("nightly_steps") or {}).items()
                                               if isinstance(v, dict) and not v.get("ok")]
    return "failing: " + ", ".join(bad) if bad else "a background job is failing"


# (area, [(flag, what is wrong: text or function of the report, what would fix it)])
AREAS = (
    ("Saving memory", (
        ("outbox_ok", _outbox, _outbox_fix),
        ("capture_liveness_ok", _capture, "run `khipu sessions liveness` for the harness and its last error"),
        ("ledger_outbox_ok", "queued ledger writes have not reached the database", "see ledger_outbox in the report"))),
    ("Finding things", (
        ("embed_coverage_ok", _embed, "run `khipu embed backfill`"),
        ("index_freshness_ok", "the search index is out of date", "see index_freshness in the report"),
        ("drift_ok", "memory files and the database disagree", "run `khipu reconcile`"),
        ("recall_probe_ok", "the capture-then-search probe failed or is over 7 days old", "see recall_probe in the report"),
        ("prompt_recall_outcomes_ok", "prompt-time recall is timing out", "see prompt_recall_outcomes in the report"))),
    ("Routing", (
        ("routing_delivery_ok", _delivery,
         "raise the time allowed before a prompt in Settings → Routing, or see whether the Mac is busy"),)),
    ("Database and disk", (
        ("hub_ok", "the database does not answer", "check the connection with `khipu status`"),
        ("backup_ok", "the database backup is stale", "see backup in the report"),
        ("disk_space_ok", "disk space is low", "free space where Khipu writes"),
        ("schema_migrations_ok", "the database has pending migrations", "apply them"))),
    ("Background jobs", (
        ("jobs_ok", _jobs, "run `khipu jobs status`"),
        ("nightly_steps_ok", _jobs, "run `khipu jobs status`"),
        ("synth_budget_ok", "the summariser's daily limit is spent", "wait for UTC midnight or `khipu budget lift synth`"))),
)


def health_summary(report: dict) -> list[str]:
    """One line per area: ``Area: fine.`` or ``Area: what is wrong. Fix: ...``"""
    lines = []
    for area, checks in AREAS:
        bad = list(dict.fromkeys((what(report) if callable(what) else what, fix(report) if callable(fix) else fix)
                                 for flag, what, fix in checks if report.get(flag) is False))
        lines.append(f"{area}: fine." if not bad else f"{area}: " + " ".join(
            f"{what[:1].upper() + what[1:]}. Fix: {fix}." for what, fix in bad[:2]))
    return lines
