"""Structural local write barriers for a completed forget."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from khipu import capture, outbox, tombstones
from tests import test_forget_ownership as fo


def _payload() -> dict:
    return {"ts": "2026-10-15T12:00:00Z", "summary": "durable-forget sentinel",
            "harness": "codex", "session_id": "codex:durable-forget",
            "transcript_range": "0:10"}


def _forget(payload: dict, root: Path) -> dict:
    world = {"episodes": {1: {"id": 1, "ts": fo.BASE, "summary": payload["summary"],
                               "session_id": payload["session_id"], "raw": dict(payload), "deleted": False}},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, payload)], "sources_enabled": True}
    fo.write_lines(root, [payload])
    (root / "replica").mkdir(exist_ok=True)
    with mock.patch("khipu.hub_snapshot.snapshot_path", return_value=root / "replica/hub_snapshot.sqlite"):
        return fo.run_forget(world, 1, root)


def _publish_at_last_check(monkeypatch, payload: dict, root: Path):
    """Pause immediately before a writer's final guard, then run real forget."""
    original = tombstones.write_guard
    fired = {"value": False}

    @contextmanager
    def scheduled(value):
        if not fired["value"]:
            fired["value"] = True
            result = _forget(payload, root)
            assert result["complete"], result
        with original(value) as allowed:
            yield allowed

    monkeypatch.setattr(tombstones, "write_guard", scheduled)
    return fired


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "state" / "forgotten_sources.json"
    monkeypatch.setattr(tombstones, "registry_path", lambda: path)
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    return path


def test_registry_is_atomic_fsynced_and_rejects_canonical_and_redacted_forms(registry):
    payload = _payload() | {"summary": "key sk-ant-" + "A" * 40}
    with mock.patch("khipu.tombstones.os.fsync", wraps=os.fsync) as fsync, \
            mock.patch("khipu.tombstones.os.replace", wraps=os.replace) as replace:
        tombstones.publish_payloads([payload])
    raw = json.loads(registry.read_text())
    assert raw["version"] == 1
    assert tombstones.is_forgotten(payload)
    assert tombstones.is_forgotten(tombstones._redacted_form(payload))
    assert (registry.parent / ".forgotten_sources.lock").exists()
    assert replace.call_count == 1
    assert fsync.call_count >= 2  # payload file, then containing directory


def test_corrupt_registry_fails_closed_for_each_local_writer(registry, tmp_path):
    payload = _payload()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("{broken", encoding="utf-8")
    with pytest.raises(tombstones.RegistryError):
        outbox.enqueue(payload)
    child = tmp_path / "capture_v2.py"
    child.write_text("raise SystemExit(0)\n")
    with mock.patch.object(capture, "_capture_v2", return_value=child):
        assert capture.run_capture_v2(payload, suppress_mirror=True) == capture.EX_SOFTWARE


@pytest.mark.parametrize("suppress_mirror", [False, True])
def test_forget_interleaves_before_legacy_and_mirror_last_write(registry, tmp_path, monkeypatch,
                                                                 suppress_mirror):
    payload = _payload()
    child = tmp_path / "capture_v2.py"
    dest = tmp_path / "episodes.jsonl"
    child.write_text("import json,sys,pathlib\npathlib.Path(" + repr(str(dest)) +
                     ").open('a').write(json.dumps(json.load(sys.stdin)) + '\\n')\n")
    fired = _publish_at_last_check(monkeypatch, payload, tmp_path)
    with mock.patch.object(capture, "_capture_v2", return_value=child):
        assert capture.run_capture_v2(payload, suppress_mirror=suppress_mirror) == 0
    assert fired["value"]
    assert payload["summary"] not in (dest.read_text(encoding="utf-8") if dest.exists() else "")


def test_forget_interleaves_before_outbox_enqueue_and_drain_paths(registry, tmp_path, monkeypatch):
    payload = _payload()
    fired = _publish_at_last_check(monkeypatch, payload, tmp_path)
    with pytest.raises(tombstones.ForgottenSource):
        outbox.enqueue(payload)
    assert fired["value"]
    d = outbox.outbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    job = d / "late.json"
    job.write_text(json.dumps({"payload": payload, "attempts": 0}), encoding="utf-8")
    out = outbox.drain()
    assert out["forgotten"] == 1
    assert not job.exists()
    # Bad attempts make this recognizable source take the quarantine path;
    # it is removed instead of moved after the same last-boundary check.
    bad = d / "bad.json"
    bad.write_text(json.dumps({"payload": payload, "attempts": -1}), encoding="utf-8")
    outbox.drain()
    assert not bad.exists()


def _forget_waits_in_thread(payload: dict, root: Path) -> dict:
    """Start a real forget in another thread at a writer's hook point. The
    memory lock holds it off while the writer is mid-write: assert it is
    still blocked, and return a handle to join once the writer has returned."""
    box: dict = {}

    def run():
        box["result"] = _forget(payload, root)

    box["thread"] = threading.Thread(target=run)
    box["thread"].start()
    box["thread"].join(0.3)
    assert box["thread"].is_alive(), "forget ran inside a local write"
    return box


def _finish_forget(box: dict) -> None:
    box["thread"].join(10)
    assert not box["thread"].is_alive()
    assert box["result"]["complete"], box["result"]


def test_forget_interleaves_at_retry_and_bury_write_boundaries(registry, tmp_path, monkeypatch):
    """The interleaving is now excluded by the memory lock: the forget starts
    at the retry write boundary in another thread and waits for the drain,
    then removes the buried copy. Pins that the forget waits."""
    payload = _payload()
    d = outbox.outbox_dir()
    d.mkdir(parents=True)
    job = d / "retry.json"
    job.write_text(json.dumps({"payload": payload, "attempts": outbox.MAX_ATTEMPTS - 1}), encoding="utf-8")
    original = tombstones.write_guard
    calls = {"count": 0}

    @contextmanager
    def pause_at_retry(value):
        calls["count"] += 1
        if calls["count"] == 2:
            calls["forget"] = _forget_waits_in_thread(payload, tmp_path)
        with original(value) as allowed:
            yield allowed

    monkeypatch.setattr(tombstones, "write_guard", pause_at_retry)
    with mock.patch("khipu.capture.write_pg", side_effect=RuntimeError("retry me")):
        outbox.drain()
    assert calls["count"] == 2
    _finish_forget(calls["forget"])
    assert not job.exists()
    assert not (d / "dead" / job.name).exists()


def test_forget_interleaves_before_retry_dead_requeue(registry, tmp_path, monkeypatch):
    """The interleaving is now excluded by the memory lock: the forget starts
    before the requeue's guard in another thread and waits for retry_dead to
    return, then removes the dead job. Pins that the forget waits."""
    payload = _payload()
    dead = outbox.dead_dir()
    dead.mkdir(parents=True)
    job = dead / "late.json"
    job.write_text(json.dumps({"payload": payload, "attempts": outbox.MAX_ATTEMPTS}), encoding="utf-8")
    original = tombstones.write_guard
    started = {}

    @contextmanager
    def start_forget_once(value):
        if not started:
            started["forget"] = _forget_waits_in_thread(payload, tmp_path)
        with original(value) as allowed:
            yield allowed

    monkeypatch.setattr(tombstones, "write_guard", start_forget_once)
    outbox.retry_dead()
    assert started
    _finish_forget(started["forget"])
    assert not job.exists()
    assert not (outbox.outbox_dir() / job.name).exists()


def test_forget_interleaves_before_replica_write_through(registry, tmp_path, monkeypatch):
    from khipu import hub_snapshot

    payload = _payload()
    _publish_at_last_check(monkeypatch, payload, tmp_path)
    replica = tmp_path / "hub_snapshot.sqlite"
    monkeypatch.setattr(hub_snapshot, "snapshot_path", lambda: replica)
    out = hub_snapshot.upsert_episode({"id": 7, **payload, "raw": payload}, [])
    assert out["ok"] is False
    assert out["error"] == "forgotten source"


def test_forget_publish_failure_never_reports_complete(registry, monkeypatch):
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("{broken", encoding="utf-8")
    world = {"episodes": {1: {"id": 1, "ts": fo.BASE, "summary": _payload()["summary"],
                               "session_id": "codex:durable-forget", "raw": _payload(), "deleted": False}},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, _payload())],
             "sources_enabled": True}
    with mock.patch("khipu.hub_snapshot.snapshot_path", return_value=registry.parent / "snapshot.sqlite"):
        out = fo.run_forget(world, 1, registry.parent)
    assert not out["complete"]
    assert "tombstone_registry" in out["partial"]
    assert world["episodes"][1]["deleted"] is True
    assert (registry.parent / "episodes.jsonl").exists() is False


def test_exact_source_fence_allows_a_distinct_source_with_the_same_legacy_alias(registry):
    from khipu.capture_identity import source_identity

    payload = _payload()
    source = source_identity(payload)
    alias = {"ts": payload["ts"], "summary_md5": hashlib.md5(
        payload["summary"].encode("utf-8")).hexdigest()}
    tombstones.publish_forget([source], [payload], [alias])
    assert tombstones.is_forgotten(payload)
    assert tombstones.is_forgotten(payload | {"summary": "rewritten forgotten source"})
    assert not tombstones.is_forgotten(payload | {"session_id": "codex:unrelated-source"})


def test_hash_only_fence_uses_the_strict_wire_form_for_lone_surrogates(registry):
    from khipu.wire import clean

    payload = {"ts": "2026-10-15T12:00:00Z", "summary": "strict\udc80-wire"}
    tombstones.publish_payloads([payload])
    assert tombstones.is_forgotten(payload)
    assert tombstones.is_forgotten(clean(payload))


def test_replica_fence_preserves_payload_only_hash_fields(registry):
    from khipu import hub_snapshot

    payload = {"ts": "2026-10-15T12:00:00Z", "summary": "hash-only replica",
               "verbatim": {"quotes": ["retained evidence"]}}
    tombstones.publish_payloads([payload])
    out = hub_snapshot.upsert_episode({"id": 7, **payload}, [])
    assert out == {"ok": False, "error": "forgotten source"}
