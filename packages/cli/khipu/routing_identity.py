"""Pure account identity rules. Login keys belong to this Mac alone.

The usage cache and local history retain their login keys; public ids and
rename forwards never contain a login folder. Migration aliases expire,
but the historical binding and rename maps do not.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import PurePath


STABLE = re.compile(r"[a-z][a-z0-9_-]*:[a-z0-9]+(?:-[a-z0-9]+)*\Z")
LEGACY = re.compile(r"acct-[0-9a-f]{12}\Z")
BINDINGS = "routing_account_bindings"
ALIAS_SECONDS = 90 * 86400


def stable_id(harness: str, label: str) -> str:
    from khipu.routing import RouteError

    provider = harness.strip().lower()
    words = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", "-", words).strip("-")
    # Existing labels name their provider ("Claude", "Claude second").
    if words == provider:
        words = "main"
    elif words.startswith(provider + "-"):
        words = words[len(provider) + 1:]
    ident = f"{provider}:{words}"
    if not STABLE.fullmatch(ident):
        raise RouteError("an account label must contain letters or numbers for its stable id")
    return ident


def validate(accounts: list[dict]) -> None:
    from khipu.routing import RouteError

    seen = set()
    for account in accounts:
        ident = stable_id(account["harness"], account["label"])
        if ident in seen:
            raise RouteError(f"duplicate account label for {ident}; use different labels for this provider")
        seen.add(ident)
    if any(a["label"].casefold() in seen for a in accounts):
        raise RouteError("an account label cannot be another account's stable id")


def forwarded(key: str, forwards: dict[str, str]) -> str:
    seen = set()
    while key in forwards and key not in seen:
        seen.add(key)
        key = forwards[key]
    return key


def declared_id(account: dict) -> str | None:
    """Legacy folder-derived labels are not a source of public identity.

    A supplied label may name an id, but if its slug overlaps the login
    basename we cannot distinguish it from discover.default_name's output.
    Such labels use a provider-scoped opaque allocation instead.
    """
    ident = stable_id(account["harness"], account["label"])
    home = account.get("home") or ""
    folder = unicodedata.normalize("NFKD", PurePath(home).name).encode("ascii", "ignore").decode().lower()
    folder = re.sub(r"[^a-z0-9]+", "-", folder).strip("-")
    provider, slug = ident.split(":", 1)
    suffix = folder.removeprefix(provider + "-")
    if home and (slug == folder or slug.startswith(folder + "-") or
                 (suffix and (slug == suffix or slug.startswith(suffix + "-")))):
        return None
    return ident


def allocate(account: dict, used: set[str]) -> str:
    declared = declared_id(account)
    if declared is not None and declared not in used:
        return declared
    provider = account["harness"]
    n = 1
    while f"{provider}:account-{n}" in used:
        n += 1
    return f"{provider}:account-{n}"


def unique_ids(accounts: list[dict]) -> dict[str, str]:
    """Every login gets an id, including legacy colliding label slugs."""
    accounts = list({a["key"]: a for a in accounts}.values())
    declared = [declared_id(a) for a in accounts]
    counts = Counter(declared)
    used = {ident for ident in declared if ident is not None}
    out = {}
    for account, ident in zip(accounts, declared):
        if ident is None or counts[ident] != 1:
            ident = allocate(dict(account, label=account["harness"]), used | {account["harness"] + ":main"})
        out[account["key"]] = ident
        used.add(ident)
    return out


def state(events: list[dict], known: list[dict]) -> tuple[dict, dict, dict]:
    """Permanent login bindings; only an owner move transfers history.

    Labels bootstrap a fresh Mac. Once a local account-list transaction
    records a binding, reusing its label cannot rebind that identity.
    A restored migration without local list changes can bind the same
    label on another Mac, as decision 9 requires.
    """
    keys, forwards, aliases = {}, {}, {}
    migrated, pinned, stopped = set(), set(), set()
    for event in events:
        kind = event.get("type")
        author = (event.get("written_by") or {}).get("kind")
        if kind == "account_migration" and author in ("owner", "migration"):
            for key, ident in (event.get("bindings") or {}).items():
                keys.setdefault(key, ident)
                migrated.add(ident)
            for alias, ident in (event.get("aliases") or {}).items():
                aliases.setdefault(alias, (ident, event.get("started_at") or event.get("written_at")))
        elif kind == "account_binding" and (author == "owner" or
                (author == "conversation" and event.get("txn") and event.get("owner_request"))):
            key, ident = event.get("account"), event.get("to")
            if isinstance(key, str) and "|" in key and isinstance(ident, str) and STABLE.fullmatch(ident):
                if event.get("state") == "tracked":
                    keys[key] = ident
                else:
                    keys.setdefault(key, ident)
                pinned.add(keys[key])
                alias = event.get("target")
                if isinstance(alias, str) and STABLE.fullmatch(alias) and alias != ident:
                    forwards.setdefault(alias, ident)
        elif kind == "account_note" and event.get("state") == "untracked":
            stopped.add(event.get("account"))
        elif kind == "account_move" and author == "owner":
            old, new = event.get("account"), event.get("target")
            if old in keys and isinstance(new, str):
                keys[new] = keys[old]
        elif kind == "account_rename" and author == "owner":
            old, new = event.get("account"), event.get("to")
            if not isinstance(old, str) or not isinstance(new, str) or not STABLE.fullmatch(old) \
                    or not STABLE.fullmatch(new) or old.split(":")[0] != new.split(":")[0]:
                continue
            current = forwarded(old, forwards)
            if current == new or new in forwards:
                continue
            forwards[current] = new
            forwards[old] = new
    declared = unique_ids(known)
    used = set(keys.values()) | set(forwards) | set(forwards.values())
    for account in known:
        key = account["key"]
        if key in keys:
            continue
        ident = declared[key]
        # Restoring the original migration on a different Mac is the one
        # label-based bootstrap; local list changes permanently reserve ids.
        restore = ident in migrated and ident not in pinned and not any(
            k in stopped for k, value in keys.items() if value == ident)
        if ident in used and not restore and ident not in forwards:
            ident = allocate(account, used)
        keys[key] = ident
        used.add(ident)
    label_counts = Counter(stable_id(a["harness"], a["label"]) for a in known)
    for account in known:
        legacy = stable_id(account["harness"], account["label"])
        if declared_id(account) is None and label_counts[legacy] == 1:
            key = account["key"]
            if keys[key] == legacy:
                keys[key] = allocate(account, used)
                used.add(keys[key])
            forwards.setdefault(legacy, keys[key])
    return ({key: forwarded(ident, forwards) for key, ident in keys.items()},
            {key: forwarded(value, forwards) for key, value in forwards.items()}, aliases)


def local_bindings(accounts: list[dict], ids: dict | None = None) -> dict:
    ids = unique_ids(accounts) if ids is None else ids
    return {ids[a["key"]]: {"harness": a["harness"], "home": a.get("home")}
            for a in accounts if a["key"] in ids}
