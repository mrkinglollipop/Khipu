"""Conversation-scoped routing pace overrides.

The routing record stays shared and conversation-free.  This small append-only
log is read only by a conversation's own advice and by the owner's views, and is
never copied to the hub or sent in a routing push.

Records (one JSON object per line):

* ``grant``  an override: who it is for (``subject``), the account, the end (or
  ``indefinite`` with a ``review_at``), an optional ceiling.
* ``end``    ends one early.  ``unend`` takes an end back (Undo).
* ``review`` moves an indefinite override's review date (Keep).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from khipu import routing

NAME = "conversation-overrides.jsonl"
KEEP_DAYS_SETTING = "routing_override_log_keep_days"
DEFAULT_KEEP_DAYS = 30
REVIEW_DAYS = 7
CEILING_WINDOWS = ("weekly", "five-hour")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{7,63}\Z")
_PARENT_HOPS = 4  # how far up a T3 helper's chain of parents an override is looked for


def path() -> Path:
    return routing.routing_dir() / NAME


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    return routing._parse(value) if isinstance(value, str) else None


def _clean(value: Any, name: str, most: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or len(value.strip()) > most:
        raise routing.RouteError(f"{name} must be one line of at most {most} characters")
    return value.strip()


# ---- who a conversation is -------------------------------------------------------------------


def _bare_session(value: str) -> str:
    """A session id without its ``harness:`` prefix, so a parent named as
    ``claude_code:abc`` equals the ``abc`` the child's server sees."""
    head, sep, tail = value.partition(":")
    return tail if sep and head in ("claude_code", "claude", "codex", "cursor", "aegis", "t3") else value


def subject(author: dict | None = None, *, environ: dict | None = None, cwd: str | None = None,
            scope: str = "conversation") -> tuple[dict | None, str | None]:
    """The subject Khipu can prove from this process, or a fixed reason it can't.

    T3 threads are stronger than a session id, but an ambiguous or missing T3
    answer is never guessed.  Environment spoofing is accepted: advice is
    advisory and the end and ceiling bound the burn.  ``scope="project"`` names
    every conversation in this folder (the weakest subject: a folder's name).
    """
    author = author or routing.conversation_author(environ=environ, cwd=cwd)
    session = author.get("session") if isinstance(author, dict) else None
    project = author.get("project") if isinstance(author, dict) else None
    harness = author.get("harness") if isinstance(author, dict) else None
    mac = routing._local_mac()
    if scope == "project":
        if not isinstance(project, str) or not project:
            return None, "Khipu can't tell which folder this conversation is in, so it uses normal pace."
        return {"kind": "project", "id": project, "mac": mac}, None
    if not isinstance(session, str) or not session:
        return None, "Khipu can't identify this conversation, so it uses normal pace."
    try:
        from khipu import t3
        ids, known = t3.session_thread_ids_result(session)
    except Exception:  # noqa: BLE001 - unknown, never a guess
        ids, known = None, False
    if known:
        if ids:
            if len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
                return None, "Khipu can't identify this T3 thread, so it uses normal pace."
            return {"kind": "thread", "id": ids[0], "mac": mac}, None
        # T3 answered and has no thread for this session: a native session.
    else:
        t3_store = Path(os.environ.get("KHIPU_T3_DB") or Path.home() / ".t3/userdata/statev2.sqlite")
        if t3_store.exists():
            return None, "Khipu can't identify this T3 thread, so it uses normal pace."
    return {"kind": "session", "id": _bare_session(session), "harness": harness, "project": project, "mac": mac}, None


def ancestors(subj: dict, environ: dict | None = None) -> list[dict]:
    """The conversations that started this one, nearest first: a T3 helper's
    parent threads (T3's own parent link) and a native helper's parent session.
    Anything Khipu can't read is left out, so a helper falls back to normal pace."""
    out: list[dict] = []
    if subj.get("kind") == "thread":
        from khipu import t3
        seen, current = {subj["id"]}, subj["id"]
        for _ in range(_PARENT_HOPS):
            try:
                parent, definitive = t3.parent_thread_result(current)
            except Exception:  # noqa: BLE001
                break
            if not definitive or not parent or parent in seen:
                break
            out.append({"kind": "thread", "id": parent, "mac": subj.get("mac")})
            seen.add(parent)
            current = parent
    elif subj.get("kind") == "session":
        from khipu import session_capture
        env = os.environ if environ is None else environ
        parent = session_capture.parent_session_id({}, env, harness=subj.get("harness") or "", sid=subj.get("id") or "")
        if parent:
            out.append({"kind": "session", "id": _bare_session(parent), "mac": subj.get("mac")})
    return out


def _same(a: dict, b: dict) -> bool:
    return a.get("kind") == b.get("kind") and a.get("id") == b.get("id") and a.get("mac") == b.get("mac")


# ---- the log ---------------------------------------------------------------------------------


def _rows() -> list[dict]:
    try:
        data = path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise routing.RouteError("Khipu can't read conversation overrides") from exc
    out = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            out.append(row)
    return out


def _ended(rows: list[dict]) -> set[str]:
    taken_back = {r.get("end") for r in rows if r.get("type") == "unend"}
    return {r.get("override") for r in rows if r.get("type") == "end" and r.get("id") not in taken_back}


def _effective(rows: list[dict], now: datetime) -> list[dict]:
    ended = _ended(rows)
    review = {}
    for r in rows:
        if r.get("type") == "review":
            review[r.get("override")] = r.get("review_at")
    live: dict[str, dict] = {}
    for r in rows:
        if r.get("type") != "grant" or r.get("id") in ended:
            continue
        until = _parse(r.get("until"))
        if until is not None and until <= now:
            continue  # an end that has passed needs no write: readers ignore it
        live[r["id"]] = dict(r, review_at=review.get(r["id"], r.get("review_at"))) if r.get("indefinite") else r
    return list(live.values())


def list_overrides(*, now: datetime | None = None) -> list[dict]:
    return _effective(_rows(), _now(now))


def _append(make, now: datetime) -> Any:
    """Under the routing lock: ``make(rows)`` returns (new rows, result); they
    are written after the retention pass drops what has been over for a month."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with routing._lock():
        existing = _rows()
        new, result = make(existing)
        keep = routing.settings().get(KEEP_DAYS_SETTING, DEFAULT_KEEP_DAYS)
        keep = keep if isinstance(keep, int) and keep >= 0 else DEFAULT_KEEP_DAYS
        if keep:
            cutoff = now - timedelta(days=keep)
            ended = _ended(existing)
            gone = {r["id"] for r in existing if r.get("type") == "grant"
                    and ((r["id"] in ended and (_parse(r.get("at")) or now) < cutoff)
                         or (r["id"] not in ended and (_parse(r.get("until")) or now + timedelta(days=1)) < cutoff))}
            existing = [r for r in existing if r.get("id") not in gone and r.get("override") not in gone]
        try:
            p.write_text("".join(json.dumps(x, separators=(",", ":"), sort_keys=True) + "\n"
                                 for x in [*existing, *new]), encoding="utf-8")
        except OSError as exc:
            raise routing.RouteError("Khipu can't write conversation overrides") from exc
    return result


def _ident(account: str) -> str:
    """The account's one id (``codex:main``), whether it was saved under that id or
    under an older login-folder key (``codex|``): both mean the same account."""
    try:
        return routing.account_id(account)
    except Exception:  # noqa: BLE001 - an account Khipu cannot name keeps its own text
        return account


def _key(account: str) -> str:
    """The login key behind an account id, for its label."""
    try:
        return routing._resolve_account_key(account)
    except routing.RouteError:
        return account


def _event(kind: str, now: datetime, **fields) -> dict:
    return {"type": kind, "id": routing.event_identity.uuid7(), "at": _iso(now), "by": "owner", **fields}


def grant(values: dict, *, now: datetime | None = None) -> dict:
    """The owner's grant.  ``subject`` is the subject frozen when it was asked.
    A second grant for one conversation and account replaces the first in the
    same write, so there are never two."""
    now = _now(now)
    if not isinstance(values, dict):
        raise routing.RouteError("an override needs its account, effect and frozen subject")
    account = _ident(routing._resolve_account_key(_clean(values.get("account"), "account", 128)))
    subj = values.get("subject")
    if not isinstance(subj, dict) or subj.get("kind") not in ("thread", "session", "project") or not isinstance(subj.get("id"), str):
        raise routing.RouteError("an override needs the conversation subject shown in its ask")
    if values.get("effect") != "ahead_of_pace":
        raise routing.RouteError("effect must be ahead_of_pace")
    indefinite = values.get("indefinite") is True
    until = None if indefinite else routing.parse_until(_clean(values.get("until"), "until"), now)
    if until is not None and until <= now:
        raise routing.RouteError("an override's end must be in the future")
    ceiling = values.get("ceiling_pct")
    if ceiling is not None and (isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 0 < ceiling <= 100):
        raise routing.RouteError("ceiling_pct must be above 0 and at most 100")
    window = values.get("window") or ("weekly" if ceiling is not None else None)
    if window is not None and window not in CEILING_WINDOWS:
        raise routing.RouteError("window must be weekly or five-hour")
    record = {"type": "grant", "id": routing.event_identity.uuid7(), "subject": dict(subj), "account": account,
              "effect": "ahead_of_pace", "at": _iso(now), "by": "owner", "children": values.get("children") is not False,
              "request": _clean(values.get("request") or "owner grant", "request", 300),
              **({"until": _iso(until)} if until else {"indefinite": True, "review_at": _iso(now + timedelta(days=REVIEW_DAYS))}),
              **({"ceiling_pct": float(ceiling), "window": window} if ceiling is not None else {}),
              **({"reason": _clean(values["reason"], "reason", 300)} if values.get("reason") else {}),
              **({"ref": _clean(values["ref"], "ref", 120)} if values.get("ref") else {})}

    def make(rows):
        rows_ended = []
        for old in _effective(rows, now):
            if _ident(old.get("account") or "") == account and _same(old.get("subject") or {}, subj):
                rows_ended.append(_event("end", now, override=old["id"]))
        return [*rows_ended, record], rows_ended

    replaced = _append(make, now)
    return dict(record, replaced=[{"override": e["override"], "end": e["id"]} for e in replaced])


def end(override: str, *, now: datetime | None = None) -> dict:
    now = _now(now)
    if not isinstance(override, str) or not _ID.fullmatch(override):
        raise routing.RouteError("override must be its id")

    def make(rows):
        if not any(r.get("id") == override for r in _effective(rows, now)):
            raise routing.RouteError("that override is not in force")
        row = _event("end", now, override=override)
        return [row], row

    return _append(make, now)


def keep(override: str, *, now: datetime | None = None) -> dict:
    """Keep an indefinite override for another review period."""
    now = _now(now)
    if not isinstance(override, str) or not _ID.fullmatch(override):
        raise routing.RouteError("override must be its id")

    def make(rows):
        row = next((r for r in _effective(rows, now) if r.get("id") == override), None)
        if row is None:
            raise routing.RouteError("that override is not in force")
        if not row.get("indefinite"):
            raise routing.RouteError("that override has an end; only one with no end is kept")
        event = _event("review", now, override=override, review_at=_iso(now + timedelta(days=REVIEW_DAYS)),
                       previous=row.get("review_at"))
        return [event], event

    return _append(make, now)


def restore(*, ended: str | None = None, review: dict | None = None, now: datetime | None = None) -> dict:
    """Undo: take an end back, or put a review date back."""
    now = _now(now)

    def make(rows):
        if ended is not None:
            if not any(r.get("type") == "end" and r.get("id") == ended for r in rows):
                raise routing.RouteError("that ending is not on record")
            row = _event("unend", now, end=ended)
        else:
            row = _event("review", now, override=(review or {}).get("override"), review_at=(review or {}).get("review_at"))
        return [row], row

    return _append(make, now)


# ---- advice ----------------------------------------------------------------------------------


def resolve(author: dict | None = None, *, environ: dict | None = None) -> dict:
    """What an advice read needs to know about the caller, looked up once:
    its subject, the conversations that started it, whether it is a helper,
    and why it is unknown if it is."""
    subj, reason = subject(author, environ=environ)
    if subj is None:
        return {"subject": None, "ancestors": [], "reason": reason, "dispatched": False}
    from khipu import routing_advice
    return {"subject": subj, "ancestors": ancestors(subj, environ), "reason": None,
            "dispatched": routing_advice.is_dispatched(environ)}


def _applies_to(row: dict, who: dict) -> bool:
    parent = row.get("subject") or {}
    subj = who["subject"]
    kids = row.get("children", True)
    if parent.get("kind") == "project":
        inside = parent.get("id") == subj.get("project") and parent.get("mac") == subj.get("mac")
        return inside and (kids or not who["dispatched"])
    if _same(parent, subj):
        # A native helper carries its parent's session id, so it lands here
        # too; with "children" off it is excluded.
        return kids or not who["dispatched"]
    return bool(kids) and any(_same(parent, a) for a in who["ancestors"])


def matches(who: dict, now: datetime) -> dict[str, dict]:
    """account key -> the live override that applies to the caller."""
    if who.get("subject") is None:
        return {}
    out: dict[str, dict] = {}
    for row in list_overrides(now=now):
        if _applies_to(row, who):
            out.setdefault(_ident(row["account"]), row)
    return out


def match(account: str, author: dict | None = None, *, now: datetime | None = None, environ: dict | None = None) -> tuple[dict | None, str | None]:
    now = _now(now)
    account = _ident(account)
    if not any(_ident(r.get("account") or "") == account for r in list_overrides(now=now)):
        return None, None  # nothing granted for this account: no lookups
    who = resolve(author, environ=environ)
    if who["subject"] is None:
        return None, who["reason"]
    return matches(who, now).get(account), None


def short_time(value) -> str:
    """"Nov 4 19:40Z" for a stored time: plain, and the same wherever it is read."""
    when = _parse(value)
    return when.astimezone(timezone.utc).strftime("%b %-d %H:%MZ") if when else str(value)


def line(row: dict) -> str:
    """The fixed words a conversation hears (no writer-chosen text)."""
    if row.get("until"):
        return f"go (override until {short_time(row['until'])})"
    return f"go (override with no end; review {short_time(row.get('review_at'))})"


def advice(account: str, author: dict | None = None, *, now: datetime | None = None, environ: dict | None = None) -> tuple[dict | None, str | None]:
    now = _now(now)
    row, reason = match(account, author, now=now, environ=environ)
    if row is None:
        return None, reason
    return describe(row, now), None


def describe(row: dict, now: datetime) -> dict:
    return {"id": row["id"], "line": line(row), "ceiling_pct": row.get("ceiling_pct"), "window": row.get("window"),
            "until": row.get("until"), "indefinite": bool(row.get("indefinite")),
            "review_due": bool(row.get("indefinite") and (_parse(row.get("review_at")) or now) <= now)}


# ---- words -----------------------------------------------------------------------------------


def subject_words(subj: dict) -> str:
    kind = subj.get("kind")
    if kind == "project":
        return f"every conversation in the folder {subj.get('id')}"
    if kind == "thread":
        title = None
        try:
            from khipu import t3
            title = t3.thread_title(subj.get("id"))
        except Exception:  # noqa: BLE001
            title = None
        return f"the T3 conversation \"{title}\"" if title else "this T3 conversation"
    return "this conversation"


def when_words(row: dict) -> str:
    if row.get("until"):
        return f"until {short_time(row['until'])}"
    return f"with no end; Khipu will ask you again on {short_time(row.get('review_at'))}"


def cap_words(row: dict) -> str:
    """Where it stops: the ceiling named, or what the settings already say."""
    if row.get("ceiling_pct") is not None:
        w = "week" if row.get("window", "weekly") == "weekly" else "five-hour window"
        return f"stopping when the account reaches {row['ceiling_pct']:g}% of its {w}"
    return "stopping at the account's saved share cap if it has one, otherwise at its plan limit"


def ask_for(action: str, values: dict) -> str | None:
    """The question the owner says yes to for an override action, in plain words."""
    try:
        if action == "route.override.grant":
            label = routing._label_for_key(routing._resolve_account_key(values["account"]))
            row = {"until": None, "review_at": None}
            if values.get("indefinite"):
                row["review_at"] = f"{REVIEW_DAYS} days from now"
            else:
                row["until"] = _iso(routing.parse_until(values["until"], _now()))
            kids = "" if values.get("children") is False else " Helpers it starts can run ahead too."
            return (f"Let {subject_words(values['subject'])} run ahead of pace on {label} {when_words(row)}, "
                    f"{cap_words(values)}? Everyone else keeps the normal pace.{kids} "
                    f"After it ends the account may be ahead, so everyone will be asked to hold back until pace recovers.")
        if action in ("route.override.end", "route.override.keep"):
            row = next((r for r in list_overrides() if r.get("id") == values.get("override")), None)
            if row is None:
                return None
            label = routing._label_for_key(_key(row["account"]))
            who = subject_words(row.get("subject") or {})
            if action == "route.override.end":
                return f"End the override that lets {who} run ahead of pace on {label} now?"
            return f"Keep letting {who} run ahead of pace on {label} for another {REVIEW_DAYS} days?"
    except Exception:  # noqa: BLE001 - the plain ask still asks
        return None
    return None


def walkthrough(*, now: datetime | None = None, leave: str | None = None, keep_pair: str | None = None,
                restart: bool = False) -> dict:
    """The owner's walk through what to clean up in the priority and the
    overrides (``priority_cleanup``): the item to answer now, all the items,
    and the overrides in force.  Reads only; each item names the owner action
    that one yes applies."""
    from khipu import priority_cleanup

    now = _now(now)
    view = routing.read_view(now)
    out = priority_cleanup.walk(view, now, leave=leave, keep=keep_pair, restart=restart)
    out["items"] = priority_cleanup.items(view, now)
    out["overrides"] = list_overrides(now=now)
    return out


def owner_row(row: dict, now: datetime) -> dict:
    """One override as the app lists it, in plain words."""
    who = subject_words(row.get("subject") or {})
    return {"id": row["id"], "label": routing._label_for_key(_key(row["account"])), "who": who[:1].upper() + who[1:],
            "until": row.get("until"), "indefinite": bool(row.get("indefinite")), "review_at": row.get("review_at"),
            "review_due": bool(row.get("indefinite") and (_parse(row.get("review_at")) or now) <= now),
            "cap": cap_words(row)[:1].upper() + cap_words(row)[1:]}
