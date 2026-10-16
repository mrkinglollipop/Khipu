"""Round 11: a backup import restores only what a store says it may, and an
unreadable legacy writer record keeps forget open.

Backup import used to copy every file and scrub only the ``.json`` ones, so a
crash-left outbox temp (``.job.json.<random>.tmp``) holding a forgotten
payload came back into the outbox. Import is now an allow-list driven by
khipu/capture_stores.py: each store says whether a backup may restore it and
which file names it owns, every restored file goes through that store's
scrub, and anything no store claims is left out and listed in the report.

A legacy writer record that could not be parsed used to be deleted and
reported clean, so forget trusted it while the writer it named could still
append. It now stays, and forget stays partial, until a sweep can read it or
the group its name gives is gone.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import zipfile

import psycopg
import pytest

from khipu import capture_stores, db, outbox, paths, tombstones, writer_groups
from tests.test_forget_write_gate import P, _complete_forget


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("live PG forbidden"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()


# ---------------------------------------------------------------------------
# import is an allow-list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["directory", "zip"])
def test_import_does_not_restore_forgotten_outbox_temp(tmp_path, monkeypatch, kind):
    source = outbox.outbox_dir() / ".job.json.deadbeef.tmp"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"payload": P, "attempts": 0}))
    backup = tmp_path / ("backup.zip" if kind == "zip" else "backup")
    if kind == "zip":
        paths.backup_local(dest=backup)
    else:
        shutil.copytree(paths.data_dir(), backup)
    assert _complete_forget(tmp_path, monkeypatch)["complete"]
    assert not source.exists(), "precondition: forget must remove the original temp"

    imported = paths.import_local(source=backup)

    assert imported["ok"]
    assert not source.exists(), "backup import restored a forgotten capture into the outbox temp"
    assert "outbox/.job.json.deadbeef.tmp" in {r["path"] for r in imported["not_restored"]}


@pytest.mark.parametrize("kind", ["directory", "zip"])
def test_an_unknown_file_is_never_restored(tmp_path, kind):
    names = ("notes.txt", "outbox/job.json.tmp", "outbox/dead/job.bak", "state/whatever.json",
             ".memory.lock", ".legacy_writers/42.json", "index-backups/p/MEMORY.md",
             "outbox/nested/deeper/job.json")
    backup = tmp_path / "backup"
    for name in names:
        (backup / name).parent.mkdir(parents=True, exist_ok=True)
        (backup / name).write_text(json.dumps({"payload": {"summary": "unclaimed text"}}))
    (backup / "config.json").write_text("{}")
    source = backup
    if kind == "zip":
        source = tmp_path / "backup.zip"
        with zipfile.ZipFile(source, "w") as zf:
            for p in backup.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(backup).as_posix())

    imported = paths.import_local(source=source)

    state = paths.data_dir()
    assert imported["imported"] == 1 and (state / "config.json").is_file()
    for name in names:
        # The import itself takes the memory lock, so that file exists; it
        # must not be the backup's copy.
        restored = state / name
        assert not restored.exists() or "unclaimed" not in restored.read_text(), name
    assert {r["path"] for r in imported["not_restored"]} == set(names)
    assert all(r["why"] for r in imported["not_restored"])


def test_every_restorable_store_has_a_scrub():
    entries = capture_stores.restorable()
    assert entries, "no store may be restored at all"
    for entry in entries:
        assert entry.patterns, entry.name
        assert entry.scrub in paths.RESTORE_SCRUBS, (
            f"{entry.name} may be restored but names no scrub paths knows ({entry.scrub!r})")
    assert set(paths.RESTORE_SCRUBS) == set(capture_stores.RESTORE_SCRUBS)
    # A store forget scans holds capture text, so its restore must be a real scrub.
    for store in capture_stores.STORES:
        if store.restore is not None and store.scanned:
            assert store.restore.scrub != "no capture text", store.name


def test_every_capture_store_says_whether_a_backup_restores_it():
    restored = {s.name for s in capture_stores.STORES if s.restore is not None}
    assert restored == {"outbox job", "replica", "conversation media", "recall golden set"}
    claimed = [e.name for e in capture_stores.restorable()]
    assert len(claimed) == len(set(claimed))


def test_a_forgotten_outbox_job_is_still_left_out_and_counted(tmp_path, monkeypatch):
    queued = outbox.outbox_dir() / "job.json"
    queued.parent.mkdir(parents=True)
    queued.write_text(json.dumps({"payload": P, "attempts": 0}))
    backup = tmp_path / "backup"
    shutil.copytree(paths.data_dir(), backup)
    assert _complete_forget(tmp_path, monkeypatch)["complete"]
    imported = paths.import_local(source=backup)
    assert imported["skipped_forgotten"] == 1 and not queued.exists()


# ---------------------------------------------------------------------------
# legacy writer records fail closed
# ---------------------------------------------------------------------------

def _writer_child(output):
    return subprocess.Popen(
        [sys.executable, "-c", ("import pathlib,sys,time; time.sleep(0.5); "
                                "pathlib.Path(sys.argv[1]).write_text('forgotten episode\\n')"),
         str(output)],
        start_new_session=True)


@pytest.mark.parametrize("content", ["{truncated", "", '{"pgid": "x"}', '{"start": "y"}', "[]"])
def test_unreadable_live_writer_record_keeps_forget_open(tmp_path, content):
    output = tmp_path / "episodes.jsonl"
    child = _writer_child(output)
    try:
        records = writer_groups.records_dir()
        records.mkdir(parents=True, exist_ok=True)
        record = records / f"{child.pid}.json"
        record.write_text(content, encoding="utf-8")

        result = writer_groups.sweep()

        assert result["ok"] is False and record.exists()
        (loc,) = result["locations"]
        assert loc["state"] == "not checked" and "could not be read" in loc["why"]
        child.wait(timeout=3)
        assert output.exists(), "the sweep stopped a group it could not confirm was the writer"
        # Once the group the record's name gives is gone, a later sweep clears it.
        again = writer_groups.sweep()
        assert again["ok"] is True and not record.exists()
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def test_a_record_whose_name_is_not_a_group_stays_until_the_owner_removes_it():
    records = writer_groups.records_dir()
    records.mkdir(parents=True, exist_ok=True)
    record = records / "not-a-group.json"
    record.write_text("{}", encoding="utf-8")
    for _ in range(2):
        result = writer_groups.sweep()
        assert result["ok"] is False and record.exists()
    record.unlink()
    assert writer_groups.sweep()["ok"] is True


def test_forget_with_an_unreadable_writer_record_is_partial_legacy_writer(tmp_path, monkeypatch):
    child = _writer_child(tmp_path / "out.txt")
    try:
        records = writer_groups.records_dir()
        records.mkdir(parents=True, exist_ok=True)
        (records / f"{child.pid}.json").write_text("{trunc", encoding="utf-8")
        from tests import test_forget_ownership as fo
        from khipu import forget

        root = tmp_path / "memory"
        root.mkdir()
        fo.write_lines(root, [P])
        world = {"episodes": {1: dict(P, id=1, ts=fo.BASE, raw=dict(P), deleted=False)},
                 "sources": [fo.ForgetOwnershipTest._source(None, 1, P)],
                 "sources_enabled": True}
        monkeypatch.setattr(db, "connect", lambda *a, **kw: fo._Conn(fo._WorldCur(world)))
        result = forget.forget_everywhere(1, memory_root=root)
        assert not result["complete"]
        assert "legacy_writer" in result["partial"]
        assert tombstones.is_forgotten(P)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)
