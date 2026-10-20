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
    _drop_read_role(hub.connect())
    return hub


def _who(conn) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_user, current_setting('application_name')")
        return cur.fetchone()


def _drop_read_role(conn) -> None:
    """Roles are the whole cluster's and outlive a test's database."""
    conn.rollback()
    was = conn.autocommit
    conn.autocommit = True
    try:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (read_role.READ_ROLE,)).fetchone():
            conn.execute(f"DROP OWNED BY {read_role.READ_ROLE}")
            conn.execute(f"DROP ROLE {read_role.READ_ROLE}")
    finally:
        conn.autocommit = was


def _pw_dsn(owner_env, password: str = "owner-secret") -> str:
    # Trust auth ignores it; the derivation and the verifier use it.
    return owner_env.dsn.replace("postgresql://khipu@", f"postgresql://khipu:{password}@")


def _verifies(verifier: str, password: str) -> bool:
    import base64

    iterations, salt = verifier.split("$")[1].split(":")
    return read_role.scram_verifier(password, salt=base64.b64decode(salt), iterations=int(iterations)) == verifier


def _rolpassword(owner) -> str | None:
    with owner.cursor() as cur:
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (read_role.READ_ROLE,))
        row = cur.fetchone()
    owner.rollback()
    return row[0] if row else None


def test_the_read_role_is_select_only_and_no_password_is_kept_on_the_hub(owner_env, monkeypatch):
    owner = owner_env.connect()
    # A table an earlier build kept the plaintext in is removed.
    owner.execute(f"CREATE TABLE {read_role.LEGACY_SECRETS_TABLE} (role text, secret text)")
    owner.commit()
    dsn = _pw_dsn(owner_env)
    monkeypatch.setenv("KHIPU_DATABASE_URL", dsn)
    result = read_role.ensure_read_role(owner, owner_dsn=dsn)
    assert result["ok"] and result["created"], result
    with psycopg.connect(read_role.read_conninfo_for(dsn)) as reader, reader.cursor() as cur:
        cur.execute("SELECT count(*) FROM episodes")
        cur.fetchone()
        reader.rollback()
        with pytest.raises(psycopg.Error):
            cur.execute("INSERT INTO memory_query_cache (profile, query_hash, embedding) VALUES ('x', 'y', NULL)")
        reader.rollback()
    with owner.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (read_role.LEGACY_SECRETS_TABLE,))
        assert cur.fetchone()[0] is None
        cur.execute("SELECT rolsuper, rolconnlimit, rolconfig FROM pg_roles WHERE rolname = %s", (read_role.READ_ROLE,))
        sup, limit, config = cur.fetchone()
    owner.rollback()
    assert sup is False and limit == 20
    assert "statement_timeout=30000ms" in config
    assert "idle_in_transaction_session_timeout=60000ms" in config
    assert _verifies(_rolpassword(owner), read_role.derived_password(dsn))


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


def test_setup_creates_the_role_and_search_needs_nothing_stored(owner_env):
    from khipu import setup

    owner = owner_env.connect()
    entry = setup._store_read_role(owner, owner_env.dsn)
    assert entry["ok"], entry
    assert not (read_role._memo_file().parent / "read_dsn").exists()
    with db.connect(role="read") as conn:
        user, app = _who(conn)
    assert user == read_role.READ_ROLE
    assert app.startswith("khipu/read/")


def test_an_upgraded_install_provisions_on_first_search_and_a_second_mac_needs_no_owner_step(
        owner_env, monkeypatch):
    with db.connect(role="read") as conn:
        assert _who(conn)[0] == read_role.READ_ROLE
    # Another Mac (another process): it derives the same password and logs in
    # without any owner connection.
    read_role.invalidate_cache()
    labels = _watch_labels(monkeypatch)
    with db.connect(role="read") as conn:
        assert _who(conn)[0] == read_role.READ_ROLE
    assert labels == ["read"], labels
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
        assert "cannot create roles" in (read_role.cannot_host(limited) or "")
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
    assert "owner-read" not in labels and labels.count("owner") == 1, labels


def test_a_refused_password_that_cannot_be_repaired_never_falls_back_to_the_owner(owner_env, monkeypatch):
    with db.connect(role="read") as conn:
        assert _who(conn)[0] == read_role.READ_ROLE
    assert read_role._VALIDATED
    labels = _watch_labels(monkeypatch, refuse_read=True)
    with pytest.raises(read_role.ReadRoleUnavailable):
        db.connect(role="read")
    assert "owner-read" not in labels, labels
    assert not read_role._VALIDATED
    # Recorded: the next search does not open an owner connection to retry.
    labels.clear()
    with pytest.raises(read_role.ReadRoleUnavailable):
        db.connect(role="read")
    assert labels == ["read"], labels


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


def test_a_repair_sets_the_same_derived_password_whichever_client_runs_it(owner_env, monkeypatch):
    owner = owner_env.connect()
    dsn = _pw_dsn(owner_env)
    assert read_role.ensure_read_role(owner, owner_dsn=dsn)["ok"]
    first = _rolpassword(owner)
    # Two clients repairing at once set verifiers for one password.
    assert read_role.ensure_read_role(owner, owner_dsn=dsn, repair=True)["password_reset"]
    assert read_role.ensure_read_role(owner, owner_dsn=dsn, repair=True)["password_reset"]
    assert _verifies(_rolpassword(owner), read_role.derived_password(dsn))
    assert _verifies(first, read_role.derived_password(dsn))
    # It follows the owner password, and the hub's name for it does not matter.
    assert read_role.derived_password(_pw_dsn(owner_env, "rotated")) != read_role.derived_password(dsn)
    assert read_role.derived_password(dsn.replace("127.0.0.1", "hub.example")) == read_role.derived_password(dsn)
    # A setting change leaves the password alone.
    before = _rolpassword(owner)
    read_role.set_setting("read_role_statement_timeout", 9, conn=owner)
    assert _rolpassword(owner) == before


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


def test_no_password_is_ever_sent_to_the_hub_and_the_verifier_is_postgresqls_own(owner_env, monkeypatch):
    owner = owner_env.connect()
    dsn = _pw_dsn(owner_env)
    recording = _Recording(owner)
    assert read_role.ensure_read_role(recording, owner_dsn=dsn)["ok"]
    assert read_role.ensure_read_role(recording, owner_dsn=dsn, repair=True)["ok"]
    password = read_role.derived_password(dsn)
    assert not [t for t, p in recording.statements if password in t or password in repr(p)]
    assert not [t for t, _ in recording.statements if read_role.LEGACY_SECRETS_TABLE in t and "DROP" not in t]
    assert _verifies(_rolpassword(owner), password)
    with owner.cursor() as cur:
        cur.execute("SET password_encryption = 'scram-sha-256'")
        cur.execute("DROP ROLE IF EXISTS khipu_scram_probe")
        cur.execute("CREATE ROLE khipu_scram_probe PASSWORD 'probe-password'")
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = 'khipu_scram_probe'")
        server_made = cur.fetchone()[0]
        cur.execute("DROP ROLE khipu_scram_probe")
    owner.rollback()
    assert _verifies(server_made, "probe-password")


def test_the_read_password_is_scrypt_v1_derived_once_per_process(owner_env, monkeypatch):
    import base64
    import hashlib
    import hmac

    dsn = _pw_dsn(owner_env, "a-fresh-owner-password")
    calls: list[bytes] = []
    real = hashlib.scrypt
    monkeypatch.setattr(hashlib, "scrypt", lambda pw, **kw: calls.append(kw["salt"]) or real(pw, **kw))
    first = read_role.derived_password(dsn)
    assert read_role.derived_password(dsn) == first and read_role.read_conninfo_for(dsn)
    assert calls == [b"khipu_read/v1/khipu"]
    old_hmac = base64.urlsafe_b64encode(hmac.new(b"a-fresh-owner-password", b"khipu_read/khipu", "sha256")
                                        .digest()).decode("ascii").rstrip("=")
    assert first != old_hmac
    assert first == base64.urlsafe_b64encode(real(b"a-fresh-owner-password", salt=b"khipu_read/v1/khipu",
                                                  n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
                                             ).decode("ascii").rstrip("=")


def test_an_owner_dsn_without_a_password_gives_the_read_role_none(owner_env):
    owner = owner_env.connect()
    assert read_role.derived_password(owner_env.dsn) is None
    assert read_role.ensure_read_role(owner, owner_dsn=owner_env.dsn)["ok"]
    assert _rolpassword(owner) is None
    assert "password" not in read_role.read_conninfo_for(owner_env.dsn)


def test_the_read_check_is_once_per_process_and_needs_no_file_or_keychain(owner_env, monkeypatch):
    from khipu import keychain

    asked: list[str] = []
    monkeypatch.setattr(keychain, "get_password", lambda account, **k: asked.append(account))
    with db.connect(role="read"):
        pass
    checks: list[int] = []
    real = read_role._role_attributes
    monkeypatch.setattr(read_role, "_role_attributes", lambda cur: checks.append(1) or real(cur))
    with db.connect(role="read"), db.connect(role="read"):
        pass
    assert checks == [] and asked == []
    read_role.invalidate_cache()
    with db.connect(role="read"):
        pass
    assert checks == [1]


def test_a_failed_lookup_is_never_read_as_a_hub_that_cannot_host(owner_env, monkeypatch):
    owner = owner_env.connect()
    owner.autocommit = True
    owner.execute("DROP ROLE IF EXISTS khipu_limited_owner")
    owner.execute("CREATE ROLE khipu_limited_owner LOGIN NOSUPERUSER NOCREATEROLE")
    limited = owner_env.dsn.replace("postgresql://khipu@", "postgresql://khipu_limited_owner@")
    monkeypatch.setenv("KHIPU_DATABASE_URL", limited)

    def flaky(cur):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(read_role, "_role_attributes", flaky)
    labels = _watch_labels(monkeypatch)
    try:
        with pytest.raises(read_role.ReadRoleUnavailable):
            db.connect(role="read")
        assert "owner-read" not in labels
        assert read_role.cannot_host(limited) is None
    finally:
        owner.execute("DROP ROLE IF EXISTS khipu_limited_owner")


def test_an_elevated_existing_role_is_refused_even_when_its_password_works(owner_env, monkeypatch):
    owner = owner_env.connect()
    with db.connect(role="read"):
        pass
    owner.autocommit = True
    owner.execute(f"ALTER ROLE {read_role.READ_ROLE} CREATEROLE")  # widened by someone else
    read_role.invalidate_cache()
    labels = _watch_labels(monkeypatch)
    with pytest.raises(read_role.ReadRoleUnavailable) as err:
        db.connect(role="read")
    assert "createrole" in str(err.value)
    assert "owner-read" not in labels and "owner" not in labels, labels


def test_the_cannot_host_memo_is_per_hub_and_owner_and_clears_when_the_owner_gains_the_right(
        owner_env, monkeypatch):
    other = owner_env.dsn.replace("postgresql://khipu@", "postgresql://someone_else@")
    read_role._note(other, {"cannot_host_at": time.time(), "reason": "the owner role cannot create roles"})
    assert read_role.cannot_host(other) and read_role.cannot_host(owner_env.dsn) is None
    with db.connect(role="read") as conn:  # this hub and owner: the read role
        assert _who(conn)[0] == read_role.READ_ROLE
    read_role.invalidate_cache(other)
    assert read_role.cannot_host(other) is None
    # A stale memo for this owner, who can create roles: the owner-read
    # connection it gives drops it, and the next search is the read role.
    read_role._note(owner_env.dsn, {"cannot_host_at": time.time(), "reason": "stale"})
    read_role._VALIDATED.clear()
    with db.connect(role="read") as conn:
        assert _who(conn)[1].startswith("khipu/owner-read/")
    assert read_role.cannot_host(owner_env.dsn) is None
    with db.connect(role="read") as conn:
        assert _who(conn)[0] == read_role.READ_ROLE


def test_an_isolated_run_uses_the_read_role_and_never_the_owner(owner_env, monkeypatch, tmp_path):
    from khipu.paths import dsn_file

    with db.connect(role="read"):
        pass
    # The promotion gate's home holds no read credentials of its own.
    dsn_file().with_name("read_dsn").unlink(missing_ok=True)
    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(tmp_path))
    read_role.invalidate_cache()
    with db.connect(role="read") as conn:
        assert _who(conn)[0] == read_role.READ_ROLE
    # The role refused and nothing may be set up: an explicit failure.
    owner = owner_env.connect()
    _drop_read_role(owner)
    read_role.invalidate_cache()
    labels = _watch_labels(monkeypatch)
    with pytest.raises(read_role.ReadRoleUnavailable):
        db.connect(role="read")
    assert "owner-read" not in labels, labels


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
