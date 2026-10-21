"""Owner-local delivery rows for ``routing.public_view`` (survey rows 8-11).

This module only reads the same delivery ledgers, replication cursors and
routing view that slice 16's health evidence already reads. Its output is
built solely for the owner's ``khipu route --json`` view: friendly
conversation names, Mac labels, last-prompt times and per-change
recipients must never ride a push, a child page, the MCP conversation
view, a replication record, doctor output, a log or the gateway
projection. Every read failure reads as empty, never as an error.
"""
from __future__ import annotations

import hashlib
import json
import re

_SESSION_SHAPED = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,})",
    re.IGNORECASE)
# How long another Mac's copy may lag before its row reads as behind; the
# same bound ``routing_replication.status`` uses for its stale verdict.
_MAC_STALE_SECONDS = 600


def _clean_name(value):
    if not isinstance(value, str):
        return None
    name = " ".join(value.split()).strip()
    if not name or "/" in name or "\\" in name or _SESSION_SHAPED.search(name):
        return None
    return name[:80].rstrip() or None


def _author_names(view):
    """Ledger-hash -> the name a conversation gave itself in the record.

    Authors are already in the fold (``written_by`` with a session and an
    optional self-chosen name); a ledger file is named by the hash of its
    canonical session, so a name is known only when that hash matches.
    Session ids themselves never leave this function."""
    names: dict[str, str] = {}

    def visit(value):
        if isinstance(value, dict):
            name = _clean_name(value.get("name"))
            session = value.get("session")
            if name and isinstance(session, str) and session:
                from khipu import routing

                for form in routing._session_forms(session):
                    names.setdefault(hashlib.sha256(form.encode("utf-8")).hexdigest(), name)
            for inner in value.values():
                visit(inner)
        elif isinstance(value, list):
            for inner in value:
                visit(inner)

    visit(view)
    return names


def _component_kind(component):
    if component == "main":
        return "main"
    if component == "mcp":
        return "mcp"
    return "agent"


def _has_change(state, entities, version):
    if not isinstance(state, dict) or not entities:
        return False
    acked_through = state.get("acked_through")
    if isinstance(acked_through, int) and isinstance(version, int) and acked_through >= version:
        return True
    entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
    done = state.get("done") if isinstance(state.get("done"), dict) else {}
    return all(entity in done or bool((entries.get(entity) or {}).get("acked"))
               for entity in entities)


def delivery_data(view, now, cfg):
    """Live rows, other Macs and per-change recipients for the owner view.

    Returns ``{"rows": [...], "macs": [...], "recipients": {version: ...}}``
    with no session id, Mac id or path in any field."""
    try:
        return _delivery_data(view, now, cfg)
    except Exception:  # noqa: BLE001 - a damaged source reads as empty
        return {"rows": [], "macs": [], "recipients": {}}


def _delivery_data(view, now, cfg):
    from khipu import recall_prompt, routing, routing_delivery, routing_health

    cfg = cfg or {}
    mac_names = cfg.get("routing_mac_names") if isinstance(cfg.get("routing_mac_names"), dict) else {}
    local_mac = routing._local_mac()
    local_label = _clean_name(mac_names.get(local_mac)) or "This Mac"
    authors = _author_names(view)
    folder = recall_prompt._dedup_path("ledger").parent
    try:
        paths = sorted(folder.glob("*.routing.json"))
    except OSError:
        paths = []
    rows = []
    identities = []  # one entry per ledger file, for change recipients
    for path in paths:
        try:
            if path.stat().st_size > routing_delivery._SESSION_FILE_BUDGET:
                continue
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                continue
        except (OSError, ValueError):
            continue
        ledger_hash = path.name.removesuffix(".routing.json").removeprefix("recall--")
        attachment = data.get("attachment") if isinstance(data.get("attachment"), dict) else {}
        attached = routing_health._time(attachment.get("at"))
        components = [attachment.get("component")] if attached else []
        components += [name for name in data if name.startswith("agent:")]
        if not components:
            continue
        friendly = _clean_name(attachment.get("friendly_name")) or authors.get(ledger_hash)
        identity = {"friendly": friendly, "label": None, "states": []}
        for component in components:
            state = data.get(component)
            if not isinstance(state, dict):
                continue
            stamp = routing_health._time(state.get("last_delivery_point"))
            if stamp is None or stamp > now:
                continue
            live_after = cfg.get("routing_delivery_live_after")
            if live_after and (now - stamp).total_seconds() > live_after:
                continue
            health = routing_health.component_health(state, now, slack=cfg.get("routing_delivery_slack", 2))
            identity["states"].append((component, state))
            rows.append({
                "identity": identity,
                "harness": routing_delivery.health_harness(
                    state.get("harness") if component.startswith("agent:") else attachment.get("harness")),
                "component": _component_kind(component),
                "mac": "Cloud" if attachment.get("gateway") else local_label,
                "last_prompt_at": state.get("last_delivery_point"),
                "status": health["status"],
                "reasons": health.get("reasons") or [],
                "missing": health.get("missing") or [],
                "owed": health.get("owed") if isinstance(health.get("owed"), int) else None,
                "_stamp": stamp,
            })
        if identity["states"]:
            identities.append(identity)
    rows.sort(key=lambda row: (-row["_stamp"].timestamp(), row["component"]))
    numbers: dict[int, int] = {}
    for row in rows:
        key = id(row["identity"])
        if key not in numbers:
            numbers[key] = len(numbers) + 1
            row["identity"]["label"] = row["identity"]["friendly"] or f"Conversation {numbers[key]}"
        row["label"] = row["identity"]["label"]
        del row["identity"], row["_stamp"]
    recipients = {}
    for change in view.get("changes") or []:
        if not isinstance(change, dict):
            continue
        entities = [e for e in change.get("entities") or [] if isinstance(e, str)]
        version = change.get("version")
        if not entities or not isinstance(version, int) or not identities:
            continue
        have, waiting = [], []
        for identity in identities:
            # States are in doctor order (attachment component first), so
            # the first live component is the counting one, as in slice 16.
            state = _counting_state(identity)
            (have if _has_change(state, entities, version) else waiting).append(identity["label"])
        recipients[version] = {"have_count": len(have), "live_count": len(identities),
                               "have": have, "waiting": waiting}
    return {"rows": rows, "macs": _other_macs(now, mac_names, local_mac), "recipients": recipients}


def _counting_state(identity):
    states = identity["states"]
    return states[0][1] if states else {}


def _other_macs(now, mac_names, local_mac):
    from khipu import routing, routing_replication

    try:
        sources = routing_replication.status(now).get("sources") or []
    except Exception:  # noqa: BLE001 - replication evidence is optional
        return []
    macs = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        mac = source.get("mac")
        if not isinstance(mac, str) or not mac or mac == local_mac:
            continue
        seen = routing._parse(source.get("last_seen_at"))
        age = max(0, int((now - seen).total_seconds())) if seen else None
        macs.append({"label": _clean_name(mac_names.get(mac)) or "another Mac",
                     "age_seconds": age,
                     "behind": bool(age is not None and age > _MAC_STALE_SECONDS)})
    macs.sort(key=lambda row: (row["age_seconds"] is None, row["age_seconds"] or 0))
    return macs
