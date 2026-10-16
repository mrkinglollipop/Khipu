"""Durable local source tombstones used to fence late capture writers.

The registry is deliberately local state, independent of the legacy memory
root and an overridden outbox.  Every writer holds its exclusive flock from
the final identity check through its write; forget publishes under that same
flock before it scans any local copy.  A damaged registry is unsafe to reason
about, so both writers and forget fail closed.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class RegistryError(RuntimeError):
    """The local tombstone registry cannot be safely read or published."""


class ForgottenSource(RegistryError):
    """A writer attempted to persist a source that was already forgotten."""


_HANDOFF_ENV = ("KHIPU_TOMBSTONE_LOCK_FD", "KHIPU_TOMBSTONE_LOCK_DEV",
                "KHIPU_TOMBSTONE_LOCK_INO", "KHIPU_TOMBSTONE_REGISTRY")


@dataclass(frozen=True)
class _LockLease:
    fd: int
    registry: Path
    lock_path: Path
    borrowed: bool


_OWNED_WRITER_LOCK: ContextVar[_LockLease | None] = ContextVar(
    "khipu_tombstone_owned_writer_lock", default=None)


def registry_path() -> Path:
    """The one local-state location all writers resolve, regardless of outbox.

    ``data_dir`` already honours the isolated test/install state root, while
    ``KHIPU_OUTBOX`` and the configured memory root deliberately do not move
    this privacy boundary.
    """
    from khipu.paths import ensure_data_dir

    return ensure_data_dir() / "forgotten_sources.json"


def _redacted_form(payload: dict[str, Any]) -> dict[str, Any]:
    from khipu.capture import _redact_capture_values

    value = copy.deepcopy(payload)
    _redact_capture_values(value)
    return value


def _identity_values(payload: dict[str, Any]) -> tuple[set[str], set[str]]:
    from khipu.capture_identity import payload_sha256, source_key
    from khipu.wire import clean

    keys: set[str] = set()
    hashes: set[str] = set()
    # The strict wire form is what the legacy subprocess can actually persist.
    # Keep ordinary and redacted forms too, but a lone surrogate in an inbound
    # capture must not turn a valid strict-wire write into a registry outage.
    for value in (payload, _redacted_form(payload), clean(payload), clean(_redacted_form(payload))):
        try:
            keys.add(source_key(value))
            hashes.add(payload_sha256(value))
        except UnicodeEncodeError:
            continue
    if not keys or not hashes:
        raise RegistryError("tombstone payload has no UTF-8 identity")
    return keys, hashes


def _empty() -> dict[str, Any]:
    return {"version": 1, "source_keys": [], "payload_hashes": [], "legacy_identities": []}


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RegistryError(f"tombstone registry unreadable: {type(exc).__name__}") from exc
    if isinstance(value, dict) and value.get("version") == 1 and "legacy_identities" not in value:
        # Registry files written during the first rollout remain readable.
        value = {**value, "legacy_identities": []}
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("source_keys"), list)
            or not isinstance(value.get("payload_hashes"), list)
            or not isinstance(value.get("legacy_identities"), list)
            or not all(isinstance(item, str) for item in value["source_keys"])
            or not all(isinstance(item, str) for item in value["payload_hashes"])
            or not all(isinstance(item, str) for item in value["legacy_identities"])):
        raise RegistryError("tombstone registry is corrupt")
    return value


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise RegistryError(f"tombstone registry publish failed: {type(exc).__name__}") from exc
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _marker_present() -> bool:
    return any(os.environ.get(name) for name in _HANDOFF_ENV)


def _borrowed_lease(path: Path, lock_path: Path) -> _LockLease | None:
    """Use only a validated inherited open-file-description, never a name."""
    if not _marker_present():
        return None
    values = [os.environ.get(name) for name in _HANDOFF_ENV]
    if not all(values):
        raise RegistryError("tombstone lock handoff is incomplete")
    try:
        fd, dev, ino = (int(values[i]) for i in range(3))
        inherited = os.fstat(fd)
        on_disk = os.stat(lock_path)
    except (OSError, ValueError) as exc:
        raise RegistryError("tombstone lock handoff is invalid") from exc
    if (values[3] != str(path.resolve()) or inherited.st_dev != dev or inherited.st_ino != ino
            or on_disk.st_dev != dev or on_disk.st_ino != ino):
        raise RegistryError("tombstone lock handoff does not match this registry")
    try:
        # Same inherited open-file-description is reentrant; a different FD
        # would block, so nonblocking turns any forgery/staleness into refusal.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        raise RegistryError("tombstone lock handoff is not reentrant") from exc
    return _LockLease(fd, path, lock_path, borrowed=True)


@contextmanager
def _locked_registry() -> Iterator[tuple[Path, dict[str, Any], _LockLease]]:
    fd: int | None = None
    lease: _LockLease | None = None
    try:
        path = registry_path()
        # A missing registry is the normal empty state. Any failure creating
        # its state directory/lock is not equivalent to an empty registry.
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise RegistryError("tombstone registry must not be a symlink")
        lock_path = path.parent / f".{path.stem}.lock"
        lease = _borrowed_lease(path, lock_path)
        if lease is None:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            lease = _LockLease(fd, path, lock_path, borrowed=False)
        yield path, _read(path), lease
    except RegistryError:
        raise
    except OSError as exc:
        raise RegistryError(f"tombstone registry unavailable: {type(exc).__name__}") from exc
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _known(value: dict[str, Any], payload: dict[str, Any]) -> bool:
    try:
        keys, hashes = _identity_values(payload)
    except Exception as exc:  # noqa: BLE001 - identity uncertainty must block writes
        raise RegistryError(f"tombstone identity failed: {type(exc).__name__}") from exc
    from khipu.wire import clean_text

    ts, summary = payload.get("ts"), payload.get("summary")
    legacy = (f"{clean_text(ts)}\x1f{hashlib.md5(clean_text(summary).encode('utf-8')).hexdigest()}"
              if isinstance(ts, str) and isinstance(summary, str) else None)
    return bool(keys & set(value["source_keys"]) or hashes & set(value["payload_hashes"])
                or legacy in set(value["legacy_identities"]))


@contextmanager
def write_guard(payload: dict[str, Any]) -> Iterator[bool]:
    """Serialize the last source check and the caller's local write.

    Callers must keep this context open until their atomic replace/SQLite
    commit completes.  ``False`` means the payload is a forgotten source.
    ``RegistryError`` means no safe decision could be made and must abort the
    write rather than treating an unreadable registry as empty.
    """
    with _locked_registry() as (_path, value, lease):
        token = _OWNED_WRITER_LOCK.set(lease) if not lease.borrowed else None
        try:
            yield not _known(value, payload)
        finally:
            if token is not None:
                _OWNED_WRITER_LOCK.reset(token)


def is_forgotten(payload: dict[str, Any]) -> bool:
    with _locked_registry() as (_path, value, _lease):
        return _known(value, payload)


def subprocess_lock_handoff() -> tuple[dict[str, str], tuple[int, ...]]:
    """Environment and FD inheritance for a legacy child inside write_guard."""
    lease = _OWNED_WRITER_LOCK.get()
    if lease is None:
        return {}, ()
    stat = os.fstat(lease.fd)
    return ({"KHIPU_TOMBSTONE_LOCK_FD": str(lease.fd),
             "KHIPU_TOMBSTONE_LOCK_DEV": str(stat.st_dev),
             "KHIPU_TOMBSTONE_LOCK_INO": str(stat.st_ino),
             "KHIPU_TOMBSTONE_REGISTRY": str(lease.registry.resolve())}, (lease.fd,))


def publish_payloads(payloads: list[dict[str, Any]]) -> None:
    """Atomically add canonical and redacted identities before local erasure."""
    keys: set[str] = set()
    hashes: set[str] = set()
    try:
        for payload in payloads:
            k, h = _identity_values(payload)
            keys.update(k)
            hashes.update(h)
    except Exception as exc:  # noqa: BLE001
        raise RegistryError(f"tombstone identity failed: {type(exc).__name__}") from exc
    publish_identities(keys, hashes)


def publish_sources(source_rows: list[dict[str, Any]] | None) -> None:
    """Publish source identity snapshots returned by ``forget_episode``."""
    keys = {str(row["source_key"]) for row in source_rows or [] if row.get("source_key")}
    hashes = {str(row["payload_sha256"]) for row in source_rows or [] if row.get("payload_sha256")}
    if not keys and not hashes:
        raise RegistryError("forget has no durable source identity to publish")
    publish_identities(keys, hashes)


def publish_forget(source_rows: list[dict[str, Any]] | None,
                   payloads: list[dict[str, Any]],
                   legacy_identities: list[dict[str, Any]]) -> None:
    """Publish source snapshots plus pre-migration/raw payload forms together."""
    keys = {str(row["source_key"]) for row in source_rows or [] if row.get("source_key")}
    hashes = {str(row["payload_sha256"]) for row in source_rows or [] if row.get("payload_sha256")}
    try:
        for payload in payloads:
            found_keys, found_hashes = _identity_values(payload)
            keys.update(found_keys)
            hashes.update(found_hashes)
    except Exception as exc:  # noqa: BLE001
        raise RegistryError(f"tombstone identity failed: {type(exc).__name__}") from exc
    # Timestamp/summary aliases are only a fallback when historical rows have
    # no recoverable source or payload identity. With exact identities present,
    # an unrelated source may legitimately share the same legacy alias.
    legacy = ({f"{row['ts']}\x1f{row['summary_md5']}" for row in legacy_identities
               if isinstance(row.get("ts"), str) and isinstance(row.get("summary_md5"), str)}
              if not keys and not hashes else set())
    publish_identities(keys, hashes, legacy)


def publish_identities(keys: set[str], hashes: set[str], legacy: set[str] | None = None) -> None:
    legacy = legacy or set()
    if not keys and not hashes and not legacy:
        raise RegistryError("forget has no source identity to publish")
    # Publication from a borrowed child could let an outer writer that already
    # observed ``allowed=True`` survive a concurrent forget. Only the lock
    # owner may publish; marker presence is enough to refuse, even if forged.
    if _marker_present():
        raise RegistryError("tombstone publication refused from borrowed writer scope")
    with _locked_registry() as (path, current, _lease):
        next_value = {
            "version": 1,
            "source_keys": sorted(set(current["source_keys"]) | keys),
            "payload_hashes": sorted(set(current["payload_hashes"]) | hashes),
            "legacy_identities": sorted(set(current["legacy_identities"]) | legacy),
        }
        _atomic_write(path, next_value)
