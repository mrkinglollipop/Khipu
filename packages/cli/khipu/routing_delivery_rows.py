"""Owner-local delivery rows for ``routing.public_view`` (survey rows 8-11).

This module only reads the same delivery ledgers, replication cursors and
routing view that slice 16's health evidence already reads. Its output is
built solely for the owner's ``khipu route --json`` view: friendly
conversation names, Mac labels, last-prompt times and per-change
recipients must never ride a push, a child page, the MCP conversation
view, a replication record, doctor output, a log or the gateway
projection. A damaged whole-source read returns ``rows: null`` so the
owner view can tell that apart from "nobody live".
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time

_SESSION_SHAPED = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,})",
    re.IGNORECASE)
# A label is shown only when it is a real title. Anything shaped like a
# session or component identity is rejected: harness-prefixed ids
# (``claude_code_…``, ``codex:…``, ``t3:…``), ``agent:``/Explore-style
# component ids, uuid/hex runs, and a conversation's own canonical
# identity or session id (in any of its forms, checked against the
# ledger hash, which is the hash of that canonical identity).
_HARNESS_PREFIX = re.compile(
    r"^(?:claude_code|claude-code|codex|claude|cursor|grok_build|grok-build|"
    r"grok|aegis|muse|gateway|t3|agent|main|mcp)(?:[:/_.\-]|$)", re.IGNORECASE)
# The same harness prefixes anywhere in a title, not only at its start:
# ``Investigate t3:… today`` and ``Investigate claude_code_… today`` are
# identifiers with surrounding words, not titles.
_HARNESS_ANYWHERE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:claude_code|claude-code|codex|claude|cursor|"
    r"grok_build|grok-build|grok|aegis|muse|gateway|t3|agent|main|mcp)"
    r"(?:[:/_.\-])\S*", re.IGNORECASE)
_COMPONENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[0-9a-fA-F]{4,}(?:-[0-9a-fA-F]+)*$")
_COMPONENT_ANYWHERE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*-[0-9a-fA-F]{4,}(?:-[0-9a-fA-F]+)*")
_COLON_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*:[A-Za-z0-9][^\s]*")
_HEX_TOKEN = re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b")
# A hex run of 8+ anywhere, even glued to a prefix by ``_``/``:``/``-``
# (where a word boundary would hide it from ``_HEX_TOKEN``).
_HEX_RUN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8,}(?![0-9a-fA-F])", re.IGNORECASE)
# How long another Mac's copy may lag before its row reads as behind; the
# same bound ``routing_replication.status`` uses for its stale verdict.
_MAC_STALE_SECONDS = 600
# How long a failed background title resolution waits before a retry.
_TITLE_RETRY_SECONDS = 300


def _identity_forms(name):
    forms = {name}
    bare = name.split(":", 1)[1] if ":" in name else name
    forms.add(bare)
    forms.update((f"claude:{bare}", f"codex:{bare}", f"t3:{bare}"))
    return forms


def _is_identity_shaped(name, forbidden=(), ledger_hash=None):
    lowered = {value.lower() for value in forbidden if isinstance(value, str) and len(value) >= 3}
    if name.lower() in lowered:
        return True
    # An identifier embedded in surrounding words is still an identifier.
    if any(value in name.lower() for value in lowered):
        return True
    if _SESSION_SHAPED.search(name) or _HEX_TOKEN.search(name) or _HEX_RUN.search(name):
        return True
    if _HARNESS_PREFIX.match(name) or _HARNESS_ANYWHERE.search(name):
        return True
    if _COMPONENT_ANYWHERE.search(name) or _COLON_ID.search(name):
        return True
    if " " not in name and (":" in name or _COMPONENT_ID.match(name)):
        return True
    if ledger_hash:
        # The whole name, and every token in it, in each of its identity
        # forms: an embedded own session id hashes to the ledger hash.
        candidates = {name}
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9:/_.\-]*", name):
            candidates.add(token)
            candidates.update(part for part in re.split(r"[\s:/_.\-]+", token) if part)
        if any(hashlib.sha256(form.encode("utf-8")).hexdigest() == ledger_hash
               for candidate in candidates for form in _identity_forms(candidate)):
            return True
    return False


def _write_private_file(path, text):
    """Write ``text`` to ``path`` privately and atomically.

    The folder is made 0700 and the file 0600, through a same-folder
    temporary replaced with ``os.replace`` (like
    ``routing_replication._atomic``), so a reader never sees a partial
    file and another local user never reads an identity or a title."""
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=folder)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        directory = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _clean_name(value, *, forbidden=(), ledger_hash=None):
    if not isinstance(value, str):
        return None
    name = " ".join(value.split()).strip()
    if not name or "/" in name or "\\" in name:
        return None
    if _is_identity_shaped(name, forbidden, ledger_hash):
        return None
    return name[:80].rstrip() or None


def _resolve_pending_titles(folder):
    """Resolve queued T3 thread titles into the title cache.

    This runs on the ``route --json`` read path, never on per-prompt
    delivery: delivery only queues a pending file (see
    ``routing_delivery._queue_title_resolution``) and reads stored or
    cached titles. A failed lookup stays queued and is retried at most
    once per ``_TITLE_RETRY_SECONDS``."""
    from khipu import t3

    try:
        paths = sorted(folder.glob("title-pending--*.json"))[:8]
    except OSError:
        return
    for path in paths:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        # Pending files hold only the identity's digest (in the file name
        # and in ``digest``), never the raw canonical identity. A legacy
        # file that still carries ``root`` is rewritten digest-only below
        # before any lookup, and its raw value is used only to check that
        # it belongs to this digest.
        root = raw.get("root")
        digest = path.name.removeprefix("title-pending--").removesuffix(".json")
        stored = raw.get("digest")
        if isinstance(root, str) and root.startswith("t3:"):
            if hashlib.sha256(root.encode("utf-8")).hexdigest() != digest:
                root = None
        elif root is not None:
            root = None
        if not digest or (stored is not None and stored != digest) or (root is None and stored is None
                                                                      and "root" in raw):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        attempted = raw.get("attempted_at")
        if isinstance(attempted, (int, float)) and time.time() - attempted < _TITLE_RETRY_SECONDS:
            continue
        try:
            _write_private_file(path, json.dumps({"digest": digest, "attempted_at": time.time()}))
        except OSError:
            continue
        try:
            found = t3.thread_title_for_digest(digest)
            if found is None and isinstance(root, str):
                # A legacy pending file whose thread the digest scan
                # cannot see (for example a mocked title reader).
                found = t3.thread_title(root[3:])
            forbidden = {root, root[3:]} if isinstance(root, str) else set()
            title = _clean_name(found, forbidden=forbidden, ledger_hash=digest)
        except Exception:  # noqa: BLE001 - a failed lookup stays queued for a later read
            continue
        if not title:
            continue
        try:
            _write_private_file(folder / f"title-cache--{digest}.json",
                                json.dumps({"name": title}))
            path.unlink()
        except OSError:
            continue


def _cached_title(folder, ledger_hash):
    try:
        raw = json.loads((folder / f"title-cache--{ledger_hash}.json").read_text())
    except (OSError, ValueError):
        return None
    return _clean_name(raw.get("name") if isinstance(raw, dict) else None,
                       ledger_hash=ledger_hash)


def _component_kind(component):
    if component == "main":
        return "main"
    if component == "mcp":
        return "mcp"
    return "agent"


def _entity_revisions(view, now, cfg):
    """The delivery revision each changed entity carries in this view.

    Computed with the delivery planner itself, so the revision a ledger
    must prove is exactly the revision delivery would hand out now."""
    entities = sorted({entity for change in view.get("changes") or [] if isinstance(change, dict)
                       for entity in change.get("entities") or [] if isinstance(entity, str)})
    if not entities:
        return {}
    try:
        from khipu import routing_delivery

        out = {"kind": "changes", "owed_entities": entities, "shown_entities": [],
               "text": "", "version": view.get("version"), "epoch": view.get("epoch")}
        plan_cfg = {"routing_prompt_chars": 2000, "routing_delivery_lease": 60,
                    "routing_line_chars": 200, **(cfg or {})}
        planned = routing_delivery.plan(out, view, now, plan_cfg)
        return {item["entity"]: item["revision"] for item in planned["delivery_plan"]["items"]
                if isinstance(item, dict) and isinstance(item.get("entity"), str)
                and isinstance(item.get("revision"), str)}
    except Exception:  # noqa: BLE001 - no revision evidence reads as no recipients
        return {}


def _has_change(state, entities, version, epoch, revisions):
    """Evidence this conversation has this change, for its revision+epoch.

    An acknowledgement under another epoch, or of an older revision of an
    entity, is not evidence for the change as it now stands."""
    if not isinstance(state, dict) or not entities:
        return False
    if epoch is not None and state.get("epoch") != epoch:
        return False
    acked_through = state.get("acked_through")
    if isinstance(acked_through, int) and isinstance(version, int) and acked_through >= version:
        return True
    entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
    done = state.get("done") if isinstance(state.get("done"), dict) else {}
    for entity in entities:
        expected = revisions.get(entity)
        if not isinstance(expected, str) or not expected:
            return False
        if done.get(entity) == expected:
            continue
        entry = entries.get(entity)
        if isinstance(entry, dict) and entry.get("acked") and entry.get("revision") == expected:
            continue
        return False
    return True


def delivery_data(view, now, cfg):
    """Live rows, other Macs and per-change recipients for the owner view.

    Returns ``{"rows": [...], "macs": [...], "recipients": {version: ...}}``
    with no session id, Mac id or path in any field."""
    try:
        return _delivery_data(view, now, cfg)
    except Exception:  # noqa: BLE001 - damaged ≠ empty; null rows, not []
        return {"rows": None, "macs": [], "recipients": {}}


def _delivery_data(view, now, cfg):
    from khipu import recall_prompt, routing, routing_delivery, routing_health

    cfg = cfg or {}
    mac_names = cfg.get("routing_mac_names") if isinstance(cfg.get("routing_mac_names"), dict) else {}
    local_mac = routing._local_mac()
    local_label = _clean_name(mac_names.get(local_mac)) or "This Mac"
    folder = recall_prompt._dedup_path("ledger").parent
    _resolve_pending_titles(folder)
    revisions = _entity_revisions(view, now, cfg)
    try:
        paths = sorted(folder.glob("*.routing.json"))
    except OSError:
        paths = []
    rows = []
    identities = []  # one entry per ledger file, for change recipients
    for path in paths:
        try:
            if path.stat().st_size > routing_delivery._SESSION_FILE_BUDGET:
                continue
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                continue
        except (OSError, ValueError):
            continue
        ledger_hash = path.name.removesuffix(".routing.json").removeprefix("recall--")
        attachment = data.get("attachment") if isinstance(data.get("attachment"), dict) else {}
        attached = routing_health._time(attachment.get("at"))
        components = [attachment.get("component")] if attached else []
        components += [name for name in data if name.startswith("agent:")]
        if not components:
            continue
        # Only a T3 thread title (stored at resolution time, or in the
        # title cache) may name a conversation; conversation-authored
        # ``name`` fields in the record are not a title source.
        friendly = (_clean_name(attachment.get("friendly_name"), ledger_hash=ledger_hash)
                    or _cached_title(folder, ledger_hash))
        identity = {"friendly": friendly, "label": None, "states": [], "harness": None}
        for component in components:
            state = data.get(component)
            if not isinstance(state, dict):
                continue
            stamp = routing_health._time(state.get("last_delivery_point"))
            if stamp is None or stamp > now:
                continue
            live_after = cfg.get("routing_delivery_live_after")
            if live_after and (now - stamp).total_seconds() > live_after:
                continue
            health = routing_health.component_health(state, now, slack=cfg.get("routing_delivery_slack", 2))
            identity["states"].append((component, state))
            harness = routing_delivery.health_harness(
                state.get("harness") if component.startswith("agent:") else attachment.get("harness"))
            if identity["harness"] is None:
                identity["harness"] = harness
            rows.append({
                "identity": identity,
                "harness": harness,
                "component": _component_kind(component),
                "mac": "Cloud" if attachment.get("gateway") else local_label,
                "last_prompt_at": state.get("last_delivery_point"),
                "status": health["status"],
                "reasons": health.get("reasons") or [],
                "missing": health.get("missing") or [],
                "owed": health.get("owed") if isinstance(health.get("owed"), int) else None,
                "_stamp": stamp,
            })
        if identity["states"]:
            identities.append(identity)
    rows.sort(key=lambda row: (-row["_stamp"].timestamp(), row["component"]))
    numbers: dict[int, int] = {}
    for row in rows:
        key = id(row["identity"])
        if key not in numbers:
            numbers[key] = len(numbers) + 1
            row["identity"]["label"] = (row["identity"]["friendly"]
                                        or f"Conversation {numbers[key]} · {row['identity']['harness'] or 'unknown'}")
        row["label"] = row["identity"]["label"]
        del row["identity"], row["_stamp"]
    recipients = {}
    for change in view.get("changes") or []:
        if not isinstance(change, dict):
            continue
        entities = [e for e in change.get("entities") or [] if isinstance(e, str)]
        version = change.get("version")
        if not entities or not isinstance(version, int) or not identities:
            continue
        have, waiting = [], []
        for identity in identities:
            # States are in doctor order (attachment component first), so
            # the first live component is the counting one, as in slice 16.
            state = _counting_state(identity)
            (have if _has_change(state, entities, version, view.get("epoch"), revisions)
             else waiting).append(identity["label"])
        recipients[version] = {"have_count": len(have), "live_count": len(identities),
                               "have": have, "waiting": waiting}
    return {"rows": rows, "macs": _other_macs(now, mac_names, local_mac), "recipients": recipients}


def _counting_state(identity):
    states = identity["states"]
    return states[0][1] if states else {}


def _other_macs(now, mac_names, local_mac):
    from khipu import routing, routing_replication

    try:
        sources = routing_replication.status(now).get("sources") or []
    except Exception:  # noqa: BLE001 - replication evidence is optional
        return []
    macs = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        mac = source.get("mac")
        if not isinstance(mac, str) or not mac or mac == local_mac:
            continue
        # ``last_seen_at`` advances when a Mac uploads an event, not when
        # it catches up, so only a real catch-up receipt says how fresh
        # its copy is; without one the freshness is unknown.
        caught = routing._parse(source.get("caught_up_at"))
        age = max(0, int((now - caught).total_seconds())) if caught else None
        behind = bool(age is not None and age > _MAC_STALE_SECONDS)
        macs.append({"label": _clean_name(mac_names.get(mac)) or "another Mac",
                     "age_seconds": age,
                     "behind": behind,
                     "freshness": "behind" if behind else "current" if age is not None else "unknown"})
    macs.sort(key=lambda row: (row["age_seconds"] is None, row["age_seconds"] or 0))
    return macs
