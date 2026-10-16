"""Round 13 of the cross-family forget review: a top-level link in the state
folder, writer records of the wrong types, and a natural identity held only
in an episode's own columns. No PostgreSQL and no network."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

import psycopg
import pytest

from khipu import capture_identity as ci
from khipu import db, outbox, paths, tombstones, writer_groups
from tests import test_forget_ownership as fo
from tests.test_forget_write_gate import P, _complete_forget


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "state" / "outbox"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("live PG forbidden"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()


EMPTY_REGISTRY = json.dumps({"version": 1, "source_keys": [], "payload_hashes": [],
                             "legacy_identities": [], "episode_ids": []})


def _backup(tmp_path, archive: bool, name: str = "config.json"):
    source = tmp_path / ("backup.zip" if archive else "backup")
    if archive:
        with zipfile.ZipFile(source, "w") as z:
            z.writestr(name, EMPTY_REGISTRY)
    else:
        source.mkdir()
        (source / name).write_text(EMPTY_REGISTRY)
    return source


def _assert_still_forgotten():
    with pytest.raises(tombstones.ForgottenSource):
        outbox.enqueue(dict(P))


@pytest.mark.parametrize("archive", [False, True])
def test_a_top_level_link_to_the_registry_is_refused_and_the_registry_kept(tmp_path, monkeypatch, archive):
    _complete_forget(tmp_path, monkeypatch)
    registry = tombstones.registry_path()
    before = registry.read_bytes()
    (paths.data_dir() / "config.json").symlink_to(registry)
    with pytest.raises(paths.UnsafeDestination):
        paths.import_local(source=_backup(tmp_path, archive))
    assert registry.read_bytes() == before
    _assert_still_forgotten()


@pytest.mark.parametrize("archive", [False, True])
def test_a_top_level_hard_link_to_the_registry_is_refused(tmp_path, monkeypatch, archive):
    _complete_forget(tmp_path, monkeypatch)
    registry = tombstones.registry_path()
    before = registry.read_bytes()
    os.link(registry, paths.data_dir() / "config.json")
    with pytest.raises(paths.UnsafeDestination):
        paths.import_local(source=_backup(tmp_path, archive))
    assert registry.read_bytes() == before
    _assert_still_forgotten()


def test_a_top_level_folder_or_linked_folder_destination_is_refused(tmp_path, monkeypatch):
    _complete_forget(tmp_path, monkeypatch)
    (paths.data_dir() / "config.json").mkdir()
    with pytest.raises(paths.UnsafeDestination):
        paths.import_local(source=_backup(tmp_path, False))


def test_a_plain_destination_is_still_restored(tmp_path, monkeypatch):
    _complete_forget(tmp_path, monkeypatch)
    out = paths.import_local(source=_backup(tmp_path, True))
    assert out["imported"] == 1
    assert (paths.data_dir() / "config.json").read_text() == EMPTY_REGISTRY
    _assert_still_forgotten()


@pytest.mark.parametrize("damaged", [{"start": None}, {"start": 123}, {"start": ""},
                                     {"pgid": True}, {"pgid": "PID"}, {"extra": 1}])
def test_a_writer_record_of_the_wrong_shape_keeps_forget_open(tmp_path, monkeypatch, damaged):
    # ps is denied in the sandbox: model a successful start-time lookup. The
    # child's liveness and the late append use a real process group.
    monkeypatch.setattr(writer_groups, "start_time", lambda pid: "Fri Oct 16 01:00:00 2026")
    release = tmp_path / "release"
    output = tmp_path / "memory" / "episodes.jsonl"
    child = subprocess.Popen([
        sys.executable, "-c",
        "import pathlib,sys,time; release=pathlib.Path(sys.argv[1]); end=time.monotonic()+20\n"
        "while not release.exists() and time.monotonic()<end: time.sleep(.01)\n"
        "if release.exists():\n with open(sys.argv[2], 'a') as f: f.write(sys.argv[3]+'\\n')\n",
        str(release), str(output), json.dumps(P)], start_new_session=True)
    try:
        records = writer_groups.records_dir()
        records.mkdir(parents=True, exist_ok=True)
        body = {"pgid": child.pid, "start": "Fri Oct 16 01:00:00 2026", **damaged}
        if damaged.get("pgid") == "PID":
            body["pgid"] = str(child.pid)
        record = records / f"{child.pid}.json"
        record.write_text(json.dumps(body))
        sweep = writer_groups.sweep()
        assert sweep["ok"] is False
        assert record.exists()
        assert child.poll() is None
        assert any(loc["state"] == "not checked" for loc in sweep["locations"])
    finally:
        release.touch()
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


COLUMN_ONLY = {"ts": "2026-10-16T09:00:00Z", "summary": "column identity only",
               "harness": "codex", "session_id": "codex:column-only", "transcript_range": "0:10"}


def _world(where: str) -> dict:
    """One episode whose natural identity lives only in ``where``: its source
    rows, its raw, or its own columns."""
    p = COLUMN_ONLY
    ep = {"id": 1, "ts": datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc),
          "summary": p["summary"], "session_id": None, "deleted": False,
          "raw": {"ts": p["ts"], "summary": p["summary"]}}
    sources = []
    if where == "source rows":
        sources = [{"episode_id": 1, "role": "origin", "ts": fo.instant(p["ts"]),
                    "summary_md5": fo.md5(p["summary"]), "source_key": ci.source_key(p),
                    "payload_sha256": ci.payload_sha256(p)}]
    elif where == "raw":
        ep["raw"] = dict(p)
    elif where == "columns":
        ep.update(harness=p["harness"], session_id=p["session_id"],
                  transcript_range=p["transcript_range"])
    return {"episodes": {1: ep}, "sources": sources, "sources_enabled": True, "lines": []}


# Every place a natural identity can live. A new place must be added here and
# to forget._read_forget_sources together.
IDENTITY_SOURCES = ("source rows", "raw", "columns")


@pytest.mark.parametrize("where", IDENTITY_SOURCES)
def test_forget_registers_a_natural_identity_from_every_place_it_lives(tmp_path, monkeypatch, where):
    root = tmp_path / "memory"
    root.mkdir()
    monkeypatch.setenv("KHIPU_OUTBOX", str(root / "outbox"))
    result = fo.run_forget(_world(where), 1, root)
    assert result["complete"], result
    replay = {**COLUMN_ONLY, "summary": "same natural source synthesized again"}
    with pytest.raises(tombstones.ForgottenSource):
        outbox.enqueue(replay, reason="post-forget replay")
    assert outbox.status()["pending"] == 0
