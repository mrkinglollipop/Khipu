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


def test_a_hub_that_cannot_host_the_role_searches_as_the_owner_records_it_and_doctor_says_so(owner_env,
                                                                                               monkeypatch):
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
        assert "cannot create roles" in (read_role.cannot_host() or "")
        # Recorded: the next search does not ask the hub again.
        monkeypatch.setattr(read_role, "ensure_read_role", lambda *a, **k: pytest.fail("re-checked every search"))
        with db.connect(role="read") as conn:
            assert _who(conn)[1].startswith("khipu/owner-read/")
        block = read_role.doctor_block()
        assert block["state"] == "cannot_host" and block["level"] == "amber", block
        assert "share the save connections" in block["message"] and "CREATEROLE" in block["message"]
    finally:
        owner.execute(f"REVOKE CONNECT ON DATABASE {owner_env.name} FROM khipu_limited_owner")
        owner.execute("DROP ROLE IF EXISTS khipu_limited_owner")


def _watch_labels(monkeypatch, refuse_read: bool = False) -> list[str]:
    labels: list[str] = []
    real_open = db._open

    def watch(dsn, label, **kw):
        labels.append(label)
        if refuse_read and label == "read":
            raise psycopg.OperationalError(f'password authentication failed for user "{read_role.READ_ROLE}"')
        return real_open(dsn, label, **kw)

    monkeypatch.setattr(db, "_open", watch)
    return labels


def test_a_failed_provisioning_never_falls_back_to_the_owner(owner_env, monkeypatch):
    from khipu import hub_errors

    monkeypatch.setattr(read_role, "ensure_read_role", lambda *a, **k: {"ok": False, "reason": "boom"})
    labels = _watch_labels(monkeypatch)
    with pytest.raises(read_role.ReadRoleUnavailable) as err:
        db.connect(role="read")
    assert "boom" in str(err.value)
    # Search takes the same path as for an unreachable hub (this Mac's copy).
    assert hub_errors.unreachable_error(err.value)
    # Recorded, so the next search answers at once, still never as the owner.
    with pytest.raises(read_role.ReadRoleUnavailable):
        db.connect(role="read")
    assert "owner-read" not in labels and "read" not in labels, labels


def test_a_refused_password_that_cannot_be_repaired_never_falls_back_to_the_owner(owner_env, monkeypatch):
    with db.connect(role="read") as conn:
        assert _who(conn)[0] == read_role.READ_ROLE
    assert read_role._CACHED
    labels = _watch_labels(monkeypatch, refuse_read=True)
    with pytest.raises(read_role.ReadRoleUnavailable):
        db.connect(role="read")
    assert "owner-read" not in labels, labels
    assert not read_role._CACHED and read_role.stored_read_dsn() is None


def test_read_entry_points_connect_as_the_read_role(owner_env, monkeypatch, capsys):
    import argparse

    from khipu import cli, recall_prompt

    with db.connect(role="read"):  # provisioning itself is an owner connect
        pass
    labels = _watch_labels(monkeypatch)
    assert cli.cmd_graph(argparse.Namespace(id="topic:nothing", hops=1, limit=5)) == 0
    assert cli.cmd_decisions(argparse.Namespace(decisions_cmd="list", project=None, since=None, limit=5,
                                                standing=False, superseded=False, retracted=False,
                                                all=False)) == 0
    monkeypatch.setattr(recall_prompt, "_project_for_cwd", lambda cwd: "khipu/test")
    monkeypatch.setattr(recall_prompt, "_replica_deliverable_line", lambda *a, **k: None)
    recall_prompt._deliverable_context_line(["swarm", "slice"], cwd="/repo")
    capsys.readouterr()
    assert labels and set(labels) == {"read"}, labels


def test_role_settings_change_only_the_one_saved_and_show_what_the_hub_holds(owner_env):
    owner = owner_env.connect()
    assert read_role.ensure_read_role(owner)["ok"]
    owner.execute(f"ALTER ROLE {read_role.READ_ROLE} CONNECTION LIMIT 7")  # another Mac's change
    owner.commit()
    read_role.set_setting("read_role_statement_timeout", 5, conn=owner)
    with owner.cursor() as cur:
        cur.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname = %s", (read_role.READ_ROLE,))
        assert cur.fetchone()[0] == 7
    owner.rollback()
    shown = read_role.settings_status(conn=owner)
    assert shown["read_role_connection_limit"]["value"] == 7 and shown["read_role_connection_limit"]["on_hub"]
    assert shown["read_role_statement_timeout"]["value"] == 5
    assert shown["read_role_idle_in_transaction_timeout"]["value"] == 60


def _limited_owner(owner_env, owner, *, createrole: bool) -> str:
    owner.autocommit = True
    owner.execute("DROP ROLE IF EXISTS khipu_cr_owner")
    owner.execute(f"CREATE ROLE khipu_cr_owner LOGIN NOSUPERUSER {'CREATEROLE' if createrole else ''}")
    # It acts for the hub owner's tables the way an owner group would.
    owner.execute("GRANT khipu TO khipu_cr_owner")
    return owner_env.dsn.replace("postgresql://khipu@", "postgresql://khipu_cr_owner@")


def test_a_createrole_owner_without_superuser_provisions_the_role(owner_env, monkeypatch):
    owner = owner_env.connect()
    dsn = _limited_owner(owner_env, owner, createrole=True)
    owner.execute(f"DROP ROLE IF EXISTS {read_role.READ_ROLE}")  # roles outlive a test's database
    try:
        with psycopg.connect(dsn) as limited:
            result = read_role.ensure_read_role(limited)
            assert result["ok"] and result["created"], result
            assert read_role.set_setting("read_role_connection_limit", 3, conn=limited)
        with owner.cursor() as cur:
            cur.execute("SELECT rolsuper, rolcreaterole, rolconnlimit FROM pg_roles WHERE rolname = %s",
                        (read_role.READ_ROLE,))
            assert cur.fetchone() == (False, False, 3)
    finally:
        owner.execute(f"DROP OWNED BY {read_role.READ_ROLE}")
        owner.execute(f"DROP ROLE IF EXISTS {read_role.READ_ROLE}")
        owner.execute("DROP OWNED BY khipu_cr_owner")
        owner.execute("DROP ROLE IF EXISTS khipu_cr_owner")


def test_an_existing_read_role_with_more_rights_is_refused_not_altered(owner_env, monkeypatch):
    owner = owner_env.connect()
    owner.autocommit = True
    owner.execute(f"DROP ROLE IF EXISTS {read_role.READ_ROLE}")
    owner.execute(f"CREATE ROLE {read_role.READ_ROLE} LOGIN CREATEROLE")
    try:
        owner.autocommit = False
        result = read_role.ensure_read_role(owner)
        assert not result["ok"] and result.get("unsafe"), result
        assert "createrole" in result["reason"]
        with owner.cursor() as cur:
            cur.execute("SELECT rolcreaterole FROM pg_roles WHERE rolname = %s", (read_role.READ_ROLE,))
            assert cur.fetchone()[0] is True
        owner.rollback()
        labels = _watch_labels(monkeypatch)
        with pytest.raises(read_role.ReadRoleUnavailable):
            db.connect(role="read")
        assert "owner-read" not in labels
        assert "createrole" in " ".join(read_role.doctor_block(conn=owner)["warnings"])
    finally:
        owner.rollback()
        owner.autocommit = True
        owner.execute(f"DROP ROLE IF EXISTS {read_role.READ_ROLE}")


def _kept_secret(owner) -> str:
    with owner.cursor() as cur:
        cur.execute(f"SELECT secret FROM {read_role.SECRETS_TABLE} WHERE role = %s", (read_role.READ_ROLE,))
        secret = cur.fetchone()[0]
    owner.rollback()
    return secret


def test_a_refused_password_is_rotated_only_while_it_is_still_the_kept_one(owner_env):
    owner = owner_env.connect()
    assert read_role.ensure_read_role(owner)["ok"]
    current = _kept_secret(owner)
    # Another Mac already rotated: the password this one was refused is not kept.
    result = read_role.ensure_read_role(owner, refused_password="an-older-password")
    assert result["ok"] and not result["password_reset"], result
    assert _kept_secret(owner) == current
    result = read_role.ensure_read_role(owner, refused_password=current)
    assert result["ok"] and result["password_reset"], result
    assert _kept_secret(owner) != current


class _Recording:
    """An owner connection that keeps every statement's text and parameters."""

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[tuple[str, object]] = []

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def cursor(self):
        rec = self

        class _Cursor:
            def __init__(self, cur):
                self._cur = cur

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._cur.close()

            def __getattr__(self, name):
                return getattr(self._cur, name)

            def execute(self, query, params=None):
                text = query.as_string(rec._conn) if hasattr(query, "as_string") else str(query)
                rec.statements.append((text, params))
                return self._cur.execute(query, params)

        return _Cursor(self._conn.cursor())


def test_the_password_never_appears_in_statement_text_and_the_hub_keeps_a_scram_verifier(owner_env):
    owner = owner_env.connect()
    recording = _Recording(owner)
    assert read_role.ensure_read_role(recording)["ok"]
    secret = _kept_secret(owner)
    assert not [text for text, _ in recording.statements if secret in text]
    assert ("log_statement", "none", ) == next(p[:2] for t, p in recording.statements if p and "set_config" in t)
    with owner.cursor() as cur:
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (read_role.READ_ROLE,))
        stored = cur.fetchone()[0]
        # The local verifier is the one PostgreSQL computes itself.
        cur.execute("SET password_encryption = 'scram-sha-256'")
        cur.execute("DROP ROLE IF EXISTS khipu_scram_probe")
        cur.execute("CREATE ROLE khipu_scram_probe PASSWORD 'probe-password'")
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = 'khipu_scram_probe'")
        server_made = cur.fetchone()[0]
        cur.execute("DROP ROLE khipu_scram_probe")
    owner.rollback()

    def again(verifier: str, password: str) -> str:
        import base64

        iterations, salt = verifier.split("$")[1].split(":")
        return read_role.scram_verifier(password, salt=base64.b64decode(salt), iterations=int(iterations))

    assert stored.startswith("SCRAM-SHA-256$") and again(stored, secret) == stored
    assert again(server_made, "probe-password") == server_made


def test_the_read_dsn_is_cached_and_the_file_is_preferred_over_the_keychain(owner_env, monkeypatch):
    from khipu import keychain

    asked: list[str] = []
    monkeypatch.setattr(keychain, "get_password", lambda account, **k: asked.append(account))
    with db.connect(role="read") as conn:  # provisions, writes the file
        assert _who(conn)[0] == read_role.READ_ROLE
    asked.clear()  # with no file yet, the Keychain was the place left to look
    read_role.invalidate_cache()
    with db.connect(role="read") as conn:  # from the file, validated
        assert _who(conn)[0] == read_role.READ_ROLE
    assert asked == []
    read_role._read_dsn_file().unlink()
    with db.connect(role="read") as conn:  # from memory: no file, no Keychain
        assert _who(conn)[0] == read_role.READ_ROLE
    assert asked == []
    read_role.invalidate_cache()
    assert read_role.read_conninfo(owner_env.dsn) is None


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
