"""The live view and Waiting on you (Work view, phase 3;
docs/plans/2026-10-15-work-view.md, Part B).

Each project's job runner writes a summary file (``work-summary.json``,
format 1, schema in ``docs/formats/work-summary-1.schema.json``). Khipu only
reads it, through ``khipu.safe_read`` (the private-folder and login-folder
checks, an open that follows no link, a regular file with one name, size
checked, and the name checked again after the read; no lock, no temp file).
Nothing here writes a project's files or sends anything to a runner.

``build_view`` folds the summaries, the ledger, Khipu's open commitments that
need the user and the routing record into one document, ``khipu-work-view``
version 1, kept at ``state/work/view.json``. It carries no path, login folder,
account key or pre-formatted local time, so a later phone or web view needs
only it and the ledger reads. Every string leaves through ``_Scan``: its
free text cleaned where it is read, then one fail-closed scan of the finished
document that withholds any field still holding a path, a login folder, an
account key or a secret (and, when the login-folder list can't be loaded,
every string that is not a time or work.py's own words, saying so).
"""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from khipu import safe_read

FORMAT = "khipu-work-summary"
FORMAT_VERSION = 1
VIEW_FORMAT = "khipu-work-view"
VIEW_VERSION = 1
PROJECTS_MOST = 64
NOW_LINES = 3
NOW_CHARS = 90
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_LEDGER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
_TOP_LISTS = ("jobs", "prs", "trains")

# ---- settings -----------------------------------------------------------------
#
# Every cap and time is a setting with Change... in Settings -> Work (rule 7);
# 0 lifts a cap. The job status line is Matt's design, not a limit: its only
# switch is ``show_now`` per project.

SETTINGS: dict[str, tuple[str, Any, str]] = {
    "work_projects": ("work_projects", [], "Projects whose job runner writes a summary file"),
    "work_file_max_bytes": ("count", 256 * 1024, "Largest summary file read, in bytes (0: no limit)"),
    "work_max_jobs": ("count", 50, "Most jobs shown per project (0: no limit)"),
    "work_max_prs": ("count", 200, "Most pull requests shown per project (0: no limit)"),
    "work_max_trains": ("count", 20, "Most release trains shown per project (0: no limit)"),
    "work_max_runs": ("count", 20, "Most test runs shown per project (0: no limit)"),
    "work_line_chars": ("count", 200, "Longest summary line (0: no limit)"),
    "work_stale_after": ("auto_duration", "auto",
                         "A summary counts as out of date after (auto: five of the runner's intervals; 0: never)"),
    "work_job_quiet_after": ("duration", 15 * 60, "A running job counts as quiet after (0: never)"),
    "work_gone_after": ("duration", 24 * 3600, "A project counts as not reporting after (0: never)"),
    "work_finished_for": ("duration", 30 * 60, "Show finished jobs for (0: always)"),
    "work_merged_for": ("duration", 7 * 86400,
                        "Show merged pull requests and shipped or dropped trains for (0: always)"),
    "work_clock_slack": ("duration", 5 * 60, "A time this far ahead of this Mac's clock is shown as now"),
    "work_refresh": ("duration", 5, "How often the open Work tab asks for the view"),
}
_STALE_MIN = 120
_STALE_NO_INTERVAL = 600


def _coerce(kind: str, value: Any, key: str) -> Any:
    from khipu import routing

    if kind == "auto_duration":
        if isinstance(value, str) and value.strip().lower() == "auto":
            return "auto"
        return routing._coerce("duration", value, key)
    if kind == "work_projects":
        return _coerce_projects(value, key)
    return routing._coerce(kind, value, key)


def _coerce_projects(value: Any, key: str) -> list[dict]:
    from khipu import ledger, routing

    data = routing._loads(value) if isinstance(value, str) else value
    if not isinstance(data, list) or len(data) > PROJECTS_MOST:
        raise ValueError(f'{key} must be a JSON list of at most {PROJECTS_MOST} '
                         '{"project", "name", "file", "on", "show_now"}')
    out: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        where = f"{key} entry {i + 1}"
        if not isinstance(entry, dict) or set(entry) - {"project", "name", "file", "on", "show_now"}:
            raise ValueError(f"{where} must have only project, name, file, on and show_now")
        try:
            project = ledger._check_project(entry.get("project"))
        except ledger.LedgerError as exc:
            raise ValueError(f"{where}: {exc}") from None
        if project in seen:
            raise ValueError(f"{key}: two entries are for {project!r}")
        seen.add(project)
        name = entry.get("name")
        name = routing._short_text(name, f"{where}: name") if name not in (None, "") else project
        # Kept exactly as given: safe_read parses it, here and at each read.
        path = routing._short_text(entry.get("file"), f"{where}: file", 1024)
        try:
            folder, file_name = safe_read.parse(path, full=True)
        except safe_read.NotFullPath:
            raise ValueError(f"{where}: give the summary file's full path") from None
        except safe_read.Refused:
            raise ValueError(f"{where}: give the summary file's full path, with no \".\" or \"..\" "
                             "folder and no \"/\" at the end") from None
        if routing.credential_location(Path(folder, file_name)):
            raise ValueError(f"{where}: a summary file can't be inside a private folder (API Keys, My Portfolio, "
                             "a keychain or an auth.json)")
        out.append({"project": project, "name": name, "file": path,
                    "on": routing._coerce("switch", entry.get("on", True), f"{where}: on"),
                    "show_now": routing._coerce("switch", entry.get("show_now", True), f"{where}: show_now")})
    return out


def settings(config: dict | None = None) -> dict[str, Any]:
    """Each work setting's value: the stored one when valid, else the default."""
    if config is None:
        from khipu.config import load_config

        config = load_config()
    out: dict[str, Any] = {}
    for key, (kind, default, _words) in SETTINGS.items():
        value = default
        if config.get(key) is not None:
            try:
                value = _coerce(kind, config[key], key)
            except (ValueError, TypeError):
                value = default
        out[key] = value
    return out


def settings_status() -> dict[str, dict]:
    from khipu.config import load_config

    config = load_config()
    values = settings(config)
    out = {}
    for key, (kind, default, words) in SETTINGS.items():
        source = "default"
        if config.get(key) is not None:
            try:
                _coerce(kind, config[key], key)
                source = "file"
            except (ValueError, TypeError):
                source = "invalid"
        out[key] = {"value": values[key], "default": default, "kind": kind, "source": source, "words": words}
    return out


def set_setting(key: str, raw: Any) -> Path:
    """Store a work setting (``None`` removes it: the default again)."""
    from khipu.config import _load_config_for_mutation, save_config

    if key not in SETTINGS:
        raise KeyError(f"unknown work setting {key!r}")
    data = _load_config_for_mutation()
    if raw is None:
        data.pop(key, None)
    else:
        data[key] = _coerce(SETTINGS[key][0], raw, key)
    return save_config(data)


# ---- time, text and state files ----------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _z_up(dt: datetime | None) -> str | None:
    """``_z`` for a deadline: a part second rounds up, so the deadline is
    never at or before the instant it was worked out from."""
    if dt is not None and dt.microsecond:
        dt = dt.replace(microsecond=0) + timedelta(seconds=1)
    return _z(dt)


def _parse_time(value: Any) -> datetime | None:
    """An RFC 3339 time with ``Z`` or an offset; anything else is None."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", value.strip()):
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, OverflowError):
        return None


def state_dir() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / "work"


def view_file() -> Path:
    return state_dir() / "view.json"


def _last_file(project: str) -> Path:
    return state_dir() / "last" / (quote(project, safe="") + ".json")


def _write_json(path: Path, data: Any) -> None:
    """Replace ``path`` atomically (a temp file beside it, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def _trips_redaction(text: str) -> bool:
    """Whether Khipu's secret redaction (and the routing reader's extra
    patterns) would change ``text``: such a field is left out whole."""
    from khipu import routing_sources

    return routing_sources._redacted(text, len(text) + 1) != text


PATH_WORDS = "(a path)"


def _path_token(token: str) -> bool:
    """Whether one space-free word is path-shaped once the quotes and
    punctuation around it are set aside."""
    return bool(_PATH_SHAPE.search(token.lstrip(_WRAP_OPEN).rstrip(_WRAP_CLOSE)))


def _pieces(token: str) -> list[tuple[str, bool]]:
    """``token`` split into (piece, is_link): each link by its exact span,
    and the words around it split again at "=", "," and ";" (kept as their
    own pieces), so a path joined to a link or a value is still seen."""
    out: list[tuple[str, bool]] = []
    last = 0
    for m in _LINK.finditer(token):
        out += [(p, False) for p in _JOINS.split(token[last:m.start()]) if p]
        out.append((m.group(), True))
        last = m.end()
    out += [(p, False) for p in _JOINS.split(token[last:]) if p]
    return out


def _has_path(text: str) -> bool:
    return any(not link and _path_token(p) for t in text.split() for p, link in _pieces(t))


def _cut_paths(text: str) -> str:
    """Each path-shaped piece of a word replaced with PATH_WORDS (its quotes
    and punctuation kept); links, cut out by their exact span, and every
    other piece are left as they are."""
    out = []
    for token in text.split(" "):
        parts = []
        for piece, link in _pieces(token):
            if not link and _path_token(piece):
                core = piece.lstrip(_WRAP_OPEN).rstrip(_WRAP_CLOSE)
                start = piece.index(core)
                piece = piece[:start] + PATH_WORDS + piece[start + len(core):]
            parts.append(piece)
        out.append("".join(parts))
    return " ".join(out)


def clean_text(value: Any, cap: int) -> str | None:
    """One line of a runner's words as the view may show it: control
    characters removed, spaces collapsed, path-like tokens cut to their last
    name, clipped at ``cap`` (0: no clip). None when it is not text, is
    empty, or trips secret redaction (never shown masked)."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value)
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())
    if not text or _trips_redaction(text) or _ACCOUNT_KEY.search(text):
        return None
    text = _cut_paths(text)
    if cap and len(text) > cap:
        text = text[: max(1, cap - 1)].rstrip() + "…"
    return text


# A routing account key ("harness|login folder"). A word is path-shaped when it
# starts with ~, ./, ../, / or \ (or a separator follows punctuation inside it,
# as in "dir=/x"), is a drive path, holds two or more separators, or holds one
# and ends in a file extension. So "a/b/c" and "source/private.txt" are paths;
# "CI/CD", "and/or" and "3/4" are not. A link's own path is not a local path:
# links are cut out first.
_ACCOUNT_KEY = re.compile(r"(?i)(?<![\w-])(?:claude|codex|grok|muse)\|")
_PATH_SHAPE = re.compile(r"^~(?:[/\\]|[A-Za-z_]|$)|^\.{1,2}[/\\]|^[/\\](?=[\w.~-])|[^\w.-][/\\]"
                         r"|[A-Za-z]:[/\\]|[/\\].*[/\\]|[/\\][^/\\]*\.[A-Za-z0-9]+$")
_WRAP_OPEN = "\"'`([{<"
_WRAP_CLOSE = "\"'`)]}>,;:!?."
# A link's exact span: it ends at a space, a quote or a bracket, so text
# joined to its end is checked on its own.
_LINK = re.compile(r"https://[^\s\"'`<>()\[\]{}]+")
_JOINS = re.compile(r"([=,;])")


def _private_text(text: str, table: Any = None) -> bool:
    """Whether ``text`` may not leave work.py: it trips secret redaction,
    holds an account key or is path-shaped, or (given agent_view's table)
    names a login folder, the home folder or a user name."""
    one = " ".join(text.split())
    if _trips_redaction(one) or _ACCOUNT_KEY.search(text) or _has_path(text):
        return True
    if table is not None:
        from khipu import agent_view

        return agent_view._scrub_text(text, table, []) != text
    return False


def _word(value: Any, shape: re.Pattern = _ID_RE) -> str | None:
    """An id, state or reference a file gives: the value when it has
    ``shape`` and nothing private in it, else None."""
    if isinstance(value, str) and shape.match(value) and not _private_text(value):
        return value
    return None


PRIVACY_INCOMPLETE = ("Khipu couldn't load its list of login folders and user names, so every name, title "
                      "and line that could hold one is left out of this view.")
_Z_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SCHEMA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _fixed_words() -> frozenset[str]:
    """Every string work.py itself writes into a view: the only strings a
    view may keep when the login-folder table can't be loaded."""
    words = {VIEW_FORMAT, UNKNOWN_STATE, WITHHELD_WORDS, PRIVACY_INCOMPLETE, *JOB_KINDS, *JOB_ENDED, *VERDICTS,
             *RUN_STATES, *SOURCE_PROBLEMS.values(), *STATE_APP_WORDS, *STATE_APP_WORDS.values(),
             "ok", "stale", "gone", "missing", "refused", "off", "none", "unreadable", "too_big", "wrong_project",
             "newer_format", "open", "needs", "done", "dropped", "ledger", "commitment", "routing", "rule", "hold",
             "seeded", "same", "not_same", "snooze", "hide", "drop", "answered", "open_ledger", "open_link",
             "open_owed", "open_routing", "Confirm a standing rule a conversation changed"}
    for table in (JOB_STATES, PR_STAGES, TRAIN_STATES):
        words.update(table)
        words.update(table.values())
    return frozenset(words)


class _Scan:
    """The fail-closed last pass over everything work.py hands out: any
    string still private is withheld (None in an object, left out of a
    list; an object key that is private drops its entry).

    Without the login-folder table (``degraded``) a name can't be told
    from other words, so only times and work.py's own words are kept, and
    only object keys shaped like the view's own field names."""

    def __init__(self) -> None:
        try:
            from khipu import agent_view

            self.table = agent_view._table(None)
        except Exception:  # noqa: BLE001  (fail closed: see __call__)
            self.table = None
        self.degraded = self.table is None
        if self.degraded:
            self.fixed = _fixed_words()
            from khipu import agent_view

            home = agent_view._home_dir()
            self.names = {n.casefold() for n in (os.path.basename(home or ""), os.environ.get("USER", "")) if n}

    def _keep(self, text: str, key: bool) -> bool:
        if self.degraded:
            if key:
                return bool(_SCHEMA_KEY.match(text) or text in self.fixed) and text.casefold() not in self.names
            return text in self.fixed or bool(_Z_TIME.match(text))
        try:
            return not _private_text(text, self.table)
        except Exception:  # noqa: BLE001  (unsure is private)
            return False

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return value if self._keep(value, False) else None
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items() if not isinstance(k, str) or self._keep(k, True)}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value if not isinstance(v, str) or self._keep(v, False)]
        return value


def _clean_link(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    link = urlunsplit(("https", parts.hostname + (f":{port}" if port else ""), parts.path, "", ""))
    return None if _trips_redaction(link) else link


def _label(value: Any) -> str | None:
    from khipu.routing_sources import WITHHELD, safe_name

    # An account label may hold single spaces ("Claude second"): the strict
    # shape check runs on the label with the spaces made hyphens.
    if isinstance(value, str) and " " in value.strip():
        one = " ".join(value.split())
        return one if safe_name(one.replace(" ", "-")) != WITHHELD else WITHHELD
    return safe_name(value)


# ---- reading one summary file -------------------------------------------------------

JOB_STATES = {"queued": "Waiting to start", "running": "Running", "waiting": "Waiting", "done": "Finished",
              "failed": "Stopped with a problem", "stopped": "Stopped"}
JOB_KINDS = frozenset({"build", "check", "fix", "review", "test", "release", "other"})
JOB_ENDED = frozenset({"done", "failed", "stopped"})
PR_STAGES = {"built": "Built", "checking": "Being checked", "fixing": "Being fixed", "cleared": "Cleared",
             "flagged": "Flagged", "merged": "Merged", "closed": "Closed without merging"}
VERDICTS = frozenset({"cleared", "flagged", "unclear"})
TRAIN_STATES = {"open": "Gathering changes", "testing": "Testing", "blocked": "Held up", "shipped": "Shipped",
                "dropped": "Dropped"}
RUN_STATES = frozenset({"queued", "running", "passed", "failed", "skipped"})
UNKNOWN_STATE = "State not known to this Khipu"


class _Drop(Exception):
    """One item can't be read; it is left out and counted."""


class _Reading:
    """What one read of one file found besides its items."""

    def __init__(self, project: str, cfg: dict, now: datetime):
        self.project, self.cfg, self.now = project, cfg, now
        self.dropped = 0  # every left-out item, another project's included
        self.other_project = 0  # how many of those named another project
        self.clamped = 0
        self.capped: dict[str, dict[str, int]] = {}

    def time(self, value: Any, *, required: bool = True) -> str | None:
        dt = _parse_time(value)
        if dt is None:
            if required or value is not None:
                raise _Drop
            return None
        if (dt - self.now).total_seconds() > self.cfg["work_clock_slack"]:
            self.clamped += 1
            dt = self.now
        return _z(dt)

    def ident(self, value: Any) -> str:
        word = _word(value)
        if word is None:
            raise _Drop
        return word

    def text(self, value: Any, cap: int, *, required: bool = False) -> str | None:
        text = clean_text(value, cap)
        if text is None and required:
            raise _Drop
        return text

    def same_project(self, item: dict) -> None:
        if item.get("project") != self.project:
            self.other_project += 1
            raise _Drop


def _state(value: Any, words) -> tuple[str | None, bool]:
    """(the state word, whether this Khipu knows it). An unknown word keeps
    the item; a missing one drops it."""
    if not isinstance(value, str) or not value.strip():
        raise _Drop
    word = value.strip()
    if word in words:
        return word, True
    return _word(word), False


def _job(item: dict, r: _Reading) -> dict:
    r.same_project(item)
    state, known = _state(item.get("state"), JOB_STATES)
    out: dict[str, Any] = {
        "id": r.ident(item.get("id")), "state": state, "state_known": known,
        "state_words": JOB_STATES.get(state or "", UNKNOWN_STATE),
        "started_at": r.time(item.get("started_at")), "updated_at": r.time(item.get("updated_at")),
        "summary": r.text(item.get("summary"), r.cfg["work_line_chars"], required=True),
    }
    kind = item.get("kind") if isinstance(item.get("kind"), str) else None
    out["kind"] = kind if kind in JOB_KINDS else ("other" if kind else None)
    target: dict[str, str] = {}
    raw_for = item.get("for") if isinstance(item.get("for"), dict) else {}
    for key, shape in (("pr", _ID_RE), ("train", _ID_RE), ("ledger", _LEDGER_ID_RE)):
        word = _word(raw_for.get(key), shape)
        if word:
            target[key] = word
    task = r.text(raw_for.get("task"), 80)
    if task:
        target["task"] = task
    out["for"] = target
    for key in ("account", "model", "lane"):
        out[key] = _label(item.get(key))
    out["ended_at"] = r.time(item.get("ended_at"), required=False)
    lines = []
    raw_now = item.get("now") if isinstance(item.get("now"), list) else []
    for line in raw_now[-NOW_LINES:]:
        if not isinstance(line, dict) or _parse_time(line.get("at")) is None:
            continue
        text = clean_text(line.get("text"), NOW_CHARS)
        if text is not None:
            lines.append({"at": r.time(line.get("at")), "text": text})
    out["now"] = lines
    return out


def _pr(item: dict, r: _Reading) -> dict:
    r.same_project(item)
    stage, known = _state(item.get("stage"), PR_STAGES)
    out: dict[str, Any] = {
        "id": r.ident(item.get("id")), "stage": stage, "stage_known": known,
        "stage_words": PR_STAGES.get(stage or "", UNKNOWN_STATE),
        "title": r.text(item.get("title"), 120, required=True), "stage_at": r.time(item.get("stage_at")),
        "opened_at": r.time(item.get("opened_at"), required=False),
        "merged_at": r.time(item.get("merged_at"), required=stage == "merged"),
        "link": _clean_link(item.get("link")), "summary": r.text(item.get("summary"), r.cfg["work_line_chars"]),
        "verdict": None,
    }
    verdict = item.get("verdict")
    if isinstance(verdict, dict) and isinstance(verdict.get("outcome"), str) and verdict["outcome"] in VERDICTS:
        out["verdict"] = {"outcome": verdict["outcome"], "first_line": clean_text(verdict.get("first_line"), 200),
                          "by": _label(verdict.get("by")), "at": r.time(verdict.get("at"), required=False)}
    return out


def _train(item: dict, r: _Reading) -> dict:
    r.same_project(item)
    state, known = _state(item.get("state"), TRAIN_STATES)
    runs = []
    raw_runs = item.get("runs") if isinstance(item.get("runs"), list) else []
    for run in raw_runs:
        try:
            if not isinstance(run, dict):
                raise _Drop
            run_state, run_known = _state(run.get("state"), RUN_STATES)
            runs.append({"name": r.text(run.get("name"), 60, required=True), "state": run_state,
                         "state_known": run_known, "at": r.time(run.get("at")),
                         "summary": r.text(run.get("summary"), r.cfg["work_line_chars"])})
        except (_Drop, TypeError, ValueError, OverflowError):
            r.dropped += 1
    ident = r.ident(item.get("id"))
    prs = [w for w in map(_word, item["prs"]) if w] if isinstance(item.get("prs"), list) else []
    return {"id": ident, "name": r.text(item.get("name"), 40, required=True), "state": state,
            "state_known": known, "state_words": TRAIN_STATES.get(state or "", UNKNOWN_STATE),
            "updated_at": r.time(item.get("updated_at")), "prs": prs, "runs": runs,
            "summary": r.text(item.get("summary"), r.cfg["work_line_chars"])}


def _newest(items: list[dict], key: str, cap: int, r: _Reading, what: str) -> list[dict]:
    """The newest ``cap`` items (0: all), in the order the file gave them."""
    if not cap or len(items) <= cap:
        return items
    keep = {id(i) for i in sorted(items, key=lambda i: i.get(key) or "", reverse=True)[:cap]}
    r.capped[what] = {"shown": cap, "of": len(items)}
    return [i for i in items if id(i) in keep]


_READERS = {"jobs": (_job, "updated_at", "work_max_jobs"), "prs": (_pr, "stage_at", "work_max_prs"),
            "trains": (_train, "updated_at", "work_max_trains")}


def _cap_runs(trains: list[dict], cfg: dict, r: _Reading) -> None:
    # work_max_runs counts every run of the trains shown, not each train's.
    runs = [run for t in trains for run in t["runs"]]
    kept = {id(run) for run in _newest(runs, "at", cfg["work_max_runs"], r, "runs")}
    for t in trains:
        t["runs"] = [run for run in t["runs"] if id(run) in kept]


def _within_caps(contents: dict | None, cfg: dict) -> dict | None:
    """A last good read held to the caps and line limit set now: it was
    cleaned under the settings of its day, which may since be lower."""
    if not contents:
        return contents
    out = copy.deepcopy(contents)
    line = cfg["work_line_chars"]
    r = _Reading(out.get("project") or "", cfg, _now())
    jobs = []
    for item in out.get("jobs") or []:
        item["summary"] = clean_text(item.get("summary"), line)
        if item["summary"] is None:  # a job's summary is required
            r.dropped += 1
            continue
        jobs.append(item)
    out["jobs"] = jobs
    for item in [*(out.get("prs") or []), *(out.get("trains") or []),
                 *(run for t in out.get("trains") or [] for run in t.get("runs") or [])]:
        item["summary"] = clean_text(item.get("summary"), line)
    for key, (_reader, newest, cap_key) in _READERS.items():
        out[key] = _newest(out.get(key) or [], newest, cfg[cap_key], r, key)
    _cap_runs(out["trains"], cfg, r)
    capped = dict(out.get("capped") or {})
    for what, now_capped in r.capped.items():
        before = capped.get(what) if isinstance(capped.get(what), dict) else {}
        capped[what] = {"shown": now_capped["shown"], "of": max(now_capped["of"], before.get("of") or 0)}
    out["capped"] = capped
    out["dropped"] = (out.get("dropped") or 0) + r.dropped
    return out


class Unreadable(Exception):
    """The file as a whole can't be read; ``state`` says which way."""

    def __init__(self, state: str, problem: str, **detail: Any):
        super().__init__(problem)
        self.state, self.problem, self.detail = state, problem, detail


def _project_slug(project: str) -> str:
    """A ledger project key as a summary id may spell it (``owner/repo`` ->
    ``repo``): a summary id can't hold a slash."""
    return project.rsplit("/", 1)[-1]


def parse_summary(data: bytes, project: str | None, cfg: dict, now: datetime) -> dict:
    """The cleaned contents of one summary file. ``project`` None takes the
    file's own ``project.id`` (``khipu work check``). Raises ``Unreadable``
    for a file that can't be read as a whole; a bad item drops only itself.
    Unknown keys are ignored and never kept."""
    from khipu import routing

    try:
        doc = routing._loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Unreadable("unreadable", "The file is not readable JSON.") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise Unreadable("unreadable", "The file is not a Khipu work summary.")
    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise Unreadable("unreadable", "The file has no format version.")
    if version > FORMAT_VERSION:
        raise Unreadable("newer_format", f"Written in a newer format ({version}) than this Khipu reads "
                         f"({FORMAT_VERSION}). Update Khipu to see it.", version=version)
    head = doc.get("project") if isinstance(doc.get("project"), dict) else {}
    pid = head.get("id")
    if version < 1 or _parse_time(doc.get("written_at")) is None or not isinstance(pid, str) \
            or not _ID_RE.match(pid) or clean_text(head.get("name"), 80) is None:
        raise Unreadable("unreadable", "The file is missing its written_at time or its project.")
    if project is not None and pid not in (project, _project_slug(project)):
        raise Unreadable("wrong_project", f"This file says it is for {pid}, not {project}.")
    r = _Reading(pid, cfg, now)
    out: dict[str, Any] = {"written_at": r.time(doc.get("written_at")), "project": pid,
                           "name": clean_text(head.get("name"), 80)}
    runner = doc.get("runner") if isinstance(doc.get("runner"), dict) else {}
    interval = runner.get("interval_s")
    ok_interval = isinstance(interval, (int, float)) and not isinstance(interval, bool) and 0 < interval <= 86400
    out["runner"] = {"name": _label(runner.get("name")), "interval_s": interval if ok_interval else None}
    for key, (reader, newest, cap_key) in _READERS.items():
        raw = doc.get(key)
        items = []
        for item in raw if isinstance(raw, list) else []:
            try:
                if not isinstance(item, dict):
                    raise _Drop
                items.append(reader(item, r))
            except (_Drop, TypeError, ValueError, OverflowError):
                r.dropped += 1
        out[key] = _newest(items, newest, cfg[cap_key], r, key)
    _cap_runs(out["trains"], cfg, r)
    waiting = doc.get("waiting_on_owner")
    out["waiting_list_ignored"] = 0 if waiting is None else (len(waiting) if isinstance(waiting, list) else 1)
    out.update(dropped=r.dropped, other_project=r.other_project, clamped=r.clamped, capped=r.capped)
    return out


SOURCE_PROBLEMS = {
    "missing": "No summary file yet at the place you set.",
    "refused": "Not read: the file is inside a private or login folder, or is reached through a link.",
    "off": "Turned off",
    "none": "No job runner reports for this project",
    "unreadable": "Couldn't read the newest summary; showing what it said last.",
    "older": "The newest summary is older than the last one read; showing the last one.",
}


def _stale_after(cfg: dict, parsed: dict | None) -> float:
    """Seconds until a summary counts as out of date (0: never)."""
    if cfg["work_stale_after"] != "auto":
        return float(cfg["work_stale_after"])
    interval = ((parsed or {}).get("runner") or {}).get("interval_s")
    return max(_STALE_MIN, 5 * interval) if interval else _STALE_NO_INTERVAL


def _cfg_digest(cfg: dict) -> str:
    keys = ("work_max_jobs", "work_max_prs", "work_max_trains", "work_max_runs", "work_line_chars",
            "work_clock_slack")
    return hashlib.sha256(json.dumps([cfg[k] for k in keys]).encode()).hexdigest()[:16]


def _kb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"


def _failed(state: str, problem: str, last: dict | None, cfg: dict, **detail: Any) -> dict:
    """A read that failed: its state, and the last good read to show."""
    return {"state": state, "problem": problem, "contents": _within_caps((last or {}).get("contents"), cfg),
            **detail}


def _private_place(path: str) -> bool:
    return safe_read.private_place(path)


def _open_checked(path: str, limit: int) -> safe_read.Opened:
    """``safe_read.open_file``, with a file over ``limit`` as ``Unreadable``
    (``too_big``). Raises ``safe_read.Refused``, ``FileNotFoundError`` or
    ``OSError`` otherwise."""
    try:
        return safe_read.open_file(path, limit)
    except safe_read.TooBig as exc:
        raise Unreadable("too_big", f"The summary file is {_kb(exc.size)}, over the {_kb(limit)} limit.",
                         size=exc.size, limit=limit) from None


def read_source(entry: dict | None, cfg: dict, now: datetime) -> dict:
    """One project's summary source: its state, the contents to show (this
    read, or the last good read when this one failed) and the counts. A file
    whose time and size are unchanged since the last good read is not parsed
    again, and a file older than the last good read never replaces it."""
    if entry is None:
        return {"state": "none", "problem": SOURCE_PROBLEMS["none"], "contents": None}
    if not entry.get("on", True):
        return {"state": "off", "problem": SOURCE_PROBLEMS["off"], "contents": None}
    project, path = entry["project"], entry["file"]
    if _private_place(path):
        return {"state": "refused", "problem": SOURCE_PROBLEMS["refused"], "contents": None}
    # The last good read stands in only for the same file (its name as set,
    # kept as a hash): a source set to another file never shows this one's.
    name = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
    last = _read_json(_last_file(project))
    last = last if isinstance(last, dict) and last.get("project_key") == project \
        and isinstance(last.get("stamp"), list) and last["stamp"][:1] == [name] else None
    limit = cfg["work_file_max_bytes"]
    try:
        opened = _open_checked(path, limit)
    except safe_read.Refused:
        return {"state": "refused", "problem": SOURCE_PROBLEMS["refused"], "contents": None}
    except FileNotFoundError:
        return {"state": "missing", "problem": SOURCE_PROBLEMS["missing"], "contents": None}
    except Unreadable as exc:
        return _failed(exc.state, exc.problem, last, cfg, **exc.detail)
    except OSError:
        return _failed("unreadable", SOURCE_PROBLEMS["unreadable"], last, cfg)
    st = opened.st
    try:
        # Which file was read (its name as set, device and inode) as well as
        # its times and size: another file with the same time and size is
        # read afresh. The name is kept only as a hash.
        stamp = [name, st.st_dev, st.st_ino,
                 st.st_mtime_ns, st.st_ctime_ns, st.st_size, _cfg_digest(cfg)]
        if last is not None and last.get("stamp") == stamp:
            parsed = last["contents"]
        else:
            try:
                data = opened.read(limit or st.st_size or 1)
                opened.recheck()
            except safe_read.Refused:
                return {"state": "refused", "problem": SOURCE_PROBLEMS["refused"], "contents": None}
            except OSError:
                return _failed("unreadable", SOURCE_PROBLEMS["unreadable"], last, cfg)
            try:
                parsed = parse_summary(data, project, cfg, now)
            except Unreadable as exc:
                if exc.state in ("wrong_project", "newer_format"):
                    return {"state": exc.state, "problem": exc.problem, "contents": None, **exc.detail}
                return _failed("unreadable", SOURCE_PROBLEMS["unreadable"], last, cfg)
            before = _parse_time(((last or {}).get("contents") or {}).get("written_at"))
            if before is not None and _parse_time(parsed["written_at"]) < before:
                return _failed("unreadable", SOURCE_PROBLEMS["older"], last, cfg)
            try:
                _write_json(_last_file(project), {"project_key": project, "stamp": stamp, "contents": parsed})
            except OSError:
                pass  # the last good read is a convenience; this read still shows
    finally:
        opened.close()
    age = (now - _parse_time(parsed["written_at"])).total_seconds()
    stale = _stale_after(cfg, parsed)
    # Each limit is reached at its instant (>=), the instant build_view
    # hands on as valid_until: a refresh at that instant sees the change.
    if cfg["work_gone_after"] and age >= cfg["work_gone_after"]:
        state = "gone"
    elif stale and age >= stale:
        state = "stale"
    else:
        state = "ok"
    return {"state": state, "problem": None, "contents": parsed}


# ---- the ledger, commitments and routing (each fail-open) ---------------------------


def _ledger_projects(conn) -> list[dict]:
    """Every ledger project with its display name and hidden mark: from the
    hub, or from the offline copy when it is away."""
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT project, name, hidden FROM ledger_projects ORDER BY project")
            got = cur.fetchall()
        conn.rollback()
    else:
        import sqlite3

        from khipu.hub_snapshot import snapshot_path

        if not snapshot_path().is_file():
            return []
        con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
        try:
            got = con.execute("SELECT project, name, hidden FROM ledger_projects ORDER BY project").fetchall()
        except sqlite3.Error:
            got = []
        finally:
            con.close()
    return [{"project": p, "name": n or p, "hidden": bool(h)} for p, n, h in got]


def _entered(conn, project: str, rows: list[dict], history: list[dict] | None) -> dict[str, datetime]:
    """When each of ``rows`` last entered its current state, from its history.
    An entry that keeps the state (a note or an edit) is not an entering."""
    if not rows:
        return {}
    states = {r["row_id"]: r["state"] for r in rows}
    out: dict[str, datetime] = {}
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT row_id, to_state, max(at) FROM ledger_history WHERE project = %s "
                        "AND row_id = ANY(%s) AND from_state IS DISTINCT FROM to_state "
                        "GROUP BY row_id, to_state", (project, list(states)))
            found = cur.fetchall()
        conn.rollback()
    else:
        found = [(h.get("row_id"), h.get("to_state"), h.get("at")) for h in history or []
                 if h.get("from_state") != h.get("to_state")]
    for row_id, to_state, at in found:
        at = _parse_time(at)
        if row_id in states and states[row_id] == to_state and at is not None \
                and (row_id not in out or at > out[row_id]):
            out[row_id] = at
    return out


def read_ledger() -> dict[str, Any]:
    """Every ledger project's words, live rows, and when each needs-you row
    entered its state: {"projects": {key: {...}}, "offline", "as_of", "error"}."""
    from khipu import ledger

    out: dict[str, Any] = {"projects": {}, "offline": False, "as_of": None, "error": None}
    try:
        conn = ledger._connect()
    except ledger.HubAway:
        conn = None
        out["offline"] = True
    except Exception as exc:  # noqa: BLE001  (the view still shows what runners say)
        out["error"] = type(exc).__name__
        return out
    try:
        if conn is not None:
            try:
                ledger.drain(conn)
            except Exception:  # noqa: BLE001  (waiting writes stay waiting; doctor says so)
                conn.rollback()
        for proj in _ledger_projects(conn):
            key = proj["project"]
            history = None
            if conn is not None:
                view = ledger._hub_view(conn, key)
            else:
                offline = ledger._offline_view(key) or {"vocab": {"state": [], "kind": []}, "rows": [],
                                                        "as_of": None}
                view = ledger._overlay(offline, key)
                history = offline.get("history")
                out["as_of"] = view.get("as_of")
            needs = [r for r in view["rows"] if ledger._meaning(view["vocab"], r["state"]) == "needs"]
            out["projects"][key] = {"name": proj["name"], "hidden": proj["hidden"], "vocab": view["vocab"],
                                    "rows": view["rows"], "entered": _entered(conn, key, needs, history)}
    except Exception as exc:  # noqa: BLE001
        if not ledger._tables_missing(exc):
            out["error"] = type(exc).__name__
    finally:
        if conn is not None:
            conn.close()
    return out


def read_owed() -> dict[str, Any]:
    """Khipu's open commitments marked "needs the user", across every
    project, snoozed ones left out."""
    try:
        from khipu.hub_snapshot import try_hub_connect

        conn = try_hub_connect(connect_timeout=5)
    except Exception as exc:  # noqa: BLE001
        return {"items": [], "error": type(exc).__name__}
    try:
        from khipu.commitments import list_owed

        with conn.cursor() as cur:
            # No limit: the list is every open commitment, the user's picked after.
            rows = list_owed(cur, status="open", limit=None, hide_snoozed=True)
            # A snoozed one enters Waiting when its snooze ends: that end is
            # a deadline of the view (valid_until).
            shown = {r.get("id") for r in rows}
            ends = [_parse_time(r.get("due_after")) for r in list_owed(cur, status="open", limit=None)
                    if r.get("owner") == "user" and r.get("id") not in shown]
            ends = [t for t in ends if t is not None]
        return {"items": [r for r in rows if r.get("owner") == "user"], "error": None,
                "valid_until": min(ends) if ends else None}
    except Exception as exc:  # noqa: BLE001
        return {"items": [], "error": type(exc).__name__}
    finally:
        conn.close()


def read_routing(now: datetime) -> dict[str, Any]:
    """The routing record's accounts (the Home card's rows, no key or login
    folder), rules, holds, and the lanes ``routing_lane_map`` names."""
    from khipu import routing

    try:
        with routing.lock_budget(2.0):
            view = routing.read_view(now)
        facts = routing.plan_facts(view, now)
        accounts = []
        for entry in view.get("accounts") or []:
            item = routing._public_account(entry, now)
            item["plan"] = routing._public_fact(facts.get(entry["key"]))
            accounts.append(item)
        known = [{"key": e["key"], "label": e["label"]} for e in view.get("accounts") or []]
        lanes: dict[str, dict[str, dict]] = {}
        for runner, mapped in (routing.settings().get("routing_lane_map") or {}).items():
            for lane, target in mapped.items():
                key = routing._resolve_quiet(target, known)
                if key:
                    lanes.setdefault(runner, {})[lane] = {"id": routing.account_id(key),
                                                          "label": routing._label_of(key, known)}
        labels = {a["label"].casefold(): {"id": a["id"], "label": a["label"]} for a in accounts}
        return {"accounts": accounts, "rules": view.get("rules") or [], "holds": view.get("holds") or [],
                "lanes": lanes, "labels": labels, "valid_until": view.get("valid_until"), "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"accounts": [], "rules": [], "holds": [], "lanes": {}, "labels": {}, "error": type(exc).__name__}


# ---- links and hidden items (Khipu's own record, never the ledger) -------------------


def _links_file() -> Path:
    return state_dir() / "links.json"


def _hidden_file() -> Path:
    return state_dir() / "hidden.json"


def read_links() -> dict[str, list]:
    data = _read_json(_links_file())
    data = data if isinstance(data, dict) else {}
    return {k: [x for x in data.get(k) or [] if isinstance(x, list) and len(x) == 3] for k in ("same", "not_same")}


def set_link(project: str, row_id: str, commitment: str, same: bool) -> dict[str, list]:
    """Remember Matt's answer to "Looks like": these are (or are not) the
    same. The newest answer for a pair wins."""
    links = read_links()
    pair = [project, row_id, str(commitment)]
    for key in ("same", "not_same"):
        links[key] = [x for x in links[key] if x != pair]
    links["same" if same else "not_same"].append(pair)
    _write_json(_links_file(), links)
    return links


def _hide_mark(item: dict) -> str:
    return hashlib.sha256(f"{item.get('kind')}\x00{item.get('text')}".encode("utf-8", "replace")).hexdigest()[:16]


def read_hidden() -> dict[str, str]:
    data = _read_json(_hidden_file())
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def set_hidden(commitment: str, item: dict | None) -> dict[str, str]:
    """Hide one commitment until its words change (``item`` None unhides it)."""
    hidden = read_hidden()
    if item is None:
        hidden.pop(str(commitment), None)
    else:
        hidden[str(commitment)] = _hide_mark(item)
    _write_json(_hidden_file(), hidden)
    return hidden


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _alike(a: str, b: str) -> bool:
    return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


# ---- the read model -----------------------------------------------------------------

STATE_APP_WORDS = {"VERIFIED": "Verified", "MERGED-UNVERIFIED": "Merged, not verified",
                   "BUILT-UNMERGED": "Built, not merged", "IN FLIGHT": "In progress", "QUEUED": "Queued",
                   "NOT STARTED": "Not started", "NEEDS OWNER": "Needs you", "NEEDS MATT": "Needs you",
                   "DROPPED": "Dropped", "DONE": "Done"}
_MERGED_WORDS = frozenset({"MERGED-UNVERIFIED", "VERIFIED"})
TITLE_CHARS = 200


def state_words(word: str) -> str:
    return STATE_APP_WORDS.get(word, word)


def disagreement(pr: dict, row: dict, meaning: str | None) -> str | None:
    """The phrase a pull request shows when the runner and a ledger row that
    names it disagree. Khipu changes neither side."""
    said = state_words(row["state"]).lower()
    if pr["stage"] == "merged" and meaning == "open" and row["state"] not in _MERGED_WORDS:
        return f"The runner says this merged; the ledger still says {said}"
    if pr["stage"] == "closed" and meaning == "open":
        return f"The runner says this closed without merging; the ledger still says {said}"
    if row["state"] in _MERGED_WORDS and pr["stage_known"] and pr["stage"] not in ("merged", "closed"):
        return f"The ledger says {said}; the runner says {pr['stage_words'].lower()}"
    return None


def _when(row: dict) -> datetime:
    at = _parse_time(row.get("updated_at"))
    return at or datetime.min.replace(tzinfo=timezone.utc)


def _row_out(row: dict, vocab: dict) -> dict:
    from khipu import ledger

    return {"id": row["row_id"], "title": clean_text(row["title"], TITLE_CHARS), "kind": row["kind"],
            "parent": row["parent"] or None, "state": row["state"], "state_words": state_words(row["state"]),
            "meaning": ledger._meaning(vocab, row["state"]), "owner": clean_text(row["owner"], 80),
            "updated_at": _z(_parse_time(row.get("updated_at")))}


def _ledger_part(rows: list[dict], vocab: dict, now: datetime, times: list[datetime] | None = None) -> dict:
    """A board's ledger parts: counts by state, open goals with their
    children's counts, loose ends newest-changed first (at most 10), and the
    rows changed in the last 24 hours (in ledger order). Each time one of
    these changes on its own (a row leaves the last 24 hours, a loose end
    goes stale) is added to ``times``."""
    times = times if times is not None else []
    from khipu import ledger

    stale_after = ledger.settings()["ledger_stale_after"]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    ids = {r["row_id"] for r in rows}
    ordered = ledger._order(rows, vocab)
    goals = []
    for g in ordered:
        if g["kind"] != "goal" or ledger._meaning(vocab, g["state"]) not in ("open", "needs", None):
            continue
        children = [r for r in rows if r["parent"] == g["row_id"]]
        by_state: dict[str, int] = {}
        for c in children:
            by_state[c["state"]] = by_state.get(c["state"], 0) + 1
        goals.append({**_row_out(g, vocab), "children": len(children), "by_state": by_state,
                      "verified": by_state.get("VERIFIED", 0),
                      "done": sum(1 for c in children if ledger._meaning(vocab, c["state"]) == "done")})
    loose = sorted((r for r in rows if ledger.is_loose(r, vocab)), key=_when, reverse=True)
    loose_out = []
    for r in loose[:10]:
        item = _row_out(r, vocab)
        item["stale"] = bool(stale_after and (now - _when(r)).total_seconds() >= stale_after)
        if stale_after and not item["stale"]:
            times.append(_when(r) + timedelta(seconds=stale_after))
        item["parent_missing"] = bool(r["parent"]) and r["parent"] not in ids
        loose_out.append(item)
    recent = [_row_out(r, vocab) for r in ordered if (now - _when(r)).total_seconds() < 86400]
    times.extend(_when(r) + timedelta(seconds=86400) for r in ordered if (now - _when(r)).total_seconds() < 86400)
    # The project's own words and what each means, so an app can offer them
    # (Done on a Needs you row is this project's done word, not always DONE).
    return {"rows": len(rows), "by_state": counts, "goals": goals, "loose_ends": loose_out,
            "loose_end_count": len(loose), "changed_today": recent,
            "vocab": {"state": [list(w) for w in vocab["state"]], "kind": list(vocab["kind"])}}


def _account_ref(job: dict, runner: str | None, routing_info: dict) -> dict:
    """The account a job runs on: through ``routing_lane_map`` for its
    runner and lane, else its label when it names a known account; otherwise
    the label as given and ``matched: false``."""
    lane = job.get("lane")
    if runner and lane and lane in (routing_info.get("lanes") or {}).get(runner, {}):
        return {**routing_info["lanes"][runner][lane], "matched": True}
    label = job.get("account")
    if label and label.casefold() in (routing_info.get("labels") or {}):
        return {**routing_info["labels"][label.casefold()], "matched": True}
    return {"label": label, "matched": False}


def _waiting(ledger_data: dict, owed: dict, routing_info: dict, now: datetime) -> list[dict]:
    """Waiting on you: ledger needs-you rows, commitments that need the user,
    and routing items. A linked or exactly matching row and commitment show
    once (the row, with ``also_owed``); a near match is only suggested.
    Blocking first, then oldest first."""
    from khipu import ledger
    from khipu.commitments import content_hash

    links, hidden = read_links(), read_hidden()
    same = {(p, r): c for p, r, c in links["same"]}
    not_same = {(p, r, c) for p, r, c in links["not_same"]}
    items: list[dict] = []
    needs: list[tuple[str, dict, dict]] = []  # (project, row, item)
    for project, data in (ledger_data.get("projects") or {}).items():
        vocab, rows = data["vocab"], data["rows"]
        parents = {r["parent"] for r in rows if r["parent"] and ledger.is_loose(r, vocab)}
        for row in rows:
            if ledger._meaning(vocab, row["state"]) != "needs":
                continue
            since = (data.get("entered") or {}).get(row["row_id"]) or _parse_time(row.get("updated_at"))
            item = {"source": "ledger", "key": f"ledger:{project}#{row['row_id']}", "project": project,
                    "id": row["row_id"], "title": clean_text(row["title"], TITLE_CHARS), "kind": row["kind"],
                    "since": _z(since), "evidence": clean_text(row.get("evidence"), TITLE_CHARS),
                    "link": _clean_link(row.get("link")),
                    "blocking": row["row_id"] in parents, "also_owed": None,
                    "actions": ["done", "drop", "answered", "open_ledger"] + (["open_link"] if row.get("link") else [])}
            items.append(item)
            needs.append((project, row, item))
    explicit = {id(item): {x for x in (str((row.get("refs") or {}).get("commitment") or ""),
                                       same.get((p, row["row_id"])) or "") if x}
                for p, row, item in needs}
    for c in owed.get("items") or []:
        cid, project, text = str(c.get("id")), c.get("project") or None, c.get("text") or ""
        if hidden.get(cid) == _hide_mark(c):
            continue
        # An explicit link (the row's reference or a remembered "Same") wins;
        # equal words link only a row with no explicit link, never a pair
        # remembered as "Not the same".
        # A row shows one commitment: a second one linked to it shows on its own.
        linked = next((item for p, row, item in needs if p == project and cid in explicit[id(item)]
                       and item["also_owed"] in (None, cid)), None)
        if linked is None:
            linked = next((item for p, row, item in needs if p == project and not explicit[id(item)]
                           and item["also_owed"] is None and (p, row["row_id"], cid) not in not_same
                           and content_hash(project, row["title"]) == content_hash(project, text)), None)
        if linked is not None:
            linked["also_owed"] = cid
            linked["blocking"] = linked["blocking"] or c.get("kind") == "blocker"
            linked["actions"] = list(dict.fromkeys([*linked["actions"], "open_owed"]))
            continue
        looks_like = next(({"id": row["row_id"], "title": clean_text(row["title"], TITLE_CHARS)}
                           for p, row, _item in needs
                           if p == project and (p, row["row_id"], cid) not in not_same
                           and _alike(_norm(row["title"]), _norm(text))), None)
        opened = _parse_time(c.get("opened_at"))
        items.append({"source": "commitment", "key": f"commitment:{cid}", "project": project, "id": cid,
                      "title": clean_text(text, TITLE_CHARS), "kind": c.get("kind"), "since": _z(opened),
                      "blocking": c.get("kind") == "blocker", "looks_like": looks_like,
                      "actions": ["done", "snooze", "open_owed", "hide"] + (["same", "not_same"] if looks_like else [])})
    rules = routing_info.get("rules") or []
    for r in rules:
        if r.get("pending"):
            items.append({"source": "routing", "key": f"routing:rule:{r.get('n')}", "project": None,
                          "id": f"rule:{r.get('n')}", "title": "Confirm a standing rule a conversation changed",
                          "kind": "rule", "since": r.get("at"), "blocking": False, "actions": ["open_routing"]})
    seeded = [r for r in rules if r.get("seeded") and not r.get("confirmed") and not r.get("pending")]
    if seeded:
        items.append({"source": "routing", "key": "routing:seeded", "project": None, "id": "seeded",
                      "title": f"{len(seeded)} seeded rule{'s' if len(seeded) != 1 else ''} to confirm",
                      "kind": "rule", "since": min((r.get("at") or "" for r in seeded), default="") or None,
                      "blocking": False, "count": len(seeded), "actions": ["open_routing"]})
    for h in routing_info.get("holds") or []:
        if h.get("owner") and h.get("remind"):
            at = _parse_time(h.get("at"))
            days = int((now - at).total_seconds() // 86400) if at else 0
            items.append({"source": "routing", "key": f"routing:hold:{h.get('id')}", "project": None,
                          "id": f"hold:{h.get('id')}",
                          "title": f"Your hold on {clean_text(h.get('scope_label'), 60) or 'an account'} "
                                   f"has been on for {days} days",
                          "kind": "hold", "since": h.get("at"), "blocking": False, "actions": ["open_routing"]})
    items.sort(key=lambda i: (not i["blocking"], i.get("since") or "9999"))
    return items


def build_view(now: datetime | None = None, *, cfg: dict | None = None, ledger_data: dict | None = None,
               owed: dict | None = None, routing_info: dict | None = None) -> dict:
    """The ``khipu-work-view`` document. Each input can be given (tests);
    otherwise it is read, and an input that can't be read costs its own
    part, never the view."""
    from khipu import ledger

    now = now or _now()
    cfg = cfg or settings()
    ledger_data = ledger_data if ledger_data is not None else read_ledger()
    owed = owed if owed is not None else read_owed()
    routing_info = routing_info if routing_info is not None else read_routing(now)

    registered = {e["project"]: e for e in cfg["work_projects"]}
    ledgers = ledger_data.get("projects") or {}
    order = list(registered) + [k for k in sorted(ledgers) if k not in registered and not ledgers[k].get("hidden")]
    projects, live, times = [], [], []
    for key in order:
        entry, ldata = registered.get(key), ledgers.get(key)
        source = read_source(entry, cfg, now)
        contents = source.pop("contents") or {}
        board: dict[str, Any] = {
            "project": key, "name": clean_text((entry or {}).get("name"), 80)
            or clean_text((ldata or {}).get("name"), 80) or key,
            "source": {"state": source["state"], "problem": source.get("problem"),
                       "as_of": contents.get("written_at"),
                       "dropped": contents.get("dropped", 0),
                       "other_project": contents.get("other_project", 0), "capped": contents.get("capped", {}),
                       "clamped": contents.get("clamped", 0),
                       "waiting_list_ignored": contents.get("waiting_list_ignored", 0),
                       **{k: source[k] for k in ("size", "limit", "version") if k in source}},
            "jobs": [], "prs": [], "trains": [], "merged": [], "ledger": None,
        }
        rows = (ldata or {}).get("rows") or []
        vocab = (ldata or {}).get("vocab") or {"state": [], "kind": []}
        if ldata is not None:
            board["ledger"] = _ledger_part(rows, vocab, now, times)
        if contents:
            written = _parse_time(contents["written_at"])
            if source["state"] in ("ok", "stale") and _stale_after(cfg, contents):
                times.append(written + timedelta(seconds=_stale_after(cfg, contents)))
            # A failed read shows the last good one: its jobs stop showing
            # live once that read is older than work_gone_after.
            gone = bool(cfg["work_gone_after"]) and (now - written).total_seconds() >= cfg["work_gone_after"]
            if cfg["work_gone_after"] and not gone:
                times.append(written + timedelta(seconds=cfg["work_gone_after"]))
            runner = (contents.get("runner") or {}).get("name")
            for raw_job in contents.get("jobs") or []:
                job = dict(raw_job, project=key)
                if not (entry or {}).get("show_now", True):
                    job["now"] = []
                updated = _parse_time(job["updated_at"])
                if job["state"] in JOB_ENDED and cfg["work_finished_for"]:
                    ended = _parse_time(job.get("ended_at")) or updated
                    if (now - ended).total_seconds() >= cfg["work_finished_for"]:
                        continue
                    times.append(ended + timedelta(seconds=cfg["work_finished_for"]))
                quiet = cfg["work_job_quiet_after"]
                job["quiet"] = bool(job["state"] == "running" and quiet and (now - updated).total_seconds() >= quiet)
                if job["state"] == "running" and quiet and not job["quiet"]:
                    times.append(updated + timedelta(seconds=quiet))
                job["account_ref"] = _account_ref(job, runner, routing_info)
                board["jobs"].append(job)
                if job["state"] in ("running", "queued", "waiting") and source["state"] != "gone" and not gone:
                    live.append(job)
            by_pr: dict[str, list[dict]] = {}
            for r in rows:
                ref = str((r.get("refs") or {}).get("pr") or "")
                if ref:
                    by_pr.setdefault(ref, []).append(r)
            for raw_pr in contents.get("prs") or []:
                pr = dict(raw_pr)
                if pr["stage"] in ("merged", "closed") and cfg["work_merged_for"]:
                    done_at = _parse_time(pr.get("merged_at") or pr["stage_at"])
                    if (now - done_at).total_seconds() >= cfg["work_merged_for"]:
                        continue
                    times.append(done_at + timedelta(seconds=cfg["work_merged_for"]))
                pr["ledger"] = [{"id": r["row_id"], "state": r["state"], "state_words": state_words(r["state"]),
                                 "disagrees": disagreement(pr, r, ledger._meaning(vocab, r["state"]))}
                                for r in by_pr.get(pr["id"], [])]
                pr["disagrees"] = next((x["disagrees"] for x in pr["ledger"] if x["disagrees"]), None)
                board["prs"].append(pr)
                if pr["stage"] == "merged":
                    board["merged"].append({"id": pr["id"], "title": pr["title"], "merged_at": pr["merged_at"],
                                            "link": pr["link"]})
            for train in contents.get("trains") or []:
                if train["state"] in ("shipped", "dropped") and cfg["work_merged_for"]:
                    at = _parse_time(train["updated_at"])
                    if (now - at).total_seconds() >= cfg["work_merged_for"]:
                        continue
                    times.append(at + timedelta(seconds=cfg["work_merged_for"]))
                board["trains"].append(train)
        board["flagged"] = sum(1 for p in board["prs"]
                               if p["stage"] == "flagged" or (p.get("verdict") or {}).get("outcome") == "flagged")
        projects.append(board)

    waiting = _waiting(ledger_data, owed, routing_info, now)
    try:
        outbox = int(ledger.outbox_status().get("pending") or 0)
    except Exception:  # noqa: BLE001
        outbox = 0
    # The earliest deadline of every input: the routing record's (a hold
    # reminder entering Waiting on you) and any the ledger or commitments give.
    times.extend(_parse_time(part.get("valid_until")) for part in (ledger_data, owed, routing_info))
    future = [t for t in times if t is not None and t > now]
    scan = _Scan()
    view = scan({
        "format": VIEW_FORMAT, "version": VIEW_VERSION, "generated_at": _z(now),
        "valid_until": _z_up(min(future)) if future else None,
        "projects": projects, "live": live, "waiting": waiting, "accounts": routing_info.get("accounts") or [],
        "counts": {"waiting": len(waiting), "blocking": sum(1 for w in waiting if w["blocking"]),
                   "running": sum(1 for j in live if j["state"] == "running"),
                   "flagged": sum(p["flagged"] for p in projects),
                   "stale_sources": sum(1 for p in projects if p["source"]["state"] not in ("ok", "none", "off")),
                   "ledger_outbox": outbox},
        "problems": {k: v for k, v in (("ledger", ledger_data.get("error")), ("owed", owed.get("error")),
                                       ("routing", routing_info.get("error"))) if v},
        "ledger_offline": bool(ledger_data.get("offline")), "ledger_as_of": ledger_data.get("as_of"),
        "refresh_s": cfg["work_refresh"],
    })
    if scan.degraded:
        view["problems"]["privacy"] = PRIVACY_INCOMPLETE
    return view


def write_view(view: dict) -> None:
    from khipu import agent_view

    try:
        _write_json(view_file(), agent_view.project(view))
    except OSError:
        pass  # the saved copy is for a later reader; the answer stands without it


# ---- doctor -------------------------------------------------------------------------


def doctor_block(now: datetime | None = None) -> dict[str, Any]:
    """Each summary source's state, last good read, dropped, capped and
    clamped counts, and any waiting list a file still carries. Warns only;
    never gates ``ok``."""
    now = now or _now()
    cfg = settings()
    out: dict[str, Any] = {"sources": [], "warnings": []}
    for entry in cfg["work_projects"]:
        source = read_source(entry, cfg, now)
        contents = source.pop("contents") or {}
        last = _read_json(_last_file(entry["project"]))
        last_good = ((last.get("contents") or {}).get("written_at")) if isinstance(last, dict) else None
        item = {"project": entry["project"], "state": source["state"], "problem": source.get("problem"),
                "as_of": contents.get("written_at"), "last_good": last_good,
                "dropped": contents.get("dropped", 0),
                "capped": contents.get("capped", {}), "clamped": contents.get("clamped", 0),
                "waiting_list_ignored": contents.get("waiting_list_ignored", 0)}
        out["sources"].append(item)
        if source["state"] not in ("ok", "off"):
            out["warnings"].append(f"work: {entry['project']}: {source.get('problem') or source['state']}")
        if item["waiting_list_ignored"]:
            out["warnings"].append(f"work: {entry['project']}: this file has a waiting list Khipu no longer reads; "
                                   "add those as ledger rows")
        if item["clamped"]:
            out["warnings"].append(f"work: {entry['project']}: {item['clamped']} time(s) ahead of this Mac's clock "
                                   "shown as now")
    scan = _Scan()
    out = scan(out)
    if scan.degraded:
        out["warnings"].append(f"work: {PRIVACY_INCOMPLETE}")
    return out


# ---- the CLI ------------------------------------------------------------------------


def check_file(path: str) -> tuple[bool, list[str], dict | None]:
    """For runner authors: whether ``path`` reads as a format 1 summary, and
    what Khipu would leave out. Read-only, like every read here."""
    from khipu import routing_sources

    cfg = settings()
    limit = cfg["work_file_max_bytes"]
    try:
        opened = _open_checked(path, limit)
    except safe_read.Refused:
        return False, [SOURCE_PROBLEMS["refused"]], None
    except FileNotFoundError:
        return False, ["No file at that path."], None
    except Unreadable as exc:
        return False, [exc.problem], None
    except OSError as exc:
        return False, [f"The file can't be read ({routing_sources._short_error(exc)})."], None
    try:
        data = opened.read(limit or opened.st.st_size or 1)
        opened.recheck()
    except safe_read.Refused:
        return False, [SOURCE_PROBLEMS["refused"]], None
    except OSError as exc:
        return False, [f"The file can't be read ({routing_sources._short_error(exc)})."], None
    finally:
        opened.close()
    try:
        parsed = parse_summary(data, None, cfg, _now())
    except Unreadable as exc:
        return False, [exc.problem], None
    problems = []
    if parsed["dropped"] or parsed["other_project"]:
        problems.append(f"{parsed['dropped']} item(s) couldn't be read and would be left out "
                        "(a missing required field, a bad id or time, or another project's item).")
    for what, cap in parsed["capped"].items():
        problems.append(f"Only the newest {cap['shown']} of {cap['of']} {what} would be shown.")
    if parsed["clamped"]:
        problems.append(f"{parsed['clamped']} time(s) are ahead of this Mac's clock.")
    if parsed["waiting_list_ignored"]:
        problems.append("waiting_on_owner is no longer read; add each need as a ledger row "
                        "(khipu ledger add ... --kind needs-owner).")
    unknown = [f"{key} {i['id']}" for key in _TOP_LISTS for i in parsed[key]
               if not i.get("state_known", i.get("stage_known", True))]
    if unknown:
        problems.append("State not known to this Khipu: " + ", ".join(unknown[:10]))
    return not problems, problems, parsed


WITHHELD_WORDS = "(left out: it held a path or a secret)"


def render_text(view: dict) -> str:
    c = view["counts"]
    lines = [f"Waiting on you: {c['waiting']} ({c['blocking']} blocking)"]
    for w in view["waiting"][:8]:
        lines.append(f"  - {'[blocking] ' if w['blocking'] else ''}{w['title'] or WITHHELD_WORDS}"
                     + (f" ({w['project']})" if w.get("project") else ""))
    lines.append(f"Running now: {c['running']}")
    for j in view["live"]:
        acct = (j.get("account_ref") or {}).get("label") or "an account not named"
        line = f"  - {j['project'] or WITHHELD_WORDS}: {j['summary'] or WITHHELD_WORDS} · {j['state_words']} · {acct}"
        if j.get("now"):
            line += f" · {j['now'][-1]['text']}"
        lines.append(line)
    for p in view["projects"]:
        src = p["source"]
        lines.append(f"{p['name'] or WITHHELD_WORDS}: {src['problem'] or 'reporting'}"
                     + (f"; {len(p['prs'])} pull request(s), {p['flagged']} flagged" if p["prs"] else ""))
        if p["ledger"]:
            lines.append(f"  ledger: {p['ledger']['rows']} row(s), {p['ledger']['loose_end_count']} loose end(s)")
    return "\n".join(lines)


def _emit(payload: Any = None, text: str | None = None, *, as_json: bool = True, indent: int | None = 2) -> None:
    """Every answer of ``khipu work`` (``agent_view.emit``: projected first)."""
    from khipu import agent_view

    agent_view.emit(payload, text, as_json=as_json, indent=indent)


def cmd(args: argparse.Namespace) -> int:
    from khipu import ledger

    sub = getattr(args, "work_cmd", None)
    try:
        ledger.require(f"work {sub}" if sub else "work")
    except ledger.LedgerError as exc:
        _emit({"ok": False, "error": str(exc), "reason": exc.reason}, indent=None)
        return 1
    if sub == "check":
        ok, problems, parsed = check_file(args.file)
        if args.json:
            _emit({"ok": ok, "problems": problems,
                   "counts": {k: len(parsed[k]) for k in _TOP_LISTS} if parsed else None})
        else:
            lines = []
            if parsed:
                lines.append(f"Read {len(parsed['jobs'])} job(s), {len(parsed['prs'])} pull request(s) and "
                             f"{len(parsed['trains'])} release train(s) for {parsed['project']}.")
            lines.append("OK: Khipu reads this file as it is." if ok else "\n".join(f"- {p}" for p in problems))
            _emit(text="\n".join(lines), as_json=False)
        return 0 if ok else 1
    if sub in ("hide", "unhide"):
        item = None
        if sub == "hide":
            owed = {str(c.get("id")): c for c in read_owed()["items"]}
            if args.id not in owed:
                _emit({"ok": False, "error": f"no open commitment {args.id} needs you"}, indent=None)
                return 1
            item = owed[args.id]
        set_hidden(args.id, item)
        _emit({"ok": True, "hidden": sorted(read_hidden())}, indent=None)
        return 0
    if sub in ("same", "not-same"):
        project, _, row_id = args.row.partition("#")
        if not project or not row_id:
            _emit({"ok": False, "error": "name the ledger row as <project>#<row id>"}, indent=None)
            return 1
        set_link(project, row_id, args.commitment, sub == "same")
        _emit({"ok": True}, indent=None)
        return 0
    view = build_view()
    write_view(view)
    _emit(view, None if args.json else render_text(view), as_json=bool(args.json))
    return 0


def add_parser(sub) -> None:
    """``khipu work``: the live view (read-only), ``check`` for runner
    authors, and the app's hide and "Looks like" answers."""
    p = sub.add_parser("work", help="What is done, what is running, and what is waiting on you")
    p.add_argument("--json", action="store_true")
    verbs = p.add_subparsers(dest="work_cmd")
    c = verbs.add_parser("check", help="Check a work-summary.json the way Khipu reads it")
    c.add_argument("file")
    c.add_argument("--json", action="store_true")
    for name, words in (("hide", "Hide a commitment from Waiting on you until it changes"),
                        ("unhide", "Show a hidden commitment again")):
        h = verbs.add_parser(name, help=words)
        h.add_argument("id")
    for name in ("same", "not-same"):
        s = verbs.add_parser(name, help="Answer 'Looks like': a ledger row and a commitment are (not) the same")
        s.add_argument("row", help="<project>#<row id>")
        s.add_argument("commitment")
    p.set_defaults(func=cmd)
