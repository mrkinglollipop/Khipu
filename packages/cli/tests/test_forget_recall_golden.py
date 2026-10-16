import json
from unittest import mock

from khipu import capture_identity as ci
from khipu import hub_snapshot as hs, paths
from tests import test_forget_ownership as fo


def test_forget_scrubs_recall_golden_lines_naming_the_episode(tmp_path, monkeypatch):
    payload = {
        "ts": "2026-10-01T09:00:00Z",
        "summary": "PRIVATE-FORGOTTEN-RECALL-SENTINEL",
        "harness": "codex",
        "session_id": "codex:A",
        "transcript_range": "0:10",
    }
    source = {
        "episode_id": 1,
        "role": "origin",
        "ts": fo.BASE,
        "summary_md5": fo.md5(payload["summary"]),
        "source_key": ci.source_key(payload),
        "payload_sha256": ci.payload_sha256(payload),
    }
    world = {
        "episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
        "sources": [source],
        "sources_enabled": True,
    }
    fo.write_lines(tmp_path, [payload])
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    golden = data / "recall-golden.jsonl"
    other = json.dumps({"query": "unrelated", "expect": ["2"]}) + "\n"
    golden.write_text("# owner note\n" + json.dumps({"query": payload["summary"], "expect": [1]})
                      + "\n" + other)

    with mock.patch.object(hs, "snapshot_path", return_value=tmp_path / "replica/hub_snapshot.sqlite"):
        result = fo.run_forget(world, 1, tmp_path)

    assert result["complete"], result
    assert result["recall_golden"]["removed"] == 1, result["recall_golden"]
    assert golden.read_text() == "# owner note\n" + other


def test_unreadable_recall_golden_keeps_forget_incomplete(tmp_path, monkeypatch):
    payload = {
        "ts": "2026-10-01T09:00:00Z",
        "summary": "PRIVATE-FORGOTTEN-RECALL-SENTINEL",
        "harness": "codex",
        "session_id": "codex:A",
        "transcript_range": "0:10",
    }
    source = {
        "episode_id": 1, "role": "origin", "ts": fo.BASE,
        "summary_md5": fo.md5(payload["summary"]),
        "source_key": ci.source_key(payload),
        "payload_sha256": ci.payload_sha256(payload),
    }
    world = {
        "episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
        "sources": [source], "sources_enabled": True,
    }
    fo.write_lines(tmp_path, [payload])
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    golden = data / "recall-golden.jsonl"
    text = json.dumps({"query": payload["summary"], "expect": [1]}) + "\nnot json\n"
    golden.write_text(text)

    with mock.patch.object(hs, "snapshot_path", return_value=tmp_path / "replica/hub_snapshot.sqlite"):
        result = fo.run_forget(world, 1, tmp_path)

    assert not result["complete"], result
    assert "recall_golden" in result["partial"], result["partial"]
    assert golden.read_text() == text


def test_backup_import_resurrects_forgotten_text_in_golden(tmp_path, monkeypatch):
    payload = {
        "ts": "2026-10-01T09:00:00Z",
        "summary": "PRIVATE-FORGOTTEN-IMPORT-SENTINEL",
        "harness": "codex",
        "session_id": "codex:A",
        "transcript_range": "0:10",
    }
    source = {
        "episode_id": 1, "role": "origin", "ts": fo.BASE,
        "summary_md5": fo.md5(payload["summary"]),
        "source_key": ci.source_key(payload),
        "payload_sha256": ci.payload_sha256(payload),
    }
    world = {
        "episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
        "sources": [source], "sources_enabled": True,
    }
    fo.write_lines(tmp_path, [payload])
    data = tmp_path / "data"
    backup = tmp_path / "backup"
    data.mkdir()
    backup.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    backed = backup / "recall-golden.jsonl"
    backed.write_text(json.dumps({"query": payload["summary"], "expect": [1]}) + "\n")

    with mock.patch.object(hs, "snapshot_path", return_value=tmp_path / "replica/hub_snapshot.sqlite"):
        result = fo.run_forget(world, 1, tmp_path)
    assert result["complete"], result
    imported = paths.import_local(source=backup)

    assert imported["skipped_forgotten"] == 1, imported
    assert payload["summary"] not in (data / "recall-golden.jsonl").read_text()
