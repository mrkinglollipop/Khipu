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

    return recall_prompt._dedup_path(session).with_suffix(".routing.json")


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

def plan(out, view, now, cfg):
    """Attach current, scrubbed entity revisions to a hook's rendered offer."""
    from khipu import routing as r

    full = out.get("kind") == "full"
    entities = list(out.get("owed_entities") or [])
    if full and view.get("record"):
        entities = list(dict.fromkeys([*entities, *[e for e in view.get("effective", {})
                                                  if not e.startswith("owner:")]]))
    rules = {r._rule_component(rule): rule for rule in view.get("rules") or []}
    items = []
    facts = r.plan_facts(view, now)
    text = out.get("text") or ""
    shown = set(out.get("shown_entities") or [])
    for entity in entities:
        rule = rules.get(entity)
        whole = entity in ("note", "priority") or rule is not None
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
        revision = hashlib.sha256(json.dumps(effective, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        items.append(dict(entity=entity, revision=revision, text=line, label=label, whole=whole))
    projected = r._for_agents([(item["text"], item["label"]) for item in items], view) if items else []
    for item, (line, label) in zip(items, projected):
        item.update(text=line, label=label)
        # Rules and notes require their complete words. Accounts and holds
        # may use the existing renderer's identifying line projection.
        visible = line in text if item["whole"] else item["entity"] in shown
        if full and not item["whole"]:
            width = cfg["routing_line_chars"]
            clipped = line[:max(0, width - 3)].rstrip() + "." * min(width, 3) if width and len(line) > width else line
            visible = bool(line and (line in text or clipped in text or r._clip(line, width) in text))
        item["visible"] = visible
    return dict(out, delivery_plan={"items": items, "at": r._iso(now),
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

def in_flight(session, component, view, now):
    """Reuse an unchanged reservation before rendering or scrubbing again."""
    state = snapshot(session, component)
    flight = state.get("flight")
    if state.get("epoch") != view.get("epoch") or not flight:
        return None
    until = datetime.fromisoformat(flight["until"].replace("Z", "+00:00"))
    if until <= now:
        return None
    for item in flight["out"]["routing_changes"]["items"]:
        effective = view.get("effective", {}).get(item["entity"])
        if effective is None:
            return None
        revision = hashlib.sha256(json.dumps(effective, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        if revision != item["revision"]:
            return None
    return copy.deepcopy(flight["out"])

def _merge(state, out):
    epoch = out.get("epoch")
    if state.get("epoch") != epoch:
        state.clear()
        state.update(epoch=epoch, entries={}, batches={})
    entries = state.setdefault("entries", {})
    replaced = set()
    for item in out["delivery_plan"]["items"]:
        entity = item["entity"]
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
    due = [e for e in entries.values() if not e.get("acked")]
    if not due:
        # Informational full/stale blocks have no change debt to reserve.
        answer = {k: v for k, v in out.items() if k not in
                  ("delivery_plan", "delivery_reservation", "routing_changes", "delivery", "_delivery_view", "_reserved")}
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
    answer = {k: v for k, v in out.items() if k not in ("delivery_plan", "delivery_reservation", "_delivery_view", "_reserved")}
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
        return _render(state, out, now, component)


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
        return _render(state, out, _clock(), component)

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
        state.setdefault("batches", {})[batch] = True
        state.pop("flight", None)
        if all(e.get("acked") for e in state["entries"].values()):
            state["acked_through"] = flight["out"]["version"]
        return True
