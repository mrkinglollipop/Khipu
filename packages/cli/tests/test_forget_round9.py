"""Round 9: a killed capture's legacy writer group, and crash-left replica
temps in a backup.

A capture records its legacy writer's process group before the writer runs,
so a forget stops a group that outlived its capture (SIGKILL) before it reads
the legacy logs. A backup's temporary replica files never went through
staging and scrubbing, so an import leaves them out and says so.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from khipu import db, forget, hub_snapshot, outbox, paths, writer_groups
from tests.test_forget_round8 import _fake_legacy, _wait_file
from tests.test_forget_write_gate import P, _complete_forget

CLI = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()


def test_sigkill_capture_parent_cannot_leave_a_writer_after_complete(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()
    episodes = case / "memory" / "episodes.jsonl"
    files = _fake_legacy(tmp_path, episodes, exit_at_once=True)
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(CLI)!r})\n"
        "from pathlib import Path\n"
        "from khipu import capture\n"
        f"capture._capture_v2 = lambda: Path({str(files['legacy'])!r})\n"
        f"capture.run_capture_v2({P!r}, suppress_mirror=True)\n"
    )
    wrapper = subprocess.Popen([sys.executable, "-c", code],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pgid = None
    try:
        _wait_file(files["ready"])
        _wait_file(files["parent_pid"])
        pgid = int(files["parent_pid"].read_text())
        grandchild = int(files["grandchild_pid"].read_text())
        assert os.getpgid(grandchild) == pgid
        time.sleep(0.2)
        assert wrapper.poll() is None
        wrapper.kill()
        wrapper.communicate(timeout=5)
        os.kill(grandchild, 0)
        result = _complete_forget(case, monkeypatch)
        assert result["complete"]
        assert result["legacy_writers"]["stopped"] == 1
        assert not writer_groups.group_alive(pgid)
        assert list(writer_groups.records_dir().glob("*.json")) == []
        files["resume"].write_text("resume")
        time.sleep(0.5)
        assert P["summary"] not in episodes.read_text(), (
            "the legacy grandchild appended the forgotten source after forget completed")
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.communicate(timeout=5)
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_finished_capture_leaves_no_record(tmp_path):
    files = _fake_legacy(tmp_path, tmp_path / "episodes.jsonl", exit_at_once=True)
    files["resume"].write_text("resume")
    from khipu import capture

    capture._run_legacy_group([sys.executable, str(files["legacy"])],
                              input=json.dumps({"summary": "s"}), env=dict(os.environ),
                              timeout=20, pass_fds=())
    assert (tmp_path / "episodes.jsonl").read_text().strip()
    assert list(writer_groups.records_dir().glob("*.json")) == []


def test_unrecorded_group_never_runs_the_writer(tmp_path, monkeypatch):
    from khipu import capture

    marker = tmp_path / "ran"
    script = tmp_path / "writer.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")

    def refuse(pgid, started):
        raise OSError("disk full")

    monkeypatch.setattr(writer_groups, "record", refuse)
    with pytest.raises(OSError):
        capture._run_legacy_group([sys.executable, str(script)], input="{}",
                                  env=dict(os.environ), timeout=20, pass_fds=())
    assert not marker.exists()


def test_stale_records_are_cleared_without_signalling_anyone(tmp_path):
    directory = writer_groups.records_dir()
    directory.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    dead.wait()
    (directory / f"{dead.pid}.json").write_text(json.dumps({"pgid": dead.pid, "start": "x"}))
    # A live, unrelated group leader whose start time is not the recorded one.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)
    try:
        (directory / f"{other.pid}.json").write_text(
            json.dumps({"pgid": other.pid, "start": "Thu Jan  1 00:00:00 1970"}))
        result = writer_groups.sweep()
        assert result["ok"] and result["stopped"] == 0
        assert other.poll() is None, "an unrelated process was signalled"
        assert list(directory.glob("*.json")) == []
    finally:
        other.kill()
        other.wait()


def test_forget_stays_open_while_a_recorded_group_cannot_be_stopped(tmp_path, monkeypatch):
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True)
    try:
        writer_groups.record(live.pid, writer_groups.start_time(live.pid))
        monkeypatch.setattr(writer_groups, "_stop", lambda pgid: False)
        case = tmp_path / "case"
        case.mkdir()
        with pytest.raises(AssertionError):
            _complete_forget(case, monkeypatch)
        out = forget.plain_summary({"id": 1, "partial": ["legacy_writer"],
                                    "locations": writer_groups.sweep()["locations"]})
        assert "could not be stopped" in out
        assert (writer_groups.records_dir() / f"{live.pid}.json").exists()
    finally:
        live.kill()
        live.wait()


def _stale_replica(path: Path, summary: str) -> None:
    with sqlite3.connect(path) as con:
        hub_snapshot._create_schema(con)
        con.execute("INSERT INTO episodes (id, ts, summary, session_id, raw) VALUES (?, ?, ?, ?, ?)",
                    (1, "2026-10-01T09:00:00Z", summary, "codex:old", json.dumps({"summary": summary})))
        con.commit()


TEMP_NAMES = (".hub_snapshot.crash.sqlite", ".hub_snapshot.crash.sqlite-journal",
              ".hub_snapshot.crash.sqlite-wal", ".hub_snapshot.crash.sqlite-shm")


def test_backup_import_does_not_restore_forgotten_snapshot_temp(tmp_path, monkeypatch):
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "forgotten snapshot temp text",
               "session_id": "codex:old", "transcript_range": "0:10"}
    backup = tmp_path / "backup"
    backup.mkdir()
    _stale_replica(backup / TEMP_NAMES[0], payload["summary"])
    for name in TEMP_NAMES[1:]:
        (backup / name).write_bytes(b"pages")
    (backup / "notes.txt").write_text("kept")
    assert _complete_forget(tmp_path, monkeypatch, payload=payload)["complete"]

    imported = paths.import_local(source=backup)

    state = Path(os.environ["KHIPU_DATA_DIR"])
    assert imported["ok"] and imported["imported"] == 0
    assert sorted(imported["skipped_replica_temps"]) == sorted(TEMP_NAMES)
    assert "temporary replica files" in imported["notes"][0]
    # Round 11: a name no store claims is never restored either.
    assert not (state / "notes.txt").exists()
    assert "notes.txt" in {r["path"] for r in imported["not_restored"]}
    for name in TEMP_NAMES:
        assert not (state / name).exists(), name


def test_zip_import_does_not_restore_snapshot_temps(tmp_path):
    stale = tmp_path / "stale.sqlite"
    _stale_replica(stale, "forgotten zip temp text")
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(stale, TEMP_NAMES[0])
        for name in TEMP_NAMES[1:]:
            zf.writestr(name, b"pages")
        zf.writestr(".legacy_writers/123.json", json.dumps({"pgid": 123, "start": "x"}))
    imported = paths.import_local(source=archive)
    state = Path(os.environ["KHIPU_DATA_DIR"])
    assert imported["imported"] == 0
    assert sorted(imported["skipped_replica_temps"]) == sorted(TEMP_NAMES)
    assert not any((state / name).exists() for name in TEMP_NAMES)
    assert not (state / ".legacy_writers").exists()
