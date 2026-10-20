"""Event identity shared by the local writer and the pure routing fold.

Legacy identities are derived without modifying the log. Rule numbers are
display aliases; only an entity id can identify a rule across machines.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid


NAMESPACE = uuid.UUID("19fa2d39-15d2-55d1-8c10-458320d66a5a")
RULE_CHANGES = frozenset({"rule_edit", "rule_retire", "rule_restore", "rule_confirm", "rule_undo",
                          "rule_unconfirm", "rule_dismiss"})


def uuid7() -> str:
    # RFC 9562: 48 timestamp bits, version 7, 74 random bits, RFC variant.
    stamp = int(time.time() * 1000) & ((1 << 48) - 1)
    bits = (stamp << 80) | (7 << 76) | (secrets.randbits(12) << 64)
    return str(uuid.UUID(int=bits | (2 << 62) | secrets.randbits(62)))


def derived(kind: str, key) -> str:
    return f"{kind}-{uuid.uuid5(NAMESPACE, f'{kind}:{key}')}"


def revision(event: dict) -> str:
    if event.get("_revision"):
        return event["_revision"]
    if event.get("mac") and event.get("id"):
        return event["id"]
    from khipu.routing import _id_salt

    body = {k: v for k, v in event.items() if not k.startswith("_")}
    return body_hash(body, _id_salt(operations=True))


def legacy_mac(event: dict) -> str:
    """Stable origin for untagged history; a fold supplies its log's origin."""
    return (event.get("legacy_mac") or event.get("_legacy_mac") or event.get("mac")
            or derived("mac", event.get("id") or revision(event)))


def entity(event: dict) -> str | None:
    if event.get("entity"):
        return event["entity"]
    kind = event.get("type", "")
    if kind == "rule_add":
        return derived("rule", f"{legacy_mac(event)}:{event.get('n')}")
    if kind in RULE_CHANGES:
        target = event.get("target") or ""
        return target if target.startswith("rule-") else derived("rule", f"{legacy_mac(event)}:{event.get('n')}")
    if kind == "hold":
        return derived("hold", event.get("id") or revision(event))
    if kind == "hold_end":
        target = event.get("target", "")
        return target if target.startswith("hold-") else derived("hold", target)
    if kind.startswith("note_"):
        return derived("note", "owner")
    if kind.startswith("priority_"):
        return derived("priority", "owner")
    if kind in ("pause", "lift"):
        return derived("pause", event.get("account"))
    if kind == "definition_set":
        value = event.get("value") or {}
        key = value.get("id") or f"{event.get('mac')}:{value.get('runner')}:{value.get('lane')}"
        return derived("definition", f"{event.get('definition')}:{key}")
    return None


def hlc_key(event: dict) -> tuple:
    stamp = event["hlc"]
    return stamp["wall"], stamp["counter"], stamp["mac"], revision(event)


def body_hash(event: dict, salt: bytes) -> str:
    body = {k: v for k, v in event.items() if not k.startswith("_") and k != "op_body"}
    return hmac.new(salt, json.dumps(body, sort_keys=True, ensure_ascii=True).encode(), hashlib.sha256).hexdigest()
