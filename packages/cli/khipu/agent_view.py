"""The one scrub every agent-facing output passes through (settings-clarity
plan, slice 9A): khipu_usage, khipu_route, khipu_route_report, khipu_status
(its hub_error included), khipu route --summary, the "Routing at" pushes at
session start and on each prompt, and every MCP error message, over stdio and
through the gateway alike.

What it takes out, wherever it appears in any string: each login folder Khipu
is configured with, discovers or holds in its routing record (as typed,
expanded, resolved, in its ~/ form and by its folder name), replaced by the
account's id as khipu_route gives it; the other configured folders and files;
this Mac's home folder; the user names those paths carry; and anything else
shaped like a private path. The app and the owner's own `khipu usage --json`
never pass through here and keep their paths. Memory content (khipu_search,
khipu_get, khipu_status's prior work) never passes through here either: a
scrub would alter what was remembered.

One deliberate exception: the owner's note and his standing (confirmed, not
pending) rules reach agents as he wrote them, paths included. They are his own
words to agents, so they pass through untouched: those in the record now
(``keep_owner_words``, rebuilt whenever the record changes), never one he has
ended. A rule a conversation proposed and the owner has not confirmed is not
his words yet, and every hold (the owner's too: its line is clipped, so it
can't be matched whole) is scrubbed like anything else.
"""
from __future__ import annotations

import contextlib
import getpass
import hashlib
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Iterable

HOME_WORD = "the home folder"
USER_WORD = "the user"
FOLDER_WORD = "a folder"
LOGIN_WORD = "a login folder"
WITHHELD = "[redacted]"
# A private-looking path Khipu doesn't know: a "~/" path, or one under a
# folder that holds people's or temp files. It ends at the first space.
_PATHLIKE = re.compile(r"~/[^\s\"'`,;)\]}]*"
                       r"|(?<![\w.])/(?i:Users|home|private|tmp|var|Volumes|root)(?:/[^\s\"'`,;)\]}]*)?")
_SEG = r"[^\s\"'`,;)\]}/]+"
# After a known root, the path that follows it: inner segments may hold
# spaces (a "/" follows them); the last ends at one.
_TAIL = rf"(?:/{_SEG}(?:\s{_SEG})*(?=/))*(?:/[^\s\"'`,;)\]}}]*)?"
# Each harness's login folder when none is configured: its key is "harness|".
_DEFAULT_FOLDERS = {"claude": ".claude", "codex": ".codex", "grok": ".grok"}
_LOCK = threading.Lock()


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def keep_owner_words(view) -> list[str]:
    """The owner's note and each standing rule he confirmed, from a routing
    view as it is now, as text to pass through untouched. Never remembered
    beyond the call: a note he ended is no longer his words to agents."""
    words: list[str] = []
    if not isinstance(view, dict):
        return words
    note = view.get("note")
    if isinstance(note, dict) and isinstance(note.get("text"), str) and note["text"].strip():
        words.append(note["text"])
    for rule in view.get("rules") or []:
        if isinstance(rule, dict) and not rule.get("pending") and isinstance(rule.get("text"), str) \
                and rule["text"].strip():
            words.append(rule["text"])
    return words


def _home_dir() -> str | None:
    try:
        home = os.path.expanduser("~")
    except Exception:  # noqa: BLE001
        return None
    return home.rstrip("/") if home and home not in ("~", "/") else None


def _saved_view() -> dict:
    with contextlib.suppress(Exception):
        from khipu import routing

        return routing._load_view() or {}
    return {}


def _homes(extra_keys: Iterable[str] | None, view: dict) -> list[tuple[str, str | None]]:
    """Every (harness, login folder) Khipu knows: configured (invalid entries
    included), discovered Claude homes, the routing record's accounts, the
    caller's ``extra_keys`` ("harness|home") and each default login."""
    out: list[tuple[str, str | None]] = [(h, None) for h in _DEFAULT_FOLDERS]
    with contextlib.suppress(Exception):
        from khipu.config import load_config

        for entry in load_config().get("usage_accounts") or []:
            if isinstance(entry, dict) and isinstance(entry.get("home"), str) and entry["home"].strip():
                out.append((str(entry.get("harness") or "").strip().lower(), entry["home"].strip()))
    with contextlib.suppress(Exception):
        from khipu import claude_homes

        for found in claude_homes.discover():
            out.append(("claude", None if found.is_default else str(found.path)))
    keys = list(extra_keys or [])
    keys += [a.get("key") for a in view.get("accounts") or [] if isinstance(a, dict)]
    for key in keys:
        if isinstance(key, str) and "|" in key:
            harness, _, home = key.partition("|")
            out.append((harness, home or None))
    return out


class _Id:
    """An account's id, worked out only when a scrub replaces something
    with it: the id needs this install's salt, which is made on first use,
    and a scrub that finds nothing must write nothing."""

    __slots__ = ("harness", "home", "_value")

    def __init__(self, harness: str, home: str | None):
        self.harness, self.home, self._value = harness, home, None

    def __str__(self) -> str:
        if self._value is None:
            try:
                from khipu import routing, usage

                self._value = routing.account_id(usage._key({"harness": self.harness, "home": self.home}))
            except Exception:  # noqa: BLE001
                self._value = LOGIN_WORD
        return self._value


def _symlink_forms(path: str) -> set[str]:
    """Full login-path spellings after each symlink hop, including parent links."""
    path = Path(path)
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    pending = list(path.parts[1:] if path.is_absolute() else path.parts)
    forms: set[str] = set()
    hops = 0
    while pending and hops < 40:  # Bound cycles and pathological link chains.
        part = pending.pop(0)
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        try:
            target = Path(os.readlink(candidate))
        except OSError:
            current = candidate
            continue
        hops += 1
        forms.add(str(current / target / Path(*pending)))
        if target.is_absolute():
            current = Path(target.anchor)
            pending = list(target.parts[1:]) + pending
        else:
            pending = list(target.parts) + pending
    return forms


def _forms(raw: str, home: str | None) -> set[str]:
    """``raw`` as typed, expanded, each symlink hop and resolved, also ~/ and NFC."""
    raw = raw.strip()
    forms = {raw.rstrip("/") or raw}
    with contextlib.suppress(Exception):
        expanded = os.path.expanduser(raw).rstrip("/")
        forms |= {expanded, os.path.realpath(expanded).rstrip("/")}
        forms |= _symlink_forms(expanded)
    if home:
        for base in {home, os.path.realpath(home)}:
            forms |= {"~" + f[len(base):] for f in list(forms) if f.startswith(base + "/")}
    forms |= {unicodedata.normalize("NFC", f) for f in forms}
    return {f for f in forms if len(f) > 1 and f not in ("~", "/")}


def _user_names(paths: Iterable[str]) -> set[str]:
    names: set[str] = set()
    with contextlib.suppress(Exception):
        names.add(getpass.getuser())
    for path in paths:
        parts = Path(path).parts
        for i, part in enumerate(parts[:-1]):
            if part.lower() in ("users", "home"):
                names.add(parts[i + 1])
    with contextlib.suppress(Exception):
        home = Path.home()
        if home.parent.name.lower() in ("users", "home"):
            names.add(home.name)
    return {n for n in names if n.strip()}


def _name_regex(variants, token_names, *, whole_value=False):
    variants = sorted(set(variants), key=len, reverse=True)
    tokens = "|".join(re.escape(n) for n in variants if _fold(n) in token_names)
    plain = "|".join(re.escape(n) for n in variants if _fold(n) not in token_names)
    patterns = []
    if tokens:
        patterns.append(rf"(?<![^\W_])(?:{tokens})(?![^\W_])")
    if plain:
        # Lowercase ASCII words need path context so ordinary prose survives.
        patterns.append(rf"(?:(?<=/)(?:{plain})(?![^\W_])|(?<![\w./~])(?:{plain})(?=/))")
        if whole_value:
            patterns.append(rf"^(?:{plain})$")
    return re.compile("|".join(patterns), re.I) if patterns else None


class _Table:
    """One scrub's replacements, rebuilt whenever a source changes:
    ``roots`` every known path (login folders, the home folder, volumes, the
    memory root, configured folders) taken whole with the path after it;
    ``names`` distinctive tokens and lowercase words in path components; and the
    owner's words as the saved record holds them now."""

    __slots__ = ("roots", "root_to", "names", "name_to", "owner", "secrets", "name_secrets")

    def __init__(self, roots, root_to, names, name_to, owner, token_names):
        self.roots, self.root_to, self.names, self.name_to, self.owner = roots, root_to, names, name_to, owner
        self.secrets = tuple({r for r, _ in root_to if r != "~"}
                             | {n for n in name_to if not n.isalnum()})
        # A single-name value can itself be a relative path. In prose a
        # plain name is private only as a whole path component.
        self.name_secrets = _name_regex(name_to, token_names, whole_value=True)


def _other_paths() -> list[str]:
    others: list[str] = []
    with contextlib.suppress(Exception):
        from khipu import routing

        others += routing._configured_paths()
    with contextlib.suppress(Exception):
        others += [os.path.join("/Volumes", n) for n in os.listdir("/Volumes")]
    with contextlib.suppress(Exception):
        from khipu.config import path_setting

        memory = path_setting("memory_root")
        if memory:
            others.append(str(memory).rstrip("/"))
    return others


def _rules(extra_keys: Iterable[str] | None = None) -> _Table:
    view = _saved_view()
    home = _home_dir()
    root_to: dict[str, object] = {}  # folded path -> an _Id or a word
    name_to: dict[str, object] = {}  # folded name -> an _Id or USER_WORD
    paths: list[str] = []
    spelled: set[str] = set()  # names as written, beside their folded keys
    token_names: set[str] = set()

    def add_name(form, to, plain):
        name_to.setdefault(_fold(form), to)
        spelled.add(form)
        if not plain:
            token_names.add(_fold(form))

    for harness, raw in _homes(extra_keys, view):
        ident = _Id(harness, raw)
        default = raw is None
        if raw is None:
            folder = _DEFAULT_FOLDERS.get(harness)
            if not folder or not home:
                continue
            raw = os.path.join(home, folder)
        for path in sorted(_forms(raw, home)):
            root_to.setdefault(_fold(path), ident)
            paths.append(path)
            folder = unicodedata.normalize("NFC", Path(path.rstrip("/")).name).strip()
            # Default harness names are public vocabulary; only their dotted
            # folder spelling is private. Symlink targets still need aliases.
            if default and folder == _DEFAULT_FOLDERS.get(harness):
                add_name(folder, ident, plain=False)
                continue
            name = folder.lstrip(".")
            # The name as written, and as Khipu itself writes it into a
            # window's name ("weekly_<slug>", usage.parse_claude).
            for form in (folder, name, re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")):
                if form:
                    # Aliases retain the original name's classification:
                    # stripping a dot or casefolding must not make it prose.
                    add_name(form, ident, plain=re.fullmatch(r"[a-z]+", folder) is not None)
    for path in _other_paths():
        if len(path.rstrip("/")) > 1:
            root_to.setdefault(_fold(path.rstrip("/")), FOLDER_WORD)
            paths.append(path)
    if home:
        for form in {home, os.path.realpath(home)}:
            root_to.setdefault(_fold(form), HOME_WORD)
        root_to.setdefault("~", HOME_WORD)
        paths.append(home)
    for user in _user_names(paths):
        add_name(unicodedata.normalize("NFC", user), USER_WORD,
                 plain=re.fullmatch(r"[a-z]+", user) is not None)
    order = sorted(root_to, key=len, reverse=True)
    alts = "|".join(re.escape(r) for r in order)
    roots = re.compile(rf"(?:{alts})(?![^\s/\"'`,;)\]}}]){_TAIL}", re.I) if alts else None
    names = _name_regex(set(name_to) | spelled, token_names)
    return _Table(roots, [(r, root_to[r]) for r in order], names, name_to,
                  keep_owner_words(view), token_names)


_TABLE_TTL_S = 60.0
_TABLE: dict = {"sig": None, "at": 0.0, "table": None}


def _signature(extra_keys) -> tuple:
    """File stamps and effective values of every replacement-table source.

    Discovery and path expansion must run even on cache hits: environment
    overrides, symlink targets and newly discovered logins need not change
    the config or routing record.
    """
    def stat(path):
        try:
            st = path.stat()
            return (str(path), st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return (str(path), None)

    from khipu import routing, t3
    from khipu.config import config_file

    view = _saved_view()
    home = _home_dir()
    homes = _homes(extra_keys, view)
    paths = _other_paths()
    for harness, raw in homes:
        if raw is None:
            folder = _DEFAULT_FOLDERS.get(harness)
            if not folder or not home:
                continue
            raw = os.path.join(home, folder)
        paths.extend(_forms(raw, home))
    if home:
        paths.append(home)
    forms = tuple(sorted((path, tuple(sorted(_forms(path, home)))) for path in paths))
    keys = tuple(sorted(str(k) for k in extra_keys)) if extra_keys is not None else None
    discovered = hashlib.sha256(repr((homes, forms, sorted(_user_names(paths)))).encode("utf-8")).digest()
    return (stat(config_file()), stat(routing.view_file()), stat(t3.settings_path()),
            view.get("epoch"), view.get("version"), tuple(keep_owner_words(view)),
            discovered, keys, home)


def _table(extra_keys) -> _Table:
    """The cached table, rebuilt on any source change or TTL expiry."""
    extra_keys = tuple(extra_keys) if extra_keys is not None else None
    try:
        sig = _signature(extra_keys)
    except Exception:  # noqa: BLE001
        return _rules(extra_keys)
    now = time.monotonic()
    with _LOCK:
        if _TABLE["sig"] == sig and now - _TABLE["at"] < _TABLE_TTL_S:
            return _TABLE["table"]
    table = _rules(extra_keys)
    with _LOCK:
        _TABLE.update(sig=sig, at=now, table=table)
    return table


def _lookup(mapping: dict, found: str, fallback: str) -> str:
    return str(mapping.get(_fold(found), fallback))


def _scrub_text(text: str, table: _Table, keep: list[str]) -> str:
    if not text.isascii():
        text = unicodedata.normalize("NFC", text)
    pathy = "/" in text or "~" in text
    held = [w for w in keep if w and w in text]
    for i, word in enumerate(held):
        text = text.replace(word, f"\x00{i}\x00")
    if pathy:
        if table.roots is not None:
            def root(m: re.Match) -> str:
                found = _fold(m.group(0))
                return next((str(to) for r, to in table.root_to if found.startswith(r)), FOLDER_WORD)
            text = table.roots.sub(root, text)
        text = _PATHLIKE.sub(FOLDER_WORD, text)
    # A later name replacement must not hide a root the path regex missed.
    folded = _fold(text)
    unmatched_root = pathy and any(r in folded for r, _ in table.root_to if r != "~")
    if table.names is not None:
        text = table.names.sub(lambda m: _lookup(table.name_to, m.group(0), LOGIN_WORD), text)
    # Regex case-insensitivity cannot expand Unicode casefolds (ß -> ss).
    # Check the finished value even when it contained no apparent path or
    # token. Owner words remain masked until this fail-closed check is done.
    folded = _fold(text)
    if unmatched_root or any(secret in folded for secret in table.secrets) \
            or (table.name_secrets is not None and table.name_secrets.search(folded)):
        text = WITHHELD + "".join(f" \x00{i}\x00" for i in range(len(held)))
    for i, word in enumerate(held):
        text = text.replace(f"\x00{i}\x00", word)
    return text


def _walk(value, table, keep):
    if isinstance(value, str):
        return _scrub_text(value, table, keep)
    if isinstance(value, dict):
        return {(_scrub_text(k, table, keep) if isinstance(k, str) else k): _walk(v, table, keep)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, table, keep) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk(v, table, keep) for v in value)
    return value


def scrub(value, *, keep: Iterable[str] = (), extra_keys: Iterable[str] | None = None):
    """``value`` (a string, or dicts and lists of them, keys included) with
    every login folder, home folder, user name and private path taken out.
    ``keep``: the owner's own words, passed through as written, besides those
    the saved record holds now."""
    table = _table(extra_keys)
    words = sorted({w for w in [*keep, *table.owner] if isinstance(w, str) and w}, key=len, reverse=True)
    return _walk(value, table, words)


def error_text(exc: BaseException) -> str:
    """An exception as an agent may read it: a filesystem error in fixed
    words (its own text names the file), anything else scrubbed."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: Khipu could not read or write one of its files"
    if isinstance(exc, SystemExit):
        return f"SystemExit: capture rejected the payload (exit {scrub(str(exc.code))})"
    return f"{type(exc).__name__}: {scrub(str(exc))}"
