"""Pure account identity rules. Login keys belong to this Mac alone.

The usage cache and local history retain their login keys; public ids and
rename forwards never contain a login folder. Migration aliases expire,
but the historical binding and rename maps do not.
"""
from __future__ import annotations

import re
import unicodedata


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


def state(events: list[dict], known: list[dict]) -> tuple[dict, dict, dict]:
    """Local key -> stable id, stable forwards, alias -> (id, start).

    Event order decides crossing renames. Forward the losing destination
    too, so a writer still naming the original id reaches the winner.
    The first migration witness owns an alias's start, even after restore.
    """
    keys = {a["key"]: stable_id(a["harness"], a["label"]) for a in known}
    forwards, aliases = {}, {}
    for event in events:
        kind = event.get("type")
        author = (event.get("written_by") or {}).get("kind")
        if kind == "account_migration" and author in ("owner", "migration"):
            for key, ident in (event.get("bindings") or {}).items():
                keys.setdefault(key, ident)
            for alias, ident in (event.get("aliases") or {}).items():
                aliases.setdefault(alias, (ident, event.get("started_at") or event.get("written_at")))
        elif kind == "account_rename" and author == "owner":
            old, new = event.get("account"), event.get("to")
            if not isinstance(old, str) or not isinstance(new, str) or not STABLE.fullmatch(old) \
                    or not STABLE.fullmatch(new) or old.split(":")[0] != new.split(":")[0]:
                continue
            current = forwarded(old, forwards)
            if current == new or new in forwards:
                continue  # no cycles or reuse of an earlier identity
            forwards[current] = new
            forwards[old] = new
    return ({key: forwarded(ident, forwards) for key, ident in keys.items()},
            {key: forwarded(value, forwards) for key, value in forwards.items()}, aliases)


def local_bindings(accounts: list[dict]) -> dict:
    return {stable_id(a["harness"], a["label"]): {"harness": a["harness"], "home": a.get("home")}
            for a in accounts}
