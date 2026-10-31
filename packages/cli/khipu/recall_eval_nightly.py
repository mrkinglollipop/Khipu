"""Recall accuracy as a nightly measurement: the ``recall_eval`` nightly step,
the doctor row that reads it, and ``khipu recall eval --install``.

The step scores the maintainer-local golden set (``recall_eval``) on every
path against this Mac's replica only, with every hub, embedding, network and
subprocess path refused (``_offline``): no golden query leaves the machine,
the run adds no hub load and cannot be slowed by an outage. Entries whose
expected ids are now forgotten, erased or deleted are skipped, never counted
as misses (a forgotten episode that no longer answers is the forget working).

Scoring runs in a spawned child process with the guard installed inside it,
and the child is killed at the deadline: a lookup thread that outlives the
deadline dies with the child, so it can never reach a real exit after the
guard would have come off, and the step returns by deadline + KILL_GRACE_S.

The push's cosine leg needs a query vector, and embedding is refused in the
sandbox. The child is therefore handed the golden queries' vectors from a local
0600 store (``vectors_path``), keyed like the push's own query cache
(``recall_prompt._query_embed_cache_key``, active profile). Only the owner-run
``khipu recall eval --refresh-vectors`` fills it, outside the sandbox through
the normal query-embedding client; the nightly never does. A query with no
stored vector stays a keyword-only lookup and is counted as ``cosine_missing``;
the doctor row says whether tonight's figure was measured with semantic search.

The OS sandbox is the guard. The child is a plain subprocess started with
``close_fds=True`` under ``/usr/bin/sandbox-exec`` with ``(deny network*)``;
the work reaches it as JSON on stdin and only JSON lines on stdout come back,
so it holds no descriptor of the parent's (a connected socket included). The
child's stderr is read by the parent and reduced to a line count and exception
type names. When sandbox-exec is missing or its probe fails, the step does not
run: the record says ``skipped: no OS sandbox`` and the doctor row is yellow.
The Python-level patches of every exit (``GUARDED_EXITS``) and the refusal to
import ``ctypes``/``cffi`` (``NATIVE_MODULES``) stay as belt and braces, but
they are NOT the guarantee: a reference taken before they were installed
(a ctypes module, a pre-connected socket written with ``os.write``) walks past
them, and only the sandbox stops it.

``<data dir>/state/recall-eval-history.json`` keeps the last 30 run records:
figures, counts and the golden file's size and mtime. It never holds a query
or an id, so it is not owner text and forget has nothing to scan in it.
A record's ``paths`` holds figures only for the paths the replica can answer
(``MEASURED_PATHS``). ``not_measured`` counts lookups left out of every
figure, by path and reason: ``needs_hub`` (the search-tool path, which only
the hub answers, is never run) and ``hub_fallback`` (a replica lookup that
reached for the hub, was refused and failed). Neither is a found/missed
result or an error, so the doctor's hit rate and failure share never count
them.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from khipu import recall_eval
from khipu.lines import split_lines

HISTORY_KEEP = 30
BUDGET_S = 300
MIN_ENTRIES = 20
STALE_AFTER_H = 48
ALLOWED_DROP = 0.05
KILL_GRACE_S = 1.0
FAILED_SHARE = 0.25
HIT_FLOOR = 0.6
# The search-tool path (hybrid_search) only reads the hub; offline it can only
# fail, so the nightly never runs it and counts it under ``needs_hub``.
MEASURED_PATHS = tuple(p for p in recall_eval.ALL_PATHS if p != recall_eval.PATH_EXPLICIT)
_ROW_FIELDS = ("expect_none", "hit", "reciprocal_rank", "abstain_correct", "stale_violation",
               "timeout", "error", "degraded", "outcome", "ms", "payload_chars")
GOLDEN_TEMP_PREFIX = ".recall-golden."
BACKUP_NAME = ".recall-golden.bak.tmp"
_KEPT_METRICS = ("total", "hits", "hit_rate", "mrr", "abstain_total", "abstain_correct",
                 "stale_violations", "timeouts", "errors")


def history_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / "recall-eval-history.json"


def read_history() -> list[dict[str, Any]]:
    try:
        data = json.loads(history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("records") if isinstance(data, dict) else None
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


def _write_history(records: list[dict[str, Any]]) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".recall-eval-history.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps({"records": records[-HISTORY_KEEP:]}, indent=2))  # text-safe: run records hold figures and counts only, never a query
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _Offline:
    """What the offline guard refused during one run, by kind. Counts only."""

    def __init__(self) -> None:
        self.refused = {"hub": 0, "embed": 0, "network": 0, "process": 0, "native": 0,
                        "cosine_missing": 0}


# Foreign-function modules that reach libc directly, past every patch below.
NATIVE_MODULES = ("ctypes", "_ctypes", "cffi", "_cffi_backend")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)"


class _NativeBlocker:
    """Meta-path finder that refuses the foreign-function modules and counts
    each refusal."""

    def __init__(self, guard: _Offline) -> None:
        self.guard = guard

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in NATIVE_MODULES:
            self.guard.refused["native"] += 1
            raise ImportError(f"{name} refused: the recall eval runs offline")
        return None


# Every exit that can leave this process, as (object path, attribute, kind).
# The offline guard patches each one for the whole scoring run, and the test
# iterates this list, so a new exit added later has to be listed to be covered.
# UDP sends, name lookups, spawned processes and forks are all here because a
# lookup that cannot connect over TCP can still leak a golden query by DNS or
# by a helper process that makes its own connection.
GUARDED_EXITS: tuple[tuple[str, str, str], ...] = (
    ("socket.socket", "connect", "network"), ("socket.socket", "connect_ex", "network"),
    ("socket.socket", "send", "network"), ("socket.socket", "sendall", "network"),
    ("socket.socket", "sendto", "network"), ("socket.socket", "sendmsg", "network"),
    ("socket", "create_connection", "network"), ("socket", "getaddrinfo", "network"),
    ("socket", "gethostbyname", "network"), ("socket", "gethostbyname_ex", "network"),
    ("subprocess", "Popen", "process"),
    ("os", "posix_spawn", "process"), ("os", "posix_spawnp", "process"),
    ("os", "fork", "process"), ("os", "forkpty", "process"), ("os", "system", "process"),
    ("os", "execv", "process"), ("os", "execve", "process"), ("os", "execvp", "process"),
    ("os", "execvpe", "process"), ("os", "execl", "process"), ("os", "execle", "process"),
    ("os", "execlp", "process"), ("os", "execlpe", "process"),
    ("os", "spawnv", "process"), ("os", "spawnve", "process"), ("os", "spawnvp", "process"),
    ("os", "spawnvpe", "process"), ("os", "spawnl", "process"), ("os", "spawnle", "process"),
    ("os", "spawnlp", "process"), ("os", "spawnlpe", "process"),
    ("multiprocessing.process.BaseProcess", "start", "process"),
)


def _resolve(path: str) -> Any:
    import importlib

    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(path)


@contextlib.contextmanager
def _offline(vectors: dict[str, list[float]] | None = None):
    """Every way off this Mac refuses for the run, so retrieval answers from
    the replica alone and no golden query leaves the machine: hub connects
    (``try_hub_connect``, ``db.connect`` and ``psycopg.connect`` underneath
    both), embedding calls (the cosine leg is skipped and counted, unless the
    local query-vector cache already holds the vector), every socket connect
    and name lookup (any provider or HTTP client), and subprocess spawns (a
    helper process would sit outside these patches). The hub refusal reads as
    an unreachable hub, so callers take their replica fallback; the
    reachability memo the live hooks read is never written."""
    from khipu import db, embed, hub_reach, hub_snapshot

    guard = _Offline()
    try:
        import psycopg

        hub_exc: type[Exception] = psycopg.OperationalError
    except ImportError:  # pragma: no cover - psycopg ships with the CLI
        psycopg = None
        hub_exc = ConnectionError

    def refuser(kind: str, exc_type: type[Exception], message: str):
        def refuse(*_a: Any, **_k: Any):
            guard.refused[kind] += 1
            raise exc_type(message)
        return refuse

    hub = refuser("hub", hub_exc, "connection refused: the recall eval reads the local replica only")
    no_embed = refuser("embed", RuntimeError, "embedding refused: the recall eval runs offline")
    no_net = refuser("network", ConnectionRefusedError, "network refused: the recall eval runs offline")
    no_proc = refuser("process", PermissionError, "subprocess refused: the recall eval runs offline")
    by_kind = {"network": no_net, "process": no_proc, "embed": no_embed, "hub": hub}
    targets: list[tuple[Any, str, Any]] = [
        (hub_snapshot, "try_hub_connect", hub), (db, "connect", hub),
        (embed, "embed_one", no_embed), (embed, "embed_batch", no_embed),
        (embed, "embed_batch_images", no_embed), (embed, "_post_json", no_embed),
        (hub_reach, "note_unreachable", lambda *_a, **_k: None),
        (hub_reach, "note_reachable", lambda *_a, **_k: None),
    ]
    for obj_path, name, kind in GUARDED_EXITS:
        targets.append((_resolve(obj_path), name, by_kind[kind]))
    if psycopg is not None:
        targets.append((psycopg, "connect", hub))
    if vectors is not None:
        from khipu import recall_prompt
        from khipu.redact import redact_secrets

        def stored_query_embed(prompt: str, profile: str, deadline: float | None = None) -> list[float]:
            prompt, _ = redact_secrets(prompt)
            vec = vectors.get(recall_prompt._query_embed_cache_key(profile, prompt))
            if vec:
                return vec
            guard.refused["cosine_missing"] += 1
            raise RuntimeError("no stored vector: the recall eval runs offline")

        targets.append((recall_prompt, "_cached_query_embed", stored_query_embed))
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in targets if hasattr(obj, name)]
    for obj, name, fake in targets:
        if hasattr(obj, name):
            setattr(obj, name, fake)
    # Already-imported foreign-function modules come out of sys.modules so a
    # re-import reaches the finder; the originals go back on exit.
    purged = {m: sys.modules.pop(m) for m in list(sys.modules)
              if m.split(".")[0] in NATIVE_MODULES}
    blocker = _NativeBlocker(guard)
    sys.meta_path.insert(0, blocker)
    try:
        yield guard
    finally:
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(blocker)
        sys.modules.update(purged)
        for obj, name, real in saved:
            setattr(obj, name, real)


def _row_exists(con, sql: str, arg: Any) -> tuple[bool, Any] | None:
    """(found, first column) for a one-row lookup; None when the replica has
    no such table (an old replica: nothing to judge)."""
    try:
        row = con.execute(sql, (arg,)).fetchone()
    except Exception:  # noqa: BLE001 - a missing table or column
        return None
    return (row is not None, row[0] if row else None)


def _unavailable_ids(ids: set[str], con) -> set[str]:
    """Ids that no longer answer: forgotten, erased or deleted episodes (the
    replica's episodes view already hides all three), and topic slugs or
    graph node ids the replica no longer holds or marks deleted. Library
    chunk ids are not in the replica and are never judged."""
    gone: set[str] = set()
    try:
        from khipu import tombstones

        forgotten = {str(i) for i in tombstones.forgotten_episode_ids()}
    except Exception:  # noqa: BLE001 - the replica view still covers forgotten rows
        forgotten = set()
    for ident in ids:
        if ident in forgotten:
            gone.add(ident)
        elif ident.isdigit():
            if con.execute("SELECT 1 FROM episodes WHERE id = ?", (int(ident),)).fetchone() is None:
                gone.add(ident)
        elif not ident.startswith("library:"):
            topic = _row_exists(con, "SELECT deleted_at FROM topics WHERE slug = ?", ident)
            if topic is not None and topic[0]:
                if topic[1] is not None:
                    gone.add(ident)
                continue
            node = _row_exists(con, "SELECT 1 FROM nodes WHERE id = ?", ident)
            if node is not None and node[0]:
                continue
            if topic is not None or node is not None:
                gone.add(ident)
    return gone


_ID_TYPES = (str, int)


def entry_problem(entry: Any) -> str | None:
    """Why a golden entry cannot be scored, as a short reason code (never the
    query), or None when its shape and types are sound."""
    if not isinstance(entry, dict):
        return "not_an_object"
    query = entry.get("query")
    if not isinstance(query, str) or not query.strip():
        return "bad_query"
    expect, expect_none = entry.get("expect"), entry.get("expect_none")
    if expect_none not in (None, True, False):
        return "bad_expect_none"
    if expect is not None and (not isinstance(expect, list) or any(
            isinstance(x, bool) or not isinstance(x, _ID_TYPES) for x in expect)):
        return "bad_expect"
    if bool(expect) == bool(expect_none):
        return "bad_expect"
    k = entry.get("k")
    if k is not None and (isinstance(k, bool) or not isinstance(k, int) or k < 1):
        return "bad_k"
    if entry.get("mode") is not None and entry["mode"] not in ("hybrid", "literal", "semantic"):
        return "bad_mode"
    paths = entry.get("paths")
    if paths is not None and (not isinstance(paths, list)
                              or any(p not in recall_eval.ALL_PATHS for p in paths)):
        return "bad_paths"
    stale = entry.get("stale")
    if stale is not None and (not isinstance(stale, list) or any(
            isinstance(x, bool) or not isinstance(x, _ID_TYPES) for x in stale)):
        return "bad_stale"
    for key in ("project", "cwd", "since", "until", "note", "source"):
        if entry.get(key) is not None and not isinstance(entry[key], str):
            return f"bad_{key}"
    n = entry.get("n")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int)):
        return "bad_n"
    return None


def _load_entries(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The golden file's sound entries, and a count of malformed lines by
    reason. One bad line is skipped, never fatal (``load_golden`` raises)."""
    entries: list[dict[str, Any]] = []
    malformed: dict[str, int] = {}
    for raw in split_lines(path.read_text(encoding="utf-8")):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            problem: str | None = "bad_json"
        else:
            problem = entry_problem(entry)
        if problem:
            malformed[problem] = malformed.get(problem, 0) + 1
        else:
            entries.append(entry)
    return entries, malformed


def _golden_stat(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {"bytes": None, "mtime": None}
    return {"bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}


def _completed(rec: dict[str, Any]) -> bool:
    return (bool(rec.get("ok", True)) and not rec.get("truncated") and not rec.get("skipped")
            and bool(rec.get("paths")))


def _previous_figures(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last completed run's figures: a stopped or failed run is no baseline."""
    for rec in reversed(history):
        if _completed(rec) and isinstance(rec.get("paths"), dict):
            return {"ts": rec.get("ts"), "paths": {
                p: {"hit_rate": m.get("hit_rate"), "mrr": m.get("mrr")}
                for p, m in rec["paths"].items() if isinstance(m, dict)}}
    return None


def _score_entry(entry: dict[str, Any], deadline: float,
                 guard: _Offline) -> list[tuple[str, dict[str, Any] | None]] | None:
    """Every measured path's row for one entry, trimmed to the summary fields
    (no query, no id); a row is None when that lookup reached for the hub,
    was refused and failed (not measured). None overall when the deadline
    stopped the entry (its partial rows are dropped, not counted)."""
    rows: list[tuple[str, dict[str, Any] | None]] = []
    for p in MEASURED_PATHS:
        if recall_eval._entry_applies(entry, p):
            hub_before = guard.refused["hub"]
            row = recall_eval.eval_one_path(entry, p, deadline=deadline)
            if row.get("reason") == recall_eval.DEADLINE_REASON:
                return None
            if guard.refused["hub"] > hub_before and (row.get("error") or row.get("timeout")):
                rows.append((p, None))
                continue
            rows.append((p, {k: row.get(k) for k in _ROW_FIELDS}))
    return rows


def _quiet_excepthooks() -> None:
    """The child's stderr is the nightly's local log. A default traceback from
    a stray scorer thread would print exception args, and those can hold a
    golden query: write only the exception type name and a running count."""
    count = [0]

    def note(exc_type) -> None:
        count[0] += 1
        try:
            sys.stderr.write(f"[khipu-recall-eval] uncaught {exc_type.__name__} "
                             f"(#{count[0]}) in scoring child\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - a closed stderr must not change the run
            pass

    threading.excepthook = lambda args: note(args.exc_type)
    sys.excepthook = lambda exc_type, _value, _tb: note(exc_type)


_SANDBOX_PROBE = (
    "import socket\n"
    "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
    "try:\n"
    "    s.sendto(b'x', ('127.0.0.1', 9))\n"
    "except OSError:\n"
    "    print('blocked')\n"
)
_sandbox_cache: dict[str, bool] = {}
SKIPPED_NO_SANDBOX = "no OS sandbox"


def _sandbox_available() -> bool:
    """True when sandbox-exec exists and a probe under it both starts Python
    and finds a loopback UDP send refused. Without it the step does not run."""
    if "ok" not in _sandbox_cache:
        ok = False
        if os.path.exists(SANDBOX_EXEC):
            try:
                out = subprocess.run(
                    [SANDBOX_EXEC, "-p", SANDBOX_PROFILE, sys.executable, "-I", "-c", _SANDBOX_PROBE],
                    capture_output=True, text=True, timeout=30, close_fds=True)
                ok = out.returncode == 0 and out.stdout.strip() == "blocked"
            except (OSError, subprocess.SubprocessError):
                pass
        _sandbox_cache["ok"] = ok
    return _sandbox_cache["ok"]


_CHILD_BOOT = ("import sys, json; sys.path[:0] = json.loads(sys.argv[1]); "
               "from khipu.recall_eval_nightly import _child_entry; _child_entry()")


def _child_command() -> list[str]:
    return [SANDBOX_EXEC, "-p", SANDBOX_PROFILE, sys.executable, "-c", _CHILD_BOOT,
            json.dumps([p for p in sys.path if isinstance(p, str)])]


def _setup_ref(setup: tuple[Callable[..., Any], tuple] | None) -> list[Any] | None:
    """A test seam travels as (module, qualified name, JSON args): the child
    imports it by name, since nothing but the stdin JSON reaches it."""
    if setup is None:
        return None
    fn, args = setup
    return [fn.__module__, fn.__qualname__, list(args)]


VECTORS_NAME = "recall-eval-vectors.sqlite3"


def vectors_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / VECTORS_NAME


def _read_vectors(path: Any) -> dict[str, list[float]]:
    """The stored golden-query vectors by cache key, read in the child. A
    missing or unreadable store is an empty one: every lookup is then counted
    as ``cosine_missing`` and stays keyword-only."""
    import sqlite3

    from khipu import recall_prompt

    if not path:
        return {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute("SELECT key, vector FROM query_embed_cache").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    out: dict[str, list[float]] = {}
    for key, blob in rows:
        vec = recall_prompt._unpack_vector(blob) if blob else []
        if vec:
            out[str(key)] = vec
    return out


def refresh_vectors(golden: Path | None = None, *, store: Path | None = None) -> dict[str, Any]:
    """The owner-run step (outside the sandbox, never the nightly): embed each
    golden query the prompt path scores through the push's own query-embedding
    client and rewrite the store to hold exactly those vectors (keys only, no
    query text), mode 0600. Returns counts only."""
    from khipu import hub_snapshot, recall_prompt
    from khipu.redact import redact_secrets

    path = golden or recall_eval.default_golden_path()
    target = store or vectors_path()
    try:
        entries, _ = _load_entries(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"golden file unreadable ({type(exc).__name__})"}
    profile = hub_snapshot.active_snapshot_profile()
    if not profile:
        return {"ok": False, "error": "no active embedding profile on this Mac's replica"}
    queries = sorted({str(e["query"]) for e in entries
                      if recall_eval._entry_applies(e, recall_eval.PATH_PROMPT)})
    vectors: dict[str, list[float]] = {}
    failed = 0
    for query in queries:
        try:
            vec = recall_prompt._cached_query_embed(query, profile)
        except Exception:  # noqa: BLE001 - one failed embed leaves that query keyword-only
            failed += 1
            continue
        key = recall_prompt._query_embed_cache_key(profile, redact_secrets(query)[0])
        vectors[key] = vec
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".recall-eval-vectors.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        import sqlite3

        con = sqlite3.connect(tmp)
        try:
            con.execute("CREATE TABLE query_embed_cache (key TEXT PRIMARY KEY, "
                        "vector BLOB NOT NULL, created REAL NOT NULL)")
            con.executemany("INSERT INTO query_embed_cache VALUES (?, ?, ?)",
                            [(k, recall_prompt._pack_vector(v), time.time()) for k, v in vectors.items()])
            con.commit()
        finally:
            con.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"ok": failed == 0, "stored": len(vectors), "failed": failed, "queries": len(queries)}


def _child_entry() -> None:
    """The scoring child's entry, started by ``_run_child`` under sandbox-exec
    with no inherited descriptors. The work arrives as one JSON document on
    stdin; results leave as JSON lines on a private copy of stdout (the real
    fd 1 is pointed at stderr, so a stray print can never forge a message).
    ``deadline`` is a ``time.monotonic`` value, which is system-wide on macOS
    and Linux, so parent and child share it. The guard is never taken off:
    the child leaves through ``os._exit`` inside it, so a lookup thread still
    running dies with the process instead of reaching a restored exit."""
    try:
        payload = json.loads(sys.stdin.buffer.read())
        out = os.fdopen(os.dup(1), "w", encoding="utf-8")
        os.dup2(2, 1)

        def emit(*msg: Any) -> None:
            out.write(json.dumps(msg) + "\n")  # text-safe: counts, figures and refusal tallies only
            out.flush()

        _quiet_excepthooks()
        scoring, deadline, ref = payload["entries"], payload["deadline"], payload["setup"]
        if ref is not None:
            _resolve(f"{ref[0]}.{ref[1]}")(*ref[2])
        with _offline(_read_vectors(payload.get("vectors"))) as guard:
            try:
                for entry in scoring:
                    if time.monotonic() >= deadline:
                        emit("stop", dict(guard.refused))
                        break
                    try:
                        got = _score_entry(entry, deadline, guard)
                    except Exception:  # noqa: BLE001 - one bad entry is skipped, never fatal
                        emit("bad", dict(guard.refused))
                        continue
                    if got is None:
                        emit("stop", dict(guard.refused))
                        break
                    emit("entry", got, dict(guard.refused))
                else:
                    emit("done", dict(guard.refused))
            finally:
                os._exit(0)
    finally:
        os._exit(1)


_STDERR_LINE = re.compile(r"^\[khipu-recall-eval\] uncaught (\w{1,80}) \(#\d+\)")


def _reduce_stderr(stream, summary: dict[str, Any]) -> None:
    """Drain the child's stderr and keep only a line count and the exception
    type names its excepthook announced; no other text is retained."""
    types: dict[str, int] = {}
    lines = 0
    try:
        for raw in iter(lambda: stream.readline(4096), b""):
            lines += 1
            match = _STDERR_LINE.match(raw.decode("utf-8", "replace"))
            if match and (match.group(1) in types or len(types) < 20):
                types[match.group(1)] = types.get(match.group(1), 0) + 1
    except (OSError, ValueError):
        pass
    summary.update(lines=lines, types=types)


def _run_child(scoring: list[dict[str, Any]], deadline: float,
               setup: tuple[Callable[..., Any], tuple] | None, vectors: Path | None = None):
    """Yield the child's messages until it finishes or deadline + grace, then
    kill it. The child is a plain subprocess under sandbox-exec with
    ``close_fds=True``: nothing but the stdin JSON reaches it, and only stdout
    JSON lines come back. Yields ``("stderr", summary)`` once it is reaped,
    then ``("killed", None)`` if it had to be killed or ``("exited", code)``
    if it ended without a final message."""
    import select

    payload = json.dumps({"entries": scoring, "deadline": deadline, "setup": _setup_ref(setup),
                          "vectors": str(vectors) if vectors else None}).encode()
    proc = subprocess.Popen(_child_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, close_fds=True)
    err_summary: dict[str, Any] = {}
    err_thread = threading.Thread(target=_reduce_stderr, args=(proc.stderr, err_summary), daemon=True)
    err_thread.start()

    def feed() -> None:
        try:
            proc.stdin.write(payload)  # text-safe: the work goes to the sandboxed child over its own stdin pipe
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    threading.Thread(target=feed, daemon=True).start()
    hard = deadline + KILL_GRACE_S
    fd = proc.stdout.fileno()
    buf = b""
    final = False
    try:
        while not final:
            left = hard - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf and not final:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, list) or not msg:
                    continue
                final = msg[0] in ("stop", "done")
                yield msg
    finally:
        killed = proc.poll() is None and not final
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        err_thread.join(KILL_GRACE_S)
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.close()
    yield ("stderr", dict(err_summary))
    if not final:
        yield ("killed", None) if killed else ("exited", proc.returncode)


def run_nightly_eval(*, golden: Path | None = None, budget_s: float = BUDGET_S,
                     child_setup: tuple[Callable[..., Any], tuple] | None = None) -> dict[str, Any]:
    """The nightly step. ``ok`` means the measurement ran; a regression is the
    doctor row's to flag. The history record is written on every outcome.
    Returns the compact record it stored. ``child_setup`` is a test seam: a
    picklable ``(function, args)`` the scoring child runs before its guard."""
    from khipu import hub_snapshot

    now = datetime.now(timezone.utc)
    path = golden or recall_eval.default_golden_path()
    history = read_history()
    record: dict[str, Any] = {
        "ts": now.isoformat(), "ok": True, "entries": 0, "scored": 0,
        "skipped_forgotten": 0, "skipped_ids": 0, "skipped_malformed": 0, "truncated": False,
        "paths": {}, "not_measured": {}, "previous": _previous_figures(history), "golden": _golden_stat(path),
    }
    try:
        try:
            entries, malformed = _load_entries(path) if path.is_file() else ([], {})
        except (OSError, ValueError) as exc:
            record.update(ok=False, transient=True, error=f"golden file unreadable ({type(exc).__name__})")
            entries, malformed = [], {}
        record["entries"] = len(entries) + sum(malformed.values())
        record["skipped_malformed"] = sum(malformed.values())
        if malformed:
            record["malformed"] = malformed
        gone: set[str] = set()
        if entries:
            try:
                con = hub_snapshot.open_snapshot()
            except Exception as exc:  # noqa: BLE001 - no replica: nothing to score against
                record.update(ok=False, transient=True, error=f"replica unavailable ({type(exc).__name__})")
                entries = []
            else:
                try:
                    named = {str(x) for e in entries for x in (e.get("expect") or [])}
                    gone = _unavailable_ids(named, con)
                finally:
                    con.close()
                record["skipped_ids"] = len(gone)
        if entries:
            _score_all(entries, gone, record, budget_s, child_setup)
    finally:
        _write_history([*history, record])
    return record


def _score_all(entries: list[dict[str, Any]], gone: set[str], record: dict[str, Any],
               budget_s: float, child_setup: tuple[Callable[..., Any], tuple] | None = None) -> None:
    scoring: list[dict[str, Any]] = []
    for entry in entries:
        expect = [str(x) for x in (entry.get("expect") or [])]
        if expect and all(x in gone for x in expect):
            record["skipped_forgotten"] += 1
            continue
        scoring.append({**entry, "expect": [x for x in expect if x not in gone]}
                       if expect else entry)
    not_measured: dict[str, dict[str, int]] = {}

    def count(path: str, reason: str) -> None:
        by_reason = not_measured.setdefault(path, {})
        by_reason[reason] = by_reason.get(reason, 0) + 1

    deadline = time.monotonic() + budget_s
    rows: dict[str, list[dict[str, Any]]] = {p: [] for p in MEASURED_PATHS}
    refused = {"hub": 0, "embed": 0, "network": 0, "process": 0, "native": 0, "cosine_missing": 0}
    record["stopped_child"] = False
    if scoring and time.monotonic() < deadline and not _sandbox_available():
        # No OS sandbox, no run: the Python guard is not the guarantee.
        record["skipped"] = SKIPPED_NO_SANDBOX
        record["stopped_child"] = False
        record["offline"] = {**refused, "cosine_skipped": False}
        record["not_measured"] = {}
        return
    if scoring and time.monotonic() < deadline:
        record["sandbox"] = "os"
        for msg in _run_child(scoring, deadline, child_setup, vectors_path()):
            kind = msg[0]
            if kind in ("entry", "bad", "stop", "done"):
                refused = msg[-1]
            if kind == "stderr":
                record["child_stderr"] = msg[1]
                for name, times in sorted((msg[1].get("types") or {}).items()):
                    sys.stderr.write(f"[khipu-recall-eval] child stderr: uncaught {name} x{times}\n")  # text-safe: name is an exception type name matched by \\w{1,80}, times a count
            elif kind == "entry":
                for p, row in msg[1]:
                    if row is None:
                        count(p, "hub_fallback")
                    else:
                        rows[p].append(row)
                record["scored"] += 1
            elif kind == "bad":
                record["skipped_malformed"] += 1
            elif kind == "stop" or kind == "killed":
                record["truncated"] = True
                record["stopped_child"] = kind == "killed"
            elif kind == "exited":
                record.update(ok=False, error=f"the recall check process ended early (exit {msg[1]})")
    elif scoring:
        record["truncated"] = True
    explicit = sum(1 for e in scoring if recall_eval._entry_applies(e, recall_eval.PATH_EXPLICIT))
    if explicit:
        not_measured[recall_eval.PATH_EXPLICIT] = {"needs_hub": explicit}
    record["not_measured"] = not_measured
    record["offline"] = {**refused, "cosine_skipped": refused["embed"] > 0 or refused["cosine_missing"] > 0}
    for p, got_rows in rows.items():
        summary = recall_eval.summarize_rows(got_rows)
        record["paths"][p] = {k: summary[k] for k in _KEPT_METRICS}


# ---- doctor row --------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _measured_with(last: dict[str, Any]) -> str:
    """Which figure the record holds: the push with its cosine leg, or keyword only."""
    off = last.get("offline") if isinstance(last.get("offline"), dict) else {}
    if not off.get("cosine_skipped"):
        return "with semantic search"
    missing = int(off.get("cosine_missing") or 0)
    return f"keyword only, {missing} vectors missing" if missing else "keyword only"


def recall_eval_health(*, now: datetime | None = None) -> dict[str, Any]:
    """The doctor row. ``level`` is green / yellow / red; yellow never gates
    (``ok`` stays true), red does."""
    from khipu.jobs import _is_index_sync_host

    if not _is_index_sync_host():
        return {"ok": True, "applicable": False,
                "note": "not the nightly host — evaluated on the sync host"}
    history = read_history()
    if not history:
        return {"ok": True, "level": "yellow",
                "summary": "Not measured yet. The first recall check runs with tonight's nightly."}
    last = history[-1]
    now = now or datetime.now(timezone.utc)
    try:
        age_h = (now - datetime.fromisoformat(str(last.get("ts")))).total_seconds() / 3600
    except (TypeError, ValueError):
        age_h = None
    paths = last.get("paths") if isinstance(last.get("paths"), dict) else {}
    violations = sum(int(m.get("stale_violations") or 0) for m in paths.values() if isinstance(m, dict))
    prompt = paths.get(recall_eval.PATH_PROMPT) if isinstance(paths.get(recall_eval.PATH_PROMPT), dict) else {}
    positives = int(prompt.get("total") or 0) - int(prompt.get("abstain_total") or 0)
    rate = float(prompt["hit_rate"]) if positives > 0 and prompt.get("hit_rate") is not None else None
    prev = ((last.get("previous") or {}).get("paths") or {}).get(recall_eval.PATH_PROMPT) or {}
    prev_rate = prev.get("hit_rate")
    detail = {"ts": last.get("ts"), "age_hours": None if age_h is None else round(age_h, 1),
              "prompt_hit_rate": rate, "previous_prompt_hit_rate": prev_rate,
              "stale_violations": violations, "entries": last.get("entries"),
              "skipped_forgotten": last.get("skipped_forgotten")}

    def out(level: str, summary: str) -> dict[str, Any]:
        return {"ok": level != "red", "level": level, "summary": summary, **detail}

    if not last.get("ok", True):
        return out("yellow", f"Tonight's recall check could not run: {last.get('error') or 'unknown reason'}.")
    if last.get("skipped"):
        return out("yellow", f"Tonight's recall check did not run: {last['skipped']} "
                             "(the check only runs inside the macOS sandbox that keeps queries on this Mac).")
    if violations:
        return out("red", f"{violations} memories you forgot or replaced came back in search results. "
                          "Run `khipu recall eval --path all` to see which.")
    if last.get("truncated"):
        return out("yellow", f"The recall check stopped at its {BUDGET_S // 60}-minute limit before "
                             "every query ran, so tonight's figures are not compared.")
    failed = int(prompt.get("timeouts") or 0) + int(prompt.get("errors") or 0)
    total = int(prompt.get("total") or 0)
    if total and failed > FAILED_SHARE * total:
        return out("yellow", f"{failed} of {total} recall lookups failed or timed out, "
                             "so tonight's figures are not compared.")
    if rate is not None and isinstance(prev_rate, (int, float)) and rate < prev_rate - ALLOWED_DROP - 1e-9:
        return out("red", f"Recall got worse: the right memory is found {_pct(rate)} of the time, "
                          f"down from {_pct(prev_rate)} the run before ({_measured_with(last)}).")
    if rate is not None and rate < HIT_FLOOR:
        return out("red", f"Recall is below the {_pct(HIT_FLOOR)} floor: the right memory is found "
                          f"{_pct(rate)} of the time ({_measured_with(last)}).")
    if age_h is None or age_h > STALE_AFTER_H:
        return out("yellow", "The recall check has not run in over 48 hours.")
    if int(last.get("entries") or 0) < MIN_ENTRIES:
        return out("yellow", f"Only {last.get('entries') or 0} test queries in the golden set; "
                             f"{MIN_ENTRIES} are needed for a trustworthy reading.")
    fell_back = int(((last.get("not_measured") or {}).get(recall_eval.PATH_PROMPT) or {}).get("hub_fallback") or 0)
    if rate is None and fell_back:
        return out("yellow", f"{fell_back} recall lookups could not be answered from this Mac's copy of "
                             "memory and would have needed the hub, so recall is not measured.")
    if rate is None:
        return out("yellow", "The golden set has no queries for the prompt path, so recall is not measured.")
    return out("green", f"The right memory is found {_pct(rate)} of the time, {_measured_with(last)}"
                        + (f" (was {_pct(prev_rate)})." if isinstance(prev_rate, (int, float)) else "."))


# ---- khipu recall eval --install ----------------------------------------------

def _write_golden_atomic(path: Path, text: str) -> None:
    """Replace the golden file, keeping the previous one beside it. The backup
    and the rewrite use the names forget's golden sweep already removes, so a
    forgotten episode's quote cannot outlive it there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        backup = path.parent / BACKUP_NAME
        shutil.copyfile(path, backup)
        os.chmod(backup, 0o600)
    fd, tmp = tempfile.mkstemp(prefix=GOLDEN_TEMP_PREFIX, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)  # text-safe: the golden file the maintainer asked to merge, local only
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install_candidates(candidates: Path, golden: Path | None = None) -> dict[str, Any]:
    """Merge golden candidate entries into the golden file, de-duplicated by
    query (an existing entry wins), refusing any whose shape or types the
    scorer cannot take (counted by reason) and any whose expected ids are not
    in the replica. Returns counts only; no query or id is ever returned."""
    from khipu import hub_snapshot

    path = golden or recall_eval.default_golden_path()
    try:
        incoming, malformed = _load_entries(candidates)
        existing_text = path.read_text(encoding="utf-8") if path.is_file() else ""
        existing, _ = _load_entries(path) if existing_text else ([], {})
        con = hub_snapshot.open_snapshot()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"could not read the files or the replica ({type(exc).__name__})"}
    try:
        gone = _unavailable_ids({str(x) for e in incoming for x in (e.get("expect") or [])}, con)
    finally:
        con.close()
    seen = {str(e["query"]).strip() for e in existing}
    lines: list[str] = []
    duplicates = missing = 0
    for entry in incoming:
        query = str(entry["query"]).strip()
        if query in seen:
            duplicates += 1
            continue
        if any(str(x) in gone for x in (entry.get("expect") or [])):
            missing += 1
            continue
        seen.add(query)
        lines.append(json.dumps(entry, ensure_ascii=False))
    if lines:
        base = existing_text if not existing_text or existing_text.endswith("\n") else existing_text + "\n"
        _write_golden_atomic(path, base + "\n".join(lines) + "\n")
    return {"ok": True, "added": len(lines), "duplicates": duplicates,
            "rejected_missing_id": missing, "rejected_malformed": sum(malformed.values()),
            "malformed_reasons": malformed, "total": len(existing) + len(lines)}
