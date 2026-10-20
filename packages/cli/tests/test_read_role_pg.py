"""The hub's search role (docs/plans/2026-10-19-swarm-ready.md, slice 2),
against the throwaway PostgreSQL of tests/hermetic_pg.py.

A full ``khipu_read`` never delays a capture; setup and an upgraded install
both end with a working read DSN and no owner step; search never writes on
its read connection; role settings change the role on the hub.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import psycopg
import pytest

from khipu import db, read_role
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


@pytest.fixture
def owner_env(hub, monkeypatch):  # noqa: F811
    # The suite forbids psycopg.connect (the live hub); only the throwaway
    # cluster's address is let through, as either role.
    from psycopg.conninfo import conninfo_to_dict

    port = str(conninfo_to_dict(hub.dsn)["port"])

    def throwaway_only(conninfo, **kw):
        params = conninfo_to_dict(conninfo)
        assert params.get("host") == "127.0.0.1" and str(params.get("port")) == port, "not the throwaway cluster"
        return psycopg.Connection.connect(conninfo, **kw)

    monkeypatch.setattr(psycopg, "connect", throwaway_only)
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
    monkeypatch.delenv("KHIPU_READ_DATABASE_URL", raising=False)
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    return hub


def _who(conn) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_user, current_setting('application_name')")
        return cur.fetchone()


def test_the_read_role_is_select_only_and_cannot_read_its_password(owner_env):
    owner = owner_env.connect()
    result = read_role.ensure_read_role(owner)
    assert result["ok"], result
    conninfo = read_role.read_conninfo_for(owner_env.dsn, read_role.hub_password(owner))
    with psycopg.connect(conninfo) as reader, reader.cursor() as cur:
        cur.execute("SELECT count(*) FROM episodes")
        cur.fetchone()
        reader.rollback()
        with pytest.raises(psycopg.Error):
            cur.execute("INSERT INTO memory_query_cache (profile, query_hash, embedding) VALUES ('x', 'y', NULL)")
        reader.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(f"SELECT secret FROM {read_role.SECRETS_TABLE}")
        reader.rollback()
    with owner.cursor() as cur:
        cur.execute("SELECT rolsuper, rolconnlimit, rolconfig FROM pg_roles WHERE rolname = %s", (read_role.READ_ROLE,))
        sup, limit, config = cur.fetchone()
    assert sup is False and limit == 20
    assert "statement_timeout=30000ms" in config
    assert "idle_in_transaction_session_timeout=60000ms" in config


def test_a_capture_lands_within_two_seconds_while_the_read_role_is_full(owner_env, monkeypatch):
    from khipu import capture

    owner = owner_env.connect()
    read_role.set_setting("read_role_connection_limit", 2, conn=owner)
    held = [db.connect(role="read"), db.connect(role="read")]
    try:
        assert all(_who(c)[0] == read_role.READ_ROLE for c in held)
        with pytest.raises(psycopg.OperationalError) as full:
            db.connect(role="read")
        assert read_role.role_limit_error(full.value)
        # A full read role is not an outage: nothing marks the hub down.
        from khipu import hub_reach

        assert hub_reach.known_unreachable() is None

        labels: list[str] = []
        real_open = db._open

        def watch(dsn, label, **kw):
            labels.append(label)
            return real_open(dsn, label, **kw)

        monkeypatch.setattr(db, "_open", watch)
        payload = dict(ts="2026-10-20T00:00:00Z", summary="Saved while searches filled the read role",
                       session_id="codex:swarm", harness="codex", project="khipu/test", repo_root="/repo",
                       parent_session_id=None, transcript_range="0:10", topics=[], decisions=[],
                       preferences=[], people=[], tags=[], raw={}, verbatim={})
        t0 = time.monotonic()
        stats = capture.write_pg(payload)
        elapsed = time.monotonic() - t0
    finally:
        for c in held:
            c.close()
    assert elapsed < 2.0, elapsed
    assert stats.get("episode_inserted"), stats
    assert labels and set(labels) == {"owner"}, labels
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM episodes WHERE summary = %s", (payload["summary"],))
        assert cur.fetchone()[0] == 1


def test_setup_creates_the_role_and_saves_a_working_read_dsn(owner_env):
    from khipu import setup

    owner = owner_env.connect()
    entry = setup._store_read_role(owner, owner_env.dsn)
    assert entry["ok"], entry
    assert read_role._read_dsn_file().is_file()
    assert oct(read_role._read_dsn_file().stat().st_mode & 0o777) == "0o600"
    with db.connect(role="read") as conn:
        user, app = _who(conn)
    assert user == read_role.READ_ROLE
    assert app.startswith("khipu/read/")


def test_an_upgraded_install_provisions_on_first_search_and_a_second_mac_reuses_the_password(owner_env):
    assert read_role.stored_read_dsn() is None
    with db.connect(role="read") as conn:
        assert _who(conn)[0] == read_role.READ_ROLE
    owner = owner_env.connect()
    with owner.cursor() as cur:
        cur.execute(f"SELECT secret, rotated_at FROM {read_role.SECRETS_TABLE} WHERE role = %s",
                    (read_role.READ_ROLE,))
        first = cur.fetchone()
    owner.rollback()
    # Another Mac: nothing stored locally, the hub already holds the password.
    read_role._read_dsn_file().unlink()
    with db.connect(role="read") as conn:
        assert _who(conn)[0] == read_role.READ_ROLE
    with owner.cursor() as cur:
        cur.execute(f"SELECT secret, rotated_at FROM {read_role.SECRETS_TABLE} WHERE role = %s",
                    (read_role.READ_ROLE,))
        assert cur.fetchone() == first
    # Captures and writes keep the owner role.
    with db.connect() as conn:
        user, app = _who(conn)
    assert user != read_role.READ_ROLE and app.startswith("khipu/owner/")


def test_an_owner_that_cannot_create_roles_searches_as_the_owner_labelled(owner_env, monkeypatch):
    owner = owner_env.connect()
    owner.autocommit = True
    owner.execute("DROP ROLE IF EXISTS khipu_limited_owner")
    owner.execute("CREATE ROLE khipu_limited_owner LOGIN NOSUPERUSER NOCREATEROLE")
    owner.execute(f"GRANT CONNECT ON DATABASE {owner_env.name} TO khipu_limited_owner")
    limited = owner_env.dsn.replace("postgresql://khipu@", "postgresql://khipu_limited_owner@")
    monkeypatch.setenv("KHIPU_DATABASE_URL", limited)
    try:
        with db.connect(role="read") as conn:
            user, app = _who(conn)
        assert user == "khipu_limited_owner"
        assert app.startswith("khipu/owner-read/")
        assert "cannot create roles" in (read_role.provision_blocked() or "")
    finally:
        owner.execute(f"REVOKE CONNECT ON DATABASE {owner_env.name} FROM khipu_limited_owner")
        owner.execute("DROP ROLE IF EXISTS khipu_limited_owner")


def _cache_profile(owner) -> tuple[str, int]:
    with owner.cursor() as cur:
        cur.execute("SELECT id FROM embedding_profiles ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO embedding_profiles (id, provider, model, dim) VALUES ('t-768', 'gemini', 'm', 768)"
                        " RETURNING id")
            row = cur.fetchone()
        cur.execute("SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
                    " WHERE attrelid = 'memory_query_cache'::regclass AND attname = 'embedding'")
        kind = cur.fetchone()[0]
    owner.commit()
    dim = int(kind.split("(")[1].rstrip(")")) if "(" in kind else 3
    return row[0], dim


def test_search_writes_nothing_on_its_read_connection_and_the_drain_stores_the_cache(owner_env, monkeypatch):
    from khipu import embed, query_cache_queue

    owner = owner_env.connect()
    profile, dim = _cache_profile(owner)
    vec = [0.5] * dim
    monkeypatch.setattr(embed, "embed_one", lambda *a, **k: vec)
    monkeypatch.setattr(embed, "resolve_spec", lambda *a, **k: SimpleNamespace(dim=dim))
    with db.connect(role="read") as conn, conn.cursor() as cur:
        assert _who(conn)[0] == read_role.READ_ROLE
        out, state = embed._query_vec(cur, conn, profile, "what did we decide")
        assert (out, state) == (vec, "miss")
        conn.rollback()
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_query_cache")
        assert cur.fetchone()[0] == 0
    owner.rollback()
    assert query_cache_queue.pending() == 1
    assert query_cache_queue.flush_on_owner() == {"put": 1, "hit": 0}
    # A fresh process reads the stored vector as a hit and queues only a bump.
    query_cache_queue._MEMORY.clear()
    monkeypatch.setattr(embed, "embed_one", lambda *a, **k: pytest.fail("no provider call on a hit"))
    with db.connect(role="read") as conn, conn.cursor() as cur:
        _out, state = embed._query_vec(cur, conn, profile, "what did we decide")
        assert state == "hit"
        conn.rollback()
    assert query_cache_queue.flush_on_owner() == {"put": 0, "hit": 1}
    with owner.cursor() as cur:
        cur.execute("SELECT hits FROM memory_query_cache")
        assert cur.fetchone()[0] == 1


def test_role_settings_change_the_role_on_the_hub_and_zero_lifts_them(owner_env):
    owner = owner_env.connect()
    read_role.set_setting("read_role_statement_timeout", 5, conn=owner)
    read_role.set_setting("read_role_connection_limit", 0, conn=owner)
    with owner.cursor() as cur:
        cur.execute("SELECT rolconnlimit, rolconfig FROM pg_roles WHERE rolname = %s", (read_role.READ_ROLE,))
        limit, config = cur.fetchone()
    assert limit == -1
    assert "statement_timeout=5000ms" in config
    assert read_role.settings_status()["read_role_statement_timeout"]["value"] == 5


def test_doctor_names_a_client_still_searching_as_the_owner(owner_env):
    owner = owner_env.connect()
    read_role.ensure_read_role(owner)
    old_client = psycopg.Connection.connect(owner_env.dsn)  # an old client sets no label
    try:
        block = read_role.doctor_block(conn=owner)
    finally:
        old_client.close()
    assert block["owner_searchers"], block
    assert any("not upgraded" in w for w in block["warnings"]), block


def test_reserved_connections_is_staged_only_where_it_is_postmaster_context(pg_cluster):  # noqa: F811
    with pg_cluster.admin() as admin:
        context = admin.execute(read_role.RESERVED_CONNECTIONS_CHECK_SQL).fetchone()
        if context is None:
            pytest.skip("this PostgreSQL has no reserved_connections")
        try:
            result = read_role.stage_reserved_connections(admin)
            assert result["ok"] is (context[0] == "postmaster"), result
        finally:
            admin.execute(read_role.RESERVED_CONNECTIONS_ROLLBACK_SQL)
