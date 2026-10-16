# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu CLI entry — status / search / graph / doctor / regen-memory / reconcile."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from khipu.search_text import search_tokens
from khipu.output_guard import guard_output, remember


def _env(*names: str, default: str = "") -> str:
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return default


# Pre-existing bug fixed in passing (Phase 1, 2026-09-05): this was defined
# only as a local inside build_parser(), so cmd_jobs()'s own reference to it
# was an unconditional NameError on every `khipu jobs install/refresh/
# uninstall <name>` call with explicit names — a different function's scope
# does not see a parser-builder's locals. Module level so both see it.
_JOBS_CHOICES = ("nightly", "monthly", "graph_build", "notes_watch", "queue_drain", "recall_daemon", "replica_refresh")


def _memory_root_default() -> str | None:
    """argparse default for --memory-root: env → config.json → None.

    Resolved lazily so importing this module never touches the config file, and
    so a fresh install (no legacy file wiki) gets None rather than a path that
    exists only on the machine this was first written on.
    """
    from khipu.config import path_setting

    v = path_setting("memory_root")
    return str(v) if v else None


def _require_memory_root(args: argparse.Namespace) -> Path | None:
    """The legacy memory tree, or a JSON error on stdout and None.

    Commands that read or write the file wiki cannot do anything useful without
    it, so they fail loudly rather than walk a nonexistent directory and report
    zero of everything.
    """
    v = getattr(args, "memory_root", None)
    if v:
        return Path(v)
    print(
        json.dumps(
            {
                "ok": False,
                "error": "memory_root is not configured",
                "fix": "khipu config --set memory_root /path/to/memory/conversations "
                "(or export KHIPU_MEMORY_ROOT)",
            }
        )
    )
    return None


def _add_paths() -> None:
    from khipu.paths import repo_root

    root = repo_root()
    for p in (root / "packages" / "cli", root / ".python_libs"):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)



def _strict_json(value, **kw) -> str:
    """JSON the desktop's serde_json parses whatever the text holds: ASCII,
    no lone surrogate escape (khipu.wire)."""
    from khipu import wire

    return wire.dumps(value, ensure_ascii=True, **kw)


def cmd_status(args: argparse.Namespace) -> int:
    from khipu.drift import status_payload
    from khipu.hub_snapshot import (
        hub_connection_failed,
        snapshot_freshness,
        status_payload_snapshot,
    )

    mem = Path(args.memory_root) if args.memory_root else None
    try:
        data = status_payload(
            mem,
            conflict_sample=int(args.sample),
            include_drift=bool(args.drift),
        )
        # Do not dump hub_snapshot here. Status is the tab people click; a full
        # embedding replica over Tailscale froze the desktop. Doctor (throttled)
        # and `khipu snapshot refresh` own the dump. snapshot_freshness (W2.4)
        # is still cheap — it only compares two timestamps already in hand.
        data["hub_snapshot"] = snapshot_freshness(
            data.get("latest_ingested_at"), lag=data.pop("replica_lag", None))
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        data = status_payload_snapshot()
        if mem:
            data["status_error"] = f"{type(exc).__name__}: {exc}"
    # W6.2: recall-quality ratios, same block `khipu doctor` reports. Wrapped
    # separately so a metrics-query failure never blanks the rest of status.
    try:
        from khipu.drift import recall_quality

        data["recall_quality"] = recall_quality(hub_snapshot=data.get("hub_snapshot"))
    except Exception as exc:  # noqa: BLE001
        data["recall_quality"] = {"error": f"{type(exc).__name__}: {exc}"}
    # Bundle seal: `na` off a maintainer checkout, red the moment a signed
    # .app has gained a file it shouldn't have (the 0.3.15 incident). Cheap
    # (na short-circuits before spawning codesign) so it's fine on the status
    # tab, not just doctor.
    try:
        from khipu import bundle_seal

        data["bundle_seal"] = bundle_seal.check()
    except Exception as exc:  # noqa: BLE001
        data["bundle_seal"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(_strict_json(data, indent=2, default=str))
    return 0


def cmd_revisions(args: argparse.Namespace) -> int:
    from khipu.revisions import conflict_report, recent_revisions, revision_for_id

    if args.show is not None:
        row = revision_for_id(args.show)
        if row is None:
            print(f"error: revision {args.show} not found", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, default=str))
        return 0

    mem = _require_memory_root(args)
    if mem is None:
        return 2
    out = {
        "conflicts": conflict_report(mem, sample=args.sample),
        "recent": recent_revisions(limit=args.limit, slug=args.slug),
    }
    print(json.dumps(out, indent=2, default=str))
    # Exit 2 only on active file↔pg drift — multi_revision alone is healthy LWW archive
    return 0 if out["conflicts"]["ok"] else 2


def _synth_provider_block(secrets: dict) -> tuple[dict, bool]:
    """The summariser provider row doctor reports: which provider and model,
    whether its key (or, for a CLI provider, its binary) is there, and what a
    failed window falls back to. Presence only, never key material."""
    from khipu.models import CLI_PROVIDERS, synth_settings, cloud_model_id

    _synth = synth_settings()
    _provider = _synth["provider"]
    _key_present = (_provider != "local" and any(secrets.get(f"{_provider}_{field}")
                    for field in ("in_keychain", "env", "file_key_present")))
    synth_provider = {"provider": _provider,
                      "model": _synth["model_id"] if _provider == "local" else cloud_model_id(_synth),
                      "key_present": bool(_key_present)}
    synth_provider_ok = _provider == "local" or bool(_key_present)
    if _provider in CLI_PROVIDERS:
        from khipu.synth_cli import binary

        synth_provider.update(key_present=None, auth="subscription",
                              binary=binary(_provider.removesuffix("-cli")))
        synth_provider_ok = synth_provider["binary"] is not None
    # What a failed window does on a provider other than Gemini (Settings
    # shows and sets it): a retry on Gemini needs a Gemini key to run.
    from khipu.config import choice_setting

    _fallback, _fallback_source = choice_setting("synth_fallback")
    synth_provider["fallback"] = {
        "setting": _fallback, "source": _fallback_source,
        "gemini_key": any(secrets.get(f"gemini_{field}")
                          for field in ("in_keychain", "env", "file_key_present")),
    }
    return synth_provider, synth_provider_ok


# What `doctor --quick` imports: the modules every hook, job and the MCP
# server load, plus the third-party packages they need. A module missing
# under the launchd environment shows here instead of at the next 06:05Z run.
_QUICK_IMPORTS = (
    "khipu.config", "khipu.db", "khipu.keychain", "khipu.session_capture",
    "khipu.hook_background", "khipu.capture", "khipu.extract", "khipu.embed",
    "khipu.recall_prompt", "khipu.recall_rule", "khipu.recall_daemon",
    "khipu.hub_snapshot", "khipu.forget", "khipu.mcp_server", "khipu.jobs",
    "psycopg",
)


# The `*_ok` keys of the checks that read only this Mac. Plain `doctor` and
# `doctor --quick` both compute them with `_doctor_local_checks` and both gate
# on every one; --quick leaves out only the hub, network and probe checks.
LOCAL_GATES = (
    "outbox_ok", "capture_liveness_ok", "git_sync_ok", "dsn_file_ok", "synth_provider_ok",
    "jobs_ok", "index_freshness_ok", "synth_budget_ok", "graph_backup_ok", "bundle_seal_ok",
    "prompt_recall_snapshot_ok", "prompt_recall_outcomes_ok", "recall_daemon_ok",
    "nightly_steps_ok", "degraded_rate_ok", "launchers_ok", "ledger_outbox_ok",
)


def _doctor_local_checks(mem: Path | None) -> dict:
    """Every doctor check that needs nothing but this Mac: no hub, no
    network, no probe. Returns each check's block and its ``*_ok`` (the keys
    in ``LOCAL_GATES``), plus ``not_configured`` names it found."""
    from khipu.keychain import secrets_status

    not_configured: list[str] = []
    # Outbox: a queued capture is something PG does not have yet — red until
    # an explicit `khipu outbox drain` clears it. Doctor may maintain local
    # diagnostic cache/credential state, but never replays this queue.
    try:
        from khipu.outbox import status as outbox_status

        outbox = outbox_status()
    except Exception as e:  # noqa: BLE001
        outbox = {"pending": -1, "error": f"{type(e).__name__}: {e}"}
    # D6: pending==0 alone missed a job that keeps failing to enqueue-then-
    # drain and sits there forever with pending temporarily 0 between
    # attempts; oldest_age_s (renamed here to the name the fix message uses)
    # is the age signal that was missing. Red only past an hour — draining
    # takes a few minutes normally, an hour is evidence something is stuck.
    outbox["oldest_pending_seconds"] = outbox.get("oldest_age_s")
    outbox_oldest_ok = (outbox.get("oldest_pending_seconds") or 0) <= 3600
    outbox_ok = outbox.get("pending") == 0 and not outbox.get("dead") and outbox_oldest_ok
    if not outbox_oldest_ok:
        outbox["fix"] = "run `khipu sessions drain`"
    # Capture liveness: is each harness ACTUALLY being recorded? Red on evidence
    # of a failure (hook error, drain failure, stale queue, cadence never
    # firing) — never on idleness. maintainer, 2026-08-17: a session that captured
    # only at compaction looked healthy on every other check here. (Plain
    # doctor adds the gateway rows, which need the network.)
    try:
        from khipu.session_capture import liveness_all

        liveness = liveness_all()
    except Exception as e:  # noqa: BLE001
        liveness = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "red": [],
            "harnesses": {},
        }
    # Git sync: is the memory tree's nightly auto-sync landing on GitHub? It is
    # soft-failed inside the nightly by design, so until this it could die and
    # every light stayed green (state-of-play 2026-08-17 item 7). Judged from
    # the heartbeat git_sync.py writes plus local repo evidence; not applicable
    # (green) on any Mac that does not run the nightly.
    try:
        from khipu.git_sync_health import status as git_sync_status

        git_sync = git_sync_status()
    except Exception as e:  # noqa: BLE001
        git_sync = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "reasons": ["check failed"],
        }
    try:
        secrets = secrets_status()
    except Exception as e:  # noqa: BLE001 — malformed local diagnostics must not crash doctor
        secrets = {"error": f"{type(e).__name__}: {e}", "dsn_file": {"ok": False}}
    # The file DSN is what every sandboxed harness falls back to when the
    # Keychain is denied. It went stale on 2026-08-04 and nothing noticed until
    # 2026-08-18, because every check ran where the Keychain worked.
    dsn_file_ok = bool((secrets.get("dsn_file") or {}).get("ok", True))
    try:
        synth_provider, synth_provider_ok = _synth_provider_block(secrets)
    except Exception as e:  # noqa: BLE001
        synth_provider, synth_provider_ok = {"error": f"{type(e).__name__}: {e}"}, False
    try:
        from khipu.jobs import index_freshness, job_status

        jobs = job_status()
        index_fresh = index_freshness(memory_root=mem)
        index_freshness_ok = bool(index_fresh.get("ok"))
    except Exception as e:  # noqa: BLE001
        jobs = {"error": f"{type(e).__name__}: {e}"}
        index_fresh = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        index_freshness_ok = False
    known_job_failures = [
        name for name in _JOBS_CHOICES
        if isinstance(jobs.get(name), dict) and jobs[name].get("last_exit") not in (None, 0)
        and (jobs[name].get("plist_loaded") or jobs[name].get("plist_mtime") is not None
             or jobs[name].get("expected"))
    ]
    jobs_ok = not known_job_failures
    # The summariser's daily call ceiling (embed scope "synth"). Red while it
    # is spent: captures wait in the queue until UTC midnight, or until the
    # owner lifts it (`khipu budget lift synth`). `warn` is the 80% notice and
    # never gates. Today's tokens and estimated cost ride along.
    try:
        from khipu.embed import budget_status

        synth_budget = budget_status("synth")
        synth_budget_ok = not synth_budget.get("exhausted")
    except Exception as e:  # noqa: BLE001
        synth_budget = {"error": f"{type(e).__name__}: {e}"}
        synth_budget_ok = False
    try:
        from khipu.extract import synth_usage_today

        _usage = synth_usage_today()
        synth_budget["tokens_today"] = {k: _usage[k] for k in
                                        ("prompt_tokens", "output_tokens", "thought_tokens")}
        synth_budget["estimated_usd_today"] = _usage["estimated_usd"]
        synth_budget["usage_by_provider"] = _usage["by_provider"]
    except Exception as e:  # noqa: BLE001 — accounting is informational
        synth_budget["tokens_today"] = None
        synth_budget["estimated_usd_today"] = None
        synth_budget["usage_error"] = f"{type(e).__name__}: {e}"
    try:
        from khipu import graph_backup

        graph_backup_block = graph_backup.local_health()
    except Exception as e:  # noqa: BLE001
        graph_backup_block = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # Bundle seal: red the moment a signed .app has gained a file it shouldn't
    # have — this is the tripwire the 0.3.15 "Khipu is damaged" incident
    # needed (see khipu.bundle_seal). `na` (maintainer checkout, no codesign)
    # counts as ok so it never fails a machine the check does not apply to.
    try:
        from khipu import bundle_seal

        bundle_seal_block = bundle_seal.check()
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        bundle_seal_block = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # R1 follow-up: khipu.recall_prompt (the UserPromptSubmit hook) searches
    # this local replica first and only falls back to the hub when it is
    # missing or older than 24h — a degrade that is otherwise invisible.
    # Never red on its own (the fallback IS the documented safe behaviour);
    # this only surfaces WHY prompt-time recall might be slow right now.
    try:
        from khipu.hub_snapshot import prompt_recall_snapshot_status

        prompt_recall_snapshot = prompt_recall_snapshot_status()
    except Exception as e:  # noqa: BLE001
        prompt_recall_snapshot = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # Phase 0 session C (finding B10): prompt_recall_snapshot above only
    # tests replica freshness — it says nothing about whether the lane
    # actually answers within its own budget. This reads the hook's own
    # outcome log for the real timeout rate, the evidence that was missing
    # while the lane silently discarded results on 85% of prompts.
    try:
        from khipu.recall_prompt import prompt_recall_outcomes

        prompt_recall_outcomes_block = prompt_recall_outcomes()
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        prompt_recall_outcomes_block = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # The warm recall service behind the per-prompt hook. Not running is fine
    # (the hook falls back to the one-shot path); running on different code
    # than the disk for over a minute is not — it should have restarted itself.
    try:
        from khipu.recall_daemon import daemon_health

        recall_daemon_block = daemon_health()
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        recall_daemon_block = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # D1: per-step nightly evidence — notes reconcile, embed backfill,
    # commitments hygiene and mark-stale each get their own red/green
    # instead of only the legacy driver's exit code (`jobs.ok` above).
    try:
        from khipu.jobs import nightly_step_health

        nightly_steps = nightly_step_health()
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        err = f"{type(e).__name__}: {e}"
        nightly_steps = {
            k: {"ok": False, "applicable": True, "error": err}
            for k in ("notes_reconcile_ok", "embed_provider_ok",
                      "commitments_hygiene_ok", "mark_stale_ok")
        }
    # A step that has never been recorded is skipped, not red — a fresh
    # install or a Mac that just updated has nothing in nightly-last.json
    # yet, which is not evidence of a failure (maintainer, 2026-09-14:
    # "especially after they update"). Reuses the same not_configured /
    # grey-row convention memory_root and graph_sqlite already use.
    from khipu.jobs import skipped_step_names

    not_configured.extend(skipped_step_names(nightly_steps))
    try:
        from khipu import query_log

        degraded_rate = query_log.degraded_rate()
    except Exception as e:  # noqa: BLE001
        degraded_rate = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # The five launcher links, read-only. `consistent`
    # (mixed roots — a stray worktree still live) is a warning, never a gate;
    # `launchers_ok` (a pack's own config pointed at a dangling/not_a_link
    # launcher) is a gate, same as every other check below.
    try:
        from khipu import integrations as integ

        launchers = integ.launcher_health()
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        launchers = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # The ledger's own outbox: a queued ledger write is one the hub does not
    # have yet. Red while it holds anything, as the capture outbox is.
    try:
        from khipu.ledger import outbox_status as ledger_outbox_status

        ledger_outbox = ledger_outbox_status()
    except Exception as e:  # noqa: BLE001  (a failed check must not look like a pass)
        ledger_outbox = {"pending": -1, "error": f"{type(e).__name__}: {e}"}
    ledger_outbox_ok = ledger_outbox.get("pending") == 0 and not ledger_outbox.get("refused")
    return {
        "outbox": outbox, "outbox_ok": outbox_ok,
        "ledger_outbox": ledger_outbox, "ledger_outbox_ok": ledger_outbox_ok,
        "capture_liveness": liveness, "capture_liveness_ok": bool(liveness.get("ok")),
        "git_sync": git_sync, "git_sync_ok": bool(git_sync.get("ok")),
        "secrets": secrets, "dsn_file_ok": dsn_file_ok,
        "synth_provider": synth_provider, "synth_provider_ok": synth_provider_ok,
        "jobs": jobs, "job_failures": known_job_failures, "jobs_ok": jobs_ok,
        "index_freshness": index_fresh, "index_freshness_ok": index_freshness_ok,
        "synth_budget": synth_budget, "synth_budget_ok": synth_budget_ok,
        "graph_backup": graph_backup_block, "graph_backup_ok": bool(graph_backup_block.get("ok")),
        "bundle_seal": bundle_seal_block, "bundle_seal_ok": bool(bundle_seal_block.get("ok")),
        "prompt_recall_snapshot": prompt_recall_snapshot,
        "prompt_recall_snapshot_ok": bool(prompt_recall_snapshot.get("ok")),
        "prompt_recall_outcomes": prompt_recall_outcomes_block,
        "prompt_recall_outcomes_ok": bool(prompt_recall_outcomes_block.get("ok")),
        "recall_daemon": recall_daemon_block, "recall_daemon_ok": bool(recall_daemon_block.get("ok")),
        "nightly_steps": nightly_steps,
        "nightly_steps_ok": all(bool(v.get("ok")) for v in nightly_steps.values()),
        "degraded_rate": degraded_rate, "degraded_rate_ok": bool(degraded_rate.get("ok")),
        "launchers": launchers, "launchers_ok": bool(launchers.get("ok")),
        "not_configured": not_configured,
    }


def _promotion_block() -> dict:
    """A promotion waiting for the hub (khipu.promote.queued), for the app.
    Never gates ``ok``."""
    try:
        from khipu.promote import queued

        return {"queued": queued()}
    except Exception as e:  # noqa: BLE001
        return {"queued": None, "error": f"{type(e).__name__}: {e}"}


def _cmd_doctor_quick(args: argparse.Namespace) -> int:
    """`khipu doctor --quick`: imports, config, DSN presence and every local
    check plain doctor gates on (``LOCAL_GATES``: outbox, capture liveness,
    launchers, bundle seal, jobs, summariser provider and the rest); it skips
    only the hub, network and probe checks. The promotion gate runs it under
    a candidate's launchd environment (khipu.promote)."""
    import importlib

    imports: dict[str, str] = {}
    for name in _QUICK_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 — a failed import is the finding
            imports[name] = f"{type(e).__name__}: {e}"
    try:
        from khipu.config import config_file, load_config

        load_config()
        config = {"ok": True, "path": str(config_file())}
    except Exception as e:  # noqa: BLE001
        config = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    mem = Path(args.memory_root) if getattr(args, "memory_root", None) else None
    local = _doctor_local_checks(mem)
    from khipu import __version__

    out = {
        "quick": True,
        "version": __version__,
        "imports": {"ok": not imports, "failed": imports, "checked": len(_QUICK_IMPORTS)},
        "config": config,
        **local,
        "dsn_present": bool(local["secrets"].get("dsn")) and not local["secrets"].get("error"),
        "skipped": "hub, network and probe checks (run plain `khipu doctor` for those)",
    }
    # The one hub read --quick makes: the code this gate is about to promote
    # may ship a migration the hub has not had. An unreachable hub is not
    # gated here (--quick skips reachability); a pending migration is.
    from khipu.migrate import doctor_check as _schema_check

    schema = _schema_check()
    out["schema_migrations"] = schema
    out["schema_migrations_ok"] = not schema.get("pending")
    out["warnings"] = list(schema.get("warnings") or [])
    out["ok"] = (not imports and bool(config.get("ok")) and out["dsn_present"]
                 and all(bool(local[k]) for k in LOCAL_GATES) and out["schema_migrations_ok"])
    print(_strict_json(out, indent=2, default=str))
    return 0 if out["ok"] else 2


def cmd_doctor(args: argparse.Namespace) -> int:
    if getattr(args, "quick", False):
        return _cmd_doctor_quick(args)
    from khipu.drift import backup_health, sample_drift, status_payload

    mem = Path(args.memory_root) if args.memory_root else None
    # The three checks that wait longest on the hub (file-vs-hub drift, the
    # graph mirror's drift, embedding coverage with its topic lag) and the
    # checks that read only this Mac run in worker threads, the hub ones on
    # their own connections, while the rest of doctor goes on; each answer is
    # what it was when they ran one after another (58 s end to end on
    # 2026-10-15, most of it waiting on the network).
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="khipu-doctor")
    try:
        workers = _doctor_start_workers(pool, args, mem, sample_drift)
        return _cmd_doctor_body(args, workers, mem, status_payload, backup_health)
    finally:
        pool.shutdown(wait=True)


def _doctor_sample_drift(sample_drift, mem: Path, sample) -> dict:
    # Every other check is wrapped; this one was not, so a single unreadable
    # topic file aborted the entire health report with a traceback instead of
    # reporting red (audit 2026-08-17).
    try:
        return sample_drift(mem, sample=sample)
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        return {
            "error": f"{type(e).__name__}: {e}",
            "episodes_missing_in_pg": -1,
            "topic_mismatches": [],
            "topic_files_unreadable": [],
        }


def _doctor_graph_drift() -> dict:
    try:
        from khipu.graph_sync import graph_drift

        return graph_drift()
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _doctor_embed_checks() -> tuple[dict, bool, dict]:
    """``(embed_coverage, embed_coverage_ok, topics_lag)``, in that order on
    one thread: the lag reuses the gaps coverage kept."""
    memory_gaps_kept: dict = {}
    try:
        from khipu.embed import coverage

        # The chunk plan is built from this Mac's fingerprint cache (the same
        # answer as re-reading every row's text: only rows whose version moved
        # are re-read) and kept for topics_embed_lag below, so one doctor run
        # plans the memory space once instead of reading ~28 MB of text twice.
        embed_coverage = coverage(detail=True, cached_plan=True, keep_gaps=memory_gaps_kept)
        embed_coverage_ok = (
            embed_coverage.get("episodes", {}).get("missing", 0) == 0
            and embed_coverage.get("topics", {}).get("missing", 0) == 0
            and embed_coverage.get("missing", 0) == 0
            and embed_coverage.get("stale", 0) == 0
        )
    except Exception as e:  # noqa: BLE001
        embed_coverage = {"error": f"{type(e).__name__}: {e}"}
        embed_coverage_ok = False
    # D6/F2/F4: topic-embedding lag and the search-degrade rate — both were
    # buried keys nothing aggregated before this (audit 2026-08-17 class).
    try:
        from khipu.embed import topics_embed_lag_minutes

        topics_lag = topics_embed_lag_minutes(gaps=memory_gaps_kept)
    except Exception as e:  # noqa: BLE001
        topics_lag = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return embed_coverage, embed_coverage_ok, topics_lag


def _doctor_start_workers(pool, args, mem, sample_drift) -> dict:
    workers: dict = {
        "drift": pool.submit(_doctor_sample_drift, sample_drift, mem, args.sample)
        if mem is not None else None,
        "graph": None,
        "graph_configured": None,
        "graph_error": None,
    }
    try:
        from khipu.config import path_setting

        workers["graph_configured"] = path_setting("graph_sqlite") is not None
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        workers["graph_error"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if workers["graph_configured"]:
        workers["graph"] = pool.submit(_doctor_graph_drift)
    workers["embed"] = pool.submit(_doctor_embed_checks)
    # Every check that reads only this Mac; `doctor --quick` gates on the same
    # set (khipu.promote's first gate step).
    workers["local"] = pool.submit(_doctor_local_checks, mem)
    from khipu.migrate import doctor_check as _schema_check

    workers["schema"] = pool.submit(_schema_check)
    return workers


def _cmd_doctor_body(args, workers, mem, status_payload, backup_health) -> int:
    from khipu.hub_snapshot import (
        hub_connection_failed,
        snapshot_freshness,
        status_payload_snapshot,
    )

    drift_future, graph_future, embed_future, local_future = (
        workers["drift"], workers["graph"], workers["embed"], workers["local"])
    graph_configured, graph_error = workers["graph_configured"], workers["graph_error"]
    hub_ok = True
    try:
        status = status_payload(None)
    except Exception as exc:
        if hub_connection_failed(exc):
            hub_ok = False
            status = status_payload_snapshot()
            status["hub_error"] = f"{type(exc).__name__}: {exc}"
        else:
            raise
    # Replica freshness phase 1: per-table lag while the hub answers; with it
    # down the state is "unknown" (hub_ok is already red, more specifically)
    # and the last measurement rides along.
    hub_snapshot = snapshot_freshness(
        status.get("latest_ingested_at") if hub_ok else None,
        lag=status.pop("replica_lag", None),
    )
    # A check whose input is not configured on this machine is SKIPPED and
    # named in `not_configured` — distinct from red (configured but broken)
    # and from green (checked and clean). A fresh install has no legacy file
    # wiki, so drift against it is inapplicable, not passing.
    not_configured: list[str] = []
    if mem is None:
        not_configured.append("memory_root")
        drift = {
            "skipped": "memory_root not configured",
            "episodes_missing_in_pg": 0,
            "topic_mismatches": [],
            "topic_files_unreadable": [],
        }
    backup = backup_health()
    try:
        from khipu import graph_backup

        _graph_offsite = graph_backup.offsite_health()
    except Exception as e:  # noqa: BLE001
        _graph_offsite = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # --probe captures to the hub and then searches for it, so the hub checks
    # finish first, as they did when everything ran in order: a probe row
    # must never show up in their answers.
    if getattr(args, "probe", False):
        for fut in (drift_future, graph_future, embed_future):
            if fut is not None:
                fut.exception()
    # Informational: per-library coverage and profile, beside embed coverage.
    # Never gates status (a library mid-backfill is not a broken hub).
    try:
        from khipu.library import doctor_block as _library_block

        libraries = _library_block()
    except Exception as e:  # noqa: BLE001
        libraries = {"error": f"{type(e).__name__}: {e}"}
    # R8 (Phase 1 stop condition): the literal-search trigram migration
    # degrades to a no-op when pg_trgm cannot be created — that is a SKIP,
    # never red. Only a genuinely half-applied hub (extension present, an
    # index missing) turns this red.
    try:
        from khipu.embed import literal_trgm_status

        literal_trgm = literal_trgm_status()
    except Exception as e:  # noqa: BLE001
        literal_trgm = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # --probe runs a fresh end-to-end capture-then-search probe. Plain doctor
    # reads its last result; diagnostic helpers may maintain local cache or
    # credential state but never replay a queue or refresh the hub replica.
    recall_probe = None
    if getattr(args, "probe", False):
        try:
            from khipu import probe

            probe_harness = getattr(args, "harness", None) or _env("KHIPU_HARNESS", default="doctor")
            current_probe = probe.run_probe(probe_harness)
            recall_probe = {
                "ok": bool(current_probe.get("ok")),
                "reason": current_probe.get("reason") or current_probe.get("error"),
                "last_probe": current_probe,
                "age_seconds": 0,
                "harnesses": probe.harness_results(),
            }
        except Exception as e:  # noqa: BLE001 — a probe crash must become a red result
            recall_probe = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    if recall_probe is None:
        try:
            from khipu import probe

            recall_probe = probe.status()
        except Exception as e:  # noqa: BLE001
            recall_probe = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    # W6.2: recall-quality ratios (fragmentation, dangling topics, junk paths,
    # commitments, query zero-result rate). Surfaced for visibility; per the
    # scope these only warn and never gate doctor's overall ok below.
    try:
        from khipu.drift import recall_quality

        recall_quality_block = recall_quality(hub_snapshot=hub_snapshot)
    except Exception as e:  # noqa: BLE001
        recall_quality_block = {"error": f"{type(e).__name__}: {e}"}
    # P5: the last organisation run (index rewrite, size warnings, stale
    # report) — info only, never gates `ok` below. None (not red) when
    # organisation has never run on this Mac yet.
    try:
        from khipu import organise

        notes_organise = organise.last_run()
    except Exception as e:  # noqa: BLE001 — a failed check must not look like a pass
        notes_organise = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # The last migration run's table lock hold times: info only, never gates
    # `ok`. None when no migration that locks tables has run on this Mac.
    try:
        from khipu import migrate as _migrate

        notes_migrate = _migrate.last_lock_notes()
    except Exception as e:  # noqa: BLE001
        notes_migrate = {"error": f"{type(e).__name__}: {e}"}
    # K9: gateway per-token liveness, read over the network from the
    # deployed gateway's own /healthz (never deployed by doctor itself).
    # Applicable only when a gateway_url is configured.
    try:
        from khipu.integrations import gateway_liveness_check

        gw_liveness = gateway_liveness_check()
    except Exception as e:  # noqa: BLE001
        gw_liveness = {"ok": False, "applicable": True, "error": f"{type(e).__name__}: {e}"}
    # Aegis-specific gateway round trip, same K9 evidence-must-arrive discipline
    # but keyed on the bearer AEGIS ITSELF resolves (env or the on-disk token
    # file, never Keychain — its sandbox cannot reach it). Folded into the
    # aegis harness row, not just the cross-harness "gateway" row above,
    # because this is what actually explains a 401'ing Aegis recall while the
    # hook-liveness row stays green (2026-09-14 gap: this evidence never
    # arrived, so both this row and `verify aegis` stayed green with no
    # bearer ever staged).
    try:
        from khipu.integrations import aegis_gateway_check

        aegis_gw = aegis_gateway_check()
    except Exception as e:  # noqa: BLE001
        aegis_gw = {"ok": False, "applicable": True, "error": f"{type(e).__name__}: {e}"}
    # K8: a heartbeat file under the dispatch dir from a harness Khipu does
    # not recognise — a warning (never a hard red: the hook that wrote it may
    # still be working fine, it is just unidentified).
    try:
        from khipu.session_capture import unknown_harness_heartbeats

        unknown_harness = unknown_harness_heartbeats()
    except Exception as e:  # noqa: BLE001
        unknown_harness = {"warnings": [], "error": f"{type(e).__name__}: {e}"}
    # One row per Claude home Khipu found (~/.claude, CLAUDE_CONFIG_DIR, T3's
    # accounts). Visibility only, like a detected-but-uninstalled Cursor.
    try:
        from khipu import integrations as _integ

        claude_homes_block = _integ.claude_homes_report()
    except Exception as e:  # noqa: BLE001
        claude_homes_block = {"homes": [], "error": f"{type(e).__name__}: {e}"}
    # T3's own files are read-only.  It is omitted entirely when no T3
    # settings file exists, so a normal non-T3 install never gets a phantom
    # provider card.  A malformed settings file still counts as detected: its
    # contents can hold credentials, and health needs only its existence.
    try:
        from khipu import t3 as _t3

        t3_health = _t3.health()
    except Exception:  # noqa: BLE001 — doctor must never fail on T3 drift
        t3_health = None
    # The switch registry's states, for visibility only — never gates `ok`.
    try:
        from khipu import features

        features_block = features.states()
    except Exception as e:  # noqa: BLE001
        features_block = {"error": f"{type(e).__name__}: {e}"}
    # Phase 2, session C: candidate supersession links awaiting review —
    # visibility only, same posture as features_block above; never gates `ok`.
    try:
        from khipu import decisions as _decisions

        decision_links_block = _decisions.candidate_link_count()
    except Exception as e:  # noqa: BLE001 — a failed check must not crash doctor
        decision_links_block = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # The worker threads' answers, joined in the order their not_configured
    # names and liveness rows used to be added.
    local = local_future.result()
    not_configured.extend(local.pop("not_configured"))
    _graph_backup = local["graph_backup"]
    liveness = local["capture_liveness"]
    (outbox, outbox_ok, git_sync, secrets, dsn_file_ok, synth_provider, synth_provider_ok, jobs,
     known_job_failures, jobs_ok, index_fresh, index_freshness_ok, synth_budget, synth_budget_ok,
     prompt_recall_snapshot, prompt_recall_outcomes_block, recall_daemon_block, bundle_seal_block,
     nightly_steps, degraded_rate, launchers) = (local[k] for k in (
        "outbox", "outbox_ok", "git_sync", "secrets", "dsn_file_ok", "synth_provider",
        "synth_provider_ok", "jobs", "job_failures", "jobs_ok", "index_freshness",
        "index_freshness_ok", "synth_budget", "synth_budget_ok", "prompt_recall_snapshot",
        "prompt_recall_outcomes", "recall_daemon", "bundle_seal", "nightly_steps", "degraded_rate",
        "launchers"))
    if drift_future is not None:
        drift = drift_future.result()
    # P2b gate: directional. Every file episode must exist in PG by identity
    # (ts, md5(summary)); PG holding MORE than the file is healthy (hub writes,
    # archived episodes survive the upsert-only reconcile), so the old
    # count-delta tolerance is gone — it could not fail once PG ⊃ file.
    drift_ok = (
        drift.get("episodes_missing_in_pg") == 0
        and len(drift.get("topic_mismatches") or []) == 0
        # A topic we could not read is a topic we did not check; unchecked is
        # not the same as clean.
        and not drift.get("topic_files_unreadable")
        and not drift.get("error")
    )
    # Graph: graphify's graph.sqlite vs PG's graphify-owned rows, both
    # directions (audit 2026-08-17 — PG had silently diverged for two weeks
    # while doctor stayed green because it only looked at episodes/topics).
    # A CONFIGURED-but-missing graph.sqlite is a red result, not a skip: that
    # machine is supposed to be able to see the graph's source. An UNCONFIGURED
    # one is skipped and named.
    if graph_configured is False:
        not_configured.append("graph_sqlite")
        graph = {"ok": True, "skipped": "graph_sqlite not configured"}
    elif graph_future is not None:
        graph = graph_future.result()
    else:
        graph = graph_error
    embed_coverage, embed_coverage_ok, topics_lag = embed_future.result()
    # Pending migrations: red. With the hub down the read fails too; hub_ok
    # is already red for that, so the schema row says why it was not checked.
    schema = workers["schema"].result()
    if not hub_ok and schema.get("error"):
        schema = {**schema, "ok": True, "skipped": "the database did not answer"}
    schema_ok = bool(schema.get("ok"))
    liveness.setdefault("harnesses", {})["gateway"] = gw_liveness
    if not gw_liveness.get("ok", True):
        liveness["red"] = list(dict.fromkeys([*(liveness.get("red") or []), "gateway"]))
        liveness["ok"] = False
    if aegis_gw.get("applicable"):
        aegis_row = liveness.setdefault("harnesses", {}).setdefault("aegis", {"harness": "aegis", "ok": True})
        aegis_row["gateway"] = aegis_gw
        if not aegis_gw.get("ok", True):
            aegis_row["ok"] = False
            liveness["red"] = list(dict.fromkeys([*(liveness.get("red") or []), "aegis"]))
            liveness["ok"] = False
    # Plan usage (docs/plans/2026-10-14-plan-usage.md, phase 2): each account's
    # tightest window still in force, read from the usage cache only (no CLI
    # is probed, nothing waits), and one warning per window at 90% or more.
    # Visibility only: it never gates `ok`.
    try:
        from khipu import usage as _usage

        plan_usage_block = _usage.doctor_block()
    except Exception as e:  # noqa: BLE001 — a failed check must not crash doctor
        plan_usage_block = {"accounts": [], "warnings": [], "error": f"{type(e).__name__}: {e}"}
    # Routing truth (docs/plans/2026-10-14-routing-truth.md, phase 1a): the
    # record's version, each account's line and its warnings. Read from the
    # record only (nothing is probed); visibility only, it never gates `ok`.
    try:
        from khipu import routing as _routing

        routing_block = _routing.doctor_block()
    except Exception as e:  # noqa: BLE001 — a failed check must not crash doctor
        routing_block = {"warnings": [], "error": f"{type(e).__name__}: {e}"}
    # Owner direction (docs/plans/2026-10-16-owner-direction.md, 2.6): turns
    # the prompt hook recorded in the last day per harness. Red when the check
    # is on and a harness with the hook installed recorded none; never gates `ok`.
    try:
        from khipu import owner_turns as _owner_turns

        owner_direction_block = _owner_turns.doctor_block()
    except Exception as e:  # noqa: BLE001 — a failed check must not crash doctor
        owner_direction_block = {"ok": False, "warnings": [], "error": f"{type(e).__name__}: {e}"}
    # The ledger (Work view phase 1): counts per project, removed rows waiting
    # to purge, purged markers, offline copy. Only the outbox gates (above).
    try:
        from khipu import ledger as _ledger

        ledger_block = _ledger.doctor_block()
    except Exception as e:  # noqa: BLE001  (a failed check must not crash doctor)
        ledger_block = {"warnings": [], "error": f"{type(e).__name__}: {e}"}
    # The live view's summary files (Work view phase 3): warns, never gates `ok`.
    try:
        from khipu import work as _work

        work_block = _work.doctor_block()
    except Exception as e:  # noqa: BLE001  (a failed check must not crash doctor)
        work_block = {"warnings": [], "error": f"{type(e).__name__}: {e}"}
    out = {
        "status": status,
        "hub_ok": hub_ok,
        "hub_snapshot": hub_snapshot,
        # Its own row in the app's health report; red only when stale.
        "hub_snapshot_ok": bool(hub_snapshot.get("ok", True)),
        "drift": drift,
        "graph_drift": graph,
        "outbox": outbox,
        "ledger_outbox": local["ledger_outbox"],
        "capture_liveness": liveness,
        "git_sync": git_sync,
        "backup": backup,
        "secrets": secrets,
        "jobs": jobs,
        "job_failures": known_job_failures,
        "index_freshness": index_fresh,
        "embed_coverage": embed_coverage,
        "libraries": libraries,
        "synth_budget": synth_budget,
        "synth_provider": synth_provider,
        "literal_trgm": literal_trgm,
        "prompt_recall_snapshot": prompt_recall_snapshot,
        "prompt_recall_outcomes": prompt_recall_outcomes_block,
        "recall_daemon": recall_daemon_block,
        "recall_probe": recall_probe,
        "recall_quality": recall_quality_block,
        "bundle_seal": bundle_seal_block,
        "notes_organise": notes_organise,
        "notes_migrate": notes_migrate,
        "nightly_steps": nightly_steps,
        "topics_embed_lag": topics_lag,
        "topics_embed_lag_minutes": topics_lag.get("lag_minutes"),
        "degraded_rate": degraded_rate,
        "unknown_harness": unknown_harness,
        "launchers": launchers,
        "claude_homes": claude_homes_block,
        **({"t3": t3_health} if t3_health is not None else {}),
        "features": features_block,
        "decision_links": decision_links_block,
        "plan_usage": plan_usage_block,
        "routing": routing_block,
        "owner_direction": owner_direction_block,
        "ledger": ledger_block,
        "schema_migrations": schema,
        "work": work_block,
        "promotion": _promotion_block(),
        # Warnings that never gate `ok` (plans nearly used up; routing; ledger; work; schema).
        "warnings": (list(plan_usage_block.get("warnings") or []) + list(routing_block.get("warnings") or [])
                     + list(owner_direction_block.get("warnings") or [])
                     + list(ledger_block.get("warnings") or []) + list(work_block.get("warnings") or [])
                     + list(schema.get("warnings") or [])),
        "not_configured": not_configured,
        "ok": (
            hub_ok
            and drift_ok
            and graph.get("ok", False)
            and outbox_ok
            and backup["ok"]
            and bool(liveness.get("ok"))
            and bool(git_sync.get("ok"))
            and dsn_file_ok
            and index_freshness_ok
            and jobs_ok
            and embed_coverage_ok
            and synth_budget_ok
            and synth_provider_ok
            and bool(_graph_backup.get("ok"))
            # graph_offsite_ok was reported but never aggregated, so a stale
            # offsite copy left doctor (and the soak probe reading doctor.ok)
            # green for days (08-28..08-31).
            and bool(_graph_offsite.get("ok"))
            # hub_snapshot["ok"] was computed and displayed but never folded
            # into the aggregate — a snapshot stuck behind ingest by hours
            # still read doctor.ok == true (W6, 2026-09-03).
            and bool(hub_snapshot.get("ok", True))
            # recall_probe_ok: the end-to-end "capture then search finds it"
            # signal (W6.1) — red when no probe has run, the last one failed,
            # or it is older than 7 days. This is a hard gate, not a warning.
            and bool(recall_probe.get("ok"))
            # bundle_seal_ok: red the moment a running .app bundle has gained
            # a file post-signing (0.3.15's root cause). `ok` is true for both
            # a clean signature and `na` (not running from inside a bundle).
            and bool(bundle_seal_block.get("ok"))
            # literal_trgm_ok: true for a clean index set AND for the
            # documented no-op skip (pg_trgm unavailable); false only when
            # the extension exists but an index is actually missing.
            and bool(literal_trgm.get("ok"))
            and bool(prompt_recall_snapshot.get("ok"))
            and bool(prompt_recall_outcomes_block.get("ok"))
            and bool(recall_daemon_block.get("ok"))
            and all(bool(v.get("ok")) for v in nightly_steps.values())
            and bool(topics_lag.get("ok"))
            and bool(degraded_rate.get("ok"))
            # launchers_ok: red only when some pack's own config points at a
            # dangling/not_a_link launcher. An inconsistent root set
            # (`launchers["consistent"]`) is a warning, folded in separately
            # below, and never reaches this aggregate.
            and bool(launchers.get("ok"))
            and local["ledger_outbox_ok"]
            and schema_ok
        ),
        "graph_backup": _graph_backup,
        "graph_backup_ok": bool(_graph_backup.get("ok")),
        "graph_offsite": _graph_offsite,
        "graph_offsite_ok": bool(_graph_offsite.get("ok")),
        "drift_ok": drift_ok,
        "graph_drift_ok": bool(graph.get("ok")),
        "outbox_ok": outbox_ok,
        "capture_liveness_ok": bool(liveness.get("ok")),
        "git_sync_ok": bool(git_sync.get("ok")),
        "backup_ok": backup["ok"],
        "dsn_file_ok": dsn_file_ok,
        "index_freshness_ok": index_freshness_ok,
        "jobs_ok": jobs_ok,
        "embed_coverage_ok": embed_coverage_ok,
        "synth_budget_ok": synth_budget_ok,
        "synth_provider_ok": synth_provider_ok,
        "recall_probe_ok": bool(recall_probe.get("ok")),
        "bundle_seal_ok": bool(bundle_seal_block.get("ok")),
        "literal_trgm_ok": bool(literal_trgm.get("ok")),
        "prompt_recall_snapshot_ok": bool(prompt_recall_snapshot.get("ok")),
        "prompt_recall_outcomes_ok": bool(prompt_recall_outcomes_block.get("ok")),
        "recall_daemon_ok": bool(recall_daemon_block.get("ok")),
        "notes_reconcile_ok": bool(nightly_steps.get("notes_reconcile_ok", {}).get("ok")),
        "embed_provider_ok": bool(nightly_steps.get("embed_provider_ok", {}).get("ok")),
        "commitments_hygiene_ok": bool(nightly_steps.get("commitments_hygiene_ok", {}).get("ok")),
        "mark_stale_ok": bool(nightly_steps.get("mark_stale_ok", {}).get("ok")),
        "topics_embed_lag_ok": bool(topics_lag.get("ok")),
        "degraded_rate_ok": bool(degraded_rate.get("ok")),
        "launchers_ok": bool(launchers.get("ok")),
        "nightly_steps_ok": local["nightly_steps_ok"],
        "ledger_outbox_ok": local["ledger_outbox_ok"],
        "schema_migrations_ok": schema_ok,
    }
    print(_strict_json(out, indent=2, default=str))
    return 0 if out["ok"] else 2


def _escape_like(term: str) -> str:
    """Escape ILIKE metacharacters so user input can't act as a wildcard (F7).

    Backslash first (it's the escape char itself), then the two ILIKE wildcards.
    Paired with ``ESCAPE '\\'`` at each call site.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fair_shares(total: int, n: int) -> list[int]:
    """Split `total` into `n` non-negative shares, remainder to the first ones.

    Pure arithmetic (no DB) so per-kind fairness (F7) is unit testable (F8).
    """
    total = max(0, int(total))
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def _clip_search_row(r) -> dict:
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    return {
        "kind": r[0],
        "id": r[1],
        "label": clip_snippet(r[2], LABEL_LIMIT),
        "snippet": clip_snippet(r[3], SNIPPET_LIMIT),
    }


def _token_match_sql(columns: tuple[str, ...], n: int) -> tuple[str, str]:
    """WHERE any-token-hits and ORDER BY how-many-tokens-hit.

    ``n`` is the token count already in the bound params ``t0``..``t{n-1}``.
    """
    wheres: list[str] = []
    scores: list[str] = []
    for i in range(n):
        ors = " OR ".join(f"{c} ILIKE %(t{i})s ESCAPE '\\'" for c in columns)
        wheres.append(f"({ors})")
        scores.append(f"(CASE WHEN {ors} THEN 1 ELSE 0 END)")
    return " OR ".join(wheres), " + ".join(scores)


def _ilike_token_params(tokens: list[str]) -> dict[str, str]:
    return {f"t{i}": f"%{_escape_like(tok)}%" for i, tok in enumerate(tokens)}


# Episode ILIKE used to search summary only, so a capture tagged
# ``recap-chip`` was invisible unless the summary also said those words.
# Extract JSON is the same family episode_text() already embeds.
_EPISODE_ILIKE_COLUMNS = (
    "summary",
    "COALESCE(topics::text, '')",
    "COALESCE(decisions::text, '')",
    "COALESCE(preferences::text, '')",
    "COALESCE(people::text, '')",
)

# fast=True columns for `_literal_candidates` (recall_prompt's budgeted
# lexical leg, khipu_status's prior_work — 2026-09-15): migration 0015 put a
# pg_trgm GIN index on exactly episodes.summary / topics.title / topics.body
# / nodes.name. An OR predicate spanning an indexed column AND an unindexed
# one (e.g. the full 5-column episode ILIKE, or topics.slug alongside
# title/body) cannot use a BitmapOr of index scans for every disjunct, so
# Postgres falls back to a full sequential scan for the whole WHERE —
# measured live against the deployed gateway: 1.18s on episodes (8.3k rows),
# 0.36s on topics, EXPLAIN showing "Seq Scan" throughout even though the
# indexes exist. Restricting the ILIKE columns to exactly what is indexed
# (plus the random_page_cost nudge below) turns the episodes query into a
# Bitmap Heap Scan.
#
# topics.body stayed slow even once the plan used the index: measured
# avg(length(body)) ~= 3.9KB, one outlier at 614KB — every candidate row's
# ILIKE match (and the bitmap scan's heap recheck) pays TOAST detoast cost
# on that large text, ~300-400ms regardless of scan method (seq or bitmap).
# topics.title averages 27 bytes; the identical query against title alone
# measured 0.8ms. So the fast topic leg drops body entirely and searches
# title only — the trade is a real recall loss (a prompt's words often live
# in a topic's body, not its title), accepted here because the cosine leg
# (same body text, embedded) still covers that ground semantically, and
# `fast` is already a "best effort within budget_ms" surface, not
# `khipu search`'s full-recall path.
_EPISODE_ILIKE_COLUMNS_FAST = ("summary",)
_TOPIC_ILIKE_COLUMNS_FAST = ("COALESCE(title, '')",)


def _id_shaped(term: str) -> bool:
    """A query that looks like a graph node id (``kind:slug`` or ``a__b``).

    W2.2: nodes are excluded from default (literal and hybrid) results — 73%
    of path nodes were junk (2026-09-03 evidence) and let a near-zero node
    starve real hits. An id-shaped query is exactly how a caller reaches a
    node on purpose, so it still can without ``kind: "node"``.
    """
    t = term or ""
    return ":" in t or "__" in t


def _search_query(
    cur,
    term: str,
    limit: int,
    *,
    kind: str | None = None,
    include_nodes: bool | None = None,
) -> list[dict]:
    """Deterministically-ordered, per-kind-fair ILIKE search (F7).

    Each active kind gets its own fair share of `limit` and its own ORDER BY,
    so one kind can no longer starve the others under a shared LIMIT, and
    results are stable across runs instead of arbitrary scan order.

    Multi-token queries rank by token coverage (OR + hit count), not one
    giant substring. A query that yields no tokens still uses the whole
    escaped term as before.

    ``kind`` restricts to one of 'topic'/'episode'/'node' (None = all
    eligible kinds). Nodes are excluded from the eligible set by default
    (W2.2) unless ``kind == "node"``, ``include_nodes`` is explicitly True,
    or (when ``include_nodes`` is left at its default None) the query looks
    id-shaped (``_id_shaped``). Pass ``include_nodes=False`` to force nodes
    out even for an id-shaped query.
    """
    if kind is not None and kind not in ("topic", "episode", "node"):
        raise ValueError("kind must be 'topic', 'episode', or 'node'")
    if kind == "node":
        want_nodes = True
    elif include_nodes is None:
        want_nodes = _id_shaped(term)
    else:
        want_nodes = bool(include_nodes)
    active_kinds = (
        [kind]
        if kind
        else (["topic", "episode", "node"] if want_nodes else ["topic", "episode"])
    )
    shares = dict(zip(active_kinds, _fair_shares(limit, len(active_kinds))))
    topic_n = shares.get("topic", 0)
    episode_n = shares.get("episode", 0)
    node_n = shares.get("node", 0)
    tokens = search_tokens(term)
    if not tokens:
        tokens = [(term or "").strip()] if (term or "").strip() else []
        if not tokens:
            return []
        params: dict = {"q": f"%{_escape_like(tokens[0])}%"}
        topic_where = (
            "body ILIKE %(q)s ESCAPE '\\' OR slug ILIKE %(q)s ESCAPE '\\' "
            "OR COALESCE(title, '') ILIKE %(q)s ESCAPE '\\'"
        )
        topic_order = "slug ASC"
        episode_where = " OR ".join(
            f"{c} ILIKE %(q)s ESCAPE '\\'" for c in _EPISODE_ILIKE_COLUMNS
        )
        episode_order = "ts DESC NULLS LAST, id DESC"
        node_where = (
            "id ILIKE %(q)s ESCAPE '\\' OR COALESCE(name, '') ILIKE %(q)s ESCAPE '\\'"
        )
        node_order = "id ASC"
    else:
        params = _ilike_token_params(tokens)
        n = len(tokens)
        topic_where, topic_score = _token_match_sql(
            ("body", "slug", "COALESCE(title, '')"), n
        )
        topic_order = f"({topic_score}) DESC, slug ASC"
        episode_where, episode_score = _token_match_sql(_EPISODE_ILIKE_COLUMNS, n)
        episode_order = f"({episode_score}) DESC, ts DESC NULLS LAST, id DESC"
        node_where, node_score = _token_match_sql(("id", "COALESCE(name, '')"), n)
        node_order = f"({node_score}) DESC, id ASC"
    results: list[dict] = []

    if topic_n > 0:
        cur.execute(
            f"""
            SELECT 'topic' AS kind, slug AS id, COALESCE(title, slug) AS label,
                   left(body, 4000) AS snippet
            FROM topics
            WHERE deleted_at IS NULL
              AND ({topic_where})
            ORDER BY {topic_order}
            LIMIT %(lim)s
            """,
            {**params, "lim": topic_n},
        )
        results.extend(_clip_search_row(r) for r in cur.fetchall())

    if episode_n > 0:
        cur.execute(
            f"""
            SELECT 'episode' AS kind, id::text AS id, summary AS label,
                   summary AS snippet
            FROM episodes
            WHERE {_episode_live_clause(cur)}({episode_where})
            ORDER BY {episode_order}
            LIMIT %(lim)s
            """,
            {**params, "lim": episode_n},
        )
        results.extend(_clip_search_row(r) for r in cur.fetchall())

    if node_n > 0:
        cur.execute(
            f"""
            SELECT 'node' AS kind, id AS id, COALESCE(name, id) AS label,
                   left(COALESCE(payload::text, ''), 4000) AS snippet
            FROM nodes
            WHERE {node_where}
            ORDER BY {node_order}
            LIMIT %(lim)s
            """,
            {**params, "lim": node_n},
        )
        results.extend(_clip_search_row(r) for r in cur.fetchall())

    return results


def _episode_live_clause(cur) -> str:
    """``deleted_at IS NULL AND `` when the episodes table has the column.

    A forgotten episode (``khipu episode --forget``) is a tombstone: topics
    have excluded ``deleted_at IS NOT NULL`` rows from every keyword query
    since 0010, but the episode branches did not, so a forgotten episode came
    straight back on the next search (audit 2026-09-04). Gated on the column
    existing, same posture as ``embed.coverage`` / ``activity`` on a
    pre-migration hub.
    """
    from khipu.db import has_columns

    try:
        return "deleted_at IS NULL AND " if has_columns(cur, "episodes", "deleted_at") else ""
    except Exception:  # noqa: BLE001 — schema probe must never break a search
        return ""


def _episode_rank_text(summary, topics, decisions, preferences, people, verbatim=None) -> str:
    """Full (unclipped) episode text for ranking — delegates to embed.episode_text
    (no byte copy) so the two never drift on what "the episode's text" means."""
    from khipu.embed import episode_text

    return episode_text({
        "summary": summary, "topics": topics, "decisions": decisions,
        "preferences": preferences, "people": people, "verbatim": verbatim,
    })


def _episode_verbatim_select(cur) -> str:
    """``verbatim`` when the column exists (0017), else a NULL of the same
    shape — keeps every episode SELECT's column count fixed regardless of
    migration state, same posture as ``_episode_live_clause``."""
    from khipu.db import has_columns

    try:
        return "verbatim" if has_columns(cur, "episodes", "verbatim") else "NULL::jsonb"
    except Exception:  # noqa: BLE001 — schema probe must never break a search
        return "NULL::jsonb"


def _neg_ts_sort_key(ts) -> float:
    if ts is None:
        return 0.0
    try:
        return -ts.timestamp()
    except (AttributeError, OverflowError, OSError, ValueError):
        return 0.0


def _literal_candidates(
    cur, term: str, limit: int, *, kind: str | None = None, filters=None, fast: bool = False
) -> list[dict]:
    """Globally-ranked literal ILIKE candidates for RRF fusion (bugfix,
    reported live: "Recorded mobile followup task" — episode 11286 named the
    query verbatim in its decisions but landed at literal rank 21 because
    ``_search_query``'s per-kind-fair split concatenates topic rows before
    episode rows, so *every* topic outranked *every* episode regardless of
    actual hit strength).

    ``_search_query`` stays fair-share-partitioned (F7) — that is correct for
    a stand-alone ILIKE listing and is what ``_pushed_memory_slice`` and
    ``mode=literal``'s per-kind test coverage still exercise. This function
    is the input to fusion instead: one flat pool (ranked by phrase-boost,
    then hit count desc, then ts desc, across every eligible kind together),
    because fairness at the *output* is ``_fair_fill``'s job, and pre-sorting
    the input into kind blocks defeats RRF's ranking entirely.

    Each row carries ``rank_text`` (unclipped: summary + topics/decisions/
    preferences/people for episodes, title + body for topics, id + name +
    payload for nodes) so the caller can also rank token overlap over the
    same text without a second query.

    ``fast`` (default False): restricts the ILIKE columns to exactly the
    ones migration 0015 put a pg_trgm GIN index on (episodes.summary;
    topics.title/body) and never queries ``nodes`` regardless of ``kind`` or
    id-shape — see ``_EPISODE_ILIKE_COLUMNS_FAST`` for why. Used only by
    ``recall_prompt``'s budgeted lexical leg (``khipu_status``'s
    ``prior_work``), which needs an index-scan-bounded statement per table,
    not full-column recall.

    ``fast`` also issues ``SET LOCAL random_page_cost`` on ``cur``'s
    transaction before the ILIKE queries run. Measured on the deployed
    gateway (Linode, SSD-backed block storage): even the narrowed
    single/two-column OR above still planned as a full ``Seq Scan`` — the
    ``BitmapOr`` over the trgm GIN index *is* available and ~8x faster
    end to end (28.9ms vs 235.6ms warm-cache; far more under concurrent
    load), but the planner's default ``random_page_cost = 4`` (tuned for
    spinning disks) makes it look more expensive than the sequential scan.
    ``SET LOCAL`` only affects the current transaction on this
    throwaway connection — no server/global config is touched.
    """
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    if kind is not None and kind not in ("topic", "episode", "node"):
        raise ValueError("kind must be 'topic', 'episode', or 'node'")
    want_nodes = (not fast) and (kind == "node" or (kind is None and _id_shaped(term)))
    active_kinds = (
        [kind] if kind else (["topic", "episode", "node"] if want_nodes else ["topic", "episode"])
    )
    if fast:
        active_kinds = [k for k in active_kinds if k != "node"]
        if not active_kinds:
            return []
        try:
            cur.execute("SET LOCAL random_page_cost = 1.1")
        except Exception:  # noqa: BLE001 — a GUC that can't be set (sqlite test
            # double, a locked-down role) must not sink the search; the
            # query still runs, just possibly via the slower seq-scan plan.
            pass
    lim = max(1, int(limit))
    tokens = search_tokens(term)
    pool: list[dict] = []
    # ``filters`` (embed._SearchFilters) pushes project/since/until/session_id/
    # harness into THIS pool's SQL instead of trimming it after the fact — a
    # filtered search used to be blind to anything past literal rank ~50
    # (audit 2026-09-04). None = unfiltered, exactly as before.
    ep_filter = tp_filter = nd_filter = "TRUE"
    filter_params: dict = {}
    if filters is not None and filters.active:
        filter_params = filters.params
        ep_filter = filters.episode_sql(cur)
        tp_filter = filters.topic_sql()
        nd_filter = filters.node_sql()
        if filters.episode_only:
            active_kinds = [k for k in active_kinds if k == "episode"]
            if not active_kinds:
                return []

    if not tokens:
        toks = [(term or "").strip()] if (term or "").strip() else []
        if not toks:
            return []
        params: dict = {"q": f"%{_escape_like(toks[0])}%", "lim": lim, **filter_params}
        if "topic" in active_kinds:
            topic_ilike_where = (
                "COALESCE(title, '') ILIKE %(q)s ESCAPE '\\'"
                if fast
                else (
                    "body ILIKE %(q)s ESCAPE '\\' OR slug ILIKE %(q)s ESCAPE '\\'"
                    " OR COALESCE(title, '') ILIKE %(q)s ESCAPE '\\'"
                )
            )
            cur.execute(
                f"""
                SELECT slug, COALESCE(title, slug) AS label, body,
                       COALESCE(updated_at, created_at) AS ts
                FROM topics
                WHERE deleted_at IS NULL AND ({topic_ilike_where})
                  AND ({tp_filter})
                ORDER BY slug ASC
                LIMIT %(lim)s
                """,
                params,
            )
            for slug, label, body, ts in cur.fetchall():
                pool.append({
                    "kind": "topic", "id": slug, "label": label, "snippet": body or "",
                    "rank_text": f"{label}\n\n{body or ''}", "hits": 1, "ts": ts,
                })
        if "episode" in active_kinds:
            # A backslash can't appear inside an f-string's {...} on Python
            # 3.11 (PEP 701 lifted this in 3.12 only) — build the ESCAPE '\'
            # clause as its own string first, same as the literal-mode
            # episode_where a few lines up, instead of nesting the join()
            # generator directly inside the outer f"""...""" query below.
            episode_ilike_where = " OR ".join(
                f"{c} ILIKE %(q)s ESCAPE '\\'"
                for c in (_EPISODE_ILIKE_COLUMNS_FAST if fast else _EPISODE_ILIKE_COLUMNS)
            )
            cur.execute(
                f"""
                SELECT id::text, summary, topics, decisions, preferences, people, ts,
                       {_episode_verbatim_select(cur)} AS verbatim
                FROM episodes
                WHERE {_episode_live_clause(cur)}({episode_ilike_where})
                  AND ({ep_filter})
                ORDER BY ts DESC NULLS LAST, id DESC
                LIMIT %(lim)s
                """,
                params,
            )
            for eid, summary, topics, decisions, preferences, people, ts, verbatim in cur.fetchall():
                pool.append({
                    "kind": "episode", "id": eid, "label": summary, "snippet": summary,
                    "rank_text": _episode_rank_text(summary, topics, decisions, preferences, people, verbatim),
                    "hits": 1, "ts": ts,
                })
        if "node" in active_kinds:
            cur.execute(
                f"""
                SELECT id, COALESCE(name, id) AS label, payload::text AS payload
                FROM nodes
                WHERE (id ILIKE %(q)s ESCAPE '\\' OR COALESCE(name, '') ILIKE %(q)s ESCAPE '\\')
                  AND ({nd_filter})
                ORDER BY id ASC
                LIMIT %(lim)s
                """,
                params,
            )
            for nid, label, payload in cur.fetchall():
                pool.append({
                    "kind": "node", "id": nid, "label": label,
                    "snippet": (payload or "")[:4000],
                    "rank_text": f"{label}\n{payload or ''}", "hits": 1, "ts": None,
                })
    else:
        params = {**_ilike_token_params(tokens), **filter_params}
        n = len(tokens)
        if "topic" in active_kinds:
            topic_cols = _TOPIC_ILIKE_COLUMNS_FAST if fast else ("body", "slug", "COALESCE(title, '')")
            topic_where, topic_score = _token_match_sql(topic_cols, n)
            cur.execute(
                f"""
                SELECT slug, COALESCE(title, slug) AS label, body,
                       COALESCE(updated_at, created_at) AS ts, ({topic_score}) AS hits
                FROM topics
                WHERE deleted_at IS NULL AND ({topic_where}) AND ({tp_filter})
                ORDER BY hits DESC, slug ASC
                LIMIT %(lim)s
                """,
                {**params, "lim": lim},
            )
            for slug, label, body, ts, hits in cur.fetchall():
                pool.append({
                    "kind": "topic", "id": slug, "label": label, "snippet": body or "",
                    "rank_text": f"{label}\n\n{body or ''}", "hits": int(hits), "ts": ts,
                })
        if "episode" in active_kinds:
            episode_cols = _EPISODE_ILIKE_COLUMNS_FAST if fast else _EPISODE_ILIKE_COLUMNS
            episode_where, episode_score = _token_match_sql(episode_cols, n)
            cur.execute(
                f"""
                SELECT id::text, summary, topics, decisions, preferences, people, ts,
                       ({episode_score}) AS hits, {_episode_verbatim_select(cur)} AS verbatim
                FROM episodes
                WHERE {_episode_live_clause(cur)}({episode_where}) AND ({ep_filter})
                ORDER BY hits DESC, ts DESC NULLS LAST, id DESC
                LIMIT %(lim)s
                """,
                {**params, "lim": lim},
            )
            for eid, summary, topics, decisions, preferences, people, ts, hits, verbatim in cur.fetchall():
                pool.append({
                    "kind": "episode", "id": eid, "label": summary, "snippet": summary,
                    "rank_text": _episode_rank_text(summary, topics, decisions, preferences, people, verbatim),
                    "hits": int(hits), "ts": ts,
                })
        if "node" in active_kinds:
            node_where, node_score = _token_match_sql(("id", "COALESCE(name, '')"), n)
            cur.execute(
                f"""
                SELECT id, COALESCE(name, id) AS label, payload::text AS payload, ({node_score}) AS hits
                FROM nodes
                WHERE ({node_where}) AND ({nd_filter})
                ORDER BY hits DESC, id ASC
                LIMIT %(lim)s
                """,
                {**params, "lim": lim},
            )
            for nid, label, payload, hits in cur.fetchall():
                pool.append({
                    "kind": "node", "id": nid, "label": label,
                    "snippet": (payload or "")[:4000],
                    "rank_text": f"{label}\n{payload or ''}", "hits": int(hits), "ts": None,
                })

    qlower = (term or "").strip().lower()

    def _phrase_hit(r: dict) -> int:
        return 1 if qlower and qlower in (r.get("rank_text") or "").lower() else 0

    pool.sort(key=lambda r: (-_phrase_hit(r), -r.get("hits", 0), _neg_ts_sort_key(r.get("ts"))))
    out = []
    for r in pool[:lim]:
        out.append({
            "kind": r["kind"], "id": r["id"],
            "label": clip_snippet(r["label"] or "", LABEL_LIMIT),
            "snippet": clip_snippet(r["snippet"] or "", SNIPPET_LIMIT),
            "rank_text": r["rank_text"],
        })
    return out


def _resolve_search_mode(args: argparse.Namespace) -> str:
    """``--mode`` wins; the legacy ``--semantic`` flag is still an alias for
    ``mode=semantic`` (W2.1) so existing scripts/muscle-memory keep working."""
    mode = getattr(args, "mode", None)
    if mode:
        return mode
    if getattr(args, "semantic", False):
        return "semantic"
    return "hybrid"


def cmd_search(args: argparse.Namespace) -> int:
    from khipu import query_log
    from khipu.hub_snapshot import hub_connection_failed, search_stale_payload

    mode = _resolve_search_mode(args)
    kind = getattr(args, "kind", None)
    project = getattr(args, "project", None)
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    session_id = getattr(args, "session_id", None)
    harness = getattr(args, "harness", None)
    via = getattr(args, "via", None)
    source = getattr(args, "source", None)
    provenance = {"via": via} if via else {}
    try:
        from khipu.embed import hybrid_search

        payload = hybrid_search(
            args.query, limit=args.limit, mode=mode, kind=kind, project=project,
            since=since, until=until, session_id=session_id, harness=harness,
            source=source, **provenance,
        )
    except ValueError as err:
        print(json.dumps({"ok": False, "error": str(err)}))
        return 2
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        if kind == "library" or source:
            print(json.dumps({"ok": False, "error": (
                "library search needs the hub; the offline snapshot holds no libraries")}))
            return 2
        try:
            payload = search_stale_payload(
                args.query, args.limit, semantic=(mode == "semantic"), kind=kind,
                since=since, until=until, project=project, session_id=session_id,
                harness=harness, **provenance,
            )
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
    query_log.log_query(
        args.query, mode=mode,
        filters={"kind": kind, "project": project, "since": since, "until": until,
                 "session_id": session_id, "harness": harness, "via": via, "source": source},
        result_count=len(payload.get("results") or []), top=payload.get("results") or [],
        degraded=payload.get("degraded"),
    )
    print(json.dumps(payload, indent=2))
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    from khipu.embed import activate, backfill, coverage

    if args.embed_cmd == "status":
        print(json.dumps(coverage(profile=getattr(args, "profile", None), detail=True), indent=2))
        return 0
    if args.embed_cmd == "profiles":
        return _cmd_embed_profiles(args)
    if args.embed_cmd in ("estimate", "jobs", "test-key"):
        return _cmd_embed_ops(args)
    if args.embed_cmd == "index":
        return _cmd_embed_index(args)
    if args.embed_cmd == "backfill" and (getattr(args, "job", False) or getattr(args, "job_id", None)):
        return cmd_embed_backfill_job(args)
    if args.embed_cmd == "activate":
        try:
            out = activate(args.profile, force=bool(args.force))
        except (ValueError, RuntimeError) as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        print(json.dumps(out, indent=2))
        return 0
    stats = backfill(
        kind=args.kind,
        limit=args.limit,
        dry_run=args.dry_run,
        profile=getattr(args, "profile", None),
    )
    print(json.dumps(stats, indent=2))
    return _backfill_exit_code(stats)


def _backfill_exit_code(stats: dict) -> int:
    """Failed or budget-stopped backfills are incomplete."""
    failed = int(stats.get("failed_chunks", stats.get("failed", 0)) or 0)
    return 1 if stats.get("ok") is False or failed or stats.get("budget_exhausted") else 0


def _cmd_embed_profiles(args: argparse.Namespace) -> int:
    """`khipu embed profiles list|add`: the embedding_profiles table, no model call."""
    from khipu import profiles
    from khipu.db import connect

    if args.profiles_cmd == "list":
        from khipu import embed_ops

        print(json.dumps({"profiles": embed_ops.list_detailed(parallel=True)}, indent=2, default=str))
        return 0
    if args.profiles_cmd == "delete":
        return _cmd_embed_profile_delete(args)
    try:
        spec = profiles.validate_spec(
            args.id, provider=args.provider, model=args.model, dim=args.dim,
            endpoint=args.endpoint, normalize=args.normalize,
        )
        with connect() as conn:
            with conn.cursor() as cur:
                out = profiles.add_profile(cur, spec)
                # No index here: an empty one would be maintained row by row
                # through the first bulk load. The first backfill or import
                # builds it once over finished rows.
                out["indexes"] = "built on first backfill or import"
            conn.commit()
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(out, indent=2))
    return 0


def _cmd_embed_index(args: argparse.Namespace) -> int:
    """`khipu embed index PROFILE [--table memory|library]`: build the profile's
    HNSW index (default both tables); a no-op where it exists. Honours
    KHIPU_INDEX_MEMORY."""
    from khipu import profiles
    from khipu.db import connect

    tables = {"memory": ("memory_embeddings",), "library": ("library_embeddings",),
              None: profiles.INDEXED_TABLES}[getattr(args, "table", None)]
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                spec = profiles.resolve_spec(args.profile, cur)
                built = {t: profiles.ensure_profile_index(cur, spec.id, t, dim=spec.dim)
                         for t in tables}
            conn.commit()
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps({"ok": True, "profile": spec.id, "indexes": built}, indent=2))
    return 0


# --bypass-harness (sonnet lane): dispatched agent, brief says do not delegate.
def _cmd_embed_profile_delete(args: argparse.Namespace) -> int:
    """`khipu embed profiles delete ID --yes`: delete a profile nothing uses and
    every vector it owns. Refuses (naming the users) when it is the active
    memory profile or a library is embedded with it."""
    from khipu import profiles
    from khipu.db import connect

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                if profiles.load_spec(cur, args.id) is None:
                    raise ValueError(f"unknown embedding profile {args.id!r}")
                users = profiles.profile_users(cur, args.id)
                if users:
                    raise ValueError(
                        f"profile {args.id!r} is in use by {', '.join(users)}; move those to "
                        "another profile first, then delete"
                    )
                if not args.yes:
                    rows = next((p["rows"] for p in profiles.list_profiles(cur)
                                 if p["id"] == args.id), {"memory": 0, "library": 0})
                    raise ValueError(
                        f"refusing to delete {args.id!r} without --yes: this deletes "
                        f"{rows['memory']} memory vectors and {rows['library']} library vectors "
                        "for it, its cached query vectors and its indexes (the text stays; "
                        "it can be embedded again)"
                    )
                out = profiles.delete_profile(cur, args.id)
            conn.commit()
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(out, indent=2))
    return 0


def _cmd_embed_ops(args: argparse.Namespace) -> int:
    """`khipu embed estimate | jobs | test-key`."""
    from khipu import embed_jobs, embed_ops

    cmd = args.embed_cmd
    if cmd == "jobs":
        out = embed_jobs.clear_jobs() if args.clear else {"jobs": embed_jobs.list_jobs()}
        print(json.dumps(out, indent=2))
        return 0
    if cmd == "test-key":
        out = embed_ops.check_key(args.provider, endpoint=args.endpoint, model=args.model)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 2
    from khipu import library

    try:
        out = embed_ops.estimate(args.profile, args.space, stale=bool(args.stale))
    except (ValueError, RuntimeError, library.LibraryError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(out, indent=2))
    return 0


def cmd_embed_backfill_job(args: argparse.Namespace) -> int:
    """Memory backfill as a detached job with a progress file (`khipu embed
    backfill --profile P --job`, or the top-level `embed-backfill`). Prints one
    final JSON line; exit 0 for done and cancelled, 1 for failed."""
    from khipu import embed_jobs
    from khipu.embed import backfill

    profile = (getattr(args, "profile", None) or "").strip()
    if not profile:
        print(json.dumps({"ok": False, "error": "a job needs --profile (the profile to embed under)"}))
        return 2
    try:
        job_id = embed_jobs.validate_job_id(args.job_id) if getattr(args, "job_id", None) else None
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    return embed_jobs.run_job(
        kind="embed-backfill", profile=profile, space="memory", job_id=job_id,
        work=lambda job: backfill(
            kind=getattr(args, "kind", None), limit=getattr(args, "limit", None),
            dry_run=bool(getattr(args, "dry_run", False)), profile=profile, job=job,
        ),
    )


def cmd_library_backfill_job(args: argparse.Namespace) -> int:
    """Library backfill as a detached job (`khipu library backfill NAME --job`,
    or the top-level `library-backfill NAME`)."""
    from khipu import embed_jobs, library
    from khipu.db import connect

    try:
        library.validate_name(args.name)
        job_id = embed_jobs.validate_job_id(args.job_id) if getattr(args, "job_id", None) else None
    except (library.LibraryError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    override = (getattr(args, "profile", None) or "").strip() or None

    def work(job):
        with connect() as conn:
            with conn.cursor() as cur:
                src = library.get_source(cur, args.name)
            job.profile = override or src.profile
            job.write()
            return library.backfill(
                conn, args.name, limit=getattr(args, "limit", None),
                stale=bool(getattr(args, "stale", False)), profile=override, job=job,
            )

    return embed_jobs.run_job(
        kind="library-backfill", profile=override or "", space=f"library:{args.name}",
        job_id=job_id, work=work,
    )


def cmd_library(args: argparse.Namespace) -> int:
    """`khipu library ...`: library sources (a folder of .txt/.md files as a
    search space). Refusals print {"ok": false, "error": ...} and exit 2;
    `scan` makes no model call, `backfill` and a profile-less `import` may."""
    from khipu import library

    cmd = args.library_cmd
    # --bypass-harness (sonnet lane): dispatched agent, brief says do not delegate.
    if cmd == "backfill" and (getattr(args, "job", False) or getattr(args, "job_id", None)):
        return cmd_library_backfill_job(args)
    try:
        if cmd == "add":
            library.prepare_add(args.name, args.root)  # refuse before touching the hub
        elif getattr(args, "name", None):
            library.validate_name(args.name)
        from khipu.db import connect

        with connect() as conn:
            if cmd == "add":
                out = library.add_source(conn, args.name, args.root, args.profile)
            elif cmd == "list":
                out = {"libraries": library.list_sources(conn)}
            elif cmd == "status":
                out = library.source_status(conn, args.name)
            elif cmd == "remove":
                out = library.remove_source(conn, args.name, yes=bool(args.yes))
            elif cmd in ("enable", "disable"):
                out = library.set_enabled(conn, args.name, cmd == "enable")
            elif cmd == "set-profile":
                out = library.set_profile(conn, args.name, args.profile)
            elif cmd == "scan":
                out = library.scan(conn, args.name, allow_rebuild=bool(getattr(args, "allow_rebuild", False)))
            elif cmd == "backfill":
                # --bypass-harness (sonnet lane): dispatched agent, no delegation.
                out = library.backfill(
                    conn, args.name, limit=args.limit, stale=bool(args.stale),
                    profile=getattr(args, "profile", None),
                )
            else:  # import
                out = library.import_index(
                    conn, args.name, args.path,
                    strip_prefix=args.strip_prefix or "", profile=args.profile,
                    batch=getattr(args, "batch", None) or library.IMPORT_BATCH,
                )
    except library.LibraryError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(out, indent=2, default=str))
    return _backfill_exit_code(out) if cmd == "backfill" else 0


def cmd_embed_media_backfill(args: argparse.Namespace) -> int:
    """Top-level hyphenated job: native PNG/JPEG into gemini-embedding-2@768."""
    from khipu.embed import backfill_media
    from khipu.jobs import _write_job_state

    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    try:
        stats = backfill_media(
            dry_run=dry_run,
            yes=yes,
            limit=getattr(args, "limit", None),
            source_id=getattr(args, "source_id", None),
            profile=getattr(args, "profile", None),
        )
    except Exception as e:  # noqa: BLE001
        if not dry_run:
            _write_job_state("embed_media_backfill", 1)
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    if stats.get("needs_yes"):
        if not dry_run:
            _write_job_state("embed_media_backfill", 2)
        print(json.dumps({"ok": False, **stats}, indent=2))
        return 2
    if not dry_run:
        _write_job_state("embed_media_backfill", 0)
    print(json.dumps({"ok": True, **stats}, indent=2))
    return 0


def _graph_query(cur, node_id: str, hops: int, limit: int) -> dict:
    """Neighborhood lookup for `khipu graph`.

    hops == 1 returns the direct edges (either direction) plus a best-effort
    SQL/PGQ GRAPH_TABLE sample. hops >= 2 walks a recursive CTE that must stay
    undirected at every step (F4) — the 1-hop branch above is already
    undirected (`e.src = id OR e.dst = id`); the old recursive branch seeded
    and walked outbound-only, which silently dropped inbound-only neighbors
    (e.g. "…__doc" / "…__smry" concept nodes) once hops >= 2. The walk is
    ordered hops-then-node so a shared LIMIT can never let a hop>=2 row bump
    a hop=1 row out of the result — that ordering is what makes "hops>=2 is a
    superset of hops=1" hold under a LIMIT, not just in an unbounded query.
    """
    hops = max(1, int(hops))
    from khipu.topic_graph import (
        PATH_PREFIX,
        TOPIC_PREFIX,
        extract_paths,
        graph_query_aliases,
        topic_slug_from_label,
    )

    episode_meta: dict | None = None
    raw_id = (node_id or "").strip()
    synthetic: list[dict] = []
    if raw_id.isdigit():
        cur.execute(
            f"SELECT topics, summary FROM episodes WHERE {_episode_live_clause(cur)}id = %s",
            (int(raw_id),),
        )
        row = cur.fetchone()
        slugs: list[str] = []
        aliases: list[str] = []
        if row is None:
            episode_meta = {"id": int(raw_id), "missing": True, "topics": []}
        else:
            topics_raw, summary = row
            for item in topics_raw or []:
                slug = topic_slug_from_label(str(item))
                if slug:
                    slugs.append(slug)
            slugs = list(dict.fromkeys(slugs))
            for slug in slugs:
                aliases.extend(graph_query_aliases(slug))
            for rel in extract_paths(summary or ""):
                aliases.append(PATH_PREFIX + rel.rstrip("/"))
            aliases = list(dict.fromkeys(a for a in aliases if a))
            episode_meta = {
                "id": int(raw_id),
                "missing": False,
                "topics": slugs,
            }
            synthetic = [
                {
                    "src": f"episode:{raw_id}",
                    "dst": f"{TOPIC_PREFIX}{slug}",
                    "type": "capture_topic",
                    "weight": 1.0,
                }
                for slug in slugs
            ]
    else:
        aliases = graph_query_aliases(node_id)

    if hops == 1:
        sql_edges: list[tuple] = []
        gt: list[tuple] = []
        if aliases:
            cur.execute(
                """
                SELECT e.src, e.dst, e.type, e.weight
                FROM edges e
                WHERE e.src = ANY(%(ids)s) OR e.dst = ANY(%(ids)s)
                ORDER BY (CASE WHEN e.src = ANY(%(ids)s) THEN e.dst ELSE e.src END) ASC, e.type ASC
                LIMIT %(lim)s
                """,
                {"ids": aliases, "lim": limit},
            )
            sql_edges = cur.fetchall()
            try:
                cur.execute(
                    """
                    SELECT * FROM GRAPH_TABLE (
                      alzy_graph
                      MATCH (a IS node WHERE a.id = ANY(%(ids)s))-[r IS edge]->(b IS node)
                      COLUMNS (a.id AS src, b.id AS dst, r.type AS edge_type)
                    ) LIMIT %(lim)s
                    """,
                    {"ids": aliases, "lim": limit},
                )
                gt = cur.fetchall()
            except Exception:  # noqa: BLE001 — beta GRAPH_TABLE may flake
                gt = []
        merged = synthetic + [
            {"src": a, "dst": b, "type": t, "weight": w} for a, b, t, w in sql_edges
        ]
        out = {
            "id": node_id,
            "hops": 1,
            "edges": merged[:limit],
            "graph_table": [{"src": a, "dst": b, "type": t} for a, b, t in gt],
        }
        if episode_meta is not None:
            out["episode"] = episode_meta
        return out

    walk_rows: list[tuple] = []
    if aliases:
        cur.execute(
            """
            WITH RECURSIVE walk AS (
              SELECT
                CASE WHEN e.src = ANY(%(ids)s) THEN e.dst ELSE e.src END AS node_id,
                %(id)s AS via,
                e.type,
                1 AS hops
              FROM edges e
              WHERE e.src = ANY(%(ids)s) OR e.dst = ANY(%(ids)s)
              UNION
              SELECT
                CASE WHEN e.src = w.node_id THEN e.dst ELSE e.src END AS node_id,
                w.node_id AS via,
                e.type,
                w.hops + 1
              FROM walk w
              JOIN edges e ON e.src = w.node_id OR e.dst = w.node_id
              WHERE w.hops < %(max_hops)s
            )
            SELECT node_id, via, type, hops FROM walk
            ORDER BY hops ASC, node_id ASC
            LIMIT %(lim)s
            """,
            {"ids": aliases, "id": node_id, "max_hops": hops, "lim": limit},
        )
        walk_rows = cur.fetchall()
    synthetic_walk = [
        {
            "node_id": e["dst"],
            "via": e["src"],
            "type": e["type"],
            "hops": 1,
        }
        for e in synthetic
    ]
    out = {
        "id": node_id,
        "hops": hops,
        "walk": (
            synthetic_walk
            + [
                {"node_id": a, "via": b, "type": t, "hops": h}
                for a, b, t, h in walk_rows
            ]
        )[:limit],
    }
    if episode_meta is not None:
        out["episode"] = episode_meta
    return out


def cmd_graph(args: argparse.Namespace) -> int:
    from khipu.hub_snapshot import (
        graph_neighbors_snapshot,
        hub_connection_failed,
        stale_fields,
        try_hub_connect,
    )

    try:
        with try_hub_connect() as conn:
            with conn.cursor() as cur:
                out = _graph_query(cur, args.id, args.hops, args.limit)
    except Exception as exc:
        if not hub_connection_failed(exc):
            raise
        out = graph_neighbors_snapshot(args.id, args.hops, args.limit)
        out.update(stale_fields())
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_topic_graph_backfill(args: argparse.Namespace) -> int:
    from khipu.mirror import backfill_topic_graph

    mem = _require_memory_root(args)
    if mem is None:
        return 2
    stats = backfill_topic_graph(mem, dry_run=bool(getattr(args, "dry_run", False)))
    print(json.dumps(stats, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# Memory reliability (2026-09-03): owed / episode forget / topic purge /
# backfill identity / hygiene paths. New subcommands only — the search/
# status/doctor internals above are owned by a concurrent change.
# ---------------------------------------------------------------------------

def _snooze_until_sql(raw: str) -> tuple[str, object] | None:
    """`--until` for `khipu owed --snooze`, as an (sql_expr, param) pair.

    Accepts everything the capture path's lenient parser already accepts
    (ISO 8601 / ``YYYY-MM-DD``, ``N days|weeks|months``, ``in N days``) plus
    the compact ``7d`` / ``2w`` / ``3m`` the Owed screen's snooze presets
    send, which is expanded before handing over. Returns ``None`` when the
    value is not a date at all — ``_parse_due_after`` answers NULL for free
    text, and silently clearing a due date is not what "snooze" means.
    """
    import re as _re

    from khipu import commitments

    text = (raw or "").strip()
    m = _re.match(r"^(\d+)\s*([dwm])$", text, _re.I)
    if m:
        unit = {"d": "days", "w": "weeks", "m": "months"}[m.group(2).lower()]
        text = f"{m.group(1)} {unit}"
    expr, param = commitments._parse_due_after(text)
    return None if expr == "NULL" else (expr, param)


def cmd_usage(args: argparse.Namespace) -> int:
    """`khipu usage`: each subscription CLI's own plan headroom (scope
    docs/plans/2026-10-14-plan-usage.md). Reports only; routing stays with
    the orchestrator."""
    from khipu import usage

    if getattr(args, "forget_login_paths", False):
        from khipu import session_capture

        # A conversation busy at that moment is cleared by its next sweep;
        # until then this says it is not finished and how many are left.
        result = session_capture.forget_login_homes()
        out = {"ok": True, "finished": not (result["pending"] or result.get("failed")
                                             or result.get("unreachable")), **result}
        if result.get("unreachable"):
            out["note"] = "Not finished: Khipu could not open its conversation records, so nothing was cleared"
        elif result.get("failed"):
            out["note"] = ("Not finished: Khipu could not rewrite some conversation records; "
                           "it tries again on the next capture")
        elif result["pending"]:
            n = result["pending"]
            out["note"] = (f"Not finished: {n} conversation{' was' if n == 1 else 's were'} busy; "
                           f"Khipu clears {'it' if n == 1 else 'them'} when {'it is' if n == 1 else 'they are'} free")
        print(json.dumps(out))
        return 0 if out["finished"] else 1
    # Settings clarity phase 9 (9D): signing in a new account, for the app.
    # No MCP tool reaches these (they carry folder paths and sign-in codes).
    sign_in_new = (getattr(args, "sign_in_offers", False) or getattr(args, "sign_in_folder", False)
                   or getattr(args, "sign_in_new", False) or getattr(args, "sign_in_status", None) is not None
                   or getattr(args, "sign_in_cancel", None) is not None or getattr(args, "sign_in_reap", False))
    if sign_in_new:
        from khipu import signin

        if args.sign_in_offers:
            print(json.dumps(signin.offers(), indent=2))
            return 0
        if args.sign_in_reap:
            print(json.dumps(signin.reap(), indent=2))
            return 0
        if args.sign_in_status is not None:
            out = signin.status(args.sign_in_status)
            print(json.dumps(out, indent=2))
            return 2 if out.get("error") == signin.E_NO_ATTEMPT else 0
        if args.sign_in_cancel is not None:
            out = signin.cancel(args.sign_in_cancel or None, remove_folder=bool(args.remove_folder),
                                cli=getattr(args, "harness", None), folder=getattr(args, "folder", None))
            print(json.dumps(out, indent=2))
            return 0 if out.get("ok") else 1
        if not getattr(args, "harness", None):
            print(json.dumps({"ok": False, "error": "name the CLI with --harness"}))
            return 2
        if args.sign_in_folder:
            if not args.name:
                print(json.dumps({"ok": False, "error": "name the account with --name"}))
                return 2
            out = signin.make_folder(args.harness, args.name)
            print(json.dumps(out, indent=2))
            return 0 if out.get("ok") else 2
        out = signin.start(args.harness, getattr(args, "folder", None), again=bool(args.again))
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    if getattr(args, "discover", False) or getattr(args, "check", None) is not None:
        # Settings clarity phase 9 (9A): the login folders Khipu could track.
        # A read for the app; no MCP tool starts it (it carries paths).
        from khipu import discover

        out = discover.discover(now=bool(getattr(args, "now", False)), check=getattr(args, "check", None))
        if getattr(args, "check", None) is None:
            out["clis"] = discover.cli_states()
        # The owner's automatic-tracking switch (off by default): what Home
        # would offer is tracked through the one save path.
        from khipu import accounts

        out["auto_tracked"] = accounts.auto_track(out)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            for c in out["candidates"]:
                print(f"{c['cli_name']:<11} {c['path'] or '(one login)':<40} {c['state']}"
                      + (f" ({c['reason']})" if c.get("reason") else ""))
            if not out["candidates"]:
                print("Looking..." if out.get("looking") else "No other logins found.")
            check = out.get("check")
            if check is not None and not check.get("ok"):
                print(f"Check not done: {check.get('error') or 'unknown'}")
        check = out.get("check")
        return 2 if check is not None and not check.get("ok") and check.get("error") != "timeout" else 0
    if getattr(args, "try_account", False):
        from khipu import discover

        if not getattr(args, "harness", None):
            print(json.dumps({"ok": False, "error": "name the CLI with --harness"}))
            return 2
        out = discover.try_account(args.harness, getattr(args, "folder", None))
        print(json.dumps(out, indent=2))
        return 0 if out.get("row") is not None else 2

    max_age = usage.DEFAULT_MAX_AGE_S if args.max_age is None else max(0.0, float(args.max_age))
    label = getattr(args, "account", None)
    key = getattr(args, "account_key", None)
    account, error = (usage.resolve_account(label, key) if label is not None or key is not None
                      else (None, None))
    if error:
        print(json.dumps({"ok": False, "error": error}))
        return 2
    if getattr(args, "sign_in", False):
        if account is None:
            print(json.dumps({"ok": False, "error": "name the account with --account"}))
            return 2
        out = usage.sign_in(account["label"], key=usage.account_key(account))
        print(json.dumps(out, indent=2))
        return 0 if out["ok"] else 1
    one = {"accounts": [account]} if account else {}
    rep = usage.report(fresh=bool(args.fresh), max_age=max_age, **one)
    if args.json:
        print(json.dumps(rep, indent=2))
    elif not rep["accounts"]:
        print("No subscription CLI found (claude, codex, grok, muse); set usage_accounts in config.json.")
    else:
        print(usage.table(rep))
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    """`khipu accounts`: track, rename, stop tracking, move and decline the
    accounts plan usage reads (settings clarity phase 9, 9B). Owner only."""
    from khipu import accounts

    return accounts.run_cli(args)


def cmd_route(args: argparse.Namespace) -> int:
    """`khipu route`: the routing record (scope docs/plans/2026-10-14-routing-truth.md,
    phases 1a to 2). Khipu reports which accounts are open, by whom and since when;
    it never chooses, refuses, launches or reorders work."""
    from khipu import routing

    return routing.run_cli(args)


def cmd_owed(args: argparse.Namespace) -> int:
    """W3.4: `khipu owed` — list, close, reopen, or snooze commitments. With
    the hub away a close, reopen or snooze waits on this Mac and replays when
    it answers (`waiting_to_sync`), and the list comes from this Mac's copy."""
    from khipu import commitments, offline, offline_reads, owner_writes
    from khipu.db import connect
    from khipu.hub_snapshot import hub_connection_failed

    snooze_id = getattr(args, "snooze", None)
    if snooze_id:
        parsed = _snooze_until_sql(getattr(args, "until", None) or "")
        if parsed is None:
            print(json.dumps({
                "ok": False, "id": int(snooze_id),
                "error": "--until must be a date (2026-09-30) or a window (7d, 2 weeks)",
            }))
            return 2
        cid = int(snooze_id)
        until = (getattr(args, "until", None) or "").strip()
        try:
            res = owner_writes.run("owed_update", {"id": cid, "action": "snooze", "until": until})
        except ValueError:
            print(json.dumps({"ok": False, "id": cid, "until": until}))
            return 1
        print(json.dumps({"ok": True, "id": cid, "until": until,
                          **({"waiting_to_sync": True} if res.get("waiting_to_sync") else {})}))
        return 0

    close_id = getattr(args, "close", None)
    reopen_id = getattr(args, "reopen", None)
    if close_id or reopen_id:
        cid = int(close_id or reopen_id)
        new_status = "closed" if close_id else "open"
        try:
            res = owner_writes.run("owed_update", {"id": cid, "action": "close" if close_id else "reopen"})
        except ValueError:
            print(json.dumps({"ok": False, "id": cid, "status": new_status}))
            return 1
        print(json.dumps({"ok": True, "id": cid, "status": new_status,
                          **({"waiting_to_sync": True} if res.get("waiting_to_sync") else {})}))
        return 0

    project = getattr(args, "project", None)
    status = getattr(args, "status", None) or "open"
    limit = int(getattr(args, "limit", None) or 50)
    stale: dict = {}
    try:
        with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
            with conn.cursor() as cur:
                rows = commitments.list_owed(cur, project=project, status=status, limit=limit)
                # Ages counted on the hub's own clock, so a Mac clock that runs
                # ahead of or behind the hub can't make an age negative.
                cur.execute("SELECT now()")
                hub_now = (cur.fetchone() or [None])[0]
    except Exception as exc:  # noqa: BLE001
        if not hub_connection_failed(exc):
            raise
        copy = offline_reads.owed(project, status, limit)
        if copy is None:
            raise
        rows = copy["results"]
        stale = {"stale": True, "snapshot_refreshed_at": copy.get("snapshot_refreshed_at")}
        from datetime import datetime, timezone

        hub_now = datetime.now(timezone.utc)
    commitments.add_hub_ages(rows, hub_now)
    owner_writes.annotate(rows, "owed_update")
    if getattr(args, "envelope", False):
        print(json.dumps({"results": rows, "stale": bool(stale), "snapshot_refreshed_at": stale.get("snapshot_refreshed_at"),
                          "waiting_to_sync": owner_writes.status()}, indent=2, default=str))
    else:
        print(json.dumps(rows, indent=2, default=str))
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    """O2 / Phase 2A: `khipu decisions list [--project] [--since] [--standing|
    --superseded|--retracted|--all]`, `supersede OLD NEW [--reason] [--force]`,
    `restore ID`, `retract ID --reason`, `links [--state]`, `confirm LINK_ID`,
    `reject LINK_ID`, `backfill [--dry-run|--apply]`."""
    from khipu.db import connect
    from khipu import decisions as _decisions

    sub_cmd = getattr(args, "decisions_cmd", None)

    if sub_cmd == "supersede":
        old_id, new_id = int(args.old_id), int(args.new_id)
        reason = getattr(args, "reason", None)
        force = bool(getattr(args, "force", False))
        with connect() as conn:
            with conn.cursor() as cur:
                try:
                    ok = _decisions.supersede(cur, old_id, new_id, reason=reason, force=force)
                except ValueError as exc:
                    print(json.dumps({"ok": False, "error": str(exc)}))
                    return 1
            conn.commit()
        print(json.dumps({"ok": ok, "old_id": old_id, "new_id": new_id}))
        return 0 if ok else 1

    if sub_cmd == "restore":
        decision_id = int(args.id)
        with connect() as conn:
            with conn.cursor() as cur:
                try:
                    ok = _decisions.restore(cur, decision_id)
                except ValueError as exc:
                    print(json.dumps({"ok": False, "error": str(exc)}))
                    return 1
            conn.commit()
        print(json.dumps({"ok": ok, "id": decision_id}))
        return 0 if ok else 1

    if sub_cmd == "retract":
        decision_id = int(args.id)
        with connect() as conn:
            with conn.cursor() as cur:
                ok = _decisions.retract(cur, decision_id, getattr(args, "reason", None))
            conn.commit()
        print(json.dumps({"ok": ok, "id": decision_id}))
        return 0 if ok else 1

    if sub_cmd == "links":
        with connect() as conn:
            with conn.cursor() as cur:
                rows = _decisions.list_links(
                    cur, state=getattr(args, "state", None) or None,
                    project=getattr(args, "project", None),
                )
        print(json.dumps(rows, indent=2, default=str))
        return 0

    if sub_cmd in ("confirm", "reject"):
        link_id = int(args.link_id)
        with connect() as conn:
            with conn.cursor() as cur:
                try:
                    out = _decisions.resolve_link(cur, link_id, sub_cmd)
                except ValueError as exc:
                    print(json.dumps({"ok": False, "error": str(exc)}))
                    return 1
            conn.commit()
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("ok") else 1

    if sub_cmd == "backfill":
        apply = bool(getattr(args, "apply", False))
        with connect() as conn:
            with conn.cursor() as cur:
                report = _decisions.backfill_decisions(
                    cur, apply=apply, limit=getattr(args, "limit", None)
                )
            if apply:
                conn.commit()
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("ok", True) else 1

    # "list" (default)
    since_raw = (getattr(args, "since", None) or "").strip()
    since_dt = None
    if since_raw:
        from khipu.search_text import parse_time_filter

        try:
            since_dt = parse_time_filter(since_raw)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"--since: {exc}"}))
            return 2
    status = None
    if getattr(args, "standing", False):
        status = "standing"
    elif getattr(args, "superseded", False):
        status = "superseded"
    elif getattr(args, "retracted", False):
        status = "retracted"
    elif getattr(args, "all", False):
        status = "all"
    with connect() as conn:
        with conn.cursor() as cur:
            rows = _decisions.list_decisions(
                cur, project=getattr(args, "project", None), since=since_dt,
                limit=int(getattr(args, "limit", None) or 50), status=status,
            )
    print(json.dumps(rows, indent=2, default=str))
    return 0


def _reembed_episode(cur, episode_id: int) -> bool:
    """Replace one episode's vectors from its current row, using the same text
    builder, profile and upsert `embed.embed_on_capture` uses — so a corrected
    summary is findable by meaning immediately instead of at the next nightly.
    The API call happens before the DELETE, so a failed embed leaves the old
    vectors in place rather than none at all."""
    from khipu.embed import (
        _active_profile,
        _api_texts,
        _md5,
        _upsert_chunks,
        chunk_text,
        embed_batch,
        episode_text,
    )

    cols = ("summary", "decisions", "preferences", "topics", "people")
    verbatim_col = _episode_verbatim_select(cur)
    # A forgotten episode's vectors were removed on purpose; an edit of its
    # tombstone must not put them back.
    cur.execute(
        f"SELECT {', '.join(cols)}, {verbatim_col} AS verbatim FROM episodes "
        f"WHERE {_episode_live_clause(cur)}id = %s",
        (episode_id,),
    )
    row = cur.fetchone()
    if not row:
        return False
    chunks = chunk_text(episode_text(dict(zip((*cols, "verbatim"), row))))
    if not chunks:
        return False
    profile = _active_profile(cur)
    vecs = embed_batch(_api_texts(profile, [("", c) for c in chunks]), profile=profile)
    cur.execute(
        "DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = %s",
        (str(episode_id),),
    )
    _upsert_chunks(cur, profile, [
        ("episode", str(episode_id), i, c, _md5(c), v)
        for i, (c, v) in enumerate(zip(chunks, vecs))
    ])
    return True


def _cmd_episode_edit(args: argparse.Namespace) -> int:
    """`khipu episode edit ID --summary TEXT` — correct a wrong summary.

    Re-embedding is fail-open, the same posture as embed-on-capture: the
    correction is durable with no embedding profile and no API key, and the
    payload says whether the vectors moved with it rather than pretending.
    """
    from khipu.db import connect

    eid = int(args.id)
    summary = (getattr(args, "summary", None) or "").strip()
    if not summary:
        print(json.dumps({"ok": False, "id": eid, "error": "--summary must not be empty"}))
        return 2
    reembedded = False
    with connect() as conn:
        with conn.cursor() as cur:
            # A forgotten episode is not edited: its re-embed would bring a
            # vector back for a row every reader skips.
            cur.execute("UPDATE episodes SET summary = %s WHERE id = %s AND deleted_at IS NULL",
                        (summary, eid))
            updated = cur.rowcount > 0
            if updated:
                try:
                    reembedded = _reembed_episode(cur, eid)
                except Exception as exc:  # noqa: BLE001 — the edit itself stands
                    print(
                        f"warn: re-embed skipped: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
        conn.commit()
    print(json.dumps({"ok": updated, "id": eid, "reembedded": reembedded}))
    return 0 if updated else 1


def cmd_episode(args: argparse.Namespace) -> int:
    """W5.6: `khipu episode forget ID` — soft-delete + remove its vectors.
    Plus `khipu episode edit ID --summary TEXT` (desktop overhaul phase 3)."""
    episode_cmd = getattr(args, "episode_cmd", None)
    if episode_cmd == "edit":
        return _cmd_episode_edit(args)
    if episode_cmd != "forget":
        print(json.dumps({
            "ok": False,
            "error": "usage: khipu episode forget ID | khipu episode edit ID --summary TEXT",
        }))
        return 2
    from khipu.forget import forget_everywhere, plain_summary

    out = forget_everywhere(int(args.id))
    print(json.dumps(out, default=str))
    # The JSON is for scripts; the same result in plain words goes to stderr.
    print(out.get("message") or plain_summary(out), file=sys.stderr)
    snapshot_ok = (out.get("snapshot") or {}).get("ok", True)
    legacy_reason = str((out.get("legacy_file") or {}).get("reason") or "")
    legacy_ok = not legacy_reason.startswith("unreadable:")
    return 0 if out.get("ok") and snapshot_ok and legacy_ok else 1


def cmd_topic(args: argparse.Namespace) -> int:
    """W5.6: `khipu topic purge SLUG --yes` — hard-delete a TOMBSTONED topic
    (deleted_at already set) and its revisions/embeddings. The command a
    migration comment (0003_reconcile_upsert.sql) promised but nobody wrote."""
    if getattr(args, "topic_cmd", None) != "purge":
        print(json.dumps({"ok": False, "error": "usage: khipu topic purge SLUG --yes"}))
        return 2
    if not getattr(args, "yes", False):
        print(json.dumps({"ok": False, "error": "refusing: pass --yes to confirm a hard delete"}))
        return 2
    from khipu.db import connect

    slug = args.slug
    with connect() as conn:
        with conn.cursor() as cur:
            # Locked first: an embedding catch-up holds this row shared while it
            # writes vectors, so neither can leave vectors for a purged topic.
            cur.execute("SELECT deleted_at FROM topics WHERE slug = %s FOR UPDATE", (slug,))
            row = cur.fetchone()
            if row is None:
                print(json.dumps({"ok": False, "error": f"no such topic: {slug}"}))
                return 1
            if row[0] is None:
                print(json.dumps({
                    "ok": False,
                    "error": "topic is not tombstoned (deleted_at IS NULL) — purge only "
                             "removes a topic reconcile already tombstoned; delete its file "
                             "first and let reconcile tombstone it",
                }))
                return 1
            cur.execute("DELETE FROM memory_embeddings WHERE kind = 'topic' AND ref = %s", (slug,))
            embeddings_removed = cur.rowcount
            cur.execute("DELETE FROM topic_revisions WHERE slug = %s", (slug,))
            revisions_removed = cur.rowcount
            cur.execute("DELETE FROM topics WHERE slug = %s", (slug,))
        conn.commit()
    # The hub delete is durable from here; this Mac's replica would keep
    # serving the topic until the next full refresh unless it is purged too.
    from khipu.hub_snapshot import purge_topic_in_snapshot

    replica = purge_topic_in_snapshot(slug)
    replica_ok = bool(replica.get("ok"))
    print(json.dumps({
        "ok": replica_ok, "slug": slug, "revisions_removed": revisions_removed,
        "embeddings_removed": embeddings_removed,
        "hub": {"ok": True, "revisions_removed": revisions_removed,
                "embeddings_removed": embeddings_removed},
        "replica": replica,
    }))
    if not replica_ok:
        message = (
            f"Purged topic {slug} from the hub, but this Mac's local replica still "
            f"has it ({replica.get('reason')}) and can still return it. The "
            "replica drops it at its next full refresh, which copies the hub."
        )
    elif not replica.get("applicable", True):
        message = f"Purged topic {slug} from the hub. This Mac has no local replica."
    else:
        message = f"Purged topic {slug} from the hub and from this Mac's local replica."
    print(message, file=sys.stderr)
    return 0 if replica_ok else 1


def cmd_backfill(args: argparse.Namespace) -> int:
    """W1.5: `khipu backfill identity [--dry-run|--apply]`."""
    if getattr(args, "backfill_cmd", None) != "identity":
        print(json.dumps({"ok": False, "error": "usage: khipu backfill identity [--dry-run|--apply]"}))
        return 2
    from khipu.db import connect
    from khipu import hygiene

    apply = bool(getattr(args, "apply", False))
    with connect() as conn:
        with conn.cursor() as cur:
            if apply:
                report = hygiene.apply_backfill_identity(cur, limit=getattr(args, "limit", None))
            else:
                report = hygiene.backfill_identity_report(cur, sample_limit=getattr(args, "limit", None) or 20)
        if apply:
            conn.commit()
    report["dry_run"] = not apply
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    """K6: `khipu project backfill [--dry-run|--apply] [--limit N]` — resolve
    a project for episodes that have none, from the session's other episodes
    or a known repo_root. Never from a session id or free-text scope."""
    if getattr(args, "project_cmd", None) != "backfill":
        print(json.dumps({"ok": False, "error": "usage: khipu project backfill [--dry-run|--apply]"}))
        return 2
    from khipu.db import connect
    from khipu import hygiene

    apply = bool(getattr(args, "apply", False))
    limit = getattr(args, "limit", None)
    with connect() as conn:
        with conn.cursor() as cur:
            if apply:
                report = hygiene.apply_backfill_project(cur, limit=limit)
            else:
                report = hygiene.backfill_project_report(cur, sample_limit=limit or 20)
        if apply:
            conn.commit()
    report["dry_run"] = not apply
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_hygiene_commitments(args: argparse.Namespace) -> int:
    """`khipu hygiene commitments [--session-ended] [--dry-run|--apply]
    [--project X] [--limit N]`.

    Default pass: re-judges every OPEN commitment against the one commitment
    definition (``khipu.commitments.COMMITMENT_DEFINITION``) — deterministic
    filter first, then the summariser model in batches — and drops the rejects
    / merges the paraphrase duplicates.

    ``--session-ended``: the model-free ownership pass (2026-09-04). Infers
    ``owner``/``future_trigger`` for every open row with the same
    deterministic rules capture uses, closes the assistant-owned rows with no
    cross-session trigger whose session has ended, and drops within-session
    reporting duties.

    Both are dry runs by default and print every verdict, so a human reads the
    list before anything is written. ``--apply`` takes a binary-COPY backup of
    the whole table first and never DELETEs a row.
    """
    from khipu import hygiene
    from khipu.db import connect

    apply = bool(getattr(args, "apply", False))
    project = getattr(args, "project", None)
    limit = getattr(args, "limit", None)
    session_ended = bool(getattr(args, "session_ended", False))
    max_calls = int(getattr(args, "max_calls", None) or hygiene.MAX_JUDGE_CALLS)
    backup_dir = None
    with connect() as conn:
        if apply:
            backup_dir = hygiene.backup_commitments(conn)
        with conn.cursor() as cur:
            if session_ended:
                report = hygiene.run_session_ended_pass(
                    cur, apply=apply, project=project,
                    limit=int(limit) if limit else None,
                    hours=int(getattr(args, "ended_after_hours", None)
                              or hygiene.SESSION_ENDED_HOURS),
                )
            else:
                report = hygiene.run_commitments_hygiene(
                    cur, apply=apply, project=project,
                    limit=int(limit) if limit else None, max_calls=max_calls,
                )
        if apply:
            conn.commit()
    if backup_dir is not None:
        report["backup"] = str(backup_dir)
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_hygiene(args: argparse.Namespace) -> int:
    """W5.2: `khipu hygiene paths [--dry-run|--apply]`, plus
    `khipu hygiene commitments` (Owed quality, 2026-09-04)."""
    if getattr(args, "hygiene_cmd", None) == "commitments":
        return cmd_hygiene_commitments(args)
    if getattr(args, "hygiene_cmd", None) != "paths":
        print(json.dumps({
            "ok": False,
            "error": "usage: khipu hygiene paths|commitments [--dry-run|--apply]",
        }))
        return 2
    from khipu.db import connect
    from khipu import hygiene

    apply = bool(getattr(args, "apply", False))
    with connect() as conn:
        with conn.cursor() as cur:
            if apply:
                report = hygiene.apply_purge_junk_paths(cur)
            else:
                report = hygiene.report_junk_paths(cur)
        if apply:
            conn.commit()
    report["dry_run"] = not apply
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    """W4.3 + Phase 5 (organisation without the user): `khipu notes ...`.

    `reconcile` mirrors harness-native per-project notes into topics,
    append-only. `index`/`split`/`stale`/`supersede` (Phase 5) are the
    maintainer-facing verbs for the same organisation that ALSO runs by
    itself from every reconcile (khipu.organise.after_reconcile) — these
    exist for a manual pass, never as the only trigger. Never runs against
    the live hub except through the real khipu.db.connect() this shares
    with every other write path."""
    cmd = getattr(args, "notes_cmd", None)
    from khipu import notes

    if cmd == "reconcile":
        report = notes.reconcile(
            dry_run=bool(getattr(args, "dry_run", False)),
            changed_only=bool(getattr(args, "changed_only", False)),
        )
        print(json.dumps(report, indent=2, default=str))
        return 0
    if cmd == "index":
        from khipu import organise

        project_dir = Path(args.project).expanduser()
        if not project_dir.is_dir():
            print(json.dumps({"ok": False, "error": f"no such directory: {project_dir}"}))
            return 2
        cur = None
        conn = None
        if not args.dry_run:
            try:
                from khipu.db import connect

                conn = connect()
                cur = conn.cursor()
            except Exception as exc:  # noqa: BLE001 — ranking degrades without hits_30d, never blocks
                print(f"[khipu-notes] hits_30d ranking unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            report = organise.rewrite_index(
                project_dir, cap_bytes=int(args.cap), dry_run=bool(args.dry_run),
                force=bool(args.force), cur=cur,
            )
        finally:
            if conn is not None:
                conn.commit()
                conn.close()
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("ok") else 2
    if cmd == "split":
        from khipu import organise

        path = Path(args.path).expanduser()
        report = organise.split_note(path, dry_run=bool(args.dry_run))
        if report.get("ok") and not report.get("skipped") and not args.dry_run:
            report["reconcile"] = notes.reconcile(dry_run=False)
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("ok") else 2
    if cmd == "stale":
        from khipu import organise
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                report = organise.stale_report(cur, project=getattr(args, "project", None))
        print(json.dumps(report, indent=2, default=str))
        return 0
    if cmd == "supersede":
        from khipu import organise
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                report = organise.supersede(cur, args.old_slug, args.new_slug)
            conn.commit()
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("ok") else 2
    print(json.dumps({
        "ok": False,
        "error": "usage: khipu notes {reconcile,index,split,stale,supersede}",
    }))
    return 2


# Secrets the UI and CLI may write. Anything outside this set is refused rather
# than silently creating a stray Keychain item under the Khipu service.
# Keyed by account, because secrets_status() does not name its fields after the
# Keychain accounts: database_url reports as "dsn_in_keychain", not "database_*".
SETTABLE_SECRETS = {
    "gemini_api_key": "gemini_in_keychain",
    "anthropic_api_key": "anthropic_in_keychain",
    "openai_api_key": "openai_in_keychain",
    "openrouter_api_key": "openrouter_in_keychain",
    "database_url": "dsn_in_keychain",
    "openai_compat_api_key": "openai_compat_in_keychain",
    "voyage_api_key": "voyage_in_keychain",
}


def cmd_secrets(args: argparse.Namespace) -> int:
    from khipu.keychain import secrets_status, set_password

    account = getattr(args, "set", None)
    if account:
        if account not in SETTABLE_SECRETS:
            print(
                json.dumps({"ok": False, "error": f"not a settable secret: {account}"})
            )
            return 2
        # The value arrives on stdin, never as an argument: argv is world-readable
        # through `ps`, and a secret typed as a flag would also land in shell
        # history. Only the trailing newline the caller adds is stripped.
        value = sys.stdin.read().strip()
        if not value:
            print(json.dumps({"ok": False, "error": "empty value on stdin"}))
            return 2
        try:
            set_password(account, value)
        except (RuntimeError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        # Report presence by re-reading, so a success claim reflects the
        # Keychain's state rather than the absence of an exception.
        status = secrets_status()
        stored = bool(status.get(SETTABLE_SECRETS[account]))
        print(json.dumps({"ok": True, "account": account, "stored": stored}))
        return 0

    print(json.dumps(secrets_status(), indent=2))
    return 0


def cmd_secrets_verify(args: argparse.Namespace) -> int:
    from khipu.modelcheck import check_model_keys

    if getattr(args, "gemini", False):
        which = "gemini"
    elif getattr(args, "openai", False):
        which = "openai"
    else:
        which = "all"
    try:
        result = check_model_keys(which=which)
    except ValueError:
        print(json.dumps({"ok": False, "error": "Unknown check selection."}))
        return 2
    except Exception:  # noqa: BLE001 - fixed text: an error here could echo a key
        print(json.dumps({"ok": False, "error": "The key check could not be completed."}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_activity(args: argparse.Namespace) -> int:
    from khipu.activity import activity_payload, episode_detail

    if args.show is not None:
        row = episode_detail(args.show)
        if row is None:
            print(f"error: episode {args.show} not found", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, default=str))
        return 0
    print(json.dumps(activity_payload(limit=args.limit), indent=2, default=str))
    return 0


def cmd_regen_memory(args: argparse.Namespace) -> int:
    if getattr(args, "index", False) is True:
        from khipu.jobs import BUILD_INDEX, run_build_index

        # build_index.py hardcodes ROOT; --memory-root cannot redirect it.
        engine_root = BUILD_INDEX.resolve().parent.parent
        mem = getattr(args, "memory_root", None)
        if mem:
            passed = Path(mem).resolve()
            if passed != engine_root:
                print(
                    f"error: --memory-root {passed} does not match build_index.py "
                    f"engine root {engine_root} (the engine hardcodes ROOT; "
                    "omit --memory-root or pass that path)",
                    file=sys.stderr,
                )
                return 2
        rc = run_build_index()
        print(f"build_index exited {rc} (memory root {engine_root})")
        return rc

    from khipu.memory_md import regen_memory_md

    out = args.out
    if not out:
        mem = _require_memory_root(args)
        if mem is None:
            return 2
        out = mem / "MEMORY.from-khipu.md"
    n = regen_memory_md(Path(out), limit=args.limit)
    print(f"wrote {n} topics → {out}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from khipu.mirror import reconcile_memory_root

    mem = _require_memory_root(args)
    if mem is None:
        return 2
    stats = reconcile_memory_root(mem)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    from khipu.capture import capture, load_payload

    raw = (
        Path(args.payload_file).read_text(encoding="utf-8")
        if args.payload_file
        else sys.stdin.read()
    )
    return capture(load_payload(raw), mode=args.mode)


def cmd_capture_now(args: argparse.Namespace) -> int:
    """K1: `khipu capture now [--note TEXT] [--harness H --session-id ID]` —
    flags a session for capture on its next Stop/PreCompact/SessionEnd,
    regardless of cadence. With no explicit --harness/--session-id, targets
    the most recently active local session (newest per-session state file)."""
    from khipu.session_capture import request_capture_now, resolve_session_ref

    try:
        harness, sid, resolved_by = resolve_session_ref(
            getattr(args, "harness", None) or None, getattr(args, "session_id", None) or None)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    path = request_capture_now(harness, sid, note=getattr(args, "note", None))
    print(json.dumps({
        "queued": True, "captured_by": "next stop",
        "harness": harness, "session_id": sid, "flag": str(path), "resolved_by": resolved_by,
    }))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    """`khipu get ID [--kind episode|topic]` — episode or topic detail for
    local inspection, including the verbatim tier (K2). Same data khipu_get
    (MCP) returns."""
    from khipu.activity import episode_detail, topic_detail

    ident = str(args.id)
    kind = (getattr(args, "kind", None) or "").strip().lower() or None
    from khipu import library_search

    if kind is None and library_search.is_library_id(ident):
        kind = "library"
    if kind == "library":
        # library:<name>:<doc> or library:<name>:<doc>#<chunk>: the chunk (or
        # document) plus the chunks either side of a hit.
        try:
            print(json.dumps(library_search.get(ident), indent=2, default=str))
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 1
        return 0
    if kind is None:
        kind = "episode" if ident.isdigit() else "topic"
    if kind == "episode":
        if not ident.isdigit():
            print(json.dumps({"ok": False, "error": "episode id must be digits"}))
            return 2
        row = episode_detail(int(ident))
    else:
        from khipu.text_pages import page_args

        try:
            offset, max_bytes = page_args(
                getattr(args, "offset", None), getattr(args, "max_bytes", None))
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        if getattr(args, "all", False):
            return _get_topic_all(ident, offset, max_bytes)
        row = topic_detail(ident, offset=offset, max_bytes=max_bytes)
    if row is None:
        print(json.dumps({"ok": False, "error": f"{kind} not found: {ident}"}))
        return 1
    row = dict(row)
    verbatim = row.pop("verbatim", None) if kind == "episode" else None
    row.pop("raw", None)
    print(json.dumps({"kind": kind, **row}, indent=2, default=str))
    if verbatim:
        print("\nverbatim:")
        print(json.dumps(verbatim, indent=2, default=str))
    return 0


def _get_topic_all(ident: str, offset: int, max_bytes: int) -> int:
    """`khipu get <topic> --all`: follow body_next_offset and write the body
    from ``offset`` to the end on stdout, raw, one page at a time. Stops with
    an error if the topic changes between pages (content_hash or updated_at),
    so the output is never a splice of two versions."""
    from khipu.activity import topic_detail

    first = None
    while True:
        row = topic_detail(ident, offset=offset, max_bytes=max_bytes)
        if row is None:
            print(json.dumps({"ok": False, "error": f"topic not found: {ident}"}),
                  file=sys.stderr)
            return 1
        version = (row.get("content_hash"), row.get("updated_at"))
        if first is None:
            first = version
        elif version != first:
            print(json.dumps({"ok": False, "error": f"topic changed while paging: {ident}"}),
                  file=sys.stderr)
            return 1
        # UTF-8 bytes whatever the locale, so the output equals the stored body.
        out = getattr(sys.stdout, "buffer", None)
        if out is not None:
            sys.stdout.flush()
            out.write(row["body"].encode("utf-8"))
            out.flush()
        else:
            sys.stdout.write(row["body"])
        nxt = row.get("body_next_offset")
        if row.get("complete") or nxt is None:
            sys.stdout.flush()
            return 0
        if nxt <= offset:
            print(json.dumps({"ok": False, "error": f"paging made no progress at {offset}"}),
                  file=sys.stderr)
            return 1
        offset = nxt


def _owner_or_request(args: argparse.Namespace) -> "dict | int":
    """{} for the owner's write (the app, or a typed yes on a terminal), a
    conversation's request with ``--at-owner-request``, or the exit code
    after a refusal was printed."""
    from khipu import routing

    if getattr(args, "at_owner_request", None) is not None:
        words = routing._one_line(args.at_owner_request)
        if not words:
            print(json.dumps({"ok": False, "error": "describe the owner's request in one line",
                              "code": "request_needs_words"}))
            return 2
        return {"request": words, "by": routing.conversation_author(), "at": routing._iso(routing._now())}
    via = _owner_only(args)
    return via if isinstance(via, int) else {}


def _owner_only(args: argparse.Namespace) -> "str | int":
    """The owner check for a config change only the owner makes (the app,
    or a typed yes on a terminal; ``routing._owner_via``): how it was
    authorised ("app" or "cli"), or the exit code after a refusal was
    printed."""
    from khipu import routing

    auth = routing._OWNER_AUTH.set(None)
    try:
        via = routing._owner_via(args)
    finally:
        routing._OWNER_AUTH.reset(auth)
    if via is None:
        print(json.dumps({"ok": False, "error": routing._REFUSED, "code": "owner_only"}))
        return 2
    return via


def cmd_config(args: argparse.Namespace) -> int:
    from khipu.config import (
        CHOICE_SETTINGS,
        FLOAT_SETTINGS,
        INT_SETTINGS,
        LIST_SETTINGS,
        MINUTE_SETTINGS,
        capture_mode,
        config_file,
        gateway_url,
        load_config,
        set_capture_mode,
        set_gateway_url,
    )

    if args.set_capture_mode:
        path = set_capture_mode(args.set_capture_mode)
        print(json.dumps({"capture_mode": capture_mode(), "config_file": str(path)}))
        return 0
    if args.set_gateway_url is not None:
        path = set_gateway_url(args.set_gateway_url)
        print(json.dumps({"gateway_url": gateway_url(), "config_file": str(path)}))
        return 0
    if args.set and args.set[0].startswith("relevance.cosine_floor_by_profile."):
        from khipu import relevance

        key, raw = args.set
        profile = key[len(relevance.BY_PROFILE_KEY):]
        try:
            value = float(raw)
            path = relevance.set_cosine_floor_for_profile(profile, value)
        except (TypeError, ValueError) as err:
            msg = str(err)
            if msg.startswith("could not convert"):
                msg = f"{key} must be a number, got {raw!r}"
            print(json.dumps({"ok": False, "error": msg}))
            return 2
        print(json.dumps({"ok": True,
                          "relevance_cosine_floor_by_profile": relevance.cosine_floor_by_profile(),
                          "config_file": str(path)}))
        return 0
    if args.unset and args.unset.startswith("relevance.cosine_floor_by_profile."):
        from khipu import relevance

        try:
            path = relevance.set_cosine_floor_for_profile(
                args.unset[len(relevance.BY_PROFILE_KEY):], None)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        print(json.dumps({"ok": True,
                          "relevance_cosine_floor_by_profile": relevance.cosine_floor_by_profile(),
                          "config_file": str(path)}))
        return 0
    if args.set and args.set[0] == "relevance.cosine_floor":
        from khipu import relevance

        _key, raw = args.set
        try:
            value = float(raw)
        except (TypeError, ValueError):
            print(json.dumps({"ok": False, "error": f"relevance.cosine_floor must be a number, got {raw!r}"}))
            return 2
        if not (0.0 < value <= 1.0):  # also refuses nan
            print(json.dumps({"ok": False, "error": f"relevance.cosine_floor must be above 0 and at most 1, got {value}"}))
            return 2
        path = relevance.set_cosine_floor(value)
        print(json.dumps({"ok": True, "relevance_cosine_floor": relevance.cosine_floor_status(),
                          "config_file": str(path)}))
        return 0
    if args.unset == "relevance.cosine_floor":
        from khipu import relevance

        path = relevance.set_cosine_floor(None)
        print(json.dumps({"ok": True, "relevance_cosine_floor": relevance.cosine_floor_status(),
                          "config_file": str(path)}))
        return 0
    if (args.set and args.set[0] == "usage_accounts") or args.unset == "usage_accounts":
        # The accounts `khipu usage` reads, as JSON (plan usage phase 2):
        # `--set usage_accounts '[{"label": "Claude second", "harness":
        # "claude", "home": "~/.claude-second"}]'`; `--unset` (or `[]`) goes
        # back to every installed CLI with its default login.
        from khipu import accounts, routing, usage
        from khipu.config import config_file

        # Decision 15: the owner's (the app, or a typed yes on a terminal),
        # or a conversation's at his request, marked until he answers it.
        request = via = None
        # The owner's check stays set through the save, so the record's
        # lines about it say how it was authorised.
        auth = routing._OWNER_AUTH.set(None)
        try:
            if getattr(args, "at_owner_request", None) is not None:
                words = routing._one_line(args.at_owner_request)
                if not words:
                    print(json.dumps({"ok": False, "error": "describe the owner's request in one line",
                                      "code": "request_needs_words"}))
                    return 2
                request = {"request": words, "by": routing.conversation_author(),
                           "at": routing._iso(routing._now())}
            else:
                via = routing._owner_via(args)
                if via is None:
                    print(json.dumps({"ok": False, "error": routing._REFUSED, "code": "owner_only"}))
                    return 2
            # One write path for the list: the save becomes the account
            # operations it implies (track, rename, stop, move), each with
            # its lines in the routing record, on the version it read.
            saved = accounts.save_list(args.set[1] if args.set else None, expect=getattr(args, "expect", None),
                                       via=via or "cli", request=request)
        except usage.AccountsError as err:
            # A fixed code, the entry and the field at fault: the app says it
            # in its own words and never shows the text.
            print(json.dumps(err.refusal()))
            return 2
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        finally:
            routing._OWNER_AUTH.reset(auth)
        print(json.dumps({"ok": True, "version": saved["version"], "usage_accounts": usage.accounts_status(),
                          "config_file": str(config_file())}))
        return 0
    from khipu.config import CLI_SETTINGS

    if (args.set and args.set[0] in CLI_SETTINGS) or args.unset in CLI_SETTINGS:
        # Where a CLI and its Node live, set in the app (settings clarity
        # phase 9, decision 10): `--set cli_path.codex /path/to/codex`,
        # `--set node_folder /path/to/node/bin`; `--unset` clears it.
        from khipu.config import cli_settings_status, set_cli_setting

        key = args.set[0] if args.set else args.unset
        try:
            path = set_cli_setting(key, args.set[1] if args.set else None)
        except (KeyError, ValueError) as err:
            print(json.dumps({"ok": False, "error": str(err).strip("'\""), "key": key}))
            return 2
        print(json.dumps({"ok": True, "cli_settings": cli_settings_status(), "config_file": str(path)}))
        return 0
    if (args.set and args.set[0] == "graph_backup_remote") or args.unset == "graph_backup_remote":
        # Where `graph-backup offsite` copies the graph snapshot (rclone
        # remote:path); `--unset` goes back to the default.
        from khipu.config import graph_backup_remote_status, set_graph_backup_remote

        try:
            path = set_graph_backup_remote(args.set[1] if args.set else None)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        print(json.dumps({"ok": True, "graph_backup_remote": graph_backup_remote_status(),
                          "config_file": str(path)}))
        return 0
    from khipu import replica_heal

    if (args.set and args.set[0] in replica_heal.SETTINGS) or args.unset in replica_heal.SETTINGS:
        # This Mac's copy (offline design A): how old before the background
        # job updates it, and how often it looks. The check interval is baked
        # into the job's plist, so changing it re-renders the job.
        key = args.set[0] if args.set else args.unset
        try:
            path = replica_heal.set_setting(key, args.set[1] if args.set else None)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        reply = {"ok": True, key: replica_heal.settings_status()[key], "config_file": str(path)}
        if key == "replica_check_every":
            from khipu import launchd_gen

            reply["job"] = launchd_gen.rerender_replica_job()
        print(json.dumps(reply))
        return 0
    from khipu import ledger as _ledger

    if (args.set and args.set[0] in _ledger.SETTINGS) or args.unset in _ledger.SETTINGS:
        # Every ledger limit and time (Work view, rule 7): `--set
        # ledger_title_chars 0` lifts it, `--unset` for the default again.
        key = args.set[0] if args.set else args.unset
        try:
            path = _ledger.set_setting(key, args.set[1] if args.set else None)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        print(json.dumps({"ok": True, key: _ledger.settings_status()[key], "config_file": str(path)}))
        return 0
    from khipu import work as _work

    if (args.set and args.set[0] in _work.SETTINGS) or args.unset in _work.SETTINGS:
        # Every work-view limit and time (rule 7): `--set work_max_jobs 0`
        # lifts it, `--unset` for the default again.
        key = args.set[0] if args.set else args.unset
        try:
            path = _work.set_setting(key, args.set[1] if args.set else None)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        print(json.dumps({"ok": True, key: _work.settings_status()[key], "config_file": str(path)}))
        return 0
    from khipu import offline as _offline

    if (args.set and args.set[0] in _offline.SETTINGS) or args.unset in _offline.SETTINGS:
        # How long the hub is waited on before this Mac's copy is used: `--set
        # hub_statement_timeout 0` lifts it, `--unset` for the default again.
        key = args.set[0] if args.set else args.unset
        try:
            path = _offline.set_setting(key, args.set[1] if args.set else None)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        print(json.dumps({"ok": True, key: _offline.settings_status()[key], "config_file": str(path)}))
        return 0
    from khipu import routing as _routing

    if isinstance(getattr(args, "answer_request", None), (list, tuple)):
        # The owner's answer to a conversation's change to a routing setting.
        key, action = args.answer_request
        if isinstance(_owner_only(args), int):
            return 2
        try:
            path = _routing.answer_setting_request(key, action)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err), "code": getattr(err, "code", None)}))
            return 2
        print(json.dumps({"ok": True, "action": action, key: _routing.settings_status().get(key),
                          "config_file": str(path)}))
        return 0
    if (args.set and args.set[0] in _routing.SETTINGS) or args.unset in _routing.SETTINGS:
        # Every size, time and switch of the routing record (routing truth
        # scope, decision 13): `--set routing_stale_after 12h`, `--unset` for
        # the default again.
        key = args.set[0] if args.set else args.unset
        # Decision 15, as for the account list: the owner's (the app, or a
        # typed yes on a terminal), or a conversation's at his request,
        # marked until he answers it. A lane map or a job runner folder
        # decides whose limits a report counts against.
        request = _owner_or_request(args)
        if isinstance(request, int):
            return request
        try:
            path = _routing.set_setting(key, args.set[1] if args.set else None, request=request or None)
        except ValueError as err:
            out = {"ok": False, "error": str(err)}
            if getattr(err, "code", None):
                out["code"] = err.code
            print(json.dumps(out))
            return 2
        print(json.dumps({"ok": True, key: _routing.settings_status()[key], "config_file": str(path)}))
        return 0
    if (args.set and args.set[0] in CHOICE_SETTINGS) or args.unset in CHOICE_SETTINGS:
        from khipu.config import choice_setting, set_choice_setting

        key = args.set[0] if args.set else args.unset
        try:
            path = set_choice_setting(key, args.set[1] if args.set else None)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        value, source = choice_setting(key)
        print(json.dumps({"ok": True, key: value, "source": source, "config_file": str(path)}))
        return 0
    if (args.set and args.set[0] == "promote_gate_ignore") or args.unset == "promote_gate_ignore":
        return _config_gate_override(args)
    if (args.set and args.set[0] in MINUTE_SETTINGS) or args.unset in MINUTE_SETTINGS:
        from khipu.config import minute_settings_status, set_minute_setting

        key = args.set[0] if args.set else args.unset
        try:
            path = set_minute_setting(key, args.set[1] if args.set else None)
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        print(json.dumps({"ok": True, key: minute_settings_status()[key], "config_file": str(path)}))
        return 0
    if (args.set and args.set[0] in INT_SETTINGS) or args.unset in INT_SETTINGS:
        # Daily call ceilings (synth_daily_calls, embed_daily_calls,
        # library_embed_daily_calls): whole numbers >= 0, 0 = unlimited.
        from khipu.config import parse_int_setting, set_int_setting
        from khipu.embed import budget_status

        key = args.set[0] if args.set else args.unset
        try:
            value = parse_int_setting(key, args.set[1]) if args.set else None
        except ValueError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        path = set_int_setting(key, value)
        print(json.dumps({"ok": True, key: value,
                          "budget": budget_status(INT_SETTINGS[key]),
                          "config_file": str(path)}))
        return 0
    if args.set and args.set[0] in FLOAT_SETTINGS:
        # dedup_similarity / commitment_close_similarity are floats, not paths.
        # `--set` routed every key through set_path_setting, which stored them
        # as expanduser()'d STRINGS — float_setting then ignored the stored
        # value (it accepts int/float only) and silently kept the default, so
        # tuning either knob from the CLI did nothing (audit 2026-09-04).
        from khipu.config import float_setting, set_float_setting

        key, raw = args.set
        try:
            value = float(raw)
        except (TypeError, ValueError):
            print(json.dumps({"ok": False, "error": f"{key} must be a number, got {raw!r}"}))
            return 2
        if not (0.0 <= value <= 1.0):
            print(json.dumps({"ok": False, "error": f"{key} must be between 0 and 1, got {value}"}))
            return 2
        path = set_float_setting(key, value)
        print(json.dumps({"ok": True, key: float_setting(key), "config_file": str(path)}))
        return 0
    if args.set and args.set[0] in LIST_SETTINGS:
        # user_aliases is a comma-separated list, not a path (2026-09-05):
        # `khipu config --set user_aliases "matt,matthew"`.
        from khipu.commitments import reset_user_patterns
        from khipu.config import list_setting, set_list_setting

        key, raw = args.set
        path = set_list_setting(key, raw)
        if key == "user_aliases":
            reset_user_patterns()
        print(json.dumps({"ok": True, key: list(list_setting(key)), "config_file": str(path)}))
        return 0
    if args.set or args.unset:
        from khipu.config import path_settings_status, set_path_setting

        try:
            if args.set:
                key, value = args.set
                path = set_path_setting(key, value)
            else:
                key, path = args.unset, set_path_setting(args.unset, None)
        except KeyError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        print(
            json.dumps(
                {"ok": True, key: path_settings_status()[key], "config_file": str(path)}
            )
        )
        return 0
    from khipu import relevance, usage
    from khipu.config import (
        cli_settings_status,
        float_settings_status,
        graph_backup_remote_status,
        list_setting,
        minute_settings_status,
        path_settings_status,
    )

    out = {
        "capture_mode": capture_mode(),
        "gateway_url": gateway_url() or None,
        "capture_mode_source": (
            "env"
            if os.environ.get("KHIPU_CAPTURE_MODE")
            else "file"
            if "capture_mode" in load_config()
            else "default"
        ),
        "paths": path_settings_status(),
        "user_aliases": list(list_setting("user_aliases")),
        "float_settings": float_settings_status(),
        "minute_settings": minute_settings_status(),
        "promote_gate_ignore": _promote_gate_ignore_status(),
        "relevance_cosine_floor": relevance.cosine_floor_status(),
        "relevance_cosine_floor_by_profile": relevance.cosine_floor_by_profile(),
        "usage_accounts": usage.accounts_status(),
        "cli_settings": cli_settings_status(),
        "routing": _routing.settings_status(),
        "ledger": _ledger.settings_status(),
        "replica": replica_heal.settings_status(),
        "work": _work.settings_status(),
        "offline": _offline.settings_status(),
        "graph_backup_remote": graph_backup_remote_status(),
        "config_file": str(config_file()),
        "config": load_config(),
    }
    print(json.dumps(out, indent=2))
    return 0


def _config_gate_override(args: argparse.Namespace) -> int:
    """`config --set promote_gate_ignore CHECKS` / `--unset`: the owner's
    one-time "Update anyway". The next promotion skips these red checks
    once, names them in its record, then clears it; unused, it lapses after
    a day. Only the owner sets or cancels it (the app, or a typed yes)."""
    from khipu import promote as _promote

    via = _owner_only(args)
    if isinstance(via, int):
        return via
    try:
        path = _promote.set_gate_ignore(args.set[1] if args.set else None,
                                        by="app" if via == "app" else "terminal")
    except ValueError as err:
        print(json.dumps({"ok": False, "error": str(err)}))
        return 2
    print(json.dumps({"ok": True, "promote_gate_ignore": _promote.gate_ignore_status(),
                      "config_file": str(path)}))
    return 0


def _promote_gate_ignore_status() -> dict:
    try:
        from khipu.promote import gate_ignore_status

        return gate_ignore_status()
    except Exception as e:  # noqa: BLE001 — showing config must not fail on this
        return {"value": [], "choices": [], "error": f"{type(e).__name__}: {e}"}


BUDGET_LIFT_SCOPES = ("synth", "memory", "library")


def _budget_status_all() -> dict:
    """Every budget scope's day, calls, cap, remaining, exhausted and where the
    cap came from; the synth scope adds today's tokens and estimated cost."""
    from khipu.embed import budget_status
    from khipu.extract import synth_usage_today

    keys = ("day", "calls", "cap", "remaining", "exhausted", "warn", "cap_source")
    out: dict = {}
    for scope in BUDGET_LIFT_SCOPES:
        st = budget_status(scope)
        out[scope] = {k: st.get(k) for k in keys}
        if st.get("error"):
            out[scope]["error"] = st["error"]
    try:
        usage = synth_usage_today()
        out["synth"]["tokens_today"] = {k: usage[k] for k in
                                        ("prompt_tokens", "output_tokens", "thought_tokens")}
        out["synth"]["usage_by_model"] = usage["by_model"]
        out["synth"]["usage_by_provider"] = usage["by_provider"]
        out["synth"]["estimated_usd_today"] = usage["estimated_usd"]
    except Exception as e:  # noqa: BLE001 — accounting is informational
        out["synth"]["tokens_today"] = None
        out["synth"]["estimated_usd_today"] = None
        out["synth"]["usage_error"] = f"{type(e).__name__}: {e}"
    return out


def cmd_budget(args: argparse.Namespace) -> int:
    """`khipu budget status` / `khipu budget lift SCOPE --today|--cap N|--unlimited`."""
    if args.budget_cmd == "status":
        print(json.dumps(_budget_status_all(), indent=2))
        return 0
    from khipu import session_capture
    from khipu.config import INT_SETTINGS, set_int_setting
    from khipu.embed import budget_reset_today, budget_status

    scope = args.scope
    key = next(k for k, s in INT_SETTINGS.items() if s == scope)
    if args.today:
        cleared = budget_reset_today(scope)
        did = f"reset today's {scope} counter ({cleared['calls_cleared']} calls cleared)"
        out = {"ok": True, "scope": scope, "action": "today", **cleared}
    else:
        if args.cap is not None and args.cap < 0:
            print(json.dumps({"ok": False, "error": f"--cap must be >= 0, got {args.cap}"}))
            return 2
        cap = 0 if args.unlimited else args.cap
        path = set_int_setting(key, cap)
        did = (f"{scope} ceiling set to {'unlimited' if cap == 0 else cap} "
               f"({key} in {path})")
        out = {"ok": True, "scope": scope, "action": "unlimited" if args.unlimited else "cap",
               key: cap, "config_file": str(path)}
    session_capture._log(f"budget lift: {did}")
    out["did"] = did
    out["budget"] = budget_status(scope)
    print(json.dumps(out, indent=2))
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from khipu.migrate import run

    out = run(dry_run=bool(args.dry_run), claim_local_rows=True)
    print(json.dumps(out, indent=2))
    # A pending list after a real run means a migration failed to apply.
    return 0 if args.dry_run or not out["pending"] else 1


def cmd_db(args: argparse.Namespace) -> int:
    """khipu db preflight|connect|move|status — the setup pipeline's CLI face
    (see docs/plans/2026-09-05-setup-that-cannot-strand-you.md). Every
    subcommand prints JSON and never the password."""
    from khipu.setup import connect_database, host_kind, mask_dsn

    def _missing_dsn(flag: str, env_name: str) -> int:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "dsn_required",
                    "detail": f"Pass {flag} or set {env_name}.",
                }
            )
        )
        return 2

    if args.db_cmd == "preflight":
        dsn = args.dsn or os.environ.get("KHIPU_DB_DSN")
        if not dsn:
            return _missing_dsn("--dsn", "KHIPU_DB_DSN")
        out = connect_database(dsn, store=False, install_jobs=False, prove=False)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1

    if args.db_cmd == "connect":
        dsn = args.dsn or os.environ.get("KHIPU_DB_DSN")
        if not dsn and getattr(args, "use_stored", False):
            from khipu.db import resolve_dsn

            dsn = resolve_dsn()
        if not dsn:
            return _missing_dsn("--dsn", "KHIPU_DB_DSN")
        out = connect_database(
            dsn,
            store=True,
            install_jobs=not args.no_jobs,
            prove=not args.no_prove,
            root_crt=args.root_crt,
        )
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1

    if args.db_cmd == "move":
        explicit_target = args.to or os.environ.get("KHIPU_DB_TARGET_DSN")
        if getattr(args, "new_local", False) and explicit_target:
            print(json.dumps({"ok": False, "error": "--new-local conflicts with --to/KHIPU_DB_TARGET_DSN"}))
            return 2
        from khipu.dbmove import move_database

        if getattr(args, "new_local", False):
            from khipu.setup import provision_local_move_target

            provisioned = provision_local_move_target()
            if not provisioned.get("ok"):
                print(json.dumps(provisioned, indent=2))
                return 1
            target = provisioned["dsn"]
        else:
            target = explicit_target
            if not target:
                return _missing_dsn("--to", "KHIPU_DB_TARGET_DSN")

        def _progress(table: str, rows: int) -> None:
            print(f"  {table}: {rows} rows", file=sys.stderr, flush=True)

        out = move_database(
            target,
            dry_run=bool(args.dry_run),
            allow_nonempty=bool(getattr(args, "into_nonempty", False)),
            progress=_progress,
        )
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1

    if args.db_cmd == "status":
        from khipu.db import connect, dsn_configured, resolve_dsn

        if not dsn_configured():
            print(json.dumps({"ok": False, "error": "no_dsn_configured"}))
            return 1
        dsn = resolve_dsn()
        out = {"ok": True, "dsn": mask_dsn(dsn), "host_kind": host_kind(dsn)}
        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM episodes WHERE deleted_at IS NULL")
                    out["episodes"] = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM topics")
                    out["topics"] = cur.fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            out["ok"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1

    print(json.dumps({"ok": False, "error": f"unknown db subcommand: {args.db_cmd}"}))
    return 2


def _add_passphrase_args(parser: argparse.ArgumentParser) -> None:
    """A join kit passphrase never travels as a literal argument (other
    processes can read argv): env, stdin, a file descriptor or the prompt."""
    parser.add_argument("--passphrase", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--passphrase-stdin",
        action="store_true",
        help="Read the join kit passphrase from the first line of stdin "
        "(or set KHIPU_JOIN_PASSPHRASE; a terminal asks)",
    )
    parser.add_argument(
        "--passphrase-fd",
        type=int,
        default=None,
        metavar="FD",
        help="Read the join kit passphrase from the first line of this open file descriptor",
    )


@guard_output
def cmd_join(args: argparse.Namespace) -> int:
    from khipu.join import (
        export_kit,
        import_kit,
        verify_live_counts,
        write_kit_file,
    )

    from khipu.join import LITERAL_PASSPHRASE_REFUSAL, public_error, read_passphrase

    if getattr(args, "passphrase", None) is not None:
        print(json.dumps({"ok": False, "error": LITERAL_PASSPHRASE_REFUSAL}))
        return 2
    allow_unencrypted = bool(getattr(args, "allow_unencrypted", False))
    try:
        passphrase = read_passphrase(
            stdin=bool(getattr(args, "passphrase_stdin", False)),
            fd=getattr(args, "passphrase_fd", None),
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"could not read the passphrase: {type(exc).__name__}"}))
        return 2
    remember(passphrase, "passphrase")
    if (
        args.join_cmd in ("export", "advertise")
        and not passphrase
        and not allow_unencrypted
        and sys.stdin.isatty()
    ):
        import getpass

        first = remember(getpass.getpass("Join kit passphrase: ").strip(), "passphrase")
        if first and remember(getpass.getpass("Type it again: ").strip(), "passphrase") != first:
            print(json.dumps({"ok": False, "error": "the two passphrases did not match"}))
            return 2
        passphrase = first

    if args.join_cmd in ("export", "advertise") and not passphrase and not allow_unencrypted:
        from khipu.join import UNENCRYPTED_REFUSAL

        print(json.dumps({"ok": False, "error": UNENCRYPTED_REFUSAL}))
        return 2

    if args.join_cmd == "export":
        try:
            blob = export_kit(passphrase, allow_unencrypted=allow_unencrypted)
            out_path = write_kit_file(Path(args.out), blob)
            from khipu.join import decrypt_payload

            payload = decrypt_payload(blob, passphrase)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": public_error(exc, "Join export")}))
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "export",
                    "out": str(out_path),
                    "expected": payload.get("expected"),
                    "created_at": payload.get("created_at"),
                },
                indent=2,
            )
        )
        return 0

    if args.join_cmd == "import":
        try:
            blob = Path(args.file).expanduser().read_bytes()
            summary = import_kit(blob, passphrase)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": public_error(exc, "Join import")}))
            return 1
        migrate_out: dict | None = None
        counts = verify_live_counts(summary.get("expected") or {})
        if not getattr(args, "skip_migrate_check", False) and not counts.get("error"):
            from khipu.migrate import run

            try:
                migrate_out = run(dry_run=False)
            except Exception as exc:  # noqa: BLE001 — its text can quote the DSN
                migrate_out = {"ok": False, "error": public_error(exc, "Applying migrations")}
        hub_ok = bool(counts.get("ok"))
        result = {
            "ok": True,
            "kit_imported": True,
            "hub_ok": hub_ok,
            "action": "import",
            "summary": summary,
            "counts": counts,
        }
        if counts.get("error"):
            result["warning"] = counts["error"]
        elif not hub_ok and counts.get("mismatches"):
            result["warning"] = "; ".join(counts["mismatches"])
        if migrate_out is not None:
            result["migrate"] = migrate_out
        print(json.dumps(result, indent=2))
        return 0

    if args.join_cmd == "advertise":
        try:
            from khipu.join_pair import advertise_join_kit

            out = advertise_join_kit(
                passphrase,
                timeout=int(getattr(args, "timeout", 600) or 600),
                pin=getattr(args, "pin", None),
                allow_unencrypted=allow_unencrypted,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": public_error(exc, "Join advertise")}))
            return 1
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1

    if args.join_cmd == "receive":
        try:
            from khipu.join_pair import receive_join_kit

            pin = str(getattr(args, "pin", "") or "").strip()
            out_arg = getattr(args, "out", None)
            out_path = Path(out_arg).expanduser() if out_arg else None
            result = receive_join_kit(passphrase, pin, out_path=out_path)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": public_error(exc, "Join receive")}))
            return 1
        print(json.dumps(result, indent=2))
        return 0 if result.get("kit_imported") or result.get("ok") else 2

    print(
        json.dumps({"ok": False, "error": f"unknown join subcommand: {args.join_cmd}"})
    )
    return 2


def cmd_outbox(args: argparse.Namespace) -> int:
    """Offline outbox: captures whose PG write failed, replayed when PG is back."""
    from khipu import outbox

    if args.outbox_cmd == "status":
        print(json.dumps(outbox.status(), indent=2))
        return 0
    if args.outbox_cmd == "retry-dead":
        print(json.dumps(outbox.retry_dead(), indent=2))
        return 0
    out = outbox.drain(limit=args.limit)
    out["remaining"] = outbox.status()["pending"]
    print(json.dumps(out, indent=2))
    return 0 if not out.get("failed") else 1


def cmd_promote(args: argparse.Namespace) -> int:
    """`khipu promote [ref]`: gate a ref in the inactive live slot and flip to
    it on a pass; `--rollback`, `--status`, `--gate-only STEP` (see
    khipu.promote and docs/plans/2026-10-14-promotion-gate.md)."""
    from khipu import promote as pm

    chosen = [flag for flag in ("rollback", "status", "gate_only", "retry_queued") if getattr(args, flag, None)]
    if len(chosen) > 1:
        print(json.dumps({"ok": False, "error": "choose one of --rollback, --status, --gate-only, --retry-queued"}))
        return 2
    if (args.rollback or args.status or args.retry_queued) and args.ref:
        print(json.dumps({"ok": False, "error": "a ref applies to a promotion or --gate-only only"}))
        return 2
    if args.retry_queued:
        out = pm.retry_queued()
    elif args.status:
        out = pm.status()
    elif args.rollback:
        out = pm.rollback()
    elif args.gate_only:
        out = pm.gate_only(args.gate_only, args.ref)
    else:
        out = pm.promote(args.ref or "origin/main")
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(pm.format_result(out))
    return 0 if out.get("ok") else 1


def cmd_jobs(args: argparse.Namespace) -> int:
    """Scheduled LaunchAgents: status, install, refresh (re-render after an
    app update so a stale bundled-Python path or Application Support path
    lands in the plist without a reinstall), uninstall."""
    from khipu import jobs as jobs_mod
    from khipu import launchd_gen

    if args.jobs_cmd == "status":
        print(json.dumps(jobs_mod.job_status(), indent=2))
        return 0
    names = getattr(args, "names", None) or None
    bad = [n for n in (names or []) if n not in _JOBS_CHOICES]
    if bad:
        print(json.dumps({"ok": False, "error": f"unknown job(s): {bad}; choose from {list(_JOBS_CHOICES)}"}))
        return 2
    if args.jobs_cmd == "install":
        out = launchd_gen.install_scheduled_jobs(names)
    elif args.jobs_cmd == "refresh":
        out = launchd_gen.refresh_scheduled_jobs(names)
    elif args.jobs_cmd == "uninstall":
        out = launchd_gen.uninstall_scheduled_jobs(names)
    else:
        return 2
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


def cmd_snapshot(args: argparse.Namespace) -> int:
    from khipu.hub_snapshot import refresh, snapshot_freshness

    if args.snapshot_cmd == "heal":
        from khipu import jobs, replica_heal

        out = replica_heal.run()
        rc = 0 if out.get("ok") else 1
        jobs._write_job_state("replica_refresh", rc)
        print(json.dumps(out, indent=2))
        return rc
    if args.snapshot_cmd == "status":
        # Local evidence only (no hub connection): the state, per-table
        # cursors and the last measurement status or doctor made.
        print(json.dumps(snapshot_freshness(None), indent=2))
        return 0
    try:
        out = refresh()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 2


def cmd_recall(args: argparse.Namespace) -> int:
    """W2.5: the search query log — recent searches, and queries that came
    back empty (the seed for a zero-result / golden-query review). Also W6.3
    `khipu recall eval` — score the maintainer-local recall-golden.jsonl against default
    search and print hit@k per line plus overall. `serve` / `status` are the warm
    recall service (khipu.recall_daemon) behind the per-prompt hook."""
    if args.recall_cmd == "serve":
        from khipu import recall_daemon

        return recall_daemon.serve()
    if args.recall_cmd == "status":
        from khipu import recall_daemon

        answer = recall_daemon.ping()
        print(json.dumps(answer or {"ok": False, "running": False}, indent=2))
        return 0 if answer else 1
    if args.recall_cmd == "log":
        from khipu import query_log

        print(json.dumps(query_log.tail(args.tail), indent=2, default=str))
        return 0
    if args.recall_cmd == "zero-results":
        from khipu import query_log

        print(json.dumps(query_log.zero_results(args.days), indent=2, default=str))
        return 0
    if args.recall_cmd == "eval":
        from pathlib import Path as _Path

        from khipu import recall_eval

        golden_path = _Path(args.golden) if getattr(args, "golden", None) else None
        path_arg = getattr(args, "path", None) or "explicit"
        record_to = getattr(args, "record", None)
        compare_to = getattr(args, "compare", None)
        replay_from = getattr(args, "replay", None)
        allow_changes = bool(getattr(args, "allow_changes", False))
        budget_ms = int(getattr(args, "budget_ms", None) or recall_eval.DEFAULT_STATUS_BUDGET_MS)

        rerank_arg = getattr(args, "rerank", None)
        if rerank_arg:
            if (path_arg != "explicit" or record_to or compare_to or replay_from
                    or getattr(args, "relevance_floor", None)):
                print(json.dumps({"ok": False, "error": "--rerank scores the explicit path only; "
                                  "it cannot combine with --path, --record, --compare, --replay "
                                  "or --relevance-floor"}))
                return 2
            try:
                entries = recall_eval.load_golden(golden_path or recall_eval.default_golden_path())
            except (OSError, ValueError) as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
                return 2
            report = recall_eval.run_rerank_eval(entries, rerank=(rerank_arg == "on"))
            for row in report["rows"]:
                print(f"rank without={row['rank_without']} with={row['rank_with']} {row['query']!r}",
                      file=sys.stderr)
            print(json.dumps(report, indent=2, default=str))
            return 0

        floor_arg = getattr(args, "relevance_floor", None)
        if floor_arg:
            if record_to or compare_to or replay_from:
                print(json.dumps({"ok": False, "error": "--relevance-floor cannot combine with "
                                  "--record, --compare or --replay"}))
                return 2
            try:
                entries = recall_eval.load_golden(golden_path or recall_eval.default_golden_path())
            except (OSError, ValueError) as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
                return 2
            paths = recall_eval.ALL_PATHS if path_arg == "all" else (path_arg,)
            report = recall_eval.run_relevance_eval(
                entries, paths, floor=(floor_arg == "on"), budget_ms=budget_ms
            )
            for path, summary in report["paths"].items():
                for query in summary.get("emptied_positives", []):
                    print(f"[EMPTIED] path={path} {query!r}", file=sys.stderr)
            print(json.dumps(report, indent=2, default=str))
            summaries = list(report["paths"].values())
            floor_regression = floor_arg == "on" and any(
                not row.get("expect_none")
                and bool(row.get("hit_without"))
                and not bool(row.get("hit_with"))
                for row in report.get("rows", [])
            )
            ok = bool(summaries) and all(
                s.get("entries", 0) > 0
                and s["abstain_correct"] == s["abstain_total"]
                and not s.get("emptied_positives")
                for s in summaries
            ) and not floor_regression
            return 0 if ok else 1

        # Legacy fast path: zero new flags touched -> byte-identical to the
        # original W6.3 command. run_eval/eval_one/load_golden are untouched
        # by Phase 1 session A, so an existing golden file or script must
        # never observe a single JSON key move here.
        if path_arg == "explicit" and not record_to and not compare_to and not replay_from:
            try:
                report = recall_eval.run_eval(golden_path)
            except (OSError, ValueError) as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
                return 2
            for row in report["rows"]:
                mark = "hit " if row["hit"] else "MISS"
                print(f"[{mark}] {row['query']!r} -> got={row['got']} expect={row['expect']}",
                      file=sys.stderr)
            print(json.dumps(report, indent=2, default=str))
            return 0 if report["overall_hit_rate"] >= 0.8 else 1

        paths = recall_eval.ALL_PATHS if path_arg == "all" else (path_arg,)
        try:
            if replay_from:
                if not record_to and not compare_to:
                    raise ValueError("--replay needs --record or --compare")
                entries = recall_eval.load_replay_entries(
                    _Path(replay_from), sample=getattr(args, "sample", None),
                    seed=int(getattr(args, "seed", 0) or 0),
                )
            else:
                entries = recall_eval.load_golden(golden_path or recall_eval.default_golden_path())
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
            return 2

        if record_to:
            record = recall_eval.build_record(entries, paths, budget_ms=budget_ms)
            recall_eval.write_record(_Path(record_to), record)
            print(json.dumps(record, indent=2, default=str))
            return 0

        if compare_to:
            try:
                control = recall_eval.read_record(_Path(compare_to))
            except (OSError, ValueError) as exc:
                print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
                return 2
            result = recall_eval.compare_record(entries, paths, control, budget_ms=budget_ms)
            for d in result["diffs"]:
                print(f"[{d['status']}] path={d['path']} {d['query']!r}", file=sys.stderr)
            print(json.dumps(result, indent=2, default=str))
            material_change = result["changed"] > 0 or any(
                d.get("status") == "reordered"
                or d.get("control_reason") != d.get("live_reason")
                for d in result["diffs"]
            )
            return 0 if allow_changes or not material_change else 1

        # --path prompt|status|all (no --record/--compare): score and print
        # per-path metrics, same "hit@k >= 0.8" gate as legacy, generalized
        # to also require abstention accuracy on any expect_none entries —
        # a path is not passing if it hallucinates memory that isn't there.
        report = recall_eval.run_eval_paths(entries, paths, budget_ms=budget_ms)
        for row in report["rows"]:
            mark = "hit " if row["hit"] else ("abst" if row["expect_none"] else "MISS")
            print(f"[{mark}] path={row['path']} {row['query']!r} -> got={row['got']}", file=sys.stderr)
        print(json.dumps(report, indent=2, default=str))
        ok = all(
            (s["total"] - s["abstain_total"] == 0 or s["hit_rate"] >= 0.8)
            and (s["abstain_total"] == 0 or s["abstain_correct"] == s["abstain_total"])
            for s in report["paths"].values()
        )
        return 0 if ok else 1
    return 2


def cmd_graph_sync(args: argparse.Namespace) -> int:
    """Mirror graphify's graph.sqlite into PG (idempotent) or report drift."""
    from khipu.graph_sync import graph_drift, sync_from_sqlite

    sqlite = Path(args.sqlite) if args.sqlite else None
    if args.check:
        d = graph_drift(sqlite)
        print(json.dumps(d, indent=2))
        return 0 if d.get("ok") else 2
    out = sync_from_sqlite(sqlite, dry_run=args.dry_run)
    print(json.dumps(out, indent=2))
    return 0 if args.dry_run or out.get("drift", {}).get("ok") else 2


def cmd_sessions(args: argparse.Namespace) -> int:
    """Native session capture across every harness: the shared queue, per-harness
    heartbeat + liveness, and the drain (model extraction + capture) that turns
    queued sessions into episodes. `khipu aegis ...` is the older name for the
    same thing and still works."""
    from khipu import session_capture as sc

    if args.aegis_cmd == "status":
        harness = getattr(args, "harness", None)
        print(
            json.dumps(
                sc.status(harness)
                if harness and harness != "all"
                else sc.liveness_all(),
                indent=2,
            )
        )
        return 0
    if args.aegis_cmd == "liveness":
        lv = sc.liveness_all()
        print(json.dumps(lv, indent=2))
        return 0 if lv["ok"] else 2
    if args.aegis_cmd == "sweep":
        print(json.dumps(sc.sweep_idle(limit=args.limit, dry_run=args.dry_run), indent=2))
        return 0
    out = sc.drain(limit=args.limit, dry_run=args.dry_run, sweep=True)
    if not args.dry_run:
        # A promotion queued while the hub was out of reach is retried from
        # here, in its own process (khipu.promote.kick_queued_retry).
        from khipu.promote import kick_queued_retry

        kicked = kick_queued_retry()
        if kicked is not None:
            out["promotion_retry"] = kicked
    print(json.dumps(out, indent=2))
    return 0 if out["failed"] == 0 else 2


def cmd_git_sync(args: argparse.Namespace) -> int:
    """Is the memory tree's nightly git auto-sync landing? Exit 2 when red."""
    from khipu.git_sync_health import status

    out = status()
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["ok"] else 2


cmd_aegis = cmd_sessions


def cmd_integrations(args: argparse.Namespace) -> int:
    from khipu import integrations as integ

    targets = list(integ.HARNESSES) if args.harness == "all" else [args.harness]
    project = getattr(args, "project", None)
    # --home narrows a Claude Code install/uninstall/verify to one of the homes
    # `integrations status` lists (its `homes[].path`); without it every home
    # Khipu found is acted on.
    home = getattr(args, "home", None)
    if home is not None:
        try:
            if args.harness != "claude_code":
                raise integ.UnknownClaudeHome("--home applies to the claude_code harness only")
            integ.resolve_claude_home(home)
        except integ.UnknownClaudeHome as e:
            print(json.dumps({"harness": args.harness, "ok": False, "error": str(e)}, indent=2))
            return 2

    # ``project`` reaches EVERY harness, not just grok_bot (audit 2026-09-04):
    # integrations.verify uses it for the Cursor stale-rule check, so
    # `khipu integrations verify cursor --project X` silently ignored the flag.
    def _status(h):
        return integ.status(h, project=project)

    def _verify(h):
        if home is not None:
            return integ.verify(h, project=project, home=home)
        return integ.verify(h, project=project)

    if args.integ_cmd == "status":
        print(json.dumps([_status(h) for h in targets], indent=2))
        return 0
    if args.integ_cmd == "verify":
        results = [_verify(h) for h in targets]
        synth_check = integ.verify_synth_provider()
        if synth_check is not None:
            for result in results:
                result.setdefault("components", {})["synth_provider"] = synth_check
                result["ok"] = result.get("ok", not result["detected"]) and synth_check["ok"]
        print(json.dumps(results, indent=2))
        return 0 if all(r.get("ok", not r["detected"]) for r in results) else 2
    fn = integ.install if args.integ_cmd == "install" else integ.uninstall
    results = [fn(h, dry_run=args.dry_run, project=project, home=home) for h in targets]
    print(json.dumps(results, indent=2))
    if not all(r.get("ok", True) and not r.get("aborted", False) for r in results):
        return 1
    if args.integ_cmd == "install" and not args.dry_run and not args.no_verify:
        verified = [_verify(h) for h in targets]
        print(json.dumps({"verify": verified}, indent=2))
        return 0 if all(v.get("ok", not v["detected"]) for v in verified) else 2
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    """`khipu gateway token set` / `status` — the on-disk bearer convention
    Aegis's own config points at (`[memory.khipu] token_file`), and the fix
    named by `khipu integrations verify aegis` on a 401 or a missing token.
    Never prints the token itself."""
    from khipu import integrations as integ

    if args.gateway_cmd != "token":
        print(json.dumps({"ok": False, "error": f"unknown gateway command {args.gateway_cmd!r}"}))
        return 2
    if args.gateway_token_cmd == "status":
        print(json.dumps(integ.gateway_token_file_status(), indent=2))
        return 0
    if args.gateway_token_cmd == "set":
        if args.from_env is not None:
            token = os.environ.get(args.from_env, "")
            if not token.strip():
                print(json.dumps({"ok": False, "error": f"env {args.from_env} is empty or unset"}))
                return 2
        else:
            token = sys.stdin.read()
        try:
            integ.set_gateway_token(token)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        print(json.dumps({"ok": True, **integ.gateway_token_file_status()}, indent=2))
        return 0
    print(json.dumps({"ok": False, "error": f"unknown gateway token command {args.gateway_token_cmd!r}"}))
    return 2


def cmd_features(args: argparse.Namespace) -> int:
    """`khipu features` — the switch registry's states as JSON; `--set NAME
    on|off` persists one to config.json first."""
    from khipu import features

    if args.set:
        name, value = args.set
        parsed = features.parse_bool(value)
        if parsed is None:
            print(json.dumps({"ok": False, "error": f"value must be on/off (got {value!r})"}))
            return 2
        try:
            features.set_enabled(name, parsed)
        except KeyError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
    print(json.dumps(features.states(), indent=2))
    return 0


def cmd_briefs(args: argparse.Namespace) -> int:
    """`khipu briefs plan | build [--topic SLUG] [--limit N] | show SLUG`."""
    from khipu import briefs

    return briefs.cli_main(args)


def cmd_reflect(args: argparse.Namespace) -> int:
    """`khipu reflect "question" [--project P]`."""
    from khipu import reflect

    return reflect.cli_main(args)


def cmd_paths(args: argparse.Namespace) -> int:
    from khipu.paths import paths_status, set_data_dir

    if args.set:
        path = set_data_dir(Path(args.set))
        print(json.dumps({"ok": True, "data_dir": str(path)}, indent=2))
        return 0
    print(json.dumps(paths_status(), indent=2))
    return 0


def cmd_backup_local(args: argparse.Namespace) -> int:
    from khipu.paths import backup_local

    out = backup_local(dest=Path(args.out))
    print(json.dumps(out, indent=2))
    return 0


def cmd_nightly(_args: argparse.Namespace) -> int:
    from khipu.jobs import run_nightly

    return run_nightly()


def cmd_monthly(args: argparse.Namespace) -> int:
    from khipu.jobs import run_monthly

    return run_monthly(dry_run=bool(args.dry_run))


def cmd_graph_build(_args: argparse.Namespace) -> int:
    from khipu.jobs import run_graph_build

    return run_graph_build()


def cmd_models(args: argparse.Namespace) -> int:
    from khipu import models as models_mod

    action = getattr(args, "models_cmd", None) or "show"
    try:
        if action == "show":
            print(models_mod.dump_show_json())
            return 0
        if action == "set":
            raw_json = getattr(args, "models_json", None)
            role = getattr(args, "role", None)
            if raw_json:
                if role:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "error": "pass either a JSON blob or --role flags, not both",
                            }
                        )
                    )
                    return 2
                try:
                    payload = json.loads(raw_json)
                except ValueError as e:
                    print(json.dumps({"ok": False, "error": f"invalid JSON: {e}"}))
                    return 2
                if not isinstance(payload, dict):
                    print(
                        json.dumps(
                            {"ok": False, "error": "JSON payload must be an object"}
                        )
                    )
                    return 2
                out = models_mod.set_models_replace(payload)
                print(json.dumps({"ok": True, "models": out}, indent=2))
                return 0
            if not role:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "models set requires a JSON blob or --role …",
                        }
                    )
                )
                return 2
            provider = getattr(args, "provider", None)
            if not provider:
                print(
                    json.dumps(
                        {"ok": False, "error": "--provider is required with --role"}
                    )
                )
                return 2
            endpoint = getattr(args, "endpoint", None)
            model_id = getattr(args, "model_id", None)
            out = models_mod.set_models_merge_role(
                role,
                provider=provider,
                endpoint=endpoint,
                model_id=model_id,
            )
            print(json.dumps({"ok": True, "models": out}, indent=2))
            return 0
        if action == "welcome":
            raw_json = getattr(args, "models_json", None)
            if not raw_json:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "models welcome requires a JSON blob",
                        }
                    )
                )
                return 2
            try:
                payload = json.loads(raw_json)
            except ValueError as e:
                print(json.dumps({"ok": False, "error": f"invalid JSON: {e}"}))
                return 2
            if not isinstance(payload, dict):
                print(
                    json.dumps(
                        {"ok": False, "error": "JSON payload must be an object"}
                    )
                )
                return 2
            out = models_mod.apply_welcome_models(
                synth_choice=str(payload.get("synth_choice") or "skip"),
                embed_choice=str(payload.get("embed_choice") or "skip"),
                synth_endpoint=str(payload.get("synth_endpoint") or ""),
                synth_model_id=str(payload.get("synth_model_id") or ""),
                embed_endpoint=str(payload.get("embed_endpoint") or ""),
                embed_model_id=str(payload.get("embed_model_id") or ""),
            )
            embed_part = out.get("embed") or {}
            if embed_part.get("ok") is False:
                err = embed_part.get("error") or "embed activate failed"
                print(json.dumps({**out, "ok": False, "error": err}))
                return 2
            print(json.dumps(out, indent=2))
            return 0
        print(json.dumps({"ok": False, "error": f"unknown models action: {action}"}))
        return 2
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


def cmd_sources(args: argparse.Namespace) -> int:
    from khipu import sources

    action = getattr(args, "sources_cmd", None)
    try:
        if action == "list":
            out = {
                "sources": sources.load_sources().get("sources", []),
                "resolved": sources.resolve_for_graphify(),
                "graph_producer": __import__(
                    "khipu.graph_sync", fromlist=["is_graph_producer"]
                ).is_graph_producer(),
            }
            print(json.dumps(out, indent=2, default=str))
            return 0
        if action == "enable":
            sources.set_enabled(args.source_id, True)
            print(json.dumps({"ok": True, "id": args.source_id, "enabled": True}))
            return 0
        if action == "disable":
            sources.set_enabled(args.source_id, False)
            print(json.dumps({"ok": True, "id": args.source_id, "enabled": False}))
            return 0
        if action == "set-embed-media":
            raw = str(getattr(args, "embed_media_value", "")).strip().lower()
            if raw not in ("on", "off"):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "set-embed-media expects on|off",
                        }
                    )
                )
                return 2
            on = raw == "on"
            sources.set_embed_media(args.source_id, on)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "id": args.source_id,
                        "embed_media": on,
                    }
                )
            )
            return 0
        if action == "add":
            sources.add_code_root(Path(args.root))
            print(json.dumps({"ok": True, "root": args.root}))
            return 0
        if action == "remove":
            sources.remove_user_source(args.source_id)
            print(json.dumps({"ok": True, "id": args.source_id}))
            return 0
        if action == "export":
            out = sources.export_resolved()
            print(
                json.dumps(
                    {"ok": True, "path": str(sources.resolved_path()), "resolved": out}
                )
            )
            return 0
        print(json.dumps({"ok": False, "error": f"unknown sources action: {action}"}))
        return 2
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


@guard_output
def cmd_graph_backup(args: argparse.Namespace) -> int:
    from khipu import graph_backup
    from khipu.join_errors import redact_connection_text

    action = getattr(args, "graph_backup_cmd", None)
    actions = {
        "record-local": graph_backup.record_local,
        "offsite": graph_backup.run_offsite,
        "drill": graph_backup.scratch_drill,
        "status": graph_backup.status_payload,
    }
    if action not in actions:
        print(
            json.dumps({"ok": False, "error": f"unknown graph-backup action: {action}"})
        )
        return 2
    try:
        out = actions[action]()
    except Exception:  # noqa: BLE001 — backup failures can quote credentials
        print(json.dumps({"ok": False, "error": f"graph backup {action} failed"}))
        return 2
    print(redact_connection_text(json.dumps(out, indent=2, default=str)))
    return 0 if action == "status" or out.get("ok", False) else 2


def cmd_import_local(args: argparse.Namespace) -> int:
    from khipu.paths import import_local

    out = import_local(source=Path(args.source), merge=bool(args.replace))
    print(json.dumps(out, indent=2))
    return 0


def _print_component_result(payload: dict) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload.get("ok") else 1


def cmd_components(args: argparse.Namespace) -> int:
    action = getattr(args, "components_cmd", None)
    if action == "select-compat-row":
        from khipu.components_matrix import select_compat_row

        return _print_component_result(
            select_compat_row(
                args.mode,
                pgvector_extversion=getattr(args, "pgvector_extversion", None),
                server_version=getattr(args, "server_version", None),
                pgvector=getattr(args, "pgvector", None),
                refresh=not getattr(args, "no_refresh", False),
            )
        )
    if action == "install-local-postgres":
        from khipu.components_postgres import install_local_postgres

        return _print_component_result(install_local_postgres())
    if action == "bootstrap-local-backup":
        from khipu.components_backup import bootstrap_local_backup

        return _print_component_result(bootstrap_local_backup())
    if action == "install-graphify":
        from khipu.components_graphify import install_graphify

        return _print_component_result(install_graphify(first_run=True))
    if action == "status-json":
        from khipu.components_postgres import components_status

        return _print_component_result(components_status())
    if action == "upgrade-postgres":
        from khipu.components_postgres import upgrade_postgres

        return _print_component_result(upgrade_postgres())
    if action == "upgrade-graphify":
        from khipu.components_graphify import upgrade_graphify

        return _print_component_result(upgrade_graphify())
    if action == "check-remote":
        from khipu.components_postgres import check_remote_postgres

        return _print_component_result(
            check_remote_postgres(full=bool(getattr(args, "full", False)))
        )
    if action == "install":
        target = getattr(args, "install_target", None)
        if target == "postgres":
            from khipu.components_postgres import install_local_postgres

            return _print_component_result(install_local_postgres())
        if target == "graphify":
            from khipu.components_graphify import install_graphify

            return _print_component_result(install_graphify(first_run=False))
        print(json.dumps({"ok": False, "error": f"unknown install target: {target}"}))
        return 1
    if action == "status":
        from khipu.components_postgres import components_status

        return _print_component_result(components_status())
    if action == "upgrade":
        target = getattr(args, "upgrade_target", None)
        if target == "postgres":
            from khipu.components_postgres import upgrade_postgres

            return _print_component_result(upgrade_postgres())
        if target == "graphify":
            from khipu.components_graphify import upgrade_graphify

            return _print_component_result(upgrade_graphify())
        print(json.dumps({"ok": False, "error": f"unknown upgrade target: {target}"}))
        return 1
    print(json.dumps({"ok": False, "error": "missing components subcommand"}))
    return 1


class _Parser(argparse.ArgumentParser):
    """Usage errors quote what was typed; a key-shaped value is masked first.
    Subparsers inherit the class."""

    def error(self, message: str):
        from khipu.redact import redact_secrets

        super().error(redact_secrets(message, force=True)[0])


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="khipu", description="Khipu memory hub CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="PG counts + optional drift")
    s.add_argument("--memory-root", default=_memory_root_default())
    s.add_argument(
        "--sample",
        type=int,
        default=0,
        help="file↔pg hash sample size (0 = PG-only conflicts, skip NAS walk)",
    )
    s.add_argument(
        "--drift",
        action="store_true",
        help="Include sample_drift (slower; doctor already covers this)",
    )
    s.set_defaults(func=cmd_status)

    d = sub.add_parser("doctor", help="Drift samples + health JSON")
    d.add_argument("--memory-root", default=_memory_root_default())
    # Default None = check every topic. It used to be 25, and because the walk
    # is alphabetical that meant doctor compared the same first 4% of 622 topics
    # forever (audit 2026-08-17). The full pass measures 0.09 s.
    d.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Cap the topic drift pass at N topics (default: all)",
    )
    d.add_argument(
        "--probe",
        action="store_true",
        help="Run a fresh end-to-end recall probe (writes a nonce episode, "
        "soft-deletes it after) before reporting; plain diagnostics may maintain local cache/credential state",
    )
    d.add_argument(
        "--harness",
        default=None,
        help="Harness label for --probe (default: $KHIPU_HARNESS or 'doctor')",
    )
    d.add_argument(
        "--quick",
        action="store_true",
        help="Imports, config, secrets presence, jobs and the summariser provider only: "
        "no hub, network or probe checks (the promotion gate's first step)",
    )
    d.set_defaults(func=cmd_doctor)

    rv = sub.add_parser(
        "revisions",
        help="Topic revisions + conflict visibility (LWW losers queryable)",
    )
    rv.add_argument("--memory-root", default=_memory_root_default())
    rv.add_argument("--limit", type=int, default=40)
    # Default None = compare every topic. It was 40, and because the walk is
    # alphabetical this report cleared all 622 topics after checking the first
    # 40 (audit 2026-08-17) — the same defect doctor's --sample carried.
    rv.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Cap the file↔pg pass at N topics (0 = PG-only, default: all)",
    )
    rv.add_argument("--slug", default=None, help="Filter recent revisions to one slug")
    rv.add_argument(
        "--show",
        type=int,
        default=None,
        metavar="ID",
        help="Print full body for one topic_revisions.id",
    )
    rv.set_defaults(func=cmd_revisions)

    act = sub.add_parser(
        "activity",
        help="Recent capture_v2 → PG episodes (read/inspect)",
    )
    act.add_argument("--limit", type=int, default=40)
    act.add_argument(
        "--show",
        type=int,
        default=None,
        metavar="ID",
        help="Full episode row by id",
    )
    act.set_defaults(func=cmd_activity)

    sec = sub.add_parser(
        "secrets",
        help="Keychain/env/file presence for DSN + Gemini (no secret values)",
    )
    sec.add_argument(
        "--set",
        metavar="ACCOUNT",
        choices=SETTABLE_SECRETS,
        help="store a secret read from stdin (never pass the value as an argument)",
    )
    sec_sub = sec.add_subparsers(dest="secrets_cmd", required=False)
    sv = sec_sub.add_parser(
        "verify",
        help="Prove a configured model key works with one real, cheap call",
    )
    sv_group = sv.add_mutually_exclusive_group()
    sv_group.add_argument(
        "--gemini", action="store_true",
        help="Check Gemini embeddings + generation only",
    )
    sv_group.add_argument(
        "--openai", action="store_true",
        help="Check the configured local OpenAI-compatible synth endpoint only",
    )
    sv_group.add_argument(
        "--all", action="store_true",
        help="Check every configured provider (default)",
    )
    sv.add_argument(
        "--json", action="store_true",
        help="JSON output (default; accepted for explicitness)",
    )
    sv.set_defaults(func=cmd_secrets_verify, secrets_cmd="verify")
    sec.set_defaults(func=cmd_secrets)

    se = sub.add_parser(
        "search",
        help="Hybrid search (cosine + token overlap + literal), or --mode literal/semantic",
    )
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=20)
    se.add_argument(
        "--mode",
        choices=("hybrid", "literal", "semantic"),
        help="Default hybrid. literal = old ILIKE-only. semantic = cosine + token overlap only.",
    )
    se.add_argument(
        "--semantic",
        action="store_true",
        help="Deprecated alias for --mode semantic",
    )
    se.add_argument(
        "--kind",
        choices=("episode", "topic", "node", "media", "library"),
        help="Restrict to one kind (media is semantic-only; node is literal/hybrid-only; "
        "library searches the enabled library sources)",
    )
    se.add_argument(
        "--source", metavar="NAME",
        help="Library name (see `khipu library list`); implies --kind library",
    )
    se.add_argument("--project", help="Match COALESCE(episodes.project, episodes.scope)")
    se.add_argument("--since", help="ISO date/datetime or relative, e.g. 7d / 24h")
    se.add_argument("--until", help="ISO date/datetime or relative, e.g. 7d / 24h")
    se.add_argument("--session-id", dest="session_id", help="Episode session_id prefix match")
    se.add_argument("--harness", help="Prefix of session_id before the colon")
    se.add_argument("--via", help="Exact capture origin, e.g. t3")
    se.set_defaults(func=cmd_search)

    em = sub.add_parser(
        "embed", help="Vectors: backfill / status / activate (profiles)"
    )
    em_sub = em.add_subparsers(dest="embed_cmd", required=True)
    bf = em_sub.add_parser(
        "backfill", help="Embed missing/changed episode+topic chunks"
    )
    bf.add_argument(
        "--kind",
        choices=("episode", "topic", "commitment"),
        help="commitment is opt-in: it heals commitment vectors the Stop-hook "
        "catch-up fell behind on, and is never part of the default sweep",
    )
    bf.add_argument("--limit", type=int, help="Cap chunks this run (pilot / smoke)")
    bf.add_argument("--dry-run", action="store_true", help="Count only; no API calls")
    bf.add_argument(
        "--profile",
        help="Target profile id (default: active). e.g. gemini-embedding-2@768",
    )
    # --bypass-harness (sonnet lane): dispatched agent, brief says do not delegate.
    bf.add_argument(
        "--stale", action="store_true",
        help="Accepted for symmetry with `library backfill`: memory backfill always "
        "re-embeds chunks whose text changed since they were embedded",
    )
    bf.add_argument(
        "--job", action="store_true",
        help="Run as a job: progress file in <data_dir>/jobs, SIGTERM cancels after the "
        "current batch, one JSON line on stdout. Needs --profile",
    )
    bf.add_argument("--job-id", dest="job_id", default=None,
                    help="Job id for the progress file (implies --job; default generated)")
    es = em_sub.add_parser(
        "estimate", help="Chunks, tokens, list price and time a re-embed would take (no model call)"
    )
    es.add_argument("--profile", required=True, help="Profile id to embed under")
    es.add_argument("--space", default="memory", help="memory (default) or library:NAME")
    es.add_argument("--stale", action="store_true",
                    help="Library only: also count chunks whose text changed (memory always does)")
    jb = em_sub.add_parser("jobs", help="List embedding job files (progress receipts)")
    jb.add_argument("--clear", action="store_true", help="Remove finished jobs' files")
    tk = em_sub.add_parser(
        "test-key", help="Embed one short string with the provider's stored key: {ok, ms, dim}"
    )
    tk.add_argument("--provider", required=True, help="gemini | voyage | openai-compatible")
    tk.add_argument("--endpoint", help="openai-compatible only: base URL")
    tk.add_argument("--model", help="Model (default gemini-embedding-2 / voyage-3; required for "
                    "openai-compatible)")
    ix = em_sub.add_parser(
        "index", help="Build a profile's HNSW index (no-op if present); honours KHIPU_INDEX_MEMORY"
    )
    ix.add_argument("profile", help="Profile id, e.g. voyage-3@1024")
    ix.add_argument("--table", choices=("memory", "library"), default=None,
                    help="Only this space (default: both)")
    st = em_sub.add_parser(
        "status", help="Coverage per kind for active or named profile"
    )
    st.add_argument(
        "--profile",
        help="Profile id to report (default: active)",
    )
    act = em_sub.add_parser(
        "activate",
        help="Flip the one-active search pointer (refuses if coverage incomplete)",
    )
    act.add_argument(
        "profile", help="Profile id to activate, e.g. gemini-embedding-2@768"
    )
    act.add_argument(
        "--force",
        action="store_true",
        help="Activate even when the profile still has missing vectors",
    )
    pr = em_sub.add_parser(
        "profiles", help="List embedding profiles or add one (provider adapter + record)"
    )
    pr_sub = pr.add_subparsers(dest="profiles_cmd", required=True)
    # --bypass-harness (sonnet lane): dispatched agent, brief says do not delegate.
    pr_sub.add_parser(
        "list",
        help="Every profile: provider, model, dim, endpoint, rows per space, where it is used, "
        "coverage per space, key presence, median query ms, list price",
    )
    pd = pr_sub.add_parser(
        "delete",
        help="Delete a profile nothing uses and all its vectors (refuses when in use)",
    )
    pd.add_argument("id", help="Profile id, e.g. voyage-3@1024")
    pd.add_argument("--yes", action="store_true", help="Required: confirms the delete")
    pa = pr_sub.add_parser(
        "add",
        help="Register a profile (inactive); the id must be model@dim, never overwritten",
    )
    pa.add_argument("id", help="Profile id, exactly model@dim, e.g. voyage-3@1024")
    pa.add_argument(
        "--provider", required=True,
        help="gemini | voyage | openai-compatible (checked by the command)",
    )
    pa.add_argument("--model", required=True, help="Provider model name")
    pa.add_argument("--dim", required=True, type=int, help="Vector dimension")
    pa.add_argument(
        "--endpoint",
        help="openai-compatible only: base URL, https (http only for localhost). "
        "The key, if any, is the Keychain item openai_compat_api_key",
    )
    pa.add_argument(
        "--normalize", default="l2", choices=("l2", "none"),
        help="L2-normalise vectors on the way in (default l2)",
    )
    em.set_defaults(func=cmd_embed)

    lib = sub.add_parser(
        "library",
        help="Library sources: a folder of .txt/.md files indexed as a search space",
    )
    lib_sub = lib.add_subparsers(dest="library_cmd", required=True)
    la = lib_sub.add_parser("add", help="Register a library (root must exist; profile must exist)")
    la.add_argument("name", help="[a-z0-9_-]{1,40}")
    la.add_argument("--root", required=True, help="Folder holding the .txt/.md files")
    la.add_argument("--profile", required=True, help="Embedding profile id (see `embed profiles list`)")
    lib_sub.add_parser("list", help="Every library with document/chunk/embedded/missing/stale counts")
    ls_ = lib_sub.add_parser("status", help="One library: counts, last scan/backfill, sample gaps")
    ls_.add_argument("name")
    lr = lib_sub.add_parser("remove", help="Delete a library's documents, chunks and vectors (files stay)")
    lr.add_argument("name")
    lr.add_argument("--yes", action="store_true", help="Required: confirms the delete")
    for _verb in ("enable", "disable"):
        lib_sub.add_parser(_verb, help=f"{_verb.capitalize()} a library (nightly sweep, search)").add_argument("name")
    lsp = lib_sub.add_parser(
        "set-profile",
        help="Move a library's search pointer to another profile (refuses unless that profile "
        "has a vector for every chunk)",
    )
    lsp.add_argument("name")
    lsp.add_argument("profile", help="Embedding profile id (see `embed profiles list`)")
    lsc = lib_sub.add_parser("scan", help="Walk the root and record documents + chunks (no model call)")
    lsc.add_argument("name")
    lsc.add_argument("--allow-rebuild", action="store_true",
                     help="Allow a mass rebuild that deletes existing vectors")
    lb = lib_sub.add_parser("backfill", help="Embed chunks that have no vector under the library's profile")
    lb.add_argument("name")
    lb.add_argument("--limit", type=int, default=None, help="Embed at most N chunks this run")
    # --bypass-harness (sonnet lane): dispatched agent, brief says do not delegate.
    lb.add_argument(
        "--stale", action="store_true",
        help="Also re-embed chunks whose text changed since they were embedded",
    )
    lb.add_argument("--profile", default=None,
                    help="Embed under this profile instead of the library's own (a re-embed "
                    "in preparation; the library's pointer does not move)")
    lb.add_argument("--job", action="store_true",
                    help="Run as a job: progress file in <data_dir>/jobs, SIGTERM cancels "
                    "after the current batch, one JSON line on stdout")
    lb.add_argument("--job-id", dest="job_id", default=None,
                    help="Job id for the progress file (implies --job; default generated)")
    li = lib_sub.add_parser(
        "import",
        help="Import vectors computed elsewhere (graphify SQLite `embeddings` table, or .jsonl)",
    )
    li.add_argument("name")
    li.add_argument("path", help="SQLite file or .jsonl with node_id, chunk_idx, source_file, "
                    "chunk_text, embedding, model, dims")
    li.add_argument("--strip-prefix", dest="strip_prefix", default="",
                    help="Remove this leading text from each row's source_file")
    li.add_argument("--profile", default=None,
                    help="Profile the vectors belong to (default: model@dims from each row)")
    li.add_argument("--batch", type=int, default=500,
                    help="Rows per insert-and-commit batch (default 500)")
    lib.set_defaults(func=cmd_library)

    g = sub.add_parser("graph", help="Neighbors (join / GRAPH_TABLE / CTE)")
    g.add_argument("id", help="Node id")
    g.add_argument("--hops", type=int, default=1)
    g.add_argument("--limit", type=int, default=50)
    g.set_defaults(func=cmd_graph)

    r = sub.add_parser("regen-memory", help="Write MEMORY.md from PG topics")
    r.add_argument(
        "--out",
        default=None,  # resolved against memory_root at run time
    )
    r.add_argument("--limit", type=int, default=200)
    r.add_argument(
        "--index",
        action="store_true",
        help="Rebuild live MEMORY.md index via build_index.py (file wiki SSOT); "
        "default without --index writes MEMORY.from-khipu.md sidecar from PG",
    )
    r.add_argument("--memory-root", default=_memory_root_default())
    r.set_defaults(func=cmd_regen_memory)

    from khipu import ledger as _ledger

    _ledger.add_parser(sub)
    from khipu import work as _work

    _work.add_parser(sub)

    nt = sub.add_parser("nightly", help="Run consolidate_nightly.py (memory wiki)")
    nt.set_defaults(func=cmd_nightly)

    mo = sub.add_parser(
        "monthly",
        help="Run conversation-memory-monthly.py (wiki classify)",
    )
    mo.add_argument(
        "--dry-run",
        action="store_true",
        help="Only for Cursor-era consolidate_monthly.py; refused on the live Claude driver",
    )
    mo.set_defaults(func=cmd_monthly)

    gb = sub.add_parser(
        "graph-build", help="Run graphify_nightly.py once (graph.sqlite)"
    )
    gb.set_defaults(func=cmd_graph_build)

    emb = sub.add_parser(
        "embed-media-backfill",
        help="Embed PNG/JPEG under sources with embed_media (active Gemini Embedding 2)",
    )
    emb.add_argument("--dry-run", action="store_true", help="Count only; no API calls")
    emb.add_argument(
        "--yes",
        action="store_true",
        help=f"Required when more than {1000} images would be scanned",
    )
    emb.add_argument("--limit", type=int, default=None, help="Cap files this run")
    emb.add_argument(
        "--source-id",
        dest="source_id",
        default=None,
        help="Only walk this source id (must have embed_media on + root)",
    )
    emb.add_argument(
        "--profile",
        default=None,
        help="Profile id (default gemini-embedding-2@768)",
    )
    emb.set_defaults(func=cmd_embed_media_backfill)

    # --bypass-harness (sonnet lane): dispatched agent, brief says do not delegate.
    # Detached-spawn wrappers (the desktop app's job pattern: one hyphenated
    # subcommand, output goes to a log, progress goes to <data_dir>/jobs/<id>.json).
    ebj = sub.add_parser(
        "embed-backfill",
        help="Job: embed memory under --profile with a progress file (= embed backfill --job)",
    )
    ebj.add_argument("--profile", required=True, help="Profile id to embed under")
    ebj.add_argument("--job-id", dest="job_id", default=None, help="Job id (default generated)")
    ebj.add_argument("--limit", type=int, default=None, help="Cap chunks this run")
    ebj.set_defaults(func=cmd_embed_backfill_job)
    lbj = sub.add_parser(
        "library-backfill",
        help="Job: embed library NAME with a progress file (= library backfill NAME --job)",
    )
    lbj.add_argument("name")
    lbj.add_argument("--profile", default=None, help="Embed under this profile, not the library's own")
    lbj.add_argument("--stale", action="store_true", help="Also re-embed changed chunks")
    lbj.add_argument("--job-id", dest="job_id", default=None, help="Job id (default generated)")
    lbj.add_argument("--limit", type=int, default=None, help="Cap chunks this run")
    lbj.set_defaults(func=cmd_library_backfill_job)

    md = sub.add_parser(
        "models",
        help="Per-role model Settings (synth / embed / vision): show / set",
    )
    md_sub = md.add_subparsers(dest="models_cmd", required=False)
    md_sub.add_parser("show", help="JSON: three roles + models_error").set_defaults(
        func=cmd_models, models_cmd="show"
    )
    md_set = md_sub.add_parser(
        "set",
        help="Merge one role (--role …) or replace all three (JSON blob)",
    )
    md_set.add_argument(
        "models_json",
        nargs="?",
        default=None,
        help="Full models object JSON (all three roles); replaces the whole models key",
    )
    md_set.add_argument(
        "--role",
        choices=("synth", "embed", "vision"),
        default=None,
        help="Merge one role (other roles unchanged)",
    )
    md_set.add_argument(
        "--provider",
        choices=("gemini", "anthropic", "openai", "openrouter", "claude-cli", "codex-cli", "cloud", "local", "off"),
        default=None,
        help="Provider for --role merge",
    )
    md_set.add_argument("--endpoint", default=None, help="Local endpoint origin")
    md_set.add_argument(
        "--model-id", "--model",
        dest="model_id",
        default=None,
        help="Model id for the role",
    )
    md_set.set_defaults(func=cmd_models, models_cmd="set")
    md_welcome = md_sub.add_parser(
        "welcome",
        help="First-run synth/embed choices; activates cloud embed in-process",
    )
    md_welcome.add_argument(
        "models_json",
        help="JSON: synth_choice (cloud, local, skip or a synth provider), embed_choice, optional endpoint/model_id fields",
    )
    md_welcome.set_defaults(func=cmd_models, models_cmd="welcome")
    md.set_defaults(func=cmd_models, models_cmd="show")

    src = sub.add_parser(
        "sources", help="Graph membership: list / enable / disable / add / export"
    )
    src_sub = src.add_subparsers(dest="sources_cmd", required=True)
    src_sub.add_parser(
        "list", help="JSON: sources + resolve_for_graphify"
    ).set_defaults(func=cmd_sources)
    en = src_sub.add_parser("enable", help="Enable a seeded or user source")
    en.add_argument("source_id")
    en.set_defaults(func=cmd_sources)
    dis = src_sub.add_parser("disable", help="Disable a source (does not purge PG)")
    dis.add_argument("source_id")
    dis.set_defaults(func=cmd_sources)
    sem = src_sub.add_parser(
        "set-embed-media",
        help="Opt a source into native image embed (on|off; default off)",
    )
    sem.add_argument("source_id")
    sem.add_argument(
        "embed_media_value",
        choices=("on", "off"),
        help="on = walk PNG/JPEG under this source's root on embed-media-backfill",
    )
    sem.set_defaults(func=cmd_sources)
    add = src_sub.add_parser("add", help="Add a code_ast root (absolute path)")
    add.add_argument("--root", required=True, dest="root", help="Absolute code root")
    add.set_defaults(func=cmd_sources)
    rem = src_sub.add_parser("remove", help="Remove a user-added source row")
    rem.add_argument("source_id")
    rem.set_defaults(func=cmd_sources)
    src_sub.add_parser("export", help="Write graph_sources.resolved.json").set_defaults(
        func=cmd_sources
    )
    src.set_defaults(func=cmd_sources)

    gbu = sub.add_parser(
        "graph-backup",
        help="Graph.sqlite snapshot record / offsite / drill / status (producer Mac)",
    )
    gbu_sub = gbu.add_subparsers(dest="graph_backup_cmd", required=True)
    gbu_sub.add_parser(
        "record-local",
        help="Integrity-check latest snapshot and record graph_snapshot ops_event",
    ).set_defaults(func=cmd_graph_backup)
    gbu_sub.add_parser(
        "offsite",
        help="rclone copyto latest snapshot to graph_backup_remote (default r2:khipu-backups/khipu-graph)",
    ).set_defaults(func=cmd_graph_backup)
    gbu_sub.add_parser(
        "status", help="Local + offsite health and last ops_events"
    ).set_defaults(func=cmd_graph_backup)
    gbu_sub.add_parser(
        "drill",
        help="Scratch restore drill on latest snapshot (never touches live graph.sqlite)",
    ).set_defaults(func=cmd_graph_backup)
    gbu.set_defaults(func=cmd_graph_backup)

    rc = sub.add_parser("reconcile", help="Full file→PG episodes/topics sync")
    rc.add_argument("--memory-root", default=_memory_root_default())
    rc.set_defaults(func=cmd_reconcile)

    owed = sub.add_parser("owed", help="List / close / reopen commitments (memory reliability W3)")
    owed.add_argument("--project", default=None)
    owed.add_argument("--status", default="open", choices=("open", "closed", "stale"))
    owed.add_argument("--limit", type=int, default=50)
    owed.add_argument("--close", metavar="ID", default=None, help="Close commitment ID")
    owed.add_argument("--reopen", metavar="ID", default=None, help="Reopen commitment ID")
    owed.add_argument(
        "--snooze", metavar="ID", default=None,
        help="Push commitment ID's due date out (needs --until)",
    )
    owed.add_argument(
        "--until", default=None,
        help="With --snooze: a date (2026-09-30) or a window (7d, 2w, 3m, 2 weeks)",
    )
    owed.add_argument(
        "--envelope", action="store_true",
        help="Print {results, stale, snapshot_refreshed_at, waiting_to_sync} instead of the bare list",
    )
    owed.set_defaults(func=cmd_owed)

    us = sub.add_parser(
        "usage",
        help="Plan headroom of every subscription CLI (Claude, Codex, Grok, Muse), read from the CLIs",
    )
    us.add_argument("--json", action="store_true", help="Print the JSON report instead of a table")
    us.add_argument("--fresh", action="store_true", help="Probe now; ignore the cache")
    us.add_argument(
        "--max-age", type=float, default=None, metavar="S",
        help="Reuse a cached probe younger than S seconds (default 120); older figures are marked stale",
    )
    us.add_argument("--account", default=None, metavar="LABEL",
                    help="Only this configured account (its label, as Settings shows it)")
    us.add_argument("--account-key", default=None, metavar="KEY",
                    help="Only the account with this key (harness|login folder); wins over a label two accounts share")
    us.add_argument("--sign-in", action="store_true",
                    help="With --account: start that CLI's own login in its login folder (opens the browser), "
                         "wait for it, then read the account again")
    us.add_argument("--discover", action="store_true",
                    help="Look for login folders Khipu could track (for the app; prints paths)")
    us.add_argument("--now", action="store_true",
                    help="With --discover: check every due folder now, ignoring the once-a-day wait")
    us.add_argument("--check", default=None, metavar="FOLDER",
                    help="Check one found login folder now (the owner's Check; the CLI may write into it)")
    us.add_argument("--try", dest="try_account", action="store_true",
                    help="One read of an account that is not saved: --harness H and optionally --folder F")
    us.add_argument("--harness", default=None, choices=("claude", "codex", "grok", "muse"),
                    help="With --try: the CLI")
    us.add_argument("--folder", default=None, metavar="F",
                    help="With --try: the login folder (~/... or a full path); none = the default login")
    us.add_argument("--forget-login-paths", action="store_true",
                    help="Clear the login folder recorded for every saved conversation")
    us.add_argument("--sign-in-offers", action="store_true",
                    help="Which CLIs the app may offer Sign in to another account for (JSON)")
    us.add_argument("--sign-in-folder", action="store_true",
                    help="With --harness H --name N: make the new login folder ~/.<cli>-<name> (JSON)")
    us.add_argument("--name", default=None, metavar="N", help="With --sign-in-folder: the account's name")
    us.add_argument("--sign-in-new", action="store_true",
                    help="With --harness H and --folder F (a folder --sign-in-folder made): start that CLI's "
                         "login and return at once with an attempt id (JSON)")
    us.add_argument("--again", action="store_true",
                    help="With --sign-in-new: end the running sign-in first (Start again)")
    us.add_argument("--sign-in-status", default=None, metavar="ID", help="The sign-in attempt's state (JSON)")
    us.add_argument("--sign-in-cancel", default=None, nargs="?", const="", metavar="ID",
                    help="Stop and cancel that sign-in (JSON); with no ID, only --remove-folder runs")
    us.add_argument("--remove-folder", action="store_true",
                    help="With --sign-in-cancel (and --harness H --folder F once the sign-in ended): also "
                         "remove the folder Khipu made if it is still empty")
    us.add_argument("--sign-in-reap", action="store_true",
                    help="End a leftover sign-in Khipu started (on the app's quit)")
    us.set_defaults(func=cmd_usage)

    rt = sub.add_parser(
        "route",
        help="The routing record: which accounts are open, by whom and since when (Khipu reports; it never routes)",
    )
    rt.add_argument("--json", action="store_true", help="Print JSON")
    rt.add_argument("--since", default=None, metavar="V",
                    help="The current state of everything that changed after version V (or EPOCH:V)")
    rt.add_argument("--summary", action="store_true", help="The summary a conversation gets at its start")
    rt.add_argument("--brief", action="store_true", help="The text a job runner puts into a brief")
    rt.add_argument("--account", dest="view_account", default=None, metavar="A", help="One account only")
    rt.set_defaults(func=cmd_route, route_cmd=None)
    # Writes always print JSON; --json is accepted after any of them too, so a
    # script that adds it to every khipu call doesn't lose its write. SUPPRESS
    # keeps `route --json <write>` from being reset by the subcommand's default.
    rt_json = argparse.ArgumentParser(add_help=False)
    rt_json.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                         help="Print JSON (writes always do)")
    rt_sub = rt.add_subparsers(dest="route_cmd")
    rcheck = rt_sub.add_parser("check", help="Check one account now without spending a turn", parents=[rt_json])
    rcheck.add_argument("account", help="The account (its label, or harness|login folder)")
    rep = rt_sub.add_parser("report", help="Record what a conversation learned about an account", parents=[rt_json])
    rep.add_argument("--account", default=None, metavar="A", help="The account (its label, or harness|login folder)")
    rep.add_argument("--runner", default=None,
                     help="With --lane: a job runner's lane; only a lane routing_lane_map names")
    rep.add_argument("--lane", default=None)
    rep.add_argument("--state", required=True, choices=("limited", "open", "down"))
    rep.add_argument("--until", default=None, metavar="T", help="When the limit ends (a guess unless --message says)")
    rep.add_argument("--window", default=None, choices=("five-hour", "weekly"))
    rep.add_argument("--model", default=None,
                     help="The model a model-scoped limit closes (a model's name; not with --state down)")
    rep.add_argument("--message", default=None, help="The limit message itself, word for word")
    rep.add_argument("--seen", default=None, metavar="T",
                     help="When the message was shown (a time, or how long ago: 10m); a reset clock time "
                          "with no day counts as the CLI's own only with it")
    rep.add_argument("--name", default=None, help="What the conversation calls itself (shown beside its author)")
    rep.add_argument("--evidence", default=None, help="One line of evidence")
    hold = rt_sub.add_parser("hold", help='Add a hold: hold "<words>" --reason R; or end one: hold end <id>',
                             parents=[rt_json])
    hold.add_argument("words", nargs="+")
    hold.add_argument("--reason", default=None, choices=("machine-busy", "disk-low", "keep", "outage", "other"))
    hold.add_argument("--until", default=None, metavar="T")
    hold_scope = hold.add_mutually_exclusive_group()
    hold_scope.add_argument("--account", default=None, metavar="A")
    hold_scope.add_argument("--machine", action="store_true")
    hold_scope.add_argument("--all", action="store_true")
    hold.add_argument("--owner", action="store_true", help="The owner's hold (asks for a typed confirmation)")
    hold.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    rpause = rt_sub.add_parser("pause", help="Owner: mark an account paused (asks for a typed confirmation)",
                               parents=[rt_json])
    rpause.add_argument("account")
    rpause.add_argument("--until", default=None, metavar="T")
    rpause.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    rlift = rt_sub.add_parser("lift", help="Owner: lift a pause (asks for a typed confirmation)", parents=[rt_json])
    rlift.add_argument("account")
    rlift.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    rclear = rt_sub.add_parser("clear", help="Owner: clear an account, or dismiss one report (--report ID)",
                               parents=[rt_json])
    rclear.add_argument("account")
    rclear.add_argument("--report", default=None, metavar="ID")
    rclear.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    rnote = rt_sub.add_parser("note", help="Owner: set or end the note to every conversation", parents=[rt_json])
    rnote.add_argument("action", choices=("set", "end"))
    rnote.add_argument("text", nargs="?", default=None)
    rnote.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    rt_sub.add_parser("sources", help="Each job runner folder with its last good read and any error",
                      parents=[rt_json])
    rwait = rt_sub.add_parser(
        "wait", parents=[rt_json],
        help="Wait until an account is open; exits 0 then, 1 at the timeout (it waits; it launches nothing)")
    rwait.add_argument("account", help="The account (its label, or harness|login folder)")
    rwait.add_argument("--confirmed", action="store_true",
                       help="Only once Khipu itself has read it open (a usage probe or a reset check), "
                            "not when its limit's time has merely passed")
    rwait.add_argument("--timeout", default=None, metavar="S",
                       help="Give up after S seconds (or 30m, 2h; 0 checks once); default: no end")
    rprio = rt_sub.add_parser(
        "priority", parents=[rt_json],
        help="The owner's priority: show it; set, confirm, undo or clear it (asks for a typed confirmation)")
    rprio.add_argument("action", nargs="?", default="show", choices=("show", "set", "confirm", "undo", "clear"))
    rprio.add_argument("accounts", nargs="*", help="set: the accounts in your order (or --entries)")
    rprio.add_argument("--entries", default=None, metavar="JSON",
                       help='set: [{"account": "...", "role": "your words", "until": "T"}, ...] in your order')
    rprio.add_argument("--until", default=None, metavar="T", help="set: when the whole priority ends")
    rprio.add_argument("--at-owner-request", default=None, metavar="REQUEST",
                       help="A conversation: the owner's request, in one line (set only; stays unconfirmed)")
    rprio.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    rprio.add_argument("--expect", default=None, metavar="V",
                       help="The priority's version as read; refused if it changed since")
    rrule = rt_sub.add_parser("rule", help="Owner: add, edit, retire, restore or confirm a standing rule", parents=[rt_json])
    rrule.add_argument("action", choices=("add", "edit", "retire", "restore", "confirm", "undo", "dismiss"))
    rrule.add_argument("params", nargs="*")
    rrule.add_argument("--source", default=None, help="Where the rule comes from (rule add)")
    rrule.add_argument("--at-owner-request", default=None, metavar="REQUEST",
                       help="A conversation: the owner's request, in one line (rule add or edit; stays unconfirmed)")
    rrule.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)

    dec = sub.add_parser(
        "decisions", help="Decisions registry: list / supersede / restore / retract / links (O2, Phase 2A)"
    )
    dec_sub = dec.add_subparsers(dest="decisions_cmd", required=True)
    dec_list = dec_sub.add_parser("list", help="List decisions")
    dec_list.add_argument("--project", default=None)
    dec_list.add_argument("--since", default=None, help="ISO date or a window (7d, 24h)")
    dec_list.add_argument("--limit", type=int, default=50)
    dec_list_status = dec_list.add_mutually_exclusive_group()
    dec_list_status.add_argument("--standing", action="store_true", help="Exclude superseded and retracted")
    dec_list_status.add_argument("--superseded", action="store_true", help="Superseded only")
    dec_list_status.add_argument("--retracted", action="store_true", help="Retracted only")
    dec_list_status.add_argument("--all", action="store_true", help="Every row (default)")
    dec_sup = dec_sub.add_parser("supersede", help="Mark OLD decision superseded by NEW")
    dec_sup.add_argument("old_id", type=int)
    dec_sup.add_argument("new_id", type=int)
    dec_sup.add_argument("--reason", default=None, help="Why, in the user's own words")
    dec_sup.add_argument(
        "--force", action="store_true",
        help="Allow superseding across different projects",
    )
    dec_restore = dec_sub.add_parser("restore", help="Undo a supersession")
    dec_restore.add_argument("id", type=int)
    dec_retract = dec_sub.add_parser("retract", help="Mark a decision wrong outright (not merely replaced)")
    dec_retract.add_argument("id", type=int)
    dec_retract.add_argument("--reason", default=None)
    dec_links = dec_sub.add_parser("links", help="List supersede/conflict candidate links")
    dec_links.add_argument("--project", default=None)
    dec_links.add_argument(
        "--state", default=None,
        help="candidate | applied | rejected | restored (omit for every state)",
    )
    dec_confirm = dec_sub.add_parser("confirm", help="Confirm a candidate link, applying it")
    dec_confirm.add_argument("link_id", type=int)
    dec_reject = dec_sub.add_parser("reject", help="Reject a candidate link")
    dec_reject.add_argument("link_id", type=int)
    dec_bf = dec_sub.add_parser(
        "backfill", help="Backfill the decisions table from existing episodes.decisions"
    )
    dec_bf.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    dec_bf.add_argument(
        "--apply", dest="apply", action="store_true",
        help="Actually write the backfill (idempotent; safe to re-run)",
    )
    dec_bf.add_argument("--limit", type=int, default=None)
    dec.set_defaults(func=cmd_decisions)

    ep = sub.add_parser("episode", help="Episode maintenance (memory reliability W5.6)")
    ep_sub = ep.add_subparsers(dest="episode_cmd", required=True)
    ep_forget = ep_sub.add_parser("forget", help="Soft-delete an episode and remove its vectors")
    ep_forget.add_argument("id", type=int)
    ep_edit = ep_sub.add_parser("edit", help="Correct an episode's summary and re-embed it")
    ep_edit.add_argument("id", type=int)
    ep_edit.add_argument("--summary", required=True, help="Replacement summary text")
    ep.set_defaults(func=cmd_episode)

    tp = sub.add_parser("topic", help="Topic maintenance (memory reliability W5.6)")
    tp_sub = tp.add_subparsers(dest="topic_cmd", required=True)
    tp_purge = tp_sub.add_parser(
        "purge", help="Hard-delete a TOMBSTONED topic + its revisions/embeddings"
    )
    tp_purge.add_argument("slug")
    tp_purge.add_argument("--yes", action="store_true", help="Required to confirm the hard delete")
    tp.set_defaults(func=cmd_topic)

    bf = sub.add_parser("backfill", help="Backfill jobs (memory reliability W1.5)")
    bf_sub = bf.add_subparsers(dest="backfill_cmd", required=True)
    bf_identity = bf_sub.add_parser(
        "identity", help="Derive repo_root/project for episodes with an absolute-path scope"
    )
    bf_identity.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    bf_identity.add_argument(
        "--apply", dest="apply", action="store_true",
        help="Actually write the backfill (destructive; needs an explicit go — never run against the live shared hub without it)",
    )
    bf_identity.add_argument("--limit", type=int, default=None)
    bf.set_defaults(func=cmd_backfill)

    proj = sub.add_parser("project", help="Project identity maintenance (K6)")
    proj_sub = proj.add_subparsers(dest="project_cmd", required=True)
    proj_bf = proj_sub.add_parser(
        "backfill",
        help="Resolve a project for episodes that have none (session lineage / repo_root only)",
    )
    proj_bf.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    proj_bf.add_argument(
        "--apply", dest="apply", action="store_true",
        help="Actually write the backfill (destructive; needs an explicit go — never run against the live shared hub without it)",
    )
    proj_bf.add_argument("--limit", type=int, default=None)
    proj.set_defaults(func=cmd_project)

    hy = sub.add_parser("hygiene", help="Graph hygiene jobs (memory reliability W5.2)")
    hy_sub = hy.add_subparsers(dest="hygiene_cmd", required=True)
    hy_paths = hy_sub.add_parser(
        "paths", help="Report (or purge) path: graph nodes that fail the real-path shape rule"
    )
    hy_paths.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    hy_paths.add_argument(
        "--apply", dest="apply", action="store_true",
        help="Actually delete the failing path: nodes (destructive; needs an explicit go)",
    )
    hy_com = hy_sub.add_parser(
        "commitments",
        help="Re-judge open commitments: drop non-commitments, merge paraphrase duplicates",
    )
    hy_com.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    hy_com.add_argument(
        "--apply", dest="apply", action="store_true",
        help="Actually drop/merge (backs the table up first; never DELETEs)",
    )
    hy_com.add_argument("--project", default=None, help="Limit to one project")
    hy_com.add_argument("--limit", type=int, default=None, help="Max open commitments to judge")
    hy_com.add_argument(
        "--max-calls", dest="max_calls", type=int, default=None,
        help="Cost guard: max summariser calls per run (default 8, ~40 items each)",
    )
    hy_com.add_argument(
        "--session-ended", dest="session_ended", action="store_true",
        help="Model-free ownership pass: fill owner/future_trigger, close "
             "assistant commitments with no cross-session trigger whose "
             "session has ended, drop within-session reporting duties",
    )
    hy_com.add_argument(
        "--ended-after-hours", dest="ended_after_hours", type=int, default=None,
        help="With --session-ended: silence (hours) after which a session counts "
             "as ended (default 6)",
    )
    hy.set_defaults(func=cmd_hygiene)

    nt = sub.add_parser("notes", help="Index harness-native per-project notes as topics (memory reliability W4)")
    nt_sub = nt.add_subparsers(dest="notes_cmd", required=True)
    nt_reconcile = nt_sub.add_parser(
        "reconcile",
        help="Mirror ~/.claude/projects/<slug>/memory/*.md and ~/.codex/memories/*.md into topics",
    )
    nt_reconcile.add_argument("--dry-run", action="store_true", help="Report without writing")
    nt_reconcile.add_argument(
        "--changed-only", action="store_true",
        help="F1: skip files unchanged since the last changed-only run (Stop hook / WatchPaths agent)",
    )
    nt_index = nt_sub.add_parser(
        "index",
        help="P5 G1: rewrite one memory dir's MEMORY.md under the host's load cap, ranked",
    )
    nt_index.add_argument("--project", required=True, help="A memory dir, e.g. ~/.claude/projects/<slug>/memory")
    nt_index.add_argument("--cap", type=int, default=20_000,
                           help="Byte cap for the kept index (default 20000; see khipu.organise.DEFAULT_INDEX_CAP_BYTES)")
    nt_index.add_argument("--dry-run", action="store_true", help="Report the summary; never write")
    nt_index.add_argument(
        "--force", action="store_true",
        help="Write even when it would drop more than 30%% of the existing index's lines",
    )
    nt_index.set_defaults(func=cmd_notes)
    nt_split = nt_sub.add_parser(
        "split",
        help="P5 G2/G4: split one oversized note by '## ' section into child notes, then re-reconcile",
    )
    nt_split.add_argument("path", help="Path to the note .md file to split")
    nt_split.add_argument("--dry-run", action="store_true", help="Report the split plan; never write")
    nt_split.set_defaults(func=cmd_notes)
    nt_stale = nt_sub.add_parser(
        "stale",
        help="P5 G2: report-only — note topics with no recent hits and an old event_at",
    )
    nt_stale.add_argument("--project", default=None, help="Restrict to one resolved project")
    nt_stale.set_defaults(func=cmd_notes)
    nt_supersede = nt_sub.add_parser(
        "supersede",
        help="P5 R6/G5: mark OLD superseded and point it at NEW",
    )
    nt_supersede.add_argument("old_slug")
    nt_supersede.add_argument("new_slug")
    nt_supersede.set_defaults(func=cmd_notes)
    nt.set_defaults(func=cmd_notes)

    paths = sub.add_parser(
        "paths",
        help="Show or set Mac-local data directory (DSN/certs/cache files)",
    )
    paths.add_argument(
        "--set",
        default=None,
        metavar="DIR",
        help="Set local data folder (copies dsn/root.crt if missing)",
    )
    paths.set_defaults(func=cmd_paths)

    bl = sub.add_parser(
        "backup-local",
        help="Zip Mac-local data dir for download/backup",
    )
    bl.add_argument(
        "--out",
        required=True,
        help="Destination .zip path, or a directory to write khipu-local-*.zip into",
    )
    bl.set_defaults(func=cmd_backup_local)

    il = sub.add_parser(
        "import-local",
        help="Import a local backup zip/dir into the Mac data folder",
    )
    il.add_argument("--source", required=True, help="Path to .zip or directory")
    il.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite existing files (default merges / skips existing)",
    )
    il.set_defaults(func=cmd_import_local)

    cap = sub.add_parser(
        "capture",
        help="Capture an episode (JSON payload on stdin) — routed by capture_mode",
    )
    cap.add_argument("--payload-file", help="Read the JSON payload from a file")
    cap.add_argument(
        "--mode",
        choices=("legacy", "dual", "hub"),
        help="Override capture_mode for this run",
    )
    cap_sub = cap.add_subparsers(dest="capture_cmd", required=False)
    cap_now = cap_sub.add_parser(
        "now",
        help="Flag a session for capture on its next Stop, regardless of cadence (K1)",
    )
    cap_now.add_argument("--note", default=None, help="Attached to the job; lands in verbatim.note")
    cap_now.add_argument(
        "--harness", default=None,
        help="Defaults to the most recently active local session's harness",
    )
    cap_now.add_argument(
        "--session-id", dest="session_id", default=None,
        help="Defaults to the most recently active local session's id",
    )
    cap_now.set_defaults(func=cmd_capture_now)
    cap.set_defaults(func=cmd_capture)

    gt = sub.add_parser(
        "get",
        help="Episode or topic detail by id/slug, including the verbatim tier (K2)",
    )
    gt.add_argument(
        "id", help="Episode id (digits), topic slug, or library:<name>:<doc>[#<chunk>]"
    )
    gt.add_argument("--kind", choices=("episode", "topic", "library"), default=None)
    gt.add_argument(
        "--offset", type=int, default=None,
        help="Topics: UTF-8 byte offset to start the body page at (default 0)",
    )
    gt.add_argument(
        "--max-bytes", dest="max_bytes", type=int, default=None,
        help="Topics: largest body page in UTF-8 bytes (default 65536, at most 524288)",
    )
    gt.add_argument(
        "--all", action="store_true",
        help="Topics: write the whole body (from --offset) to stdout, page by page",
    )
    gt.set_defaults(func=cmd_get)

    mg = sub.add_parser(
        "migrate", help="Apply pending ops/migrations/*.sql to the database"
    )
    mg.add_argument(
        "--dry-run", action="store_true", help="Show applied/pending; change nothing"
    )
    mg.set_defaults(func=cmd_migrate)

    db = sub.add_parser(
        "db",
        help="Connect / move / preflight the Postgres database (setup pipeline)",
    )
    db_sub = db.add_subparsers(dest="db_cmd", required=True)
    db_pre = db_sub.add_parser(
        "preflight",
        help="Run setup validation against a DSN; it may modify remote schema/privileges, but does not store the DSN, install jobs, or run a probe",
        description="Run setup validation against a DSN. It may modify remote schema/privileges, but does not store the DSN, install jobs, or run a probe.",
    )
    # --dsn is optional here (and on connect/move's --to below) so a caller that
    # cannot risk the password showing up in `ps` (the desktop app) can set
    # KHIPU_DB_DSN in the child's environment instead — see cmd_db's resolution
    # and apps/desktop/src-tauri/src/lib.rs's khipu_db_preflight/connect/move,
    # which mirror set_khipu_secret's "never in argv" rule via env instead of
    # stdin (env is enough here: these are one-shot .output() calls, not a
    # secret-write that needs the write-then-close stdin dance).
    db_pre.add_argument(
        "--dsn", default=None, help="Connection string to check (or KHIPU_DB_DSN)"
    )
    db_pre.set_defaults(func=cmd_db)
    db_conn = db_sub.add_parser(
        "connect", help="Run the full connect pipeline and store the DSN"
    )
    db_conn.add_argument(
        "--dsn", default=None, help="Connection string to adopt (or KHIPU_DB_DSN)"
    )
    db_conn.add_argument(
        "--use-stored",
        action="store_true",
        help="Use the already-configured DSN (Keychain / data-folder fallback) "
        "instead of --dsn/KHIPU_DB_DSN — for finishing setup right after "
        "install-local-postgres or a join import, which already stored it",
    )
    db_conn.add_argument(
        "--root-crt", default=None, help="Path to a certificate file to trust (verify-full etc.)"
    )
    db_conn.add_argument(
        "--no-jobs", action="store_true", help="Skip installing the nightly/monthly/graph LaunchAgents"
    )
    db_conn.add_argument(
        "--no-prove", action="store_true", help="Skip the capture-to-search round trip"
    )
    db_conn.set_defaults(func=cmd_db)
    db_move = db_sub.add_parser(
        "move", help="Copy the database to another host, verify row counts, and switch"
    )
    db_move_target = db_move.add_mutually_exclusive_group()
    db_move_target.add_argument(
        "--to", default=None, help="Target connection string (or KHIPU_DB_TARGET_DSN)"
    )
    db_move_target.add_argument(
        "--new-local",
        dest="new_local",
        action="store_true",
        help="Create a fresh local-Docker Postgres and use it as the target "
        "(mutually exclusive with --to/KHIPU_DB_TARGET_DSN)",
    )
    db_move.add_argument(
        "--dry-run", action="store_true", help="Preflight + row counts only; copies nothing"
    )
    db_move.add_argument(
        "--into-nonempty",
        dest="into_nonempty",
        action="store_true",
        help="Allow a target that already holds Khipu rows",
    )
    db_move.set_defaults(func=cmd_db)
    db_status = db_sub.add_parser(
        "status", help="Current masked DSN, host kind, and row counts"
    )
    db_status.set_defaults(func=cmd_db)

    join = sub.add_parser(
        "join",
        help="Export/import hub join kits (passphrase required to export; never prints secrets)",
    )
    join_sub = join.add_subparsers(dest="join_cmd", required=True)
    join_export = join_sub.add_parser(
        "export", help="Write a join kit from this Mac's Keychain/config"
    )
    _add_passphrase_args(join_export)
    join_export.add_argument(
        "--allow-unencrypted",
        action="store_true",
        help="I understand: make an unencrypted kit anyone with the file can read",
    )
    join_export.add_argument(
        "--out",
        required=True,
        help="Destination .khipujoin file path",
    )
    join_export.set_defaults(func=cmd_join)
    join_import = join_sub.add_parser(
        "import", help="Apply a join kit's hub credentials on this Mac"
    )
    _add_passphrase_args(join_import)
    join_import.add_argument(
        "--file",
        required=True,
        help="Path to a .khipujoin file",
    )
    join_import.add_argument(
        "--skip-migrate-check",
        action="store_true",
        help="Skip applying pending migrations after import",
    )
    join_import.set_defaults(func=cmd_join)
    join_advertise = join_sub.add_parser(
        "advertise",
        help="Advertise join kit on LAN via Bonjour + TLS (macOS dns-sd)",
    )
    _add_passphrase_args(join_advertise)
    join_advertise.add_argument(
        "--allow-unencrypted",
        action="store_true",
        help="I understand: make an unencrypted kit anyone with the file can read",
    )
    join_advertise.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Seconds to advertise (default 600)",
    )
    join_advertise.add_argument(
        "--pin",
        default=None,
        help="Optional fixed 6-digit PIN (default: random)",
    )
    join_advertise.set_defaults(func=cmd_join)
    join_receive = join_sub.add_parser(
        "receive",
        help="Find nearby Mac, send PIN, import join kit",
    )
    _add_passphrase_args(join_receive)
    join_receive.add_argument(
        "--pin",
        required=True,
        help="Six-digit PIN shown on the exporting Mac",
    )
    join_receive.add_argument(
        "--out",
        default=None,
        help="Optional path to save the .khipujoin file",
    )
    join_receive.set_defaults(func=cmd_join)

    cfg = sub.add_parser("config", help="Show / set Hub config (capture_mode, paths)")
    cfg.add_argument(
        "--set",
        nargs=2,
        metavar=("KEY", "VALUE"),
        help="Persist a machine-specific path (memory_root, memory_repo, "
        "capture_v2, graph_sqlite, gemini_key_file), a 0-1 similarity knob "
        "(dedup_similarity, commitment_close_similarity), the relevance "
        "floor (relevance.cosine_floor, above 0 up to 1; per embedding profile "
        "relevance.cosine_floor_by_profile.<profile>), a daily call "
        "ceiling (synth_daily_calls, embed_daily_calls, "
        "library_embed_daily_calls; whole number, 0 = unlimited), the "
        "summariser fallback (synth_fallback gemini|none: what a failed "
        "window does on a provider other than Gemini), the comma-"
        'separated user_aliases list (e.g. --set user_aliases "matt,matthew") '
        "the graph backup destination (graph_backup_remote, an rclone "
        "remote:path, default r2:khipu-backups/khipu-graph), how far back "
        "doctor's rate checks look (doctor_rate_window_minutes, default 60), "
        "how long the promotion gate waits for an unreachable hub "
        "(promote_hub_wait_minutes, default 10), the red doctor checks the "
        "next promotion skips once (promote_gate_ignore, comma-separated "
        "check names; the owner's only, it clears after one use or a day) "
        "or the plan usage accounts as JSON (usage_accounts "
        '\'[{"label": "Claude second", "harness": "claude", "home": "~/.claude-second"}]\')',
    )
    cfg.add_argument("--unset", metavar="KEY",
                     help="Remove a path setting, a daily call ceiling, a minutes setting, synth_fallback, graph_backup_remote or usage_accounts, or "
                     "relevance.cosine_floor / "
                     "relevance.cosine_floor_by_profile.<profile> to restore its default")
    cfg.add_argument(
        "--set-gateway-url",
        metavar="URL",
        help="Public https:// URL of the Khipu gateway (empty string clears it)",
    )
    cfg.add_argument(
        "--set-capture-mode",
        choices=("legacy", "dual", "hub"),
        help="Persist capture_mode to the Hub config file",
    )
    cfg.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    cfg.add_argument("--expect", default=None, metavar="V",
                     help="With usage_accounts: the list's version as read; refused if it changed since")
    cfg.add_argument("--at-owner-request", default=None, metavar="REQUEST",
                     help="With usage_accounts or a routing setting, a conversation: the owner's request, in one "
                          "line (the change stays unconfirmed until he answers it)")
    cfg.add_argument("--answer-request", nargs=2, default=None, metavar=("KEY", "ANSWER"),
                     help="The owner's answer (confirm or undo) to a conversation's change to a routing setting")
    cfg.set_defaults(func=cmd_config)

    acc = sub.add_parser("accounts", help="The accounts plan usage reads: track, rename, stop tracking, move "
                                          "(owner only; for the app)")
    acc.set_defaults(func=cmd_accounts, accounts_cmd=None)
    acc_owner = argparse.ArgumentParser(add_help=False)
    acc_owner.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    acc_cas = argparse.ArgumentParser(add_help=False)
    acc_cas.add_argument("--expect", default=None, metavar="V",
                         help="The list's version as read (accounts status); refused if it changed since")
    acc_sub = acc.add_subparsers(dest="accounts_cmd")
    acc_sub.add_parser("status", help="The tracked accounts, the list's version and what is not offered again")
    atrack = acc_sub.add_parser("track", parents=[acc_owner, acc_cas], help="Track one or more logins")
    atrack.add_argument("--entries", required=True, metavar="JSON",
                        help='[{"harness": "claude", "home": "~/.claude-work" or null, "label": "..." or null}]')
    atrack.add_argument("--shown-defaults", default=None, metavar="JSON",
                        help="The default accounts as the app showed them (written out when no list is saved)")
    atrack.add_argument("--no-read", action="store_true", help="Do not read the new accounts once")
    aren = acc_sub.add_parser("rename", parents=[acc_owner, acc_cas], help="Rename an account (its key stays)")
    aren.add_argument("--account", required=True, metavar="KEY")
    aren.add_argument("--name", required=True)
    astop = acc_sub.add_parser("stop", parents=[acc_owner, acc_cas], help="Stop tracking an account")
    astop.add_argument("--account", required=True, metavar="KEY")
    astop.add_argument("--keep-priority", action="store_true", help="Leave it in your priority")
    astop.add_argument("--keep-pause", action="store_true", help="Leave its pause until its end")
    astop.add_argument("--keep-lanes", action="store_true", help="Leave the lanes that point at it")
    astop.add_argument("--end-hold", action="append", default=None, metavar="ID", help="End this hold on it too")
    astop.add_argument("--confirm-last", action="store_true",
                       help="It is the last account: track none (Read no accounts)")
    amove = acc_sub.add_parser("move", parents=[acc_owner, acc_cas],
                               help="The account's login folder moved: carry everything to the new folder")
    amove.add_argument("--account", required=True, metavar="KEY")
    amove.add_argument("--folder", required=True, metavar="F")
    for name, words in (("decline", "Not this one: do not offer this login again"),
                        ("offer-again", "Offer a login turned down again")):
        adec = acc_sub.add_parser(name, parents=[acc_owner], help=words)
        adec.add_argument("--harness", required=True, choices=("claude", "codex", "grok", "muse"))
        adec.add_argument("--folder", default=None, metavar="F", help="The login folder; none = the default login")
    aauto = acc_sub.add_parser("auto-track", parents=[acc_owner],
                               help="Track found logins a conversation used without asking (on this Mac)")
    aauto.add_argument("state", choices=("on", "off"))
    areq = acc_sub.add_parser("request", parents=[acc_owner, acc_cas],
                              help="Answer a conversation's change to the account list")
    areq.add_argument("action", choices=("confirm", "undo"))

    bud = sub.add_parser("budget", help="Daily call ceilings: status, and lifting one")
    bud_sub = bud.add_subparsers(dest="budget_cmd", required=True)
    bud_sub.add_parser("status", help="Every scope's calls, ceiling and its source; synth tokens and cost")
    lift = bud_sub.add_parser("lift", help="Lift a ceiling: today only, to N, or remove it")
    lift.add_argument("scope", choices=BUDGET_LIFT_SCOPES)
    how = lift.add_mutually_exclusive_group(required=True)
    how.add_argument("--today", action="store_true",
                     help="Reset today's counter: the full ceiling is available again until UTC midnight")
    how.add_argument("--cap", type=int, metavar="N", help="Persist a new daily ceiling (0 = unlimited)")
    how.add_argument("--unlimited", action="store_true", help="Persist no ceiling (cap 0)")
    bud.set_defaults(func=cmd_budget)

    ig = sub.add_parser(
        "integrations",
        help="Per-harness native packs: install / verify / uninstall / status",
    )
    ig_sub = ig.add_subparsers(dest="integ_cmd", required=True)
    for name, help_ in (
        (
            "install",
            "Write MCP + Khipu stop-hook entries (alongside legacy), then verify",
        ),
        ("verify", "Probe MCP handshake + hook exit for installed components"),
        ("uninstall", "Remove Khipu-owned entries only (backups kept)"),
        ("status", "Which components are present per harness"),
    ):
        sp = ig_sub.add_parser(name, help=help_)
        sp.add_argument(
            "harness",
            nargs="?",
            default="all",
            choices=("all", "claude_code", "cursor", "aegis", "codex", "grok_bot"),
        )
        if name in ("install", "uninstall"):
            sp.add_argument("--dry-run", action="store_true")
        if name in ("install", "uninstall", "verify"):
            sp.add_argument(
                "--home",
                help="claude_code only: act on this one Claude home (a `homes[].path` from "
                     "`integrations status`) instead of every home Khipu found",
            )
        if name in ("install", "uninstall", "status", "verify"):
            sp.add_argument(
                "--project",
                help="Cursor / Grok Bot: repo dir for .cursor/rules/khipu.mdc and .cursor/mcp.json",
            )
        if name == "install":
            sp.add_argument("--no-verify", action="store_true")
    ig.set_defaults(func=cmd_integrations)

    for name, help_ in (
        (
            "sessions",
            "Native session capture (every harness): drain / sweep / status / liveness",
        ),
        ("aegis", "Older name for `sessions`; same queue, same drain"),
    ):
        ag = sub.add_parser(name, help=help_)
        ag_sub = ag.add_subparsers(dest="aegis_cmd", required=True)
        dr = ag_sub.add_parser(
            "drain", help="Turn queued sessions (any harness) into episodes"
        )
        dr.add_argument(
            "--limit", type=int, default=None, help="Process at most N jobs"
        )
        dr.add_argument(
            "--dry-run", action="store_true", help="Extract and print; write nothing"
        )
        sw = ag_sub.add_parser("sweep", help="Queue idle unread session windows")
        sw.add_argument("--limit", type=int, default=25, help="Read at most N transcripts")
        sw.add_argument("--dry-run", action="store_true", help="Report candidates without queueing or advancing state")
        stt = ag_sub.add_parser(
            "status",
            help="Per-harness liveness (default) or one harness's queue + last dispatch",
        )
        stt.add_argument(
            "--harness",
            default="aegis" if name == "aegis" else "all",
            choices=("all", "claude_code", "cursor", "codex", "aegis"),
        )
        ag_sub.add_parser(
            "liveness",
            help="Red/green per harness; exit 2 if any harness is not being recorded",
        )
        ag.set_defaults(func=cmd_sessions)

    gsy = sub.add_parser(
        "git-sync",
        help="Memory-tree git auto-sync liveness (heartbeat + repo evidence); exit 2 if red",
    )
    gsy.set_defaults(func=cmd_git_sync)

    gs = sub.add_parser(
        "graph-sync",
        help="Mirror graphify's graph.sqlite into PG (nodes/edges); --check reports drift only",
    )
    gs.add_argument(
        "--sqlite",
        help="Path to graph.sqlite (default: KHIPU_GRAPH_SQLITE or the Graph volume)",
    )
    gs.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the sync in a transaction and roll back",
    )
    gs.add_argument(
        "--check", action="store_true", help="Drift only; exit 2 when not zero"
    )
    gs.set_defaults(func=cmd_graph_sync)

    ob = sub.add_parser(
        "outbox", help="Offline outbox: drain (replay into PG) / status"
    )
    ob_sub = ob.add_subparsers(dest="outbox_cmd", required=True)
    obd = ob_sub.add_parser("drain", help="Replay queued captures into PG")
    obd.add_argument("--limit", type=int, default=None)
    ob_sub.add_parser("status", help="Pending count + oldest age + dead-letter count")
    ob_sub.add_parser(
        "retry-dead",
        help="Move captures buried after KHIPU_OUTBOX_MAX_ATTEMPTS failures back into the queue",
    )
    ob.set_defaults(func=cmd_outbox)

    jb = sub.add_parser(
        "jobs",
        help="Scheduled LaunchAgents: status, install, refresh (re-render after an app update), uninstall",
    )
    jb_sub = jb.add_subparsers(dest="jobs_cmd", required=True)
    jb_sub.add_parser("status", help="Print khipu.jobs.job_status() as JSON")
    jbi = jb_sub.add_parser("install", help="Render + load the named jobs (default: all)")
    jbi.add_argument("names", nargs="*", metavar="{nightly,monthly,graph_build,notes_watch,queue_drain,recall_daemon,replica_refresh}")
    jbr = jb_sub.add_parser(
        "refresh",
        help="Re-render + reload installed jobs whose plist is stale (default: all installed)",
    )
    jbr.add_argument("names", nargs="*", metavar="{nightly,monthly,graph_build,notes_watch,queue_drain,recall_daemon,replica_refresh}")
    jbu = jb_sub.add_parser("uninstall", help="Unload + remove the named jobs (default: all)")
    jbu.add_argument("names", nargs="*", metavar="{nightly,monthly,graph_build,notes_watch,queue_drain,recall_daemon,replica_refresh}")
    jb.set_defaults(func=cmd_jobs)

    pmt = sub.add_parser(
        "promote",
        help="Gate a ref in the inactive live slot and make it live on a pass (--rollback, --status, --gate-only)",
    )
    pmt.add_argument("ref", nargs="?", default=None, help="Ref to promote (default: origin/main)")
    pmt.add_argument("--rollback", action="store_true", help="Flip live back to the other slot, no gate")
    pmt.add_argument("--status", action="store_true", help="Live commit, main commit, drift, last gate result")
    pmt.add_argument("--gate-only", choices=("doctor", "recall", "capture", "version"), default=None,
                     metavar="STEP", help="Run one gate step (doctor, recall, capture, version); never flips")
    pmt.add_argument("--retry-queued", action="store_true",
                     help="Run the promotion queued while the hub was out of reach, if the hub answers now")
    pmt.add_argument("--json", action="store_true", help="Print the full result as JSON")
    pmt.set_defaults(func=cmd_promote)

    snap = sub.add_parser(
        "snapshot",
        help="Hub read replica: refresh hub_snapshot.sqlite / status",
    )
    snap_sub = snap.add_subparsers(dest="snapshot_cmd", required=True)
    snap_sub.add_parser(
        "refresh", help="Dump hub tables into hub_snapshot.sqlite (hub must be up)"
    )
    snap_sub.add_parser(
        "heal",
        help="Bring this Mac's copy up to date when it needs it (what the replica_refresh job runs)",
    )
    snap_sub.add_parser("status", help="Snapshot age, size, and row counts from meta")
    snap.set_defaults(func=cmd_snapshot)

    rc = sub.add_parser("recall", help="Search query log: recent / zero-result queries")
    rc_sub = rc.add_subparsers(dest="recall_cmd", required=True)
    rc_sub.add_parser(
        "serve", help="Run the warm recall service the per-prompt hook asks first (foreground)"
    )
    rc_sub.add_parser("status", help="Ping the warm recall service")
    rlog = rc_sub.add_parser("log", help="Print recent query_log.jsonl entries")
    rlog.add_argument("--tail", type=int, default=20)
    rzero = rc_sub.add_parser(
        "zero-results", help="Queries with result_count 0 in the last N days"
    )
    rzero.add_argument("--days", type=int, default=7)
    reval = rc_sub.add_parser(
        "eval",
        help=(
            "Score recall-golden.jsonl (or --golden / KHIPU_RECALL_GOLDEN) against one or all "
            "retrieval paths (W6.3 + Phase 1 session A); --record/--compare/--replay for regression control"
        ),
    )
    reval.add_argument(
        "--golden", default=None,
        help="Path to the golden JSONL file (default: <config dir>/recall-golden.jsonl, or KHIPU_RECALL_GOLDEN)",
    )
    reval.add_argument(
        "--path", choices=["explicit", "prompt", "status", "all"], default="explicit",
        help=(
            "Which retrieval path scores each entry: explicit=embed.hybrid_search (default, legacy "
            "behavior), prompt=recall_prompt.prior_work_for_prompt unbudgeted, status=the same "
            "budgeted (--budget-ms), all=every path. An entry's own 'paths' key narrows this further."
        ),
    )
    reval.add_argument(
        "--budget-ms", type=int, default=None,
        help="budget_ms for the status path (default 600 — recall_eval.DEFAULT_STATUS_BUDGET_MS)",
    )
    reval.add_argument(
        "--record", default=None,
        help="Write a control (ordered ids per entry/path + reason/degraded + a stamp) to this file instead of scoring",
    )
    reval.add_argument(
        "--compare", default=None,
        help="Rerun and diff against a control written by --record; exits 1 on any difference unless --allow-changes",
    )
    reval.add_argument(
        "--allow-changes", action="store_true",
        help="With --compare: still exit 0 when entries differ (latency differences never fail a comparison either way)",
    )
    reval.add_argument(
        "--replay", default=None,
        help="Build no-expectation entries from a query_log.jsonl instead of the golden file; only valid with --record/--compare",
    )
    reval.add_argument(
        "--rerank", choices=["on", "off"], default=None,
        help=(
            "Score the explicit path with the optional reranker switched on or off for this process "
            "only; 'on' scores each entry both ways and reports the expected id's rank with and without"
        ),
    )
    reval.add_argument(
        "--relevance-floor", dest="relevance_floor", choices=["on", "off"], default=None,
        help=(
            "Score the chosen --path(s) with the absolute relevance gate switched on or off for this "
            "process only; 'on' scores each entry both ways and lists every golden positive whose query the gate emptied"
        ),
    )
    reval.add_argument("--sample", type=int, default=None, help="With --replay: cap to N entries")
    reval.add_argument("--seed", type=int, default=0, help="With --replay --sample: deterministic sampling seed")
    rc.set_defaults(func=cmd_recall)

    tg = sub.add_parser(
        "topic-graph",
        help="Persist topic wiki/path graph (topics.links + Khipu-owned nodes/edges)",
    )
    tg_sub = tg.add_subparsers(dest="topic_graph_cmd", required=True)
    tgb = tg_sub.add_parser(
        "backfill",
        help="Walk all topic .md files: UPDATE topics.links/frontmatter and mint topic:/path: edges",
    )
    tgb.add_argument(
        "--dry-run",
        action="store_true",
        help="Report column updates + graph upserts; roll back writes",
    )
    tgb.add_argument("--memory-root", default=_memory_root_default())
    tg.set_defaults(func=cmd_topic_graph_backfill)

    gb = sub.add_parser(
        "grok-bot-config",
        help="Print the account-level Cursor cloud config (covers every repo) + the secret to add",
    )
    gb.set_defaults(
        func=lambda a: (
            print(
                json.dumps(
                    __import__(
                        "khipu.integrations", fromlist=["x"]
                    ).grok_bot_account_config(),
                    indent=2,
                )
            )
            or 0
        )
    )

    gw = sub.add_parser(
        "gateway",
        help="Khipu gateway bearer token: set / status (never prints the token)",
    )
    gw_sub = gw.add_subparsers(dest="gateway_cmd", required=True)
    gwt = gw_sub.add_parser(
        "token", help="Write / inspect ~/.config/khipu/gateway-token"
    )
    gwt_sub = gwt.add_subparsers(dest="gateway_token_cmd", required=True)
    gwt_set = gwt_sub.add_parser(
        "set",
        help="Write the bearer file (mode 0600) from stdin, or --from-env",
    )
    gwt_set.add_argument(
        "--from-env",
        nargs="?",
        const="KHIPU_GATEWAY_TOKEN",
        default=None,
        metavar="VAR",
        help="Read the token from this env var instead of stdin (default KHIPU_GATEWAY_TOKEN)",
    )
    gwt_sub.add_parser(
        "status", help="exists / mode / bytes only — never the token"
    )
    gw.set_defaults(func=cmd_gateway)

    feat = sub.add_parser("features", help="Show or set feature switches")
    feat.add_argument("--set", nargs=2, metavar=("NAME", "VALUE"), default=None)
    feat.set_defaults(func=cmd_features)

    br = sub.add_parser("briefs", help="Source-backed topic briefs: plan / build / show")
    br_sub = br.add_subparsers(dest="briefs_cmd", required=True)
    br_sub.add_parser("plan", help="Topics whose brief is missing or stale")
    br_build = br_sub.add_parser(
        "build", help="Build briefs for planned topics (needs the briefs switch on)"
    )
    br_build.add_argument("--topic", default=None, help="Build only this planned topic")
    br_build.add_argument("--limit", type=int, default=5, help="Most topics to build (default 5)")
    br_show = br_sub.add_parser("show", help="Print a topic's current brief")
    br_show.add_argument("slug")
    br.set_defaults(func=cmd_briefs)

    rf = sub.add_parser("reflect", help="Answer a question from memory with cited evidence (needs the reflect switch on)")
    rf.add_argument("question")
    rf.add_argument("--project", default=None, help="Prefer sources from this project")
    rf.set_defaults(func=cmd_reflect)

    comp = sub.add_parser(
        "components",
        help="Postgres / Graphify component install, status, and upgrades",
    )
    comp_sub = comp.add_subparsers(dest="components_cmd", required=True)

    scr = comp_sub.add_parser(
        "select-compat-row",
        help="Refresh matrix and persist versions.json pending row",
    )
    scr.add_argument(
        "--mode",
        required=True,
        choices=("local_docker", "remote"),
    )
    scr.add_argument("--pgvector-extversion", default=None)
    scr.add_argument("--server-version", default=None)
    scr.add_argument("--pgvector", default=None)
    scr.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use bundled ∪ cache only; do not fetch khipu-compat",
    )
    scr.set_defaults(func=cmd_components, components_cmd="select-compat-row")

    comp_sub.add_parser(
        "install-local-postgres",
        help="Pull/run local PG19 container through migrate (Welcome Radio A)",
    ).set_defaults(func=cmd_components, components_cmd="install-local-postgres")

    comp_sub.add_parser(
        "bootstrap-local-backup",
        help="pg_dump + restore_drill into ops_events (Welcome Radio A)",
    ).set_defaults(func=cmd_components, components_cmd="bootstrap-local-backup")

    comp_sub.add_parser(
        "install-graphify",
        help="Install pending graphify tarball (Task 5)",
    ).set_defaults(func=cmd_components, components_cmd="install-graphify")

    comp_sub.add_parser(
        "status-json",
        help="JSON component versions for the desktop shell",
    ).set_defaults(func=cmd_components, components_cmd="status-json")

    comp_sub.add_parser(
        "upgrade-postgres",
        help="Dump/restore Postgres upgrade runbook",
    ).set_defaults(func=cmd_components, components_cmd="upgrade-postgres")

    comp_sub.add_parser(
        "upgrade-graphify",
        help="Upgrade installed graphify semver",
    ).set_defaults(func=cmd_components, components_cmd="upgrade-graphify")

    cr = comp_sub.add_parser(
        "check-remote",
        help="Probe remote DSN for PG19 (+ optional vector/GRAPH_TABLE)",
    )
    cr.add_argument(
        "--full",
        action="store_true",
        help="After migrate: require vector extension and GRAPH_TABLE",
    )
    cr.set_defaults(func=cmd_components, components_cmd="check-remote")

    cst = comp_sub.add_parser("status", help="Show installed component versions")
    cst.set_defaults(func=cmd_components, components_cmd="status")

    cins = comp_sub.add_parser("install", help="Install a named component")
    cins.add_argument("install_target", choices=("postgres", "graphify"))
    cins.set_defaults(func=cmd_components, components_cmd="install")

    cup = comp_sub.add_parser("upgrade", help="Upgrade a named component")
    cup.add_argument("upgrade_target", choices=("postgres", "graphify"))
    cup.set_defaults(func=cmd_components, components_cmd="upgrade")

    comp.set_defaults(func=cmd_components)

    return p


@guard_output
def _dispatch_guarded_command(args: argparse.Namespace) -> int:
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 — the final stderr printer is guarded too
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    _add_paths()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func in (cmd_join, cmd_graph_backup):
        return _dispatch_guarded_command(args)
    try:
        return int(args.func(args))
    except Exception as e:  # noqa: BLE001 — CLI surface
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
