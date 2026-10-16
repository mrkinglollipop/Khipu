"""Tracking and managing accounts (settings clarity phase 9, 9B).

Scope: docs/plans/2026-10-15-settings-clarity.md, "Phase 9": decisions 4 to
20 and sections 5 (Tracking), 7 (Living with an account), 9 (Stop tracking)
and 11 (the agents' view). Every write here is the owner's: the CLI
(``khipu accounts``) lets it through only from the app or after a typed yes
(``routing._owner_via``), and no MCP tool reaches it. Per Mac; nothing here
syncs through the hub.

The account list is ``usage_accounts`` in config.json. Each write holds
``config.config_write_lock`` from its read to its save and is refused when
the version the caller read (``--expect``, ``config.accounts_version``) has
moved. Account keys (``usage._key``: the CLI and the login folder) never
change on rename (decision 7); a default login stays home-less (decision 6),
so writing the default list out changes no key.
"""
from __future__ import annotations

import contextlib
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


def _store(data: dict, entries: list) -> None:
    """The list into ``data`` as the owner's write: "Read no accounts" and a
    conversation's waiting request end with it."""
    if entries:
        data["usage_accounts"] = entries
    else:
        data.pop("usage_accounts", None)
    data.pop("usage_accounts_none", None)
    data.pop("usage_accounts_request", None)


@contextlib.contextmanager
def _locked(expect):
    """The config document under the write lock, version checked; the
    caller saves it. The routing lock is held around it (taken first), so
    the list's change and the record's lines about it fold as one version:
    no fold in between shows the account half changed."""
    with routing._lock(), config.config_write_lock():
        data = config._load_config_for_mutation()
        config.check_accounts_version(data, expect)
        yield data


def _notes(events: list[dict], via: str) -> dict:
    try:
        return routing.owner_writes(events, via)
    except routing.RouteError as err:
        # The account list is saved; only the routing record's line is not.
        return {"ok": False, "error": str(err)}


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
    fd = usage._open_lock(usage.WRITE_LOCK_NAME)
    try:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX)
        rows = usage._load_cache()
        yield rows
        usage._save_cache(rows)
    finally:
        usage._release(fd)


def _drop_row(key: str) -> None:
    with _cache() as rows:
        rows.pop(key, None)


def _move_row(old: str, new: str) -> None:
    with _cache() as rows:
        row = rows.pop(old, None)
        if row is not None and new not in rows:
            rows[new] = row


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
        known = known_of(base)
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
        repairs = convert_lanes(data, known)
        _store(data, parsed)
        config.save_config(data)
        version = config.accounts_version(data)
        added = [usage._account_from_entry(e) for e in parsed[len(base):]]
        keys = [usage._key(a) for a in added]
        record = _notes([{"type": "account_note", "account": k, "state": "tracked"} for k in keys], via)
    for account in added:
        _set_declined(account["harness"], _account_path(account), None)
    rows = usage.report(fresh=True, accounts=added)["accounts"] if read else []
    out = {"ok": True, "version": version, "accounts": [{"key": k, "label": a["label"]} for k, a in zip(keys, added)],
           "rows": rows, "lane_repairs": repairs}
    if not record.get("ok"):
        out["record_error"] = record.get("error")
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
        known = known_of(base)
        old = usage._account_from_entry(base[i])["label"]
        changed = list(base)
        changed[i] = dict(base[i], label=name)
        parsed = usage.parse_usage_accounts(changed, previous=stored)
        repairs = convert_lanes(data, known)
        _store(data, parsed)
        config.save_config(data)
        version = config.accounts_version(data)
        lanes = len(_lanes_on(data.get("routing_lane_map") or {}, key, known_of(parsed)))
        record = _notes([dict({"type": "account_note", "account": key, "state": "renamed", "text": old},
                              **_cuts(repairs))], via)
    out = {"ok": True, "version": version, "key": key, "label": name, "was": old, "mentions": _mentions(old),
           "lanes": lanes, "lane_repairs": repairs}
    if not record.get("ok"):
        out["record_error"] = record.get("error")
    return out


def _cuts(repairs: list[dict]) -> dict:
    """The lanes whose label two or more accounts carried before this change
    (``lanes_cut`` on its note): what they reported while it named no one
    account stays unmatched once one name is left."""
    pairs = [[r["runner"], r["lane"]] for r in repairs if (r.get("matches") or 0) >= 2]
    return {"lanes_cut": pairs} if pairs else {}


def _stop_events(view: dict, key: str, label: str, *, keep_priority: bool, keep_pause: bool,
                 end_holds: list[str], cuts: dict | None = None) -> tuple[list[dict], dict]:
    """The routing record's lines for a Stop tracking, from the view read
    before it: the priority without it (or cleared), its pause lifted, the
    holds the owner ticked ended, and the note."""
    done: dict = {"priority": None, "pause": None}
    events: list[dict] = []
    held = view.get("priority")
    if held and any(e["account"] == key for e in held["entries"]):
        rest = [e for e in held["entries"] if e["account"] != key]
        if keep_priority:
            done["priority"] = "kept"
        elif held.get("pending"):
            # A conversation's request is the owner's to confirm or undo first.
            done["priority"] = "pending"
        else:
            events.append({"type": "priority_set", "until": held.get("until"),
                           "entries": [{"account": e["account"], "label": e["label"], "role": e.get("role"),
                                        "until": e.get("until")} for e in rest]}
                          if rest else {"type": "priority_clear"})
            done["priority"] = "removed"
    entry = next((a for a in view["accounts"] if a["key"] == key), None)
    if entry is not None and entry["state"] == routing.PAUSED:
        if keep_pause:
            done["pause"] = "kept"
        else:
            events.append({"type": "lift", "account": key})
            done["pause"] = "ended"
    events += [{"type": "hold_end", "target": hold} for hold in end_holds]
    events.append(dict({"type": "account_note", "account": key, "state": "untracked", "text": label}, **(cuts or {})))
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
        if not rest and not confirm_last:
            raise AccountsError("this is the last account Khipu tracks on this Mac; stopping it leaves none read",
                                "last_account")
        known = known_of(base)
        repairs = convert_lanes(data, known)
        lanes = _lanes_on(data.get("routing_lane_map") or {}, key, known)
        if lanes and not keep_lanes:
            lane_map = {r: dict(v) for r, v in data["routing_lane_map"].items()}
            for runner, lane in lanes:
                lane_map[runner].pop(lane, None)
                if not lane_map[runner]:
                    lane_map.pop(runner)
            data["routing_lane_map"] = lane_map
        if rest:
            _store(data, usage.parse_usage_accounts(rest, previous=stored))
        else:
            _store(data, [])
            data["usage_accounts_none"] = True
        events, done = _stop_events(view, key, account["label"], keep_priority=keep_priority,
                                    keep_pause=keep_pause, end_holds=end_holds, cuts=_cuts(repairs))
        config.save_config(data)
        version = config.accounts_version(data)
        _drop_row(key)
        record = _notes(events, via)
    _set_declined(account["harness"], _account_path(account), STOPPED_WHY)
    shown = [{"runner": r, "lane": lane} for r, lane in lanes]
    out = {"ok": True, "version": version, "key": key, "label": account["label"], "none": not rest,
           "priority": done["priority"], "pause": done["pause"],
           "lanes_removed": [] if keep_lanes else shown, "lanes_kept": shown if keep_lanes else [],
           "holds_ended": end_holds, "holds_kept": [h for h in holds_on if h not in end_holds],
           "mentions": _mentions(account["label"], view), "declined": True, "lane_repairs": repairs}
    if not record.get("ok"):
        out["record_error"] = record.get("error")
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
        known = known_of(base)
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
        repairs = convert_lanes(data, known)
        moved = _lanes_on(data.get("routing_lane_map") or {}, key, known)
        for runner, lane in moved:
            data["routing_lane_map"][runner][lane] = new_key
        _store(data, parsed)
        config.save_config(data)
        version = config.accounts_version(data)
        _move_row(key, new_key)
        record = _notes([{"type": "account_move", "account": key, "target": new_key}], via)
    out = {"ok": True, "version": version, "key": new_key, "was": key, "label": label, "lanes": len(moved),
           "lane_repairs": repairs}
    if not record.get("ok"):
        out["record_error"] = record.get("error")
    return out


def set_auto_track(on: bool) -> dict:
    with config.config_write_lock():
        data = config._load_config_for_mutation()
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
            done = track([entry], via="auto")
        except (AccountsError, routing.RouteError, config.InvalidConfigDocument):
            continue
        out += [dict(a, via="auto") for a in done["accounts"]]
    return out


def answer_request(action: str) -> dict:
    """The owner's answer to a conversation's change to the list (decision
    15): confirm keeps it; undo puts back the list it replaced."""
    with config.config_write_lock():
        data = config._load_config_for_mutation()
        request = data.get("usage_accounts_request")
        if not isinstance(request, dict):
            raise AccountsError("no conversation's change to the account list is waiting", "no_request")
        if action == "undo":
            prior = request.get("prior")
            _store(data, prior if isinstance(prior, list) else [])
            if request.get("prior_none") is True:
                data["usage_accounts_none"] = True
        data.pop("usage_accounts_request", None)
        config.save_config(data)
        version = config.accounts_version(data)
    return {"ok": True, "version": version, "action": action}


def status() -> dict:
    """``khipu accounts status``: what the app's Accounts group reads. Paths
    included: the app's, never an agent's."""
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
            out = answer_request(args.action)
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
