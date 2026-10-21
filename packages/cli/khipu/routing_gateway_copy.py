"""Mac-only projection publisher and hub-backed gateway reads and receipts."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import tempfile
import contextlib
import uuid
from datetime import timedelta

from khipu import routing

DELIVERY_RETENTION_SECONDS = 7 * 24 * 3600
DELIVERY_PRUNE_LIMIT = 1000
HEALTH_ROW_LIMIT = 512
HEALTH_REFRESH_SECONDS = 60

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

    @staticmethod
    def _prune_delivery(cur, now):
        # A live lease wins over the age bound. SKIP LOCKED avoids waiting
        # behind a receipt transaction refreshing an otherwise old identity.
        cur.execute("DELETE FROM routing_gateway_delivery WHERE identity IN ("
                    "SELECT identity FROM routing_gateway_delivery "
                    "WHERE coalesce(state->>'last_delivery_point', '') < %s "
                    "AND coalesce(state->'flight'->>'until', '') <= %s "
                    "ORDER BY state->>'last_delivery_point', identity LIMIT %s FOR UPDATE SKIP LOCKED)",
                    (routing._iso(now - timedelta(seconds=DELIVERY_RETENTION_SECONDS)),
                     routing._iso(now), DELIVERY_PRUNE_LIMIT))

    def health_rows(self, *, now=None, live_after=None):
        from khipu import db
        now = now or routing._now()
        live_after = routing.settings()["routing_delivery_live_after"] if live_after is None else live_after
        window = min(live_after or DELIVERY_RETENTION_SECONDS, DELIVERY_RETENTION_SECONDS)
        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                self._prune_delivery(cur, now)
                # Transfer health fields only, never ack history or batch state.
                # One extra row lets the caller report truncation honestly.
                cur.execute("SELECT identity, state->>'copy_epoch', state->'copy_revision', "
                            "state->>'copy_revision_at', state->>'last_delivery_point', "
                            "state->'pending_points', state->'expired' FROM routing_gateway_delivery "
                            "WHERE state->>'last_delivery_point' >= %s AND state->>'last_delivery_point' <= %s "
                            "ORDER BY state->>'last_delivery_point' DESC, identity LIMIT %s",
                            (routing._iso(now - timedelta(seconds=window)), routing._iso(now), HEALTH_ROW_LIMIT + 1))
                rows = []
                for identity, epoch, revision, revision_at, at, pending, expired in cur.fetchall():
                    reasons = []
                    if (pending or 0) >= 3:
                        reasons.append("budget-stall")
                    if (expired or 0) >= 2:
                        reasons.append("lease-expired-twice")
                    rows.append(dict(identity=identity, copy_epoch=epoch, copy_revision=revision,
                                     copy_revision_at=revision_at, at=at,
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
                self._prune_delivery(cur, now)
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


def _save(state, *, path=None):
    path = path or _path()
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


def _publisher(*, create):
    if create:
        with routing._lock():
            salt, mac = routing._id_salt(), routing._mac_locked()
    else:
        # Revocation must reconstruct the id without minting a new identity.
        mac = routing._local_mac()
        try:
            salt = (routing.routing_dir() / "id-salt").read_bytes()
        except FileNotFoundError:
            return None
        if mac is None or len(salt) < 16:
            return None
    return hmac.new(salt, ("gateway-copy:" + mac).encode(), hashlib.sha256).hexdigest()


def _publishers_path():
    return routing.routing_dir() / "gateway-publishers.json"


def _publishers(state):
    try:
        journal = json.loads(_publishers_path().read_text())
    except (OSError, ValueError):
        journal = []
    candidates = [state.get("publisher")]
    for values in (journal, state.get("publishers")):
        if isinstance(values, list):
            candidates.extend(values)
    return {value for value in candidates if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)}


def _failed(state, now):
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


_HUB_CONFIGURED_TTL_SECONDS = 300
_hub_configured_cache = None


def _hub_configured_cached(now):
    """``routing._hub_configured()`` (two Keychain lookups) at most once
    per TTL window; the checker identity is part of the key so a replaced
    checker is never served a stale answer."""
    global _hub_configured_cache
    checker = routing._hub_configured
    if _hub_configured_cache is not None:
        cached_checker, checked_at, value = _hub_configured_cache
        if cached_checker is checker and checked_at <= now \
                and (now - checked_at).total_seconds() < _HUB_CONFIGURED_TTL_SECONDS:
            return value
    value = bool(checker())
    _hub_configured_cache = (checker, now, value)
    return value


def _disabled_skip(state, now, *, force):
    known = _publishers(state)
    if not known:
        # A recomputable id is not evidence this Mac ever published. A
        # cleanup that never ran (or whose outcome was lost) is not
        # retried, but one that failed against a configured hub is:
        # otherwise a row published before both local files were lost
        # would stay on the hub forever.
        if state.get("cleanup_attempted") is True:
            if state.get("disabled") is True or not state.get("error"):
                return {"ok": True, "disabled": True}
            retry = routing._parse(state.get("retry_at"))
            if not force and retry and retry > now:
                return {"ok": False, "error": "copy_retry_pending"}
            if _publisher(create=False) is None:
                return {"ok": True, "disabled": True}
            return None
        if not _hub_configured_cached(now):
            return {"ok": True, "disabled": True}
        if _publisher(create=False) is None:
            return {"ok": True, "disabled": True}
    retry = routing._parse(state.get("retry_at"))
    if not force and retry and retry > now:
        return {"ok": False, "error": "copy_retry_pending"}
    return None


def publish(*, force=False, now=None, hub=None):
    """Trailing debounce and bounded retry, polled independently of refolding."""
    now = now or routing._now()
    state, acquired, attempting_lock = None, False, False
    try:
        from khipu.routing_replication import _sync_lock
        state = status()
        if not routing.settings()["routing_cloud_copy"]:
            skipped = _disabled_skip(state, now, force=force)
            if skipped:
                return skipped
        attempting_lock = True
        with _sync_lock():
            acquired, attempting_lock = True, False
            state = status()
            cfg = routing.settings()
            enabled = cfg["routing_cloud_copy"]
            if not enabled:
                skipped = _disabled_skip(state, now, force=force)
                if skipped:
                    return skipped
            publisher = _publisher(create=enabled)
            known = _publishers(state)
            if not enabled and not known:
                # Commit the attempt marker before remote I/O. If the outcome
                # is lost, an idle Mac cannot retry this speculative deletion;
                # a recorded failure instead retries on its bounded backoff.
                failures = state.get("failures")
                state = dict(cleanup_attempted=True, cleanup_publisher=publisher)
                if isinstance(failures, int) and not isinstance(failures, bool) and failures > 0:
                    state["failures"] = failures
                _save(state)
                (hub or PostgresHub()).delete(publisher)
                state = dict(cleanup_attempted=True, cleanup_publisher=publisher, disabled=True)
                _save(state)
                return {"ok": True, "disabled": True, "deleted": True}
            if publisher:
                known.add(publisher)
            if not known:
                return {"ok": True, "disabled": True}
            used = sorted(known)
            if (not enabled and not force and state.get("disabled") and not state.get("error")
                    and state.get("publisher") == publisher and state.get("publishers") == used):
                return {"ok": True, "disabled": True}
            # Keep the journal independently of replaceable publication state.
            # Both ids are durable before a delete or upload can commit remotely.
            _save(used, path=_publishers_path())
            state["publishers"] = used
            _save(state)
            hub = hub or PostgresHub()
            if not enabled:
                for previous in used:
                    hub.delete(previous)
                _save(dict(publisher=publisher, publishers=used, disabled=True))
                return {"ok": True, "disabled": True, "deleted": True}
            if state.get("publisher") != publisher:
                for previous in used:
                    if previous != publisher:
                        hub.delete(previous)
                state = dict(publisher=publisher, publishers=used)
                _save(state)
            state.pop("disabled", None)
            state.pop("cleanup_attempted", None)
            state.pop("cleanup_publisher", None)
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
            refreshed = routing._parse(state.get("health_refreshed_at"))
            if force or refreshed is None or (now - refreshed).total_seconds() >= HEALTH_REFRESH_SECONDS:
                rows = hub.health_rows(now=now, live_after=cfg["routing_delivery_live_after"])
                state["gateway_rows"] = rows[:HEALTH_ROW_LIMIT]
                state["gateway_rows_truncated"] = len(rows) > HEALTH_ROW_LIMIT
                state["health_refreshed_at"] = routing._iso(now)
            _save(state)
            return {"ok": True, "published": True, "epoch": state["epoch"], "revision": state["revision"]}
    except BlockingIOError:
        if attempting_lock and not acquired:
            return {"ok": True, "skipped": "busy"}
        return _failed(state, now)
    except Exception:
        return _failed(state, now)


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
