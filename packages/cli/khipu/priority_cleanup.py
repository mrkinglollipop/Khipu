"""Duplicate and conflict checks for the priority's temporary instructions
(exceptions) and the conversation overrides, and the owner's walk-through of
what to clean up.  Reads only: every change goes through an owner action the
owner says yes to, so the walk never writes the routing record itself.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

NEAR_OVERLAP = 0.8  # word overlap at which two instructions read as the same
SOON = timedelta(hours=6)
STATE_NAME = "priority-walkthrough.json"
_WORD = re.compile(r"[a-z0-9#]+")
_NUMBER = re.compile(r"#?\d+(?:\.\d+)?")


def fold(text) -> str:
    """The words with case, spacing and punctuation removed."""
    return " ".join(_WORD.findall(str(text or "").lower().replace("'", "")))


def _numbers(text) -> frozenset[str]:
    return frozenset(_NUMBER.findall(str(text or "")))


def relation(a, b) -> str | None:
    """``same`` (D1) or ``near`` (D2) for two instruction texts, else None.
    Near needs the same numbers and PR references, so "PRs #3 and #11" and
    "PRs #3 and #12" stay different."""
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return None
    if fa == fb:
        return "same"
    wa, wb = set(fa.split()), set(fb.split())
    if wa and wb and len(wa & wb) / len(wa | wb) >= NEAR_OVERLAP and _numbers(a) == _numbers(b):
        return "near"
    return None


def find_duplicate(text, existing) -> tuple[str, dict] | None:
    """The first live exception that ``text`` repeats: (``same`` | ``near``, it)."""
    best = None
    for other in existing or []:
        found = relation(text, other.get("text"))
        if found == "same":
            return found, other
        if found and best is None:
            best = (found, other)
    return best


# ---- the walk-through ------------------------------------------------------------------------


def _state_path() -> Path:
    from khipu import routing

    return routing.routing_dir() / STATE_NAME


def _state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"left": {}, "kept": {}}
    return {"left": dict(data.get("left") or {}), "kept": dict(data.get("kept") or {})} if isinstance(data, dict) \
        else {"left": {}, "kept": {}}


def _save(state: dict) -> None:
    from khipu import routing

    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with routing._lock():
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _item(code: str, key: str, words: str, *, yes: str, confirm: dict | None, **extra) -> dict:
    return {"code": code, "key": key, "words": words, "yes": yes,
            "settle": {"confirm": confirm}, **extra}


def _end_exception(version: str, account: str, exception: dict) -> dict:
    return {"action": "route.priority.amend",
            "args": {"account": account, "end_exception": exception["id"], "expect": version}}


def _exceptions(view: dict, now: datetime) -> list[tuple[dict, dict]]:
    from khipu import routing

    out = []
    for entry in (view.get("priority") or {}).get("entries") or []:
        for exc in entry.get("exceptions") or []:
            end = routing._parse(exc.get("until"))
            if end is None or end > now:
                out.append((entry, exc))
    return out


def _ends(exc: dict) -> str:
    return f"ends {exc.get('until')}" if exc.get("until") else "has no end"


def items(view: dict, now: datetime) -> list[dict]:
    """Every live clean-up item, in the order the walk shows them."""
    from khipu import conversation_overrides as overrides, routing

    found: list[dict] = []
    version = view.get("priority_version") or "p0"
    pairs = _exceptions(view, now)
    seen: set[tuple[str, str]] = set()
    for i, (entry, one) in enumerate(pairs):
        for other_entry, two in pairs[i + 1:]:
            if entry["account"] == other_entry["account"]:
                kind = relation(one.get("text"), two.get("text"))
                if kind:
                    # Keep the one written first, end the later.
                    later = two if (two.get("at") or "") >= (one.get("at") or "") else one
                    first = one if later is two else two
                    key = f"dup:{entry['account']}:{later['id']}"
                    if (entry["account"], later["id"]) in seen:
                        continue
                    seen.add((entry["account"], later["id"]))
                    label = entry.get("label") or routing._label_for_key(entry["account"])
                    sentence = "twice" if kind == "same" else "twice, worded a little differently"
                    found.append(_item(
                        "duplicate" if kind == "same" else "near_duplicate", key,
                        f"{label} has \"{routing._word_clip(first['text'], 80)}\" {sentence} ({_ends(first)} and {_ends(later)}).",
                        yes="End the second one and keep the first.", confirm=_end_exception(version, entry["account"], later),
                        account=entry["account"]))
            else:
                if relation(one.get("text"), two.get("text")) == "same":
                    key = f"twin:{one['id']}:{two['id']}"
                    found.append(_item(
                        "twin", key,
                        f"The same instruction \"{routing._word_clip(one['text'], 80)}\" is on both "
                        f"{entry.get('label')} and {other_entry.get('label')}.",
                        yes="Keep both; stop asking about this pair.", confirm=None, keeps=True))
    # Two share percentages on one account: the larger is the one that counts.
    by_account: dict[str, list[tuple[dict, dict]]] = {}
    for entry, exc in pairs:
        if exc.get("share_pct") is not None:
            by_account.setdefault(entry["account"], []).append((entry, exc))
    for account, rows in by_account.items():
        if len({exc["share_pct"] for _, exc in rows}) > 1:
            rows.sort(key=lambda r: r[1].get("at") or "")
            older, newest = rows[0][1], rows[-1][1]
            label = rows[0][0].get("label") or routing._label_for_key(account)
            found.append(_item(
                "share_clash", f"clash:{account}:{older['id']}",
                f"{label} has two different shares at once: {older['share_pct']:g}% and {newest['share_pct']:g}%. "
                "Only the higher one counts.",
                yes=f"End the older {older['share_pct']:g}% one.", confirm=_end_exception(version, account, older),
                account=account))
    closed = {a["key"]: a for a in view.get("accounts") or []
              if a.get("state") in (routing.PAUSED, routing.DOWN, routing.LIMITED)}
    for entry, exc in pairs:
        if entry["account"] in closed:
            found.append(_item(
                "closed_account", f"closed:{entry['account']}:{exc['id']}",
                f"\"{routing._word_clip(exc['text'], 80)}\" is on {entry.get('label')}, which is "
                f"{closed[entry['account']]['state']} right now ({_ends(exc)}).",
                yes="End it.", confirm=_end_exception(version, entry["account"], exc), account=entry["account"]))
    live = overrides.list_overrides(now=now)
    grouped: dict[tuple, list[dict]] = {}
    for row in live:
        sub = row.get("subject") or {}
        grouped.setdefault((row["account"], sub.get("kind"), sub.get("id"), sub.get("mac")), []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: r.get("at") or "")
        for old in rows[:-1]:
            found.append(_item(
                "stacked", f"stacked:{old['id']}",
                f"{routing._label_for_key(old['account'])} has two overrides for {overrides.subject_words(old['subject'])}.",
                yes="End the older one.", confirm={"action": "route.override.end", "args": {"override": old["id"]}},
                override=old["id"]))
    caps = {a["key"]: next((c.get("pct") for c in a.get("caps") or [] if c.get("kind") == "share"), None)
            for a in view.get("accounts") or []}
    for row in live:
        label = routing._label_for_key(row["account"])
        who = overrides.subject_words(row.get("subject") or {})
        end = {"action": "route.override.end", "args": {"override": row["id"]}}
        if row["account"] in closed:
            found.append(_item(
                "closed_account", f"closed-override:{row['id']}",
                f"{who[:1].upper() + who[1:]} may run ahead on {label}, which is {closed[row['account']]['state']} right now.",
                yes="End the override.", confirm=end, override=row["id"]))
        share = caps.get(row["account"])
        if row.get("ceiling_pct") is not None and share is not None and row["ceiling_pct"] > share:
            found.append(_item(
                "over_cap", f"over-cap:{row['id']}",
                f"{who[:1].upper() + who[1:]} may run ahead on {label} up to {row['ceiling_pct']:g}%, but the saved "
                f"share cap is {share:g}%, so the cap stops it first.",
                yes="End the override.", confirm=end, override=row["id"]))
        if (row.get("subject") or {}).get("kind") == "thread" and _thread_gone(row["subject"]["id"]):
            found.append(_item(
                "gone", f"gone:{row['id']}",
                f"T3 no longer shows the conversation behind the override on {label}.",
                yes="End the override.", confirm=end, override=row["id"]))
        if row.get("indefinite") and (routing._parse(row.get("review_at")) or now) <= now:
            found.append(_item(
                "review_due", f"review:{row['id']}",
                f"{who[:1].upper() + who[1:]} has been allowed to run ahead on {label} with no end, and it is time to "
                "look at it again.",
                yes=f"Keep it for another {overrides.REVIEW_DAYS} days.",
                confirm={"action": "route.override.keep", "args": {"override": row["id"]}},
                undo=end, override=row["id"]))
    return found


def _thread_gone(thread_id: str) -> bool:
    """True only when T3 answers and has no such thread (never on a failed read)."""
    from khipu import t3

    try:
        exists, definitive = t3.thread_exists_result(thread_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(definitive and not exists)


def ending_soon(view: dict, now: datetime) -> list[str]:
    from khipu import conversation_overrides as overrides, routing

    out = []
    for row in overrides.list_overrides(now=now):
        end = routing._parse(row.get("until"))
        if end is not None and end - now <= SOON:
            out.append(f"The override on {routing._label_for_key(row['account'])} ends {row['until']}.")
    return out


def walk(view: dict, now: datetime, *, leave: str | None = None, keep: str | None = None,
         restart: bool = False) -> dict:
    """One item at a time.  ``leave`` sets an item aside (it comes back after
    the rest); ``keep`` records the owner's yes to keeping a pair of twins as
    they are; ``restart`` forgets what was left and kept.  Reading writes
    nothing; the only thing saved is which items were set aside, so a walk
    can be left and picked up again."""
    state = _state()
    if restart:
        state = {"left": {}, "kept": {}}
        _save(state)
    all_items = items(view, now)
    keys = {it["key"] for it in all_items}
    if leave and leave in keys:
        state["left"][leave] = now.isoformat()
    if keep and keep in keys:
        state["kept"][keep] = now.isoformat()
    # What is no longer true needs no remembering.
    state["left"] = {k: v for k, v in state["left"].items() if k in keys}
    state["kept"] = {k: v for k, v in state["kept"].items() if k in keys}
    if leave or keep or restart:
        _save(state)
    pending = [it for it in all_items if it["key"] not in state["kept"]]
    fresh = [it for it in pending if it["key"] not in state["left"]]
    later = [it for it in pending if it["key"] in state["left"]]
    ordered = fresh + later
    out: dict = {"ok": True, "total": len(ordered), "left_for_later": len(later), "soon": ending_soon(view, now),
                 "resume": "Say 'leave it' to set an item aside; the walk can be stopped any time and picked up again "
                           "(only the items set aside are remembered)."}
    if not ordered:
        out.update(item=None, line="Nothing to clean up.")
        return out
    item = dict(ordered[0])
    item["number"] = 1
    item["of"] = len(ordered)
    item["came_back"] = item["key"] in state["left"]
    out["item"] = item
    out["line"] = (f"{item['words']} {item['yes']} (item 1 of {len(ordered)}"
                   + (f", {len(later)} set aside" if later else "") + ")")
    return out
