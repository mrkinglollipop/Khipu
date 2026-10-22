"""Routing directions that never reach Khipu (routing plan section 13 E).

An owner's typed turn can carry a routing direction ("use GLM only one job
at a time") that no conversation ever writes into Khipu. Capture finds such
directions in the transcript's owner turns with the deterministic patterns
below — no model ever sees the prompt text — and opens one owed item naming
the owner's own words. The words live only in this module's private store
(the owner-turns folder: 0700, its file 0600, never synced), shown only to
the owner through the owed item. The pushed memory slice never carries
the words: in the direction's own project (and never for a dispatched
job) it carries only a count and the direction ids, pointing at
``khipu owed``. Everywhere else the direction is its key:
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
import contextlib
import fcntl
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
_QUOTES = (('"', '"'), ("“", "”"), ("‘", "’"))


def _sentences(text: str):
    """(offset, span) per sentence. A dot between two digits (a version
    such as 3.11) never ends a sentence."""
    start = 0
    for i, char in enumerate(text):
        if char == "\n":
            if text[start:i].strip():
                yield start, text[start:i]
            start = i + 1
        elif char in ".!?" and not (
                char == "." and 0 < i < len(text) - 1
                and text[i - 1].isdigit() and text[i + 1].isdigit()):
            yield start, text[start:i + 1]
            start = i + 1
    if text[start:].strip():
        yield start, text[start:]


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


def _norm_entity(name) -> str:
    text = unicodedata.normalize("NFC", str(name or "")).lower()
    text = re.sub(r"[_:\-|/]+", " ", text)
    return " ".join(re.sub(r"[^a-z0-9. ]+", " ", text).split())


def known_entities() -> dict:
    """The names a direction may name, from Khipu's routing record:
    ``{"all": {norm: canonical}, "accounts": {norm: canonical}}`` where
    accounts also holds providers and lanes (the entities a structured
    target may name) and all also holds provider model ids. Nothing here
    is a static list: with no record, nothing is known and extract
    abstains. Fail-open to empty — a broken record opens no items."""
    all_names: dict[str, str] = {}
    accounts: dict[str, str] = {}

    def add(name, *, account: bool = False, canonical: str | None = None) -> None:
        norm = _norm_entity(name)
        if not norm:
            return
        shown = canonical or _entity(str(name))
        all_names.setdefault(norm, shown)
        if account:
            accounts.setdefault(norm, shown)

    try:
        from khipu import routing

        for acct in routing.accounts():
            label = acct.get("label")
            if label:
                add(label, account=True)
            ident = acct.get("id")
            if ident:
                add(ident, account=True, canonical=label or _entity(str(ident).replace(":", " ")))
            if acct.get("provider"):
                add(acct["provider"], account=True)
        try:
            definitions, _cache = routing._entry_definitions()
        except Exception:  # noqa: BLE001 - definitions are one source among several
            definitions = {}
        for provider in (definitions.get("providers") or {}).values():
            if not isinstance(provider, dict):
                continue
            label = provider.get("label") or provider.get("id")
            if provider.get("id"):
                add(provider["id"], account=True, canonical=label)
            if label:
                add(label, account=True)
            for lane in provider.get("lane_names") or []:
                add(lane, account=True)
            for model in (provider.get("models_allowed") or []) + (provider.get("models_never") or []):
                add(model)
        for acct in (definitions.get("accounts") or {}).values():
            if not isinstance(acct, dict):
                continue
            label = acct.get("label") or acct.get("id")
            if label:
                add(label, account=True)
            if acct.get("id"):
                add(acct["id"], account=True, canonical=label)
            if acct.get("provider"):
                add(acct["provider"], account=True)
        for lanes in (definitions.get("lane_maps") or {}).values():
            if isinstance(lanes, dict):
                for lane in lanes:
                    add(lane, account=True)
    except Exception:  # noqa: BLE001 - no readable record, no known names
        return {"all": {}, "accounts": {}}
    return {"all": all_names, "accounts": accounts}


def _coerce_known(entities) -> dict:
    if isinstance(entities, dict) and "all" in entities:
        return entities
    if entities is None:
        return known_entities()
    names = {}
    for name in entities:
        norm = _norm_entity(name)
        if norm:
            names[norm] = _entity(str(name))
    return {"all": dict(names), "accounts": dict(names)}


def _find_known(text: str, names: dict) -> str | None:
    """The canonical known name in ``text``: earliest, then longest.
    Whole-phrase matching on the normalized text, never a substring of
    a longer name ('Claude' does not match inside 'Claude second')."""
    norm = _norm_entity(text)
    best = None
    for key, canonical in names.items():
        m = re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", norm)
        if m and (best is None or m.start() < best[0] or (m.start() == best[0] and len(key) > len(best[1]))):
            best = (m.start(), key, canonical)
    return best[2] if best else None


def _target(span: str, known: dict) -> dict | None:
    accounts = known["accounts"]
    for pattern in _CONCURRENCY:
        m = pattern.search(span)
        if m:
            canonical = accounts.get(_norm_entity(m.group(1)))
            return {"entity": canonical, "kind": "concurrency", "value": _num(m.group(2))} if canonical else None
    m = _SHARE.search(span)
    if m:
        canonical = _find_known(span, accounts)
        if not canonical:
            return None
        value = float(m.group(2))
        return {"entity": canonical, "kind": "share",
                "value": int(value) if value == int(value) else value}
    m = _SPEND.search(span)
    if m and re.search(r"\b(week|day|month|rolling|budget|spend|cost)\b", span, re.I):
        # A spend target needs a real account entity from the record;
        # words before the $ that name none open nothing.
        canonical = _find_known(span, accounts)
        if not canonical:
            return None
        value = float(m.group(2))
        return {"entity": canonical, "kind": "spend",
                "value": int(value) if value == int(value) else value}
    canonical = _find_known(span, known["all"])
    return {"entity": canonical, "kind": None, "value": None} if canonical else None


def extract(text: str, entities=None) -> list[dict]:
    """The routing directions in one owner turn: ``[{words, target}]``.

    A direction must name a known account, provider, lane or model from
    the routing record (``entities`` overrides that set, for tests), so
    ordinary prompts that happen to start with a routing verb open
    nothing. Abstains on questions, quotations, hypotheticals and
    reported speech. A span's words are a byte-identical slice of the
    turn as typed."""
    if not text or is_machine(text, anywhere=True):
        return []
    known = _coerce_known(entities)
    if not known["all"]:
        return []
    quoted = _quoted_spans(text)
    out = []
    for start0, span in _sentences(text):
        words = span.strip()
        if not words:
            continue
        start = start0 + (len(span) - len(span.lstrip()))
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
        target = _target(words, known)
        if target is None:
            continue
        out.append({"words": words, "target": target})
    return out


# ---- the private store ---------------------------------------------------


def store_path() -> Path:
    return owner_turns.folder() / STORE_NAME


@contextlib.contextmanager
def _store_lock():
    owner_turns._ensure_folder()
    with store_path().with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _valid_row(key, row):
    if not isinstance(row, dict) or row.get("id") != key:
        return False
    if not all(isinstance(row.get(k), str) for k in ("id", "session", "words", "status")):
        return False
    if "occurrence" in row and (type(row["occurrence"]) is not int or row["occurrence"] < 1):
        return False
    return row.get("target") is None or isinstance(row["target"], dict)


def _load(*, strict: bool = False) -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"directions": {}}
    except (OSError, ValueError):
        if strict:
            raise
        return {"directions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("directions"), dict):
        if strict:
            raise ValueError("invalid routing direction store")
        return {"directions": {}}
    if strict and any(not _valid_row(key, row) for key, row in data["directions"].items()):
        raise ValueError("invalid routing direction row")
    data["directions"] = {key: row for key, row in data["directions"].items() if _valid_row(key, row)}
    return data


def _save(data: dict) -> None:
    owner_turns._ensure_folder()
    owner_turns._write_private(store_path(), json.dumps(data, indent=1) + "\n")


def _direction_id(session: str, sha: str, occurrence: int, words: str) -> str:
    raw = f"{session}\0{sha}\0{occurrence}\0{words}"
    return "rd-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


QUARANTINE_NAME = "routing-directions.quarantine.json"


def _quarantine_path() -> Path:
    return store_path().with_name(QUARANTINE_NAME)


def _raw_store() -> dict:
    """The store as written, malformed rows included. A missing store is
    empty; an unreadable or wrong-shaped file raises."""
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"directions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("directions"), dict):
        raise ValueError("invalid routing direction store")
    return data


def malformed_rows() -> list[dict]:
    """The rows the store cannot use, as ``{key, reason}`` (never their
    words), for doctor and for the repair. An unreadable store is one
    entry keyed ``(store)``."""
    try:
        data = _raw_store()
    except (OSError, ValueError) as err:
        return [{"key": "(store)", "reason": f"{type(err).__name__}: {err}"[:200]}]
    return [{"key": str(key), "reason": "not a valid direction row"}
            for key, row in data["directions"].items() if not _valid_row(key, row)]


def _session_of(row) -> str | None:
    return row.get("session") if isinstance(row, dict) and isinstance(row.get("session"), str) else None


def repair_store() -> list[str]:
    """Move every malformed row out of the store into a private quarantine
    file, so a forget no longer stops on it. Every row keeps its full content
    there (owner_files lists the file as persistent, so the sweep leaves it,
    and a later forget of a session scrubs that session's words from it).
    The valid rows are untouched.
    Returns the keys moved. An unreadable store file is left alone."""
    with _store_lock():
        try:
            data = _raw_store()
        except (OSError, ValueError):
            return []
        bad = {k: v for k, v in data["directions"].items() if not _valid_row(k, v)}
        if not bad:
            return []
        try:
            held = json.loads(_quarantine_path().read_text(encoding="utf-8"))
            held = held if isinstance(held, dict) else {}
        except (OSError, ValueError):
            held = {}
        for key, row in bad.items():
            held[str(key)] = row
        owner_turns._write_private(_quarantine_path(), json.dumps(held, indent=1) + "\n")
        _save({**data, "directions": {k: v for k, v in data["directions"].items() if k not in bad}})
        return sorted(str(k) for k in bad)


def quarantined_keys() -> list[str]:
    """The keys held in quarantine (doctor stays red while any remain). An
    unreadable quarantine file is one entry keyed ``(quarantine)``."""
    try:
        held = json.loads(_quarantine_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return ["(quarantine)"]
    return sorted(str(k) for k in held) if isinstance(held, dict) else ["(quarantine)"]


def _scrub_quarantine(names) -> None:
    try:
        held = json.loads(_quarantine_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    if not isinstance(held, dict):
        _quarantine_path().unlink()
        return
    # A row whose session cannot be read is judged by its text, as a temp is.
    kept = {k: v for k, v in held.items()
            if not any(n in json.dumps(v, ensure_ascii=False) for n in names)}
    if len(kept) != len(held):
        if kept:
            owner_turns._write_private(_quarantine_path(), json.dumps(kept, indent=1) + "\n")
        else:
            _quarantine_path().unlink()


def forget_sessions(sessions) -> None:
    """Remove the forgotten sessions' directions. A malformed row that names
    a forgotten session goes with them; any other malformed row cannot be
    shown clear of those words, so the valid rows are scrubbed and saved, and
    the error then names the rows left (``repair_store`` moves them aside),
    keeping the forget incomplete rather than claiming it done."""
    names = {s for s in sessions if s}
    if not names:
        return
    with _store_lock():
        _scrub_quarantine(names)
        data = _raw_store()
        kept = {k: v for k, v in data["directions"].items() if _session_of(v) not in names}
        if len(kept) != len(data["directions"]):
            _save({**data, "directions": kept})
        left = sorted(str(k) for k, v in kept.items() if not _valid_row(k, v))
    if left:
        raise ValueError("malformed routing direction row(s) " + ", ".join(left)
                         + " could not be checked for the forgotten session's words; "
                         "repair them (khipu doctor names them) and forget again")


def directions() -> list[dict]:
    return sorted(_load()["directions"].values(), key=lambda d: (d.get("session") or "", d.get("occurrence") or 0,
                                                                 d.get("id") or ""))


def dismiss(direction_id: str) -> dict | None:
    """The owner dismisses a direction: it stays dismissed on later captures."""
    with _store_lock():
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
    from datetime import datetime, timezone

    out = []
    for row in directions():
        if row.get("status") != "open":
            continue
        out.append({"id": row["id"], "kind": "routing_direction",
                    "text": f"Routing said here, not in Khipu: '{row['words']}'",
                    "words": row["words"], "session": row["session"], "target": row.get("target"),
                    "project": row.get("project"),
                    "opened_at": (datetime.fromtimestamp(row["at"], timezone.utc).isoformat()
                                  if type(row.get("at")) in (int, float) else None),
                    "join": row.get("join") or "joined", "reason": row.get("reason")})
    return out


def _project_from_cwd(cwd) -> str | None:
    if not cwd:
        return None
    try:
        from khipu.identity import resolve_repo_root

        return resolve_repo_root(str(cwd)).get("project") or None
    except Exception:  # noqa: BLE001 - an unresolvable cwd scopes nothing
        return None


def _project_for(session, sha=None) -> str | None:
    """A direction session's own project, from its recorded turns' cwd.
    None when the turns are gone or their cwd resolves to no project:
    the direction then scopes to no slice at all (fail closed)."""
    try:
        turns = owner_turns.turns(session)
    except Exception:  # noqa: BLE001 - no readable turns, no project
        return None
    with_cwd = [t for t in turns if t.get("cwd")]
    if sha:
        matched = [t for t in with_cwd if t.get("prompt_sha256") == sha]
        with_cwd = matched or with_cwd
    for turn in reversed(with_cwd):
        project = _project_from_cwd(turn.get("cwd"))
        if project:
            return project
    return None


def pushed_slice_ids(project, *, job: bool = False) -> list[str]:
    """The open directions a pushed slice may name for ``project``: ids
    only, never the words. A dispatched job's slice names none, and a
    direction names only in its own project's slice."""
    if job or not project:
        return []
    own: dict[str, str | None] = {}
    ids = []
    for row in directions():
        if row.get("status") != "open":
            continue
        session = row.get("session")
        if session not in own:
            own[session] = row.get("project") or _project_for(session, row.get("prompt_sha256"))
        if own[session] and own[session] == project:
            ids.append(row["id"])
    return ids


# ---- correlation with applied owner actions -------------------------------


def _action_target(line: dict) -> dict | None:
    facts = line.get("facts") if isinstance(line.get("facts"), dict) else {}
    after = line.get("after") if isinstance(line.get("after"), dict) else {}
    for source in (facts.get("cap"), facts, after):
        if not isinstance(source, dict):
            continue
        kind = source.get("kind")
        field = {"share": "pct", "spend": "usd", "concurrency": "n"}.get(kind, "value")
        value = source.get(field, source.get("value"))
        entity = source.get("account") or source.get("entity") or line.get("target")
        if kind and value is not None and entity:
            return {"entity": str(entity), "kind": kind, "value": value}
    return None


def _entity_key(name, known: dict | None = None) -> str:
    """One entity's comparison key: exact after normalization and after
    resolving a stable id or key to its known name, so 'Claude' and
    'Claude second' are different keys in both directions."""
    norm = _norm_entity(name)
    if known:
        accounts = known.get("accounts") or {}
        if norm in accounts:
            return norm
        # A stable id (claude:second) or a usage key (claude|home) names
        # the account its parts name, when the record knows that name.
        for variant in (norm.replace(" ", " "), norm.split(" ")[0],
                        " ".join(norm.split(" ")[1:])):
            if variant in accounts:
                return variant
    return norm


def _matches(direction: dict, line: dict, known: dict | None = None) -> bool:
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
    known = known if known is not None else known_entities()
    return _entity_key(target["entity"], known) == _entity_key(got["entity"], known)


def _applied_actions(actions: list[dict] | None) -> list[dict]:
    if actions is not None:
        return actions
    from khipu import owner_direction

    return [a for a in owner_direction.actions() if a.get("state") == "applied"]


def _join_reason(row: dict, counter: dict | None) -> str | None:
    """Why this direction cannot be joined, or None when it can. Recomputed
    on every capture and refresh, never sticky: a turn record that simply
    arrives after a capture clears the reason on the next pass."""
    if counter is None:
        return "the occurrence counter is gone, so this direction cannot be joined"
    if counter.get("lost"):
        recreated = counter.get("recreated_at")
        at = row.get("at")
        if not isinstance(at, (int, float)) or recreated is None or at < recreated:
            return "the occurrence counter was lost, so this direction cannot be joined"
    counted = int((counter.get("counts") or {}).get(row.get("prompt_sha256")) or 0)
    if counted < int(row.get("occurrence") or 0):
        return "the recorder and the transcript count this prompt differently"
    return None


def refresh(actions: list[dict] | None = None) -> list[dict]:
    """Re-join every stored direction against the applied owner actions."""
    with _store_lock():
        return _refresh_locked(actions)


def _refresh_locked(actions):
    applied = _applied_actions(actions)
    known = None  # loaded on the first structured match candidate
    counters: dict[str, dict | None] = {}
    data = _load()
    for row in data["directions"].values():
        if row.get("status") in ("dismissed", "closed"):
            continue
        row["status"] = "open"
        session = row.get("session")
        if session not in counters:
            counters[session] = owner_turns.occurrence_state(session)
        reason = _join_reason(row, counters[session])
        if reason is not None:
            row["join"] = "unjoined"
            row["reason"] = reason
            continue
        row["join"] = "joined"
        row.pop("reason", None)
        joined = [a for a in applied if a.get("session") == row["session"]
                  and a.get("prompt_sha256") == row["prompt_sha256"]
                  and a.get("occurrence") == row["occurrence"]]
        if joined and known is None:
            known = known_entities()
        match = next((a for a in joined if _matches(row, a, known)), None)
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
    with _store_lock():
        return _capture_scan_locked(session, turns, now=now, actions=actions)


def _capture_scan_locked(session, turns, *, now, actions):
    now = time.time() if now is None else now
    data = _load()
    rows = data["directions"]
    recorded: dict[str, dict] = {}
    recorded_occs: dict[str, list[int]] = {}
    for turn in owner_turns.turns(session, now):
        if isinstance(turn.get("prompt_sha256"), str):
            recorded.setdefault(turn["prompt_sha256"], turn)
            if isinstance(turn.get("occurrence"), int):
                recorded_occs.setdefault(turn["prompt_sha256"], []).append(turn["occurrence"])
    known = known_entities()
    # First pass: the eligible typed turns in this scanned window, in
    # order. Occurrences come from the owner turn record (the durable
    # per-session counter the recorder numbered), never from counting
    # within this window alone: a window holding only the second typing
    # of a prompt takes the recorder's occurrence 2, so it opens the
    # second direction and only its own action can close it. A scan
    # holding k typings takes the recorder's last k occurrences; when
    # the record holds fewer (a missed hook, pruned turn records),
    # transcript numbering is the placeholder and the join check below
    # marks the direction unjoined until the record agrees.
    typed_turns: list[tuple[str, float | None, str]] = []
    for raw in turns:
        text, at = _turn_parts(raw)
        typed = owner_text(text)
        if typed is None or not typed.strip() or is_machine(typed, anywhere=True):
            continue
        sha = owner_turns.prompt_sha256(typed)
        seen = recorded.get(sha)
        if (seen is not None and (seen.get("delegated") or seen.get("machine"))) \
                or owner_turns.brief_for(typed) is not None:
            continue  # a dispatched job's prompt is never the owner's direction
        typed_turns.append((typed, at, sha))
    totals: dict[str, int] = {}
    for _typed, _at, sha in typed_turns:
        totals[sha] = totals.get(sha, 0) + 1
    assigned: dict[str, list[int]] = {}
    for sha, total in totals.items():
        occs = sorted(recorded_occs.get(sha) or [])
        assigned[sha] = occs[-total:] if len(occs) >= total else list(range(1, total + 1))
    counts: dict[str, int] = {}
    for typed, at, sha in typed_turns:
        counts[sha] = counts.get(sha, 0) + 1
        occurrence = assigned[sha][counts[sha] - 1]
        for found in extract(typed, known):
            key = _direction_id(session, sha, occurrence, found["words"])
            if key in rows:
                continue  # a dismissal (or a close) survives the next capture
            stale = next((k for k, r in list(rows.items())
                          if r.get("session") == session and r.get("prompt_sha256") == sha
                          and r.get("words") == found["words"] and r.get("status") == "open"
                          and r.get("join") == "unjoined" and r.get("occurrence") != occurrence
                          and len(recorded_occs.get(sha) or []) >= totals[sha]), None)
            if stale is not None:
                # The turn record arrived after an earlier capture had
                # numbered this direction from the window alone: re-key
                # it to the recorder's occurrence so it can join.
                row = rows.pop(stale)
                row.update({"id": key, "occurrence": occurrence})
                rows[key] = row
                continue
            rows[key] = {"id": key, "session": session, "prompt_sha256": sha,
                         "occurrence": occurrence, "words": found["words"], "target": found["target"],
                         "project": _project_from_cwd((recorded.get(sha) or {}).get("cwd")),
                         "status": "open", "join": "joined", "at": at}
    for row in rows.values():
        if row.get("session") == session and not row.get("project"):
            row["project"] = _project_from_cwd((recorded.get(row.get("prompt_sha256")) or {}).get("cwd"))
    counter = owner_turns.occurrence_state(session)
    for row in rows.values():
        if row.get("session") != session or row.get("status") in ("dismissed", "closed"):
            continue
        reason = _join_reason(row, counter)
        if reason is not None:
            row["join"] = "unjoined"
            row["reason"] = reason
        else:
            row["join"] = "joined"
            row.pop("reason", None)
    _save(data)
    _refresh_locked(actions)
    return [r for r in directions() if r.get("session") == session]
