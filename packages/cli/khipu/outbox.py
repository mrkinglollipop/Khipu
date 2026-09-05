"""Offline outbox — the durability leg that lets Postgres be the writer of record.

Before this (2026-08-17 evening), a capture whose PG write failed either
survived only as a legacy file line (dual mode, healed later by the tail sync or
the nightly reconcile) or was lost outright (hub mode: no file, no retry). The
plan's condition for making ``hub`` the default was exactly this outbox.

Contract:
  * ``enqueue(payload)`` writes the capture payload to a durable JSON file
    under ``<data_dir>/outbox/`` (temp + rename). The filename is the episode's
    identity — ``ts`` + ``md5(summary)`` — so re-queuing the same failed capture
    from two legs (``khipu capture`` and capture_v2's mirror) yields ONE job.
  * ``drain()`` replays each job through the same upsert every other write path
    uses (``write_pg``: identity ``(ts, md5(summary))``, ON CONFLICT DO NOTHING),
    then embeds; a job is deleted only after PG accepted it. Replaying twice is
    harmless. A connection failure stops the drain early (every job would fail
    the same way) and keeps everything queued.
  * ``status()`` is what ``khipu doctor`` reports; **pending > 0 makes doctor
    red** — a queued job is, by definition, something PG does not yet have.

Drain points: every harness Stop hook (unsandboxed ones), the nightly
consolidate, ``khipu outbox drain``, and the desktop app's doctor refresh.
Aegis's own hook cannot reach this directory (its sandbox denies ~/.config);
Aegis captures go through ``khipu aegis drain`` → ``capture()``, which is where
the outbox catches a PG failure for them.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _log(msg: str) -> None:
    print(f"[khipu-outbox] {msg}", file=sys.stderr, flush=True)


def outbox_dir() -> Path:
    env = os.environ.get("KHIPU_OUTBOX")
    if env:
        return Path(env)
    from khipu.paths import data_dir

    return data_dir() / "outbox"


def _identity(payload: dict[str, Any]) -> str:
    ts = str(payload.get("ts") or "").replace(":", "").replace("-", "").replace("+00:00", "Z")
    md = hashlib.md5((payload.get("summary") or "").encode("utf-8")).hexdigest()[:10]
    return f"{ts or 'nots'}-{md}"


def enqueue(payload: dict[str, Any], *, reason: str = "") -> Path:
    """Durably queue a capture payload for a later PG replay. Idempotent by
    episode identity. Never raises for a bad payload — the caller already has
    a durable copy elsewhere or is about to write one."""
    d = outbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{_identity(payload)}.json"
    job = {"payload": payload, "reason": reason, "queued_at": datetime.now(timezone.utc).isoformat(),
           "attempts": 0}
    if dst.is_file():  # keep the first queued_at; bump nothing
        try:
            prev = json.loads(dst.read_text(encoding="utf-8"))
            job["queued_at"] = prev.get("queued_at", job["queued_at"])
            job["attempts"] = int(prev.get("attempts", 0))
        except (OSError, ValueError):
            pass
    _atomic_write(dst, job)
    _log(f"queued {dst.name} ({reason or 'pg write failed'})")
    return dst


def _atomic_write(dst: Path, job: dict[str, Any]) -> None:
    """Temp + rename, always. A torn job file is unreadable, and an unreadable
    job is never replayed and never deleted — it just holds `pending` above zero
    and keeps doctor red forever. The retry path used to write in place (audit
    2026-08-17), which is exactly where a crash is most likely."""
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, dst)


MAX_ATTEMPTS = int(os.environ.get("KHIPU_OUTBOX_MAX_ATTEMPTS", "25"))


def dead_dir() -> Path:
    return outbox_dir() / "dead"


def dead_jobs() -> list[Path]:
    d = dead_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if p.is_file())


def _bury(jp: Path, job: dict[str, Any]) -> None:
    """A payload that has failed MAX_ATTEMPTS times for a reason that is not
    the connection will never succeed by itself (audit 2026-09-04: attempts
    was written and never read, so such a job retried on every hook forever
    and kept doctor red). Move it aside so the queue drains and doctor names
    it; ``khipu outbox retry-dead`` puts it back after the cause is fixed."""
    dead_dir().mkdir(parents=True, exist_ok=True)
    job["buried_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(dead_dir() / jp.name, job)
    jp.unlink(missing_ok=True)
    _log(f"buried {jp.name} after {job.get('attempts')} attempts: {job.get('last_error')}")


def retry_dead() -> dict[str, Any]:
    """Move every dead job back to the queue with attempts reset."""
    moved = 0
    for jp in dead_jobs():
        try:
            job = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        job["attempts"] = 0
        job.pop("buried_at", None)
        _atomic_write(outbox_dir() / jp.name, job)
        jp.unlink(missing_ok=True)
        moved += 1
    return {"moved": moved, "pending": len(jobs())}


def jobs() -> list[Path]:
    try:
        return sorted(p for p in outbox_dir().glob("*.json"))
    except OSError:
        return []


def status() -> dict[str, Any]:
    """`pending` is what makes doctor red. `oldest_*` describe the job that has
    been WAITING longest, which is not the same as the one whose episode has the
    earliest timestamp — jobs() sorts by filename, i.e. by episode ts, so the
    old code reported whichever episode was oldest (audit 2026-08-17)."""
    js = jobs()
    oldest_age: int | None = None
    oldest_job: str | None = None
    unreadable = 0
    now = datetime.now(timezone.utc)
    for jp in js:
        try:
            q = json.loads(jp.read_text(encoding="utf-8")).get("queued_at")
            age = int((now - datetime.fromisoformat(q)).total_seconds()) if q else None
        except (OSError, ValueError):
            unreadable += 1
            continue
        if age is not None and (oldest_age is None or age > oldest_age):
            oldest_age, oldest_job = age, jp.name
    return {"dir": str(outbox_dir()), "pending": len(js), "oldest_age_s": oldest_age,
            "oldest_job": oldest_job, "unreadable": unreadable,
            "dead": len(dead_jobs()), "max_attempts": MAX_ATTEMPTS}


def drain(*, limit: int | None = None) -> dict[str, Any]:
    """Replay queued captures into PG. Deletes a job only after PG accepted it."""
    js = jobs()
    if limit is not None:
        js = js[:limit]
    out: dict[str, Any] = {"jobs": len(js), "replayed": 0, "failed": 0, "stopped_early": False}
    if not js:
        return out
    from khipu.capture import write_pg
    from khipu.embed import embed_on_capture

    for jp in js:
        try:
            job = json.loads(jp.read_text(encoding="utf-8"))
            payload = job["payload"]
        except (OSError, ValueError, KeyError) as e:
            out["failed"] += 1
            _log(f"unreadable job {jp.name}: {e}")
            continue
        try:
            stats = write_pg(payload)
        except Exception as e:  # noqa: BLE001 — keep the job; note the attempt
            out["failed"] += 1
            try:
                job["attempts"] = int(job.get("attempts", 0)) + 1
                job["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                _atomic_write(jp, job)
            except OSError:
                pass
            name = type(e).__name__
            if "Operational" in name or "Connection" in name or "connect" in str(e).lower():
                out["stopped_early"] = True
                _log(f"PG unreachable ({name}); leaving {len(js) - js.index(jp)} job(s) queued")
                break
            _log(f"replay failed for {jp.name}: {name}: {e}")
            if int(job.get("attempts", 0)) >= MAX_ATTEMPTS:
                _bury(jp, job)
                out["buried"] = out.get("buried", 0) + 1
            continue
        if stats.get("episode_inserted"):
            try:
                embed_on_capture(payload)
            except Exception as e:  # noqa: BLE001 — row is durable; backfill heals vectors
                _log(f"embed skipped for {jp.name}: {e}")
        jp.unlink(missing_ok=True)
        out["replayed"] += 1
        _log(f"replayed {jp.name} (inserted={stats.get('episode_inserted')})")
    return out


if __name__ == "__main__":  # `python -m khipu.outbox [drain|status]`
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    print(json.dumps(drain() if cmd == "drain" else status(), indent=2))
    sys.exit(0)
