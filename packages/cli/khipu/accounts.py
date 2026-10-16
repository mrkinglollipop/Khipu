"""Tracking and managing accounts (settings clarity phase 9, 9B).

Scope: docs/plans/2026-10-15-settings-clarity.md, "Phase 9": decisions 4 to
20 and sections 5 (Tracking), 7 (Living with an account), 9 (Stop tracking)
and 11 (the agents' view). Every write here is the owner's: the CLI
(``khipu accounts``) lets it through only from the app or after a typed yes
(``routing._owner_via``), and no MCP tool reaches it. Per Mac; nothing here
syncs through the hub.

The account list is ``usage_accounts`` in config.json. Each write holds
``config.mutate_config`` (the one config write path) from its read to its save and is refused when
the version the caller read (``--expect``, ``config.accounts_version``) has
moved. Account keys (``usage._key``: the CLI and the login folder) never
change on rename (decision 7); a default login stays home-less (decision 6),
so writing the default list out changes no key.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import threading

from khipu import config, discover, routing, usage
from khipu.usage import AccountsError

DECLINED_WHY = "you chose Not this one"
STOPPED_WHY = "you stopped tracking it"
DECLINED_LOCK_NAME = "accounts-declined.lock"


def _now_iso() -> str:
    return routing._iso(routing._now())


# ---- the list as stored ---------------------------------------------------------------


def _one_line(value, what: str, *, field: str | None = None, index: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.isprintable():
        raise AccountsError(f"{what} must be one line of plain text", "not_one_line", index=index, field=field)
    return value.strip() or None


def _shown_defaults(raw) -> list[dict]:
    """The default accounts as the caller showed them (the Plans card's own
    list): home-less entries of known CLIs with their labels."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise AccountsError("--shown-defaults must be JSON", "bad_list") from None
    if not isinstance(raw, list) or len(raw) > usage.ACCOUNTS_MAX:
        raise AccountsError("--shown-defaults must be a list of default accounts", "bad_list")
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) - {"label", "harness", "home"}:
            raise AccountsError(f"shown default {i + 1} is not an account", "bad_entry", index=i)
        harness = str(item.get("harness") or "").strip().lower()
        if harness not in usage.HARNESSES:
            raise AccountsError(f"shown default {i + 1} names no CLI Khipu reads", "unknown_cli", index=i,
                                field="harness")
        if item.get("home"):
            raise AccountsError(f"shown default {i + 1} has a login folder; a default login has none", "bad_entry",
                                index=i, field="home")
        label = _one_line(item.get("label"), "a label", field="label", index=i) or usage.LABELS[harness]
        out.append({"label": label, "harness": harness})
    return out


def current_list(data: dict, shown_defaults=None) -> tuple[list | None, list]:
    """(the stored list or None, the list a write starts from): the stored
    one; none after "Read no accounts"; else the default accounts written out
    as the caller saw them (``shown_defaults``, or the installed CLIs now),
    labels unchanged and with no ``home``."""
    raw = data.get("usage_accounts")
    if isinstance(raw, list) and raw:
        return raw, [dict(e) if isinstance(e, dict) else e for e in raw]
    if usage.reads_none(data):
        return None, []
    if shown_defaults is not None:
        return None, _shown_defaults(shown_defaults)
    return None, [{"label": a["label"], "harness": a["harness"]} for a in usage.configured_accounts()
                  if not a.get("home")]


def known_of(entries: list) -> list[dict]:
    """The valid entries of a list as ``routing`` knows accounts."""
    out = []
    for entry in entries:
        account = usage._account_from_entry(entry)
        if not account.get("invalid"):
            out.append({"key": usage._key(account), "label": account["label"], "harness": account["harness"],
                        "home": account.get("home")})
    return out


def normal_key(key) -> str:
    """An account key (``harness|login folder``) as ``usage._key`` writes it."""
    if not isinstance(key, str) or "|" not in key or not key.isprintable():
        raise AccountsError("name the account by its key (the CLI, |, and its login folder)", "not_tracked")
    harness, _, home = key.partition("|")
    harness = harness.strip().lower()
    if harness not in usage.HARNESSES:
        raise AccountsError("that names no CLI Khipu reads (claude, codex, grok, muse)", "not_tracked")
    return usage._key({"harness": harness, "home": home.strip() or None})


def _find(entries: list, key: str) -> int:
    for i, entry in enumerate(entries):
        account = usage._account_from_entry(entry)
        if not account.get("invalid") and usage._key(account) == key:
            return i
    raise AccountsError("Khipu doesn't track that account on this Mac", "not_tracked")


def canonical_home(harness: str, home) -> str | None:
    """The one ``home`` an account is stored with (section 5, step 2):
    none for the CLI's default login (by real folder) and for Muse, else
    ``~/...`` under the home folder, else the full path, with symlinks and
    ``..`` resolved first."""
    raw = _one_line(home, "a login folder", field="home")
    if harness == "muse":
        if raw:
            raise AccountsError("Muse has one sign-in, so it takes no login folder", "muse_folder", field="home")
        return None
    if raw is None:
        return None
    path = discover._expand(raw)
    if path is None:
        raise AccountsError(f"Khipu cannot find the login folder {raw}; start it with ~/ (for example "
                            "~/.codex-work) or give the full path", "no_such_folder", field="home")
    real = os.path.realpath(path)
    if usage.login_identity({"harness": harness, "home": real}) == usage.login_identity({"harness": harness}):
        return None
    base = os.path.realpath(discover._home()).rstrip(os.sep)
    if real == base:
        return "~"
    if real.startswith(base + os.sep):
        return "~/" + real[len(base) + 1:]
    return real


def _unique_name(name: str, taken: set[str]) -> str:
    if name.casefold() not in taken:
        return name
    n = 2
    while f"{name} {n}".casefold() in taken:
        n += 1
    return f"{name} {n}"


def convert_lanes(data: dict, known: list[dict]) -> list[dict]:
    """Convert ``routing_lane_map`` in ``data`` to account keys against
    ``known`` (routing.convert_lane_map); the lanes left for the owner. A map
    that is not valid is left as it is."""
    raw = data.get("routing_lane_map")
    if raw is None:
        return []
    try:
        lane_map = routing._coerce("lane_map", raw, "routing_lane_map")
    except (ValueError, TypeError):
        return []
    converted, repairs = routing.convert_lane_map(lane_map, known)
    if converted != raw:
        data["routing_lane_map"] = converted
    return repairs


def _lanes_on(lane_map: dict, key: str, known: list[dict]) -> list[tuple[str, str]]:
    return [(runner, lane) for runner, lanes in (lane_map or {}).items() if isinstance(lanes, dict)
            for lane, target in lanes.items() if routing._lane_target(target, known) == key]


def _store(data: dict, entries: list, *, none: bool = False, request: dict | None = None) -> None:
    """The list into ``data``, the one place it is written (``_commit``
    calls it): "Read no accounts" only with ``none``; a conversation's
    request kept as ``request`` (with what it replaced), else ended."""
    if entries:
        data["usage_accounts"] = entries
    else:
        data.pop("usage_accounts", None)
    if none:
        data["usage_accounts_none"] = True
    else:
        data.pop("usage_accounts_none", None)
    if request is not None:
        data["usage_accounts_request"] = request
    else:
        data.pop("usage_accounts_request", None)


VERSION_NEEDED = ("say which version of the account list this change was made on (--expect); "
                  "read the list again first")


@contextlib.contextmanager
def _locked(expect):
    """The config document under the write lock, version checked; the
    caller saves it. ``expect`` (the version the caller read) is required.
    The routing lock is held around it (taken first), and any account change
    left unfinished is settled first (``recover``)."""
    if not str(expect or "").strip():
        raise AccountsError(VERSION_NEEDED, "version_needed")
    with routing._lock():
        recover()
        with config.mutate_config() as data:
            config.check_accounts_version(data, expect)
            yield data


# ---- one account change, one transaction ------------------------------------------------

_txn = threading.local()


def _cache_ops(lines: list[dict], declines=None) -> list[tuple]:
    """The changes outside the config an owner's change makes, from its lines
    and its begin line's ``declines``: each move's usage cache row follows
    it, each stopped account's row goes, then each login it tracked or
    stopped leaves or joins Not offered again."""
    owner = [line for line in lines if (line.get("written_by") or {}).get("kind") == "owner"]
    return ([("move", line["account"], line["target"]) for line in owner
             if line.get("type") == "account_move" and line.get("account") and line.get("target")]
            + [("drop", line["account"]) for line in owner
               if line.get("type") == "account_note" and line.get("state") == "untracked" and line.get("account")]
            + [("decline", d["account"], d.get("why")) for d in declines or []
               if isinstance(d, dict) and isinstance(d.get("account"), str)])


def _apply_cache(ops: list[tuple], *, again: bool = False) -> None:
    """Apply the changes (``again``: possibly a second time, by ``recover``);
    a rollback puts back what ``_snapshot`` read before them (``_restore``)."""
    for op in ops:
        if op[0] == "move":
            _move_row(op[1], op[2], again=again)
        elif op[0] == "drop":
            _drop_row(op[1])
        else:
            harness, _, home = op[1].partition("|")
            _set_declined(harness, _account_path({"harness": harness, "home": home or None}), op[2])


def _transact(data: dict, before: dict, lines: list[dict], declines: list[dict]) -> dict:
    """Commit one account change as a transaction, under the routing and
    config locks: its intent (its lines, and the account list's version
    before and after) goes into the routing record first; then the config
    and the usage cache; then its ``done`` line. A step that fails rolls back
    all it touched (the config, the cache rows, Not offered again) and aborts
    it, and is raised, never returned as
    done. A change cut off part way is finished or rolled back by
    ``recover`` before anything reads it, so the list and the record's
    history never disagree."""
    failed = "Khipu could not save this change to its accounts, so nothing changed"
    try:
        txn = routing.account_txn_begin(lines, before=config.accounts_version(before),
                                        after=config.accounts_version(data), declines=declines)
    except routing.RouteError as err:
        raise AccountsError(f"{failed}: {err}", "record_failed") from None
    ops = _cache_ops(lines, declines)
    _txn.open = True
    try:
        data[config.ACCOUNT_CHANGE_OPEN] = txn
        try:
            config.save_config(data)
        except OSError:
            data.pop(config.ACCOUNT_CHANGE_OPEN, None)
            _abort(txn)
            raise AccountsError(f"{failed}: Khipu can't write its config file", "write_failed") from None
        snap = None
        try:
            snap = _snapshot(ops)
            _apply_cache(ops)
            out = routing.account_txn_end(txn, "done")
        except (OSError, routing.RouteError, AccountsError):
            changed = dict(data)
            data.clear()
            data.update(before)
            try:
                config.save_config(data)
            except OSError:
                # The config holds the change: it is finished on the next read.
                data.clear()
                data.update(changed)
                raise AccountsError("Khipu could not finish this change to its accounts; it finishes it the next "
                                    "time it reads them", "record_failed") from None
            if snap is not None:
                with contextlib.suppress(OSError, AccountsError):
                    _restore(snap)
            _abort(txn)
            raise AccountsError(failed, "record_failed") from None
        data.pop(config.ACCOUNT_CHANGE_OPEN, None)
        try:
            config.save_config(data)
        except OSError:
            data[config.ACCOUNT_CHANGE_OPEN] = txn  # done: the next read clears it
        return out
    finally:
        _txn.open = False


def _abort(txn: str) -> None:
    # An abort that can't be written is written by ``recover``: the config
    # names the list as it was, not the one this change made.
    with contextlib.suppress(routing.RouteError):
        routing.account_txn_end(txn, "abort")


UNFINISHED = ("Khipu could not finish an earlier change to its accounts ({why}), so it changes no account "
              "until it can; nothing changed")


def recover() -> None:
    """Finish or roll back each account change left unfinished (a process
    stopped between its steps): done when the config holds the list it
    made, its other changes applied again; else aborted, its lines left
    out. Runs before any read of the accounts or the routing record; never
    inside a change of this thread's own.

    One it can neither finish nor roll back raises ``change_unfinished``:
    every account write (``_locked``) is then refused, since a later change
    would leave the config at another list and so abort the one the config
    already holds. Until then the record counts its lines as the config
    holds them (``routing._split_txns``)."""
    if getattr(_txn, "open", False):
        return
    try:
        marked = bool(config.read_config_unsettled().get(config.ACCOUNT_CHANGE_OPEN))
        if not marked and (not routing.events_file().exists() or not routing.account_txns_open()):
            return
        with routing._lock():
            unfinished = routing.account_txns_open()
            with config.mutate_config() as data:
                version = config.accounts_version(data)
                for begin, lines in unfinished:
                    if version == begin.get("list_after"):
                        _apply_cache(_cache_ops(lines, begin.get("declines")), again=True)
                        routing.account_txn_end(begin["id"], "done")
                    else:
                        routing.account_txn_end(begin["id"], "abort")
                data.pop(config.ACCOUNT_CHANGE_OPEN, None)
    except OSError as err:
        raise AccountsError(UNFINISHED.format(why=f"it can't write a file it keeps: {err.strerror or err}"),
                            "change_unfinished") from None
    except (routing.RouteError, AccountsError) as err:
        raise AccountsError(UNFINISHED.format(why=str(err)), "change_unfinished") from None


def _installed_defaults() -> list[dict]:
    """The list an unset ``usage_accounts`` reads: each installed CLI's
    default login, home-less."""
    return [{"label": usage.LABELS[h], "harness": h} for h in usage.HARNESSES if usage._command(h)]


def _plan(before: list[dict], after: list[dict], *, moves: bool) -> list[tuple]:
    """The operations that turn one list into another (each as ``known_of``
    gives it), by key: a key in both under another name is renamed; with
    ``moves``, a key gone and a new one of the same CLI under the same name
    is that account's login folder moved; every other key gone is stopped
    and every other new one tracked. A change of order alone is a reorder,
    which needs no line in the record."""
    old = {a["key"]: a for a in before}
    new = {a["key"]: a for a in after}
    gone = [k for k in old if k not in new]
    came = [k for k in new if k not in old]
    ops: list[tuple] = []
    if moves:
        for key in list(gone):
            twin = next((n for n in came if new[n]["harness"] == old[key]["harness"]
                         and new[n]["label"] == old[key]["label"]), None)
            if twin is not None:
                ops.append(("move", key, twin))
                gone.remove(key)
                came.remove(twin)
    ops += [("rename", k) for k in old if k in new and old[k]["label"] != new[k]["label"]]
    ops += [("stop", k) for k in gone]
    ops += [("track", k) for k in came]
    return ops


def _commit(data: dict, stored, base: list, parsed: list, *, via: str, request: dict | None = None,
            none: bool = False, view: dict | None = None, keep_priority: bool = False, keep_pause: bool = False,
            keep_lanes: bool = False, end_holds=(), confirmed: list | None = None) -> dict:
    """The one write of the account list, under ``_locked``: ``base`` (the
    list as it stood) becomes ``parsed`` (checked by
    ``usage.parse_usage_accounts``) through the operations ``_plan`` finds,
    each with its effects and its lines in the routing record, committed
    as one transaction (``_transact``): the record's intent, one config
    write, the usage cache, then the record's ``done``.

    The owner's: lanes move to keys first (``convert_lanes``), so a rename
    breaks none; a move carries its lanes and its usage cache row, and its
    ``account_move`` forwards the old key's records; a stop takes its lanes
    (unless ``keep_lanes``), its priority entry, its pause and the holds in
    ``end_holds``, and its cache row; each rename and stop carries the lanes
    whose label it left to one account (``lanes_cut``); a track's note ends
    any forwarding of its key. A conversation's at the owner's ``request``:
    the same list change and notes, written by the conversation and kept
    for the owner to confirm or undo; no move (moving a login folder is the
    owner's), and the owner's own settings (lanes, priority, pause, holds,
    Not offered again) stay as they are.

    The owner's confirm of that request passes ``confirmed``, the list as it
    stood before it: each account the request took off then gets the owner's
    Stop tracking, as above; its other notes are the request's own."""
    owner = request is None
    before = copy.deepcopy(data)
    after_list = parsed if parsed or none else _installed_defaults()
    known, after = known_of(base if confirmed is None else confirmed), known_of(after_list)
    ops = _plan(known, after, moves=owner and confirmed is None)
    waiting = data.get("usage_accounts_request")
    if owner and confirmed is None and isinstance(waiting, dict):
        # Any other owner write ends a conversation's change still waiting
        # (decision 15), and so accepts it: each account it took off that
        # this write leaves off gets the owner's full Stop tracking, as his
        # Confirm gives it, never a list change alone.
        listed = {a["key"] for a in [*known, *after]}
        taken = [a for a in known_of(_request_prior(waiting)) if a["key"] not in listed]
        known = known + taken
        ops += [("stop", a["key"]) for a in taken]
    old = {a["key"]: a for a in known}
    moves = [(op[1], op[2]) for op in ops if op[0] == "move"]
    renames = [op[1] for op in ops if op[0] == "rename"]
    stops = [op[1] for op in ops if op[0] == "stop"]
    tracks = [op[1] for op in ops if op[0] == "track"]
    repairs = convert_lanes(data, known)
    stop_lanes = {k: _lanes_on(data.get("routing_lane_map") or {}, k, known) for k in stops}
    moved_lanes = []
    if owner:
        for was, now in moves:
            for runner, lane in _lanes_on(data.get("routing_lane_map") or {}, was, known):
                data["routing_lane_map"][runner][lane] = now
                moved_lanes.append((runner, lane))
            # A conversation's lane map still waiting for the owner keeps the
            # map it replaced for his Undo: its lanes follow the move too, so
            # Undo never hands them to whoever tracks the old folder next.
            waiting = routing._setting_requests(data).get("routing_lane_map") or {}
            if isinstance(waiting.get("prior"), dict):
                for runner, lane in _lanes_on(waiting["prior"], was, known):
                    waiting["prior"][runner][lane] = now
        if not keep_lanes and any(stop_lanes.values()):
            lane_map = {r: dict(v) for r, v in data["routing_lane_map"].items()}
            for runner, lane in (pair for k in stops for pair in stop_lanes[k]):
                lane_map[runner].pop(lane, None)
                if not lane_map[runner]:
                    lane_map.pop(runner)
            data["routing_lane_map"] = lane_map
        waiting_lanes = routing._setting_requests(data).get("routing_lane_map") or {}
        if not keep_lanes and stops and isinstance(waiting_lanes.get("prior"), dict):
            # The map a waiting lane request replaced loses a stopped
            # account's lanes too, so Undo never gives them back to it.
            prior = waiting_lanes["prior"]
            for runner, lane in (pair for k in stops for pair in _lanes_on(prior, k, known)):
                prior[runner].pop(lane, None)
                if not prior[runner]:
                    prior.pop(runner)
    events: list[dict] = [{"type": "account_move", "account": was, "target": now} for was, now in moves]
    notes = [{"type": "account_note", "account": k, "state": "renamed", "text": old[k]["label"]}
             for k in renames if confirmed is None]
    done: dict = {"priority": None, "pause": None}
    if stops and owner:
        view = routing.read_view() if view is None else view
        stop_events, done = _stop_events(view, stops, keep_priority=keep_priority, keep_pause=keep_pause,
                                         end_holds=list(end_holds))
        events += stop_events
    notes += [{"type": "account_note", "account": k, "state": "untracked", "text": old[k]["label"]} for k in stops]
    if notes and _cuts(repairs):
        # What a lane said while two accounts carried its label stays
        # unmatched once this change leaves the label to one.
        notes[0] = dict(notes[0], **_cuts(repairs))
    events += notes + [{"type": "account_note", "account": k, "state": "tracked"} for k in tracks
                       if confirmed is None]
    held = None
    if not owner:
        held = dict(request, prior=stored, prior_none=data.get("usage_accounts_none"))
    _store(data, parsed, none=none, request=held)
    version = config.accounts_version(data)
    if owner:
        lines = routing.owner_lines(events, via)
    else:
        by = request.get("by") if isinstance(request.get("by"), dict) else routing.conversation_author()
        lines = routing.request_lines(events, by, str(request.get("request") or ""))
    # The owner's: a login tracked leaves Not offered again, one stopped is
    # not offered again, as part of the same transaction.
    declines = ([{"account": k, "why": None} for k in tracks] + [{"account": k, "why": STOPPED_WHY} for k in stops]
                if owner else [])
    if lines or declines:
        record = _transact(data, before, lines, declines)
    else:
        # No line in the record (a reorder): the config alone.
        config.save_config(data)
        record = {"ok": True, "ids": [], "version": None}
    return {"version": version, "repairs": repairs, "record": record, "done": done, "stop_lanes": stop_lanes,
            "moved_lanes": moved_lanes, "owner": owner}


def _request_prior(request: dict) -> list:
    """The list a conversation's waiting change replaced, as Undo puts it
    back: the stored list, none after "Read no accounts", else the default
    accounts."""
    prior = request.get("prior")
    if isinstance(prior, list) and prior:
        return prior
    return [] if request.get("prior_none") is True else _installed_defaults()


def _canonical(raw, stored):
    """Each new or changed entry's home as ``canonical_home`` stores it (its
    real folder; none for its CLI's default login), before the list is
    checked: a symlink or a full path then names the same account as the
    folder it reaches, and the naming rules judge the home the entry is
    stored with. An entry kept as stored, and one whose folder cannot be
    made canonical, goes on as given for ``usage.parse_usage_accounts``.

    Stored entries are made canonical the same way, so a changed entry
    whose folder is a stored account's (by a symlink, say) keeps that
    account's stored home and so its key: a rename stays a rename."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    if not isinstance(raw, list):
        return raw
    kept = [e for e in stored or [] if isinstance(e, dict)]

    def folder(harness: str, home):
        try:
            return True, canonical_home(harness, home)
        except AccountsError:
            return False, None

    out = []
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("home"), str) and entry["home"].strip() \
                and isinstance(entry.get("harness"), str) and not any(usage._same_entry(entry, k) for k in kept):
            harness = entry["harness"].strip().lower()
            known = harness in usage.HARNESSES
            made, home = folder(harness, entry["home"]) if known else (False, None)
            if not made:
                home = entry["home"]
            else:
                twin = next((k for k in kept if str(k.get("harness") or "").strip().lower() == harness
                             and isinstance(k.get("home"), str) and k["home"].strip()
                             and folder(harness, k["home"]) == (True, home)), None)
                if twin is not None:
                    home = twin["home"]
            entry = {k: v for k, v in entry.items() if k != "home"}
            if home:
                entry["home"] = home
        out.append(entry)
    return out


def save_list(raw, *, expect, via: str = "app", request: dict | None = None) -> dict:
    """A save of the whole list (``khipu config --set/--unset
    usage_accounts``: the app, a typed yes, or a conversation at the owner's
    ``request``), turned into the operations it implies and committed as
    they are (``_commit``). None or an empty list goes back to every
    installed CLI's default login."""
    with _locked(expect) as data:
        if request is not None and isinstance(data.get("usage_accounts_request"), dict):
            raise AccountsError("a conversation's change to the account list is still waiting for the owner; "
                                "he confirms or undoes it in Settings first", "request_waiting")
        stored, base = current_list(data)
        parsed = [] if raw is None else usage.parse_usage_accounts(_canonical(raw, stored), previous=stored)
        # Stop's rule: a list left with only invalid entries reads no account,
        # which only Stop tracking with its confirmation may do.
        if parsed and known_of(base) and not known_of(parsed):
            raise AccountsError("this leaves no account Khipu can read on this Mac; Stop tracking on the last "
                                "account asks first", "last_account")
        res = _commit(data, stored, base, parsed, via=via, request=request)
    out = {"ok": True, "version": res["version"], "lane_repairs": res["repairs"]}
    return out


def _mentions(label: str, view: dict | None = None) -> list[dict]:
    """The rules and the note whose words name ``label`` (ignoring case):
    free text Khipu never rewrites (decision 13)."""
    view = routing.read_view() if view is None else view
    word = label.casefold()
    out = [{"kind": "rule", "n": r["n"]} for r in view.get("rules") or []
           if isinstance(r.get("text"), str) and word in r["text"].casefold()]
    note = view.get("note") or {}
    if isinstance(note.get("text"), str) and word in note["text"].casefold():
        out.append({"kind": "note"})
    return out


# ---- the usage cache row ---------------------------------------------------------------


@contextlib.contextmanager
def _cache():
    """The usage cache rows under its write lock, saved after. Inside a
    transaction a save is not best-effort (``usage._save_cache`` is): one
    that fails raises ``OSError`` and the change rolls back."""
    fd = usage._open_lock(usage.WRITE_LOCK_NAME)
    try:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX)
        rows = usage._load_cache()
        yield rows
        usage._save_cache(rows, strict=True)
    finally:
        usage._release(fd)


def _drop_row(key: str) -> None:
    with _cache() as rows:
        rows.pop(key, None)


def _move_row(old: str, new: str, *, again: bool = False) -> None:
    """The moved account's figures, never an earlier tenant's of the folder
    it moved to: with none of its own, the row goes. ``again`` (``recover``
    finishing a change that may have moved it already) keeps the row there."""
    with _cache() as rows:
        row = rows.pop(old, None)
        if row is not None:
            rows[new] = row
        elif not again:
            rows.pop(new, None)


def _login_of(key: str) -> tuple[str, object]:
    harness, _, home = key.partition("|")
    return harness, _account_path({"harness": harness, "home": home or None})


def _snapshot(ops: list[tuple]) -> tuple:
    """What ``ops`` touch outside the config, as it stands before they run:
    the cache rows of their keys and the Not offered again entries of their
    logins, so a rollback puts back exactly these."""
    keys = {k for op in ops if op[0] in ("move", "drop") for k in op[1:]}
    rows = usage._load_cache()
    logins = [_login_of(op[1]) for op in ops if op[0] == "decline"]
    declined = [copy.deepcopy(i) for i in discover.turned_down() if any(_same_login(i, *lg) for lg in logins)]
    return keys, {k: copy.deepcopy(rows[k]) for k in keys if k in rows}, logins, declined


def _restore(snap: tuple) -> None:
    keys, rows_before, logins, declined = snap
    if keys:
        with _cache() as rows:
            for key in keys:
                rows.pop(key, None)
            rows.update(rows_before)
    if logins:
        with _declined_lock():
            items = [i for i in discover.turned_down() if not any(_same_login(i, *lg) for lg in logins)]
            _save_declined(items + declined)


# ---- Not offered again ------------------------------------------------------------------


@contextlib.contextmanager
def _declined_lock():
    with discover._data_state(create=True) as dfd:
        fd = None
        if dfd is not None:
            with contextlib.suppress(OSError):
                fd = os.open(DECLINED_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        usage._release(fd)


def _save_declined(items: list[dict]) -> None:
    """``discover.declined_file()`` written as ``discover._save_store``
    writes its store: below the data folder with no symlink followed, to a
    temp file renamed into place."""
    text = json.dumps(items, indent=2, sort_keys=True)
    failed = AccountsError("Khipu can't write its list of logins not offered again", "write_failed")
    with discover._data_state(create=True) as dfd:
        if dfd is None:
            raise failed
        tmp = f"{discover.DECLINED_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                os.write(fd, text.encode("utf-8"))
            finally:
                os.close(fd)
            os.rename(tmp, discover.DECLINED_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise failed from None


def _login_path(harness: str, folder):
    """The folder a decline names: the given one (``~/`` or full), else the
    CLI's default login; None for Muse."""
    if harness == "muse":
        if folder:
            raise AccountsError("Muse has one sign-in, so it takes no login folder", "muse_folder", field="home")
        return None
    raw = _one_line(folder, "a login folder", field="home")
    if raw is None:
        return discover.default_folder(harness)
    path = discover._expand(raw)
    if path is None:
        raise AccountsError("give the login folder starting with ~/ or as a full path", "bad_folder", field="home")
    return path


def _same_login(item: dict, harness: str, path) -> bool:
    """Whether a Not offered again entry is this login: by its stored
    identity, or by its stored folder (a folder that is gone still matches)."""
    if item.get("cli") != harness:
        return False
    if harness == "muse":
        return True
    ident, shown = discover.identity(path), discover.display_path(path)
    if item.get("identity") == ident or item.get("path") == shown:
        return True
    stored = discover._expand(item.get("path")) if item.get("path") else discover.default_folder(harness)
    return stored is not None and discover.identity(stored) == ident


def _decline_entry(harness: str, path, why: str) -> dict:
    return {"cli": harness, "path": discover.display_path(path) if path is not None else None,
            "identity": discover.identity(path) if path is not None else discover.MUSE_IDENTITY, "why": why,
            "at": _now_iso()}


def _set_declined(harness: str, path, why: str | None) -> int:
    """Add this login to Not offered again (``why``), or take it off (None);
    how many entries it took off."""
    with _declined_lock():
        items = discover.turned_down()
        kept = [i for i in items if not _same_login(i, harness, path)]
        removed = len(items) - len(kept)
        if why is not None:
            kept.append(_decline_entry(harness, path, why))
        if why is not None or removed:
            _save_declined(kept)
    return removed


def _account_path(account: dict):
    if account["harness"] == "muse":
        return None
    return usage._home_path(account["home"]) if account.get("home") else discover.default_folder(account["harness"])


def decline(harness: str, folder=None) -> dict:
    """Not this one: the login is not offered again until Offer again."""
    harness = _harness(harness)
    path = _login_path(harness, folder)
    _set_declined(harness, path, DECLINED_WHY)
    return {"ok": True, "declined": len(discover.turned_down())}


def offer_again(harness: str, folder=None) -> dict:
    harness = _harness(harness)
    path = _login_path(harness, folder)
    removed = _set_declined(harness, path, None)
    return {"ok": True, "removed": removed, "declined": len(discover.turned_down())}


def _harness(raw) -> str:
    harness = str(raw or "").strip().lower()
    if harness not in usage.HARNESSES:
        raise AccountsError("that names no CLI Khipu reads (claude, codex, grok, muse)", "unknown_cli",
                            field="harness")
    return harness


# ---- the owner's actions ------------------------------------------------------------------


def _track_entries(raw) -> list[tuple[str, str | None, str | None]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise AccountsError('--entries must be JSON: [{"harness", "home", "label"}]', "bad_list") from None
    if not isinstance(raw, list) or not raw or len(raw) > usage.ACCOUNTS_MAX:
        raise AccountsError('--entries must list the accounts to track: [{"harness", "home", "label"}]',
                            "bad_list")
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) - {"harness", "home", "label"}:
            raise AccountsError(f"entry {i + 1} takes harness, home and label", "bad_entry", index=i)
        harness = str(item.get("harness") or "").strip().lower()
        if harness not in usage.HARNESSES:
            raise AccountsError(f"entry {i + 1} names no CLI Khipu reads", "unknown_cli", index=i, field="harness")
        out.append((harness, _one_line(item.get("home"), "a login folder", field="home", index=i),
                    _one_line(item.get("label"), "a name", field="label", index=i)))
    return out


def track(entries, *, expect=None, shown_defaults=None, read: bool = True, via: str = "app") -> dict:
    """Track…, the dialog's Save, setup's Next and automatic tracking: one
    save path (section 5). One config write: the defaults written out when
    no list is stored, each new account with its one canonical home and a
    name (its own, or the default name made unique), the whole list checked
    by ``usage.parse_usage_accounts`` against the stored one. Then the login
    leaves Not offered again, the record notes it, and it is read once."""
    wanted = _track_entries(entries)
    with _locked(expect) as data:
        if via == "auto" and isinstance(data.get("usage_accounts_request"), dict):
            # Saving would end a conversation's change still waiting for the
            # owner (decision 15); only the owner's own write may.
            raise AccountsError("a conversation's change to the account list is still waiting for the owner",
                                "request_waiting")
        stored, base = current_list(data, shown_defaults)
        taken = {usage._account_from_entry(e)["label"].casefold() for e in base if isinstance(e, dict)}
        new: list[dict] = []
        for i, (harness, raw_home, label) in enumerate(wanted):
            where = len(base) + i
            try:
                home = canonical_home(harness, raw_home)
            except AccountsError as err:
                err.index = where
                raise
            if label is None:
                path = usage.default_login(harness) if home is None else usage._home_path(home)
                label = discover.default_name(harness, str(path) if path is not None else None, default=home is None)
                if label is None:
                    raise AccountsError("Khipu can't suggest a name for this login folder; give it one",
                                        "name_needed", index=where, field="label")
                label = _unique_name(label, taken)
            taken.add(label.casefold())
            entry = {"label": label, "harness": harness}
            if home:
                entry["home"] = home
            new.append(entry)
        parsed = usage.parse_usage_accounts([*base, *new], previous=stored)
        added = [usage._account_from_entry(e) for e in parsed[len(base):]]
        keys = [usage._key(a) for a in added]
        res = _commit(data, stored, base, parsed, via=via)
    rows = usage.report(fresh=True, accounts=added)["accounts"] if read else []
    out = {"ok": True, "version": res["version"],
           "accounts": [{"key": k, "label": a["label"]} for k, a in zip(keys, added)],
           "rows": rows, "lane_repairs": res["repairs"]}
    return out


def rename(key, name, *, expect=None, via: str = "app") -> dict:
    """The account's name only; its key never changes (decision 7). Lanes
    move to keys first, so none is lost to the new name."""
    key = normal_key(key)
    name = _one_line(name, "a name", field="label")
    if not name:
        raise AccountsError("give the account a name", "not_one_line", field="label")
    with _locked(expect) as data:
        stored, base = current_list(data)
        i = _find(base, key)
        old = usage._account_from_entry(base[i])["label"]
        changed = list(base)
        changed[i] = dict(base[i], label=name)
        parsed = usage.parse_usage_accounts(changed, previous=stored)
        res = _commit(data, stored, base, parsed, via=via)
        lanes = len(_lanes_on(data.get("routing_lane_map") or {}, key, known_of(parsed)))
    out = {"ok": True, "version": res["version"], "key": key, "label": name, "was": old, "mentions": _mentions(old),
           "lanes": lanes, "lane_repairs": res["repairs"]}
    return out


def _cuts(repairs: list[dict]) -> dict:
    """The lanes whose label two or more accounts carried before this change
    (``lanes_cut`` on its note): what they reported while it named no one
    account stays unmatched once one name is left."""
    pairs = [[r["runner"], r["lane"]] for r in repairs if (r.get("matches") or 0) >= 2]
    return {"lanes_cut": pairs} if pairs else {}


def _stop_events(view: dict, keys: list[str], *, keep_priority: bool, keep_pause: bool,
                 end_holds: list[str]) -> tuple[list[dict], dict]:
    """The routing record's lines for a Stop tracking of ``keys``, from the
    view read before it: the priority without them (or cleared), their
    pauses lifted and the holds the owner ticked ended. Their notes are
    the caller's."""
    done: dict = {"priority": None, "pause": None}
    events: list[dict] = []
    held = view.get("priority")
    replaced = ((held or {}).get("replaced") or {}).get("entries") or []
    if held and any(e["account"] in keys for e in [*held["entries"], *replaced]):
        rest = [e for e in held["entries"] if e["account"] not in keys]
        if keep_priority:
            done["priority"] = "kept"
        elif held.get("pending"):
            # A conversation's request stays the owner's to confirm or undo,
            # without the account, and so does the priority it replaced.
            events += [{"type": "priority_drop", "account": key} for key in keys]
            done["priority"] = "removed"
        else:
            events.append({"type": "priority_set", "until": held.get("until"),
                           "entries": [{"account": e["account"], "label": e["label"], "role": e.get("role"),
                                        "until": e.get("until")} for e in rest]}
                          if rest else {"type": "priority_clear"})
            done["priority"] = "removed"
    for key in keys:
        entry = next((a for a in view["accounts"] if a["key"] == key), None)
        if entry is not None and entry["state"] == routing.PAUSED:
            if keep_pause:
                done["pause"] = "kept"
            else:
                events.append({"type": "lift", "account": key})
                done["pause"] = "ended"
    events += [{"type": "hold_end", "target": hold} for hold in end_holds]
    return events, done


def stop(key, *, expect=None, keep_priority: bool = False, keep_pause: bool = False, keep_lanes: bool = False,
         end_holds=(), confirm_last: bool = False, via: str = "app") -> dict:
    """Stop tracking (section 9, decision 16's defaults): one config write
    takes the account off the list ("Read no accounts" after the last one,
    only with ``confirm_last``) and off its lanes; then its priority entry
    goes, its pause ends and each hold named in ``end_holds`` ends, in one
    write to the routing record; its usage cache row goes and the login is
    not offered again."""
    key = normal_key(key)
    end_holds = [str(h) for h in end_holds or ()]
    with _locked(expect) as data:
        view = routing.read_view()
        holds_on = [h["id"] for h in view.get("holds") or []
                    if h["scope"].get("kind") == "account" and h["scope"].get("account") == key]
        live = {h["id"] for h in view.get("holds") or []}
        if any(hold not in live for hold in end_holds):
            raise AccountsError("that hold is not in effect", "no_such_hold")
        if any(hold not in holds_on for hold in end_holds):
            # Stop ends only this account's own holds; another's is its own.
            raise AccountsError("that hold is not on this account", "no_such_hold")
        stored, base = current_list(data)
        i = _find(base, key)
        account = usage._account_from_entry(base[i])
        rest = base[:i] + base[i + 1:]
        # Invalid entries kept beside it are read as no account.
        if not known_of(rest) and not confirm_last:
            raise AccountsError("this is the last account Khipu tracks on this Mac; stopping it leaves none read",
                                "last_account")
        parsed = usage.parse_usage_accounts(rest, previous=stored) if rest else []
        res = _commit(data, stored, base, parsed, via=via, none=not rest, view=view, keep_priority=keep_priority,
                      keep_pause=keep_pause, keep_lanes=keep_lanes, end_holds=end_holds)
    done = res["done"]
    shown = [{"runner": r, "lane": lane} for r, lane in res["stop_lanes"].get(key, [])]
    out = {"ok": True, "version": res["version"], "key": key, "label": account["label"], "none": not rest,
           "priority": done["priority"], "pause": done["pause"],
           "lanes_removed": [] if keep_lanes else shown, "lanes_kept": shown if keep_lanes else [],
           "holds_ended": end_holds, "holds_kept": [h for h in holds_on if h not in end_holds],
           "mentions": _mentions(account["label"], view), "declined": True, "lane_repairs": res["repairs"]}
    return out


def move(key, folder, *, expect=None, via: str = "app") -> dict:
    """Re-key an account whose login folder moved (section 7): one owner
    write gives it the new folder's canonical home and moves its lanes; its
    usage cache row follows; the routing record's ``account_move`` forwards
    every record on the old key, later ones included, to the new one."""
    key = normal_key(key)
    harness = key.partition("|")[0]
    if harness == "muse":
        raise AccountsError("Muse has one sign-in, so it takes no login folder", "muse_folder", field="home")
    path = discover._expand(_one_line(folder, "a login folder", field="home") or "")
    if path is None or not discover._is_dir(path):
        raise AccountsError("Khipu can't find that folder", "folder_not_found", field="home")
    home = canonical_home(harness, folder)
    with _locked(expect) as data:
        stored, base = current_list(data)
        i = _find(base, key)
        label = usage._account_from_entry(base[i])["label"]
        entry = {k: v for k, v in base[i].items() if k != "home"}
        if home:
            entry["home"] = home
        new_key = usage._key(usage._account_from_entry(entry))
        if new_key == key:
            raise AccountsError("this account already reads that folder", "login_taken", index=i, field="home")
        changed = list(base)
        changed[i] = entry
        parsed = usage.parse_usage_accounts(changed, previous=stored)
        res = _commit(data, stored, base, parsed, via=via)
    out = {"ok": True, "version": res["version"], "key": new_key, "was": key, "label": label,
           "lanes": len(res["moved_lanes"]), "lane_repairs": res["repairs"]}
    return out


def set_auto_track(on: bool) -> dict:
    with config.mutate_config() as data:
        if on:
            data[config.AUTO_TRACK_KEY] = True
        else:
            data.pop(config.AUTO_TRACK_KEY, None)
        config.save_config(data)
    return {"ok": True, "auto_track": bool(on)}


def auto_track(found: dict) -> list[dict]:
    """With the switch on, every find that Home would offer (signed in, and
    a conversation used it) tracked through ``track``, one at a time, with
    its default name. Off, nothing is saved. Undo is Stop tracking, which
    also leaves it not offered again."""
    if not config.auto_track():
        return []
    out = []
    for cand in found.get("candidates") or []:
        if not cand.get("offer_on_home"):
            continue
        entry = {"harness": cand["cli"], "home": None if cand.get("default") else cand.get("path"), "label": None}
        try:
            done = track([entry], expect=config.accounts_version(), via="auto")
        except (AccountsError, routing.RouteError, config.InvalidConfigDocument):
            continue
        out += [dict(a, via="auto") for a in done["accounts"]]
    return out


def answer_request(action: str, *, expect=None, via: str = "app") -> dict:
    """The owner's answer to a conversation's change to the list (decision
    15): confirm keeps it; undo puts back the list it replaced. ``expect``,
    the list's version as he read it, is required: an answer to a request
    he saw never lands on a newer one."""
    with _locked(expect) as data:
        request = data.get("usage_accounts_request")
        if not isinstance(request, dict):
            raise AccountsError("no conversation's change to the account list is waiting", "no_request")
        stored, base = current_list(data)
        if action == "undo":
            prior = request.get("prior")
            res = _commit(data, stored, base, prior if isinstance(prior, list) else [], via=via,
                          none=request.get("prior_none") is True)
        else:
            res = _commit(data, stored, base, list(stored or []), via=via, none=usage.reads_none(data),
                          confirmed=_request_prior(request))
    return {"ok": True, "version": res["version"], "action": action}


def status() -> dict:
    """``khipu accounts status``: what the app's Accounts group reads. Paths
    included: the app's, never an agent's."""
    recover()
    st = usage.accounts_status()
    data = config.load_config()
    lane_map = routing.settings(data)["routing_lane_map"]
    accounts = [dict(a, key=usage._key(a)) if not a.get("invalid") else dict(a) for a in st["accounts"]]
    return {"ok": True, "version": st["version"], "none": st["none"], "auto_track": config.auto_track(),
            "value": st["value"], "accounts": accounts, "user_home": st["user_home"],
            "declined": discover.turned_down(), "request": st["request"],
            "lane_repairs": routing._lane_repairs(lane_map, routing.accounts())}


# ---- the CLI -------------------------------------------------------------------------------


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def run_cli(args) -> int:
    """``khipu accounts``: one JSON object; a refusal is ``{"ok": false,
    "error", "code"}`` with exit 2. Every write is the owner's
    (``routing._owner_via``: the app, or a typed yes on a terminal)."""
    cmd = getattr(args, "accounts_cmd", None) or "status"
    auth = routing._OWNER_AUTH.set(None)
    try:
        if cmd == "status":
            _print(status())
            return 0
        via = routing._owner_via(args)
        if via is None:
            _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
            return 2
        expect = getattr(args, "expect", None)
        if cmd == "track":
            out = track(args.entries, expect=expect, shown_defaults=args.shown_defaults, read=not args.no_read,
                        via=via)
        elif cmd == "rename":
            out = rename(args.account, args.name, expect=expect, via=via)
        elif cmd == "stop":
            out = stop(args.account, expect=expect, keep_priority=args.keep_priority, keep_pause=args.keep_pause,
                       keep_lanes=args.keep_lanes, end_holds=args.end_hold or (), confirm_last=args.confirm_last,
                       via=via)
        elif cmd == "move":
            out = move(args.account, args.folder, expect=expect, via=via)
        elif cmd == "decline":
            out = decline(args.harness, args.folder)
        elif cmd == "offer-again":
            out = offer_again(args.harness, args.folder)
        elif cmd == "auto-track":
            out = set_auto_track(args.state == "on")
        elif cmd == "request":
            out = answer_request(args.action, expect=expect, via=via)
        else:
            raise AccountsError(f"unknown accounts command {cmd!r}", "bad_command")
        _print(out)
        return 0
    except AccountsError as err:
        _print(err.refusal())
        return 2
    except routing.RouteError as err:
        _print({"ok": False, "error": str(err), "code": getattr(err, "code", None) or "record_refused"})
        return 2
    except config.InvalidConfigDocument:
        _print({"ok": False, "error": "Khipu can't read its config file, so it changed nothing",
                "code": "config_unreadable"})
        return 2
    finally:
        routing._OWNER_AUTH.reset(auth)
