# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Scheduled memory/graph jobs — thin wrappers around legacy consolidate/graphify scripts.

Khipu owns the launchd labels and CLI entrypoints; the engines stay unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu.paths import DEFAULT_DIR, ensure_data_dir

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
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)


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
    if script is None or not script.is_file():
        # Record the failure so doctor/status do not keep reporting the last
        # good exit after the script path stops resolving.
        _write_job_state(state_name, 2)
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
        proc = subprocess.run(
            cmd,
            stdout=out_f,
            stderr=err_f,
            env=os.environ.copy(),
        )
    _write_job_state(state_name, proc.returncode)
    return proc.returncode


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
    steps: list[dict[str, Any]] = []
    rc = _run_script(
        CONSOLIDATE_NIGHTLY, log_stem="khipu-nightly", state_name="nightly"
    )
    _record_nightly_step(steps, "consolidate_nightly", ok=(rc == 0), counts={"rc": rc})
    _record_nightly_step(steps, "notes_reconcile", **_step_result(_reconcile_notes_if_due()))
    _record_nightly_step(steps, "embed_backfill", **_step_result(_embed_backfill()))
    _record_nightly_step(steps, "query_cache_prune", **_step_result(_prune_query_cache()))
    _record_nightly_step(steps, "commitments_mark_stale", **_step_result(_mark_stale_commitments()))
    _record_nightly_step(steps, "commitments_hygiene", **_step_result(_hygiene_commitments()))
    return rc


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
        _nightly_log(f"[khipu-embed] backfill ok {json.dumps(stats, default=str)[:400]}")
        out = {"ok": True, **stats}
    except Exception as exc:  # noqa: BLE001 — nightly must not fail on this
        _nightly_log(f"[khipu-embed] backfill skipped: {type(exc).__name__}: {exc}")
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


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
        _nightly_log(f"[khipu-hygiene] skipped: {type(exc).__name__}: {exc}")
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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
    "notes_reconcile_ok": (
        "notes_reconcile",
        "run `khipu notes reconcile` and check the error",
    ),
    "embed_provider_ok": (
        "embed_backfill",
        "check the embedding provider key/credentials, then run `khipu jobs install nightly`"
        " or wait for the next nightly",
    ),
    "commitments_hygiene_ok": (
        "commitments_hygiene",
        "run `khipu hygiene commitments` and check the error",
    ),
    "mark_stale_ok": (
        "commitments_mark_stale",
        "run `khipu jobs install nightly` or wait for the next nightly",
    ),
}


# A step whose last recorded run is older than this is treated as "the
# nightly stopped running", not "it hasn't run yet today" — same idea as
# index_freshness's INDEX_SLACK_S, wider because this compares against a
# once-a-day job rather than the index that follows it same-night.
NIGHTLY_STEP_STALE_HOURS = 36


def _hours_since(ts: Any) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def nightly_step_health() -> dict[str, dict[str, Any]]:
    """D1: one doctor-shaped block per nightly step, read from
    `nightly-last.json` (Phase 4) instead of trusting only the legacy
    driver's exit code. Applicable only on the sync host — every other Mac
    never runs the nightly and has nothing of its own to evaluate (same
    posture as `index_freshness`).

    Three states, not two (maintainer, 2026-09-14 — "especially after they
    update"): a step that has never been recorded is SKIPPED, not red — a
    fresh install or a Mac that just updated to this phase's code has
    nothing in `nightly-last.json` yet, and that is not evidence of a
    failure, only of a nightly that hasn't run since this started being
    written. A step recorded `ok: false` is red with its own error/fix. A
    step whose last recording is older than `NIGHTLY_STEP_STALE_HOURS` is
    red too, REGARDLESS of that recording's own `ok` — a nightly that
    stopped running altogether is exactly what a same-day `ok: true` from a
    week ago would otherwise hide."""
    applicable = _is_index_sync_host()
    data = nightly_last()
    by_name: dict[str, dict[str, Any]] = {}
    if data and isinstance(data.get("steps"), list):
        for entry in data["steps"]:
            if isinstance(entry, dict) and entry.get("name"):
                # Steps are appended in run order; the last write for a name
                # (the most recent nightly's outcome) is what doctor judges.
                by_name[entry["name"]] = entry
    source = str(_nightly_last_path())
    out: dict[str, dict[str, Any]] = {}
    for doctor_key, (step_name, fix) in _NIGHTLY_STEP_CHECKS.items():
        if not applicable:
            out[doctor_key] = {
                "ok": True, "applicable": False,
                "note": "not the nightly host — evaluated on the sync host",
            }
            continue
        entry = by_name.get(step_name)
        if entry is None:
            out[doctor_key] = {
                "ok": True, "applicable": True, "skipped": True,
                "reason": "not checked yet — the nightly runs at 02:05",
                "source": source,
            }
            continue
        age_h = _hours_since(entry.get("ts"))
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
            "fix": None if ok else fix,
            "ts": entry.get("ts"),
            "source": source,
        }
    return out


def skipped_step_names(health: dict[str, dict[str, Any]]) -> list[str]:
    """The base name (``key`` minus its ``_ok`` suffix) of every
    `nightly_step_health()` entry marked `skipped` — doctor folds these into
    its `not_configured` list so healthRows.tsx renders them with the same
    grey "not set up" row `memory_root`/`graph_sqlite` already use, instead
    of a false red for a step that simply hasn't run yet."""
    return [key[: -len("_ok")] for key, v in health.items() if v.get("skipped")]


def run_monthly(*, dry_run: bool = False) -> int:
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
    if rc == 0:
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
        last_ok = graph_backup._last_ok_time("graph_snapshot_offsite")
        now = datetime.now(timezone.utc)
        if not graph_backup.offsite_due(last_ok=last_ok, now=now):
            return
        out = graph_backup.run_offsite()
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


def _job_entry(name: str) -> dict[str, Any]:
    spec = _JOB_SPECS[name]
    out_log, err_log = _log_paths(spec["log_stem"])
    last_run_mtime: float | None = None
    try:
        if out_log.is_file():
            last_run_mtime = out_log.stat().st_mtime
    except OSError:
        pass
    state = _read_job_state(name)
    last_exit = state.get("exit") if state else None
    # Lazy import: launchd_gen imports from this module (_JOB_SPECS,
    # _plist_path, etc.), so importing it at module scope here would be a
    # circular import. Deferring it into the function body breaks the cycle
    # at the cost of one extra import per call.
    try:
        from khipu.launchd_gen import plist_current as _plist_current

        current = _plist_current(name)
    except Exception:
        current = None
    return {
        "plist_label": spec["plist"],
        "log_path": str(out_log),
        "err_log_path": str(err_log),
        "last_run_mtime": last_run_mtime,
        "last_run_iso": (
            datetime.fromtimestamp(last_run_mtime, tz=timezone.utc).isoformat()
            if last_run_mtime is not None
            else None
        ),
        "plist_loaded": _plist_loaded(spec["plist"]),
        "plist_current": current,
        "next_schedule": spec["schedule"],
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
    return out
