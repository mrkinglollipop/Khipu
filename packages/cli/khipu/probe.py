"""End-to-end recall probe (W6.1) — "capture then search finds it", proven.

Root cause F (docs/plans/2026-09-03-memory-reliability.md): doctor measured
plumbing (mirror lag, coverage, drift) and never asked whether a capture is
actually FINDABLE afterward. ``run_probe`` closes that loop for real: it
writes a nonce episode through the exact same path a harness hook uses
(``khipu.capture.capture``), polls the default search until the episode
surfaces in the top 3, then cleans up after itself (soft-delete + drop its
embeddings, mirroring ``khipu episode forget``) regardless of outcome, and
records the result to a small state file under the package's state dir.

Only ``khipu doctor --probe`` and ``khipu integrations verify`` (once per
installed pack) ever call ``run_probe`` — and only when a human or the
orchestrator invokes those commands against a real hub. Plain ``khipu
doctor`` never calls it; it only reads the last recorded result via
``status()``. This module must never be exercised against the live shared
hub from an automated build/test run — tests exercise it with fakes only.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

STATE_FILE = "probe.json"
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_TIMEOUT_S = 120.0
MAX_PROBE_AGE_DAYS = 7
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def state_path():
    from khipu.paths import data_dir

    return data_dir() / STATE_FILE


def _log(msg: str) -> None:
    import sys

    print(f"[khipu-probe] {msg}", file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def _find_episode_id(session_id: str) -> int | None:
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM episodes WHERE session_id = %s ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
    return int(row[0]) if row else None


def _forget_episode(episode_id: int) -> dict[str, Any]:
    """Complete forget (khipu.forget: row, vectors, commitments, legacy line)
    for the probe's own nonce episode. Never raises: a cleanup failure is
    recorded, not fatal to the probe result already formed."""
    try:
        from khipu.forget import forget_everywhere

        out = forget_everywhere(episode_id)
        return {
            "ok": bool(out.get("ok")),
            "soft_deleted": out.get("soft_deleted"),
            "embeddings_removed": out.get("embeddings_removed"),
            "commitments_closed": out.get("commitments_closed"),
            "legacy_file": out.get("legacy_file"),
            **({"error": out.get("error")} if not out.get("ok") else {}),
        }
    except Exception as exc:  # noqa: BLE001 — cleanup must never mask the probe result
        _log(f"cleanup failed for episode {episode_id}: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def harness_state_path(harness: str):
    from khipu.paths import data_dir

    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (harness or "unknown"))
    return data_dir() / f"probe-{safe}.json"


def _write_state(result: dict[str, Any]) -> None:
    """probe.json keeps the LAST probe (the doctor gate); probe-<harness>.json
    keeps each pack's own last probe, so Harnesses can say "verified" per
    card instead of borrowing another pack's round trip (audit 2026-09-04
    §1.8: one file for all harnesses)."""
    try:
        from khipu.paths import ensure_data_dir

        ensure_data_dir()
        body = json.dumps(result, indent=2, default=str) + "\n"
        state_path().write_text(body, encoding="utf-8")
        harness = str(result.get("harness") or "").strip()
        if harness:
            harness_state_path(harness).write_text(body, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — state write must never crash the probe
        _log(f"failed to write probe state: {type(exc).__name__}: {exc}")


def _age_of(result: dict[str, Any]) -> float | None:
    ts_raw = result.get("ts")
    if not isinstance(ts_raw, str):
        return None
    try:
        parsed = datetime.strptime(ts_raw, _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def harness_results() -> dict[str, dict[str, Any]]:
    """Every pack's own last probe, keyed by harness, with ``age_seconds`` and
    a ``stale`` flag (older than MAX_PROBE_AGE_DAYS)."""
    from khipu.paths import data_dir

    out: dict[str, dict[str, Any]] = {}
    try:
        files = sorted(data_dir().glob("probe-*.json"))
    except OSError:
        return out
    for f in files:
        try:
            res = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        h = str(res.get("harness") or f.stem[len("probe-"):])
        age = _age_of(res)
        out[h] = {
            "ok": bool(res.get("ok")),
            "status": res.get("status"),
            "ts": res.get("ts"),
            "seconds": res.get("seconds"),
            "error": res.get("error"),
            "reason": res.get("reason"),
            "age_seconds": age,
            "stale": age is None or age > MAX_PROBE_AGE_DAYS * 86400,
        }
    return out


def run_probe(
    harness: str,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Write a nonce episode via the normal capture path, poll default hybrid
    search until it lands in the top 3 (or ``timeout_s`` elapses), forget it,
    and persist ``{ts, harness, ok, seconds, episode_id, error}`` to the state
    file. Always returns that dict; never raises out of this function — every
    failure mode (capture error, episode never found, search never surfaces
    it, cleanup failure) is captured in the result instead.
    """
    started = time.monotonic()
    ts = _now_iso()
    from khipu.config import capture_mode

    if capture_mode() == "legacy":
        # fix 12: legacy mode never writes PG (capture_v2 owns the file
        # wiki only) — asserting a PG round trip here would fail every time
        # for a reason that has nothing to do with recall quality. Record
        # 'skipped', not a failure, and doctor treats 'skipped' the same as
        # a genuine pass (see probe.status()).
        result = {
            "ts": ts, "harness": harness, "ok": True, "status": "skipped",
            "reason": "legacy-capture-mode", "seconds": round(time.monotonic() - started, 3),
            "episode_id": None, "error": None,
        }
        _write_state(result)
        return result

    nonce = uuid.uuid4().hex[:12]
    session_id = f"{harness}:probe:{nonce}"
    phrase = f"khipu recall probe nonce {nonce}"
    payload = {
        "summary": f"Khipu recall-quality probe {phrase} — safe to delete.",
        "session_id": session_id,
        "project": "khipu-probe",
        "scope": "khipu-probe",
        "topics": [],
        "harness": harness,
        "ts": ts,
    }
    result: dict[str, Any] = {
        "ts": ts,
        "harness": harness,
        "ok": False,
        "seconds": None,
        "episode_id": None,
        "error": None,
    }
    episode_id: int | None = None
    try:
        from khipu.capture import capture
        from khipu.embed import hybrid_search

        # PG-row-only, like the gateway: the legacy file leg would append the
        # nonce to episodes.jsonl (never forgotten there) and print its own
        # "OK · episode appended" lines to stdout, which breaks callers that
        # parse this command's JSON (the desktop Integrations pane).
        prev_mirror = os.environ.get("KHIPU_HUB_FILE_MIRROR")
        os.environ["KHIPU_HUB_FILE_MIRROR"] = "0"
        try:
            with contextlib.redirect_stdout(sys.stderr):
                rc = capture(payload)
        finally:
            if prev_mirror is None:
                os.environ.pop("KHIPU_HUB_FILE_MIRROR", None)
            else:
                os.environ["KHIPU_HUB_FILE_MIRROR"] = prev_mirror
        if rc != 0:
            raise RuntimeError(f"capture exited {rc}")
        episode_id = _find_episode_id(session_id)
        if episode_id is None:
            raise RuntimeError("capture reported success but no episode row was found")
        result["episode_id"] = episode_id

        deadline = time.monotonic() + timeout_s
        found = False
        while True:
            out = hybrid_search(phrase, limit=3)
            top_ids = {
                r.get("id") for r in out.get("results", []) if r.get("kind") == "episode"
            }
            if str(episode_id) in top_ids:
                found = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_s)
        if not found:
            raise RuntimeError(
                f"nonce episode {episode_id} did not reach top-3 within {timeout_s}s"
            )
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 — the probe result IS the failure report
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["seconds"] = round(time.monotonic() - started, 3)
        if episode_id is not None:
            result["cleanup"] = _forget_episode(episode_id)
        _write_state(result)
    return result


def last_result() -> dict[str, Any] | None:
    path = state_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status() -> dict[str, Any]:
    """Read-only — never runs the probe. Mirrors the exact gate ``khipu
    doctor`` (without ``--probe``) applies: red when no probe has ever run,
    the last one failed, or it is older than ``MAX_PROBE_AGE_DAYS`` (7) days.
    """
    last = last_result()
    if last is None:
        return {
            "ok": False,
            "reason": "no probe has ever run (khipu doctor --probe)",
            "last_probe": None,
            "age_seconds": None,
        }
    age_seconds = None
    ts_raw = last.get("ts")
    if isinstance(ts_raw, str):
        try:
            parsed = datetime.strptime(ts_raw, _TS_FMT).replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
        except ValueError:
            age_seconds = None
    stale = age_seconds is None or age_seconds > MAX_PROBE_AGE_DAYS * 86400
    ok = bool(last.get("ok")) and not stale
    reason = None
    if not last.get("ok"):
        reason = f"last probe failed: {last.get('error')}"
    elif stale:
        reason = (
            f"last probe is stale ({age_seconds / 86400:.1f} days old)"
            if age_seconds is not None
            else "last probe has no readable timestamp"
        )
    return {"ok": ok, "reason": reason, "last_probe": last, "age_seconds": age_seconds,
            "harnesses": harness_results()}
