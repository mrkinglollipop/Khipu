# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""The fourth 7B fix round (astra-7b4 findings 1-3, and the benchmark's
timers): the review's probes, ported, each failing on 58d1e833. Synthetic
stores and hermetic Postgres only; the Keychain is stubbed in-process."""
from __future__ import annotations

import shutil
import socket
import sqlite3
import threading
import time

import psycopg
import pytest

from khipu import db, forget, hub_snapshot as hs, keychain
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import point, replica, shown, stamp  # noqa: F401
from tests.test_provenance_copy import _decision, _new_hub
from tests.test_provenance_forget import env, pg  # noqa: F401

_RESOLVE = db.resolve_dsn  # before the replica fixture pins it


def _keychain_without_dsn(monkeypatch) -> None:
    monkeypatch.setattr(keychain, "keychain_available", lambda: True)
    monkeypatch.setattr(keychain, "get_password", lambda *a, **kw: None)


def test_reader_file_endpoint_change_hides_old_facts(pg, replica, tmp_path, monkeypatch):
    replica(pg)
    _decision(pg, "OLD_FILE_ENDPOINT_FACT")
    assert hs.refresh()["facts"]["copied"]
    file = tmp_path / "dsn"
    file.write_text(pg.dsn)
    for name in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DSN_FILE", str(file))
    _keychain_without_dsn(monkeypatch)
    monkeypatch.setattr(db, "resolve_dsn", _RESOLVE)  # the real resolver: the file
    assert shown() > 0
    file.write_text(pg.dsn + "-replacement")
    assert hs._hub_destination() != stamp()["destination"]
    assert shown() == 0, "old facts visible after the file endpoint changed"


def test_delayed_copy_cannot_restore_stale_fingerprint(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, "OLD_FINGERPRINT_RACE")
    assert hs.refresh()["facts"]["copied"]
    _keychain_without_dsn(monkeypatch)
    original, resolved, release, results = hs._record_destination, threading.Event(), threading.Event(), []

    def pause(*args, **kw):
        if threading.current_thread().name == "old-endpoint-copy":
            resolved.set()
            assert release.wait(5)
        return original(*args, **kw)

    monkeypatch.setattr(hs, "_record_destination", pause)
    thread = threading.Thread(target=lambda: results.append(hs.copy_facts()), name="old-endpoint-copy")
    thread.start()
    try:
        assert resolved.wait(5)
        new_dsn = pg.dsn + "-replacement"
        monkeypatch.setattr(db, "resolve_dsn", lambda: new_dsn)
        monkeypatch.setattr(hs, "_saved_dsn", lambda **kw: new_dsn)
        # A damaged marker path: the fingerprint can be recorded, no marker made.
        (hs.snapshot_path().parent / hs._ENDPOINT_CHANGES_DIR).write_text("synthetic obstruction")
        hs.endpoint_changed()
        assert shown() == 0
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive(), results
    assert shown() == 0, "a delayed copy restored the older endpoint's fingerprint"


def test_endpoint_save_that_cannot_record_fails_closed(pg, replica, tmp_path, monkeypatch):
    replica(pg)
    _decision(pg, "UNRECORDED_ENDPOINT_FACT")
    (tmp_path / "dsn").write_text(pg.dsn)
    monkeypatch.setenv("KHIPU_DSN_FILE", str(tmp_path / "dsn"))
    assert hs.refresh()["facts"]["copied"]
    _keychain_without_dsn(monkeypatch)
    folder = hs.snapshot_path().parent
    assert shown() > 0
    new_dsn = pg.dsn + "-replacement"
    monkeypatch.setattr(hs, "_saved_dsn", lambda **kw: new_dsn)

    def denied(*a, **kw):
        raise PermissionError("synthetic fingerprint write denied")

    def busy(*a, **kw):
        raise hs._NotAdmitted("synthetic: the replica stayed busy")

    monkeypatch.setattr(hs, "_record_destination", denied)
    monkeypatch.setattr(hs, "_admitted", busy)
    (folder / hs._ENDPOINT_CHANGES_DIR).write_text("synthetic obstruction")
    hs.endpoint_changed()  # the old record is removed: no fact row shows
    assert shown() == 0, "an endpoint save that recorded nothing left the old endpoint's facts visible"
    # Nothing at all can be recorded: the save says so.
    (folder / hs._DESTINATION_FILE).mkdir()
    (folder / hs._DESTINATION_FILE / "occupant").write_text("x")
    with pytest.raises(hs.EndpointChangeUnrecorded):
        hs.endpoint_changed()


def test_failed_pull_lost_reread_still_hides_new_incarnation(pg, replica, pg_cluster, monkeypatch):
    replica(pg)
    _decision(pg, "OLD_INCARNATION_AFTER_REREAD_LOSS")
    assert hs.refresh()["facts"]["copied"]
    old, other, original = stamp(), _new_hub(pg_cluster), hs._one_exchange
    try:
        point(monkeypatch, other, dsn=pg.dsn)

        def bounded_connect(**kw):
            conn = other.connect()
            conn.execute("SET statement_timeout = '150ms'")
            conn.commit()
            return conn

        def disconnect_after_failed_exchange(*args, **kw):
            try:
                return original(*args, **kw)
            except Exception:
                args[0].connection.close()
                raise

        monkeypatch.setattr(hs, "try_hub_connect", bounded_connect)
        monkeypatch.setattr(hs, "_one_exchange", disconnect_after_failed_exchange)
        with other.connect() as conn:
            assert hs._hub_incarnation(conn.cursor()) != old["incarnation"]
        with other.connect() as lock:
            lock.execute("LOCK TABLE episodes IN ACCESS EXCLUSIVE MODE")
            out = hs.pull_changes(include_ledger=False)
        assert not out["ok"], out
        assert stamp() is None and shown() == 0, "the first exchange's incarnation was lost with its error"
    finally:
        pg_cluster.drop(other)


def test_failed_pull_reread_obeys_client_deadline(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, "REREAD_BUDGET")
    assert hs.refresh()["facts"]["copied"]
    server, gate, sockets = socket.create_server(("127.0.0.1", 0)), threading.Event(), []
    gate.set()

    def relay(src, dst, downstream):
        try:
            while True:
                data = src.recv(1 << 16)
                if not data:
                    return
                if downstream:
                    gate.wait(5)
                dst.sendall(data)
        except OSError:
            pass

    def accept():
        client, _ = server.accept()
        up = socket.create_connection(("127.0.0.1", pg.cluster.port))
        sockets.extend([client, up])
        for a, b, down in ((client, up, False), (up, client, True)):
            threading.Thread(target=relay, args=(a, b, down), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    remote = pg.dsn.replace(f":{pg.cluster.port}/", f":{server.getsockname()[1]}/")

    def connect(**kw):
        conn = psycopg.Connection.connect(remote)
        conn.execute("SET statement_timeout = '150ms'")
        conn.commit()
        return conn

    monkeypatch.setattr(hs, "try_hub_connect", connect)
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 0.3)
    original, failed, result = hs._one_exchange, threading.Event(), []

    def stall_after_failure(*args, **kw):
        # Any hub work after the failed exchange gets no answer from here on.
        try:
            return original(*args, **kw)
        except Exception:
            gate.clear()
            failed.set()
            raise

    monkeypatch.setattr(hs, "_one_exchange", stall_after_failure)
    with pg.connect() as lock:
        lock.execute("LOCK TABLE episodes IN ACCESS EXCLUSIVE MODE")
        thread = threading.Thread(target=lambda: result.append(hs.pull_changes(include_ledger=False)), daemon=True)
        thread.start()
        try:
            assert failed.wait(3)
            thread.join(0.35)
            alive = thread.is_alive()
        finally:
            gate.set()
            thread.join(5)
            server.close()
            for sock in sockets:
                sock.close()
    assert not alive, "a failed pull waited on the hub past its budget"
    assert result and not result[0]["ok"], result


def test_export_restore_preserves_topic_search(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    source = hs.snapshot_path()
    source.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as con:
        hs._create_schema(con)
        for slug, body in (("gone", "obsoleteword"), ("two", "quartzneedle"), ("three", "cobaltneedle")):
            con.execute("INSERT INTO topics(slug, title, body) VALUES (?, ?, ?)", (slug, slug, body))
        con.execute("DELETE FROM topics WHERE slug = 'gone'")

    def search(path, term):
        with sqlite3.connect(path) as con:
            return con.execute("SELECT t.slug FROM topics_fts f JOIN topics t ON t.rowid = f.rowid "
                               "WHERE topics_fts MATCH ?", (term,)).fetchall()

    def inspect(path):
        with sqlite3.connect(path) as con:
            rows = con.execute("SELECT rowid, slug FROM topics ORDER BY rowid").fetchall()
        return {"rows": rows, "quartz": search(path, "quartzneedle"), "cobalt": search(path, "cobaltneedle")}

    before = inspect(source)
    dest = tmp_path / "export.sqlite"
    hs.export_replica_copy(source, dest)
    assert inspect(dest) == before
    staged = hs.staging_path()
    shutil.copyfile(dest, staged)
    assert hs.publish_imported_snapshot(staged)["ok"]
    assert inspect(source) == before
    # Writes after the restore keep the index in step (its triggers by rowid).
    with sqlite3.connect(source) as con:
        con.execute("UPDATE topics SET body = 'zincneedle' WHERE slug = 'two'")
        con.execute("DELETE FROM topics WHERE slug = 'three'")
        con.execute("INSERT INTO topics(slug, title, body) VALUES ('four', 'four', 'amberneedle')")
        con.execute("INSERT INTO topics_fts(topics_fts, rank) VALUES ('integrity-check', 1)")
    assert search(source, "zincneedle") == [("two",)] and search(source, "quartzneedle") == []
    assert search(source, "cobaltneedle") == [] and search(source, "amberneedle") == [("four",)]


def test_phase_timer_times_the_pull_thread_once():
    from tests.test_provenance_copy_bench import PhaseTimer

    timer = PhaseTimer()

    def copy(*, session=None):
        time.sleep(0.05)
        return copy() if session is not None else None

    copy = timer.wrap("copy_facts", copy)
    other = threading.Thread(target=copy)
    timer.start()
    other.start()
    copy(session=object())  # calls itself, as copy_facts(session=...) does
    other.join()
    timer.stop()
    assert set(timer.phases) == {"copy_facts"}
    assert 0.09 < timer.phases["copy_facts"] < 0.14, timer.phases
