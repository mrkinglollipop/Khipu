"""Mac-only projection publisher and hub-backed gateway reads and receipts."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import tempfile
import contextlib
import uuid
from datetime import timedelta

from khipu import routing

UNAVAILABLE = "routing copy hub or durable delivery ledger unavailable; acknowledgement not confirmed; retry the same batch"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# Closed at every nested object, including pace. No container configuration or
# login inventory is needed to validate a projection already scrubbed on a Mac.
FIELDS = {
    "root": set("available about record version epoch folded_at valid_until accounts holds rules note priority".split()),
    "accounts": set("id label state basis until line witness models pace".split()),
    "witness": {"source", "at"}, "models": {"model", "basis", "until", "at"},
    "holds": set("id scope reason until owner at line text".split()),
    "scope": {"kind", "account"},
    "rules": set("id n text pending confirmed revision".split()),
    "note": {"text", "at"},
    "priority": set("entries until pending revision line".split()),
    "entries": {"account", "label", "role", "until", "level"},
    "level": {"claim", "as_of", "until"},
    "pace": set(routing._GATEWAY_PACE_FIELDS) | {"plan", "share_cap", "day_cap"},
    "plan": set(routing._GATEWAY_PLAN_FIELDS),
    "share_cap": set(routing._GATEWAY_SHARE_FIELDS),
    "day_cap": set(routing._GATEWAY_DAY_FIELDS),
}


def validate(value, kind="root"):
    if isinstance(value, list):
        for item in value:
            validate(item, kind)
    elif isinstance(value, dict):
        if kind not in FIELDS or set(value) - FIELDS[kind]:
            raise ValueError("invalid closed gateway projection")
        for key, inner in value.items():
            validate(inner, key)
    if kind == "root":
        if not isinstance(value, dict) or value.get("available") is not True:
            raise ValueError("invalid gateway copy")
        if not isinstance(value.get("epoch"), str) or type(value.get("version")) is not int:
            raise ValueError("invalid gateway revision")


class PostgresHub:
    def publish(self, publisher, projection, stale_after):
        from khipu import db
        validate(projection)
        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO routing_gateway_copies "
                            "(publisher, projection, epoch, revision, folded_at, stale_after) "
                            "VALUES (%s, %s::jsonb, %s, %s, %s::timestamptz, %s) "
                            "ON CONFLICT (publisher) DO UPDATE SET projection=EXCLUDED.projection, "
                            "epoch=EXCLUDED.epoch, revision=EXCLUDED.revision, folded_at=EXCLUDED.folded_at, "
                            "stale_after=EXCLUDED.stale_after, published_at=clock_timestamp() "
                            "RETURNING published_at",
                            (publisher, json.dumps(projection), projection["epoch"], projection["version"],
                             projection["folded_at"], stale_after))
                published = routing._iso(cur.fetchone()[0])
        return published

    def delete(self, publisher):
        from khipu import db
        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM routing_gateway_copies WHERE publisher=%s", (publisher,))

    def newest(self):
        from khipu import db
        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT projection, published_at, stale_after FROM routing_gateway_copies "
                            "ORDER BY published_at DESC, publisher LIMIT 1")
                row = cur.fetchone()
        return dict(projection=row[0], published_at=routing._iso(row[1]), stale_after=row[2]) if row else None

    def health_rows(self):
        from khipu import db
        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT identity, state FROM routing_gateway_delivery")
                rows = []
                for identity, state in cur.fetchall():
                    reasons = []
                    if state.get("pending_points", 0) >= 3:
                        reasons.append("budget-stall")
                    if state.get("expired", 0) >= 2:
                        reasons.append("lease-expired-twice")
                    rows.append(dict(identity=identity, copy_epoch=state.get("copy_epoch"), copy_revision=state.get("copy_revision"),
                                     copy_revision_at=state.get("copy_revision_at"), at=state.get("last_delivery_point"),
                                     status="red" if reasons else "green", reasons=reasons, missing=[]))
                return rows

    def delivery(self, identity, body, ack, now):
        """Commit the complete response receipt before returning any batch or ack.

        One batch covers the complete selected projection. Its digest includes
        switches and account filters, so neither can acknowledge unseen content.
        """
        from khipu import db
        identity = _digest(identity)
        revision = _digest({k: v for k, v in body.items() if k not in {"as_of", "stale", "stale_message"}})
        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                # Serialize first insert as well as subsequent row updates.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (int(identity[:15], 16),))
                cur.execute("SELECT state FROM routing_gateway_delivery WHERE identity=%s FOR UPDATE", (identity,))
                row = cur.fetchone()
                state = row[0] if row else {}
                flight = state.get("flight") or {}
                if ack:
                    if ack == flight.get("batch") and routing._parse(flight["until"]) > now:
                        state["acked"] = flight["revision"]
                        state["acked_through"] = flight["version"]
                        state["batches"] = (state.get("batches", []) + [ack])[-128:]
                        state.pop("flight", None)
                        state.update(pending_points=0, expired=0)
                    elif ack not in state.get("batches", []):
                        cur.execute("SELECT 1 FROM routing_gateway_delivery WHERE identity <> %s "
                                    "AND (state->'flight'->>'batch'=%s OR state->'batches' ? %s) LIMIT 1",
                                    (identity, ack, ack))
                        if cur.fetchone():
                            return None, "foreign"
                flight = state.get("flight") or {}
                if state.get("acked") != revision:
                    if state.get("pending_revision") != revision:
                        state.update(pending_revision=revision, pending_points=0, expired=0)
                    state["pending_points"] = state.get("pending_points", 0) + 1
                    if flight.get("revision") == revision and routing._parse(flight["until"]) <= now:
                        state["expired"] = state.get("expired", 0) + 1
                    if flight.get("revision") != revision or routing._parse(flight["until"]) <= now:
                        flight = dict(batch=str(uuid.uuid4()), revision=revision, version=body["version"],
                                      until=routing._iso(now + timedelta(seconds=60)))
                        state["flight"] = flight
                    changes = {"batch": flight["batch"], "component": "mcp", "items": [
                        {"change": "copy@" + revision, "entity": "copy", "revision": revision}]}
                else:
                    changes = None
                state.update(copy_epoch=body["epoch"], copy_revision=body["version"],
                             copy_revision_at=body["folded_at"], last_delivery_point=routing._iso(now))
                cur.execute("INSERT INTO routing_gateway_delivery(identity, state) VALUES (%s, %s::jsonb) "
                            "ON CONFLICT(identity) DO UPDATE SET state=EXCLUDED.state", (identity, json.dumps(state)))
        return changes, dict(owed=0, in_flight=int(changes is not None), acked_through=state.get("acked_through"))


def _path():
    return routing.routing_dir() / "gateway-copy.json"


def status():
    try:
        value = json.loads(_path().read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".gateway-copy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def publish(*, force=False, now=None, hub=None):
    """Trailing debounce and bounded retry, polled independently of refolding."""
    now = now or routing._now()
    state = None
    try:
        # A separate process lock protects manual pushes versus daemon ticks;
        # no network call holds the routing fold lock.
        from khipu.routing_replication import _sync_lock
        # A disabled, never-published record must not create even a lock file.
        state = status()
        if not routing.settings()["routing_cloud_copy"] and not state.get("publisher"):
            return {"ok": True, "disabled": True}
        with _sync_lock():
            state = status()
            cfg = routing.settings()
            enabled = cfg["routing_cloud_copy"]
            if not enabled and not state.get("publisher"):
                return {"ok": True, "disabled": True}
            hub = hub or PostgresHub()
            if not enabled:
                hub.delete(state["publisher"])
                _save({})
                return {"ok": True, "disabled": True, "deleted": True}
            with routing._lock():
                publisher = hmac.new(
                    routing._id_salt(), ("gateway-copy:" + routing._mac_locked()).encode(), hashlib.sha256).hexdigest()
            if state.get("publisher") != publisher:
                state = {"publisher": publisher}
            from khipu import agent_view
            with agent_view.pull_projection():
                view = routing.read_view(now)
                keys = [a["key"] for a in view.get("accounts") or []]
                agent_view._table(keys).owner = []
                projection = agent_view.scrub_strict(routing.gateway_projection(view, now=now), keep=(), extra_keys=keys)
            validate(projection)
            digest = _digest([projection["epoch"], projection["version"],
                              cfg["routing_cloud_copy_rules"], cfg["routing_cloud_copy_hold_words"],
                              cfg["routing_cloud_copy_stale_after"]])
            if state.get("pending") != digest:
                state.update(pending=digest, changed_at=routing._iso(now), retry_at=None)
            _save(state)  # Record identity before a hub commit whose response may be lost.
            if not force:
                if state.get("retry_at") and routing._parse(state["retry_at"]) > now:
                    return {"ok": False, "error": "copy_retry_pending"}
                if (now - routing._parse(state["changed_at"])).total_seconds() < cfg["routing_cloud_copy_debounce"]:
                    return {"ok": True, "pending": True}
                heartbeat = min(60, cfg["routing_cloud_copy_stale_after"] / 2) if cfg["routing_cloud_copy_stale_after"] else 60
                if state.get("digest") == digest and state.get("published_at") and (
                        now - routing._parse(state["published_at"])).total_seconds() < heartbeat:
                    return {"ok": True, "unchanged": True}
            published_at = hub.publish(publisher, projection, cfg["routing_cloud_copy_stale_after"])
            state.update(digest=digest, published_at=published_at, epoch=projection["epoch"],
                         revision=projection["version"], folded_at=projection["folded_at"], error=None,
                         retry_at=None, failures=0)
            state["gateway_rows"] = hub.health_rows()
            _save(state)
            return {"ok": True, "published": True, "epoch": state["epoch"], "revision": state["revision"]}
    except Exception:
        # Exceptions can contain DSNs or local paths; expose fixed codes only.
        if state is not None:
            state["error"] = "copy_publish_failed"
            state["failures"] = min(6, state.get("failures", 0) + 1)
            state["retry_at"] = routing._iso(now + timedelta(seconds=min(300, 5 * 2 ** state["failures"])))
            try:
                _save(state)
            except OSError:
                pass
        return {"ok": False, "error": "copy_publish_failed"}


def answer(*, identity, account=None, ack="", now=None, hub=None):
    now = now or routing._now()
    hub = hub or PostgresHub()
    row = hub.newest()
    if row is None:
        return None
    body = copy.deepcopy(row["projection"])
    validate(body)
    if account:
        selected = [a for a in body["accounts"] if account in (a.get("id"), a.get("label"))]
        if not selected:
            raise ValueError("unknown gateway account")
        ids = {a["id"] for a in selected}
        body["accounts"] = selected
        body["holds"] = [h for h in body["holds"] if h["scope"].get("kind") == "all" or h["scope"].get("account") in ids]
    as_of = routing._parse(row["published_at"])
    stale = bool(row["stale_after"] and (now - as_of).total_seconds() > row["stale_after"])
    body.update(as_of=row["published_at"], stale=stale)
    if stale:
        body["stale_message"] = "this Mac has not reported since " + as_of.strftime("%H:%MZ")
    if identity is None:
        body.update(delivery="untracked: pass session_id", routing_changes={"batch": None, "component": "mcp", "items": []})
    else:
        changes, delivery = hub.delivery(identity, body, ack, now)
        if delivery == "foreign":
            return {"foreign_ack": True}
        body["delivery"] = delivery
        if changes:
            body["routing_changes"] = changes
    return body
