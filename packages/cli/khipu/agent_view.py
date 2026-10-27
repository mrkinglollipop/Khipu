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
never pass through here and keep their paths. The ledger's and the Work
view's agent-facing and JSON outputs leave through ``project``: this scrub,
account keys first, and a stricter final scan. Memory content (khipu_search,
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
import copy
import getpass
import hashlib
import html
import os
import re
import signal
import stat
import threading
import time
import unicodedata
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, unquote_plus

HOME_WORD = "the home folder"
USER_WORD = "the user"
FOLDER_WORD = "a folder"
LOGIN_WORD = "a login folder"
WITHHELD = "[redacted]"
# A private-looking path Khipu doesn't know: a "~/" path, or one under a
# folder that holds people's or temp files. It ends at the first space.
_PATHLIKE = re.compile(r"~/[^\s\"'`,;)\]}]*"
                       r"|(?<![\w.])/(?i:Users|home|private|tmp|var|Volumes|root)(?:/[^\s\"'`,;)\]}]*)?"
                       # Windows: a drive path (C:\Users\...) or a UNC share (\\host\share).
                       r"|(?<![\w.])[A-Za-z]:[\\/][^\s\"'`,;)\]}]*"
                       r"|\\\\[^\s\"'`,;)\]}\\]+\\[^\s\"'`,;)\]}]*")
_SEG = r"[^\s\"'`,;)\]}/]+"
# After a known root, the path that follows it: inner segments may hold
# spaces (a "/" follows them); the last ends at one.
_TAIL = rf"(?:/{_SEG}(?:\s{_SEG})*(?=/))*(?:/[^\s\"'`,;)\]}}]*)?"
# Each harness's login folder when none is configured: its key is "harness|".
_DEFAULT_FOLDERS = {"claude": ".claude", "codex": ".codex", "grok": ".grok"}
_LOCK = threading.Lock()
_FORM_CACHE = threading.local()
_CHAR_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")
_NUMERIC_ENTITY = re.compile(r"&#(?:[xX][0-9a-fA-F]+|[0-9]+);?")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _decoded_variants(text: str):
    """Bound nested decoding while retaining literal plus signs as well as spaces."""
    yield text
    if not any(marker in text for marker in ("%", "\\", "&#")):
        return
    seen = {text}
    pending = {text}
    for _ in range(5):
        following = set()
        for value in pending:
            decoded = {value}
            if "%" in value or "+" in value:
                decoded.update((unquote(value), unquote_plus(value)))
            for candidate in tuple(decoded):
                if "\\" in candidate:
                    candidate = _CHAR_ESCAPE.sub(
                        lambda m: chr(int(m.group(1) or m.group(2), 16)), candidate)
                    decoded.add(candidate)
                if "&#" in candidate:
                    decoded.add(_NUMERIC_ENTITY.sub(lambda m: html.unescape(m.group()), candidate))
            following.update(decoded - seen)
        if not following:
            return
        yield from following
        seen.update(following)
        pending = following


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


def _homes(extra_keys: Iterable[str] | None, view: dict, *, local_only=False, require_complete=False) -> list[tuple[str, str | None]]:
    """Every (harness, login folder) Khipu knows: configured (invalid entries
    included), discovered Claude homes, the routing record's accounts, the
    caller's ``extra_keys`` ("harness|home"), each default login and each
    one set for this process through a harness's own variable
    (``CODEX_HOME``, ``GROK_HOME``, ``CLAUDE_CONFIG_DIR``)."""
    out: list[tuple[str, str | None]] = [(h, None) for h in _DEFAULT_FOLDERS]
    with (contextlib.nullcontext() if require_complete else contextlib.suppress(Exception)):
        from khipu.usage import _HOME_VARS

        for harness, var in _HOME_VARS.items():
            value = (os.environ.get(var) or "").strip()
            if value:
                out.append((harness, value))
    with (contextlib.nullcontext() if require_complete else contextlib.suppress(Exception)):
        from khipu.config import load_config

        for entry in load_config().get("usage_accounts") or []:
            if isinstance(entry, dict) and isinstance(entry.get("home"), str) and entry["home"].strip():
                out.append((str(entry.get("harness") or "").strip().lower(), entry["home"].strip()))
    with (contextlib.nullcontext() if require_complete else contextlib.suppress(Exception)):
        from khipu import claude_homes

        for found in (() if local_only else claude_homes.discover()):
            out.append(("claude", None if found.is_default else str(found.path)))
    with (contextlib.nullcontext() if require_complete else contextlib.suppress(Exception)):
        # Account discovery's finds (settings clarity phase 9): its last
        # stored run, which carries every source, and the login-shaped names
        # in the home folder now, so a folder is scrubbed before any run.
        from khipu import discover

        store = discover._load_store_strict() if require_complete else discover._load_store()
        for rec in store["candidates"].values():
            if rec["cli"] in _DEFAULT_FOLDERS and rec.get("abs") and not rec.get("default"):
                out.append((rec["cli"], rec["abs"]))
        for cli, folder in (() if local_only else discover._source_names()):
            # A default login stays home-less (its name is the CLI's own
            # word, which agents must still read): it is already in ``out``.
            if folder.name != _DEFAULT_FOLDERS.get(cli):
                out.append((cli, str(folder)))
    keys = list(extra_keys or [])
    keys += [a.get("key") for a in view.get("accounts") or [] if isinstance(a, dict)]
    for key in keys:
        if isinstance(key, str) and "|" in key:
            harness, _, home = key.partition("|")
            out.append((harness, home or None))
    return out


def _config_whole() -> None:
    """Raise when the config file is there but can't be read or parsed (or
    isn't an object): the login folders it names are then unknown, so the
    table ``_homes`` builds is incomplete and nothing may lean on it."""
    from khipu.config import check_config_readable

    check_config_readable()


def strict_table():
    """``_table(None)``, raising instead when ``_config_whole`` does."""
    _config_whole()
    return _table(None)


def _login_forms():
    """Each spelling (``_forms``, without a trailing slash or ``~``) of each
    login folder ``_homes`` knows. Raises while the config is unreadable."""
    _config_whole()
    home = _home_dir()
    for harness, raw in _homes(None, _saved_view()):
        if raw is None:
            folder = _DEFAULT_FOLDERS.get(harness)
            if not folder or not home:
                continue
            raw = os.path.join(home, folder)
        for form in _forms(raw, home):
            form = form.rstrip("/")
            if not form.startswith("~"):
                yield form


def in_login_folder(path: str) -> bool:
    """Whether ``path`` is in one of the login folders ``_homes`` knows (in
    any of its spellings), decided from names and links alone: nothing
    inside one is listed or opened."""
    target = _fold(path.rstrip("/"))
    return any(target == form or target.startswith(form + "/") for form in map(_fold, _login_forms()))


def login_folder_ids() -> frozenset[tuple[int, int]]:
    """The (device, inode) of each login folder ``_homes`` knows that is
    there, so a folder is known as one whatever name reached it (macOS's
    ``/System/Volumes/Data/...`` is the same folder as its plain path). Only
    each folder itself is looked at, never anything inside it."""
    ids = set()
    for form in set(_login_forms()):
        with contextlib.suppress(OSError, ValueError):
            st = os.stat(form)
            if stat.S_ISDIR(st.st_mode):
                ids.add((st.st_dev, st.st_ino))
    return frozenset(ids)


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


def _forms(raw: str, home: str | None, *, local_only=False) -> set[str]:
    """``raw`` as typed, expanded, each symlink hop and resolved, also ~/ and NFC."""
    cache = getattr(_FORM_CACHE, "current", None)
    key = (raw, home, local_only)
    if cache is not None and key in cache:
        return set(cache[key])
    raw = raw.strip()
    forms = {raw.rstrip("/") or raw}
    with contextlib.suppress(Exception):
        expanded = os.path.expanduser(raw).rstrip("/")
        forms.add(expanded)
        if not local_only:
            forms.add(os.path.realpath(expanded).rstrip("/"))
            forms |= _symlink_forms(expanded)
    if home:
        for base in ({home} if local_only else {home, os.path.realpath(home)}):
            forms |= {"~" + f[len(base):] for f in list(forms) if f.startswith(base + "/")}
    forms |= {unicodedata.normalize("NFC", f) for f in forms}
    result = {f for f in forms if len(f) > 1 and f not in ("~", "/")}
    if cache is not None:
        cache[key] = result
    return set(result)


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

    __slots__ = ("roots", "root_to", "names", "name_to", "owner", "secrets", "name_secrets", "previous")

    def __init__(self, roots, root_to, names, name_to, owner, token_names):
        self.previous = None
        self.roots, self.root_to, self.names, self.name_to, self.owner = roots, root_to, names, name_to, owner
        self.secrets = tuple({r for r, _ in root_to if r != "~"}
                             | {n for n in name_to if not n.isalnum()})
        # A single-name value can itself be a relative path. In prose a
        # plain name is private only as a whole path component.
        self.name_secrets = _name_regex(name_to, token_names, whole_value=True)


def _other_paths(*, local_only=False) -> list[str]:
    others: list[str] = []
    with contextlib.suppress(Exception):
        from khipu import routing

        if local_only:
            from khipu.config import load_config

            cfg = load_config()
            others += [cfg[k] for k in ("routing_table", "routing_rules_seed") if isinstance(cfg.get(k), str)]
            others += [item["path"] for item in cfg.get("routing_sources") or []
                       if isinstance(item, dict) and isinstance(item.get("path"), str)]
        else:
            others += routing._configured_paths()
    with contextlib.suppress(Exception):
        if not local_only:
            others += [os.path.join("/Volumes", n) for n in os.listdir("/Volumes")]
    with contextlib.suppress(Exception):
        from khipu.config import path_setting

        memory = path_setting("memory_root")
        if memory:
            others.append(str(memory).rstrip("/"))
    return others


def _rules(extra_keys: Iterable[str] | None = None, *, local_only=False, require_complete=False) -> _Table:
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

    for harness, raw in _homes(extra_keys, view, local_only=local_only, require_complete=require_complete):
        ident = _Id(harness, raw)
        default = raw is None
        if raw is None:
            folder = _DEFAULT_FOLDERS.get(harness)
            if not folder or not home:
                continue
            raw = os.path.join(home, folder)
        for path in sorted(_forms(raw, home, local_only=local_only)):
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
    for path in _other_paths(local_only=local_only):
        if len(path.rstrip("/")) > 1:
            root_to.setdefault(_fold(path.rstrip("/")), FOLDER_WORD)
            paths.append(path)
    if home:
        for form in ({home} if local_only else {home, os.path.realpath(home)}):
            root_to.setdefault(_fold(form), HOME_WORD)
        root_to.setdefault("~", HOME_WORD)
        paths.append(home)
    for user in _user_names(paths):
        add_name(unicodedata.normalize("NFC", user), USER_WORD,
                 plain=re.fullmatch(r"[a-z]+", user) is not None)
    order = sorted(root_to, key=len, reverse=True)
    alts = "|".join(r"(?<!~)~" if r == "~" else re.escape(r) for r in order)
    roots = re.compile(rf"(?:{alts})(?![^\s/\"'`,;)\]}}]){_TAIL}", re.I) if alts else None
    names = _name_regex(set(name_to) | spelled, token_names)
    return _Table(roots, [(r, root_to[r]) for r in order], names, name_to,
                  keep_owner_words(view), token_names)


_TABLE_TTL_S = 60.0
# Two one-second helper ticks; incomplete prompt probes never renew this age.
_PROMPT_TABLE_MAX_AGE_S = 2.0
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
    homes = sorted(set(_homes(extra_keys, view)), key=repr)
    paths = _other_paths()
    watched = set()
    for harness, raw in homes:
        if raw is None:
            folder = _DEFAULT_FOLDERS.get(harness)
            if not folder or not home:
                continue
            raw = os.path.join(home, folder)
        login_forms = _forms(raw, home)
        paths.extend(login_forms)
        watched.update(os.path.expanduser(p) for p in login_forms)
    if home:
        paths.append(home)
    forms = tuple(sorted((path, tuple(sorted(_forms(path, home)))) for path in set(paths)))
    keys = tuple(sorted(str(k) for k in extra_keys)) if extra_keys is not None else None
    discovered = hashlib.sha256(repr((homes, forms, sorted(_user_names(paths)))).encode("utf-8")).digest()
    return (stat(config_file()), stat(routing.view_file()), stat(t3.settings_path()),
            view.get("epoch"), view.get("version"), tuple(keep_owner_words(view)),
            discovered, keys, home, tuple(sorted(watched)))


_PROJECTION = threading.local()


@contextlib.contextmanager
def routing_projection(extra_keys):
    """Bound discovery before projecting a hook's routing output."""
    keys = tuple(extra_keys)
    table = _prompt_table(keys)
    previous = getattr(_PROJECTION, "current", None)
    _PROJECTION.current = (keys, table)
    try:
        yield
    finally:
        _PROJECTION.current = previous


class _InventoryDeadline(BaseException):
    # Discovery helpers deliberately suppress ordinary filesystem errors.
    pass



class InventoryUnavailable(RuntimeError):
    """No complete alias inventory exists; the hook must withhold routing."""


def _inventory_signature(keys, watched):
    """Check inventory inputs without building alias forms or walking login directories.

    Login-shaped home entries detect newly discoverable names. Stamps
    along known login paths detect retargeted links, including parent links.
    Owner words are refreshed separately and do not authorize new aliases.
    """
    from khipu import discover, t3
    from khipu.config import config_file

    from khipu.usage import _HOME_VARS

    home = _home_dir()
    accounts = {a.get("key") for a in _saved_view().get("accounts") or [] if isinstance(a, dict)}
    accounts.update(keys or ())
    uncovered = []
    for key in accounts:
        if not isinstance(key, str) or "|" not in key:
            continue
        harness, _, raw = key.partition("|")
        if not raw:
            folder = _DEFAULT_FOLDERS.get(harness)
            if not folder or not home:
                continue
            raw = os.path.join(home, folder)
        # A read can supply configured keys before any routing view is saved.
        # Their aliases are already covered by the prepared inventory.
        if os.path.expanduser(raw).rstrip("/") not in watched:
            uncovered.append(key)
    accounts = tuple(sorted(uncovered))
    environment = tuple((var, os.environ.get(var)) for var in sorted(_HOME_VARS.values()))
    files = {config_file(), t3.settings_path(), discover.store_file()}
    paths = set(files)
    names = ()
    if home:
        paths.add(Path(home))
        try:
            with os.scandir(home) as entries:
                found = []
                for entry in entries:
                    if not discover.name_match(entry.name):
                        continue
                    try:
                        st = entry.stat()
                        identity = (st.st_dev, st.st_ino, st.st_mode)
                    except FileNotFoundError:
                        identity = None
                    found.append((entry.name, identity))
                names = tuple(sorted(found))
        except FileNotFoundError:
            pass
    for raw in watched:
        path = Path(os.path.expanduser(raw))
        paths.update((path, *path.parents))
    stamps = []
    for path in sorted(paths):
        try:
            st = path.stat() if path in files else path.lstat()
            if path in files or stat.S_ISLNK(st.st_mode):
                stamps.append((str(path), st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino))
            else:
                # A file created inside an ordinary directory changes no alias.
                stamps.append((str(path), st.st_dev, st.st_ino))
        except FileNotFoundError:
            stamps.append((str(path), None))
    return home, accounts, environment, names, tuple(stamps)


def _bounded_inventory(call, budget):
    """Bound filesystem discovery without consuming the caller's alarm."""
    if threading.current_thread() is not threading.main_thread():
        raise InventoryUnavailable()
    timer = signal.getitimer(signal.ITIMER_REAL)
    if timer[0] and timer[0] <= budget:
        raise InventoryUnavailable()
    handler = signal.getsignal(signal.SIGALRM)
    began = time.monotonic()
    active = True

    def expired(*_):
        if active:
            raise _InventoryDeadline()

    try:
        try:
            signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, budget)
            return call()
        finally:
            active = False
    except _InventoryDeadline:
        return None
    except (OSError, ValueError) as exc:
        raise InventoryUnavailable() from exc
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, timer[0] - (time.monotonic() - began)), timer[1])


def prepare_routing_table(*, refresh=False) -> None:
    """Rebuild the helper's full inventory within 250 ms, retrying failed starts.

    Prompt children inherit ``prepared`` even after a timeout. Only successful
    full builds renew the two-second freshness window.
    """
    if not refresh and _TABLE.get("prepared"):
        return
    _TABLE["prepared"] = True

    def rebuild():
        previous = getattr(_FORM_CACHE, "current", None)
        _FORM_CACHE.current = {}
        try:
            sig = _signature(None)
            inventory = _inventory_signature(None, sig[-1])
            table = _rules(None)
            if _inventory_signature(None, sig[-1]) != inventory:
                raise InventoryUnavailable()
            _TABLE.update(helper_table=table, helper_sig=sig, helper_inventory=inventory,
                          helper_at=time.monotonic(), helper_wall_at=time.time())
            return table
        finally:
            _FORM_CACHE.current = previous

    with contextlib.suppress(InventoryUnavailable):
        _bounded_inventory(rebuild, 0.25)


def _login_signature(sig):
    # Routing revisions and current owner exemptions do not change login aliases.
    return sig[0], sig[2], sig[6], sig[8], sig[9]


def _helper_table_fresh():
    # Wall time includes Mac sleep; monotonic time still bounds a backwards
    # clock adjustment. A wall clock earlier than the build is untrusted.
    mono_age = time.monotonic() - _TABLE.get("helper_at", 0.0)
    wall_age = time.time() - _TABLE.get("helper_wall_at", 0.0)
    return 0 <= mono_age <= _PROMPT_TABLE_MAX_AGE_S and 0 <= wall_age <= _PROMPT_TABLE_MAX_AGE_S


def _prompt_table(keys):
    """Use the helper's complete table for at most two seconds.

    A completed 5 ms login probe can reject it earlier. An incomplete probe
    cannot extend its lifetime or prevent use while it is fresh, but the
    full signature is not the only guard: it also walks non-login sources
    that can stall, so the cheap inventory signature recorded with the
    table is checked too. That check covers every alias input (config,
    login-shaped home entries, and the stamps of known login paths,
    symlink targets included), so a login change since the build withholds
    routing even when the full probe does not finish in its window.
    """
    previous = _TABLE.get("helper_table")
    built_sig = _TABLE.get("helper_sig")
    if previous is None or built_sig is None or not _helper_table_fresh():
        raise InventoryUnavailable()
    sig = _bounded_inventory(lambda: _signature(keys), 0.005)
    if sig is not None and _login_signature(sig) != _login_signature(built_sig):
        raise InventoryUnavailable()
    built_inventory = _TABLE.get("helper_inventory")
    if built_inventory is not None:
        # ~0.2 ms normally; 50 ms leaves room for a loaded Mac inside the
        # prompt budget. Not finishing means the inputs can't be read, so
        # withhold rather than trust the table.
        inventory = _bounded_inventory(lambda: _inventory_signature(keys, built_sig[-1]), 0.05)
        if inventory is None or inventory != built_inventory:
            raise InventoryUnavailable()
    if not _helper_table_fresh():
        raise InventoryUnavailable()
    # Never inherit ended owner exemptions from the helper's older routing view.
    table = copy.copy(previous)
    table.owner = keep_owner_words(_saved_view())
    return table


def _table(extra_keys) -> _Table:
    """The cached table, rebuilt on any source change or TTL expiry."""
    extra_keys = tuple(extra_keys) if extra_keys is not None else None
    current = getattr(_PROJECTION, "current", None)
    if current is not None:
        if current[0] is None:
            keys = getattr(_PROJECTION, "pull_keys", ())
            if set(extra_keys or ()) - set(keys):
                keys = tuple(dict.fromkeys((*keys, *extra_keys)))
                try:
                    table = _rules(keys, require_complete=True)
                except Exception:
                    raise PullInventoryUnavailable() from None
                # Owner provenance comes from the view being projected,
                # never from a saved view preceding this pull's fold.
                table.owner = []
                _PROJECTION.current = (None, table)
                _PROJECTION.pull_keys = keys
            return _PROJECTION.current[1]
        if current[0] == extra_keys:
            return current[1]
    previous = getattr(_FORM_CACHE, "current", None)
    _FORM_CACHE.current = {}
    try:
        return _fresh_table(extra_keys)
    finally:
        _FORM_CACHE.current = previous


def _fresh_table(extra_keys):
    try:
        sig = _signature(extra_keys)
    except Exception:  # noqa: BLE001
        return _rules(extra_keys)
    now = time.monotonic()
    with _LOCK:
        if _TABLE["sig"] == sig and now - _TABLE["at"] < _TABLE_TTL_S:
            return _TABLE["table"]
        old = _TABLE["sig"]
        if old is not None and old[6] != sig[6]:
            _TABLE["inventory_changed"] = True
    watched = sig[-1]
    inventory = _inventory_signature(extra_keys, watched)
    table = _rules(extra_keys)
    if _inventory_signature(extra_keys, watched) != inventory:
        raise InventoryUnavailable()
    with _LOCK:
        _TABLE.update(sig=sig, at=now, table=table, inventory=inventory, watched=watched, inventory_changed=False)
    return table


def _lookup(mapping: dict, found: str, fallback: str) -> str:
    return str(mapping.get(_fold(found), fallback))


def _scrub_text(text: str, table: _Table, keep: list[str]) -> str:
    if table.previous is not None:
        text = _scrub_text(text, table.previous, keep)
    pathy = "/" in text or "~" in text or "\\" in text
    held = [w for w in keep if w and w in text]
    for i, word in enumerate(held):
        text = text.replace(word, f"\x00{i}\x00")
    if not text.isascii():
        text = unicodedata.normalize("NFC", text)
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
    for variant in _decoded_variants(text):
        folded = _fold(variant)
        if unmatched_root or any(secret in folded for secret in table.secrets) \
                or (table.name_secrets is not None and table.name_secrets.search(folded)) \
                or (variant != text and _PATHLIKE.search(folded)):
            text = WITHHELD + "".join(f" \x00{i}\x00" for i in range(len(held)))
            break
    for i, word in enumerate(held):
        text = text.replace(f"\x00{i}\x00", word)
    return text


def _walk(value, table, keep):
    if isinstance(value, RoutingPage):
        return value
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


# A routing account key, "harness|login folder" (the folder may be empty).
_ACCOUNT_KEY = re.compile(r"(?i)(?<![\w-])(claude|codex|grok|muse|glm)\|")
# The final scan's: a harness name with anything before or after it ahead
# of the bar ("x_codex|", "codex2|") is still a key.
_ANY_ACCOUNT_KEY = re.compile(r"(?i)(?:claude|codex|grok|muse|glm)[^\s|]*\|")
_KEY_HOME = re.compile(r"[^\s\"'`,;)\]}]*")


def _account_keys(text: str, table: _Table) -> str:
    """Each account key in ``text`` replaced by the account's id. A folder
    Khipu knows is taken whole, spaces included; any other runs to the
    first space."""
    out, last = [], 0
    for m in _ACCOUNT_KEY.finditer(text):
        if m.start() < last:
            continue
        found = table.roots.match(text, m.end()) if table.roots is not None else None
        end = found.end() if found else _KEY_HOME.match(text, m.end()).end()
        out += [text[last:m.start()], str(_Id(m.group(1).lower(), text[m.end():end] or None))]
        last = end
    return "".join(out) + text[last:]


def _project_text(text: str, table: _Table) -> str:
    from khipu.redact import redact_secrets

    from khipu import work

    # work.py's rule: each path-shaped word that is left becomes "(a path)",
    # decoded before it is checked, a link's path and query included.
    text = work._cut_paths(_scrub_text(_account_keys(text, table), table, []))
    # Fail closed: an account key, a key-shaped secret or a path still in
    # the finished value, in any decoding of it, withholds the whole value.
    for variant in _decoded_variants(text):
        if _ANY_ACCOUNT_KEY.search(variant) or redact_secrets(variant, force=True)[1] \
                or (variant != text and work._has_path(variant)):
            return WITHHELD
    return text


def _project_walk(value, table, seen: dict | None = None):
    # One walk has one table, so a string's projection never changes within
    # it (``seen``); a view repeats its keys and words thousands of times.
    if seen is None:
        seen = {}
    if isinstance(value, str):
        got = seen.get(value)
        if got is None:
            got = seen[value] = _project_text(value, table)
        return got
    if isinstance(value, dict):
        return {(_project_walk(k, table, seen) if isinstance(k, str) else k): _project_walk(v, table, seen)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_project_walk(v, table, seen) for v in value)
    return value


# Text that can't carry a name: numbers and times.
_NAMELESS = re.compile(r"[0-9TZ:.+\- ]*")
# The ledger's and the Work view's own field names: the only object keys
# kept when the privacy table can't be built. A key merely shaped like a
# field name (a tracker id, a login folder's name) is withheld with its value.
VIEW_FIELDS = frozenset({
    "absent", "account_ref", "accounts", "actions", "add", "affected", "all", "also_owed", "as_of", "at",
    "at_text", "author", "available", "away", "blocked", "blocking", "board", "both", "built", "busy",
    "by", "by_kind", "by_state", "capped", "catch_up", "changed", "changed_today", "changes", "check",
    "checking", "children", "clamped", "cleared", "closed", "commit", "contents", "copied", "count",
    "counts", "counts_differ", "counts_in_file", "counts_in_ledger", "created_at", "days_left", "default",
    "digest", "disagrees", "done", "dropped", "ended_at", "entered", "error", "event", "evidence",
    "existing_ids", "extra", "failed", "failures", "fields", "fields_differ", "fields_differ_count",
    "file", "first_line", "fixing", "flagged", "for", "format", "from", "from_state", "generated_at",
    "goals", "gone", "hidden", "hist", "history", "holds", "hub", "hub_checked", "id", "ids", "if_state",
    "import_id", "imported", "imports", "in_dump", "in_offline_copy", "incomplete", "interval_s", "items",
    "jobs", "key", "kind", "label", "labels", "landed", "lanes", "last", "last_good", "late", "ledger",
    "ledger_as_of", "ledger_evidence_chars", "ledger_id_chars", "ledger_offline", "ledger_outbox",
    "ledger_owner_word", "ledger_purge_after", "ledger_stale_after", "ledger_title_chars", "left", "line",
    "link", "live", "looks_like", "loose_end_count", "loose_ends", "masked", "matched", "meaning",
    "merged", "merged_at", "missing", "missing_parent", "name", "needs_owner", "new", "new_entries",
    "new_rows", "new_words", "none", "note", "now", "of", "off", "offline", "offline_as_of",
    "offline_copy_error", "offline_copy_waiting", "ok", "old", "older", "oldest_age_s", "on", "op",
    "op_id", "open", "opened_at", "orig", "other_keys", "other_project", "out", "outbox", "outcome",
    "owner", "parent", "parent_missing", "pending", "plan", "privacy", "problem", "problems", "project",
    "project_key", "projects", "prs", "pruned", "purged", "purged_ids", "purged_markers", "queue_added",
    "queue_removed", "queue_restored", "queued", "queued_at", "quiet", "raw", "reason", "reason_word",
    "refresh_s", "refs", "refused", "remove", "removed", "removed_at", "removed_by", "removed_note",
    "removed_waiting_to_purge", "requested", "restore", "result", "retried", "rewritten", "row", "row_id",
    "rows", "rules", "runner", "running", "runs", "seq", "set", "shape", "shipped", "show_now", "shown",
    "since", "skipped", "skipped_purged", "skipped_removed", "source", "sources", "stage", "stage_at",
    "stage_known", "stage_words", "stale", "stale_sources", "stamp", "started_at", "state", "state_known",
    "state_words", "status", "stopped", "stopped_early", "stored", "summary", "task", "testing", "text",
    "title", "to", "to_state", "trains", "ts", "unreadable", "updated", "updated_at", "valid_until",
    "value", "verb", "verdict", "verified", "version", "vocab", "waiting", "waiting_list_ignored",
    "warnings", "with_parent", "words", "work_clock_slack", "work_file_max_bytes", "work_finished_for",
    "work_gone_after", "work_job_quiet_after", "work_line_chars", "work_max_jobs", "work_max_prs",
    "work_max_runs", "work_max_trains", "work_merged_for", "work_projects", "work_refresh",
    "work_stale_after", "written_at"
})
INCOMPLETE = "Khipu withheld this answer: it could not load its list of private names and folders."


def _withhold_names(value, fixed=None, names=None):
    """``value`` with every string that could carry a name withheld, for
    when the replacement table can't be built: a login folder's name can
    then not be told from any other word. Kept: numbers, times, work.py's
    own words and object keys from ``VIEW_FIELDS`` or those words (never this Mac's
    user name). Every other key is withheld, and its value with it."""
    if fixed is None:
        from khipu import work

        fixed = work._fixed_words()
        home = _home_dir()
        names = {n.casefold() for n in (os.path.basename(home or ""), os.environ.get("USER", "")) if n}
    if isinstance(value, str):
        return value if value in fixed or _NAMELESS.fullmatch(value) else WITHHELD
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str) or ((k in VIEW_FIELDS or k in fixed) and k.casefold() not in names):
                out[k] = _withhold_names(v, fixed, names)
            else:
                out[WITHHELD] = WITHHELD
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(_withhold_names(v, fixed, names) for v in value)
    return value


class RoutingPage(str):
    """An internal fragment of a fully projected routing entry.

    JSON callers cannot supply this provenance. Only delivery may create it,
    after scanning the complete source and before cutting a page.
    """


# Every secret shape in redact.py needs one of these markers (a vendor
# prefix, a credential keyword, "://" for a URL credential, a key block);
# pure-ASCII text holding none of them cannot match, so the full regex
# scan is skipped for it. Lowercased markers only ever over-match, never
# under-match on ASCII. Non-ASCII variants always get the full scan:
# redact_secrets is case-insensitive, and re.I folds some non-ASCII
# letters (long s, dotless/dotted i, Kelvin sign) onto ASCII, so a
# keyword spelled with those can hide from the ASCII markers.
_SECRET_MARKERS = ("-----begin", "sk-", "xai-", "k_live_", "k_test_", "akia", "asia",
                   "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "xox", "aiza",
                   "ya29.", "eyj", "bearer", "basic", "://", "key", "secret", "token",
                   "passw", "pwd", "dsn", "connection", "private")


def _has_secret(text: str) -> bool:
    from khipu.redact import redact_secrets

    for variant in _decoded_variants(text):
        # The check is on each decoded variant, not the original text:
        # an ASCII escape (e.g. \u0131) only becomes non-ASCII on decode.
        if not variant.isascii():
            if redact_secrets(variant, force=True)[1]:
                return True
            continue
        lowered = variant.lower()
        if not any(marker in lowered for marker in _SECRET_MARKERS):
            continue
        if redact_secrets(variant, force=True)[1]:
            return True
    return False


def _strict_text(text: str, table: _Table, keep: list[str], *, _secret_known: bool | None = None) -> str:
    # An exemption must never split a credential into undetectable pieces.
    if _secret_known is None:
        if _has_secret(text):
            return WITHHELD
    elif _secret_known:
        return WITHHELD
    held = [w for w in keep if w and w in text]
    for variant in _decoded_variants(text):
        if "|" not in variant:
            continue
        matches = list(_ANY_ACCOUNT_KEY.finditer(variant))
        if not matches:
            continue
        spans = [(m.start(), m.end()) for word in held
                 for m in re.finditer(re.escape(word), variant)]
        if any(not any(start <= match.start() and match.end() <= end for start, end in spans)
               for match in matches):
            return WITHHELD
    return text


_OWNER_REPLACEMENTS: dict[tuple, list] = {}


def _owner_token_replacements(words):
    from khipu.redact import MASK, redact_secrets

    cache_key = tuple(words)
    cached = _OWNER_REPLACEMENTS.get(cache_key)
    if cached is not None:
        return cached
    replacements = []
    for word in words:
        if not _has_secret(word):
            continue
        cleaned = redact_secrets(word, force=True)[0]
        # Encoded credentials must be removed before owner-word masking too.
        cleaned = re.sub(r"\S+", lambda m: MASK if _has_secret(m.group()) else m.group(), cleaned)
        # A whole owner credential must not mask only the prefix of a longer
        # pending credential and leave its otherwise unrecognizable suffix.
        pattern = re.compile(r"(?<![A-Za-z0-9_./%&;=+-])" + re.escape(word) + r"(?![A-Za-z0-9_./%&;=+-])")
        replacements.append((word, cleaned, pattern))
    if len(_OWNER_REPLACEMENTS) >= 8:
        _OWNER_REPLACEMENTS.clear()
    _OWNER_REPLACEMENTS[cache_key] = replacements
    return replacements


def redact_owner_tokens(text: str, words: Iterable[str]) -> str:
    """Remove credentials only inside complete, current owner phrases."""
    for _word, cleaned, pattern in _owner_token_replacements(sorted(words, key=len, reverse=True)):
        text = pattern.sub(lambda _m: cleaned, text)
    return text


def scrub_strict(value, *, keep: Iterable[str] = (), extra_keys: Iterable[str] | None = None,
                 cut_paths: bool = False):
    """``scrub``, then the strict projection's final scan: any string still
    holding an account key (``harness|``) or a secret-shaped token is
    withheld whole. Current owner phrases preserve their words while
    secret tokens inside them are removed before any exemption is applied.
    ``cut_paths`` additionally removes arbitrary filesystem path shapes for
    a gateway copy, including inside owner words; local projections keep
    their existing owner-word policy."""
    table = _table(extra_keys)
    words = sorted({w for w in [*keep, *table.owner] if isinstance(w, str) and w}, key=len, reverse=True)
    # Only a complete owner phrase may authorize token-level redaction.
    # A pending credential containing a short owner word still fails closed.
    replacements = _owner_token_replacements(words)
    clean_words = {word: cleaned for word, cleaned, _pattern in replacements}
    words = sorted({clean_words.get(word, word) for word in words}, key=len, reverse=True)
    def walk(item):
        if isinstance(item, RoutingPage):
            return item
        if isinstance(item, str):
            for _word, cleaned, pattern in replacements:
                item = pattern.sub(lambda _m: cleaned, item)
            # Scan the source before any path/name or owner-word masking.
            if _has_secret(item):
                return WITHHELD
            item = _strict_text(item, table, words, _secret_known=False)
            if item == WITHHELD:
                return item
            scrubbed = _scrub_text(item, table, words)
            # An unchanged value already passed this exact scan; only a
            # scrubbed value can have joined fragments into a new shape.
            scrubbed = scrubbed if scrubbed == item else _strict_text(scrubbed, table, words)
            if cut_paths:
                from khipu import work

                scrubbed = work._cut_paths(scrubbed)
                if any(work._has_path(variant) for variant in _decoded_variants(scrubbed)):
                    return WITHHELD
            return scrubbed
        if isinstance(item, dict):
            return {(walk(k) if isinstance(k, str) else k): walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, tuple):
            return tuple(walk(v) for v in item)
        return item

    return walk(value)


class PullInventoryUnavailable(RuntimeError):
    """The pull cannot be projected with a complete login table."""


@contextlib.contextmanager
def pull_projection():
    """Use one complete login table for a pull, or withhold the answer.

    Unlike the bounded hook projection, a pull must not use a local-only
    fallback. The full inventory and its resolved aliases must finish.
    """
    current = getattr(_PROJECTION, "current", None)
    if current is not None and current[0] is None:
        yield
        return
    try:
        _config_whole()
        # Discovery intentionally tolerates unreadable directories. That is
        # not evidence of completeness at this privacy boundary.
        from khipu import discover

        with os.scandir(_home_dir()) as entries:
            for entry in entries:
                if discover.name_match(entry.name):
                    entry.is_dir(follow_symlinks=True)
        # The existing routing read supplies fresh account keys through
        # _table. Another read here would add a third lock wait and would
        # misclassify routing-read failures as inventory failures.
        keys = ()
        table = _rules(keys, require_complete=True)
        table.owner = []
    except Exception:
        raise PullInventoryUnavailable() from None
    previous = getattr(_PROJECTION, "current", None)
    previous_keys = getattr(_PROJECTION, "pull_keys", ())
    _PROJECTION.current = (None, table)
    _PROJECTION.pull_keys = keys
    try:
        yield
    finally:
        _PROJECTION.current = previous
        _PROJECTION.pull_keys = previous_keys


def login_inventory_available() -> bool:
    """Only the complete table prepared for this pull can cover lane aliases.

    A successful complete pull inventory proves coverage even when no
    private login exists. Hook tables retain their helper freshness gate;
    a general cached table alone never grants pull coverage.
    """
    current = getattr(_PROJECTION, "current", None)
    if current is None or current[0] is not None or current[1].previous is not None:
        return False
    return True


def _projected(value):
    """(``value`` projected, whether it is complete)."""
    try:
        table = strict_table()
    except Exception:  # noqa: BLE001
        return _withhold_names(value), False
    return _project_walk(value, table), True


def project(value):
    """``value`` as the ledger and the Work view hand it to an agent or a
    JSON reader (khipu_ledger, ``khipu ledger ... --json``, ``khipu work
    --json``, ``khipu work check --json``, view.json): account keys become
    account ids, then ``scrub``'s replacements, then a final scan that
    withholds any string still holding an account key, a secret or a
    private path. No owner's words are kept whole here.

    When the replacement table can't be built, or the config naming login
    folders can't be read, it fails closed: every string
    that could carry a name is withheld, and a dict says so with
    ``"incomplete": true``."""
    out, complete = _projected(value)
    if not complete and isinstance(out, dict):
        out["incomplete"] = True
    return out


def emit(payload=None, text: str | None = None, *, as_json: bool = False, stream=None, to_file=None,
         indent: int | None = 2, sort_keys: bool = False) -> None:
    """The one way a ``khipu ledger`` or ``khipu work`` answer leaves Khipu,
    on stdout, stderr or a file the caller named: ``payload`` as JSON when
    ``as_json``, else ``text``; either way through ``project`` first. Text is
    projected line by line, so one withheld line never blanks the rest."""
    import json
    import sys

    if as_json:
        out = json.dumps(project(payload), indent=indent, sort_keys=sort_keys, default=str, ensure_ascii=False)
    elif text is not None:
        lines, complete = _projected(str(text).split("\n"))
        out = "\n".join(lines + ([] if complete else [INCOMPLETE]))
    else:
        return
    if to_file is not None:
        with open(to_file, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        return
    print(out, file=stream or sys.stdout)


def error_text(exc: BaseException) -> str:
    """An exception as an agent may read it: a filesystem error in fixed
    words (its own text names the file), anything else scrubbed."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: Khipu could not read or write one of its files"
    if isinstance(exc, SystemExit):
        return f"SystemExit: capture rejected the payload (exit {scrub(str(exc.code))})"
    return f"{type(exc).__name__}: {scrub(str(exc))}"
