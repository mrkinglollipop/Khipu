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

Drain points: Stop hooks unless queue-only, scheduled/manual session drains
(including empty session queues), nightly drains, and ``khipu outbox drain``.
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
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

from khipu.hub_errors import unreachable_error, unreachable_text
from khipu.capture_stores import outbox_payloads


def _log(msg: str) -> None:
    print(f"[khipu-outbox] {msg}", file=sys.stderr, flush=True)


def outbox_dir() -> Path:
    from khipu.paths import data_dir, isolated_dir

    isolated = isolated_dir()
    if isolated is not None:
        return isolated / "outbox"
    env = os.environ.get("KHIPU_OUTBOX")
    if env:
        return Path(env)

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


def _queued_origin() -> dict[str, Any]:
    """The origin to keep beside the payload, stamped now from the request
    context (gateway provenance, phase 1), so a replay from another process or
    machine writes the row as the caller that queued it. When this machine has
    no readable identity the owner is recorded as unknown (None), never left
    out: a job without the key is a legacy job, which the drainer stamps with
    its own identity, and that machine may not be the one that queued it."""
    from khipu.machine_identity import MachineIdentityError
    from khipu.request_context import request_origin

    try:
        return {"origin": request_origin()}
    except MachineIdentityError:
        return {"origin": None}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _same_evidence(job: dict[str, Any], payload: dict[str, Any], stamp: dict[str, Any]) -> bool:
    return (_canonical(job["payload"]) == _canonical(payload)
            and _canonical({k: job[k] for k in ("origin",) if k in job}) == _canonical(stamp))


def _validate_resolution(record: Any) -> None:
    """Validate archived content itself, never trust its saved hash claims."""
    from khipu.capture_identity import payload_sha256
    from khipu.capture import _legacy_preferences_contained, _legacy_projection_kind, _redact_capture_values

    outbox_payloads(record)
    resolution = record.get("resolution")
    queued = record.get("queued_evidence")
    if not isinstance(queued, dict) or not isinstance(queued.get("payload"), dict):
        raise ValueError("resolution queued envelope is invalid")
    # The hub proved the projection on the copy write_pg stores: origin
    # dropped and secrets redacted. The archive keeps the file's own bytes.
    hub_view = json.loads(json.dumps(queued["payload"]))
    hub_view.pop("origin", None)
    hub_view = _redact_capture_values(hub_view)
    if (not isinstance(resolution, dict)
            or resolution.get("kind") not in ("legacy_comma_projection", "legacy_landed_line",
                                              "preferences_contained", "preferences_contained_normalized")
            or type(resolution.get("episode_id")) is not int or resolution["episode_id"] <= 0
            or not isinstance(resolution.get("resolved_at"), str) or not resolution["resolved_at"]
            or resolution.get("original_payload_sha256") != payload_sha256(record["payload"])
            or resolution.get("queued_payload_sha256") != payload_sha256(hub_view)):
        raise ValueError("resolution record conflicts with archived evidence")
    from khipu.request_context import valid_origin

    attempts = queued.get("attempts", 0)
    if (type(attempts) is not int or attempts < 0
            or ("origin" in queued and not valid_origin(queued["origin"]))
            or queued.get("reason") != "recovery: the legacy key holds different facts"
            or "origin" not in queued or queued["origin"] is not None):
        raise ValueError("resolution queued envelope is invalid")
    # preferences_contained is the owner's merge command's proof
    # (outbox_preferences), never one the drain makes.
    if resolution["kind"] == "preferences_contained":
        proved = (_legacy_projection_kind(record["payload"], hub_view) is None
                  and _legacy_preferences_contained(record["payload"], hub_view))
    elif resolution["kind"] == "preferences_contained_normalized":
        from khipu.outbox_preferences import normalized_missing

        # The projection matched at archive time, when stored, so today's
        # noise list or scope limit never decides an old record.
        proved = (_legacy_projection_kind(record["payload"], hub_view) is None
                  and not _legacy_preferences_contained(record["payload"], hub_view)
                  and normalized_missing(record["payload"], hub_view,
                                         resolution.get("normalized_projection")) == [])
    else:
        proved = _legacy_projection_kind(record["payload"], hub_view) == resolution["kind"]
    if not proved:
        raise ValueError("resolution queued envelope is invalid")
    _canonical(record)


def _recovery_terminal(payload: dict[str, Any], stamp: dict[str, Any]) -> Path | None:
    """A file sweep cannot restart held retries or recreate resolved history.

    Called under the outbox lock. Direct capture enqueue and explicit owner
    retry remain able to create/move separate histories.
    """
    for path in dead_jobs():
        try:
            if _same_evidence(_read_job(path), payload, stamp):
                return path
        except (OSError, ValueError, TypeError, RecursionError):
            continue
    for path in sorted(resolved_dir().glob("*.json")):
        try:
            record = json.loads(path.read_bytes())
            _validate_resolution(record)
            if _same_evidence(record["queued_evidence"], payload, stamp):
                return path
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            continue
    return None


def archive_resolved(jp: Path, snapshot: bytes, record: dict[str, Any]) -> str:
    """Move one held job to resolved/ with its proof, as the drain does for a
    landed line: the record (both payloads and the whole job) is validated and
    written before the job file goes, under the same source fence and outbox
    lock. ``resolved``; ``changed`` when the file moved or changed since
    ``snapshot`` was read; ``forgotten`` when a source, or the episode the
    record names, is forgotten (the job is left for forget's sweep). Raises on an invalid record or a different
    record already archived under the job's id."""
    from khipu.tombstones import episode_hidden, write_guard

    _validate_resolution(record)
    record_id = hashlib.sha256(snapshot).hexdigest()
    with write_guard(*outbox_payloads(record)) as allowed:
        with _locked_outbox():
            try:
                if jp.read_bytes() != snapshot:
                    return "changed"
            except FileNotFoundError:
                return "changed"
            # An id forgotten after the merge's batch committed (that check
            # ran before this lock) still holds the job back.
            if not allowed or episode_hidden(record["resolution"]["episode_id"]):
                return "forgotten"
            resolved_dir().mkdir(parents=True, exist_ok=True)
            record_path = resolved_dir() / (record_id + ".json")
            if record_path.exists():
                saved = json.loads(record_path.read_bytes())
                _validate_resolution(saved)
                expected = {**record, "resolution": {**record["resolution"],
                                                     "resolved_at": saved["resolution"]["resolved_at"]}}
                if _canonical(saved) != _canonical(expected):
                    raise ValueError("resolution record conflicts with queued evidence")
            else:
                _atomic_write(record_path, record)
            jp.unlink()
    return "resolved"


def unresolve(record_id: str, destination: Path, kind: str) -> str:
    """Put a resolved job back where it was held (its ``queued_evidence``),
    then remove its record: the Undo of ``archive_resolved``. Only a record of
    ``kind`` moves. ``restored``; ``absent`` (no such record); ``present``
    (a job is already at ``destination``: never overwritten, the record
    stays); ``forgotten`` (a source, or the record's episode by id, is
    forgotten: the record stays for forget's sweep)."""
    from khipu.tombstones import episode_hidden, write_guard

    record_path = resolved_dir() / (record_id + ".json")
    try:
        data = record_path.read_bytes()
    except FileNotFoundError:
        return "absent"
    record = json.loads(data)
    _validate_resolution(record)
    if record["resolution"]["kind"] != kind:
        return "absent"
    with write_guard(*outbox_payloads(record)) as allowed:
        with _locked_outbox():
            try:
                if record_path.read_bytes() != data:
                    return "absent"
            except FileNotFoundError:
                return "absent"
            if not allowed or episode_hidden(record["resolution"]["episode_id"]):
                return "forgotten"
            if destination.exists():
                return "present"
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(destination, record["queued_evidence"])
            record_path.unlink()
    return "restored"


_STAMP = object()


def enqueue(payload: dict[str, Any], *, reason: str = "", origin: Any = _STAMP,
            recovery: bool = False) -> Path:
    """Durably queue a capture; acknowledge an existing file only for equal
    content and equal origin. A full disk is raised in plain words.

    ``origin``: the owner the replay writes the row as. By default it is
    stamped now from the request context; a writer that does not know who
    made the capture passes ``None`` (unknown, which no drainer claims), as
    the file recovery writers do for a line they could not land (a synced
    file can hold another machine's captures; third Astra review #3).
    ``recovery`` acknowledges matching held/resolved evidence durably rather
    than restarting its retry history on every sweep."""
    if origin is _STAMP:
        stamp = _queued_origin()
    else:
        from khipu.request_context import valid_origin

        if not valid_origin(origin):
            raise ValueError(f"not a queued origin: {origin!r}")
        stamp = {"origin": origin}
    # Only the envelope says who owns a queued capture; the caller's own claim
    # is dropped here as write_pg drops it.
    payload = {k: v for k, v in payload.items() if k != "origin"}
    from khipu.tombstones import ForgottenSource, write_guard

    # Registry first, then the outbox lock: forget takes the same order while
    # it publishes and scans, so this check and atomic replace are one fence.
    with write_guard(payload) as allowed:
        if not allowed:
            raise ForgottenSource("queued payload belongs to a forgotten source")
        with _locked_outbox() as d:
            if recovery and (terminal := _recovery_terminal(payload, stamp)) is not None:
                return terminal
            evidence = {"payload": payload, **stamp} if stamp else payload
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False,
                                              separators=(",", ":")).encode("utf-8")).hexdigest()
            collision = d / f"{digest}.json"
            if collision.exists():
                if not _same_evidence(_read_job(collision), payload, stamp):
                    raise ValueError("outbox identity conflicts with existing evidence")
                return collision
            dst = d / f"{_identity(payload)}.json"
            job = {"payload": payload, **stamp, "reason": reason,
                   "queued_at": datetime.now(timezone.utc).isoformat(), "attempts": 0}
            if dst.exists():
                try:
                    if _same_evidence(_read_job(dst), payload, stamp):
                        return dst
                except (OSError, ValueError, TypeError):
                    pass
                # Keep legacy names for uncontested jobs; conflicting evidence gets
                # a stable full-content hash rather than replacing either source.
                dst = collision
            try:
                _atomic_write(dst, job)
            except OSError as exc:
                from khipu.storage import describe_os_error, disk_full

                if disk_full(exc):
                    # Said plainly: the callers log it as the reason it was not queued.
                    raise OSError(exc.errno, f"the capture could not be queued: "
                                             f"{describe_os_error(exc)}") from exc
                raise
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


def resolved_dir() -> Path:
    return outbox_dir() / "resolved"


def dead_jobs() -> list[Path]:
    d = dead_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if p.is_file())


def _transition_destination(preferred: Path, job: dict[str, Any]) -> Path:
    """Choose without overwriting different evidence; caller holds the lock.

    Hash the complete envelope about to move, including retry history, rather
    than its legacy episode identity. Reuse requires canonical JSON equality;
    an unreadable occupant is different evidence too.
    """
    canonical = json.dumps(job, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def equal(path: Path) -> bool:
        try:
            value = json.loads(path.read_bytes())
            return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) == canonical
        except (OSError, ValueError, TypeError):
            return False

    if not preferred.exists() or equal(preferred):
        return preferred
    alternate = preferred.parent / (hashlib.sha256(canonical.encode("utf-8")).hexdigest() + ".json")
    if alternate.exists() and not equal(alternate):
        raise FileExistsError(f"outbox transition conflicts with existing evidence: {alternate.name}")
    return alternate


def _bury(jp: Path, job: dict[str, Any]) -> None:
    """A payload that has failed MAX_ATTEMPTS times for a reason that is not
    the connection will never succeed by itself (audit 2026-09-04: attempts
    was written and never read, so such a job retried on every hook forever
    and kept doctor red). Move it aside so the queue drains and doctor names
    it; ``khipu outbox retry-dead`` puts it back after the cause is fixed."""
    dead_dir().mkdir(parents=True, exist_ok=True)
    job["buried_at"] = datetime.now(timezone.utc).isoformat()
    if job.get("resolution_failure"):
        job["hold_reason"] = ("Resolution evidence could not be archived; automatic retries exhausted. "
                              "Both original and queued evidence are retained here. Owner must "
                              "inspect the journal conflict or storage failure before retry-dead.")
    elif job.get("evidence_conflict"):
        job["hold_reason"] = ("Distinct capture evidence requires owner reconciliation with the hub; "
                              "automatic retries exhausted. Evidence is retained here. "
                              "Retry dead jobs after resolving the difference.")
    destination = _transition_destination(dead_dir() / jp.name, job)
    if not destination.exists():
        _atomic_write(destination, job)
    jp.unlink(missing_ok=True)
    _log(f"buried {jp.name} after {job.get('attempts')} attempts: {job.get('last_error')}")


def _read_job(jp: Path) -> dict[str, Any]:
    raw = jp.read_bytes()
    job = json.loads(raw)
    if not isinstance(job, dict):
        raise ValueError("job envelope is not an object")
    if not isinstance(job.get("payload"), dict):
        raise ValueError("job payload is not an object")
    outbox_payloads(job)
    attempts = job.get("attempts", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("job attempts is not a non-negative integer")
    job["attempts"] = attempts
    from khipu.request_context import valid_origin

    if "origin" in job and not valid_origin(job["origin"]):
        raise ValueError("job origin is not a queued origin")
    return job


def _quarantine_payloads(snapshot: bytes) -> tuple[list[dict[str, Any]], bool]:
    """Known identities and whether malformed nested evidence prevents a move.

    Still walk valid children of a malformed parent. Unknown nested shapes
    stay in place unless a known forgotten identity requires removal.
    """
    try:
        envelope = json.loads(snapshot)
        payloads = outbox_payloads(envelope, strict=False)
        try:
            outbox_payloads(envelope)
        except ValueError:
            nested = isinstance(envelope, dict) and any(
                key in envelope for key in ("queued_evidence", "resolution_evidence", "resolution"))
            return payloads, nested
        return payloads, False
    except RecursionError:
        return [], True
    except (ValueError, TypeError):
        return [], False


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
    from khipu import memory_lock
    from khipu.tombstones import RegistryError, write_guard

    # Do not hold the outbox lock while taking the registry lock: forget takes
    # the memory lock, the registry, then the outbox. A job is re-read under
    # all of them before moving.
    for jp in dead_jobs():
        # One memory-lock hold per job, from its read to its move, taken
        # before the outbox lock like every writer's.
        with memory_lock.shared():
            snapshot = b""
            try:
                with _locked_outbox():
                    snapshot = jp.read_bytes()
                    job = _read_job(jp)
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                identifiable, uncheckable = _quarantine_payloads(snapshot)
                if not identifiable:
                    with _locked_outbox():
                        if jp.exists() and not uncheckable:
                            _quarantine(jp, exc)
                            quarantined += 1
                    continue
                try:
                    with write_guard(*identifiable) as allowed:
                        with _locked_outbox():
                            if jp.exists() and jp.read_bytes() == snapshot:
                                if allowed and not uncheckable:
                                    _quarantine(jp, exc)
                                    quarantined += 1
                                elif not allowed:
                                    jp.unlink()
                except RegistryError as registry_error:
                    _log(f"retry-dead quarantine refused for {jp.name}: {registry_error}")
                continue
            try:
                with write_guard(*outbox_payloads(job)) as allowed:
                    with _locked_outbox() as d:
                        if not jp.exists() or jp.read_bytes() != snapshot:
                            continue
                        if not allowed:
                            jp.unlink(missing_ok=True)
                            continue
                        job["attempts"] = 0
                        job.pop("buried_at", None)
                        job.pop("hold_reason", None)
                        destination = _transition_destination(d / jp.name, job)
                        if not destination.exists():
                            _atomic_write(destination, job)
                        jp.unlink(missing_ok=True)
                        moved += 1
            except RegistryError as exc:
                _log(f"retry-dead refused for {jp.name}: {exc}")
                continue
    from khipu.session_capture import retry_held

    native_moved = retry_held()
    return {"moved": moved + native_moved, "native_moved": native_moved,
            "quarantined": quarantined, "pending": len(jobs())}


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
    offline = 0
    evidence_conflicts = 0
    line_conflicts = 0
    now = datetime.now(timezone.utc)
    for jp in js:
        try:
            job = json.loads(jp.read_bytes())
            if not isinstance(job, dict):
                raise ValueError("job envelope is not an object")
            outbox_payloads(job)
            q = job.get("queued_at")
            if not isinstance(q, str):
                raise ValueError("queued_at is not a string")
            age = int((now - datetime.fromisoformat(q)).total_seconds()) if q else None
        except (OSError, ValueError, TypeError, RecursionError):
            unreadable += 1
            continue
        attempts = job.get("attempts", 0)
        if job.get("evidence_conflict"):
            evidence_conflicts += 1
            line_conflicts += _is_line_conflict(job)
        if (not isinstance(job.get("payload"), dict) or isinstance(attempts, bool)
                or not isinstance(attempts, int) or attempts < 0):
            unreadable += 1
        if age is not None and (oldest_age is None or age > oldest_age):
            oldest_age, oldest_job = age, jp.name
        if unreachable_text(str(job.get("last_error") or job.get("reason") or "")):
            offline += 1
    quarantined = len(list(quarantine_dir().glob("*.json"))) if quarantine_dir().is_dir() else 0
    held_conflicts = []
    for jp in dead_jobs():
        try:
            held = _read_job(jp)
        except (OSError, ValueError, TypeError, RecursionError):
            continue  # dead still names unreadable held work
        if held.get("evidence_conflict"):
            held_conflicts.append(jp.name)
            line_conflicts += _is_line_conflict(held)
    evidence_conflicts += len(held_conflicts)
    resolved = len(list(resolved_dir().glob("*.json"))) if resolved_dir().is_dir() else 0
    # Existing doctor callers use ``dead`` to detect work that cannot replay.
    from khipu.session_capture import held_jobs

    native_held = [p.name for p in held_jobs()]
    dead = len(dead_jobs()) + quarantined + len(native_held)
    # Every waiting capture is waiting only because the hub could not be
    # reached: the promotion gate does not refuse a candidate on that.
    offline_only = (bool(js) and offline == len(js) and not unreadable
                    and not dead and not evidence_conflicts)
    result = {"dir": str(outbox_dir()), "pending": len(js), "oldest_age_s": oldest_age,
            "oldest_job": oldest_job, "unreadable": unreadable,
            "dead": dead, "quarantined": quarantined, "native_held": native_held,
            "max_attempts": MAX_ATTEMPTS, "offline": offline, "offline_only": offline_only,
            "evidence_conflicts": evidence_conflicts,
            "held_conflicts": len(held_conflicts), "held_conflict_jobs": held_conflicts,
            "line_conflicts": line_conflicts, "resolved": resolved}
    if line_conflicts:
        # Most of these lines differ from the hub only by preference text it
        # lacks (2026-10-27); retry-dead alone would just fail them again.
        result["fix"] = ("Run `khipu outbox merge-line-preferences` to count the held memory-file lines that "
                         "differ from the hub only by preferences it lacks; merge those with `--apply "
                         "--owner-direction` on the owner's direction. The rest need owner reconciliation.")
        if held_conflicts:
            result["fix"] += " Then run `khipu outbox retry-dead` so the merged lines resolve."
    elif evidence_conflicts:
        result["fix"] = ("Inspect the conflicting evidence in the local outbox and hub; "
                         "owner reconciliation is required before these jobs can replay.")
        if held_conflicts:
            # A buried job never re-enters the queue on its own.
            result["fix"] += " Then run `khipu outbox retry-dead` to replay the buried ones."
    return result


def _is_line_conflict(job: Any) -> bool:
    """A recovered memory-file line whose legacy key holds different facts
    (``khipu outbox merge-line-preferences`` reads these)."""
    return (_is_recovery_job(job) and job.get("reason") == "recovery: the legacy key holds different facts")


def _is_recovery_job(job: Any) -> bool:
    """A memory-file line a recovery writer queued with its owner unknown.
    Recovery reasons are "recovery: …" and "recovery fact step deferred: …"."""
    return (isinstance(job, dict) and "origin" in job and job["origin"] is None
            and str(job.get("reason") or "").startswith("recovery"))


def _retry_order(jp: Path) -> tuple[bool, bool, str, str]:
    """Real captures before file-recovery lines, so a failed capture never
    waits behind a reconcile's backlog. Within each, jobs never tried run
    first, in queue order; then failed jobs, oldest attempt first; deferred
    origin claims last, also oldest attempt first. A bounded drain therefore
    rotates through every retry instead of spending its budget on the same
    failures in filename order."""
    try:
        job = json.loads(jp.read_text())
        return (_is_recovery_job(job), bool(job.get("claim_deferred")),
                str(job.get("last_attempt_at") or ""), jp.name)
    except (OSError, ValueError, AttributeError):
        return (False, False, "", jp.name)


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
        js = sorted(jobs(), key=_retry_order)
        if limit is not None:
            js = js[:limit]
        out["jobs"] = len(js)
        from khipu import memory_lock
        from khipu.capture import CaptureEvidenceConflict, ClaimDeferred, FactStepDeferred, write_pg
        from khipu.embed import embed_on_capture
        from khipu.request_context import replaying
        from khipu.tombstones import RegistryError, write_guard

        for jp in js:
            # One memory-lock hold per job, from its read through the hub write,
            # its vectors and the file's removal; a forget waits for at most one job.
            with memory_lock.shared():
                if deadline is not None and time.monotonic() >= deadline:
                    out["time_budget_exhausted"] = True
                    break
                payload: dict[str, Any] | None = None
                identifiable: list[dict[str, Any]] = []
                read_error: Exception | None = None
                snapshot = b""
                with _locked_outbox():
                    if not jp.exists():
                        continue
                    try:
                        snapshot = jp.read_bytes()
                        job = _read_job(jp)
                        payload = json.loads(json.dumps(job["payload"]))
                    except (OSError, ValueError, TypeError, RecursionError) as e:
                        read_error = e
                        # A malformed envelope can still expose an identifiable
                        # payload (for example a bad attempts field). Do not move
                        # such a forgotten source into quarantine after forget's
                        # scan: registry then outbox is the universal ordering.
                        identifiable, uncheckable = _quarantine_payloads(snapshot)
                        if not identifiable:
                            if uncheckable:
                                out["unreadable"] = out.get("unreadable", 0) + 1
                            else:
                                _quarantine(jp, read_error or ValueError("unreadable job"))
                                out["quarantined"] = out.get("quarantined", 0) + 1
                            continue
                if identifiable:
                    try:
                        with write_guard(*identifiable) as allowed:
                            with _locked_outbox():
                                if jp.exists() and jp.read_bytes() == snapshot:
                                    if not allowed:
                                        jp.unlink()
                                        out["forgotten"] = out.get("forgotten", 0) + 1
                                    elif not uncheckable:
                                        _quarantine(jp, read_error or ValueError("unreadable job"))
                                        out["quarantined"] = out.get("quarantined", 0) + 1
                                    else:
                                        out["unreadable"] = out.get("unreadable", 0) + 1
                    except RegistryError as registry_error:
                        out["registry_blocked"] = out.get("registry_blocked", 0) + 1
                        _log(f"quarantine refused for {jp.name}: {registry_error}")
                    continue
                if payload is None:
                    continue
                # The queued origin, not this drainer's context, owns the row. A job
                # queued before origins were recorded is stamped as before.
                owner = replaying(job["origin"]) if "origin" in job else nullcontext()
                try:
                    # The registry only fences local writes. Hub ``write_pg`` has
                    # its own tombstoned-source protection and may do slow I/O;
                    # holding the local flock here would make enqueue wait for a
                    # replay. The final retry/bury write below is fenced again.
                    with write_guard(*outbox_payloads(job)) as allowed:
                        if not allowed:
                            with _locked_outbox():
                                try:
                                    if jp.read_bytes() == snapshot:
                                        jp.unlink()
                                except FileNotFoundError:
                                    pass
                            out["forgotten"] = out.get("forgotten", 0) + 1
                            continue
                    with owner:
                        if (job.get("reason") == "recovery: the legacy key holds different facts"
                                and "origin" in job and job["origin"] is None):
                            stats = write_pg(payload, legacy_projection=True)
                        else:
                            stats = write_pg(payload)
                except RegistryError as e:
                    # A corrupt or unavailable registry is a privacy failure, not
                    # a retryable reason to overwrite a queued job.
                    out["failed"] += 1
                    out["registry_blocked"] = out.get("registry_blocked", 0) + 1
                    _log(f"replay refused for {jp.name}: {e}")
                    continue
                except Exception as e:  # noqa: BLE001 — keep the job; note the attempt
                    out["failed"] += 1
                    job["attempts"] += 1
                    job["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                    job["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
                    name = type(e).__name__
                    unreachable = unreachable_error(e)
                    # A deferred origin claim is transient and its job holds the
                    # only record of the owner: retried, never buried.
                    job["claim_deferred"] = isinstance(e, ClaimDeferred)
                    job["evidence_conflict"] = isinstance(e, CaptureEvidenceConflict)
                    # A fact step that met a lock or timeout error, or the
                    # hub's fact gate: the transaction rolled back whole and
                    # a retry lands it with its claims; never buried.
                    job["fact_deferred"] = isinstance(e, FactStepDeferred)
                    if job["fact_deferred"]:
                        job["deferral_reason"] = str(e)[:200]
                    else:
                        job.pop("deferral_reason", None)
                    deferred = job["claim_deferred"] or job["fact_deferred"]
                    if deferred or job["evidence_conflict"]:
                        unreachable = False
                        out["deferred"] = out.get("deferred", 0) + 1
                    try:
                        # Recheck under the same registry lock at the actual
                        # retry/bury write boundary; otherwise a forget between a
                        # failed replay and this atomic replace could resurrect it.
                        with write_guard(*outbox_payloads(job)) as allowed:
                            with _locked_outbox():
                                try:
                                    if jp.read_bytes() == snapshot:
                                        if not allowed:
                                            jp.unlink()
                                            out["forgotten"] = out.get("forgotten", 0) + 1
                                        elif not (unreachable or deferred) and job["attempts"] >= MAX_ATTEMPTS:
                                            _bury(jp, job)
                                            out["buried"] = out.get("buried", 0) + 1
                                        else:
                                            _atomic_write(jp, job)
                                except OSError:
                                    pass
                    except RegistryError as registry_error:
                        out["registry_blocked"] = out.get("registry_blocked", 0) + 1
                        _log(f"retry state refused for {jp.name}: {registry_error}")
                    if unreachable:
                        out["stopped_early"] = True
                        _log(f"PG unreachable for {jp.name} ({name}); leaving remaining jobs queued")
                        break
                    _log(f"replay failed for {jp.name}: {name}: {e}")
                    continue
                proof = stats.get("legacy_projection_duplicate")
                if proof:
                    original = proof["payload"]
                    resolution = {
                        "kind": proof.get("kind", "legacy_comma_projection"),
                        "episode_id": proof["episode_id"],
                        "original_payload_sha256": proof["original_payload_sha256"],
                        "queued_payload_sha256": proof["queued_payload_sha256"],
                    }
                    # Immutable evidence and filename survive retry bookkeeping
                    # changes, including a crash after archive publication.
                    candidate = {
                        "payload": original, "queued_evidence": json.loads(snapshot),
                        "resolution": {**resolution, "resolved_at": datetime.now(timezone.utc).isoformat()},
                    }
                    record = job.get("resolution_evidence", candidate)
                    record_id = job.get("resolution_record_sha256", hashlib.sha256(snapshot).hexdigest())
                    try:
                        _validate_resolution(record)
                        if (not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{64}", record_id)
                                or _canonical(record["payload"]) != _canonical(original)
                                or not _same_evidence(record["queued_evidence"], job["payload"],
                                                      {k: job[k] for k in ("origin",) if k in job})
                                or any(_canonical(record["resolution"].get(k)) != _canonical(v)
                                       for k, v in resolution.items())):
                            raise ValueError("pending resolution conflicts with current proof")
                        # Both source identities are fenced through publication,
                        # queue removal, and any subsequent failure-state write.
                        with write_guard(original, *outbox_payloads(job)) as allowed:
                            with _locked_outbox():
                                if not jp.exists() or jp.read_bytes() != snapshot:
                                    continue
                                if not allowed:
                                    jp.unlink()
                                    out["forgotten"] = out.get("forgotten", 0) + 1
                                    continue
                                resolved_dir().mkdir(parents=True, exist_ok=True)
                                record_path = resolved_dir() / (record_id + ".json")
                                if not record_path.exists():
                                    _atomic_write(record_path, record)
                                else:
                                    saved = json.loads(record_path.read_bytes())
                                    _validate_resolution(saved)
                                    # The archived timestamp may predate this
                                    # attempt; every evidence and proof field
                                    # must otherwise match canonical JSON.
                                    expected = {**record, "resolution": {
                                        **record["resolution"],
                                        "resolved_at": saved["resolution"]["resolved_at"],
                                    }}
                                    if _canonical(saved) != _canonical(expected):
                                        raise ValueError("resolution record conflicts with queued evidence")
                                jp.unlink()
                        out["resolved"] = out.get("resolved", 0) + 1
                    except RegistryError as exc:
                        out["failed"] += 1
                        out["registry_blocked"] = out.get("registry_blocked", 0) + 1
                        _log(f"resolution refused for {jp.name}: {exc}")
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        out["failed"] += 1
                        out["resolution_failed"] = out.get("resolution_failed", 0) + 1
                        job["attempts"] += 1
                        job["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                        job["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
                        job["evidence_conflict"] = True
                        job["resolution_failure"] = True
                        job.setdefault("resolution_evidence", record)
                        job.setdefault("resolution_record_sha256", record_id)
                        try:
                            with write_guard(original, *outbox_payloads(job)) as allowed:
                                with _locked_outbox():
                                    if not jp.exists() or jp.read_bytes() != snapshot:
                                        continue
                                    if not allowed:
                                        jp.unlink()
                                        out["forgotten"] = out.get("forgotten", 0) + 1
                                    elif job["attempts"] >= MAX_ATTEMPTS:
                                        _bury(jp, job)
                                        out["buried"] = out.get("buried", 0) + 1
                                    else:
                                        _atomic_write(jp, job)
                        except RegistryError as registry_error:
                            out["registry_blocked"] = out.get("registry_blocked", 0) + 1
                            _log(f"resolution retry state refused for {jp.name}: {registry_error}")
                        except (OSError, ValueError, TypeError) as state_error:
                            out["retry_state_failed"] = out.get("retry_state_failed", 0) + 1
                            _log(f"resolution retry state could not be saved for {jp.name}: "
                                 f"{type(state_error).__name__}")
                        _log(f"resolution retained for {jp.name}: {type(exc).__name__}")
                    continue
                if stats.get("episode_inserted"):
                    try:
                        embed_on_capture(payload, episode_id=stats.get("episode_id"))
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


if __name__ == "__main__":  # `python -m khipu.outbox [drain|status]`
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    print(json.dumps(drain() if cmd == "drain" else status(), indent=2))
    sys.exit(0)
