# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""The fifth 7B fix round (astra-7b5 findings 1 and 2): the review's
boundary probes, ported, each failing on 40b3680c. Synthetic stores and
hermetic Postgres only; the Keychain is stubbed in-process."""
from __future__ import annotations

import json
import socket
import threading
import time

import psycopg
import pytest

from khipu import db, hub_snapshot as hs, keychain
from tests.fact_seed import announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import replica, shown, stamp  # noqa: F401
from tests.test_provenance_copy import _decision, _new_hub
from tests.test_provenance_forget import env, pg  # noqa: F401

_RESOLVE = db.resolve_dsn  # before the replica fixture pins it


def _keychain_only(monkeypatch, tmp_path, dsn: str) -> None:
    for name in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DSN_FILE", str(tmp_path / "no-dsn-file"))
    monkeypatch.setattr(hs, "_LEGACY_DSN_FILE", tmp_path / "no-legacy-file")
    monkeypatch.setattr(keychain, "keychain_available", lambda: True)
    monkeypatch.setattr(keychain, "get_password",
                        lambda account, **kw: dsn if account == keychain.DSN_ACCOUNT
                        and kw.get("service", keychain.SERVICE) == keychain.SERVICE else None)
    monkeypatch.setattr(db, "_MIGRATION_DONE", True)
    monkeypatch.setattr(db, "resolve_dsn", _RESOLVE)


def _busy(*a, **kw):
    raise hs._NotAdmitted("synthetic stamp lock timeout")


def test_failed_save_delete_does_not_reset_revision(pg, replica, tmp_path, monkeypatch):
    replica(pg)
    _decision(pg, "STALE_AFTER_FAILED_SAVE")
    _keychain_only(monkeypatch, tmp_path, pg.dsn)
    assert hs.refresh()["facts"]["copied"]
    target = hs.snapshot_path().parent / hs._DESTINATION_FILE
    assert json.loads(target.read_text())["revision"] == 0  # the first copy's record
    original, reached, release, result = hs._record_destination, threading.Event(), threading.Event(), []

    def writer(*args, **kw):
        if threading.current_thread().name == "stale-copy":
            reached.set()
            assert release.wait(5)
            return original(*args, **kw)
        raise PermissionError("synthetic endpoint record write denied")

    monkeypatch.setattr(hs, "_record_destination", writer)
    thread = threading.Thread(target=lambda: result.append(hs.copy_facts()), name="stale-copy")
    thread.start()
    try:
        assert reached.wait(5)
        _keychain_only(monkeypatch, tmp_path, pg.dsn + "-replacement")
        (target.parent / hs._ENDPOINT_CHANGES_DIR).write_text("synthetic marker obstruction")
        monkeypatch.setattr(hs, "_admitted", _busy)
        hs.endpoint_changed()
        assert not target.exists() and shown() == 0
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive(), result
    assert shown() == 0, "the failed save reset the revision; the delayed copy restored the old fingerprint"


def test_save_that_can_neither_record_nor_invalidate_raises(pg, replica, tmp_path, monkeypatch):
    replica(pg)
    _decision(pg, "NEITHER_RECORDED_NOR_INVALIDATED")
    _keychain_only(monkeypatch, tmp_path, pg.dsn)
    assert hs.refresh()["facts"]["copied"]
    folder = hs.snapshot_path().parent
    _keychain_only(monkeypatch, tmp_path, pg.dsn + "-replacement")

    def denied(*a, **kw):
        raise PermissionError("synthetic endpoint record write denied")

    monkeypatch.setattr(hs, "_record_destination", denied)
    monkeypatch.setattr(hs, "_admitted", _busy)
    (folder / hs._ENDPOINT_CHANGES_DIR).write_text("synthetic marker obstruction")
    # The generation cannot be raised: removing the record alone would let a
    # delayed copy record the old endpoint again.
    (folder / "fact-endpoint-generation").mkdir()  # hs._GENERATION_FILE
    (folder / "fact-endpoint-generation" / "occupant").write_text("x")
    with pytest.raises(hs.EndpointChangeUnrecorded):
        hs.endpoint_changed()
    assert shown() == 0


def test_received_incarnation_survives_transport_loss(pg, replica, pg_cluster, monkeypatch):
    replica(pg)
    _decision(pg, "OLD_AFTER_TRANSPORT_LOSS")
    assert hs.refresh()["facts"]["copied"]
    old, other = stamp(), _new_hub(pg_cluster)
    server, sockets, received = socket.create_server(("127.0.0.1", 0)), [], threading.Event()
    try:
        with other.connect() as conn:
            incarnation = hs._hub_incarnation(conn.cursor())
        assert incarnation != old["incarnation"]
        # A later episode read large enough to flush the incarnation to the
        # client before the rest of a batched exchange completes.
        with other.connect() as conn:
            announce(conn)
            conn.execute("INSERT INTO episodes (ts, session_id, summary, harness, project) "
                         "VALUES (now(), 'codex:transport', repeat('x', 65536), 'codex', 'acme/widget')")

        def upstream(src, dst):
            try:
                while data := src.recv(65536):
                    dst.sendall(data)
            except OSError:
                pass

        def accept():
            client, _ = server.accept()
            up = socket.create_connection(("127.0.0.1", other.cluster.port))
            sockets.extend([client, up])
            threading.Thread(target=upstream, args=(client, up), daemon=True).start()
            try:
                while data := up.recv(65536):
                    client.sendall(data)
                    if incarnation.encode() in data:
                        received.set()
                        # Delivered; then the transport fails.
                        time.sleep(0.02)
                        client.shutdown(socket.SHUT_RDWR)
                        client.close()
                        up.close()
                        return
            except OSError:
                pass

        threading.Thread(target=accept, daemon=True).start()
        remote = other.dsn.replace(f":{other.cluster.port}/", f":{server.getsockname()[1]}/") + "?sslmode=disable"
        monkeypatch.setattr(hs, "try_hub_connect", lambda **kw: psycopg.Connection.connect(remote))
        # The configured address stays A while it serves replacement B.
        monkeypatch.setattr(db, "resolve_dsn", lambda: pg.dsn)
        with other.connect() as lock:
            lock.execute("LOCK TABLE topics IN ACCESS EXCLUSIVE MODE")
            out = hs.pull_changes(include_ledger=False)
        assert received.is_set(), "the proxy never delivered the incarnation; reproduction invalid"
        assert not out["ok"], out
        assert stamp() is None and shown() == 0, "a delivered incarnation was lost with the transport"
    finally:
        server.close()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
        pg_cluster.drop(other)
