"""Short-project-name resolution never admits a project that exists only
through forgotten (or pending-erasure) episodes, not even by an alias: such a
name neither resolves a short name nor appears in an ambiguity error (P1
re-check of PR #426). Hub and replica, plus the MCP error a caller sees."""
from __future__ import annotations

import json
import sqlite3
from unittest import mock

import psycopg
import pytest

from khipu import db, hub_snapshot as hs, identity, mcp_server as srv, offline_reads
from tests import fact_seed
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import certify


# --- hub -------------------------------------------------------------------

@pytest.fixture
def hub_conn(hub, tmp_path, monkeypatch):  # noqa: F811
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    conn = hub.connect()
    hub.migrate(conn=conn)
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda *a, **k: hub.connect())
    return conn


def _hub_commitment(conn, project, text, h, *, forgotten=False):
    with conn.cursor() as cur:
        ep = None
        if forgotten:
            fact_seed.announce(cur)
            cur.execute("INSERT INTO episodes (ts, session_id, summary, project) "
                        "VALUES (now(), %s, 'gone', %s) RETURNING id", (f"s-{h}", project))
            ep = cur.fetchone()[0]
        cur.execute("INSERT INTO commitments (text, project, owner, content_hash, opened_episode) "
                    "VALUES (%s, %s, 'user', %s, %s) RETURNING id", (text, project, h, ep))
        cid = cur.fetchone()[0]
        fact_seed.adopt(cur, "commitment", cid)
        if forgotten:
            cur.execute("DELETE FROM fact_claims WHERE kind = 'commitment' AND fact_id = %s", (cid,))
            cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (ep,))
    conn.commit()


def _hub_alias(conn, alias, project):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s)", (alias, project))
    conn.commit()


def _hub_resolve(conn, name):
    with conn.cursor() as cur:
        return identity.resolve_project_name(cur, name)


def test_hub_alias_to_forgotten_only_project_is_not_a_candidate(hub_conn):
    _hub_commitment(hub_conn, "public/leaf", "public task", "h-pub")
    _hub_commitment(hub_conn, "secret/leaf", "forgotten task", "h-sec", forgotten=True)
    assert _hub_resolve(hub_conn, "leaf") == "public/leaf"
    _hub_alias(hub_conn, "old-secret", "secret/leaf")
    assert _hub_resolve(hub_conn, "leaf") == "public/leaf"
    out = srv._tool_owed({"project": "leaf"})
    assert out["project"] == "public/leaf"
    assert "secret/leaf" not in json.dumps(out, default=str)


def test_hub_forgotten_only_project_never_resolves_a_short_name(hub_conn):
    _hub_commitment(hub_conn, "secret/leaf", "forgotten task", "h-sec", forgotten=True)
    _hub_alias(hub_conn, "old-secret", "secret/leaf")
    assert _hub_resolve(hub_conn, "leaf") == "leaf"
    assert _hub_resolve(hub_conn, "SECRET/LEAF") == "SECRET/LEAF"


def test_hub_ambiguity_names_only_visible_projects(hub_conn):
    _hub_commitment(hub_conn, "public/leaf", "public task", "h-pub")
    _hub_commitment(hub_conn, "other/leaf", "other task", "h-oth")
    _hub_commitment(hub_conn, "secret/leaf", "forgotten task", "h-sec", forgotten=True)
    _hub_alias(hub_conn, "old-secret", "secret/leaf")
    with pytest.raises(identity.AmbiguousProject) as err:
        _hub_resolve(hub_conn, "leaf")
    message = str(err.value)
    assert "public/leaf" in message and "other/leaf" in message
    assert "secret/leaf" not in message and "old-secret" not in message


def test_hub_alias_of_a_visible_project_still_resolves(hub_conn):
    # Control: the canonical holds no row itself, but its alias group is
    # backed by a visible commitment, so both names stay in play.
    _hub_commitment(hub_conn, "owner/church-harness", "work", "h-ch")
    _hub_alias(hub_conn, "owner/church-harness", "owner/olivewood")
    assert _hub_resolve(hub_conn, "olivewood") == "owner/olivewood"
    assert _hub_resolve(hub_conn, "church-harness") == "owner/olivewood"
    assert _hub_resolve(hub_conn, "owner/church-harness") == "owner/church-harness"


# --- replica ---------------------------------------------------------------

@pytest.fixture
def replica_dir(tmp_path, monkeypatch):
    for key in ("KHIPU_DATABASE_URL",):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "replica"))
    (tmp_path / "replica").mkdir()
    monkeypatch.setattr(db, "resolve_dsn", lambda *a, **k: "postgresql://fake.invalid/test")
    return tmp_path / "replica"


def _replica(*, aliases=True, extra=()):
    con = sqlite3.connect(hs.snapshot_path())
    hs._create_schema(con)
    if not aliases:
        con.execute("DROP TABLE project_aliases")
    con.executemany("INSERT INTO episodes(id, summary, deleted_at) VALUES (?, ?, ?)",
                    [(1, "live", None), (2, "gone", "2026-10-27"), (3, "queued", None)])
    con.executemany(
        "INSERT INTO commitments(id, text, project, opened_episode, status, owner) "
        "VALUES (?, ?, ?, ?, 'open', 'user')",
        [(1, "public task", "public/leaf", 1), (2, "forgotten task", "secret/leaf", 2),
         (3, "pending task", "pending/leaf", 3), *extra])
    hs._queue_erasure(3)
    certify(con)
    con.commit()
    return con


def _alias_rows(con):
    con.executemany("INSERT INTO project_aliases(alias, project) VALUES (?, ?)",
                    [("old-secret", "secret/leaf"), ("old-pending", "pending/leaf")])
    con.commit()


def test_replica_alias_to_forgotten_or_pending_project_is_not_a_candidate(replica_dir):
    con = _replica()
    try:
        assert hs.snapshot_resolve_project(con, "leaf") == "public/leaf"
        _alias_rows(con)
        assert hs.snapshot_resolve_project(con, "leaf") == "public/leaf"
    finally:
        con.close()
    assert offline_reads.owed_resolve_project("leaf") == "public/leaf"


def test_replica_ambiguity_names_only_visible_projects(replica_dir):
    con = _replica(extra=[(4, "other task", "other/leaf", 1)])
    try:
        _alias_rows(con)
        with pytest.raises(identity.AmbiguousProject) as err:
            hs.snapshot_resolve_project(con, "leaf")
    finally:
        con.close()
    message = str(err.value)
    assert "public/leaf" in message and "other/leaf" in message
    for hidden in ("secret/leaf", "pending/leaf", "old-secret", "old-pending"):
        assert hidden not in message


def test_mcp_owed_error_never_names_a_pending_erasure_project(replica_dir):
    con = _replica(extra=[(4, "other task", "other/leaf", 1)])
    _alias_rows(con)
    con.close()
    with mock.patch.object(db, "connect", side_effect=psycopg.OperationalError("connection is closed")), \
         mock.patch.object(srv, "_RELOAD_ON_CHANGE", False):
        response = srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "khipu_owed", "arguments": {"project": "leaf"}}})
    rendered = json.dumps(response)
    assert response["result"]["isError"]
    assert "public/leaf" in rendered and "other/leaf" in rendered
    for hidden in ("secret/leaf", "pending/leaf", "old-secret", "old-pending"):
        assert hidden not in rendered


def test_mcp_owed_offline_resolves_past_a_hidden_alias(replica_dir):
    con = _replica()
    _alias_rows(con)
    con.close()
    with mock.patch.object(db, "connect", side_effect=psycopg.OperationalError("connection is closed")), \
         mock.patch.object(srv, "_routing_direction_rows", return_value=[]):
        out = srv.owed_listing({"project": "leaf"})
    assert out["project"] == "public/leaf"
    assert [r["id"] for r in out["results"]] == [1]


def test_replica_without_alias_table_fences_and_resolves(replica_dir):
    # Control (probe_controls.py): no project_aliases table at all.
    con = _replica(aliases=False)
    try:
        assert hs.snapshot_resolve_project(con, "leaf") == "public/leaf"
        assert hs.snapshot_resolve_project(con, "PUBLIC/LEAF") == "public/leaf"
    finally:
        con.close()
    with mock.patch.object(db, "connect", side_effect=psycopg.OperationalError("connection is closed")), \
         mock.patch.object(srv, "_routing_direction_rows", return_value=[]):
        out = srv.owed_listing({"project": "leaf"})
    assert out["project"] == "public/leaf" and [r["id"] for r in out["results"]] == [1]


def test_replica_alias_of_a_visible_project_still_resolves(replica_dir):
    con = _replica()
    try:
        con.execute("INSERT INTO project_aliases(alias, project) VALUES ('public/leaf', 'public/olivewood')")
        con.commit()
        assert hs.snapshot_resolve_project(con, "olivewood") == "public/olivewood"
        assert hs.snapshot_resolve_project(con, "leaf") == "public/olivewood"
        assert hs.snapshot_resolve_project(con, "public/leaf") == "public/leaf"
    finally:
        con.close()
