"""Read-only audit proofs. Synthetic data; disposable homes and loopback PG only."""
import json
import os

import psycopg
import pytest

from khipu import db, mcp_server, owner_writes
from tests.hermetic_pg import hub, pg_cluster


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    db._TABLE_COLUMNS_CACHE.clear()
    # Exercise real DSN resolution and db.connect, permitting only the fixture's
    # loopback PostgreSQL transport rather than the conftest network sentinel.
    def loopback_only(conninfo, **kwargs):
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(conninfo)
        assert params["host"] == "127.0.0.1"
        assert params["dbname"].startswith("t_")
        return psycopg.Connection.connect(conninfo, **kwargs)
    monkeypatch.setattr(psycopg, "connect", loopback_only)


def seed(hub, text):
    with hub.connect() as conn:
        cid = conn.execute("INSERT INTO commitments (text, content_hash) VALUES (%s, %s) RETURNING id",
                           (text, text)).fetchone()[0]
        conn.commit()
        return cid


def status(hub, cid):
    with hub.connect() as conn:
        return conn.execute("SELECT text, status FROM commitments WHERE id=%s", (cid,)).fetchone()


def test_waiting_close_must_not_mutate_same_numeric_id_on_replacement_hub(hub, pg_cluster, monkeypatch):
    """Only transport outage is injected; queue, DSN resolution and replay are real."""
    second = pg_cluster.new_database()
    try:
        hub.migrate()
        second.migrate()
        original = seed(hub, "synthetic original-owner item")
        other = seed(second, "synthetic unrelated replacement-hub item")
        assert original == other
        monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
        apply = owner_writes._apply

        def down(*args):
            raise psycopg.OperationalError("synthetic original hub unavailable")

        monkeypatch.setattr(owner_writes, "_apply", down)
        queued = mcp_server._tool_owed_update({"id": original, "action": "close"})
        assert queued["waiting_to_sync"]
        monkeypatch.setattr(owner_writes, "_apply", apply)
        monkeypatch.setenv("KHIPU_DATABASE_URL", second.dsn)
        result = owner_writes.drain()
        print("HUB_SWITCH", json.dumps({"drain": result, "original": status(hub, original),
                                        "replacement": status(second, other)}))
        assert status(second, other)[1] == "open", "queued owner close silently applied to a different hub's unrelated row"
        assert status(hub, original)[1] == "open"
        assert result["blocked"] and result["waiting"] == 1
        assert owner_writes.status()["blocked"] == 1
        # The binding survives refusal and retrying at the original destination.
        monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
        assert owner_writes.drain()["replayed"] == 1
        assert status(hub, original)[1] == "closed"
        assert status(second, other)[1] == "open"
    finally:
        pg_cluster.drop(second)


def test_waiting_supersede_must_not_upload_original_hub_words_to_replacement(hub, pg_cluster, monkeypatch):
    second = pg_cluster.new_database()
    secret = "SYNTHETIC_PRIVATE_ORIGINAL_HUB_DECISION_WORDS"
    try:
        hub.migrate()
        second.migrate()
        ids = []
        for target, words in ((hub, "synthetic original decision"), (second, "synthetic unrelated decision")):
            with target.connect() as conn:
                ids.append(conn.execute("INSERT INTO decisions (text) VALUES (%s) RETURNING id", (words,)).fetchone()[0])
                conn.commit()
        assert ids[0] == ids[1]
        monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
        apply = owner_writes._apply

        def down(*args):
            raise psycopg.OperationalError("synthetic original hub unavailable")

        monkeypatch.setattr(owner_writes, "_apply", down)
        queued = mcp_server._tool_decisions_update({"id": ids[0], "action": "supersede", "new_text": secret})
        assert queued["waiting_to_sync"]
        monkeypatch.setattr(owner_writes, "_apply", apply)
        monkeypatch.setenv("KHIPU_DATABASE_URL", second.dsn)
        result = owner_writes.drain()
        with second.connect() as conn:
            rows = conn.execute("SELECT id, text, superseded_by FROM decisions ORDER BY id").fetchall()
        print("HUB_SWITCH_SUPERSEDE", json.dumps({"drain": result, "replacement_rows": rows}))
        assert not any(row[1] == secret for row in rows), "queued original-hub decision words were uploaded to unrelated replacement hub"
        assert rows == [(ids[1], "synthetic unrelated decision", None)]
        assert result["blocked"] and result["waiting"] == 1
        job = json.loads(next(owner_writes.queue_dir().glob("*.json")).read_text())
        assert job["args"]["new_text"] == secret
        assert job["args"]["action"] == "supersede"
        monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
        assert owner_writes.drain()["replayed"] == 1
        with hub.connect() as conn:
            assert conn.execute("SELECT count(*) FROM decisions WHERE text=%s", (secret,)).fetchone()[0] == 1
        assert owner_writes.drain()["replayed"] == 0
    finally:
        pg_cluster.drop(second)


def test_destination_canonicalizes_conninfo_and_omits_credentials(monkeypatch):
    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://old:synthetic-password@127.0.0.1/fixture")
    first = owner_writes._configured_destination()
    monkeypatch.setenv("KHIPU_DATABASE_URL", "host=127.0.0.1 port=5432 dbname=fixture user=new password=other-synthetic")
    assert owner_writes._configured_destination() == first
    assert "synthetic" not in first and "127.0.0.1" not in first
    for changed in ("host=localhost dbname=fixture", "host=127.0.0.1 port=5433 dbname=fixture",
                    "host=127.0.0.1 dbname=other"):
        monkeypatch.setenv("KHIPU_DATABASE_URL", changed)
        assert owner_writes._configured_destination() != first


@pytest.mark.parametrize("state", ["waiting", "reserved", "sending"])
def test_unbound_write_is_retained_and_visibly_blocked(hub, monkeypatch, state):
    hub.migrate()
    cid = seed(hub, "synthetic unaffected item")
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
    args = {"id": cid, "action": "close", "write_id": "synthetic-unbound-write"}
    owner_writes.queue_dir().mkdir(parents=True)
    path = owner_writes.queue_dir() / "00000000000000000001-1.json"
    path.write_text(json.dumps({"tool": "owed_update", "args": args, "state": state}))
    monkeypatch.setattr(owner_writes, "_landed", lambda *a: pytest.fail("unbound job queried a hub"))
    out = owner_writes.drain()
    assert out["blocked"] and out["waiting"] == 1 and out["replayed"] == 0
    assert status(hub, cid)[1] == "open"
    job = json.loads(path.read_text())
    assert job["args"] == args and job["state"] == "blocked"
    assert "missing" in owner_writes.status()["blocked_reasons"][0]


def test_configuration_change_after_landed_check_cannot_redirect_write(hub, pg_cluster, monkeypatch):
    second = pg_cluster.new_database()
    try:
        hub.migrate()
        second.migrate()
        cid = seed(hub, "synthetic intended item")
        assert seed(second, "synthetic unrelated item") == cid
        monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
        owner_writes._enqueue("owed_update", {"id": cid, "action": "close"}, "synthetic outage")
        original_landed = owner_writes._landed
        def changed(tool, args):
            result = original_landed(tool, args)
            monkeypatch.setenv("KHIPU_DATABASE_URL", second.dsn)
            return result
        monkeypatch.setattr(owner_writes, "_landed", changed)
        out = owner_writes.drain()
        assert out["blocked"] and out["waiting"] == 1
        assert status(hub, cid)[1] == status(second, cid)[1] == "open"
        with second.connect() as conn:
            assert conn.execute("SELECT count(*) FROM owner_writes_applied").fetchone()[0] == 0
    finally:
        pg_cluster.drop(second)


def test_wrong_connection_cannot_acknowledge_same_write_id(hub, pg_cluster, monkeypatch):
    second = pg_cluster.new_database()
    try:
        hub.migrate()
        second.migrate()
        cid = seed(hub, "synthetic intended item")
        monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
        owner_writes._enqueue("owed_update", {"id": cid, "action": "close"}, "synthetic outage")
        path = next(owner_writes.queue_dir().glob("*.json"))
        job = json.loads(path.read_text())
        with second.connect() as conn:
            conn.execute("INSERT INTO owner_writes_applied(write_id, tool) VALUES (%s, 'owed_update')",
                         (job["args"]["write_id"],))
            conn.commit()
        # Config still names A, but transport returns B: validate actual conn.
        monkeypatch.setattr(db, "connect", lambda **kw: second.connect())
        out = owner_writes.drain()
        assert out["blocked"] and out["waiting"] == 1 and path.exists()
        assert status(hub, cid)[1] == "open"
    finally:
        pg_cluster.drop(second)


def test_resolved_address_is_optional_but_an_explicit_pin_is_enforced():
    from types import SimpleNamespace
    params = {"host": "synthetic.example", "hostaddr": "127.0.0.1", "port": "5432", "dbname": "fixture"}
    conn = SimpleNamespace(info=SimpleNamespace(get_parameters=lambda: params))
    with owner_writes._bound_job({"destination": owner_writes._destination(params)}):
        owner_writes._check_destination(conn)
        params["hostaddr"] = "127.0.0.2"
        with pytest.raises(owner_writes.DestinationBlocked):
            owner_writes._check_destination(conn)
    with owner_writes._bound_job({"destination": owner_writes._destination({
            k: v for k, v in params.items() if k != "hostaddr"})}):
        owner_writes._check_destination(conn)


def test_persisted_destination_never_contains_configured_credentials(monkeypatch):
    monkeypatch.setenv("KHIPU_DATABASE_URL",
                       "postgresql://synthetic_user:synthetic_password@127.0.0.1:5432/fixture")
    owner_writes._enqueue("owed_update", {"id": 1, "action": "close"}, "synthetic outage")
    contents = next(owner_writes.queue_dir().glob("*.json")).read_text()
    assert "synthetic_user" not in contents
    assert "synthetic_password" not in contents
    assert "postgresql://" not in contents
    assert "127.0.0.1" not in contents
    assert json.loads(contents)["destination"].startswith("endpoint-v1:")


def test_pending_marks_follow_binding_while_wrong_destination_stays_visible(monkeypatch):
    first = "postgresql://synthetic@127.0.0.1:5432/first"
    second = "postgresql://synthetic@127.0.0.1:5432/second"
    monkeypatch.setenv("KHIPU_DATABASE_URL", first)
    owner_writes._enqueue("owed_update", {"id": 7, "action": "close"}, "synthetic outage")
    path = next(owner_writes.queue_dir().glob("*.json"))
    original = json.loads(path.read_text())
    assert owner_writes.pending_marks("owed_update") == {7: "close"}
    monkeypatch.setenv("KHIPU_DATABASE_URL", second)
    assert owner_writes.pending_marks("owed_update") == {}
    assert owner_writes.status()["pending"] == owner_writes.status()["blocked"] == 1
    assert json.loads(path.read_text()) == original
    monkeypatch.setenv("KHIPU_DATABASE_URL", first)
    assert owner_writes.pending_marks("owed_update") == {7: "close"}
    # Legacy unbound requests remain visible but never mark a colliding row.
    path.write_text(json.dumps({k: v for k, v in original.items() if k != "destination"}))
    assert owner_writes.pending_marks("owed_update") == {}
    assert owner_writes.status()["pending"] == owner_writes.status()["blocked"] == 1
