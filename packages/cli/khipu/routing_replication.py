"""Retained routing exchange. Network access exists only behind the live switch.

The local log remains authoritative for its private fields. remote.jsonl stores
projected events and hub receipts, never a rewritten copy of events.jsonl.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import hashlib
import re
import threading
import time
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
ROUND_BUDGET_S = 240.0  # how long one sync keeps sending slices (each is saved durably) before the next tick takes over
CONTROL_EVERY_S = 270  # just under the 5-minute timer, so every tick looks


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


# What each sync rewrites whether or not it brought or sent anything: the pull
# position, the scan position, and the clock readings that say when the copy
# last looked (and the hub's "last seen" time for each other Mac, below). They
# say how current the copy is, not what the record says, so
# a view and the cached log read do not move with them. The hub's clock offset
# is kept to the minute: its jitter is sub-second, a real skew is not.
_BOOKKEEPING = frozenset({"pull_cursor", "caught_up_at", "checked_local_at", "scan_after", "scan_until",
                          "scan_order", "completed_order"})
_state_token_cache: dict[str, tuple[list, list]] = {}


def _state_token():
    """What ``replication.json`` says apart from its bookkeeping (a digest, as
    a one-item list), None when the file is missing. A file that cannot be
    read as a state counts by its stat, so damage is always a change."""
    path = _path("replication.json")
    stat = routing._stat(path)
    if stat is None:
        return None
    cached = _state_token_cache.get(str(path))
    if cached is not None and cached[0] == stat:
        return cached[1]
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError("not a state")
    except (OSError, ValueError):
        return list(stat)
    body = {k: v for k, v in state.items() if k not in _BOOKKEEPING}
    # Each pull lists the other Macs with when the hub last saw them: which
    # Macs, not when, is what the state says.
    if isinstance(body.get("sources"), list):
        body["sources"] = sorted(str(s.get("mac")) for s in body["sources"] if isinstance(s, dict))
    offset = body.get("hub_offset_seconds")
    if isinstance(offset, (int, float)) and not isinstance(offset, bool):
        body["hub_offset_seconds"] = round(offset / 60)
    token = [hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()]
    _state_token_cache[str(path)] = (stat, token)
    return token


def fingerprint():
    """The copy's two inputs to a fold: ``remote.jsonl`` (what other Macs sent,
    by stat) and what ``replication.json`` says (``_state_token``). A sync that
    brought nothing and sent nothing moves neither."""
    return [routing._stat(_path("remote.jsonl")), _state_token()]


def cache_key():
    return tuple(tuple(value or ()) for value in fingerprint())


RETENTION_KINDS = frozenset({"usage_sample", "source_seen", "evidence", "observation", "observation_renew", "reset_check"})


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
    return {"enabled": routing._replication_live(), "differences": differences,
            "requests": [r for r in remote_requests() if r["state"] != "pending"][:10],
            "waiting": [r for r in remote_requests() if r["state"] == "pending"][:10]}


def mac_label(mac):
    """What the owner calls a Mac, from his own names; never a path or address."""
    names = routing.settings().get("routing_mac_names") or {}
    name = names.get(mac) if isinstance(mac, str) else None
    return name if isinstance(name, str) and name.strip() else "your other Mac"


def _newest_setting_events():
    """The latest ``setting_set`` per key across this Mac's own lines and every
    Mac's received ones: owner time first, hub order only as the tie-break."""
    local = [e for e in routing._read_event_lines()[0] if e.get("type") == "setting_set"]
    newest = {}
    for row in merge(local):
        key = row.get("key")
        if row.get("type") == "setting_set" and routing.settings_syncable(key) \
                and (key not in newest or _event_order(row) > _event_order(newest[key])):
            newest[key] = row
    return newest


def _apply_remote_settings(state):
    """Apply the other Macs' later owner changes, once each. A change this
    Mac's owner made after them stays; a snapshot taken at first switch-on
    never overwrites (it is listed in ``settings_status`` until chosen); an
    Undo here is not undone by the next pull, because each change applies by
    its event id only once. Every applied change is logged to the owner-actions
    log, so Undo works on this Mac."""
    if not routing._replication_live():
        return
    from khipu import owner_direction
    from khipu.config import mutate_config

    with routing._lock():
        mine = routing._local_mac()
        applied = dict(_state().get("settings_applied") or {})
        todo = [(k, r) for k, r in _newest_setting_events().items()
                if r.get("mac") != mine and r.get("snapshot") is not True and applied.get(k) != r.get("id")]
    for key, row in todo:
        try:
            value = routing._coerce(routing.SETTINGS[key][0], json.loads(row["value_json"]), key)
        except (ValueError, TypeError, KeyError):
            continue
        was_set = row.get("value_set") is True
        if routing.settings()[key] != value:
            where = mac_label(row.get("mac"))
            stamp = {"auth": "other_mac", "words": f"the same change made on {where}"}
            owner_direction.noted(owner_direction.intent("config.routing_setting", key, facts={"from": where}), stamp)
            try:
                with routing._lock(), mutate_config() as data:
                    owner_direction.record_setting_before(key, data)
                    if was_set:
                        data[key] = value
                    else:
                        data.pop(key, None)
            except Exception:
                owner_direction.finish({"ok": False})
                raise
            owner_direction.finish({"ok": True, key: value})
        state.setdefault("settings_applied", {})[key] = row["id"]


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


def sync_if_due(now=None):
    """``sync``, unless one finished within ``POLL_SECONDS``. Polls run far more
    often than that, and each exchange is several hub queries."""
    last = routing._parse(_state().get("checked_local_at"))
    now = now or routing._now()
    if last is not None and 0 <= (now - last).total_seconds() < POLL_SECONDS:
        return dict(ok=True, recent=True, caught_up=False)
    return sync()


# ---- what waits on the owner, on every Mac ---------------------------------------------------
#
# A request a conversation holds for the owner lives in that Mac's own files.
# It travels as a ``request_state`` line (the routing log, ``_sync`` and
# ``routing_events``, no other path); the other Mac keeps a mirror row in its
# own held-requests file, so Needs you, the app and the CLI list it like any
# held request and the owner's yes or no is proved there as always. His answer
# goes back as a ``request_answer`` line; the Mac that holds the request applies
# it with its own owner handling (``owner_direction.confirm``) exactly once.

REQUEST_KEPT_S = 14 * 86400
_ASK_MOST = 1200
_FINAL = {"applied": "applied", "applying": "applied", "failed": "failed", "interrupted": "failed", "declined": "declined"}


def _sig(text):
    """What an answer is bound to, as the whole hash the routing record's
    ``revision_id`` shape carries (a short hex string reads as a key)."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _request_rows():
    """This Mac's own requests as (id, kind, state, action, sig, asked_at, ask, withheld)."""
    from khipu import owner_direction, needs_you
    from khipu.config import load_config
    from khipu.redact import redact_secrets

    out = []
    try:
        rows = owner_direction._read_requests(owner_direction.requests_path())
    except (OSError, ValueError):
        rows = []
    for row in rows:
        if row.get("action") == owner_direction.READ_ACTION or row.get("remote") or not row.get("id"):
            continue
        state = "pending" if row.get("state") == "pending" else _FINAL.get(row.get("state"))
        if state is None or not isinstance(row.get("args"), dict) or not owner_direction.reviewed_hold(row):
            continue
        ask = str(row.get("ask") or "")
        withheld = bool(row.get("files")) or len(ask) > _ASK_MOST or bool(redact_secrets(ask, force=True)[1])
        if withheld:
            ask = ("A conversation asked to "
                   + needs_you.ACTION_WORDS.get(row.get("action"), "make a change") + ".")
        blocked = owner_direction.unapplyable_code(row) if state == "pending" else None
        out.append((str(row["id"]), "held", state, str(row.get("action") or ""),
                    _sig(owner_direction.signature(row["action"], row["args"])), row.get("created_at"), ask, withheld,
                    (row.get("by") or {}).get("harness") if isinstance(row.get("by"), dict)
                    and row["by"].get("kind") in (None, "conversation") else None, blocked))
    config = load_config()
    for key, waiting in routing._setting_requests(config).items():
        if key not in routing.SETTINGS:
            continue
        kind, default, name = routing.SETTINGS[key]
        before = waiting.get("prior") if waiting.get("prior_set") else default
        words = (f"A conversation asked to change a routing setting: {name}. It would go from "
                 f"{needs_you._setting_value(kind, before)} to {needs_you._setting_value(kind, config.get(key))}.")
        out.append((f"setting:{key}", "setting", "pending", "config.answer_request",
                    _sig(routing.setting_request_pin(key, waiting, config.get(key))), waiting.get("at"), words, False,
                    None, None))
    return out


def _request_event(request, kind, state, action, sig, asked_at, ask, withheld, by=None, blocked=None):
    from khipu import owner_direction

    event = {"type": "request_state", "request": request, "req_kind": kind, "req_state": state, "sig": sig,
             "withheld": withheld, "ask": ask, "written_by": {"kind": "check"}, "via": "check"}
    # Which harness asked (never a session or a name) and why the holder can
    # now only refuse it, so the other Mac reads the same item.
    if isinstance(by, str) and by:
        event["asked_harness"] = by
    if blocked in owner_direction.UNAPPLYABLE_WORDS:
        event["req_blocked"] = blocked
    if action and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", action):
        event["req_action"] = action
    if isinstance(asked_at, str) and asked_at:
        event["asked_at"] = asked_at
    return event


def _emit_request_states(rows, state):
    """One ``request_state`` line for each request whose state changed since
    the last one sent; ``state`` remembers what was sent."""
    sent = state.setdefault("requests_published", {})
    batch = []
    for row in rows:
        request, kind, st, action, sig, asked_at, ask, withheld, by, blocked = row
        # A waiting request is sent again when its refusal reason appears; an
        # older line without the reason slot is sent once more to carry it.
        key = [st, sig, blocked or ""] if st == "pending" else [st, sig]
        if sent.get(request) == key:
            continue
        batch.append(_request_event(*row))
        sent[request] = key
    if batch:
        routing._write_many(batch)
    return len(batch)


def publish_requests():
    """Tell the other Macs what waits here, and how each request ended."""
    if not routing._replication_live():
        return
    with routing._lock():
        state = _state()
        if state.get("mac") not in (None, routing._local_mac()):
            return
        rows = _request_rows()
        seen = {r[0] for r in rows}
        # A setting request that is no longer waiting was answered here.
        gone = [(request, "setting" if request.startswith("setting:") else "held", "gone", "", sig, None, "", False, None, None)
                for request, (st, sig, *_rest) in (state.get("requests_published") or {}).items()
                if request not in seen and st == "pending" and request.startswith("setting:")]
        if _emit_request_states(rows + gone, state):
            state.setdefault("mac", routing._local_mac())
            _save(state)


def answer_remote(request, stamp, answer):
    """The owner's yes or no, typed here, to a request another Mac holds."""
    remote = request.get("remote") or {}
    source = (request.get("confirmed") or request.get("declined") or {})
    sha = (stamp or {}).get("prompt_sha256") or source.get("message_sha256")
    event = {"type": "request_answer", "request": remote["request"], "req_answer": answer, "sig": remote["sig"]}
    harness = (stamp or {}).get("harness")
    if isinstance(harness, str) and harness:
        event["answer_harness"] = harness
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
        event["message_sha"] = sha
    routing._write_many(routing.owner_lines([event], "direction"))
    return True


def _mirror_id(request):
    return "rm-" + request


def _write_mirror(update):
    """``update(rows, now)`` under the held-requests lock; True when it saved."""
    from khipu import owner_direction

    now = owner_direction._clock()
    with owner_direction._requests_locked() as path:
        state = owner_direction._load(path)
        if update(state["requests"], now) is False:
            return False
        owner_direction._save(path, state, now)
    return True


def _asked_at(row, now):
    """When the holder's conversation asked, not when this Mac heard of it."""
    from khipu import owner_direction

    at = row.get("asked_at")
    if isinstance(at, str) and routing._parse(at) is not None:
        return at
    return owner_direction._iso(now) if now is not None else None


def _asked_by(row):
    """Who asked, in Khipu's own fixed words for a harness; anything else is dropped."""
    harness = row.get("asked_harness")
    if not isinstance(harness, str) or not harness:
        return None
    return routing.author_words({"kind": "conversation", "harness": harness}, push=True)


def _blocked(row):
    from khipu import owner_direction

    code = row.get("req_blocked")
    return code if code in owner_direction.UNAPPLYABLE_WORDS else None


def mirror_remote_requests(state):
    """A request another Mac holds, as a held request here; and how it ended
    there, so a request settled elsewhere never lingers."""
    from khipu import owner_direction

    mine = routing._local_mac()
    newest = {}
    for row in remote_events():
        if row.get("type") == "request_state" and row.get("mac") not in (None, mine):
            key = (row.get("mac"), row.get("request"))
            if key not in newest or _event_order(row) > _event_order(newest[key]):
                newest[key] = row
    for (mac, request), row in newest.items():
        where = mac_label(mac)
        final = row.get("req_state")
        mirror = _mirror_id(request)

        def update(rows, now, row=row, mac=mac, request=request, mirror=mirror, where=where, final=final):
            current = next((r for r in rows if r.get("id") == mirror), None)
            if final == "pending":
                if current is not None:
                    # A line from a newer holder can add who asked, when, or that
                    # it can now only be refused; a request stays one row.
                    if current.get("state") != "pending":
                        return False
                    words, blocked, at = _asked_by(row), _blocked(row), _asked_at(row, None) or current.get("created_at")
                    if ((current.get("by") or {}).get("words"), (current.get("remote") or {}).get("blocked"),
                            current.get("created_at")) == (words, blocked, at):
                        return False
                    current["by"] = {"kind": "other_mac", "words": words}
                    current.setdefault("remote", {})["blocked"] = blocked
                    current["created_at"] = at
                    return True
                if row.get("withheld") is True or not row.get("ask"):
                    ask = ("A conversation on " + where + " asked for a change. Its words stay on that Mac, so they "
                           "can't be shown here: answer it there.")
                    withheld = True
                else:
                    ask, withheld = str(row["ask"]), False
                rows.append({"id": mirror, "action": "direction.remote", "args": {"request": request, "mac": mac},
                             "state": "pending", "created_ts": now, "created_at": _asked_at(row, now),
                             "session": None, "by": {"kind": "other_mac", "words": _asked_by(row)}, "ask": ask,
                             "files": None,
                             "remote": {"mac": mac, "request": request, "sig": row.get("sig"), "withheld": withheld,
                                        "kind": row.get("req_kind"), "action": row.get("req_action"),
                                        "blocked": _blocked(row)}})
                return True
            if current is None or current.get("state") not in ("pending", "answered", "applying"):
                return False
            answered = current.get("state") != "pending"
            text = {"applied": f"Applied on {where}.",
                    "declined": f"Dropped on {where}.",
                    "failed": f"{where} could not apply it, so nothing changed there.",
                    "gone": f"It was already settled on {where}.",
                    "stale": f"That was already settled on {where} before your answer arrived, so your answer "
                             "changed nothing."}.get(final, f"It ended on {where}.")
            if not answered and final in ("applied", "declined", "gone", "stale"):
                text = f"It was settled on {where} before you answered. " + text
            current.update(state="stale" if final in ("stale", "gone") else final, settled_ts=now,
                           settled_at=owner_direction._iso(now), receipt=text)
            return True

        _write_mirror(update)


def remote_requests():
    """The requests this Mac mirrors from others and how each stands, for Your
    Macs and ``route replication status``: plain words, newest first."""
    from khipu import owner_direction

    out = []
    try:
        rows = owner_direction._read_requests(owner_direction.requests_path())
    except (OSError, ValueError):
        return out
    for row in rows:
        remote = row.get("remote")
        if not remote:
            continue
        state = row.get("state")
        blocked = owner_direction.UNAPPLYABLE_WORDS.get(remote.get("blocked")) if state == "pending" else None
        out.append({"id": row.get("id"), "ask": None if remote.get("withheld") else row.get("ask"),
                    "answerable": not remote.get("withheld"),
                    # The holder would refuse a yes: only a no is offered, for the reason it gives.
                    "decline_only": bool(blocked), "reason": blocked,
                    "asked_by": (row.get("by") or {}).get("words"), "asked_at": row.get("created_at"),
                    "request": remote.get("request"), "mac": mac_label(remote.get("mac")), "state": state,
                    "words": row.get("receipt") or (
                        "Your answer is on its way; it applies there within a few minutes." if state == "answered"
                        else "Waiting for your answer." if state == "pending" else None),
                    "stale": state == "stale"})
    return out


def apply_remote_answers(state):
    """Apply each answer another Mac sent to a request held here, once, with
    this Mac's own owner handling. A duplicate delivery does nothing; an answer
    to a request that already ended says so on the other Mac."""
    from khipu import owner_act, owner_direction

    mine = routing._local_mac()
    done = set(state.get("answers_done") or [])
    answers = [e for e in remote_events() if e.get("type") == "request_answer" and e.get("mac") not in (None, mine)
               and e.get("id") not in done]
    outcomes = []
    for answer in sorted(answers, key=_event_order):
        request, wanted = answer.get("request"), answer.get("req_answer")
        verdict = _apply_one(answer, request, wanted, owner_act, owner_direction)
        done.add(answer["id"])
        if verdict:
            outcomes.append((request, verdict))
    state["answers_done"] = sorted(done)[-500:]
    if outcomes:
        _emit_outcomes(outcomes, state)


def _emit_outcomes(outcomes, state):
    sent = state.setdefault("requests_published", {})
    batch = []
    for request, (verdict, sig) in outcomes:
        batch.append(_request_event(request, "setting" if request.startswith("setting:") else "held", verdict, "", sig,
                                    None, "", False, None, None))
        if verdict not in ("stale", "gone"):
            # A stale answer says something about the answer, not the request:
            # the request's own state is still what ``publish_requests`` sends.
            sent[request] = [verdict, sig]
    routing._write_many(batch)


def _apply_one(answer, request, wanted, owner_act, owner_direction):
    """(state to publish, sig) or None for a harmless duplicate."""
    sig = answer.get("sig") or ""
    if isinstance(request, str) and request.startswith("setting:"):
        return _apply_setting_answer(answer, request, wanted, owner_act, owner_direction)
    try:
        rows = owner_direction._read_requests(owner_direction.requests_path())
    except (OSError, ValueError):
        return ("failed", sig)
    row = next((r for r in rows if r.get("id") == request and not r.get("remote")), None)
    if row is None:
        return ("gone", sig)
    if row.get("state") != "pending":
        duplicate = (wanted == "confirm" and row.get("state") in ("applying", "applied")) or (
            wanted == "decline" and row.get("state") == "declined")
        return None if duplicate else ("stale", sig)
    if _sig(owner_direction.signature(row.get("action"), row.get("args"))) != sig:
        return ("stale", sig)
    try:
        if wanted == "decline":
            owner_direction.decline(request, remote=answer)
            return ("declined", sig)
        claimed, stamp = owner_direction.confirm(request, remote=answer)
    except owner_direction.DirectionRefused as refused:
        return ("stale" if refused.code in ("not_pending", "not_found") else "failed", sig)
    ok, _result = owner_act.apply_confirmed(claimed, stamp)
    return ("applied" if ok else "failed", sig)


def _apply_setting_answer(answer, request, wanted, owner_act, owner_direction):
    from khipu.config import load_config

    key, sig = request.partition(":")[2], answer.get("sig") or ""
    config = load_config()
    waiting = routing._setting_requests(config).get(key)
    if waiting is None:
        return None if _state().get("requests_published", {}).get(request, [None])[0] in ("applied", "declined", "gone") \
            else ("gone", sig)
    pin = routing.setting_request_pin(key, waiting, config.get(key))
    if _sig(pin) != sig:
        return ("stale", sig)
    action = "config.answer_request." + ("confirm" if wanted == "confirm" else "undo")
    stamp = owner_direction.remote_stamp(answer)
    request_row = {"id": request, "action": action, "args": {"key": key, "expect": pin}}
    try:
        head, positionals = owner_act.argv(action, owner_act._values(action, request_row["args"]))
        full = owner_act.full_argv(head, positionals, ["--owner-direction"])
        with owner_direction.confirmed_replay(action, stamp):
            ok, _result = owner_act._run(action, full)
    except owner_act.Refused:
        ok = False
    return ("applied" if ok and wanted == "confirm" else "declined" if ok else "failed", sig)


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
    with routing._lock():
        mine = [e for e in routing._read_event_lines()[0] if e.get("type") == "settings_sync_switch"]
    # This Mac's owner flipped it after that line was written: his later word stands.
    if mine and max(_event_order(e) for e in mine) >= _event_order(dict(newest["payload"], hub_order=newest.get("hub_order"))):
        return
    from khipu.config import mutate_config
    with routing._lock(), mutate_config() as data:
        data["routing_replication_live"] = enabled


def _control_sync(*, hub=None):
    """The periodic off-state control poll. It only uploads/pulls the tiny
    switch record; ordinary routing stays gated until that record says on."""
    if hub is None and not routing._hub_configured():
        return {"ok": True, "disabled": True, "caught_up": False}
    from khipu.config import load_config

    if "routing_replication_live" in routing._setting_requests(load_config()):
        # A conversation asked to turn the copy on and the owner has not
        # answered: this Mac makes no hub call at all until he does.
        return {"ok": True, "disabled": True, "caught_up": False}
    try:
        with _sync_lock(), routing._lock():
            mac = routing._mac_locked()
            local, _bad = routing._read_event_lines()
        state = _state()
        # One look per timer tick, never one per prompt: a Mac with the copy
        # off touches the hub for this one small record and nothing else.
        polled = routing._parse(state.get("control_polled_at"))
        now = routing._now()
        pending = any(e.get("type") == "settings_sync_switch" and e.get("id") not in (state.get("control_uploaded") or [])
                      for e in local)
        if polled is not None and (now - polled).total_seconds() < CONTROL_EVERY_S and not pending:
            return {"ok": True, "disabled": True, "caught_up": False}
        state["control_polled_at"] = routing._iso(now)
        _save(state)
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
            publish_requests()
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
            ordered = sorted(enumerate(local, 1), key=lambda pair: (pair[1].get("mac", ""), pair[1].get("seq", pair[0]), pair[0]))
            deadline = time.monotonic() + ROUND_BUDGET_S
            first_round = True
            while True:
                # Each round sends one bounded slice and then pulls, so a large
                # local backlog never starves the other Macs; rounds repeat while
                # the budget lasts, and what is left goes on the next tick.
                sent_this_turn = 0
                local_pending = False
                finished = None
                for position, event in ordered:
                    origin = event.get("mac") or "legacy"
                    seq = event.get("seq", position)
                    retry = seq <= uploaded.get(origin, 0)
                    # A deferred row is tried again once per sync, not every round.
                    if retry and (event.get("id") not in deferred or not first_round):
                        continue
                    projected = routing.replication_payload([event], bindings=bindings)
                    known = (uploaded.get(origin), deferred.get(event.get("id")))
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
                    # A deferred row that is still deferred changed nothing: saving the
                    # whole state for each one (a thousand usage samples) rewrote it
                    # and synced it a thousand times a round.
                    if not retry or known != (uploaded.get(origin), deferred.get(event.get("id"))):
                        _save(state)
                    # A deferred row tried again is not new work for the slice.
                    sent_this_turn += 0 if retry else 1
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
                    _apply_remote_settings(state)
                    apply_remote_answers(state)
                    mirror_remote_requests(state)
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
                        finished = dict(ok=True, caught_up=not local_pending, local_pending=local_pending, skipped=skipped,
                                        deferred_kinds=sorted(set(deferred.values())))
                        break
                    if not rows:
                        raise ValueError("pull made no progress")
                    state["scan_after"] = [rows[-1]["received_at"], rows[-1]["event_id"]]
                    state["scan_until"] = result["until"]
                    _save(state)
                if finished is None:
                    return dict(ok=True, caught_up=False, partial=True)
                if local_pending and time.monotonic() < deadline:
                    first_round = False
                    continue
                return finished
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
    first_catch_up = caught is None and bool(state.get("uploaded") or state.get("scan_after"))
    return dict(caught_up_at=state.get("caught_up_at"), age_seconds=age,
                first_catch_up=first_catch_up,
                words=("Copying routing between your Macs for the first time. This can take a few minutes."
                       if first_catch_up else None),
                stale=age is None or age > 600, sources=state.get("sources", []),
                hub_offset_seconds=state.get("hub_offset_seconds"),
                deferred_kinds=sorted(set(state.get("deferred", {}).values())))


def age_line(now=None):
    if not routing._replication_live():
        return None
    current = status(now)
    if current["first_catch_up"]:
        return current["words"]
    age = current["age_seconds"]
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
