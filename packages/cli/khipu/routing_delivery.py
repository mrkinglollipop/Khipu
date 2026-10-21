"""Local hook delivery ledger. Reservations are separate from acknowledgements.

The ledger has its own local file and lock beside recall's session marker. It never changes
routing events or their fold. Pull clients attach to this seam in slice 7.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import os
import sys
import tempfile
import time
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

def _clock():
    # A cold hook must not import the routing engine to acknowledge a batch.
    engine = sys.modules.get("khipu.routing")
    return engine._now() if engine is not None else datetime.now(timezone.utc)

def _iso(now):
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# Per-agent ledgers live inside the parent session's one file. An agent only
# needs what it still owes, so finished work is kept as revision-only proof
# and whole agent components expire by age and count. The byte ceiling stays
# below the 1 MB limit at which foreign-batch scans stop reading a file.
_AGENT_MAX_COMPONENTS = 16
_AGENT_STALE_SECONDS = 7 * 24 * 3600
_AGENT_BATCHES_KEPT = 32
_SESSION_FILE_BUDGET = 900_000


def _component_time(state):
    if not isinstance(state, dict):
        return None
    values = [state.get("touched"), state.get("last_delivery_point"),
              (state.get("flight") or {}).get("until") if isinstance(state.get("flight"), dict) else None]
    parsed = []
    for value in values:
        if isinstance(value, str):
            try:
                parsed.append(_parse(value))
            except ValueError:
                pass
    return max(parsed) if parsed else None


def _slim_agent_state(state):
    entries = state.get("entries")
    if not isinstance(entries, dict):
        return
    done = state.get("done") if isinstance(state.get("done"), dict) else {}
    for entity, entry in list(entries.items()):
        text = entry.get("text") or ""
        if entry.get("acked") and entry.get("offset", 0) >= len(text):
            done[entity] = entry.get("revision")
            del entries[entity]
    if done:
        state["done"] = done
    batches = state.get("batches")
    if isinstance(batches, dict):
        for old in list(batches)[:-_AGENT_BATCHES_KEPT]:
            del batches[old]


def _prune_agent_components(data, now=None, keep=None):
    now = now or _clock()
    agents = [name for name, state in data.items()
              if name.startswith("agent:") and isinstance(state, dict)]
    for name in agents:
        _slim_agent_state(data[name])

    def live_flight(state):
        flight = state.get("flight")
        return bool(flight and isinstance(flight, dict) and flight.get("until")
                    and _parse(flight["until"]) > now)

    def finished(name):
        state = data[name]
        return not state.get("entries") and not live_flight(state)

    def stale(name):
        seen = _component_time(data[name])
        return bool(seen and (now - seen).total_seconds() > _AGENT_STALE_SECONDS)

    def drop_order(name):
        seen = _component_time(data[name])
        return (0 if finished(name) else 1 if stale(name) else 2,
                seen.timestamp() if seen else 0.0)

    for name in list(agents):
        if name != keep and stale(name) and not live_flight(data[name]):
            del data[name]
    agents = [name for name in agents if name in data]
    while len(agents) > _AGENT_MAX_COMPONENTS:
        droppable = sorted((name for name in agents if name != keep), key=drop_order)
        if not droppable:
            break
        del data[droppable[0]]
        agents.remove(droppable[0])
    while len(json.dumps(data).encode()) > _SESSION_FILE_BUDGET:
        droppable = sorted((name for name in agents if name != keep and name in data), key=drop_order)
        if not droppable:
            break
        del data[droppable[0]]
        agents.remove(droppable[0])


class Marker(dict):
    """Keep local delivery identity off the serialized version marker."""

    def __init__(self, value=(), *, session=None, component="main"):
        super().__init__(value)
        self.session = session
        self.component = component

def _size(text):
    return len(text) + sum(ord(c) > 0xffff for c in text)


def _path(session):
    from khipu import recall_prompt

    session = hook_session(session)
    # A collision-resistant name for the complete canonical identity:
    # sanitizing or truncating it made distinct identities share a file.
    digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
    return recall_prompt._dedup_path("ledger").parent / f"recall--{digest}.routing.json"


@contextlib.contextmanager
def _transaction(session):
    path = _path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        end = time.monotonic() + 0.05
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise OSError("routing delivery busy")
                time.sleep(0.005)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        yield data
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name, delete=False) as f:
            temporary = f.name
            json.dump(data, f)
        try:
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

def snapshot(session, component="main"):
    try:
        data = json.loads(_path(session).read_text())
    except (OSError, ValueError):
        return {}
    return data.get(component, {})

def notice_receipts(session):
    """Completed notice deliveries through any component of this conversation."""
    try:
        data = json.loads(_path(session).read_text())
    except (OSError, ValueError):
        return set()
    return {entity.removeprefix("notice:") for state in data.values() if isinstance(state, dict)
            for entity, entry in state.get("entries", {}).items()
            if entity.startswith("notice:") and entry.get("acked")}


def plan(out, view, now, cfg, *, session=None, project=None):
    """Attach current, scrubbed entity revisions to a hook's rendered offer."""
    from khipu import routing as r

    full = out.get("kind") == "full"
    entities = list(out.get("owed_entities") or [])
    if full and view.get("record"):
        entities = list(dict.fromkeys([*entities, *[e for e in view.get("effective", {})
                                                  if not e.startswith("owner:")]]))
    # Other entities can change the suffix of an unchanged rule or priority.
    entities = list(dict.fromkeys([*entities, *[e for e in view.get("effective", {})
                                               if e.startswith("rule:") or e in ("note", "priority")]]))
    rules = {r._rule_component(rule): rule for rule in view.get("rules") or []}
    items = []
    facts = r.plan_facts(view, now)
    text = out.get("text") or ""
    shown = set(out.get("shown_entities") or [])
    for entity in entities:
        rule = rules.get(entity)
        whole = entity in ("note", "priority") or entity.startswith("rule:")
        if rule:
            line = r.rule_line(rule, view=view, facts=facts)
            label = f"Standing rule {rule['n']}"
            names = r._named_account_labels(rule.get("text"), view)
            if names:
                label += " for " + ", ".join(names)
        elif entity == "note" and view.get("note"):
            line, label = r.note_line(view["note"], now), "Owner note"
        else:
            line, label = r._current_line(entity, view, now), entity.split(":", 1)[0]
        effective = view.get("effective", {}).get(entity)
        if effective is None:
            effective = line
        items.append(dict(entity=entity, effective=effective, text=line, label=label, whole=whole))
    for entity, fc in (out.get("pace_forecasts") or {}).items():
        entry = next((a for a in view.get("accounts") or [] if a["key"] == fc["account"]), None)
        line = r.pace_line_for(entry, now) if entry else None
        if line:
            items.append(dict(entity=entity, effective=line, text=line, label=fc["label"], whole=False))
    for entity, ad in (out.get("advice_forecasts") or {}).items():
        from khipu import routing_advice

        line = routing_advice.advice_line(ad)
        items.append(dict(entity=entity, effective=line, text=line, label=ad.get("label") or entity, whole=False))
    raw = [(item["text"], item["label"]) for item in items]
    projected = (project(raw) if project else r._for_agents(raw, view)) if items else []
    for item, (line, label) in zip(items, projected):
        effective = item.pop("effective")
        if project:
            # A projection is not evidence that the source characters were
            # delivered. Withheld/changed entries stay owed at offset zero.
            from khipu import agent_view

            # A token removed from current owner words is safe to deliver;
            # all other projection changes remain unacknowledgeable.
            words = agent_view.keep_owner_words(view)
            expected = tuple(agent_view.redact_owner_tokens(value, words)
                             for value in (item["text"], item["label"]))
            item["withheld"] = (line, label) != expected
        # Relative account ages are presentation, not a new change. Whole
        # entries also depend on their safe text and rendered suffixes.
        fingerprint = [effective, line, label] if item["entity"].startswith("rule:") or item["entity"] in ("note", "priority") else effective
        revision = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        item.update(text=line, label=label, revision=revision)
        # Rules and notes require their complete words. Accounts and holds
        # may use the existing renderer's identifying line projection.
        visible = line in text if item["whole"] else item["entity"] in shown
        if full and not item["whole"]:
            width = cfg["routing_line_chars"]
            clipped = line[:max(0, width - 3)].rstrip() + "." * min(width, 3) if width and len(line) > width else line
            visible = bool(line and (line in text or clipped in text or r._clip(line, width) in text))
        item["visible"] = visible
    notices = r._replacement_notices(view, now, session)
    for _kind, raw, ident in notices:
        line = r._for_agents(raw, view)
        items.append(dict(entity="notice:" + ident, revision=ident, text=line,
                          label="Proposal notice", whole=True, visible=ident in (out.get("notices") or [])))
    removed = {e: r.change_line(e, view, now) for e in view.get("causes", {})
               if e not in view.get("effective", {}) and (e == "note" or e.startswith("rule:"))}
    return dict(out, delivery_plan={"items": items, "pace_forecasts": out.get("pace_forecasts") or {},
                                    "advice_forecasts": out.get("advice_forecasts") or {},
                                    "current_entities": list(view.get("effective", {})),
                                    "removed": removed, "notices": ["notice:" + n[2] for n in notices], "at": r._iso(now),
                                   "limit": cfg["routing_summary_chars"] if full else cfg["routing_prompt_chars"],
                                   "full": full, "page_limit": cfg["routing_prompt_chars"], "text": out.get("text"),
                                   "lease": cfg["routing_delivery_lease"],
                                   "heading": text.split("\n", 1)[0] if text else
                                   r.CHANGES_HEADING.format(at=r._clock(now, now))})


def _cut(text, start, width):
    end, cost = start, 0
    while end < len(text) and cost + _size(text[end]) <= width:
        cost += _size(text[end])
        end += 1
    return end

def _pages_left(text, start, width):
    count = 0
    while start < len(text):
        end = _cut(text, start, width)
        if end == start:
            return None
        start, count = end, count + 1
    return count

def _merge(state, out):
    epoch = out.get("epoch")
    if state.get("epoch") != epoch:
        state.clear()
        state.update(epoch=epoch, entries={}, batches={})
    entries = state.setdefault("entries", {})
    replaced = set()
    plan = out["delivery_plan"]
    items = list(plan["items"])
    planned = {i["entity"] for i in items}
    current = set(plan.get("current_entities", entries))
    # Notice debt ends with its seven-day window or a receipt through an
    # alternate identity. It has no removal message to deliver.
    for entity in list(entries):
        if entity.startswith("notice:") and not entries[entity].get("acked") and entity not in plan.get("notices", []):
            del entries[entity]
            replaced.add(entity)
    # SessionStart's parent has only the scrubbed plan, not the routing view.
    # Missing saved debt needs a removal message, never its former words.
    for entity, old in entries.items():
        if not old.get("acked") and entity not in planned and entity not in current:
            fallback = ("The owner's note ended" if entity == "note" else
                        old["label"] + (" retired" if entity.startswith("rule:") else " is no longer current"))
            line = plan.get("removed", {}).get(entity, fallback)
            fingerprint = [line, line, old["label"]] if entity.startswith("rule:") or entity in ("note", "priority") else line
            revision = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            items.append(dict(entity=entity, revision=revision, text=line, label=old["label"], whole=True, visible=False))
    done = state.get("done") if isinstance(state.get("done"), dict) else {}
    for item in items:
        entity = item["entity"]
        if done.get(entity) == item["revision"]:
            continue  # already delivered to this agent; only its revision is kept
        if entity in done:
            del done[entity]  # a new revision is owed again
        old = entries.get(entity)
        if old is None or old["revision"] != item["revision"]:
            replaced.add(entity)
            entries[entity] = dict(item, offset=0, acked=False,
                                   restarted=bool(old and not old.get("acked") and
                                                  (old.get("offset") or any(i["entity"] == entity for i in
                                                   (state.get("flight") or {}).get("out", {}).get("routing_changes", {}).get("items", [])))))
        elif not old.get("offset"):
            old.update(item)
    flight = state.get("flight")
    if flight and any(i["entity"] in replaced for i in flight["out"]["routing_changes"]["items"]):
        state.pop("flight", None)
    return entries

def _render(state, out, now, component):
    entries = _merge(state, out)
    state["last_delivery_point"] = _iso(now)
    flight = state.get("flight")
    if flight and _parse(flight["until"]) > now:
        answer = copy.deepcopy(flight["out"])
        answer["generation"] = out.get("generation")
        return answer
    if flight:
        state["expired"] = state.get("expired", 0) + 1
        state.pop("flight", None)
    # A receipt can arrive after the plan was rendered, through another
    # hook or identity. Suppress only the notice lines already acknowledged.
    heard = [ident for ident in out.get("notices", []) if entries.get("notice:" + ident, {}).get("acked")]
    if heard and isinstance(out.get("text"), str):
        lines = out["text"].split("\n")
        for ident in heard:
            line = entries["notice:" + ident]["text"]
            if line in lines:
                lines.remove(line)
        plain = "\n".join(lines)
        out = dict(out, text=plain, delivery_plan=dict(out["delivery_plan"], text=plain))
        out["notices"] = [ident for ident in out.get("notices", []) if ident not in heard]
    due = [e for e in entries.values() if not e.get("acked")]
    # A new hold must precede the remaining pages of an older, long rule.
    due.sort(key=lambda e: not e["entity"].startswith("hold:"))
    if not due:
        # Informational full/stale blocks have no change debt to reserve.
        answer = {k: v for k, v in out.items() if k not in
                  ("delivery_plan", "delivery_reservation", "routing_changes", "delivery", "_delivery_view", "_reserved", "pace_forecasts", "advice_forecasts")}
        return answer
    limit = out["delivery_plan"]["limit"]
    # Keep ordinary small offers byte-identical. A preview is never enough
    # for a whole-text entity, and a hook's UTF-16 ceiling always wins.
    plain = (out["delivery_plan"].get("text") or "")
    legacy = (not out["delivery_plan"].get("force_pages")
              and _size(plain) <= out["delivery_plan"].get("max_chars", 9000) and all(not e["whole"] or e["visible"] for e in due)
              and not any(e.get("offset") or e.get("restarted") for e in due)
              and not any(e.get("acked") and e.get("visible") for e in entries.values()))
    selected, progress = [], {}
    if legacy:
        for e in due:
            if e["visible"]:
                selected.append(dict(change=e["entity"] + "@" + e["revision"],
                                     entity=e["entity"], revision=e["revision"]))
                progress[e["entity"]] = len(e["text"])
        if not selected and not plain.strip():
            legacy = False
    if not legacy:
        heading = out["delivery_plan"]["heading"]
        body, selected, progress = [], [], {}
        # Reserve the tail before fitting payload, so the cap also holds
        # when only one character fits or labels use surrogate pairs.
        cap = min(limit or 8000, out["delivery_plan"].get("max_chars", 9000) - _size(heading) - 2)
        tail = "More routing owed."
        used = 0
        for e in due:
            start, value = e.get("offset", 0), e["text"]
            restart = f"{e['label']} changed while being shown.\n" if e.get("restarted") else ""
            prefix = f"{e['label']}; revision {e['revision'][:8]}; page "
            room = cap - used - _size(tail) - 2
            if not start and not restart and _size(value) <= room:
                rendered, end, page = value, len(value), None
            else:
                # Start has a larger budget than subsequent prompts. Count
                # its larger first page plus exact future UTF-16 chunks.
                future_room = min(room, (out["delivery_plan"]["page_limit"] or room) - _size(tail) - 2)
                width = e.get("width") or max(2, future_room - _size(prefix) - 24)
                e.setdefault("width", width)
                available = room - _size(restart + prefix) - 24
                if start:
                    available = min(width, available)
                end = _cut(value, start, available)
                if end == start:
                    break
                remaining = _pages_left(value, end, width)
                if remaining is None:
                    break
                n = e.get("pages", 0) + 1
                page = {"n": n, "m": n + remaining}
                rendered = restart + prefix + f"{n}/{page['m']}\n" + value[start:end]
            if _size(rendered) > room:
                break
            item = dict(change=e["entity"] + "@" + e["revision"], entity=e["entity"], revision=e["revision"])
            if page:
                item["page"] = page
            selected.append(item)
            progress[e["entity"]] = end
            body.append(rendered)
            used += _size(rendered) + 1
            if end < len(value):
                break
        left = any(progress.get(e["entity"], e.get("offset", 0)) < len(e["text"]) for e in due)
        if left:
            body.append(tail)
        plain = heading + "\n" + "\n".join(body)
    complete = [entity for entity, end in progress.items() if end == len(entries[entity]["text"])]
    answer = {k: v for k, v in out.items() if k not in ("delivery_plan", "delivery_reservation", "_delivery_view", "_reserved", "pace_forecasts", "advice_forecasts")}
    if "notices" in out or out["delivery_plan"].get("notices"):
        answer["notices"] = [entity.removeprefix("notice:") for entity in complete
                             if entity.startswith("notice:")]
    answer.update(kind=out.get("kind") if out.get("kind") != "none" else "changes", text=plain,
                  owed_entities=[e["entity"] for e in due], shown_entities=complete,
                  routing_changes={"batch": str(uuid.uuid4()), "component": component, "items": selected})
    if not selected:
        answer.pop("routing_changes", None)
    if selected:
        state["flight"] = {"until": _iso(now + timedelta(seconds=out["delivery_plan"]["lease"])),
                           "out": copy.deepcopy(answer), "progress": progress,
                           "plan": copy.deepcopy(out["delivery_plan"])}
    answer["delivery"] = {"owed": sum(not e.get("acked") and e["entity"] not in progress for e in entries.values()),
                          "in_flight": len(selected), "acked_through": state.get("acked_through")}
    if selected:
        state["flight"]["out"] = copy.deepcopy(answer)
    return answer

def offer(out, *, session=None, component="main"):
    """Reserve one component's batch under its independent delivery lock."""
    now = _parse(out["delivery_plan"]["at"])
    if not session:
        state = {}
        answer = _render(state, out, now, component)
        # SessionStart can be built before the hook has a session id. Keep
        # its exact reservation for the eventual successful-delivery callback.
        answer["delivery_reservation"] = state
        answer["delivery_plan"] = out["delivery_plan"]
        return answer
    with _transaction(session) as data:
        state = data.setdefault(component, {})
        answer = _render(state, out, now, component)
        _prune_agent_components(data, now, keep=component)
        return answer


def refit(session, changes, body_limit):
    """Replace an unshown reservation when recall leaves less context room."""
    if body_limit <= 0:
        return None
    with _transaction(session) as data:
        component = changes["component"]
        state = data.get(component, {})
        flight = state.get("flight")
        if not flight or flight["out"]["routing_changes"] != changes:
            return None
        plan = copy.deepcopy(flight["plan"])
        plan["limit"] = min(plan["limit"] or body_limit, body_limit)
        plan["force_pages"] = True
        out = dict(flight["out"], delivery_plan=plan)
        state.pop("flight")
        answer = _render(state, out, _clock(), component)
        _prune_agent_components(data, _clock(), keep=component)
        return answer

def _t3_pin_path(bare):
    from khipu import recall_prompt

    digest = hashlib.sha256(("t3\0" + bare).encode("utf-8")).hexdigest()
    return recall_prompt._dedup_path("ledger").parent / f"t3-pin--{digest}.json"


_T3_PIN_CACHE: dict = {}

# Pins settle one session's identity, not a permanent record: a pin older
# than this is dropped and the session is looked up again, and at most this
# many pin files are kept (oldest first out). A hook on a machine with no
# T3 database never writes one at all (see ``_t3_root``: a missing database
# is a lookup failure, and failures are not pinned).
_T3_PIN_TTL_S = 30 * 24 * 3600
_T3_PIN_MAX = 128


def _pin_timestamp(raw, path):
    stamps = []
    if isinstance(raw, dict):
        for field in ("ts", "created_at", "pinned_at"):
            value = raw.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                stamps.append(float(value))
    try:
        stamps.append(path.stat().st_mtime)
    except OSError:
        pass
    return min(stamps) if stamps else None


def _pin_expired(stamp, now=None):
    return stamp is not None and (time.time() if now is None else now) - stamp > _T3_PIN_TTL_S


def _drop_pin(path, key=None):
    _T3_PIN_CACHE.pop(key if key is not None else str(path), None)
    try:
        path.unlink()
    except OSError:
        pass


def _prune_t3_pins(directory):
    """Drop expired pin files and keep at most ``_T3_PIN_MAX`` of the rest."""
    try:
        paths = list(directory.glob("t3-pin--*.json"))
    except OSError:
        return
    now = time.time()
    survivors = []
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if _pin_expired(mtime, now):
            _drop_pin(path)
        else:
            survivors.append((mtime, path))
    survivors.sort()
    while len(survivors) >= _T3_PIN_MAX:
        _drop_pin(survivors.pop(0)[1])


def _pinned_t3_root(bare):
    """The first definitive T3 answer for this native session, pinned on disk.

    Returns ``(root, pinned)``: ``root`` is the pinned ``t3:<thread>`` (or
    None for a pinned native identity) and ``pinned`` says whether a pin
    was read. T3 can project its session row after the session's first
    hook fires; without a pin the session would start on its native
    ledger and later flip to the thread ledger, splitting deliveries
    across both. The first definitive answer therefore wins for the whole
    session, whichever way it went. A native session id names one session, so a
    pin keyed by it cannot leak into a different conversation (a reused
    id that T3 maps to two threads resolves to no mapping at lookup
    time, and that negative answer is what gets pinned). An expired pin
    (see ``_T3_PIN_TTL_S``) is dropped and reported as no pin.
    """
    # Keyed by pin path, not bare id alone: the path lives under the
    # current Khipu state dir, which a new home must not inherit.
    path = _t3_pin_path(bare)
    key = str(path)
    if key in _T3_PIN_CACHE:
        root, stamp = _T3_PIN_CACHE[key]
        try:
            stamp = min(stamp, path.stat().st_mtime)
        except OSError:
            pass
        if _pin_expired(stamp):
            _drop_pin(path, key)
            return None, False
        return root, True
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, False
    stamp = _pin_timestamp(raw, path)
    if _pin_expired(stamp):
        _drop_pin(path, key)
        return None, False
    root = raw.get("root") if isinstance(raw, dict) else None
    root = root if isinstance(root, str) and root.startswith("t3:") else None
    _T3_PIN_CACHE[key] = (root, stamp if stamp is not None else time.time())
    return root, True


def _pin_t3_root(bare, root):
    """Store the first definitive answer; a racing hook's pin wins."""
    try:
        path = _t3_pin_path(bare)
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune_t3_pins(path.parent)
        stamp = time.time()
        with open(path, "x", encoding="utf-8") as handle:
            json.dump({"root": root, "ts": stamp}, handle)
        if len(_T3_PIN_CACHE) > 512:
            _T3_PIN_CACHE.clear()
        _T3_PIN_CACHE[str(path)] = (root, stamp)
        return root
    except FileExistsError:
        pinned, found = _pinned_t3_root(bare)
        return pinned if found else root
    except OSError:
        return root


def _t3_root(native):
    """``t3:<thread>`` for a harness-native session id T3 maps to a thread.

    This is where Khipu's T3 integration sets ``KHIPU_SESSION_ID``: T3 gives
    the agent no thread environment variable, so the canonical identity is
    derived from T3's own session-to-thread record. Fail-open and bounded
    (``thread_for_session`` has its own short deadline); a session T3 does
    not know keeps its native identity. The first definitive answer for a
    session is pinned (see ``_pinned_t3_root``) so the identity cannot flip
    mid-session; a failed lookup is never pinned."""
    if not isinstance(native, str):
        return None
    bare = native.strip()
    for prefix in ("claude:", "codex:"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break
    if not bare or bare.startswith("t3:") or "/" in bare:
        return None
    pinned, found = _pinned_t3_root(bare)
    if found:
        return pinned
    try:
        from khipu import t3

        thread, definitive = t3.thread_for_session_result(bare)
    except Exception:  # noqa: BLE001 - identity derivation never breaks a hook
        return None
    if not definitive:
        # A failure (DB missing, locked, timed out, query error) settles
        # nothing: keep the native identity for this hook only and leave
        # no pin, so a later hook retries the lookup.
        return None
    return _pin_t3_root(bare, f"t3:{thread}" if thread else None)


def _valid_child_suffix(suffix):
    return bool(suffix) and "\0" not in suffix and all(
        part not in ("", ".", "..") for part in suffix.split("/"))


def _mapped_child(session, root, natives):
    """Rewrite a native-prefixed child of a T3-mapped root onto that root.

    A child a harness names ``claude:<native>/<path>`` (or ``codex:``)
    before it knows the session maps to ``t3:<thread>`` is still a child
    of the mapped conversation; rewriting it keeps slice 7's child-ledger
    guarantee for mapped parents. Returns None when ``session`` is not
    such a child (or its path is not a strict descendant)."""
    if not isinstance(session, str) or not root.startswith("t3:"):
        return None
    for native in natives:
        if not native:
            continue
        for prefix in ("claude:", "codex:"):
            head = prefix + native + "/"
            if session.startswith(head):
                suffix = session[len(head):]
                return root + "/" + suffix if _valid_child_suffix(suffix) else None
    return None


def _t3_child(session):
    """``t3:<thread>/<path>`` for a native-prefixed child session string,
    resolving the parent part through T3; None when unmapped or invalid."""
    if not isinstance(session, str):
        return None
    parent, sep, suffix = session.strip().partition("/")
    if not sep or not _valid_child_suffix(suffix):
        return None
    root = _t3_root(parent)
    return root + "/" + suffix if root else None


_HOOKED_HARNESSES = frozenset({"claude_code", "codex"})
_PULL_ONLY_HARNESSES = frozenset({"aegis", "grok", "grok_build", "cursor", "muse", "gateway", "unknown"})


def canonical_identity(environ=None, explicit=None):
    """The one conversation identity for hooks and pull (MCP) delivery.

    Returns {"root", "session", "harness", "capability", "child"} or None
    when the caller's environment and arguments name no session. An
    identity the environment itself knows (``KHIPU_SESSION_ID``, then a
    harness-native id) cannot be replaced with an unrelated explicit id.
    Native and T3 roots cannot be supplied by
    an unidentified caller. An identified caller may extend its own root
    with a strict, nonempty descendant path for an isolated child ledger.
    Other explicit ids use an isolated pull
    namespace, so they cannot borrow custom environment-owned roots.
    ``root`` is the conversation name and ``session`` its ledger key. A root
    containing "/" is a pull-only child:
    its own ledger, never its parent's. Capability comes from the
    current attachment's harness alone.
    """
    env = os.environ if environ is None else environ

    def _clean(value):
        return value.strip() if isinstance(value, str) else ""

    harness = _clean(env.get("KHIPU_HARNESS")).lower()
    if not harness:
        if env.get("GROK_HOOK_NAME") or env.get("GROK_HOOK_EVENT"):
            harness = "aegis"
        elif env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_CODE_ENTRYPOINT"):
            harness = "claude_code"
        elif env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX") or env.get("CODEX_HOME"):
            harness = "codex"
        else:
            harness = "unknown"
    explicit = _clean(explicit)
    shared = _clean(env.get("KHIPU_SESSION_ID"))
    claude = _clean(env.get("CLAUDE_CODE_SESSION_ID"))
    codex = _clean(env.get("CODEX_THREAD_ID"))
    if shared:
        root = session = shared
    elif claude:
        root = session = _t3_root(claude) or f"claude:{claude}"
    elif codex:
        root = session = _t3_root(codex) or f"codex:{codex}"
    elif explicit and not explicit.startswith(("claude:", "codex:", "t3:")):
        root = explicit
        # Environment strings cannot contain NUL. Explicit pull ids thus
        # cannot borrow even a custom environment-owned root's ledger.
        session = "pull\0" + explicit
    else:
        return None
    if shared or claude or codex:
        if explicit and explicit != root:
            # A child named under the session's native id (either harness
            # prefix) belongs to the mapped root, not to the native one.
            rewritten = _mapped_child(explicit, root, (claude, codex))
            if rewritten is not None:
                explicit = rewritten
            prefix = root + "/"
            suffix = explicit[len(prefix):] if explicit.startswith(prefix) else ""
            if not _valid_child_suffix(suffix):
                return None
            root = session = explicit
    if "/" in root:
        # A child has no hook of its own; its pulls count on the mcp
        # component, never on a parent's main ledger that will not move.
        capability = "pull_only"
    elif harness in _HOOKED_HARNESSES:
        capability = "hooked"
    elif harness in _PULL_ONLY_HARNESSES:
        capability = "pull_only"
    elif root.startswith(("claude:", "codex:")):
        capability = "hooked"
    else:
        capability = "pull_only"
    return {"root": root, "session": session, "harness": harness,
            "capability": capability, "child": "/" in root}


def hook_session(session, environ=None):
    """Resolve a trusted hook payload once, before helper IPC or a writer.

    Payload-only harness hooks historically use bare ids. The caller's
    environment supplies the shared root or native namespace when present.
    Only a payload that names the environment's own conversation (its
    shared id, its harness-native id, or either's canonical form) resolves
    to that conversation's canonical session; any other payload is a
    different conversation and keeps its own identity, so one identity is
    used end to end without borrowing another conversation's ledger or
    marker. An empty payload falls back to the environment's identity.
    """
    env = os.environ if environ is None else environ

    def _clean(value):
        return value.strip() if isinstance(value, str) else ""

    shared = _clean(env.get("KHIPU_SESSION_ID"))
    claude = _clean(env.get("CLAUDE_CODE_SESSION_ID"))
    codex = _clean(env.get("CODEX_THREAD_ID"))
    if shared:
        canonical = shared
    elif claude:
        canonical = _t3_root(claude) or f"claude:{claude}"
    elif codex:
        canonical = _t3_root(codex) or f"codex:{codex}"
    else:
        if isinstance(session, str) and session.strip():
            session = session.strip()
            return _t3_child(session) or _t3_root(session) or session
        return session
    if not isinstance(session, str) or not session.strip():
        return canonical
    session = session.strip()
    if session == canonical or session.startswith(canonical + "/"):
        return session
    own = {value for value in (shared, claude, codex) if value}
    own.update(f"{prefix}:{value}" for prefix, value in (("claude", claude), ("codex", codex)) if value)
    if session in own:
        return canonical
    # A child named under this session's native id belongs to the mapped
    # root, exactly as in canonical_identity.
    rewritten = _mapped_child(session, canonical, (claude, codex))
    if rewritten is not None:
        return rewritten
    # A payload naming a different T3-mapped native session (or its child)
    # is that thread's own conversation, never this environment's ledger.
    return _t3_child(session) or _t3_root(session) or session


def untracked_items(out):
    """Change items for an unidentified caller: named, never reserved."""
    items = []
    for item in out["delivery_plan"]["items"]:
        items.append(dict(change=item["entity"] + "@" + item["revision"],
                          entity=item["entity"], revision=item["revision"]))
    return items


def _delivery_summary(data, component, state, changes, now, counting_component):
    entries = state.get("entries", {}) if isinstance(state, dict) else {}
    in_flight = {item["entity"] for item in changes["items"]} if changes else set()
    duplicates = []
    if component == "mcp":
        other = data.get("main")
        if isinstance(other, dict) and other.get("entries"):
            duplicates = [entity for entity in in_flight
                          if entity in other["entries"] and not other["entries"][entity].get("acked")]
        elif changes:
            # No main ledger yet: the same changes are still owed there.
            duplicates = [item["entity"] for item in changes["items"]]
    counting = data.get(counting_component) if counting_component else state
    health = "green"
    if isinstance(counting, dict):
        expired = counting.get("expired", 0) or 0
        flight = counting.get("flight")
        if flight and _parse(flight["until"]) <= now:
            expired += 1
        if expired >= 2 and any(not e.get("acked") for e in (counting.get("entries") or {}).values()):
            health = "red"
    return {"owed": sum(1 for e in entries.values() if not e.get("acked") and e["entity"] not in in_flight),
            "in_flight": len(in_flight), "acked_through": state.get("acked_through"),
            "duplicates_across_components": duplicates, "health": health}


def pull_offer(out, *, session, component="mcp", counting_component=None, now=None, skip_entities=frozenset()):
    """Reserve (or re-send) one pull component's batch under its own lock.

    A live in-flight batch is re-sent byte-identically, so a dropped
    response loses nothing. Every item carries the exact text it reserves
    (``text``: the whole line, or the one page being shown), so a batch
    never reserves content the response did not return. Entities in
    ``skip_entities`` are neither reserved nor sent. Returns
    (routing_changes or None, delivery).
    """
    now = now or _parse(out["delivery_plan"]["at"])
    counting_component = counting_component or component
    with _transaction(session) as data:
        state = data.setdefault(component, {})
        entries = _merge(state, out)
        _prune_agent_components(data, now, keep=component)
        entries = state.get("entries", {})
        skip_entities = skip_entities | frozenset(e["entity"] for e in entries.values() if e.get("withheld"))
        flight = state.get("flight")
        if flight and any(item["entity"] in skip_entities
                          for item in flight["out"]["routing_changes"]["items"]):
            state.pop("flight", None)  # withdrawn, not expired: it may not be shown at all now
            flight = None
        if flight and _parse(flight["until"]) > now:
            changes = copy.deepcopy(flight["out"]["routing_changes"])
            return _projected_pages(changes), _delivery_summary(data, component, state, changes, now, counting_component)
        if flight:
            state["expired"] = state.get("expired", 0) + 1
            state.pop("flight", None)
        due = [e for e in entries.values() if not e.get("acked") and e["entity"] not in skip_entities]
        if not due:
            return None, _delivery_summary(data, component, state, None, now, counting_component)
        plan_data = out["delivery_plan"]
        page_limit = plan_data.get("page_limit") or plan_data.get("limit") or 4000
        items, progress = [], {}
        for entry in due:
            start, value = entry.get("offset", 0), entry["text"]
            if not entry["whole"] or (not start and len(value) <= page_limit):
                items.append(dict(change=entry["entity"] + "@" + entry["revision"],
                                  entity=entry["entity"], revision=entry["revision"], text=value))
                progress[entry["entity"]] = len(value)
                continue
            width = entry.get("width") or max(2, page_limit - _size(entry["label"]) - 44)
            entry.setdefault("width", width)
            end = _cut(value, start, width)
            if end == start:
                break
            remaining = _pages_left(value, end, width)
            if remaining is None:
                break
            n = entry.get("pages", 0) + 1
            items.append(dict(change=entry["entity"] + "@" + entry["revision"],
                              entity=entry["entity"], revision=entry["revision"],
                              page={"n": n, "m": n + remaining}, text=value[start:end]))
            progress[entry["entity"]] = end
            break
        if not items:
            return None, _delivery_summary(data, component, state, None, now, counting_component)
        changes = {"batch": str(uuid.uuid4()), "component": component, "items": items}
        state["flight"] = {"until": _iso(now + timedelta(seconds=plan_data["lease"])),
                           "out": {"routing_changes": copy.deepcopy(changes),
                                   "version": out.get("version")},
                           "progress": progress, "plan": copy.deepcopy(plan_data)}
        return _projected_pages(changes), _delivery_summary(data, component, state, changes, now, counting_component)


def _projected_pages(changes):
    from khipu.agent_view import RoutingPage

    # The full entry was projected before reservation. Retain that fact
    # across disk retransmission so a fragment keeps its owner exemption.
    for item in changes["items"]:
        item["text"] = RoutingPage(item["text"])
    return changes


_CHILD_GROUPS = {"account": 0, "hold": 1, "rule": 2, "note": 3, "priority": 4, "lane": 5}


def _child_sorted(entries):
    indexed = list(enumerate(entries))
    return [entry for _i, entry in sorted(
        indexed, key=lambda pair: (_CHILD_GROUPS.get(pair[1]["entity"].split(":", 1)[0]
                                                    if ":" in pair[1]["entity"] else pair[1]["entity"], 6), pair[0]))]


def _child_opening(entry):
    from khipu import routing as r

    label = {"note": "Owner note", "priority": "Owner priority"}.get(entry["entity"], entry["label"])
    return f"{label}: \"{r._compact_opening(entry['text'], 64)}\""


def _child_header_base(entry):
    if entry["entity"] == "note":
        return "Owner note"
    if entry["entity"] == "priority":
        return "Owner priority"
    match = re.match(r"Standing rule (\S+)", entry["label"] or "")
    return f"Rule {match.group(1)}" if match else entry["label"]


def child_offer(planned, *, session=None, component, now=None, cfg=None):
    """The bounded block a subagent gets at its start (slice 8a).

    Account states and holds first, then each standing rule and the note:
    whole when it fits under ``routing_child_chars``, otherwise its number,
    kind, accounts and opening, with the whole text left owed in this
    component's ledger for its own tool results. Shown entities are
    reserved as one batch, exactly as a hook offer reserves them; the
    caller acknowledges it only once the block was printed.
    """
    from khipu import recall_rule, routing as r

    cfg = cfg or {}
    now = now or _parse(planned["delivery_plan"]["at"])
    # The harness will not keep a larger hook block whole, whatever the
    # owner set: routing_child_chars=0 means no routing limit, not no cap.
    limit = min(cfg.get("routing_child_chars", 4000) or 10 ** 9,
                recall_rule._HOOK_CONTEXT_BUDGET)
    width = cfg.get("routing_line_chars", 200)

    def build(state):
        entries = _merge(state, planned)
        state["touched"] = _iso(now)
        ordered = _child_sorted(entries.values())
        heading = planned["delivery_plan"]["heading"]
        if _size(heading) > limit:
            # The heading itself counts: a limit it alone exceeds gets
            # nothing, with everything left owed for later pages.
            return {"text": "", "owed_entities": [e["entity"] for e in ordered],
                    "shown_entities": []}
        lines, shown, deferred = [heading], {}, []
        used = _size(heading)
        candidates = [e for e in ordered if e.get("whole")]
        for entry in ordered:
            entity = entry["entity"]
            if not entry.get("whole"):
                rendered = r._clip(entry["text"], width) if width else entry["text"]
                if used + _size(rendered) + 1 <= limit:
                    lines.append(rendered)
                    shown[entity] = len(entry["text"])
                    entry.pop("child_owed", None)
                    used += _size(rendered) + 1
                else:
                    # Only a line this block actually dropped is owed as a
                    # later page; a line first seen on a tool call is not.
                    entry["child_owed"] = True
                    deferred.append(entity)
                continue
            opening = _child_opening(entry)
            at = next((i for i, e in enumerate(candidates) if e is entry), len(candidates))
            reserve = sum(_size(_child_opening(e)) + 1 for e in candidates[at + 1:])
            whole = entry["text"]
            header = _size("Standing rules:") + 1 if entity.startswith("rule:") \
                and not any(line == "Standing rules:" for line in lines) else 0
            if (not entry.get("offset") or entry.get("acked")) and \
                    used + header + _size(whole) + 1 + reserve <= limit:
                if header:
                    lines.append("Standing rules:")
                    used += header
                lines.append(whole)
                shown[entity] = len(whole)
                used += _size(whole) + 1
            elif used + header + _size(opening) + 1 <= limit:
                if header:
                    lines.append("Standing rules:")
                    used += header
                lines.append(opening)
                deferred.append(entity)
                used += _size(opening) + 1
            else:
                deferred.append(entity)
        answer = {"text": "\n".join(lines), "owed_entities": deferred,
                  "shown_entities": list(shown)}
        if session and shown:
            changes = {"batch": str(uuid.uuid4()), "component": component,
                       "items": [dict(change=e + "@" + entries[e]["revision"], entity=e,
                                      revision=entries[e]["revision"]) for e in shown]}
            answer["routing_changes"] = changes
            state["flight"] = {"until": _iso(now + timedelta(seconds=planned["delivery_plan"]["lease"])),
                               "out": {"routing_changes": copy.deepcopy(changes),
                                       "version": planned.get("version")},
                               "progress": dict(shown), "plan": copy.deepcopy(planned["delivery_plan"])}
        return answer

    if not session:
        return build({})
    with _transaction(session) as data:
        answer = build(data.setdefault(component, {}))
        _prune_agent_components(data, now, keep=component)
        return answer


def child_page(planned, *, session=None, component, now=None, cfg=None):
    """The next deferred page for one agent component, or None.

    One entity at a time, in child order, one page per delivery point.
    The page is leased as a batch before the hook prints it; while that
    lease is live no second call hands the same page out again, and only
    after it expires unacknowledged is the page offered anew. Acknowledging
    it (the hook's delivered callback) advances the ledger's offset, and a
    revision change restarts it.
    """
    from khipu import routing as r

    cfg = cfg or {}
    now = now or _parse(planned["delivery_plan"]["at"])
    if not session:
        return None
    with _transaction(session) as data:
        state = data.setdefault(component, {})
        entries = _merge(state, planned)
        state["touched"] = _iso(now)
        flight = state.get("flight")
        if flight and _parse(flight["until"]) > now:
            # Leased to a call that has not recorded delivery yet (the
            # start block or a page): never print the same page twice.
            _prune_agent_components(data, now, keep=component)
            return None
        if flight:
            state["expired"] = state.get("expired", 0) + 1
            state.pop("flight", None)
        due = [e for e in _child_sorted(entries.values())
               if not e.get("acked") and e.get("offset", 0) < len(e["text"])
               and (e.get("whole") or e.get("child_owed"))]
        if not due:
            _prune_agent_components(data, now, keep=component)
            return None
        entry = due[0]
        if not entry.get("whole"):
            # Account and hold lines the start block could not fit are
            # owed like any rule: their identifying line is the page.
            width = cfg.get("routing_line_chars", 200)
            rendered = r._clip(entry["text"], width) if width else entry["text"]
            end = len(entry["text"])
            item = dict(change=entry["entity"] + "@" + entry["revision"], entity=entry["entity"],
                        revision=entry["revision"])
            changes = {"batch": str(uuid.uuid4()), "component": component, "items": [item]}
            state["flight"] = {"until": _iso(now + timedelta(seconds=planned["delivery_plan"]["lease"])),
                               "out": {"routing_changes": copy.deepcopy(changes),
                                       "version": planned.get("version"), "text": rendered},
                               "progress": {entry["entity"]: end},
                               "plan": copy.deepcopy(planned["delivery_plan"])}
            _prune_agent_components(data, now, keep=component)
            return {"text": rendered, "routing_changes": changes, "version": planned.get("version"),
                    "epoch": planned.get("epoch"),
                    "owed_entities": [e["entity"] for e in due],
                    "shown_entities": [entry["entity"]]}
        page_limit = planned["delivery_plan"].get("page_limit") or cfg.get("routing_prompt_chars") or 2000
        base = _child_header_base(entry)
        start = entry.get("offset", 0)
        n = entry.get("pages", 0) + 1
        m, end = n, start
        for _ in range(4):
            header = f"{base} page {n}/{m}"
            width = max(2, page_limit - _size(header) - 1)
            end = _cut(entry["text"], start, width)
            if end == start:
                return None
            remaining = _pages_left(entry["text"], end, width)
            if remaining is None:
                return None
            if n + remaining == m:
                break
            m = n + remaining
        header = f"{base} page {n}/{m}"
        rendered = header + "\n" + entry["text"][start:end]
        if entry.get("restarted"):
            rendered = f"{base} changed while being shown.\n" + rendered
        item = dict(change=entry["entity"] + "@" + entry["revision"], entity=entry["entity"],
                    revision=entry["revision"], page={"n": n, "m": m})
        changes = {"batch": str(uuid.uuid4()), "component": component, "items": [item]}
        state["flight"] = {"until": _iso(now + timedelta(seconds=planned["delivery_plan"]["lease"])),
                           "out": {"routing_changes": copy.deepcopy(changes),
                                   "version": planned.get("version"), "text": rendered},
                           "progress": {entry["entity"]: end},
                           "plan": copy.deepcopy(planned["delivery_plan"])}
        _prune_agent_components(data, now, keep=component)
        return {"text": rendered, "routing_changes": changes, "version": planned.get("version"),
                "epoch": planned.get("epoch"),
                "owed_entities": [e["entity"] for e in due],
                "shown_entities": [entry["entity"]] if end == len(entry["text"]) else []}


def _batch_in_state(state, batch):
    if not isinstance(state, dict):
        return False
    if batch in (state.get("batches") or {}):
        return True
    flight = state.get("flight")
    return bool(flight and flight["out"]["routing_changes"].get("batch") == batch)


def _batch_owned_elsewhere(session, batch):
    """Best-effort foreign classification; never consume another ledger.

    Unknown acknowledgements have a fixed work budget regardless of the
    number of sessions. Beyond it, "unknown" is equally non-consuming.
    Read candidate ledgers only while holding their shared delivery lock.
    """
    from itertools import islice

    own = _path(session)
    deadline = time.monotonic() + 0.05
    reads = 0
    try:
        with os.scandir(own.parent) as entries:
            for entry in islice(entries, 32):
                if time.monotonic() >= deadline or reads >= 8:
                    break
                path = own.parent / entry.name
                if path == own or not entry.name.endswith(".routing.json"):
                    continue
                reads += 1
                try:
                    with path.with_suffix(".lock").open("a") as lock:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        if path.stat().st_size > 1_048_576:
                            continue
                        data = json.loads(path.read_text())
                        if isinstance(data, dict) and any(_batch_in_state(state, batch) for state in data.values()):
                            return True
                except (OSError, ValueError, TypeError, KeyError):
                    continue
    except OSError:
        pass
    return False


def pull_acknowledge(session, batch, *, component="mcp", now=None):
    """Ack one pull batch. Returns acked, duplicate, foreign or unknown.

    Only the issuing component of this identity may move a batch. A batch
    issued to another component or identity is "foreign" and stays put; a
    duplicate is a no-op; an unknown or expired batch is answered, never
    an error, and consumes nothing.
    """
    now = now or _clock()
    with _transaction(session) as data:
        state = data.get(component) or {}
        if batch in (state.get("batches") or {}):
            return "duplicate"
        flight = state.get("flight")
        if flight and flight["out"]["routing_changes"].get("batch") == batch:
            if _parse(flight["until"]) <= now:
                return "unknown"
            changes = flight["out"]["routing_changes"]
            for item in changes["items"]:
                entry = state["entries"].get(item["entity"])
                if entry is None or entry["revision"] != item["revision"]:
                    return "unknown"
            for item in changes["items"]:
                entry = state["entries"][item["entity"]]
                entry["offset"] = flight["progress"][item["entity"]]
                entry["acked"] = entry["offset"] == len(entry["text"])
                entry["pages"] = item.get("page", {}).get("n", 0)
                entry["restarted"] = False
            batches = state.setdefault("batches", {})
            batches[batch] = True
            for old in list(batches)[:-128]:
                del batches[old]
            state.pop("flight", None)
            if all(e.get("acked") for e in state["entries"].values()):
                state["acked_through"] = flight["out"].get("version")
            _prune_agent_components(data, now, keep=component)
            return "acked"
        if any(comp != component and _batch_in_state(other, batch) for comp, other in data.items()):
            return "foreign"
    return "foreign" if _batch_owned_elsewhere(session, batch) else "unknown"


def acknowledge(session, changes, *, reservation=None, now=None):
    """Only an issued batch can ack; failed persistence leaves it retryable."""
    try:
        return _acknowledge(session, changes, reservation=reservation, now=now)
    except Exception:
        return False

def _acknowledge(session, changes, *, reservation=None, now=None):
    """Ack only a batch actually issued to this session and component."""
    now = now or _clock()
    component, batch = changes["component"], changes["batch"]
    with _transaction(session) as data:
        state = data.setdefault(component, {})
        if reservation is not None and not state:
            state.update(copy.deepcopy(reservation))
        if batch in state.get("batches", {}):
            return True
        flight = state.get("flight")
        if not flight or flight["out"]["routing_changes"] != changes:
            return False
        if _parse(flight["until"]) <= now:
            return False
        for item in changes["items"]:
            e = state["entries"][item["entity"]]
            if e["revision"] != item["revision"]:
                return False
            e["offset"] = flight["progress"][item["entity"]]
            e["acked"] = e["offset"] == len(e["text"])
            e["pages"] = item.get("page", {}).get("n", 0)
            e["restarted"] = False
        batches = state.setdefault("batches", {})
        batches[batch] = True
        forecasts = (flight.get("plan") or {}).get("pace_forecasts") or {}
        if forecasts:
            state["pace_acked"] = {**(state.get("pace_acked") or {}), **forecasts}
        advice = (flight.get("plan") or {}).get("advice_forecasts") or {}
        if advice:
            state["advice_acked"] = {**(state.get("advice_acked") or {}), **advice}
        # Idempotence covers recent retries; older ids fail closed.
        for old in list(batches)[:-128]:
            del batches[old]
        state.pop("flight", None)
        if all(e.get("acked") for e in state["entries"].values()):
            state["acked_through"] = flight["out"]["version"]
        _prune_agent_components(data, now, keep=component)
        return True
