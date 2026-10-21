"""Append-only routing exchange. Network access exists only behind the live switch.

The local log remains authoritative for its private fields. remote.jsonl stores
projected events and hub receipts, never a rewritten copy of events.jsonl.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import tempfile
from datetime import timedelta

from khipu import routing

def _stamp(dt):
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


EPOCH = "1970-01-01T00:00:00Z"
POLL_SECONDS = 30


def _path(name):
    return routing.routing_dir() / name


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".replication-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
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


def _state():
    try:
        state = json.loads(_path("replication.json").read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    with routing._lock():
        _atomic(_path("replication.json"), json.dumps(state, sort_keys=True))


@contextlib.contextmanager
def _sync_lock():
    path = _path("replication.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def fingerprint():
    return [routing._stat(_path("remote.jsonl")), routing._stat(_path("replication.json"))]


def cache_key():
    return tuple(tuple(value or ()) for value in fingerprint())


def remote_events():
    try:
        data = _path("remote.jsonl").read_bytes()
    except FileNotFoundError:
        return []
    rows = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        row = routing._line_event(line)
        if row is None or not row.get("id"):
            raise ValueError("invalid routing replica")
        rows.append(row)
    return rows


def merge(local):
    """Private local fields win; only the hub assigns display numbers."""
    rows = {r["id"]: dict(r, remote=True) for r in remote_events()}
    for row in local:
        receipt = rows.get(row.get("id"), {})
        merged = dict(row)
        if isinstance(receipt.get("hub_order"), int):
            merged.update(hub_order=receipt["hub_order"], number_pending=False)
        rows[row.get("id") or ("local", len(rows))] = merged
    return list(rows.values())


def _store(receipts):
    with routing._lock():
        _store_locked(receipts)


def _store_locked(receipts):
    rows = {r["id"]: r for r in remote_events()}
    changed = False
    for receipt in receipts:
        payload = receipt["payload"]
        order = receipt["hub_order"]
        if (not isinstance(payload, dict) or not payload.get("id")
                or payload["id"] != receipt["event_id"]
                or not isinstance(order, int) or isinstance(order, bool) or order <= 0):
            raise ValueError("invalid hub receipt")
        checked = routing._checked(dict(payload, hub_order=order, number_pending=False))
        if checked is None:
            raise ValueError("invalid hub event")
        if payload["id"] not in rows:
            rows[payload["id"]] = checked
            changed = True
        elif "hub_order" not in rows[payload["id"]]:
            rows[payload["id"]].update(hub_order=order, number_pending=False)
            changed = True
    if changed:
        _atomic(_path("remote.jsonl"), "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows.values()))


class PostgresHub:
    """Each upload commits before its receipt is returned.

    The advisory lock serializes acceptance, not merely sequence allocation.
    BIGSERIAL is not a uniqueness constraint and is never a conflict target.
    Pull pagination uses (hub timestamp, primary key), not a unique hub_order.
    """
    def upload(self, mac, payload):
        from khipu import db

        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(1263030864)")
                cur.execute("INSERT INTO routing_events(event_id, sender_mac, payload, received_at) "
                            "VALUES (%s, %s, %s::jsonb, clock_timestamp()) "
                            "ON CONFLICT (event_id) DO NOTHING",
                            (payload["id"], mac, json.dumps(payload)))
                cur.execute("SELECT event_id, hub_order, payload, received_at FROM routing_events WHERE event_id = %s",
                            (payload["id"],))
                row = cur.fetchone()
                cur.execute("INSERT INTO routing_sources(mac, last_hub_order, last_event_id, event_count, last_seen_at) "
                            "SELECT %s, coalesce(max(hub_order), 0), %s, count(*), clock_timestamp() FROM routing_events WHERE sender_mac = %s "
                            "ON CONFLICT (mac) DO UPDATE SET last_hub_order = EXCLUDED.last_hub_order, "
                            "last_event_id = EXCLUDED.last_event_id, event_count = EXCLUDED.event_count, "
                            "last_seen_at = EXCLUDED.last_seen_at", (mac, payload["id"], mac))
                receipt = self._receipt(row)
        return receipt

    @staticmethod
    def _receipt(row):
        return dict(event_id=row[0], hub_order=row[1], payload=row[2], received_at=_stamp(row[3]))

    def pull(self, after, until, limit):
        from khipu import db

        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT clock_timestamp()")
                now = _stamp(cur.fetchone()[0])
                upper = until or now
                cur.execute("SELECT event_id, hub_order, payload, received_at FROM routing_events "
                            "WHERE (received_at, event_id) > (%s::timestamptz, %s) "
                            "AND received_at <= %s::timestamptz ORDER BY received_at, event_id LIMIT %s",
                            (*after, upper, limit + 1))
                rows = cur.fetchall()
                cur.execute("SELECT mac, last_hub_order, last_event_id, event_count, last_seen_at FROM routing_sources")
                sources = [dict(mac=r[0], last_hub_order=r[1], last_event_id=r[2], event_count=r[3],
                                last_seen_at=routing._iso(r[4])) for r in cur.fetchall()]
        return dict(rows=[self._receipt(r) for r in rows[:limit]], complete=len(rows) <= limit,
                    hub_now=now, until=upper, sources=sources)


def sync(*, hub=None, limit=500, max_pages=20):
    """One bounded exchange; a partial scan resumes without a caught-up claim.

    Upload acknowledgements are saved only after their receipts are durable.
    Replaying after either write fails is safe because event_id is the hub key.
    Errors are closed codes: a connection exception may contain credentials.
    """
    if not routing._replication_live():
        return dict(ok=True, disabled=True, caught_up=False)
    if limit <= 0 or max_pages <= 0:
        raise ValueError("replication limits must be positive")
    from khipu.hub_snapshot import FRESHNESS_OVERLAP_S

    state = None
    try:
        with _sync_lock():
            with routing._lock():
                mac = routing._mac_locked()
                local, _bad = routing._read_event_lines()
                # Permanent historical bindings, including moves, matter here.
                bindings, _forwards, _aliases = routing.account_identity.state(
                    routing._in_write_order(local), routing.accounts())
            state = _state()
            if state.get("mac") != mac:
                state = {"mac": mac}
            hub = hub if hub is not None else PostgresHub()
            uploaded = state.setdefault("uploaded", {})
            deferred = state.setdefault("deferred", {})
            skipped = 0
            ordered = sorted(enumerate(local, 1), key=lambda pair: (pair[1].get("mac", ""), pair[1].get("seq", pair[0]), pair[0]))
            for position, event in ordered:
                origin = event.get("mac") or "legacy"
                seq = event.get("seq", position)
                if seq <= uploaded.get(origin, 0) and event.get("id") not in deferred:
                    continue
                projected = routing.replication_payload([event], bindings=bindings)
                # A rejected identifier is not a new id; it must never upload.
                if projected and projected[0].get("id"):
                    receipt = hub.upload(mac, projected[0])
                    if receipt["event_id"] != projected[0]["id"]:
                        raise ValueError("wrong upload receipt")
                    _store([receipt])
                    deferred.pop(event["id"], None)
                elif not projected and event.get("id"):
                    deferred[event["id"]] = event["type"]
                else:
                    skipped += 1
                uploaded[origin] = max(seq, uploaded.get(origin, 0))
                _save(state)
            for _ in range(max_pages):
                after = state.get("scan_after") or [state.get("pull_cursor", EPOCH), ""]
                result = hub.pull(after, state.get("scan_until"), limit)
                upper = routing._parse(result["until"])
                hub_now = routing._parse(result["hub_now"])
                if upper is None or hub_now is None:
                    raise ValueError("invalid hub clock")
                rows = result["rows"]
                _store(rows)
                state["sources"] = result["sources"]
                state["hub_offset_seconds"] = (hub_now - routing._now()).total_seconds()
                if result["complete"]:
                    state["pull_cursor"] = _stamp(upper - timedelta(seconds=FRESHNESS_OVERLAP_S))
                    state["caught_up_at"] = routing._iso(upper)
                    state["checked_local_at"] = routing._iso(routing._now())
                    state.pop("scan_after", None)
                    state.pop("scan_until", None)
                    state.pop("error", None)
                    _save(state)
                    return dict(ok=True, caught_up=True, skipped=skipped,
                                deferred_kinds=sorted(set(deferred.values())))
                if not rows:
                    raise ValueError("pull made no progress")
                state["scan_after"] = [rows[-1]["received_at"], rows[-1]["event_id"]]
                state["scan_until"] = result["until"]
                _save(state)
            return dict(ok=True, caught_up=False, partial=True)
    except BlockingIOError:
        return dict(ok=False, caught_up=False, error="busy")
    except Exception:
        # Keep cursors as last persisted: the failed operation may have committed.
        return dict(ok=False, caught_up=False, error="replication_unavailable")


def restore():
    """Explicit local recovery: a fresh sender, with the old log left intact."""
    with _sync_lock(), routing._lock():
        mac = secrets.token_hex(16)
        _atomic(_path("mac-id"), mac + "\n")
        _atomic(_path("remote.jsonl"), "")
        _save({"mac": mac})
    return {"mac": mac}


def status(now=None):
    state = _state()
    now = now or routing._now()
    # A resumed scan may finish well after its snapshot. Age that snapshot,
    # using the measured hub offset, rather than the time its last page arrived.
    caught = routing._parse(state.get("caught_up_at"))
    offset = state.get("hub_offset_seconds", 0)
    age = max(0, (now + timedelta(seconds=offset) - caught).total_seconds()) if caught else None
    return dict(caught_up_at=state.get("caught_up_at"), age_seconds=age,
                stale=age is None or age > 600, sources=state.get("sources", []),
                hub_offset_seconds=state.get("hub_offset_seconds"),
                deferred_kinds=sorted(set(state.get("deferred", {}).values())))


def age_line(now=None):
    if not routing._replication_live():
        return None
    age = status(now)["age_seconds"]
    return ("routing from other Macs has not been received yet" if age is None else
            f"routing from other Macs is {int(age // 60)} min old")


def proposal(event, cfg):
    """A withheld proposal is visible as an overlay, never as invented words."""
    if not event.get("remote") or not event.get("pending"):
        return event
    if cfg.get("routing_proposal_text_between_macs", True) and "text" in event:
        return event
    out = dict(event, proposal_withheld=True)
    if event["type"] in ("rule_add", "rule_edit"):
        out["text"] = "pending rule proposal (text not shared between Macs)"
        out.pop("prior_text", None)
    elif event["type"] == "priority_set":
        out["entries"] = [dict(e, role="pending priority proposal (text not shared between Macs)")
                          for e in event.get("entries", [])]
    return out


def hub_clock(now):
    offset = _state().get("hub_offset_seconds") if routing._replication_live() else None
    return now + timedelta(seconds=offset) if isinstance(offset, (int, float)) else now


def local_deadline(body):
    """The scheduler uses this Mac's clock; observation deadlines stay in UTC."""
    offset = _state().get("hub_offset_seconds") if routing._replication_live() else None
    deadline = routing._parse(body.get("valid_until"))
    if deadline is not None and isinstance(offset, (int, float)):
        body["valid_until"] = routing._iso(deadline - timedelta(seconds=offset))
