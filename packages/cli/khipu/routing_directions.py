"""Routing directions that never reach Khipu (routing plan section 13 E).

An owner's typed turn can carry a routing direction ("use GLM only one job
at a time") that no conversation ever writes into Khipu. Capture finds such
directions in the transcript's owner turns with the deterministic patterns
below — no model ever sees the prompt text — and opens one owed item naming
the owner's own words. The words live only in this module's private store
(the owner-turns folder: 0700, its file 0600, never synced), shown only to
the owner through the owed item. Everywhere else the direction is its key:
``(session, prompt_sha256, occurrence)``, joined to applied owner actions
whose log lines carry exactly that triple (``owner_direction`` copies it
from the recorded turn; the turn records are pruned at 24 hours, the
append-only log and the occurrence counter are not).

A direction closes only when a joined, applied owner action matches its
structured target and value, or — with no structured value — when a joined
action cites it (``facts.direction``) or the owner dismisses it. Anything
else leaves it open. When the transcript and the recorder count a prompt's
occurrences differently (a missed hook, a lost counter file), the direction
is ``unjoined``: still open, with that reason, and nothing is guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

from khipu import owner_turns
from khipu.turn_text import is_machine, owner_text

STORE_NAME = "routing-directions.json"

_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"(one|two|three|four|five|six|seven|eight|nine|ten|\d+)"
_ENTITY = r"([A-Z][A-Za-z0-9._-]*(?:\s+(?:[A-Z][A-Za-z0-9._-]*|second|main))*)"

_CONCURRENCY = (
    re.compile(rf"\buse\s+{_ENTITY}\s+only\s+{_NUM}\s+(?:jobs?|requests?|tasks?)\s+at\s+a\s+time\b", re.I),
    re.compile(rf"\b{_ENTITY}\s+(?:only|just)\s+{_NUM}\s+(?:jobs?|requests?|tasks?)\s+at\s+a\s+time\b", re.I),
    re.compile(rf"\blimit\s+{_ENTITY}\s+to\s+{_NUM}\s+(?:concurrent\s+)?(?:jobs?|requests?|tasks?)\b", re.I),
    re.compile(rf"\b{_ENTITY}\s+(?:at\s+most|no\s+more\s+than)\s+{_NUM}\s+(?:concurrent\s+)?(?:jobs?|requests?)\b", re.I),
)
_SPEND = re.compile(rf"\b{_ENTITY}\b[^.!?\n]*?\$\s*(\d+(?:\.\d+)?)", re.I)
_SHARE = re.compile(rf"\b{_ENTITY}\b[^.!?\n]*?(?:at\s+most|no\s+more\s+than|max(?:imum)?\s+of|cap(?:ped)?\s+at)\s+(\d+(?:\.\d+)?)\s*%", re.I)
_IMPERATIVE = re.compile(
    r"^(use|never\s+use|don'?t\s+use|do\s+not\s+use|stop\s+using|pause|lift|prefer|avoid|keep|set|limit|route|send)\b", re.I)
_QUESTION_START = re.compile(
    r"^(what|why|how|when|where|which|who|should|would|could|can|do|does|did|is|are|was|were)\b", re.I)
_HYPOTHETICAL = re.compile(r"^(if|suppose|imagine|hypothetically|what\s+if)\b", re.I)
_REPORTING = re.compile(r"\b(said|says|asked\s+whether|wrote|quoted|discuss\w*|thinking\s+about|wondering)\b", re.I)
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_QUOTES = (('"', '"'), ("“", "”"), ("‘", "’"))


def _num(word: str):
    word = word.lower()
    return _NUMBERS.get(word) or int(word)


def _entity(raw: str) -> str:
    return " ".join((raw or "").strip().split())


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for open_q, close_q in _QUOTES:
        at = 0
        while True:
            start = text.find(open_q, at)
            if start < 0:
                break
            end = text.find(close_q, start + 1)
            if end < 0:
                break
            spans.append((start, end + 1))
            at = end + 1
    return spans


def _target(span: str) -> dict | None:
    for pattern in _CONCURRENCY:
        m = pattern.search(span)
        if m:
            return {"entity": _entity(m.group(1)), "kind": "concurrency", "value": _num(m.group(2))}
    m = _SHARE.search(span)
    if m:
        value = float(m.group(2))
        return {"entity": _entity(m.group(1)), "kind": "share",
                "value": int(value) if value == int(value) else value}
    m = _SPEND.search(span)
    if m and re.search(r"\b(week|day|month|rolling|budget|spend|cost)\b", span, re.I):
        value = float(m.group(2))
        return {"entity": _entity(m.group(1)), "kind": "spend",
                "value": int(value) if value == int(value) else value}
    m = re.match(r"(?i)(?:use|never\s+use|don'?t\s+use|do\s+not\s+use|pause|prefer|avoid)\s+(.+)", span.strip())
    if m:
        words = _entity(m.group(1)).split()
        entity = " ".join(words[:2]) if len(words) > 1 and words[1][:1].isupper() else (words[0] if words else "")
        return {"entity": entity, "kind": None, "value": None} if entity else None
    return None


def extract(text: str) -> list[dict]:
    """The routing directions in one owner turn: ``[{words, target}]``.

    Abstains on questions, quotations, hypotheticals and reported speech.
    A span's words are a byte-identical slice of the turn as typed."""
    if not text or is_machine(text, anywhere=True):
        return []
    quoted = _quoted_spans(text)
    out = []
    for match in _SENTENCE.finditer(text):
        span = match.group(0)
        words = span.strip()
        if not words:
            continue
        start = match.start() + (len(span) - len(span.lstrip()))
        if any(q[0] <= start < q[1] for q in quoted) or words[:1] in {'"', "“", "‘", ">", "'"}:
            continue
        if span.rstrip().endswith("?") or _QUESTION_START.match(words) or _HYPOTHETICAL.match(words):
            continue
        if _REPORTING.search(words):
            continue
        structured = any(p.search(words) for p in _CONCURRENCY) or _SHARE.search(words) \
            or (_SPEND.search(words) and re.search(r"\b(week|day|month|rolling|budget|spend|cost)\b", words, re.I))
        if not structured and not _IMPERATIVE.match(words):
            continue
        target = _target(words)
        if structured and target is None:
            continue
        out.append({"words": words, "target": target})
    return out


# ---- the private store ---------------------------------------------------


def store_path() -> Path:
    return owner_turns.folder() / STORE_NAME


def _load() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"directions": {}}
    return data if isinstance(data, dict) and isinstance(data.get("directions"), dict) else {"directions": {}}


def _save(data: dict) -> None:
    owner_turns._ensure_folder()
    owner_turns._write_private(store_path(), json.dumps(data, indent=1) + "\n")


def _direction_id(session: str, sha: str, occurrence: int, words: str) -> str:
    raw = f"{session}\0{sha}\0{occurrence}\0{words}"
    return "rd-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def forget_sessions(sessions) -> None:
    names = {s for s in sessions if s}
    if not names:
        return
    data = _load()
    kept = {k: v for k, v in data["directions"].items() if v.get("session") not in names}
    if len(kept) != len(data["directions"]):
        _save({**data, "directions": kept})


def directions() -> list[dict]:
    return sorted(_load()["directions"].values(), key=lambda d: (d.get("session") or "", d.get("occurrence") or 0,
                                                                 d.get("id") or ""))


def dismiss(direction_id: str) -> dict | None:
    """The owner dismisses a direction: it stays dismissed on later captures."""
    data = _load()
    row = data["directions"].get(direction_id)
    if row is None:
        return None
    row["status"] = "dismissed"
    _save(data)
    return row


def owed_items() -> list[dict]:
    """The open directions as owed items, naming the owner's own words.
    These are the only rows that carry prompt text, and they are shown
    only to the owner."""
    out = []
    for row in directions():
        if row.get("status") != "open":
            continue
        out.append({"id": row["id"], "kind": "routing_direction",
                    "text": f"Routing said here, not in Khipu: '{row['words']}'",
                    "words": row["words"], "session": row["session"], "target": row.get("target"),
                    "join": row.get("join") or "joined", "reason": row.get("reason")})
    return out


# ---- correlation with applied owner actions -------------------------------


def _action_target(line: dict) -> dict | None:
    facts = line.get("facts") if isinstance(line.get("facts"), dict) else {}
    after = line.get("after") if isinstance(line.get("after"), dict) else {}
    for source in (facts.get("cap"), facts, after):
        if not isinstance(source, dict):
            continue
        kind = source.get("kind")
        value = source.get("value", source.get("n"))
        entity = source.get("account") or source.get("entity") or line.get("target")
        if kind and value is not None and entity:
            return {"entity": str(entity), "kind": kind, "value": value}
    return None


def _matches(direction: dict, line: dict) -> bool:
    facts = line.get("facts") if isinstance(line.get("facts"), dict) else {}
    if facts.get("direction") == direction["id"]:
        return True
    target = direction.get("target") or {}
    if not target.get("kind"):
        return False
    got = _action_target(line)
    if got is None or got["kind"] != target["kind"]:
        return False
    try:
        if float(got["value"]) != float(target["value"]):
            return False
    except (TypeError, ValueError):
        return False
    want = unicodedata.normalize("NFC", target["entity"]).lower()
    have = unicodedata.normalize("NFC", got["entity"]).lower()
    return want in have or have in want


def _applied_actions(actions: list[dict] | None) -> list[dict]:
    if actions is not None:
        return actions
    from khipu import owner_direction

    return [a for a in owner_direction.actions() if a.get("state") == "applied"]


def refresh(actions: list[dict] | None = None) -> list[dict]:
    """Re-join every stored direction against the applied owner actions."""
    applied = _applied_actions(actions)
    data = _load()
    for row in data["directions"].values():
        if row.get("status") in ("dismissed", "closed"):
            continue
        row["status"] = "open"
        if row.get("join") == "unjoined":
            continue  # the count disagreement stands until a capture agrees
        joined = [a for a in applied if a.get("session") == row["session"]
                  and a.get("prompt_sha256") == row["prompt_sha256"]
                  and a.get("occurrence") == row["occurrence"]]
        match = next((a for a in joined if _matches(row, a)), None)
        if match is not None:
            row["status"] = "closed"
            row["closed_by"] = match.get("id")
    _save(data)
    return directions()


# ---- capture ----------------------------------------------------------------


def _turn_parts(raw) -> tuple[str, float | None]:
    if isinstance(raw, dict):
        at = raw.get("at", raw.get("ts"))
        return str(raw.get("text") or ""), (float(at) if isinstance(at, (int, float)) else None)
    return str(raw or ""), None


def capture_scan(session: str, turns, *, now: float | None = None, actions: list[dict] | None = None) -> list[dict]:
    """Capture's direction scan over one session's owner turns, in order.

    ``turns`` is the transcript's user turns (strings, or ``{text, at}``):
    each is cleaned to the owner's typed words the same way the recorder
    cleans them, counted by canonical hash, and any direction in it opens
    (or keeps) its owed item. Returns this session's directions."""
    now = time.time() if now is None else now
    data = _load()
    rows = data["directions"]
    recorded = {}
    for turn in owner_turns.turns(session, now):
        if isinstance(turn.get("prompt_sha256"), str):
            recorded.setdefault(turn["prompt_sha256"], turn)
    counter = owner_turns.occurrence_state(session)
    counts: dict[str, int] = {}
    totals: dict[str, int] = {}
    for raw in turns:
        text, at = _turn_parts(raw)
        typed = owner_text(text)
        if typed is None or not typed.strip() or is_machine(typed, anywhere=True):
            continue
        sha = owner_turns.prompt_sha256(typed)
        known = recorded.get(sha)
        if (known is not None and (known.get("delegated") or known.get("machine"))) \
                or owner_turns.brief_for(typed) is not None:
            continue  # a dispatched job's prompt is never the owner's direction
        counts[sha] = counts.get(sha, 0) + 1
        totals[sha] = counts[sha]
        for found in extract(typed):
            key = _direction_id(session, sha, counts[sha], found["words"])
            if key in rows:
                continue  # a dismissal (or a close) survives the next capture
            rows[key] = {"id": key, "session": session, "prompt_sha256": sha,
                         "occurrence": counts[sha], "words": found["words"], "target": found["target"],
                         "status": "open", "join": "joined", "at": at}
    for row in rows.values():
        if row.get("session") != session or row.get("status") in ("dismissed", "closed"):
            continue
        sha = row["prompt_sha256"]
        reason = None
        if counter is None:
            if recorded or any(r.get("session") == session for r in rows.values()):
                reason = "the occurrence counter is gone, so this direction cannot be joined"
        elif counter.get("lost"):
            recreated = counter.get("recreated_at")
            at = row.get("at")
            if not isinstance(at, (int, float)) or recreated is None or at < recreated:
                reason = "the occurrence counter was lost, so this direction cannot be joined"
        if reason is None and counter is not None and not counter.get("lost"):
            counted = int((counter.get("counts") or {}).get(sha) or 0)
            if sha in totals and counted != totals[sha]:
                reason = "the recorder and the transcript count this prompt differently"
        if reason is not None:
            row["join"] = "unjoined"
            row["reason"] = reason
    _save(data)
    refresh(actions)
    return [r for r in directions() if r.get("session") == session]
