# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Scheduled memory/graph jobs — thin wrappers around legacy consolidate/graphify scripts.

Khipu owns the launchd labels and CLI entrypoints; the engines stay unchanged.
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu.paths import DEFAULT_DIR, ensure_data_dir

DRIVER_TIMEOUT_SECONDS = 3600
DRIVER_TERMINATE_GRACE_SECONDS = 2

GRAPHIFY_NOT_INSTALLED = {
    "ok": False,
    "error": "graphify_not_installed",
    "fix": "khipu components install graphify",
}


def _env_script_path(env_key: str) -> Path | None:
    raw = (os.environ.get(env_key) or "").strip()
    return Path(raw) if raw else None


def _application_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Khipu"


def _versions_file() -> Path:
    return _application_support_dir() / "versions.json"


def graphify_nightly_path() -> Path | None:
    """Resolve graphify_nightly.py: versions.json, then env, then None."""
    versions_path = _versions_file()
    if versions_path.is_file():
        try:
            data = json.loads(versions_path.read_text(encoding="utf-8"))
            graphify = data.get("graphify") if isinstance(data, dict) else None
            if isinstance(graphify, dict):
                root = str(graphify.get("path") or "").strip()
                if root:
                    script = Path(root) / "graphify_nightly.py"
                    if script.is_file():
                        return script
        except (OSError, ValueError, TypeError):
            pass

    env_script = _env_script_path("KHIPU_GRAPHIFY_NIGHTLY")
    if env_script is not None and env_script.is_file():
        return env_script
    # The desktop app and an interactive shell do not carry the launchd job's
    # environment; the installed plist is the one place the maintainer path
    # is recorded, so read it back from there.
    plist_script = _plist_env_path(PLIST_GRAPH, "KHIPU_GRAPHIFY_NIGHTLY")
    if plist_script is not None and plist_script.is_file():
        return plist_script
    return None


def _plist_env_path(label: str, key: str) -> Path | None:
    import plistlib

    try:
        with open(_plist_path(label), "rb") as fh:
            data = plistlib.load(fh)
    except (OSError, ValueError):
        return None
    env = data.get("EnvironmentVariables") if isinstance(data, dict) else None
    raw = str((env or {}).get(key) or "").strip() if isinstance(env, dict) else ""
    return Path(raw) if raw else None


# Patchable module attrs (tests); no maintainer-path install defaults.
CONSOLIDATE_NIGHTLY = _env_script_path("KHIPU_CONSOLIDATE_NIGHTLY")
CONSOLIDATE_MONTHLY = _env_script_path("KHIPU_CONSOLIDATE_MONTHLY")
GRAPHIFY_NIGHTLY: Path | None = None
BUILD_INDEX = _env_script_path("KHIPU_BUILD_INDEX")

LOG_DIR = Path.home() / "Library" / "Logs" / "Khipu"
# The maintainer's launchd plists set KHIPU_LEGACY_LOG_DIR so logs written by
# the product's pre-rename name keep being found after upgrading. No default —
# a fresh install has no legacy directory to look in.
LOG_DIR_LEGACY: Path | None = (
    (Path.home() / "Library" / "Logs" / os.environ["KHIPU_LEGACY_LOG_DIR"])
    if os.environ.get("KHIPU_LEGACY_LOG_DIR")
    else None
)
LOG_DIR_CONFIG = DEFAULT_DIR / "logs"

PLIST_NIGHTLY = "com.matt.khipu-nightly"
PLIST_MONTHLY = "com.matt.khipu-monthly"
PLIST_GRAPH = "com.matt.khipu-graph"
# F1: the fourth LaunchAgent — WatchPaths on the memory dirs the notes
# scanner knows, debounced (ThrottleInterval) rather than calendar-scheduled,
# so a note edit is reconciled within the throttle window instead of waiting
# for the Stop hook of a session that may not exist right now.
PLIST_NOTES_WATCH = "com.khipu.notes-watch"
# K10: the Aegis capture queue had no drainer of its own — it waited on
# another harness's Stop hook or the nightly to happen to run `sessions
# drain`. Calendar-interval (every 5 min) rather than WatchPaths: there is
# no single directory whose mtime reliably means "a job landed" across every
# harness's queue file naming.
PLIST_QUEUE_DRAIN = "com.khipu.queue-drain"
# The warm process behind the per-prompt recall hook (khipu.recall_daemon).
# Not calendar-scheduled: KeepAlive, because it exits on purpose when the
# package's code changes on disk and must come back on the new code.
PLIST_RECALL_DAEMON = "com.khipu.recall-daemon"
# Keeps this Mac's copy of the hub current between nightlies (khipu.replica_heal).
# Checks every replica_check_every seconds, and at once when its wake file is
# touched; it opens a connection only when the copy is due.
PLIST_REPLICA_REFRESH = "com.khipu.replica-refresh"

LEGACY_PLIST_NIGHTLY = "com.matt.conversation-memory-nightly"
LEGACY_PLIST_GRAPH = "com.matt.graphify-nightly"

INDEX_SLACK_S = int(os.environ.get("KHIPU_INDEX_SLACK_S", "1800"))

_JOB_SPECS: dict[str, dict[str, str]] = {
    "nightly": {
        "plist": PLIST_NIGHTLY,
        "log_stem": "khipu-nightly",
        "schedule": "daily 02:05",
    },
    "monthly": {
        "plist": PLIST_MONTHLY,
        "log_stem": "khipu-monthly",
        "schedule": "monthly day 1 09:00",
    },
    "graph_build": {
        "plist": PLIST_GRAPH,
        "log_stem": "khipu-graph",
        "schedule": "daily 02:17",
    },
    "notes_watch": {
        "plist": PLIST_NOTES_WATCH,
        "log_stem": "khipu-notes-watch",
        "schedule": "WatchPaths, throttle 30s",
    },
    "queue_drain": {
        "plist": PLIST_QUEUE_DRAIN,
        "log_stem": "khipu-queue-drain",
        "schedule": "every 5 min",
    },
    "recall_daemon": {
        "plist": PLIST_RECALL_DAEMON,
        "log_stem": "khipu-recall-daemon",
        "schedule": "always running (KeepAlive)",
    },
    "replica_refresh": {
        "plist": PLIST_REPLICA_REFRESH,
        "log_stem": "khipu-replica-refresh",
        "schedule": "every 5 min",
    },
}


def _log_paths(stem: str) -> tuple[Path, Path]:
    if LOG_DIR_LEGACY is not None:
        legacy_out = LOG_DIR_LEGACY / f"{stem}.out.log"
        if legacy_out.exists():
            return legacy_out, LOG_DIR_LEGACY / f"{stem}.err.log"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_DIR / f"{stem}.out.log", LOG_DIR / f"{stem}.err.log"
    except OSError:
        pass
    LOG_DIR_CONFIG.mkdir(parents=True, exist_ok=True)
    return LOG_DIR_CONFIG / f"{stem}.out.log", LOG_DIR_CONFIG / f"{stem}.err.log"


def _launchagents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(label: str) -> Path:
    return _launchagents_dir() / f"{label}.plist"


def _plist_loaded(label: str) -> bool:
    """True only when launchctl reports the agent in the current GUI domain.

    A leftover plist on disk is not loaded. Nonzero print rc or any
    exception (timeout, missing launchctl) is not loaded.
    """
    try:
        uid = os.getuid()
        r = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _read_job_state(name: str) -> dict[str, Any] | None:
    path = ensure_data_dir() / "state" / f"job-{name}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _write_job_state(name: str, exit_code: int) -> None:
    state_dir = ensure_data_dir() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "exit": exit_code,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    path = state_dir / f"job-{name}.json"
    # Separate same-directory files keep concurrent receipts atomic and avoid
    # one writer renaming another writer's temporary file out from under it.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=state_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


class _JobTerminated(BaseException):
    """Unwind through child cleanup without being swallowed by maintenance."""


@contextmanager
def _sigterm_guard(name: str, *, defer: bool = False):
    # Python only permits signal handlers on the main thread. Programmatic
    # threaded callers still get locking/deadlines; the CLI owns SIGTERM here.
    state = {"defer": defer, "pending": False}
    if threading.current_thread() is not threading.main_thread():
        yield state
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        # Further bootout/termination requests cannot interrupt group cleanup
        # or the failure receipt. During Popen, defer until its PID is known.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        state["pending"] = True
        if not state["defer"]:
            raise _JobTerminated()

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield state
    except _JobTerminated:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _write_job_state(name, 143)
        raise SystemExit(143) from None
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def _job_lock(name: str):
    """Exclude manual/launchd overlap for the entire wrapper, including upkeep.

    Keep the inode on disk: unlinking a held lock would allow a new owner to
    lock a different inode. Children must not inherit this descriptor.
    """
    state_dir = ensure_data_dir() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / f"job-{name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            with _sigterm_guard(name):
                yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _on_demand_job_entry(name: str) -> dict[str, Any]:
    """Receipt-only job status (no launchd / _JOB_SPECS).

    Maps receipt ``ts`` → ``last_run_iso`` and ``exit`` → ``last_exit`` so the
    desktop On-demand row can show last run without clock/agent copy.
    """
    state = _read_job_state(name)
    ts = state.get("ts") if state else None
    return {
        "plist_label": None,
        "log_path": None,
        "err_log_path": None,
        "last_run_mtime": None,
        "last_run_iso": ts,
        "plist_loaded": None,
        "next_schedule": None,
        "last_exit": state.get("exit") if state else None,
        "last_exit_ts": ts,
        "on_demand": True,
    }


def _run_script(
    script: Path | None,
    *,
    args: list[str] | None = None,
    log_stem: str,
    state_name: str,
) -> int:
    # A fresh process group lets a timed-out engine's descendants be stopped,
    # even when the engine or a grandchild ignores SIGTERM.
    rc = 2
    try:
        if script is None or not script.is_file():
            target = script if script is not None else "unset"
            raise FileNotFoundError(f"job script not found: {target}")
        out_log, err_log = _log_paths(log_stem)
        out_log.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = f"\n--- khipu job {state_name} {stamp} ---\n".encode()
        cmd = [sys.executable, str(script), *(args or [])]
        with open(out_log, "ab") as out_f, open(err_log, "ab") as err_f:
            out_f.write(header)
            err_f.write(header)
            out_f.flush()
            err_f.flush()
            # Install before spawn, but defer a signal until Popen returns:
            # raising inside its constructor can strand a just-created child.
            with _sigterm_guard(state_name, defer=True) as termination:
                proc = subprocess.Popen(
                    cmd, stdout=out_f, stderr=err_f, env=os.environ.copy(),
                    start_new_session=True,
                )
                try:
                    termination["defer"] = False
                    if termination["pending"]:
                        raise _JobTerminated()
                    try:
                        rc = proc.wait(timeout=DRIVER_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        _stop_driver_group(proc)
                        rc = 124
                        err_f.write(f"driver timed out after {DRIVER_TIMEOUT_SECONDS}s\n".encode())
                except BaseException:
                    _stop_driver_group(proc)
                    raise
    except OSError:
        _write_job_state(state_name, 2)
        raise
    _write_job_state(state_name, rc)
    return rc


def _stop_driver_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=DRIVER_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # Always kill the group, even if its leader already exited: a descendant
    # may still be alive and ignoring the graceful termination signal.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _nightly_last_path() -> Path:
    return ensure_data_dir() / "nightly-last.json"


def _step_result(result: Any) -> dict[str, Any]:
    """Normalize a step function's return value into ``{ok, counts,
    error}`` for ``_record_nightly_step``. Every step here already returns
    (or, after this phase, now returns) a plain ``{"ok": ..., ...}`` dict on
    both its success and except branches; a mocked step in a test returns a
    bare ``Mock``/``MagicMock`` instead, which is treated as an
    unremarkable success — its shape is unknown, not its outcome."""
    if isinstance(result, dict):
        return {
            "ok": bool(result.get("ok", True)),
            "counts": result,
            "error": result.get("error") or result.get("reason"),
        }
    return {"ok": True, "counts": None, "error": None}


def _record_nightly_step(
    steps: list[dict[str, Any]], name: str, *, ok: bool, counts: Any = None, error: str | None = None
) -> None:
    """F3/D1: persist this nightly's step-by-step outcome to
    ``nightly-last.json`` (one entry per step: name/ok/counts/error/ts) —
    the "green because the evidence never arrived" class (audit
    2026-08-17): notes reconcile, embed backfill, mark-stale and hygiene
    outcomes were logged as free text and never read by anything. Phase 6
    reads this file; written here so it exists starting now. Written after
    EVERY step (not once at the end) so a step that hangs or crashes the
    process still leaves every step before it on record."""
    steps.append({
        "name": name, "ok": bool(ok), "counts": counts, "error": error,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    try:
        _nightly_last_path().write_text(
            json.dumps({"steps": steps}, default=str, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def run_nightly() -> int:
    with _job_lock("nightly") as acquired:
        if not acquired:
            print("nightly job is already running", file=sys.stderr)
            return 75
        return _run_nightly_locked()


def _run_nightly_locked() -> int:
    steps: list[dict[str, Any]] = []
    drain_result = _step_result(_drain_sessions())
    _record_nightly_step(steps, "sessions_drain", **drain_result)
    driver_error = None
    try:
        rc = _run_script(
            CONSOLIDATE_NIGHTLY, log_stem="khipu-nightly", state_name="nightly"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        rc = 124 if isinstance(exc, subprocess.TimeoutExpired) else 2
        driver_error = f"{type(exc).__name__}: {exc}"
        _write_job_state("nightly", rc)
    if rc and driver_error is None:
        driver_error = "driver timed out" if rc == 124 else f"driver failed (exit {rc})"
    _record_nightly_step(steps, "consolidate_nightly", ok=(rc == 0),
                         counts={"rc": rc}, error=driver_error)
    _record_nightly_step(steps, "notes_reconcile", **_step_result(_reconcile_notes_if_due()))
    _record_nightly_step(steps, "embed_backfill", **_step_result(_embed_backfill()))
    _record_nightly_step(steps, "query_cache_prune", **_step_result(_prune_query_cache()))
    _record_nightly_step(steps, "commitments_close_rules", **_step_result(_commitment_close_rules()))
    _record_nightly_step(steps, "commitments_mark_stale", **_step_result(_mark_stale_commitments()))
    _record_nightly_step(steps, "commitments_hygiene", **_step_result(_hygiene_commitments()))
    _record_nightly_step(steps, "ledger", **_step_result(_ledger_nightly()))
    # Before the replica refresh, so a rename found tonight reaches the replica tonight.
    _record_nightly_step(steps, "project_renames", **_step_result(_project_renames_sweep()))
    _record_nightly_step(steps, "snapshot_refresh", **_step_result(_refresh_snapshot()))
    briefs_result = _briefs_build_if_on()
    if briefs_result is not None:
        _record_nightly_step(steps, "briefs_build", **_step_result(briefs_result))
    briefs_ok = briefs_result is None or _step_result(briefs_result)["ok"]
    final_rc = rc or (0 if drain_result["ok"] and briefs_ok else 1)
    _write_job_state("nightly", final_rc)
    return final_rc


def _project_renames_sweep() -> dict[str, Any]:
    """Ask GitHub what each recent project is called now. A skipped sweep (no
    ``gh``, not signed in) is recorded as ok with its reason under ``why``; it
    is never a nightly failure."""
    try:
        from khipu import project_renames

        res = project_renames.sweep()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {k: v for k, v in res.items() if k != "reason"}
    out["ok"] = res["result"] != "failed"
    if res.get("reason"):
        out["why"] = res["reason"]
    return out


def _ledger_nightly() -> dict[str, Any]:
    """Land waiting ledger writes and purge rows removed longer ago than
    `ledger_purge_after`; before the replica dump so it carries the result."""
    try:
        from khipu.ledger import nightly

        return nightly()
    except Exception as exc:  # noqa: BLE001  (a ledger miss cannot fail the night)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _refresh_snapshot() -> dict[str, Any]:
    """Full dump of the hub into the local replica, after the steps whose
    output the replica carries (reconciled notes, backfilled vectors,
    commitment state); briefs are not a replica table. Since the
    doctor stopped refreshing as a side-effect (CLI-6, 2026-10-10) nothing
    else does, and the per-prompt recall hook stops trusting the replica
    24 h after the last dump. Fail-open: a miss is reported in the step record
    and doctor's hub_snapshot row, and tried again tomorrow."""
    try:
        from khipu.hub_snapshot import refresh

        out = refresh()
        return {"ok": bool(out.get("ok")), **{k: v for k, v in out.items() if k != "path"}}
    except Exception as exc:  # noqa: BLE001 — a replica miss cannot fail the night
        from khipu.storage import describe_os_error

        return {"ok": False, "error": describe_os_error(exc)}


def _drain_sessions() -> dict[str, Any]:
    try:
        from khipu.session_capture import drain

        out = drain(sweep=True, limit=200, time_budget_s=300)
        # A state file the sweep could not read is logged and retried; it is
        # not a failed night (one bad file on a dead session would otherwise
        # fail every nightly until someone deletes it).
        return {"ok": out["failed"] == 0 and not out.get("time_budget_exhausted"), **out}
    except Exception as exc:  # noqa: BLE001 — capture failure cannot block consolidation
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# Each brief is one request to the synth provider, so a first run over every
# topic is spread across nights instead of sent in one burst.
BRIEFS_NIGHTLY_LIMIT = 40


def _briefs_build_if_on() -> dict[str, Any] | None:
    """Rebuild the stale or missing source-backed briefs, at most
    BRIEFS_NIGHTLY_LIMIT a night. ``None`` (no step recorded) while the
    `briefs` switch is off, so an unswitched nightly is unchanged. The
    advisory lock is the same one `khipu briefs build` takes: a manual build
    in flight means this one builds nothing rather than racing it. Fail-open
    like every step here: it never raises."""
    try:
        from khipu import briefs, features

        if not features.enabled("briefs"):
            return None
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                if not briefs.acquire_build_lock(cur):
                    _nightly_log("[khipu-briefs] another build holds the lock; built 0")
                    return {"ok": True, "built": 0, "reason": "lock held"}
                try:
                    chosen = briefs.select_for_build(cur, None, BRIEFS_NIGHTLY_LIMIT)
                    if not chosen.get("available"):
                        _nightly_log(f"[khipu-briefs] unavailable: {chosen.get('reason')}")
                        return {"ok": False, "built": 0, "reason": chosen.get("reason") or "unavailable"}
                    results = briefs.build_many(conn, chosen["topics"])
                finally:
                    briefs.release_build_lock(cur)
                    conn.commit()
        out: dict[str, Any] = {"ok": True, "built": 0}
        failed_topics: list[str] = []
        could_not_run = 0
        for r in results:
            status = str(r.get("status") or "unavailable").lower()
            key = "succeeded" if status == "ok" else status
            out[key] = int(out.get(key, 0)) + 1
            if status == "failed":
                failed_topics.append(str(r.get("topic")))
            elif status in {"error", "unavailable"} or r.get("ok") is False:
                could_not_run += 1
        # A topic whose brief the model could not produce is recorded as a
        # failed row, keeps its previous brief, and is retried only when its
        # sources change: a handled outcome for one topic. Counting it as a
        # failed night made one busy topic fail the nightly on three nights of
        # four (2026-10-09..12). The night fails when the build could not run
        # for a topic (provider or table error) or when nothing got through.
        progressed = sum(int(out.get(k, 0)) for k in ("built", "succeeded", "unchanged"))
        if could_not_run or (failed_topics and not progressed):
            out["ok"] = False
        if failed_topics:
            out["failed_topics"] = failed_topics[:10]
        _nightly_log(f"[khipu-briefs] built {json.dumps(out, default=str)[:400]}")
        return out
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        _nightly_log(f"[khipu-briefs] skipped: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _nightly_log(line: str) -> None:
    """D2: append one structured evidence line to the Khipu log dir ALWAYS,
    and additionally to the legacy log dir when one is configured — never
    instead. Before this, `_log_paths` picked exactly one home per stem (the
    legacy dir won whenever it already had this stem's file), so a
    legacy-dir Mac's own Khipu-dir nightly log sat at 0 bytes since 09-06
    while doctor read neither: evidence landed somewhere, but not in the one
    place every other check here expects to find it."""
    payload = (line.rstrip("\n") + "\n").encode()
    targets: list[Path] = []
    try:
        out_log, _ = _log_paths("khipu-nightly")
        targets.append(out_log)
    except OSError:
        pass
    if LOG_DIR_LEGACY is not None:
        targets.append(LOG_DIR_LEGACY / "khipu-nightly.out.log")
    seen: set[Path] = set()
    for t in targets:
        if t in seen:
            continue
        seen.add(t)
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            with open(t, "ab") as f:
                f.write(payload)
        except OSError:
            pass


def _embed_backfill() -> dict[str, Any]:
    """Khipu owns the vector sweep. Until 2026-09-05 the only nightly embed
    backfill ran inside the legacy consolidate driver, AFTER its
    memory-root reconcile — so any reconcile failure (an unterminated
    frontmatter block, 2026-09-03..05) silently took the backfill with it
    and topic coverage stalled at 55 %. This runs regardless of the driver's
    outcome and after notes.reconcile has written its pages, so the pages
    it just created are findable by meaning the same night. Fail-open: a
    miss is healed tomorrow, and doctor's embed_coverage_ok says so."""
    try:
        from khipu import embed

        stats = embed.backfill()
        failed = int(stats.get("failed_chunks") or stats.get("failed") or 0)
        budget_exhausted = bool(
            stats.get("time_budget_exhausted") or stats.get("budget_exhausted")
        )
        ok = (
            bool(stats.get("ok", True))
            and failed == 0
            and not budget_exhausted
            and not bool(stats.get("cancelled"))
        )
        _nightly_log(
            f"[khipu-embed] backfill {'ok' if ok else 'partial'} "
            f"{json.dumps(stats, default=str)[:400]}"
        )
        out = {**stats, "ok": ok}
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        _nightly_log(f"[khipu-embed] backfill skipped: {type(exc).__name__}: {exc}")
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # Independent of the memory sweep: a memory-provider failure must not
    # leave the libraries (a different provider, perhaps) unswept.
    lib = _library_backfill()
    if lib is not None:
        out["library"] = lib
        if not lib.get("ok", False):
            out["ok"] = False
            if lib.get("error") and not out.get("error"):
                out["error"] = str(lib["error"])
    return out


def _library_backfill() -> dict[str, Any] | None:
    """The same sweep for every enabled library: scan (no model), then embed the
    missing chunks under each library's own profile, against the same daily
    embed budget. Rides inside the embed_backfill step so the step set is
    unchanged; its receipt lands under ``library`` in that step's counts and in
    its own log line. Fail-open, and never flips the memory sweep's ``ok``.
    None when no hub is configured (nothing to sweep)."""
    try:
        from khipu.db import dsn_configured

        if not dsn_configured():
            return None
        from khipu import library

        res = library.nightly()
        results = res.get("results", []) if isinstance(res, dict) else []
        nested_unhealthy = any(
            isinstance(row, dict)
            and (
                not bool(row.get("ok", True))
                or int(row.get("failed") or row.get("failed_chunks") or 0) > 0
                # Cap-hit partial with nothing failed is healthy (matches
                # library.nightly's own bf_ok); remaining after a hard failure
                # still counts.
                or (
                    int(row.get("remaining") or 0) > 0
                    and not (
                        str(row.get("status") or "") == "partial"
                        and int(row.get("failed") or row.get("failed_chunks") or 0) == 0
                    )
                )
                or bool(row.get("time_budget_exhausted") or row.get("budget_exhausted"))
                or bool(row.get("error"))
                or "budget" in str(row.get("skipped") or "").lower()
            )
            for row in results
        )
        res = {
            **res,
            "ok": (
                bool(res.get("ok", True))
                and not bool(res.get("budget_exhausted") or res.get("time_budget_exhausted"))
                and not nested_unhealthy
            ),
        }
        brief = [
            {k: r[k] for k in ("name", "added", "updated", "removed", "embedded",
                               "failed", "remaining", "error", "skipped") if k in r}
            for r in res.get("results", [])
        ]
        _nightly_log(
            f"[khipu-library] backfill {'ok' if res.get('ok') else 'partial'} "
            f"{json.dumps({'libraries': res.get('libraries'), 'results': brief}, default=str)[:400]}"
        )
        return res
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        _nightly_log(f"[khipu-library] backfill skipped: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _prune_query_cache() -> dict[str, Any]:
    """Drop query vectors nobody has asked for in a month (see
    khipu.embed.QUERY_CACHE_TTL_DAYS). Fail-open like the backfill."""
    try:
        from khipu import embed

        n = embed.prune_query_cache()
        _nightly_log(f"[khipu-embed] query cache pruned {n}")
        out = {"ok": True, "pruned": n}
    except Exception as exc:  # noqa: BLE001
        _nightly_log(f"[khipu-embed] query cache prune skipped: {type(exc).__name__}: {exc}")
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def _mark_stale_commitments() -> dict[str, Any]:
    """W3: age open commitments past STALE_AFTER_DAYS into 'stale'.

    ``commitments.mark_stale`` shipped with no caller at all (audit
    2026-09-04), so nothing ever aged: `khipu owed --status stale` was
    permanently empty and the open list grew without bound. Same posture as
    ``_reconcile_notes_if_due`` — additive, fail-open, and it must never turn
    a good nightly into a bad one (the external driver's exit code is what
    gates job_status/doctor)."""
    try:
        from khipu import commitments
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                n = commitments.mark_stale(cur)
            conn.commit()
        out = {"ok": True, "stale": int(n)}
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        out = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    _nightly_log(f"commitments-mark-stale: {json.dumps(out, default=str)[:300]}")
    return out


def _provenance_backfill(conn) -> dict[str, Any]:
    """``khipu provenance backfill`` inside the nightly's close-rules step,
    until every project is covered, then the 30-day prune of the replica
    change stream (``fact_changes``). Fail-open: an error is reported and the
    rules still run."""
    try:
        from khipu import provenance

        out = provenance.backfill(conn)
        # Fact steps a capture kept as gaps are re-run (invariant I4), after
        # the backfill and before tonight's rules read the facts.
        out["gaps"] = provenance.replay_gaps(conn)
        # The hub refusals' identity index and duplicate sweep, until every
        # episode from before 0047 is indexed (section 6.1).
        out["index"] = provenance.index_identities(conn)
        # The replica stream's 30-day retention (section 6 of the design).
        out["changes"] = provenance.prune_changes(conn)
        return out
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}


def _commitment_close_rules() -> dict[str, Any]:
    """Owed slice 2: the fact-provenance witness backfill while projects
    are pending, fill ``prs`` on rows from before migration 0034, then
    run the PR and decision close rules over every open item
    (``close_rules.run``). Before mark_stale, so an item whose PR merged
    closes as done rather than aging out. Fail-open like its neighbours."""
    try:
        from khipu import close_rules
        from khipu.db import connect

        with connect() as conn:
            # Fact provenance's witness backfill first (section 4 of the
            # 2026-10-23 design): a legacy restater proven tonight counts
            # for tonight's rules (its PR refs clear needs_owner).
            witness = _provenance_backfill(conn)
            with conn.cursor() as cur:
                filled = close_rules.backfill_prs(cur)
            conn.commit()
            with conn.cursor() as cur:
                report = close_rules.run(cur)
            conn.commit()
        out = {**report, "backfilled": int(filled), "provenance": witness}
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        out = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    _nightly_log(f"commitments-close-rules: {json.dumps(out, default=str)[:300]}")
    return out


def _hygiene_commitments() -> dict[str, Any]:
    """W3 Owed quality (2026-09-05): run the two hygiene passes every night
    instead of only on demand via `khipu hygiene commitments`.

    Same posture as `_mark_stale_commitments` — a backup first
    (``hygiene.backup_commitments``, never DELETEs), then the model-free
    ownership pass (``run_session_ended_pass``) and the re-judge/dedup pass
    (``run_commitments_hygiene``), both applied. Fail-open: a failure here is
    logged and must never turn a good nightly into a bad one (the external
    CONSOLIDATE_NIGHTLY driver's exit code is what gates job_status/doctor).
    """
    try:
        from khipu import hygiene
        from khipu.db import connect

        with connect() as conn:
            backup_dir = hygiene.backup_commitments(conn)
            with conn.cursor() as cur:
                session_report = hygiene.run_session_ended_pass(cur, apply=True)
                rejudge_report = hygiene.run_commitments_hygiene(cur, apply=True)
            conn.commit()
        _nightly_log(
            f"[khipu-hygiene] session-ended {session_report.get('counts')} · "
            f"rejudge {rejudge_report.get('counts')} · backup {backup_dir}"
        )
        out = {
            "ok": True, "session_ended": session_report.get("counts"),
            "rejudge": rejudge_report.get("counts"), "backup": str(backup_dir),
        }
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        from khipu.storage import describe_os_error

        _nightly_log(f"[khipu-hygiene] skipped: {describe_os_error(exc)}")
        out = {"ok": False, "error": describe_os_error(exc)}
    return out


def _reconcile_notes_if_due() -> dict[str, Any]:
    """W4.3: piggyback `khipu.notes.reconcile` on the nightly cadence, the
    same posture as `_offsite_if_due` on the graph job — additive,
    best-effort, and must never turn a good nightly run into a bad one on
    doctor/status (the external CONSOLIDATE_NIGHTLY driver's exit code above
    is the one that actually gates job_status/doctor)."""
    try:
        from khipu import notes

        out = notes.reconcile(dry_run=False)
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        out = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    _nightly_log(f"notes-reconcile: {json.dumps(out, default=str)[:600]}")
    return out


def nightly_last() -> dict[str, Any] | None:
    """D1: read back the per-step evidence `_record_nightly_step` writes.
    None when the nightly has never recorded a run on this Mac (not a
    failure — a fresh install, or a Mac that is not the nightly host)."""
    path = _nightly_last_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# D1: doctor `*_ok` key -> (nightly-last.json step name, fix hint). Every
# step here is written by `run_nightly` above, fail-open, so a step that
# never runs (missing key, provider outage, etc.) is otherwise invisible —
# "green because evidence never arrived" (audit 2026-08-17), the same class
# `_record_nightly_step`'s own docstring names.
_NIGHTLY_STEP_CHECKS: dict[str, tuple[str, str]] = {
    "briefs_build_ok": (
        "briefs_build",
        "check the synthesis provider, then run `khipu briefs build`",
    ),
    "notes_reconcile_ok": (
        "notes_reconcile",
        "run `khipu notes reconcile` and check the error",
    ),
    "embed_provider_ok": (
        "embed_backfill",
        "check the embedding provider key/credentials, then run `khipu jobs install nightly`"
        " or wait for the next nightly",
    ),
    "query_cache_prune_ok": (
        "query_cache_prune",
        "run `khipu jobs install nightly` or wait for the next nightly",
    ),
    "commitments_close_rules_ok": (
        "commitments_close_rules",
        "run `khipu jobs install nightly` or wait for the next nightly",
    ),
    "commitments_hygiene_ok": (
        "commitments_hygiene",
        "run `khipu hygiene commitments` and check the error",
    ),
    "mark_stale_ok": (
        "commitments_mark_stale",
        "run `khipu jobs install nightly` or wait for the next nightly",
    ),
    "ledger_ok": (
        "ledger",
        "run `khipu ledger drain` and check the error",
    ),
}

_EMBED_HTTP_STATUS_RE = re.compile(r"\b(401|403|429)\b")


def _recorded_error_texts(value: Any) -> list[str]:
    """Every ``error``/``reason`` string recorded in a nightly step entry,
    including the library sub-receipt nested in its counts."""
    texts: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("error", "reason") and item:
                texts.append(str(item))
            else:
                texts.extend(_recorded_error_texts(item))
    elif isinstance(value, list):
        for item in value:
            texts.extend(_recorded_error_texts(item))
    return texts


def _recorded_provider_failures(value: Any) -> list[dict[str, Any]]:
    """Every structured ``last_provider_failure`` recorded in a nightly step
    entry — the memory sweep's counts and the library sub-receipt nested in
    them. Each is ``{"http_status": int | None, "kind": str}`` as written by
    ``khipu.embed.provider_failure`` (status and kind only, never a body)."""
    failures: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "last_provider_failure" and isinstance(item, dict):
                failures.append(item)
            else:
                failures.extend(_recorded_provider_failures(item))
    elif isinstance(value, list):
        for item in value:
            failures.extend(_recorded_provider_failures(item))
    return failures


def _embed_backfill_fix(entry: dict[str, Any]) -> str:
    """Doctor fix for a failed embed_backfill step, following the recorded
    failure instead of always blaming the provider key: a structured
    provider failure names the provider (for 429, its quota or spending
    cap; for 401/403, its credentials), a hub connection failure names the
    database, and anything else keeps the generic provider hint."""
    from khipu.hub_errors import unreachable_text

    for failure in _recorded_provider_failures(entry):
        status = failure.get("http_status")
        if status is None:
            continue
        try:
            code = int(status)
        except (TypeError, ValueError):
            continue
        if code == 429:
            return (
                "check the embedding provider quota or spending cap (HTTP 429), "
                "then run `khipu jobs install nightly` or wait for the next nightly"
            )
        if code in (401, 403):
            return (
                f"check the embedding provider key/credentials (HTTP {code}), "
                "then run `khipu jobs install nightly` or wait for the next nightly"
            )
        return (
            f"check the embedding provider (HTTP {code}), "
            "then run `khipu jobs install nightly` or wait for the next nightly"
        )
    texts = _recorded_error_texts(entry)
    if any(unreachable_text(text) for text in texts):
        return (
            "check the hub database is reachable (its connection timed out or was refused), "
            "then run `khipu jobs install nightly` or wait for the next nightly"
        )
    for text in texts:
        match = _EMBED_HTTP_STATUS_RE.search(text)
        if match:
            code = match.group(1)
            if code == "429":
                return (
                    "check the embedding provider quota or spending cap (HTTP 429), "
                    "then run `khipu jobs install nightly` or wait for the next nightly"
                )
            return (
                f"check the embedding provider key/credentials (HTTP {code}), "
                "then run `khipu jobs install nightly` or wait for the next nightly"
            )
    return _NIGHTLY_STEP_CHECKS["embed_provider_ok"][1]


# A step whose last recorded run is older than this is treated as "the
# nightly stopped running", not "it hasn't run yet today" — same idea as
# index_freshness's INDEX_SLACK_S, wider because this compares against a
# once-a-day job rather than the index that follows it same-night.
NIGHTLY_STEP_STALE_HOURS = 36


def _parse_ts(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def _hours_since(ts: Any) -> float | None:
    t = _parse_ts(ts)
    if t is None:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def nightly_step_health() -> dict[str, dict[str, Any]]:
    """D1: one doctor-shaped block per nightly step, read from
    `nightly-last.json` (Phase 4) instead of trusting only the legacy
    driver's exit code. Applicable only on the sync host — every other Mac
    never runs the nightly and has nothing of its own to evaluate (same
    posture as `index_freshness`).

    A step that has never been recorded is skipped only during a bounded
    first-run grace period established by a loaded plist plus recent install
    or run metadata. Unknown-age or unloaded schedules are red rather than
    staying green forever. A step recorded `ok: false` is red with its own error/fix. A
    step whose last recording is older than `NIGHTLY_STEP_STALE_HOURS` is
    red too, REGARDLESS of that recording's own `ok` — a nightly that
    stopped running altogether is exactly what a same-day `ok: true` from a
    week ago would otherwise hide."""
    from khipu import features

    applicable = _is_index_sync_host()
    data = nightly_last()
    receipt_path = _nightly_last_path()
    steps_doc = data.get("steps") if isinstance(data, dict) else None
    receipt_malformed = receipt_path.exists() and (
        not isinstance(steps_doc, list)
        or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("ok"), bool)
            for entry in (steps_doc if isinstance(steps_doc, list) else [])
        )
    )
    job = _job_entry("nightly") if applicable else {}
    by_name: dict[str, dict[str, Any]] = {}
    if data and isinstance(data.get("steps"), list):
        for entry in data["steps"]:
            if isinstance(entry, dict) and entry.get("name"):
                # Steps are appended in run order; the last write for a name
                # (the most recent nightly's outcome) is what doctor judges.
                by_name[entry["name"]] = entry
    source = str(_nightly_last_path())
    out: dict[str, dict[str, Any]] = {}
    if applicable:
        exit_code = job.get("last_exit")
        exit_ts = job.get("last_exit_ts")
        exit_age = _hours_since(exit_ts)
        if exit_code not in (None, 0):
            out["nightly_job_ok"] = {
                "ok": False, "applicable": True,
                "error": f"scheduled nightly exited {exit_code}",
                "ts": exit_ts, "source": source,
            }
        elif exit_code == 0 and not exit_ts:
            out["nightly_job_ok"] = {
                "ok": False, "applicable": True,
                "error": "scheduled nightly success has no timestamp",
                "ts": exit_ts, "source": source,
            }
        elif exit_ts and exit_age is None:
            out["nightly_job_ok"] = {
                "ok": False, "applicable": True,
                "error": "scheduled nightly has an unusable success timestamp",
                "ts": exit_ts, "source": source,
            }
        elif exit_age is not None and exit_age > NIGHTLY_STEP_STALE_HOURS:
            out["nightly_job_ok"] = {
                "ok": False, "applicable": True,
                "error": "scheduled nightly receipt is overdue",
                "ts": exit_ts, "source": source, "stale": True,
            }
        elif exit_code == 0:
            out["nightly_job_ok"] = {
                "ok": True, "applicable": True, "ts": exit_ts, "source": source,
            }
        else:
            install_age = None
            if isinstance(job.get("plist_mtime"), (int, float)):
                install_age = (
                    datetime.now(timezone.utc).timestamp() - float(job["plist_mtime"])
                ) / 3600
            pending = bool(job.get("plist_loaded")) and install_age is not None and (
                0 <= install_age <= NIGHTLY_STEP_STALE_HOURS
            )
            out["nightly_job_ok"] = {
                "ok": pending, "applicable": True, "source": source,
                **(
                    {"pending": True, "reason": "fresh install awaiting its first nightly run"}
                    if pending
                    else {"error": "scheduled nightly has no usable recent run or install evidence"}
                ),
            }
    else:
        out["nightly_job_ok"] = {
            "ok": True, "applicable": False,
            "note": "not the nightly host — evaluated on the sync host",
        }
    for doctor_key, (step_name, fix) in _NIGHTLY_STEP_CHECKS.items():
        if step_name == "briefs_build" and not features.enabled("briefs"):
            out[doctor_key] = {"ok": True, "applicable": False, "note": "briefs disabled"}
            continue
        if not applicable:
            out[doctor_key] = {
                "ok": True, "applicable": False,
                "note": "not the nightly host — evaluated on the sync host",
            }
            continue
        if receipt_malformed:
            out[doctor_key] = {
                "ok": False, "applicable": True,
                "error": "malformed nightly step receipt",
                "fix": "inspect or remove the invalid nightly-last.json after preserving evidence",
                "source": source,
            }
            continue
        entry = by_name.get(step_name)
        if entry is None:
            last_run = job.get("last_run_iso") or job.get("last_exit_ts")
            age_h = _hours_since(last_run)
            if last_run is None and isinstance(job.get("plist_mtime"), (int, float)):
                age_h = (
                    datetime.now(timezone.utc).timestamp() - float(job["plist_mtime"])
                ) / 3600
            pending = bool(job.get("plist_loaded")) and age_h is not None and (
                0 <= age_h <= NIGHTLY_STEP_STALE_HOURS
            )
            if not pending:
                out[doctor_key] = {
                    "ok": False, "applicable": True,
                    "error": "scheduled nightly has no usable recent step receipt or first-run evidence",
                    "fix": "run `khipu jobs status` and inspect the nightly logs",
                    "source": source,
                }
                continue
            out[doctor_key] = {
                "ok": True, "applicable": True, "skipped": True,
                "reason": "not checked yet — the nightly runs at 02:05",
                "source": source,
            }
            continue
        age_h = _hours_since(entry.get("ts"))
        if entry.get("ok") and age_h is None:
            out[doctor_key] = {
                "ok": False, "applicable": True,
                "error": "nightly step has an unusable success timestamp",
                "fix": fix, "ts": entry.get("ts"), "source": source,
            }
            continue
        if age_h is not None and age_h > NIGHTLY_STEP_STALE_HOURS:
            last_date = str(entry.get("ts"))[:10] or "an unknown date"
            out[doctor_key] = {
                "ok": False, "applicable": True,
                "error": f"the nightly has not run since {last_date}; run `khipu jobs status`",
                "fix": "run `khipu jobs status`",
                "ts": entry.get("ts"), "source": source, "stale": True,
            }
            continue
        ok = bool(entry.get("ok"))
        out[doctor_key] = {
            "ok": ok, "applicable": True,
            "error": None if ok else entry.get("error"),
            "fix": None if ok else (
                _embed_backfill_fix(entry) if doctor_key == "embed_provider_ok" else fix
            ),
            "ts": entry.get("ts"),
            "source": source,
        }
    return out


def skipped_step_names(health: dict[str, dict[str, Any]]) -> list[str]:
    """The base name (``key`` minus its ``_ok`` suffix) of every
    `nightly_step_health()` entry marked `skipped` or `applicable: false` —
    doctor folds these into its `not_configured` list so healthRows.tsx
    renders them with the same grey "not set up" row `memory_root`/
    `graph_sqlite` already use, instead of a false green for a step that
    never runs on this Mac or has not run yet."""
    names: list[str] = []
    for key, v in health.items():
        if v.get("skipped") or v.get("applicable") is False:
            names.append(key[: -len("_ok")])
    return names


def run_monthly(*, dry_run: bool = False) -> int:
    with _job_lock("monthly") as acquired:
        if not acquired:
            print("monthly job is already running", file=sys.stderr)
            return 75
        return _run_monthly_locked(dry_run=dry_run)


def _run_monthly_locked(*, dry_run: bool = False) -> int:
    args: list[str] = []
    if dry_run:
        # Live Claude monthly (conversation-memory-monthly.py) has no --dry-run.
        # Cursor-era consolidate_monthly.py does — only pass the flag there.
        monthly = CONSOLIDATE_MONTHLY
        if monthly is not None and monthly.name == "conversation-memory-monthly.py":
            print(
                f"error: live monthly driver has no --dry-run (script={monthly})",
                file=sys.stderr,
            )
            return 2
        if monthly is not None:
            args = ["--dry-run"]
    return _run_script(
        CONSOLIDATE_MONTHLY,
        args=args,
        log_stem="khipu-monthly",
        state_name="monthly",
    )


def run_graph_build() -> int:
    script = GRAPHIFY_NIGHTLY or graphify_nightly_path()
    if script is None:
        _write_job_state("graph_build", 2)
        print(json.dumps(GRAPHIFY_NOT_INSTALLED))
        return 2
    rc = _run_script(script, log_stem="khipu-graph", state_name="graph_build")
    # Snapshot protection is independent of embedding progress or failures.
    _offsite_if_due()
    return rc


def _offsite_if_due() -> None:
    """Weekly offsite copy of the newest graph snapshot, piggybacked on the
    graph job because nothing else schedules it — `khipu graph-backup offsite`
    had run exactly once, by hand, before this (2026-08-25). Failures are
    recorded by run_offsite as ops_events (doctor's graph_offsite reads them)
    and must never fail the build whose snapshot they copy."""
    from khipu import graph_backup

    try:
        last_ok = graph_backup._last_ok_time(
            "graph_snapshot_offsite",
            statement_timeout_s=graph_backup.OFFSITE_RECEIPT_STATEMENT_TIMEOUT_SECONDS,
            connect_timeout=graph_backup.OFFSITE_RECEIPT_CONNECT_TIMEOUT_SECONDS,
            raise_on_error=True,
        )
        now = datetime.now(timezone.utc)
        if not graph_backup.offsite_due(last_ok=last_ok, now=now):
            return
        out = graph_backup.run_offsite()
    except graph_backup.OffsiteReceiptReadError as exc:
        out = {"ok": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "reason": str(exc)}
    try:
        out_log, _ = _log_paths("khipu-graph")
        with open(out_log, "ab") as f:
            f.write(f"offsite: {json.dumps(out, default=str)[:600]}\n".encode())
    except OSError:
        pass


def run_build_index() -> int:
    return _run_script(
        BUILD_INDEX, log_stem="khipu-build-index", state_name="build_index"
    )


def nightly_plist_path() -> Path:
    khipu = _plist_path(PLIST_NIGHTLY)
    legacy = _plist_path(LEGACY_PLIST_NIGHTLY)
    if khipu.is_file():
        return khipu
    return legacy


def nightly_log_path() -> Path:
    explicit = (os.environ.get("KHIPU_NIGHTLY_LOG") or "").strip()
    if explicit:
        return Path(explicit)
    khipu = _log_paths("khipu-nightly")[0]
    legacy = (
        LOG_DIR_LEGACY / "conversation-memory-nightly.out.log"
        if LOG_DIR_LEGACY is not None
        else None
    )
    if khipu.is_file():
        if legacy is None or not legacy.is_file() or khipu.stat().st_mtime >= legacy.stat().st_mtime:
            return khipu
    if legacy is not None and legacy.is_file():
        return legacy
    return khipu


def graph_plist_path() -> Path:
    khipu = _plist_path(PLIST_GRAPH)
    legacy = _plist_path(LEGACY_PLIST_GRAPH)
    if khipu.is_file():
        return khipu
    return legacy


def _installed_log_paths(plist: Path, stem: str) -> tuple[Path, Path]:
    """Where launchd actually sends this job's output: the installed plist's
    StandardOutPath/StandardErrorPath. The caller's environment is not the
    job's (the desktop CLI has no KHIPU_LEGACY_LOG_DIR), so it only decides
    when no plist is installed or it names no path."""
    out_log, err_log = None, None
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
        if isinstance(data, dict):
            if isinstance(data.get("StandardOutPath"), str):
                out_log = Path(data["StandardOutPath"])
            if isinstance(data.get("StandardErrorPath"), str):
                err_log = Path(data["StandardErrorPath"])
    except (OSError, plistlib.InvalidFileException, ValueError):
        pass
    if out_log is None or err_log is None:
        env_out, env_err = _log_paths(stem)
        out_log, err_log = out_log or env_out, err_log or env_err
    return out_log, err_log


def _job_entry(name: str) -> dict[str, Any]:
    spec = _JOB_SPECS[name]
    plist = _plist_path(spec["plist"])
    out_log, err_log = _installed_log_paths(plist, spec["log_stem"])
    last_run_mtime: float | None = None
    try:
        if out_log.is_file():
            last_run_mtime = out_log.stat().st_mtime
    except OSError:
        pass
    plist_mtime: float | None = None
    try:
        if plist.is_file():
            plist_mtime = plist.stat().st_mtime
    except OSError:
        pass
    state = _read_job_state(name)
    last_exit = state.get("exit") if state else None
    # The run receipt is the evidence a scheduled job finished; the out log
    # alone is not. On a Mac with KHIPU_LEGACY_LOG_DIR in its plists the job
    # writes its output there, while the app's CLI (no such env) reads the
    # launchd stdout file, which only moves when something prints to stdout.
    # Take the newer of the two; None means there is no evidence either way.
    log_ts = (
        datetime.fromtimestamp(last_run_mtime, tz=timezone.utc)
        if last_run_mtime is not None
        else None
    )
    receipt_ts = _parse_ts(state.get("ts")) if state else None
    if receipt_ts is not None and (log_ts is None or receipt_ts >= log_ts):
        last_run_iso, last_run_source = state.get("ts"), "receipt"
    elif log_ts is not None:
        last_run_iso, last_run_source = log_ts.isoformat(), "log"
    else:
        last_run_iso, last_run_source = None, None
    # Lazy import: launchd_gen imports from this module (_JOB_SPECS,
    # _plist_path, etc.), so importing it at module scope here would be a
    # circular import. Deferring it into the function body breaks the cycle
    # at the cost of one extra import per call.
    try:
        from khipu.launchd_gen import plist_current as _plist_current

        current = _plist_current(name)
    except Exception:
        current = None
    try:
        from khipu.launchd_gen import read_versions

        scheduled = read_versions().get("scheduled_jobs", {})
        expected = isinstance(scheduled, dict) and bool(
            scheduled.get(name) or (name == "graph_build" and scheduled.get("graph"))
        )
    except Exception:
        expected = False
    next_schedule = spec["schedule"]
    if name == "replica_refresh":
        try:
            from khipu.replica_heal import schedule_words

            next_schedule = schedule_words()
        except Exception:  # noqa: BLE001 - fail open to the spec's words
            pass
    return {
        "plist_label": spec["plist"],
        "plist_mtime": plist_mtime,
        "log_path": str(out_log),
        "err_log_path": str(err_log),
        "last_run_mtime": last_run_mtime,
        "last_run_iso": last_run_iso,
        "last_run_source": last_run_source,
        "plist_loaded": _plist_loaded(spec["plist"]),
        "plist_current": current,
        "expected": expected,
        "next_schedule": next_schedule,
        "last_exit": last_exit,
        "last_exit_ts": state.get("ts") if state else None,
    }


def job_status() -> dict[str, Any]:
    return {
        "nightly": _job_entry("nightly"),
        "monthly": _job_entry("monthly"),
        "graph_build": _job_entry("graph_build"),
        "notes_watch": _job_entry("notes_watch"),
        "queue_drain": _job_entry("queue_drain"),
        "recall_daemon": _job_entry("recall_daemon"),
        "replica_refresh": _job_entry("replica_refresh"),
        "embed_media_backfill": _on_demand_job_entry("embed_media_backfill"),
    }


def _is_index_sync_host() -> bool:
    from khipu.git_sync_health import is_sync_host

    return is_sync_host()


def index_freshness(*, memory_root: Path | None = None) -> dict[str, Any]:
    """Index files vs nightly log — red only when the nightly ran and index did not follow."""
    from khipu.config import path_setting

    mem = memory_root or path_setting("memory_root")
    out: dict[str, Any] = {
        "ok": True,
        "applicable": _is_index_sync_host(),
        "reasons": [],
    }
    if not out["applicable"]:
        out["note"] = "not the sync host — index freshness is judged on the nightly Mac"
        return out
    if mem is None or not mem.is_dir():
        out["ok"] = False
        out["reasons"].append("memory_root not configured")
        return out

    index_files: list[Path] = []
    memory_md = mem / "MEMORY.md"
    if memory_md.is_file():
        index_files.append(memory_md)
    index_files.extend(sorted(mem.glob("_index_*.md")))

    if not index_files:
        out["ok"] = False
        out["reasons"].append("no MEMORY.md or _index_*.md found under memory root")
        return out

    try:
        index_mtime = max(f.stat().st_mtime for f in index_files)
    except OSError as e:
        out["ok"] = False
        out["reasons"].append(f"could not stat index files: {e}")
        return out

    nightly_log = nightly_log_path()
    nightly_mtime: float | None = None
    try:
        if nightly_log.is_file():
            nightly_mtime = nightly_log.stat().st_mtime
    except OSError:
        pass

    out["index_mtime"] = index_mtime
    out["nightly_log"] = str(nightly_log)
    out["nightly_log_mtime"] = nightly_mtime
    out["index_files"] = [str(p.relative_to(mem)) for p in index_files[:20]]

    if nightly_mtime and nightly_mtime - index_mtime > INDEX_SLACK_S:
        lag_min = int((nightly_mtime - index_mtime) // 60)
        out["ok"] = False
        out["reasons"].append(
            f"index is {lag_min} min older than the last nightly log — rebuild may have been skipped"
        )
    elif nightly_mtime is None:
        scheduled = _job_entry("nightly")
        age_h = _hours_since(scheduled.get("last_exit_ts"))
        plist_mtime = scheduled.get("plist_mtime")
        if age_h is None and isinstance(plist_mtime, (int, float)):
            age_h = (datetime.now(timezone.utc).timestamp() - plist_mtime) / 3600
        pending = bool(scheduled.get("plist_loaded")) and age_h is not None and (
            0 <= age_h <= NIGHTLY_STEP_STALE_HOURS
        )
        if not pending:
            out["ok"] = False
            out["reasons"].append(
                "expected scheduled nightly log is missing without usable recent first-run evidence"
            )
    return out
