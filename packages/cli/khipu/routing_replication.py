"""Retained routing exchange. Network access exists only behind the live switch.

The local log remains authoritative for its private fields. remote.jsonl stores
projected events and hub receipts, never a rewritten copy of events.jsonl.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import threading
import os
import secrets
import tempfile
from datetime import datetime, timezone
from datetime import timedelta

from khipu import routing

def _stamp(dt):
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


EPOCH = "1970-01-01T00:00:00Z"
POLL_SECONDS = 30
UPLOAD_PER_TURN = 64


def _path(name):
    return routing.routing_dir() / name


_wrote = threading.local()


def _refreshing(call):
    """``call`` (a pull, a restore), then the routing view refolded and saved
    when it wrote a replication file (a fingerprint input of the view): the
    prompt that follows finds view.json current, with no fold of its own."""
    _wrote.flag = False
    try:
        return call()
    finally:
        if getattr(_wrote, "flag", False):
            _wrote.flag = False
            routing.refresh_view()


def _atomic(path, data):
    _wrote.flag = True
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


RETENTION_KINDS = frozenset({"usage_sample", "source_seen", "evidence", "observation", "observation_renew", "reset_check", "setting_set"})


def _event_order(row):
    """Owner timestamp first, hub order only as its deterministic tie-break."""
    at = routing._parse(row.get("written_at")) or datetime.min.replace(tzinfo=timezone.utc)
    return at, row.get("hub_order", 0) if isinstance(row.get("hub_order"), int) else 0


def settings_status():
    """Local/remote values retained for Your Macs; no value is silently picked
    while a first-enable snapshot disagrees."""
    from khipu.config import load_config

    remote = [e for e in remote_events() if e.get("type") == "setting_set"]
    newest = {}
    for row in remote:
        key = row.get("key")
        if routing.settings_syncable(key) and (key not in newest or _event_order(row) > _event_order(newest[key])):
            newest[key] = row
    config = load_config()
    differences = []
    for key, row in newest.items():
        try:
            remote_value = json.loads(row["value_json"])
            remote_value = routing._coerce(routing.SETTINGS[key][0], remote_value, key)
        except (ValueError, TypeError, KeyError):
            continue
        local_value = routing.settings(config)[key]
        if local_value != remote_value:
            differences.append({"key": key, "name": routing.SETTINGS[key][2], "local": local_value,
                                "remote": remote_value, "remote_snapshot": row.get("snapshot") is True,
                                "remote_at": row.get("written_at")})
    return {"enabled": routing._replication_live(), "differences": differences}


def _apply_remote_settings():
    """Apply later owner changes only.  Snapshot conflicts remain visible for
    the owner to choose; selected values write a newer local event normally."""
    if not routing._replication_live():
        return
    from khipu.config import mutate_config

    remote = [e for e in remote_events() if e.get("type") == "setting_set" and not e.get("snapshot")]
    newest = {}
    for row in remote:
        key = row.get("key")
        if routing.settings_syncable(key) and (key not in newest or _event_order(row) > _event_order(newest[key])):
            newest[key] = row
    if not newest:
        return
    with routing._lock(), mutate_config() as data:
        for key, row in newest.items():
            try:
                value = routing._coerce(routing.SETTINGS[key][0], json.loads(row["value_json"]), key)
            except (ValueError, TypeError, KeyError):
                continue
            current = routing.settings(data)[key]
            if current != value:
                if row.get("value_set"):
                    data[key] = value
                else:
                    data.pop(key, None)


def _remote_snapshot(data=None):
    if data is None:
        try:
            data = _path("remote.jsonl").read_bytes()
        except FileNotFoundError:
            return [], 0
    rows, retired = [], 0
    for index, line in enumerate(data.split(b"\n")):
        if not line.strip():
            continue
        header = json.loads(line) if index == 0 else None
        if isinstance(header, dict) and set(header) == {"retired_through_order"}:
            retired = header["retired_through_order"]
            if type(retired) is not int or retired < 0:
                raise ValueError("invalid routing replica retention")
            continue
        row = routing._line_event(line)
        if row is None or not row.get("id"):
            raise ValueError("invalid routing replica")
        rows.append(row)
    return rows, retired


def remote_events():
    return _remote_snapshot()[0]


def _remote_contents(rows, retired=0):
    header = json.dumps({"retired_through_order": retired}) + "\n" if retired else ""
    return header + "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def merge(local, *, remote=None):
    """Keep existing numbers; receipts number previously unnumbered rules."""
    rows = {r["id"]: dict(r, remote=True) for r in (remote_events() if remote is None else remote)}
    for row in local:
        receipt = rows.get(row.get("id"), {})
        merged = dict(row)
        if isinstance(receipt.get("hub_order"), int):
            merged["hub_order"] = receipt["hub_order"]
            if receipt.get("rule_number") is not None:
                merged["rule_number"] = receipt["rule_number"]
        rows[row.get("id") or ("local", len(rows))] = merged
    return list(rows.values())


def _store(receipts):
    with routing._lock():
        _store_locked(receipts)


def _store_locked(receipts):
    existing, retired = _remote_snapshot()
    rows = {r["id"]: r for r in existing}
    changed = False
    for receipt in receipts:
        payload = receipt["payload"]
        order = receipt["hub_order"]
        if (not isinstance(payload, dict) or not payload.get("id")
                or payload["id"] != receipt["event_id"]
                or not isinstance(order, int) or isinstance(order, bool) or order <= 0):
            raise ValueError("invalid hub receipt")
        number = receipt.get("rule_number")
        if number is not None and (type(number) is not int or not 0 < number <= routing._WHOLE_MOST):
            raise ValueError("invalid rule number")
        checked = routing._checked(dict(payload, hub_order=order,
                                        **({"rule_number": number} if number is not None else {})))
        if checked is None:
            raise ValueError("invalid hub event")
        from khipu import routing_compact

        if (order <= retired and payload["id"] not in rows and checked["type"] in RETENTION_KINDS
                and not routing_compact._owner(checked)):
            continue
        if payload["id"] not in rows:
            rows[payload["id"]] = checked
            changed = True
        elif "hub_order" not in rows[payload["id"]]:
            rows[payload["id"]].update(checked)
            changed = True
    if changed:
        _atomic(_path("remote.jsonl"), _remote_contents(rows.values(), retired))


class PostgresHub:
    """Each upload commits before its receipt is returned.

    The advisory lock serializes acceptance, not merely sequence allocation.
    BIGSERIAL is not a uniqueness constraint and is never a conflict target.
    Pull pagination uses (hub timestamp, primary key), not a unique hub_order.
    """
    def reserve_numbers(self, mac, floor):
        """Only a numeric high-water mark leaves id-less local history."""
        from khipu import db

        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(1263030864)")
                cur.execute("INSERT INTO routing_sources(mac, rule_number_floor) VALUES (%s, %s) "
                            "ON CONFLICT (mac) DO UPDATE SET rule_number_floor = "
                            "greatest(routing_sources.rule_number_floor, EXCLUDED.rule_number_floor)",
                            (mac, floor))

    def upload(self, mac, payload):
        from khipu import db

        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(1263030864)")
                # The same transaction lock orders both numbering and inserts.
                # Retrying an id returns its original receipt without allocating.
                cur.execute("SELECT event_id, hub_order, payload, received_at, rule_number "
                            "FROM routing_events WHERE event_id = %s", (payload["id"],))
                existing = cur.fetchone()
                if existing is not None:
                    return self._receipt(existing)
                number = routing._rule_number(payload) if payload["type"] == "rule_add" else None
                if payload["type"] == "rule_add" and number is None:
                    cur.execute("SELECT greatest("
                                "coalesce((SELECT max(rule_number_floor) FROM routing_sources), 0), "
                                "coalesce((SELECT max(rule_number) FROM routing_events), 0))")
                    number = cur.fetchone()[0] + 1
                    if number > routing._WHOLE_MOST:
                        raise ValueError("rule numbers exhausted")
                cur.execute("INSERT INTO routing_events(event_id, sender_mac, payload, received_at, rule_number) "
                            "VALUES (%s, %s, %s::jsonb, clock_timestamp(), %s) "
                            "ON CONFLICT (event_id) DO NOTHING",
                            (payload["id"], mac, json.dumps(payload), number))
                cur.execute("SELECT event_id, hub_order, payload, received_at, rule_number FROM routing_events WHERE event_id = %s",
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
        return dict(event_id=row[0], hub_order=row[1], payload=row[2], received_at=_stamp(row[3]), rule_number=row[4])

    def pull(self, after, until, limit):
        from khipu import db

        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT clock_timestamp()")
                now = _stamp(cur.fetchone()[0])
                upper = until or now
                cur.execute("SELECT event_id, hub_order, payload, received_at, rule_number FROM routing_events "
                            "WHERE (received_at, event_id) > (%s::timestamptz, %s) "
                            "AND received_at <= %s::timestamptz ORDER BY received_at, event_id LIMIT %s",
                            (*after, upper, limit + 1))
                rows = cur.fetchall()
                cur.execute("SELECT mac, last_hub_order, last_event_id, event_count, last_seen_at FROM routing_sources")
                sources = [dict(mac=r[0], last_hub_order=r[1], last_event_id=r[2], event_count=r[3],
                                last_seen_at=routing._iso(r[4])) for r in cur.fetchall()]
        return dict(rows=[self._receipt(r) for r in rows[:limit]], complete=len(rows) <= limit,
                    hub_now=now, until=upper, sources=sources)

    def pull_controls(self, after, limit):
        """The off-state poll reads only the singleton copy-control stream."""
        from khipu import db

        with db.connect(statement_timeout_s=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT event_id, hub_order, payload, received_at, rule_number FROM routing_events "
                            "WHERE payload ->> 'type' = 'settings_sync_switch' "
                            "AND (received_at, event_id) > (%s::timestamptz, %s) "
                            "ORDER BY received_at, event_id LIMIT %s", (*after, limit))
                rows = cur.fetchall()
        return {"rows": [self._receipt(r) for r in rows]}


def sync(*, hub=None, limit=500, max_pages=20):
    """One bounded exchange (``_sync``), then the view refolded if it wrote."""
    return _refreshing(lambda: _sync(hub=hub, limit=limit, max_pages=max_pages))


def _apply_remote_switch(rows):
    """The narrow off-state exception: one owner switch controls copy on all
    Macs. Older clients safely ignore this new payload kind."""
    rows = [r for r in rows if (r.get("payload") or {}).get("type") == "settings_sync_switch"]
    if not rows:
        return
    newest = max(rows, key=lambda r: _event_order(dict(r.get("payload") or {}, hub_order=r.get("hub_order"))))
    enabled = newest["payload"].get("enabled")
    if not isinstance(enabled, bool):
        return
    from khipu.config import mutate_config
    with routing._lock(), mutate_config() as data:
        data["routing_replication_live"] = enabled


def _control_sync(*, hub=None):
    """The periodic off-state control poll. It only uploads/pulls the tiny
    switch record; ordinary routing stays gated until that record says on."""
    try:
        with _sync_lock(), routing._lock():
            mac = routing._mac_locked()
            local, _bad = routing._read_event_lines()
        state = _state()
        hub = hub if hub is not None else PostgresHub()
        sent = set(state.get("control_uploaded", []))
        for event in local:
            if event.get("type") != "settings_sync_switch" or not event.get("id") or event["id"] in sent:
                continue
            payload = routing.replication_payload([event], bindings={})
            if payload:
                hub.upload(mac, payload[0])
                sent.add(event["id"])
        state["control_uploaded"] = sorted(sent)[-32:]
        cursor = state.get("control_cursor", [EPOCH, ""])
        # Test/downgrade hubs have only the old broad pull API. Re-read its
        # small synthetic window from zero so a same-timestamp late insert
        # cannot hide behind an event-id cursor; real hubs use the indexed
        # control-only query above.
        result = hub.pull_controls(cursor, 64) if hasattr(hub, "pull_controls") else hub.pull([EPOCH, ""], None, 64)
        rows = result.get("rows") or []
        _apply_remote_switch(rows)
        if rows:
            state["control_cursor"] = [rows[-1]["received_at"], rows[-1]["event_id"]]
        _save(state)
        return {"ok": True, "disabled": True, "caught_up": False}
    except BlockingIOError:
        # The periodic timer overlapping another sync is ordinary contention,
        # not a failed transport or a reason to alarm the owner.
        return {"ok": True, "caught_up": False, "busy": True}
    except Exception:
        return {"ok": False, "caught_up": False, "error": "replication_unavailable"}


def _sync(*, hub=None, limit=500, max_pages=20):
    """One bounded exchange; a partial scan resumes without a caught-up claim.

    Upload acknowledgements are saved only after their receipts are durable.
    Replaying after either write fails is safe because event_id is the hub key.
    Errors are closed codes: a connection exception may contain credentials.
    """
    if not routing._replication_live():
        return _control_sync(hub=hub)
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
            # Reserve even when this Mac has only id-less history and no upload.
            # Reserve all local numbers before sending a pending rule, regardless
            # of the log's order. Never transmit those legacy rows themselves.
            numbers = [routing._rule_number(e) for e in merge(local) if e.get("type") == "rule_add"]
            hub.reserve_numbers(mac, max((n for n in numbers if n is not None), default=0))
            uploaded = state.setdefault("uploaded", {})
            deferred = state.setdefault("deferred", {})
            skipped = 0
            sent_this_turn = 0
            local_pending = False
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
                sent_this_turn += 1
                # Uploading an old local backlog before ever pulling starves
                # every other Mac.  One bounded slice, then a pull, makes
                # progress fair and leaves the remaining local rows durable.
                if sent_this_turn >= UPLOAD_PER_TURN:
                    local_pending = any(
                        later.get("id") and later.get("seq", n) > uploaded.get(later.get("mac") or "legacy", 0)
                        for n, later in ordered[position:]
                    )
                    break
            for _ in range(max_pages):
                after = state.get("scan_after") or [state.get("pull_cursor", EPOCH), ""]
                result = hub.pull(after, state.get("scan_until"), limit)
                upper = routing._parse(result["until"])
                hub_now = routing._parse(result["hub_now"])
                if upper is None or hub_now is None:
                    raise ValueError("invalid hub clock")
                rows = result["rows"]
                _store(rows)
                _apply_remote_switch(rows)
                _apply_remote_settings()
                state["scan_order"] = max([state.get("scan_order", 0)] + [r["hub_order"] for r in rows])
                state["sources"] = result["sources"]
                state["hub_offset_seconds"] = (hub_now - routing._now()).total_seconds()
                if result["complete"]:
                    state["completed_order"] = max(state.get("completed_order", 0), state.pop("scan_order", 0))
                    state["pull_cursor"] = _stamp(upper - timedelta(seconds=FRESHNESS_OVERLAP_S))
                    state["caught_up_at"] = routing._iso(upper)
                    state["checked_local_at"] = routing._iso(routing._now())
                    state.pop("scan_after", None)
                    state.pop("scan_until", None)
                    state.pop("error", None)
                    _save(state)
                    return dict(ok=True, caught_up=not local_pending, local_pending=local_pending, skipped=skipped,
                                deferred_kinds=sorted(set(deferred.values())))
                if not rows:
                    raise ValueError("pull made no progress")
                state["scan_after"] = [rows[-1]["received_at"], rows[-1]["event_id"]]
                state["scan_until"] = result["until"]
                _save(state)
            return dict(ok=True, caught_up=False, partial=True)
    except BlockingIOError:
        return dict(ok=True, caught_up=False, busy=True)
    except Exception:
        # Keep cursors as last persisted: the failed operation may have committed.
        return dict(ok=False, caught_up=False, error="replication_unavailable")


def restore():
    """Explicit local recovery (``_restore``), then the view refolded."""
    return _refreshing(_restore)


def _restore():
    """Explicit local recovery: a fresh sender, with the old log left intact."""
    try:
        with _sync_lock(), routing._lock():
            mac = secrets.token_hex(16)
            _atomic(_path("mac-id"), mac + "\n")
            _atomic(_path("remote.jsonl"), "")
            _save({"mac": mac})
        return {"mac": mac}
    except BlockingIOError:
        return {"ok": False, "error": "busy"}


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
    # A rule proposal's words are its ``text``; a priority proposal's are
    # its entries' roles. Both follow the same switch.
    shared = "text" in event or (event.get("type") == "priority_set" and "entries" in event)
    if cfg.get("routing_proposal_text_between_macs", True) and shared:
        return event
    out = dict(event, proposal_withheld=True)
    out.pop("owner_request", None)
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
