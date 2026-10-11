"""Offline outbox — the durability leg that lets Postgres be the writer of record.

Before this (2026-08-17 evening), a capture whose PG write failed either
survived only as a legacy file line (dual mode, healed later by the tail sync or
the nightly reconcile) or was lost outright (hub mode: no file, no retry). The
plan's condition for making ``hub`` the default was exactly this outbox.

Contract:
  * ``enqueue(payload)`` writes the capture payload to a durable JSON file
    under ``<data_dir>/outbox/`` (temp + rename). The filename is the episode's
    legacy identity — ``ts`` + ``md5(summary)`` — unless different evidence
    already occupies it. Conflicting payloads use a full-content hash; only
    equal payloads share a queued job.
  * ``drain()`` replays each job through the same upsert every other write path
    uses (``write_pg``: identity ``(ts, md5(summary))``, ON CONFLICT DO NOTHING),
    then embeds; a job is deleted only after PG accepted it. Replaying twice is
    harmless. A connection failure stops the drain early (every job would fail
    the same way) and keeps everything queued.
  * ``status()`` is what ``khipu doctor`` reports; **pending > 0 makes doctor
    red** — a queued job is, by definition, something PG does not yet have.

Drain points: scheduled/manual session drains (including empty session queues),
nightly session drains, and ``khipu outbox drain``. Hooks only enqueue.
Aegis's own hook cannot reach this directory (its sandbox denies ~/.config);
Aegis captures go through ``khipu aegis drain`` → ``capture()``, which is where
the outbox catches a PG failure for them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl


def _log(msg: str) -> None:
    print(f"[khipu-outbox] {msg}", file=sys.stderr, flush=True)


def outbox_dir() -> Path:
    env = os.environ.get("KHIPU_OUTBOX")
    if env:
        return Path(env)
    from khipu.paths import data_dir

    return data_dir() / "outbox"


def _identity(payload: dict[str, Any]) -> str:
    raw_ts = str(payload.get("ts") or "")
    if len(raw_ts) <= 64 and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", raw_ts):
        ts = raw_ts.replace("Z", "+00:00").replace("+00:00", "Z").replace(":", "").replace("-", "")
    else:
        # Preserve the established readable identity for real capture stamps,
        # but do not let arbitrary caller text become a filename component.
        ts = "invalid-" + hashlib.sha256(raw_ts.encode("utf-8")).hexdigest()[:16]
    md = hashlib.md5((payload.get("summary") or "").encode("utf-8")).hexdigest()[:10]
    return f"{ts or 'nots'}-{md}"


@contextmanager
def _locked_outbox():
    """Serialize short filesystem transitions, never database/provider I/O."""
    d = outbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield d
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def enqueue(payload: dict[str, Any], *, reason: str = "") -> Path:
    """Durably queue a capture; acknowledge an existing file only for equal content."""
    with _locked_outbox() as d:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
        collision = d / f"{digest}.json"
        if collision.exists():
            if _read_job(collision)["payload"] != payload:
                raise ValueError("outbox identity conflicts with existing evidence")
            return collision
        dst = d / f"{_identity(payload)}.json"
        job = {"payload": payload, "reason": reason, "queued_at": datetime.now(timezone.utc).isoformat(),
               "attempts": 0}
        if dst.exists():
            try:
                if _read_job(dst)["payload"] == payload:
                    return dst
            except (OSError, ValueError, TypeError):
                pass
            # Keep legacy names for uncontested jobs; conflicting evidence gets
            # a stable full-content hash rather than replacing either source.
            dst = collision
        _atomic_write(dst, job)
    _log(f"queued {dst.name} ({reason or 'pg write failed'})")
    return dst


def _atomic_write(dst: Path, job: dict[str, Any]) -> None:
    """Temp + rename, always. A torn job file is unreadable, and an unreadable
    job is never replayed and never deleted — it just holds `pending` above zero
    and keeps doctor red forever. The retry path used to write in place (audit
    2026-08-17), which is exactly where a crash is most likely."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(job, tmp, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, dst)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


MAX_ATTEMPTS = int(os.environ.get("KHIPU_OUTBOX_MAX_ATTEMPTS", "25"))


def dead_dir() -> Path:
    return outbox_dir() / "dead"


def quarantine_dir() -> Path:
    return outbox_dir() / "quarantine"


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


def _read_job(jp: Path) -> dict[str, Any]:
    raw = jp.read_bytes()
    job = json.loads(raw)
    if not isinstance(job, dict):
        raise ValueError("job envelope is not an object")
    if not isinstance(job.get("payload"), dict):
        raise ValueError("job payload is not an object")
    attempts = job.get("attempts", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("job attempts is not a non-negative integer")
    job["attempts"] = attempts
    return job


def _quarantine(jp: Path, error: Exception) -> Path:
    """Preserve malformed bytes and machine-readable reason outside replay."""
    qd = quarantine_dir()
    qd.mkdir(parents=True, exist_ok=True)
    name = jp.name if jp.parent == outbox_dir() else f"dead-{jp.name}"
    dst = qd / name
    if dst.exists():
        fd, unique = tempfile.mkstemp(prefix=f"{name}.", suffix=".json", dir=qd)
        os.close(fd)
        os.unlink(unique)
        dst = Path(unique)
    os.replace(jp, dst)
    _atomic_write(qd / f"{dst.stem}.error", {
        "source": jp.name,
        "error": f"{type(error).__name__}: {str(error)[:200]}",
        "quarantined_at": datetime.now(timezone.utc).isoformat(),
    })
    _log(f"quarantined malformed job {jp.name}: {type(error).__name__}")
    return dst


def retry_dead() -> dict[str, Any]:
    """Move every dead job back to the queue with attempts reset."""
    moved = 0
    quarantined = 0
    with _locked_outbox() as d:
        for jp in dead_jobs():
            try:
                job = _read_job(jp)
            except (OSError, ValueError, TypeError) as exc:
                _quarantine(jp, exc)
                quarantined += 1
                continue
            job["attempts"] = 0
            job.pop("buried_at", None)
            _atomic_write(d / jp.name, job)
            jp.unlink(missing_ok=True)
            moved += 1
    return {"moved": moved, "quarantined": quarantined, "pending": len(jobs())}


def jobs() -> list[Path]:
    try:
        return sorted(p for p in outbox_dir().glob("*.json") if p.is_file())
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
            job = json.loads(jp.read_bytes())
            if not isinstance(job, dict):
                raise ValueError("job envelope is not an object")
            q = job.get("queued_at")
            if not isinstance(q, str):
                raise ValueError("queued_at is not a string")
            age = int((now - datetime.fromisoformat(q)).total_seconds()) if q else None
        except (OSError, ValueError, TypeError):
            unreadable += 1
            continue
        attempts = job.get("attempts", 0)
        if (not isinstance(job.get("payload"), dict) or isinstance(attempts, bool)
                or not isinstance(attempts, int) or attempts < 0):
            unreadable += 1
        if age is not None and (oldest_age is None or age > oldest_age):
            oldest_age, oldest_job = age, jp.name
    quarantined = len(list(quarantine_dir().glob("*.json"))) if quarantine_dir().is_dir() else 0
    # Existing doctor callers use ``dead`` to detect work that cannot replay.
    return {"dir": str(outbox_dir()), "pending": len(js), "oldest_age_s": oldest_age,
            "oldest_job": oldest_job, "unreadable": unreadable,
            "dead": len(dead_jobs()) + quarantined, "quarantined": quarantined,
            "max_attempts": MAX_ATTEMPTS}


def drain(*, limit: int | None = None, time_budget_s: float | None = None) -> dict[str, Any]:
    """Elect one replayer without blocking enqueue during external I/O.

    The budget bounds starting jobs; in-flight provider calls finish normally.
    Files stay queued until PG accepts them, including after a process crash.
    """
    deadline = time.monotonic() + max(0, time_budget_s) if time_budget_s is not None else None
    d = outbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    out: dict[str, Any] = {"jobs": 0, "replayed": 0, "failed": 0, "stopped_early": False}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**out, "busy": True}
        js = jobs()
        if limit is not None:
            js = js[:limit]
        out["jobs"] = len(js)
        from khipu.capture import write_pg
        from khipu.embed import embed_on_capture

        for jp in js:
            if deadline is not None and time.monotonic() >= deadline:
                out["time_budget_exhausted"] = True
                break
            with _locked_outbox():
                if not jp.exists():
                    continue
                try:
                    snapshot = jp.read_bytes()
                    job = _read_job(jp)
                    payload = json.loads(json.dumps(job["payload"]))
                except (OSError, ValueError, TypeError) as e:
                    _quarantine(jp, e)
                    out["quarantined"] = out.get("quarantined", 0) + 1
                    continue
            try:
                stats = write_pg(payload)
            except Exception as e:  # noqa: BLE001 — keep the job; note the attempt
                out["failed"] += 1
                job["attempts"] += 1
                job["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                name = type(e).__name__
                unreachable = "Operational" in name or "Connection" in name or "connect" in str(e).lower()
                with _locked_outbox():
                    try:
                        # A concurrent forget/retry owns its change. Never
                        # resurrect a removed job or overwrite a replacement.
                        if jp.read_bytes() == snapshot:
                            if not unreachable and job["attempts"] >= MAX_ATTEMPTS:
                                _bury(jp, job)
                                out["buried"] = out.get("buried", 0) + 1
                            else:
                                _atomic_write(jp, job)
                    except OSError:
                        pass
                if unreachable:
                    out["stopped_early"] = True
                    _log(f"PG unreachable ({name}); leaving remaining jobs queued")
                    break
                _log(f"replay failed for {jp.name}: {name}: {e}")
                continue
            if stats.get("episode_inserted"):
                try:
                    embed_on_capture(payload)
                except Exception as e:  # noqa: BLE001 — row is durable; backfill heals vectors
                    _log(f"embed skipped for {jp.name}: {e}")
            with _locked_outbox():
                try:
                    if jp.read_bytes() == snapshot:
                        jp.unlink()
                except FileNotFoundError:
                    pass
            out["replayed"] += 1
            _log(f"replayed {jp.name} (inserted={stats.get('episode_inserted')})")
        return out
    finally:
        os.close(fd)


def forget_identity(ts: str, summary_md5: str) -> dict[str, Any]:
    """Remove queued/dead copies of a forgotten capture by the same identity."""
    target = _identity({"ts": ts, "summary": ""})
    target = f"{target.rsplit('-', 1)[0]}-{summary_md5[:10]}"
    removed = 0
    remaining: list[str] = []
    with _locked_outbox():
        for directory in (outbox_dir(), dead_dir(), quarantine_dir()):
            if not directory.is_dir():
                continue
            for jp in directory.glob("*.json"):
                try:
                    envelope = json.loads(jp.read_bytes())
                    payload = envelope.get("payload") if isinstance(envelope, dict) else None
                    if not isinstance(payload, dict):
                        raise ValueError("payload cannot establish identity")
                except (OSError, ValueError, TypeError) as exc:
                    # Do not delete an unknown file, but never claim it was
                    # cleared: it could be the forgotten episode's bytes.
                    remaining.append(f"{jp}: {type(exc).__name__}")
                    continue
                if _identity(payload) == target:
                    jp.unlink(missing_ok=True)
                    removed += 1
    return {"ok": not remaining, "removed": removed, "remaining": remaining}


if __name__ == "__main__":  # `python -m khipu.outbox [drain|status]`
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    print(json.dumps(drain() if cmd == "drain" else status(), indent=2))
    sys.exit(0)
