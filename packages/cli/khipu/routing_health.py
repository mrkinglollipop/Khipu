"""Delivery facts from existing ledgers, hook outcomes and replication cursors.

This module only reads evidence. It never probes, acknowledges, or feeds a gate.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else None
    except ValueError:
        return None


def _result(reasons, missing, **fields):
    return dict(status="red" if reasons else "unknown" if missing else "green",
                reasons=list(dict.fromkeys(reasons)), missing=list(dict.fromkeys(missing)), **fields)


def component_health(state, now, *, slack=2):
    """Malformed evidence is unknown, never a healthy empty component."""
    try:
        return _component_health(state, now, slack=slack)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return _result([], ["delivery ledger malformed"])


def _component_health(state, now, *, slack):
    """A revision's age and retries come only from its own component."""
    if not isinstance(state, dict) or not isinstance(state.get("entries"), dict):
        return _result([], ["delivery ledger"])
    reasons, missing = [], []
    if state.get("shown_claim_invalid"):
        reasons.append("ack-unshown")
    points = state.get("delivery_points")
    if not isinstance(points, int) or not _time(state.get("last_delivery_point")):
        missing.append("delivery point history")
    done = state.get("done") or {}
    shown_done = state.get("done_shown") or {}
    if not isinstance(done, dict) or not isinstance(shown_done, dict):
        missing.append("completed delivery receipts")
    elif any(shown_done.get(entity) != revision for entity, revision in done.items()):
        missing.append("completed shown receipt")
    for entry in state["entries"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
            missing.append("delivery entry")
            continue
        offset = entry.get("offset", 0)
        shown = entry.get("shown_offset")
        if not isinstance(offset, int) or offset < 0 or (shown is not None and
                                                        (not isinstance(shown, int) or shown < 0)):
            missing.append("delivery receipt offsets")
            continue
        if offset > len(entry["text"]) or (shown is not None and shown > len(entry["text"])):
            reasons.append("ack-unshown")
            continue
        if offset or entry.get("acked"):
            if shown is None:
                missing.append("shown receipt")
            elif shown < offset or (entry.get("acked") and shown < len(entry["text"])):
                reasons.append("ack-unshown")
        if entry.get("acked"):
            continue
        if not all(isinstance(v, int) and v >= 0 for v in
                   (points, entry.get("owed_point"), entry.get("bound"))):
            missing.append("delivery point budget")
        elif points - entry["owed_point"] >= entry["bound"] + slack:
            reasons.append("budget-stall")
        expired = entry.get("expired", 0)
        if not isinstance(expired, int) or expired < 0:
            missing.append("lease expiry count")
            expired = 0
        flight = state.get("flight") or {}
        if not isinstance(flight, dict):
            missing.append("in-flight batch")
            continue
        until = _time(flight.get("until"))
        if flight and until is None:
            missing.append("in-flight lease timestamp")
        if until and until <= now and any(
                item.get("entity") == entry.get("entity") and item.get("revision") == entry.get("revision")
                for item in flight.get("out", {}).get("routing_changes", {}).get("items", [])):
            expired += 1
        if expired >= 2:
            reasons.append("lease-expired-twice")
    return _result(reasons, missing, owed=sum(not e.get("acked") for e in state["entries"].values()
                                             if isinstance(e, dict)))


def prompt_health(now):
    from khipu import recall_prompt

    try:
        raw = recall_prompt._tail_text(recall_prompt._log_path(), max_bytes=recall_prompt._OUTCOMES_TAIL_BYTES)
    except OSError:
        return _result([], ["today's hook outcomes"])
    statuses = []
    for line in raw.splitlines():
        match = recall_prompt._LOG_LINE_RE.match(line)
        stamp = _time(match.group("stamp")) if match else None
        status = recall_prompt.routing_status_of(line)
        if stamp and stamp.date() == now.astimezone(timezone.utc).date() and stamp <= now and status:
            statuses.append(status)
    if not statuses:
        return _result([], ["today's hook outcomes"])
    skipped = sum(s.startswith("skipped:") for s in statuses)
    rate = 100 * skipped / len(statuses)
    return _result(["prompt-skips"] if rate > 1 else [], [], count=len(statuses), skipped=skipped, rate_pct=rate)


def doctor_block(view, now, cfg):
    from khipu import recall_prompt, routing, routing_delivery, routing_replication

    reasons, missing, rows, gateways = [], [], [], []
    folder = recall_prompt._dedup_path("ledger").parent
    try:
        paths = list(folder.glob("*.routing.json"))
    except OSError:
        paths = []
        missing.append("delivery ledgers unreadable")
    for path in paths:
        try:
            if path.stat().st_size > routing_delivery._SESSION_FILE_BUDGET:
                raise ValueError("ledger too large")
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("ledger shape")
        except (OSError, ValueError):
            missing.append("delivery ledger unreadable")
            continue
        attachment = data.get("attachment") or {}
        if not isinstance(attachment, dict):
            missing.append("current attachment capability")
            continue
        attached = _time(attachment.get("at"))
        components = [attachment.get("component")] if attached else []
        agents = [name for name in data if name.startswith("agent:")]
        components += agents
        if agents and not attached:
            missing.append("parent attachment evidence")
        if not components and any(name in data for name in ("main", "mcp")):
            missing.append("current attachment capability")
        for component in components:
            state = data.get(component)
            stamp = _time((state or {}).get("last_delivery_point")) if isinstance(state, dict) else None
            live_at = stamp
            if not live_at:
                missing.append("last delivery point")
                continue
            if live_at > now:
                missing.append("delivery point is ahead of the clock")
                continue
            if cfg["routing_delivery_live_after"] and (now - live_at).total_seconds() > cfg["routing_delivery_live_after"]:
                continue
            if attachment.get("gateway"):
                # A quiet gateway is not a stale copy.  Once its identity is
                # outside the same liveness window as every other delivery
                # identity, it no longer supplies gateway evidence at all.
                gateways.append(attachment)
            health = component_health(state, now, slack=cfg["routing_delivery_slack"])
            rows.append(dict(identity=path.name.removesuffix(".routing.json").removeprefix("recall--"),
                             component=component,
                             harness=routing_delivery.health_harness(
                                 state.get("harness") if component.startswith("agent:") else attachment.get("harness")),
                             last_delivery_point=state["last_delivery_point"], **health))
            reasons += health["reasons"]
            missing += health["missing"]
    if not rows:
        missing.append("live delivery identities")
    prompts = prompt_health(now)
    reasons += prompts["reasons"]
    missing += prompts["missing"]
    replication = {"status": "not_applicable"}
    if routing._replication_live():
        try:
            replication = routing_replication.status(now)
        except (TypeError, ValueError, OverflowError):
            replication = {"age_seconds": None}
        if replication["age_seconds"] is None:
            missing.append("cross-Mac caught-up cursor")
            replication["status"] = "unknown"
        else:
            replication["status"] = "red" if replication["stale"] else "green"
            if replication["stale"]:
                reasons.append("cross-mac-stale")
    gateway = {"status": "not_applicable"}
    if cfg["routing_cloud_copy"]:
        current_epoch, current_revision = view.get("epoch"), view.get("version")
        current_at = _time(view.get("folded_at"))
        gateway_rows = [(row.get("copy_epoch"), row.get("copy_revision"), _time(row.get("copy_revision_at")))
                        for row in gateways]
        gateway_rows = [(epoch, revision, stamp) for epoch, revision, stamp in gateway_rows
                        if isinstance(epoch, str) and isinstance(revision, int) and stamp and stamp <= now]
        if not gateway_rows or not isinstance(current_epoch, str) or not isinstance(current_revision, int) or current_at is None:
            gateway = {"status": "unknown"}
            missing.append("gateway delivered copy revision")
        else:
            behind = [(epoch, revision, stamp) for epoch, revision, stamp in gateway_rows
                      if (epoch, revision) != (current_epoch, current_revision)]
            # A client becomes behind when this revision is folded, not
            # when it last pulled an older revision during a quiet period.
            age = max(0.0, (now - current_at).total_seconds()) if behind else 0.0
            stale = bool(behind and cfg["routing_cloud_copy_stale_after"]
                         and age > cfg["routing_cloud_copy_stale_after"])
            gateway = dict(status="red" if stale else "green", age_seconds=age)
            if stale:
                reasons.append("gateway-stale")
    for lane in view.get("unmatched") or []:
        stamp = _time(lane.get("limited_since"))
        if stamp is None:
            missing.append("unmatched limit run timestamp")
        elif (now - stamp).total_seconds() >= 3600:
            reasons.append("unmatched-limit")
    return _result(reasons, missing, identities=rows, prompts=prompts,
                   replication=replication, gateway=gateway)
