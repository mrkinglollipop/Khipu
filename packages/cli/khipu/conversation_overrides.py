"""Conversation-scoped routing pace overrides.

The routing record remains shared and deliberately conversation-free.  This
small, local append-only log is consulted only by a conversation's own advice
read and is never copied to the hub.
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
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{7,63}\Z")


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


def subject(author: dict | None = None, *, environ: dict | None = None, cwd: str | None = None) -> tuple[dict | None, str | None]:
    """The subject Khipu can prove from this process, or a fixed refusal.

    T3 threads are stronger than a session id, but an ambiguous/missing T3
    result is never guessed.  This deliberately accepts environment spoofing:
    the policy is advisory and a short end/ceiling bounds the consequence.
    """
    author = author or routing.conversation_author(environ=environ, cwd=cwd)
    session = author.get("session") if isinstance(author, dict) else None
    project = author.get("project") if isinstance(author, dict) else None
    harness = author.get("harness") if isinstance(author, dict) else None
    if not isinstance(session, str) or not session:
        return None, "Khipu can't identify this conversation, so it uses normal pace."
    try:
        from khipu import t3
        ids, known = t3.session_thread_ids_result(session)
    except Exception:
        ids, known = None, False
    if known:
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
            return None, "Khipu can't identify this T3 thread, so it uses normal pace."
        return {"kind": "thread", "id": ids[0], "mac": routing._local_mac()}, None
    # A configured T3 store that did not answer is unknown, not permission to
    # fall back.  With no T3 store this is a native session and its own id is
    # the stable subject (and is also what native children carry).
    try:
        from khipu import t3
        t3_store = Path(os.environ.get("KHIPU_T3_DB") or Path.home() / ".t3/userdata/statev2.sqlite")
    except Exception:
        t3_store = None
    if t3_store and t3_store.exists():
        return None, "Khipu can't identify this T3 thread, so it uses normal pace."
    return {"kind": "session", "id": session, "harness": harness, "project": project,
            "mac": routing._local_mac()}, None


def _same(a: dict, b: dict) -> bool:
    return a.get("kind") == b.get("kind") and a.get("id") == b.get("id") and a.get("mac") == b.get("mac")


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


def _effective(rows: list[dict], now: datetime) -> list[dict]:
    ended = {r.get("override") for r in rows if r.get("type") == "end"}
    live: dict[str, dict] = {}
    for r in rows:
        if r.get("type") != "grant" or r.get("id") in ended:
            continue
        until = _parse(r.get("until"))
        if until is not None and until <= now:
            continue
        live[r["id"]] = r
    return list(live.values())


def list_overrides(*, now: datetime | None = None) -> list[dict]:
    return _effective(_rows(), _now(now))


def _write(row: dict, now: datetime) -> dict:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # The routing lock is the writer boundary for routing-adjacent local logs.
    with routing._lock():
        existing = _rows()
        keep = routing.settings().get(KEEP_DAYS_SETTING, DEFAULT_KEEP_DAYS)
        keep = keep if isinstance(keep, int) and keep >= 0 else DEFAULT_KEEP_DAYS
        if keep:
            cutoff = now - timedelta(days=keep)
            ended = {r.get("override") for r in existing if r.get("type") == "end"}
            discard = {r["id"] for r in existing if r.get("type") == "grant" and r.get("id") in ended
                       and (_parse(r.get("at")) or now) < cutoff}
            discard |= {r["id"] for r in existing if r.get("type") == "grant" and r.get("id") not in ended
                        and (_parse(r.get("until")) or now) < cutoff}
            existing = [r for r in existing if r.get("id") not in discard and r.get("override") not in discard]
        try:
            p.write_text("".join(json.dumps(x, separators=(",", ":"), sort_keys=True) + "\n" for x in [*existing, row]),
                         encoding="utf-8")
        except OSError as exc:
            raise routing.RouteError("Khipu can't write conversation overrides") from exc
    return row


def grant(values: dict, *, now: datetime | None = None) -> dict:
    """Owner-side grant. ``subject`` is the frozen subject from the ask."""
    now = _now(now)
    if not isinstance(values, dict):
        raise routing.RouteError("an override needs its account, effect and frozen subject")
    account = routing._resolve_account_key(_clean(values.get("account"), "account", 128))
    subj = values.get("subject")
    if not isinstance(subj, dict) or subj.get("kind") not in ("thread", "session", "project") or not isinstance(subj.get("id"), str):
        raise routing.RouteError("an override needs the conversation subject shown in its ask")
    effect = values.get("effect")
    if effect != "ahead_of_pace":
        raise routing.RouteError("effect must be ahead_of_pace")
    indefinite = values.get("indefinite") is True
    until = None if indefinite else routing.parse_until(_clean(values.get("until"), "until"), now)
    if until is not None and until <= now:
        raise routing.RouteError("an override's end must be in the future")
    review_at = None
    if indefinite:
        review_at = now + timedelta(days=7)
    elif values.get("review_at"):
        review_at = routing.parse_until(_clean(values.get("review_at"), "review_at"), now)
    ceiling = values.get("ceiling_pct")
    if ceiling is not None and (isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 0 < ceiling <= 100):
        raise routing.RouteError("ceiling_pct must be above 0 and at most 100")
    record = {"type": "grant", "id": routing.event_identity.uuid7(), "subject": dict(subj), "account": account,
              "effect": effect, "at": _iso(now), "by": "owner", "children": bool(values.get("children", True)),
              "request": _clean(values.get("request") or "owner grant", "request", 300),
              **({"until": _iso(until)} if until else {"indefinite": True, "review_at": _iso(review_at)}),
              **({"ceiling_pct": float(ceiling)} if ceiling is not None else {}),
              **({"reason": _clean(values["reason"], "reason", 300)} if values.get("reason") else {}),
              **({"ref": _clean(values["ref"], "ref", 120)} if values.get("ref") else {})}
    # Replacement is explicit and preserves the audit record, never two
    # overlapping grants for one conversation/account.
    for old in list_overrides(now=now):
        if old.get("account") == account and _same(old.get("subject") or {}, subj):
            _write({"type": "end", "id": routing.event_identity.uuid7(), "override": old["id"], "at": _iso(now), "by": "owner"}, now)
    return _write(record, now)


def end(override: str, *, now: datetime | None = None) -> dict:
    now = _now(now)
    if not isinstance(override, str) or not _ID.fullmatch(override):
        raise routing.RouteError("override must be its id")
    if not any(r.get("id") == override for r in list_overrides(now=now)):
        raise routing.RouteError("that override is not in force")
    return _write({"type": "end", "id": routing.event_identity.uuid7(), "override": override, "at": _iso(now), "by": "owner"}, now)


def match(account: str, author: dict | None = None, *, now: datetime | None = None, environ: dict | None = None) -> tuple[dict | None, str | None]:
    now = _now(now)
    subj, reason = subject(author, environ=environ)
    if subj is None:
        return None, reason
    for row in list_overrides(now=now):
        parent = row.get("subject") or {}
        child_project = bool(row.get("children")) and parent.get("kind") == "project" and parent.get("id") == subj.get("project") and parent.get("mac") == subj.get("mac")
        if row.get("account") == account and (_same(parent, subj) or child_project):
            return row, None
    return None, None


def advice(account: str, author: dict | None = None, *, now: datetime | None = None, environ: dict | None = None) -> tuple[dict | None, str | None]:
    row, reason = match(account, author, now=now, environ=environ)
    if row is None:
        return None, reason
    line = "go (override" + (f" until {row['until']}" if row.get("until") else "; review due " + str(row.get("review_at"))) + ")"
    return {"id": row["id"], "line": line, "ceiling_pct": row.get("ceiling_pct"), "until": row.get("until"),
            "indefinite": bool(row.get("indefinite")), "review_due": bool(row.get("indefinite") and (_parse(row.get("review_at")) or now) <= _now(now))}, None


def walkthrough(*, now: datetime | None = None) -> dict:
    """Read-only duplicate/conflict/review report; callers propose, then grant/end."""
    now = _now(now)
    live = list_overrides(now=now)
    items: list[dict] = []
    for row in live:
        until = _parse(row.get("until"))
        if until and until - now <= timedelta(hours=6):
            items.append({"code": "ending_soon", "override": row["id"], "proposed": "end or extend it"})
        if row.get("indefinite") and (_parse(row.get("review_at")) or now) <= now:
            items.append({"code": "review_due", "override": row["id"], "proposed": "keep or end it"})
    return {"ok": True, "overrides": live, "items": items,
            "line": "Nothing to change." if not items else "Review the proposed changes together, then one yes applies them."}
