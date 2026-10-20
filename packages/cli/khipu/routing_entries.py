"""Entry kinds and observation lifetimes, independent of a Mac's cache.

Legacy lines are interpreted here without changing their bytes. Availability
is local; a quota reading can travel with its original witness and expiry.
"""
from __future__ import annotations

import hashlib
import hmac

from khipu import routing_events as identity


OBSERVATIONS = frozenset({"evidence", "observation", "observation_renew", "source_seen"})
WORDS = frozenset({"rule_add", "rule_edit", "rule_retire", "rule_restore", "rule_confirm",
                   "rule_unconfirm", "rule_undo", "rule_dismiss", "note_set", "note_end"})


def kind(event: dict) -> str:
    if event.get("type") in OBSERVATIONS:
        return "observation"
    return "owner_rule" if event.get("type") in WORDS else "fact"


def scope(event: dict, mac: str | None = None) -> dict:
    if event.get("observation_scope"):
        return event["observation_scope"]
    # Sign-in and installation failures are facts about this machine.
    local = event.get("state") == "down" and event.get("reason") in {
        "not_signed_in", "not_installed", "signin", "not-installed"}
    return {"kind": "machine", "mac": event.get("mac") or mac or "legacy"} if local else {"kind": "account_quota"}


def expiry(event: dict, cfg: dict):
    from khipu import routing as r

    if event.get("state") == "limited":
        return event.get("until")
    witness = r._parse(event.get("witness_at") or event.get("written_at"))
    seconds = cfg["routing_stale_after"]
    return r._iso(r._after(witness, seconds)) if witness is not None and seconds else None


def enrich(event: dict, cfg: dict, mac: str) -> None:
    from khipu import routing as r

    event.setdefault("kind", kind(event))
    if event.get("type") in ("evidence", "observation", "source_seen"):
        event.setdefault("witness_at", event["written_at"])
        event.setdefault("observation_scope", scope(event, mac))
        if event.get("state") == "limited" and not event.get("until"):
            event["until"] = r._iso(r._after(r._parse(event["witness_at"]),
                                          cfg["routing_report_default_limit"] or 1800))
        event.setdefault("expires_at", expiry(event, cfg))
    if event.get("type") == "hold" and (event.get("scope") or {}).get("kind") == "machine":
        event["scope"] = dict(event["scope"], mac=event["scope"].get("mac") or mac)


def observations(events: list[dict], cfg: dict) -> list[dict]:
    """Resolve renewals as a set, including ones received before their base."""
    from khipu import routing as r

    renewals = {}
    for event in events:
        if event.get("type") != "observation_renew":
            continue
        witness = r._parse(event.get("witness_at"))
        if witness is not None:
            renewals.setdefault(event.get("target"), []).append((witness, event))
    out = []
    for event in events:
        if event.get("type") not in ("evidence", "observation"):
            continue
        row = dict(event)
        modern = event.get("type") == "observation" or event.get("kind") == "observation"
        if modern:
            if event.get("type") == "evidence" and event.get("source") == "report":
                settled, original = r._parse(event.get("written_at")), r._parse(event.get("witness_at"))
                if settled is not None and original is not None and settled != original:
                    row["witness_at"] = r._iso(settled)
                    end = r._parse(row.get("expires_at"))
                    if end is not None:
                        row["expires_at"] = r._iso(end + (settled - original))
            witness = r._parse(row.get("witness_at") or row.get("written_at"))
            candidates = [(at, renewal) for at, renewal in renewals.get(row.get("id"), [])
                          if witness is not None and at > witness
                          and renewal.get("written_by") == row.get("written_by")
                          and renewal.get("mac") == row.get("mac")]
            if candidates:
                at, renewal = max(candidates, key=lambda pair: (pair[0], identity.hlc_key(pair[1])))
                base_expiry = r._parse(row.get("expires_at"))
                lifetime = base_expiry - witness if base_expiry is not None and witness is not None else None
                renewed_expiry = r._parse(renewal.get("expires_at"))
                # A renewal proves a newer witness, never a longer kind of
                # observation.  A remote or damaged line therefore cannot
                # turn a twelve-hour observation into a year-long one.
                expires_at = (min(renewed_expiry, at + lifetime) if renewed_expiry is not None
                              and lifetime is not None and lifetime.total_seconds() >= 0 else base_expiry)
                # A LIMITED observation's reset/limit end belongs to its
                # original scope.  A later reader may have a longer default
                # lifetime, but it cannot move that fixed end forward.
                scope_end = r._parse(row.get("until")) if row.get("state") == "limited" else None
                if scope_end is not None and (expires_at is None or expires_at > scope_end):
                    expires_at = scope_end
                row.update(witness_at=r._iso(at), expires_at=r._iso(expires_at))
                if "used_pct" in renewal:
                    row["used_pct"] = renewal["used_pct"]
            row["written_at"] = row.get("witness_at") or row["written_at"]
            row["observation_scope"] = scope(row)
        out.append(row)
    return out


def slot(event: dict) -> tuple:
    locality = scope(event)
    return (event.get("account"), event.get("runner"), event.get("lane"), event.get("window"),
            event.get("scope") or "all", event.get("source"), locality["kind"], locality.get("mac"))


def source_id(value: str) -> str:
    """The persisted source key, salted before a local path-derived id travels."""
    from khipu import routing as r

    return hmac.new(r._id_salt(operations=True), value.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:12]


def state(event: dict) -> tuple:
    return tuple(event.get(k) for k in ("state", "until", "basis", "reason", "model", "family", "transport", "reset_at"))


def source_events(extra: list[dict], events: list[dict], cfg: dict, mac: str,
                  key_ids: dict) -> list[dict]:
    """Only transitions or throttled, strictly newer witnesses append."""
    from khipu import routing as r

    latest = {}
    for row in observations(events, cfg):
        if row.get("type") != "observation" or row.get("mac") != mac:
            continue
        row = dict(row, account=key_ids.get(row.get("account"), row.get("account")))
        latest[slot(row)] = row
    batch = []
    for item in extra:
        witness = item.get("at")
        if witness is None:
            continue
        row = {k: item[k] for k in ("account", "runner", "lane", "state", "basis", "scope", "window",
                                   "used_pct", "source", "written_by", "via", "evidence", "reason",
                                   "model", "family", "transport", "observed", "session_logs",
                                   "message_time", "until_default", "reset_at", "declaration") if k in item}
        if "declaration" in row:
            # Login bindings are local; the observation already carries a stable account id.
            row["declaration"] = {k: v for k, v in row["declaration"].items() if k != "login_account"}
        row.update(type="observation", id=identity.uuid7(), witness_at=r._iso(witness),
                   written_at=r._iso(witness), until=r._iso(item.get("until")),
                   account=key_ids.get(item.get("account"), item.get("account")))
        row["observed_turn"] = bool(item.get("turn"))
        row["unmatched"] = bool(item.get("runner") and not item.get("account"))
        row["source_id"] = source_id(item["id"])
        enrich(row, cfg, mac)
        previous = latest.get(slot(row))
        if previous is None and row.get("runner"):
            previous = next((e for e in reversed(list(latest.values())) if e.get("runner") == row["runner"]
                             and e.get("lane") == row.get("lane") and e.get("source") == row.get("source")), None)
        prior_at = r._parse((previous or {}).get("witness_at") or (previous or {}).get("written_at"))
        if prior_at is not None and witness <= prior_at:
            continue
        crossed = previous and any(e.get("state") not in (None, row.get("state"))
                                  and key_ids.get(e.get("account"), e.get("account")) == row.get("account")
                                  and prior_at < r._parse(e.get("witness_at") or e.get("written_at")) <= witness
                                  for e in events if r._parse(e.get("witness_at") or e.get("written_at")))
        if previous and state(previous) == state(row) and not crossed:
            if (witness - prior_at).total_seconds() < cfg["routing_stale_after"] / 4:
                continue
            row = {"type": "observation_renew", "target": previous["id"], "witness_at": row["witness_at"],
                   "expires_at": row["expires_at"], "used_pct": row.get("used_pct"),
                   "source_id": row["source_id"],
                   "written_by": row.get("written_by"), "via": row.get("via")}
        batch.append(row)
        # Sources emit one reading per stream in a collection.
    return batch


def checked_level(value):
    from khipu import routing as r

    if not isinstance(value, dict) or set(value) != {"claim", "as_of", "until"}:
        return None
    if not isinstance(value["claim"], str) or not value["claim"].strip():
        return None
    start, end = r._parse(value["as_of"]), r._parse(value["until"])
    if start is None or end is None or end <= start:
        return None
    return {"claim": value["claim"], "as_of": r._iso(start), "until": r._iso(end)}


def checked_definition(name, value):
    """Closed facts carry no local binding or runner folder."""
    if name == "provider":
        from khipu.routing_providers import checked

        return checked(value)
    if not isinstance(value, dict):
        return None
    fields = {"provider": {"id", "label", "family"},
              "account": {"id", "label", "provider", "family"},
              "lane_map": {"runner", "lane", "account"}}
    required = {"provider": {"id", "label"}, "account": {"id", "label", "provider"},
                "lane_map": {"runner", "lane", "account"}}
    if name not in fields or set(value) - fields[name] or not required[name] <= set(value):
        return None
    if any(not isinstance(v, str) or not v.strip() or "/" in v or "\\" in v or "|" in v
           for k, v in value.items() if k != "label"):
        return None
    if any(not isinstance(v, str) or not v.strip() for v in value.values()):
        return None
    if name == "account":
        from khipu.routing_identity import STABLE

        if not STABLE.fullmatch(value["id"]) or value["id"].partition(":")[0] != value["provider"]:
            return None
    return dict(value)


def definitions(events: list[dict], mac: str | None) -> dict:
    out = {"providers": {}, "accounts": {}, "lane_maps": {}}
    for event in events:
        # The acceptance replay and imported provider facts use the compact
        # provider_set spelling. New owner writes use definition_set.
        if event.get("type") == "provider_set" and (event.get("written_by") or {}).get("kind") == "owner":
            from khipu.routing_providers import FIELDS

            value = {k: v for k, v in event.items() if k in FIELDS}
            value.setdefault("label", event.get("id"))
            value = checked_definition("provider", value)
            refs = event.get("accounts", [])
            if value is None or not isinstance(refs, list):
                continue
            from khipu.routing_identity import STABLE

            if any(not isinstance(a, str) or not STABLE.fullmatch(a)
                   or a.partition(":")[0] != value["id"] for a in refs):
                continue
            out["providers"][value["id"]] = value
            for ident in refs:
                out["accounts"].setdefault(ident, {"id": ident, "label": ident, "provider": value["id"]})
            continue
        if event.get("type") not in ("definition_set", "definition_remove") or (event.get("written_by") or {}).get("kind") != "owner":
            continue
        name = event.get("definition")
        value = checked_definition(name, event.get("value"))
        if value is None:
            continue
        remove = event.get("type") == "definition_remove"
        if name == "lane_map":
            if event.get("mac") != mac:
                continue
            lanes = out["lane_maps"].setdefault(value["runner"], {})
            if remove:
                lanes.pop(value["lane"], None)
            else:
                lanes[value["lane"]] = value["account"]
        else:
            values = out["providers" if name == "provider" else "accounts"]
            if remove:
                values.pop(value["id"], None)
            else:
                values[value["id"]] = value
    return out
