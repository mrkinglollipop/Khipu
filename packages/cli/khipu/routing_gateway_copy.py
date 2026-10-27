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
    "entries": {"account", "label", "role", "until", "level", "exceptions"},
    "exceptions": {"text", "until"},
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
        revision = _digest({k: v for k, v in body.items()
                            if k not in {"as_of", "stale", "stale_message", "folded_at"}})
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


# Cleanup of a switched-off copy: one invariant.
#
#   A target leaves the pending set only after its own delete is confirmed;
#   nothing else ever removes or re-adds a target. Give-up, interrupted and
#   full are pure functions of the pending set and the counters.
#
# The pending set (_pending) is every id this Mac may still have on the hub:
# the evidence ids (journal, publishers, publisher: written before any
# upload) plus the speculative targets (cleanup_publishers: recorded before
# any delete). _confirm_deleted is the only code that takes an id out, and
# only after hub.delete(id) returned. A speculative current id is admitted
# at most once per cleanup (cleanup_admitted), only while the speculative
# list has room (_CLEANUP_MAX_TARGETS). An unconfigured or unreachable hub
# never removes anything: it waits, or counts a failure.
#
# Counters: failures (every failed try; backoff 5 s doubling, capped at
# 300 s) and cleanup_failures (failed deletes; the give-up at 6).
#
#   fresh        nothing pending, or counters at zero.
#   retrying     pending, error set, cleanup_failures < 6: automatic ticks
#                retry on backoff.
#   interrupted  speculative targets pending, no error and no counts: a first
#                cleanup whose outcome was lost. Never retried automatically;
#                shown; the owner's forced retry runs it.
#   stopped      cleanup_failures >= 6 and not cleaned: automatic ticks never
#                touch the hub; only the owner's forced retry goes on.
#   full         speculative list at its bound: shown; new ids wait.
#   cleaned      "disabled": set only when the pending set is empty.
#
#   _on_failure(cleanup=False)  publish failed: failures+1, budget untouched.
#   _on_failure(cleanup=True)   delete failed: both +1, ends any cleaned marker.
#   _on_published               a new publication: back to fresh, so a later
#                               switch-off gets a whole cleanup budget again.
#   _on_cleaned                 the pending set is empty: back to fresh.
#   _load (legacy)              state from before cleanup_failures existed kept
#                               one shared count; a recorded failed cleanup
#                               (cleanup_attempted + error, not cleaned) reads
#                               that count as cleanup_failures.
#
# Known limits (both only repeat an idempotent DELETE):
#   - cleanup_admitted keeps the last 64 admitted ids; an older id that became
#     the current identity again would be admitted, and deleted, once more.
#   - _confirm_deleted writes the state file, then the journal; a crash between
#     the two leaves the id in the journal, so it is deleted once more.
_COUNTERS = ("error", "failures", "cleanup_failures", "retry_at")


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _load():
    try:
        value = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    if "cleanup_failures" not in value:
        legacy = (value.get("cleanup_attempted") is True and value.get("error")
                  and value.get("disabled") is not True)
        value["cleanup_failures"] = _count(value.get("failures")) if legacy else 0
    return value


def _counters(state):
    """The counters an attempt marker carries forward."""
    return {key: state[key] for key in ("failures", "cleanup_failures") if _count(state.get(key))}


def _on_failure(state, now, *, cleanup):
    state["error"] = "copy_publish_failed"
    state["failures"] = _count(state.get("failures")) + 1
    state["cleanup_failures"] = _count(state.get("cleanup_failures")) + int(cleanup)
    if cleanup:
        state.pop("disabled", None)
    state["retry_at"] = routing._iso(now + timedelta(seconds=min(300, 5 * 2 ** min(state["failures"], 6))))
    return state


def _on_published(state):
    state.update(error=None, retry_at=None, failures=0, cleanup_failures=0)
    return state


def _on_cleaned(state):
    for key in _COUNTERS:
        state.pop(key, None)
    state["disabled"] = True
    return state


GAVE_UP_WORDS = ("Routing copy cleanup stopped after 6 failed tries; a copy may still be on the hub. "
                 "Retry it from Settings.")


def gave_up(state):
    """The stopped state: automatic cleanup of a switched-off copy hit its cap.

    Only _on_published and _on_cleaned leave it; both zero the counter."""
    return bool(not _cleaned(state) and _count(state.get("cleanup_failures")) >= _CLEANUP_MAX_FAILURES)


INTERRUPTED_WORDS = ("Routing copy cleanup was interrupted; a copy may still be on the hub. "
                     "Retry it from Settings.")


FULL_WORDS = ("Too many unresolved routing copy cleanups; copies may still be on the hub. "
              "Retry cleanup from Settings.")


def cleanup_full(state):
    """The pending target list is at its bound: new speculative ids wait."""
    return bool(not _cleaned(state) and len(_cleanup_targets(state)) >= _CLEANUP_MAX_TARGETS)


def interrupted(state):
    """A cleanup was attempted, its outcome is unknown, and it is not cleaned."""
    return bool(not _cleaned(state) and _cleanup_targets(state)
                and not state.get("error") and not _count(state.get("cleanup_failures")))


def status():
    value = _load()
    try:
        off = not routing.settings()["routing_cloud_copy"]
    except Exception:
        off = False
    if off and gave_up(value):
        value = dict(value, cleanup_gave_up=True, cleanup_gave_up_words=GAVE_UP_WORDS)
    elif off and interrupted(value):
        value = dict(value, cleanup_interrupted=True, cleanup_interrupted_words=INTERRUPTED_WORDS)
    if off and cleanup_full(value):
        value = dict(value, cleanup_full=True, cleanup_full_words=FULL_WORDS)
    return value


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


def _cleanup_targets(state):
    """Ids a speculative cleanup recorded before its delete. Each stays a
    target until a cleanup naming it succeeds; none is ever replaced."""
    candidates = [state.get("cleanup_publisher")]
    if isinstance(state.get("cleanup_publishers"), list):
        candidates.extend(state["cleanup_publishers"])
    return {value for value in candidates if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)}


def _failed(state, now, cleanup=False):
    # Exceptions can contain DSNs or local paths; expose fixed codes only.
    if state is not None:
        _on_failure(state, now, cleanup=cleanup)
        try:
            _save(state)
        except OSError:
            pass
    return {"ok": False, "error": "copy_publish_failed"}


_HUB_CONFIGURED_TTL_SECONDS = 300
_CLEANUP_MAX_FAILURES = 6
_CLEANUP_MAX_TARGETS = 16
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
    """Whether a switched-off tick may touch the hub. Never writes state."""
    if gave_up(state) and not force:
        # Automatic retries stop for good; only the owner's forced retry goes on.
        return {"ok": False, "error": "copy_publish_failed"}
    if _cleaned(state):
        return {"ok": True, "disabled": True}
    evidence, targets = _publishers(state), _cleanup_targets(state)
    if not evidence:
        if state.get("cleanup_attempted") is True or targets:
            if not targets and _publisher(create=False) is None:
                # Nothing is pending. The owner's retry goes on to _cleanup,
                # which persists the cleaned transition (no hub call), so the
                # counters and the warning clear.
                return None if force else {"ok": True, "disabled": True}
            recorded = bool(state.get("error") or _count(state.get("cleanup_failures")))
            if not recorded and not force:
                # A first cleanup whose outcome was lost: shown as interrupted.
                return {"ok": True, "disabled": True}
            if not _hub_configured_cached(now):
                return {"ok": False, "error": "cleanup_waiting_for_hub"}
        else:
            # A recomputable id is not evidence this Mac ever published:
            # with no hub there is nothing to try, and nothing is recorded.
            if not _hub_configured_cached(now) or _publisher(create=False) is None:
                return {"ok": True, "disabled": True}
    retry = routing._parse(state.get("retry_at"))
    if not force and retry and retry > now:
        return {"ok": False, "error": "copy_retry_pending"}
    return None


def _ids(values):
    return {v for v in values if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)}


def _journal():
    try:
        value = json.loads(_publishers_path().read_text())
    except (OSError, ValueError):
        return []
    return sorted(_ids(value)) if isinstance(value, list) else []


def _pending(state):
    return _publishers(state) | _cleanup_targets(state)


def _cleaned(state):
    """Cleaned: marked so by _on_cleaned and nothing pending since."""
    return state.get("disabled") is True and not state.get("error") and not _pending(state)


def _confirm_deleted(state, target):
    """The only way out of the pending set: after hub.delete(target) returned.
    Each removal is its own atomic replace, so a crash neither loses an
    unresolved id nor brings back a deleted one."""
    state["cleanup_publishers"] = sorted(_cleanup_targets(state) - {target})
    state.pop("cleanup_publisher", None)
    state["publishers"] = sorted(_ids(state.get("publishers") or []) - {target})
    if state.get("publisher") == target:
        state.pop("publisher")
    _save(state)
    journal = _journal()
    if target in journal:
        _save([t for t in journal if t != target], path=_publishers_path())


def _cleanup(state, hub):
    """Delete every pending target; record each confirmed delete. Mutates
    ``state`` in place, so a failure records its counts on the same dict."""
    publisher = _publisher(create=False)
    admitted = [a for a in state.get("cleanup_admitted") or [] if a in _ids([a])]
    if not _publishers(state):
        # Speculative: the current id is a guess, admitted once per cleanup.
        targets = _cleanup_targets(state)
        if publisher and publisher not in admitted and len(targets) < _CLEANUP_MAX_TARGETS:
            targets.add(publisher)
            admitted.append(publisher)
        # Commit the targets before remote I/O.
        marker = dict(cleanup_attempted=True, cleanup_publishers=sorted(targets),
                      cleanup_admitted=admitted[-64:], **_counters(state))
        state.clear()
        state.update(marker)
        _save(state)
    else:
        state["publishers"] = sorted(_publishers(state))
        _save(state)
    hub = hub or PostgresHub()
    failed = None
    for target in sorted(_pending(state)):
        try:
            hub.delete(target)
        except Exception as exc:
            failed = failed or exc
            continue
        _confirm_deleted(state, target)
    if failed is not None:
        raise failed
    if _pending(state):
        raise RuntimeError("pending targets remain")
    cleaned = _on_cleaned(dict(cleanup_attempted=True, cleanup_publishers=[], cleanup_admitted=admitted[-64:]))
    state.clear()
    state.update(cleaned)
    _save(state)
    return {"ok": True, "disabled": True, "deleted": True}


def publish(*, force=False, now=None, hub=None, cleanup_only=False):
    """Trailing debounce and bounded retry, polled independently of refolding."""
    now = now or routing._now()
    state, acquired, attempting_lock, cleanup = None, False, False, False
    try:
        from khipu.routing_replication import _sync_lock
        state = _load()
        cleanup = not routing.settings()["routing_cloud_copy"]
        if cleanup_only and not cleanup:
            # The owner's cleanup retry never publishes.
            return {"ok": False, "error": "copy_is_on"}
        if not routing.settings()["routing_cloud_copy"]:
            skipped = _disabled_skip(state, now, force=force)
            if skipped:
                return skipped
        attempting_lock = True
        with _sync_lock():
            acquired, attempting_lock = True, False
            state = _load()
            cfg = routing.settings()
            enabled = cfg["routing_cloud_copy"]
            cleanup = not enabled
            if cleanup_only and enabled:
                return {"ok": False, "error": "copy_is_on"}
            if not enabled:
                skipped = _disabled_skip(state, now, force=force)
                if skipped:
                    return skipped
            if not enabled:
                return _cleanup(state, hub)
            publisher = _publisher(create=True)
            known = _publishers(state)
            # Pending speculative targets become journaled targets here, so
            # the journal is durable before the marker fields are dropped.
            known |= _cleanup_targets(state)
            if publisher:
                known.add(publisher)
            if not known:
                return {"ok": True, "disabled": True}
            used = sorted(known)
            # Keep the journal independently of replaceable publication state.
            # Both ids are durable before a delete or upload can commit remotely.
            _save(used, path=_publishers_path())
            state["publishers"] = used
            _save(state)
            hub = hub or PostgresHub()
            if state.get("publisher") != publisher:
                for previous in used:
                    if previous != publisher:
                        hub.delete(previous)
                # A new identity keeps the budget: only a publication or a
                # finished cleanup starts a fresh one.
                state = dict(publisher=publisher, publishers=used, **_counters(state))
                _save(state)
            state.pop("disabled", None)
            state.pop("cleanup_attempted", None)
            state.pop("cleanup_publisher", None)
            state.pop("cleanup_publishers", None)
            state.pop("cleanup_admitted", None)
            from khipu import agent_view
            with agent_view.pull_projection():
                view = routing.read_view(now)
                keys = [a["key"] for a in view.get("accounts") or []]
                agent_view._table(keys).owner = []
                projection = agent_view.scrub_strict(routing.gateway_projection(view, now=now), keep=(),
                                                      extra_keys=keys, cut_paths=True)
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
            # Settings writers hold the routing lock through their config
            # commit. Take it only after folding/scrubbing (never take the
            # fold lock under it), in the existing sync -> routing order.
            # A revocation that won this lock invalidates this projection;
            # one that follows the upload cannot acknowledge until its
            # commit has finished. Never overwrite the owner's choice.
            with routing._lock():
                current = routing.settings()
                sharing = ("routing_cloud_copy", "routing_cloud_copy_rules", "routing_cloud_copy_hold_words")
                if any(current[key] != cfg[key] for key in sharing):
                    return {"ok": True, "pending": True}
                published_at = hub.publish(publisher, projection, cfg["routing_cloud_copy_stale_after"])
            state.update(digest=digest, published_at=published_at, epoch=projection["epoch"],
                         revision=projection["version"], folded_at=projection["folded_at"])
            _on_published(state)
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
        return _failed(state, now, cleanup)
    except Exception:
        return _failed(state, now, cleanup)


# How priority_line writes an exception, and what can follow its closing ")":
# the next exception, a level or hold note, the next entry, or the line's end.
_LINE_EXCEPTION_OPEN = re.compile(r"\(exception(?: until [^()]{1,40}?)?: ")
_LINE_EXCEPTION_CLOSE = re.compile(r"\)(?= \((?:exception|level:|contradicts)|; \d+\. |\.|$)")


def _without_line_exceptions(line):
    """Cut the "(exception until T: words)" and "(exception: words)" forms the
    priority line is built with. The words may hold brackets, so an exception
    ends at the first ")" that a line part can follow; one with no such end is
    left alone rather than cutting what comes after it."""
    out, i = [], 0
    while i < len(line):
        start = _LINE_EXCEPTION_OPEN.search(line, i)
        end = _LINE_EXCEPTION_CLOSE.search(line, start.end()) if start else None
        if end is None:
            out.append(line[i:])
            break
        out.append(line[i:start.start()].rstrip())
        i = end.end()
    return "".join(out)


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
    # A priority exception is served only before its end, whenever the copy
    # was published: one that ended since is dropped at read time.
    entries = (body.get("priority") or {}).get("entries") or []
    structured = any("exceptions" in entry for entry in entries)
    for entry in entries:
        live = [x for x in entry.get("exceptions") or []
                if (routing._parse(x.get("until")) or now) > now]
        if live:
            entry["exceptions"] = live
        else:
            entry.pop("exceptions", None)
    as_of = routing._parse(row["published_at"])
    stale = bool(row["stale_after"] and (now - as_of).total_seconds() > row["stale_after"])
    # An older copy carried an exception only inside the priority line. Past
    # the copy's own valid_until that text may have ended, so it is cut from
    # the line; the priority itself is always kept.
    valid_until = routing._parse(body.get("valid_until"))
    priority = body.get("priority")
    if valid_until is not None and now >= valid_until and isinstance(priority, dict) and priority.get("line") \
            and not structured:
        priority["line"] = _without_line_exceptions(priority["line"])
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
