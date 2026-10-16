"""The legacy child shares its parent's forgotten-source write fence."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from khipu import capture, outbox, tombstones


def _payload() -> dict:
    return {"ts": "2026-10-15T12:00:00Z", "summary": "subprocess fence sentinel",
            "harness": "codex", "session_id": "codex:subprocess", "transcript_range": "0:10"}


def _legacy_child(path: Path) -> None:
    path.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, os.environ['TEST_KHIPU_CLI'])\n"
        "from khipu import tombstones\n"
        "tombstones.registry_path = lambda: Path(os.environ['TEST_REGISTRY'])\n"
        "from khipu.outbox import enqueue\n"
        "enqueue(json.load(sys.stdin), reason='legacy child')\n",
        encoding="utf-8",
    )


def test_legacy_child_enqueues_without_deadlocking_parent_write_guard(tmp_path, monkeypatch):
    registry = tmp_path / "state" / "forgotten_sources.json"
    outbox_root = tmp_path / "outbox"
    child = tmp_path / "legacy_capture.py"
    _legacy_child(child)
    monkeypatch.setattr(tombstones, "registry_path", lambda: registry)
    monkeypatch.setenv("KHIPU_OUTBOX", str(outbox_root))
    monkeypatch.setenv("TEST_REGISTRY", str(registry))
    monkeypatch.setenv("TEST_KHIPU_CLI", str(Path(__file__).parents[1]))
    real_run_text = capture.run_text

    def short_child_timeout(*args, **kwargs):
        kwargs["timeout"] = 3
        return real_run_text(*args, **kwargs)

    with mock.patch.object(capture, "_capture_v2", return_value=child), \
            mock.patch.object(capture, "run_text", side_effect=short_child_timeout):
        assert capture.run_capture_v2(_payload(), suppress_mirror=False) == 0
    jobs = outbox.jobs()
    assert len(jobs) == 1
    assert json.loads(jobs[0].read_text())["payload"]["summary"] == _payload()["summary"]


def test_forgotten_source_never_launches_the_legacy_child(tmp_path, monkeypatch):
    registry = tmp_path / "state" / "forgotten_sources.json"
    outbox_root = tmp_path / "outbox"
    child = tmp_path / "legacy_capture.py"
    _legacy_child(child)
    payload = _payload()
    monkeypatch.setattr(tombstones, "registry_path", lambda: registry)
    monkeypatch.setenv("KHIPU_OUTBOX", str(outbox_root))
    tombstones.publish_payloads([payload])
    with mock.patch.object(capture, "_capture_v2", return_value=child), \
            mock.patch.object(capture, "run_text") as run_text:
        assert capture.run_capture_v2(payload, suppress_mirror=False) == 0
    run_text.assert_not_called()
    assert outbox.jobs() == []


def test_invalid_handoff_and_borrowed_scope_refuse_without_lock_bypass(tmp_path, monkeypatch):
    registry = tmp_path / "state" / "forgotten_sources.json"
    payload = _payload()
    monkeypatch.setattr(tombstones, "registry_path", lambda: registry)
    for name, value in {
        "KHIPU_TOMBSTONE_LOCK_FD": "not-an-fd",
        "KHIPU_TOMBSTONE_LOCK_DEV": "1",
        "KHIPU_TOMBSTONE_LOCK_INO": "1",
        "KHIPU_TOMBSTONE_REGISTRY": str(registry),
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(tombstones.RegistryError):
        tombstones.is_forgotten(payload)
    with pytest.raises(tombstones.RegistryError):
        tombstones.publish_payloads([payload])


def test_publication_is_refused_from_a_valid_borrowed_marker(tmp_path, monkeypatch):
    registry = tmp_path / "state" / "forgotten_sources.json"
    payload = _payload()
    monkeypatch.setattr(tombstones, "registry_path", lambda: registry)
    with tombstones.write_guard(payload):
        markers, _fds = tombstones.subprocess_lock_handoff()
        for name, value in markers.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(tombstones.RegistryError):
            tombstones.publish_payloads([payload])
