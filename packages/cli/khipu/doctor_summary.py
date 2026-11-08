"""The five plain lines at the top of ``khipu doctor``: for each part of Khipu,
fine, or what is wrong and what would fix it. Read from the report's own
``*_ok`` flags, so a line goes red only when the check it names does."""
from __future__ import annotations


def _outbox(d: dict) -> str:
    box = d.get("outbox") or {}
    if box.get("error"):
        return "the queue of captures can't be read"
    parts = []
    held = box.get("dead") or 0
    if held:
        parts.append(f"{held} capture{' sits' if held == 1 else 's sit'} held on this Mac, safe but not in memory, and "
                     "Khipu will not retry " + ("it" if held == 1 else "them") + " by itself")
    if box.get("unreadable"):
        parts.append(f"{box['unreadable']} queued capture file{'' if box['unreadable'] == 1 else 's'} can't be read")
    if (box.get("oldest_pending_seconds") or 0) > 3600:
        parts.append(f"{box.get('pending')} queued, the oldest for {box['oldest_pending_seconds'] // 3600} hours: "
                     "safe on this Mac, not yet in the database")
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
    from khipu.routing_health import unchecked

    parts = []
    if "prompt-skips" in (block.get("reasons") or []):
        parts.append(f"routing lines were left out of {prompts.get('out_of_time')} of the last hour's "
                     f"{prompts.get('count')} prompts")
    elif block.get("reasons"):
        reasons = block["reasons"]
        if "cross-mac-stale" in reasons:
            parts.append("the other Mac may still be using older account advice")
        if "gateway-stale" in reasons:
            parts.append("conversations connected through the cloud may still be using older account advice")
        if any(r not in {"cross-mac-stale", "gateway-stale"} for r in reasons):
            parts.append("some conversations have not received the latest account advice")
    if block.get("missing") and unchecked(block):
        gaps = unchecked(block)
        if "gateway delivered copy revision" in gaps:
            parts.append("Khipu could not confirm which account advice cloud conversations received")
        if any(m != "gateway delivered copy revision" for m in gaps):
            parts.append("Khipu could not confirm that conversations received the latest account advice")
    return "; ".join(parts) or "routing changes may not be reaching conversations"


def _capture(d: dict) -> str:
    red = (d.get("capture_liveness") or {}).get("red") or []
    return "no recent capture from " + ", ".join(red) if red else "a harness is not being recorded"


def _jobs(d: dict) -> str:
    bad = list(d.get("job_failures") or []) + [k.removesuffix("_ok") for k, v in (d.get("nightly_steps") or {}).items()
                                               if isinstance(v, dict) and not v.get("ok")]
    return "failing: " + ", ".join(bad) if bad else "a background job is failing"


AREAS = ("Saving memory", "Finding things", "Routing", "Database and disk", "Jobs and other checks")
OTHER = 4  # a failing check no one wrote words for lands here, named

# Every check that feeds overall `ok` (cli.OK_GATES), plus routing delivery, which only reports:
# key -> (area, what is wrong: text or function of the report, what would fix it: text or None for "see its block").
WORDS = {
    "outbox_ok": (0, _outbox, _outbox_fix),
    "capture_liveness_ok": (0, _capture, "run `khipu sessions liveness` for the harness and its last error"),
    "ledger_outbox_ok": (0, "queued ledger writes have not reached the database", None),
    "git_sync_ok": (0, "the memory files are not syncing to GitHub", None),
    "embed_coverage_ok": (1, _embed, "run `khipu embed backfill`"),
    "index_freshness_ok": (1, "the search index is out of date", None),
    "topics_embed_lag_ok": (1, "topic embeddings are over an hour behind", "run `khipu embed backfill`"),
    "drift_ok": (1, "memory files and the database disagree", "run `khipu reconcile`"),
    "recall_probe_ok": (1, "the capture-then-search probe failed or is over 7 days old", None),
    "prompt_recall_snapshot_ok": (1, "this Mac's offline recall copy is behind", None),
    "prompt_recall_outcomes_ok": (1, "prompt-time recall is timing out", None),
    "recall_daemon_ok": (1, "the warm recall service is running old code", None),
    "recall_eval_ok": (1, "recall accuracy fell on the nightly check", None),
    "literal_trgm_ok": (1, "a text-search index is missing", None),
    "degraded_rate_ok": (1, "searches are falling back to a weaker mode", None),
    "routing_delivery_ok": (2, _delivery,
                            "nothing for you to do; Khipu tries again on later prompts. If this is still here tomorrow, tell a conversation"),
    "hub_ok": (3, "the database does not answer", "check the connection with `khipu status`"),
    "backup_ok": (3, "the database backup is stale", None),
    "hub_snapshot_ok": (3, "this Mac's copy of the database is behind", None),
    "ledger_copy_ok": (3, "this Mac's copy of the ledger is behind", None),
    "disk_space_ok": (3, "disk space is low", "free space where Khipu writes"),
    "schema_migrations_ok": (3, "the database has pending migrations", "apply them"),
    "dsn_file_ok": (3, "the database address file is stale", None),
    "jobs_ok": (4, _jobs, "run `khipu jobs status`"),
    "nightly_steps_ok": (4, _jobs, "run `khipu jobs status`"),
    "synth_budget_ok": (4, "the summariser's daily limit is spent", "wait for UTC midnight or `khipu budget lift synth`"),
    "synth_provider_ok": (4, "the summariser's provider is not usable", None),
    "launchers_ok": (4, "an agent launcher points at a missing file", None),
    "bundle_seal_ok": (4, "the app bundle has a file it should not", None),
    "forget_list_ok": (4, "Khipu can't read this Mac's forget list", None),
}


def failing(report: dict) -> list[tuple[int, str, str]]:
    """(area, what is wrong, fix) for every gate of overall ``ok`` that is False, plus routing delivery
    and a capture at risk."""
    from khipu.cli import OK_GATES

    keys = [k for k in (*OK_GATES, "routing_delivery_ok") if k in report and not report[k]]
    out = []
    for key in keys:
        area, what, fix = WORDS.get(key, (OTHER, f"{key.removesuffix('_ok').replace('_', ' ')} is failing", None))
        out.append((area, what(report) if callable(what) else what,
                    fix(report) if callable(fix) else fix or f"see {key.removesuffix('_ok')} in the report"))
    if (report.get("outbox") or {}).get("at_risk") and "outbox_ok" not in keys:
        out.append((0, _outbox(report), _outbox_fix(report)))
    return list(dict.fromkeys(out))


def health_summary(report: dict) -> list[str]:
    """One line per area: ``Area: fine.`` or ``Area: what is wrong. Fix: ...``"""
    bad = failing(report)
    lines = []
    for i, area in enumerate(AREAS):
        here = [(what, fix) for a, what, fix in bad if a == i]
        lines.append(f"{area}: fine." if not here else f"{area}: " + " ".join(
            f"{what[:1].upper() + what[1:]}. Fix: {fix}." for what, fix in here))
    return lines
