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
words to agents, so callers pass them as ``keep`` (``keep_owner_words``) and
they come through untouched wherever they appear. A rule a conversation
proposed and the owner has not confirmed is not his words yet, and every hold
(the owner's too: its line is clipped, so it can't be matched whole) is
scrubbed like anything else.
"""
from __future__ import annotations

import contextlib
import getpass
import os
import re
import threading
from pathlib import Path
from typing import Iterable

HOME_WORD = "the home folder"
USER_WORD = "the user"
FOLDER_WORD = "a folder"
LOGIN_WORD = "a login folder"
# Names too common to replace as words: a folder called ".claude" is the
# default login, and "claude" is also the CLI's name in every line.
_PLAIN_NAMES = {"claude", "codex", "grok", "muse", "cursor", "config", "home", "users", "logins", "root",
                "user", "private", "tmp", "var", "volumes"}
# A name is a whole word between letters and digits: "_" and "-" separate,
# since a CLI's reply turns a name into "weekly_<name>".
_NAME_EDGE = r"A-Za-z0-9"
_PATHLIKE = re.compile(r"~/[^\s\"'`,;)\]}]*"
                       r"|(?<![\w.])/(?i:Users|home|private|tmp|var|Volumes|root)(?:/[^\s\"'`,;)\]}]*)?")
# Each harness's login folder when none is configured: its key is "harness|".
_DEFAULT_FOLDERS = {"claude": ".claude", "codex": ".codex", "grok": ".grok"}
_OWNER_WORDS: set[str] = set()
_OWNER_LOCK = threading.Lock()


def keep_owner_words(view) -> list[str]:
    """The owner's note and each standing rule he confirmed, from a routing
    view, as text to pass through untouched. Remembered for this process, so
    a later scrub of the same words (the MCP boundary after routing's own)
    keeps them too."""
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
    with _OWNER_LOCK:
        if len(_OWNER_WORDS) > 500:
            _OWNER_WORDS.clear()
        _OWNER_WORDS.update(words)
    return words


def _home_dir() -> str | None:
    try:
        home = os.path.expanduser("~")
    except Exception:  # noqa: BLE001
        return None
    return home.rstrip("/") if home and home not in ("~", "/") else None


def _homes(extra_keys: Iterable[str] | None) -> list[tuple[str, str | None]]:
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
    if extra_keys is None:
        with contextlib.suppress(Exception):
            from khipu import routing

            view = routing._load_view() or {}
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


def _forms(raw: str) -> set[str]:
    """``raw`` as typed, expanded and resolved, and in its ~/ form."""
    raw = raw.strip()
    forms = {raw.rstrip("/") or raw}
    with contextlib.suppress(Exception):
        expanded = os.path.expanduser(raw).rstrip("/")
        forms.add(expanded)
        forms.add(os.path.realpath(expanded).rstrip("/"))
    home = _home_dir()
    if home:
        for form in list(forms):
            if form.startswith(home + "/"):
                forms.add("~" + form[len(home):])
        with contextlib.suppress(Exception):
            real_home = os.path.realpath(home)
            for form in list(forms):
                if form.startswith(real_home + "/"):
                    forms.add("~" + form[len(real_home):])
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
    return {n for n in names if n and len(n) >= 3 and n.lower() not in _PLAIN_NAMES}


def _word(name: str) -> re.Pattern:
    """``name`` as a whole word, any case, its parts joined by "-", "_", "."
    or nothing ("probe-login", "probe_login", "probelogin"), a leading dot
    included."""
    parts = [re.escape(p) for p in re.split(rf"[^{_NAME_EDGE}]+", name) if p]
    body = r"[-_.]?".join(parts) or re.escape(name)
    return re.compile(rf"(?<![{_NAME_EDGE}.])\.?{body}(?![{_NAME_EDGE}])", re.I)


def _rules(extra_keys: Iterable[str] | None = None):
    """(literal replacements longest first, word patterns) for one scrub."""
    literal: dict[str, object] = {}  # each value a str or an _Id
    folder_words: dict[str, object] = {}
    paths: list[str] = []
    home = _home_dir()
    for harness, raw in _homes(extra_keys):
        ident = _Id(harness, raw)
        if raw is None:
            folder = _DEFAULT_FOLDERS.get(harness)
            if not folder or not home:
                continue
            raw = os.path.join(home, folder)
        else:
            name = Path(raw.rstrip("/")).name.lstrip(".")
            if len(name) >= 4 and name.lower() not in _PLAIN_NAMES:
                folder_words.setdefault(name, ident)
        for form in _forms(raw):
            literal.setdefault(form, ident)
            paths.append(form)
    with contextlib.suppress(Exception):
        from khipu import routing

        for path in routing._configured_paths():
            literal.setdefault(path, FOLDER_WORD)
            paths.append(path)
    if home:
        literal.setdefault(home, HOME_WORD)
        paths.append(home)
        with contextlib.suppress(Exception):
            literal.setdefault(os.path.realpath(home).rstrip("/"), HOME_WORD)
    words = [(_word(n), r) for n, r in sorted(folder_words.items(), key=lambda kv: -len(kv[0]))]
    words += [(_word(n), USER_WORD) for n in sorted(_user_names(paths), key=len, reverse=True)]
    return sorted(literal.items(), key=lambda kv: -len(kv[0])), words


def _scrub_text(text: str, rules, keep: list[str]) -> str:
    literal, words = rules
    for i, word in enumerate(keep):
        if word and word in text:
            text = text.replace(word, f"\x00{i}\x00")
    for path, replacement in literal:
        if path in text:
            text = text.replace(path, str(replacement))
    text = _PATHLIKE.sub(FOLDER_WORD, text)
    for pattern, replacement in words:
        text = pattern.sub(lambda _m, r=replacement: str(r), text)
    for i, word in enumerate(keep):
        text = text.replace(f"\x00{i}\x00", word)
    return text


def _walk(value, rules, keep):
    if isinstance(value, str):
        return _scrub_text(value, rules, keep)
    if isinstance(value, dict):
        return {(_scrub_text(k, rules, keep) if isinstance(k, str) else k): _walk(v, rules, keep)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, rules, keep) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk(v, rules, keep) for v in value)
    return value


def scrub(value, *, keep: Iterable[str] = (), extra_keys: Iterable[str] | None = None):
    """``value`` (a string, or dicts and lists of them, keys included) with
    every login folder, home folder, user name and private path taken out.
    ``keep``: the owner's own words, passed through as written."""
    with _OWNER_LOCK:
        owner = set(_OWNER_WORDS)
    words = sorted({w for w in [*keep, *owner] if isinstance(w, str) and w}, key=len, reverse=True)
    return _walk(value, _rules(extra_keys), words)


def error_text(exc: BaseException) -> str:
    """An exception as an agent may read it: a filesystem error in fixed
    words (its own text names the file), anything else scrubbed."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: Khipu could not read or write one of its files"
    if isinstance(exc, SystemExit):
        return f"SystemExit: capture rejected the payload (exit {scrub(str(exc.code))})"
    return f"{type(exc).__name__}: {scrub(str(exc))}"
