"""Round 12 of the forget review: two classes of hole, each closed
structurally.

Check then use: backup import read a file once to check it and again to
restore it, so a file replaced in between restored a forgotten payload.
Import now stages every candidate once and checks and restores only the
staged bytes.

Fail closed: a listing that hid a permission error (glob), a backup job that
could not be parsed, and a native job with no identity were each read as
clean. Every forget scan now lists with scandir, which raises, and anything
that cannot be read or identified keeps the forget open or is left out of
an import and listed.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import zipfile
from pathlib import Path

import psycopg
import pytest

from khipu import (capture_stores, db, forget, hub_snapshot, outbox, owner_turns, paths, recall_eval, tombstones,
                   work)
from khipu import session_capture as sc
from khipu import writer_groups as wg
from tests import test_forget_ownership as fo
from tests.test_forget_write_gate import P, Q


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
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("live PG forbidden"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()


def _forget(tmp_path, monkeypatch, payload=P):
    """Forget episode 1, whose one source is ``payload``; no completeness
    assumed."""
    root = tmp_path / "memory"
    root.mkdir(exist_ok=True)
    fo.write_lines(root, [payload])
    world = {"episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, payload)],
             "sources_enabled": True}
    monkeypatch.setattr(db, "connect", lambda *a, **kw: fo._Conn(fo._WorldCur(world)))
    return forget.forget_everywhere(1, memory_root=root)


def _complete(tmp_path, monkeypatch, payload=P):
    result = _forget(tmp_path, monkeypatch, payload)
    assert result["complete"], result
    assert tombstones.is_forgotten(payload)
    return result


def _job(payload) -> str:
    return json.dumps({"payload": payload, "attempts": 0})


# --- check then use: import stages once ------------------------------------


def test_swapping_a_backup_file_after_staging_cannot_change_what_is_restored(tmp_path, monkeypatch):
    source = tmp_path / "backup" / "outbox" / "job.json"
    source.parent.mkdir(parents=True)
    source.write_text(_job(Q), encoding="utf-8")
    _complete(tmp_path, monkeypatch)
    real_write = paths._write_staged

    def swap_after_staging(reader, staged):
        real_write(reader, staged)
        source.write_text(_job(P), encoding="utf-8")

    monkeypatch.setattr(paths, "_write_staged", swap_after_staging)
    out = paths.import_local(source=tmp_path / "backup")

    restored = paths.data_dir() / "outbox" / "job.json"
    assert out["imported"] == 1, out
    assert json.loads(restored.read_text(encoding="utf-8"))["payload"] == Q


def test_swapping_a_backup_file_after_its_check_cannot_change_what_is_restored(tmp_path, monkeypatch):
    # Ported from the round-12 probe: the swap lands between check and restore.
    source = tmp_path / "backup" / "outbox" / "job.json"
    source.parent.mkdir(parents=True)
    source.write_text(_job(Q), encoding="utf-8")
    _complete(tmp_path, monkeypatch)
    real_check = paths._job_refusal

    def swap_after_check(data, gate):
        refusal = real_check(data, gate)
        assert refusal is None
        source.write_text(_job(P), encoding="utf-8")
        return refusal

    monkeypatch.setattr(paths, "_job_refusal", swap_after_check)
    paths.import_local(source=tmp_path / "backup")

    restored = paths.data_dir() / "outbox" / "job.json"
    assert json.loads(restored.read_text(encoding="utf-8"))["payload"] == Q


def test_zip_members_are_restored_from_their_staged_bytes(tmp_path, monkeypatch):
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("outbox/job.json", _job(Q))
    _complete(tmp_path, monkeypatch)
    real_check = paths._job_refusal

    def rewrite_archive_after_check(data, gate):
        refusal = real_check(data, gate)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("outbox/job.json", _job(P))
        return refusal

    monkeypatch.setattr(paths, "_job_refusal", rewrite_archive_after_check)
    paths.import_local(source=archive)

    restored = paths.data_dir() / "outbox" / "job.json"
    assert json.loads(restored.read_text(encoding="utf-8"))["payload"] == Q


def test_a_linked_backup_file_is_never_followed(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(_job(P), encoding="utf-8")
    link = tmp_path / "backup" / "outbox" / "job.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(elsewhere)
    _complete(tmp_path, monkeypatch)

    out = paths.import_local(source=tmp_path / "backup")

    assert not (paths.data_dir() / "outbox" / "job.json").exists()
    assert "outbox/job.json" in {r["path"] for r in out["not_restored"]}


# --- fail closed: import ----------------------------------------------------


def test_import_leaves_out_and_lists_a_malformed_job_holding_forgotten_text(tmp_path, monkeypatch):
    # Ported probe: a job torn by a crash still holds the whole payload.
    backup_job = tmp_path / "backup" / "outbox" / "broken.json"
    backup_job.parent.mkdir(parents=True)
    backup_job.write_text(_job(P)[:-1], encoding="utf-8")
    _complete(tmp_path, monkeypatch)

    out = paths.import_local(source=tmp_path / "backup")

    assert not (paths.data_dir() / "outbox" / "broken.json").exists()
    assert "outbox/broken.json" in {r["path"] for r in out["not_restored"]}


@pytest.mark.parametrize("body", [json.dumps([P]), json.dumps({"payload": "text"}),
                                  json.dumps({"envelope": P}), "\udcff"])
def test_import_never_treats_an_unjudgeable_job_as_allowed(tmp_path, monkeypatch, body):
    backup_job = tmp_path / "backup" / "outbox" / "odd.json"
    backup_job.parent.mkdir(parents=True)
    backup_job.write_bytes(body.encode("utf-8", "surrogateescape"))
    _complete(tmp_path, monkeypatch)

    out = paths.import_local(source=tmp_path / "backup")

    assert not (paths.data_dir() / "outbox" / "odd.json").exists()
    assert "outbox/odd.json" in {r["path"] for r in out["not_restored"]}


def test_import_lists_an_unreadable_backup_folder(tmp_path, monkeypatch):
    folder = tmp_path / "backup" / "outbox"
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(_job(Q), encoding="utf-8")
    folder.chmod(0)
    try:
        try:
            os.listdir(folder)
        except PermissionError:
            pass
        else:
            pytest.skip("host can list a mode-000 folder")
        out = paths.import_local(source=tmp_path / "backup")
    finally:
        folder.chmod(0o700)
    assert "outbox" in {r["path"] for r in out["not_restored"]}


# --- fail closed: forget's scans ---------------------------------------------


def test_unreadable_writer_records_folder_keeps_forget_open(tmp_path, monkeypatch):
    # Ported probe: glob read a mode-000 records folder as empty.
    output = tmp_path / "late-write.jsonl"
    child = subprocess.Popen([sys.executable, "-c",
                              "import pathlib,sys,time; time.sleep(0.8); "
                              "pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                              str(output), P["summary"]], start_new_session=True)
    started = "Thu Oct 16 00:00:00 2026"
    monkeypatch.setattr(wg, "start_time", lambda pid: started)
    wg.record(child.pid, started)
    records = wg.records_dir()
    records.chmod(0)
    try:
        try:
            os.listdir(records)
        except PermissionError:
            pass
        else:
            pytest.skip("host can list a mode-000 folder")
        result = _forget(tmp_path, monkeypatch)
        assert not result["complete"], result["legacy_writers"]
        assert "legacy_writer" in result["partial"]
    finally:
        records.chmod(0o700)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def _queue(name: str, job) -> Path:
    path = sc.queue_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job), encoding="utf-8")
    return path


def test_identityless_job_whose_extracted_episode_is_the_source_is_removed(tmp_path, monkeypatch):
    # Ported probe: an initial-release job (offset_after only) with the
    # extracted forgotten payload is never left behind under a complete forget.
    job = _queue("historic.json", {"ts": P["ts"], "harness": "codex", "session_id": "old",
                                   "offset_after": 10, "transcript": "forgotten source transcript",
                                   "extracted_payload": dict(P)})
    assert sc.job_source_identity(json.loads(job.read_text())) is None
    result = _forget(tmp_path, monkeypatch)
    assert not job.exists()
    assert result["complete"], result["native_jobs"]


def test_identityless_job_from_the_same_session_keeps_forget_open(tmp_path, monkeypatch):
    job = _queue("damaged.json", {"ts": P["ts"], "harness": "codex", "session_id": "old",
                                  "transcript": "forgotten source transcript"})
    result = _forget(tmp_path, monkeypatch)
    assert job.exists()
    assert not result["complete"]
    assert "native_jobs" in result["partial"]


def test_identityless_job_with_no_session_keeps_forget_open(tmp_path, monkeypatch):
    job = _queue("nosession.json", {"ts": P["ts"], "transcript": "some transcript"})
    result = _forget(tmp_path, monkeypatch)
    assert job.exists()
    assert "native_jobs" in result["partial"]


def test_identityless_job_from_another_session_is_clean(tmp_path, monkeypatch):
    job = _queue("other.json", {"ts": P["ts"], "harness": "codex", "session_id": "elsewhere",
                                "transcript": "unrelated transcript"})
    result = _forget(tmp_path, monkeypatch)
    assert job.exists()
    assert result["complete"], result["native_jobs"]


def test_a_native_job_that_is_not_an_object_keeps_forget_open(tmp_path, monkeypatch):
    job = _queue("list.json", [P])
    result = _forget(tmp_path, monkeypatch)
    assert job.exists()
    assert "native_jobs" in result["partial"]


# One failure per forget scan the store inventory names. A scan added to
# capture_stores.FORGET_SCANS without an entry here fails the test below.
def _deny_listing(monkeypatch, folder):
    real = os.scandir

    def scandir(path="."):
        if not isinstance(path, int) and Path(path) == Path(folder()):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def _fail_replica(monkeypatch):
    def boom(_episode_id):
        raise OSError("replica unreadable")

    monkeypatch.setattr(hub_snapshot, "forget_episode_in_snapshot", boom)


INJECT = {
    "native jobs": lambda mp, tmp: _deny_listing(mp, sc.queue_dir),
    "outbox": lambda mp, tmp: _deny_listing(mp, outbox.outbox_dir),
    "legacy logs": lambda mp, tmp: _deny_listing(mp, lambda: tmp / "memory"),
    "replica": lambda mp, tmp: _fail_replica(mp),
    "replica temps": lambda mp, tmp: _deny_listing(mp, lambda: hub_snapshot.snapshot_path().parent),
    "legacy writers": lambda mp, tmp: _deny_listing(mp, wg.records_dir),
    # A folder where the recall test file should be cannot be read as one.
    "recall golden": lambda mp, tmp: recall_eval.default_golden_path().mkdir(parents=True),
    "work view": lambda mp, tmp: _deny_listing(mp, lambda: work.view_file().parent),
    "owner words": lambda mp, tmp: (owner_turns._ensure_folder(), _deny_listing(mp, owner_turns.folder)),
}


def test_every_forget_scan_has_a_failure_injection():
    assert set(INJECT) == set(capture_stores.FORGET_SCANS)
    assert {s.forget for s in capture_stores.STORES if s.scanned} <= set(INJECT)


def test_unreadable_routing_folder_keeps_forget_open(tmp_path, monkeypatch):
    _deny_listing(monkeypatch, sc.state_dir)
    result = _forget(tmp_path, monkeypatch)
    assert not result["complete"], result["locations"]
    assert "routing_words" in result["partial"]
    assert any(loc["place"] == "routing words" and loc["state"] == "unreadable"
               for loc in result["locations"])


def test_routing_words_on_the_gateway_are_not_applicable():
    result = forget._routing_words_scan(gateway=True)
    assert result["ok"]
    assert result["locations"][0]["state"] == "not applicable"


def test_plain_summary_does_not_promise_yet_for_kept_routing_words():
    out = forget.plain_summary({
        "id": 1, "complete": True, "partial": [],
        "locations": [{"place": "routing words", "path": "/tmp/recall--x.routing.json",
                       "state": "kept",
                       "why": forget.ROUTING_WORDS_KEPT_WHY}]})
    assert "not completely forgotten yet" not in out
    assert forget.ROUTING_WORDS_KEPT_WHY in out


def test_forget_removes_the_saved_work_view_and_its_temps(tmp_path, monkeypatch):
    (tmp_path / "memory").mkdir()
    view = work.view_file()
    view.parent.mkdir(parents=True)
    view.write_text(json.dumps({"waiting": [{"source": "commitment", "text": "quoted words"}]}))
    temp = view.with_name(f".{view.name}.123.tmp")
    temp.write_text("{}")
    other = view.with_name("links.json")
    other.write_text("{}")
    result = _forget(tmp_path, monkeypatch)
    assert not view.exists() and not temp.exists() and other.exists()
    assert {loc["path"] for loc in result["locations"]
            if loc["place"] == "work view" and loc["state"] == "removed"} == {str(view), str(temp)}


@pytest.mark.parametrize("store", [s for s in capture_stores.STORES if s.scanned],
                         ids=lambda s: s.name)
def test_a_failing_scan_of_any_store_never_reports_forget_complete(tmp_path, monkeypatch, store):
    (tmp_path / "memory").mkdir()
    for folder in (sc.queue_dir(), sc.empty_dir(), outbox.outbox_dir(), wg.records_dir()):
        folder.mkdir(parents=True, exist_ok=True)
    INJECT[store.forget](monkeypatch, tmp_path)
    result = _forget(tmp_path, monkeypatch)
    assert not result["complete"], (store.name, result.get("locations"))
    assert result["partial"], store.name
