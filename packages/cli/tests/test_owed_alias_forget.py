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


# --- an exact hidden alias reads as an unknown name -------------------------

def _shape(out):
    return sorted(k for k in out if k not in ("asked",))


def _mcp_owed(project):
    with mock.patch.object(srv, "_RELOAD_ON_CHANGE", False):
        return srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "khipu_owed", "arguments": {"project": project}}})


def test_hub_exact_hidden_alias_reads_as_an_unknown_name(hub_conn):
    _hub_commitment(hub_conn, "public/leaf", "public task", "h-pub")
    _hub_commitment(hub_conn, "secret/leaf", "forgotten task", "h-sec", forgotten=True)
    _hub_alias(hub_conn, "old-secret", "secret/leaf")
    unknown = srv._tool_owed({"project": "never-heard-of-it"})
    out = srv._tool_owed({"project": "old-secret"})
    assert out["project"] == "old-secret" and "asked" not in out
    assert out["results"] == []
    assert _shape(out) == _shape(unknown)
    assert "secret/leaf" not in json.dumps(out, default=str)
    rendered = json.dumps(_mcp_owed("old-secret"))
    assert "secret/leaf" not in rendered and "forgotten task" not in rendered


@pytest.mark.parametrize("alias, hidden", [("old-secret", "secret/leaf"), ("old-pending", "pending/leaf")])
def test_replica_exact_hidden_alias_reads_as_an_unknown_name(replica_dir, alias, hidden):
    con = _replica()
    _alias_rows(con)
    con.close()
    with mock.patch.object(db, "connect", side_effect=psycopg.OperationalError("connection is closed")), \
         mock.patch.object(srv, "_routing_direction_rows", return_value=[]):
        unknown = srv.owed_listing({"project": "never-heard-of-it"})
        out = srv.owed_listing({"project": alias})
    assert out["project"] == alias and "asked" not in out
    assert out["results"] == []
    assert _shape(out) == _shape(unknown)
    assert hidden not in json.dumps(out, default=str)
    with mock.patch.object(db, "connect", side_effect=psycopg.OperationalError("connection is closed")):
        rendered = json.dumps(_mcp_owed(alias))
    assert hidden not in rendered


# --- a fence landing between hub resolution and the replica fallback --------

@pytest.mark.parametrize("surface", ["listing", "mcp", "cli"])
@pytest.mark.parametrize("kind", ["forgotten", "pending"])
def test_fallback_after_hub_resolution_never_names_a_newly_hidden_project(hub_conn, capsys, kind, surface):
    from khipu import cli, commitments

    hidden = "secret/leaf"
    with hub_conn.cursor() as cur:
        fact_seed.announce(cur)
        cur.execute("INSERT INTO episodes (ts, session_id, summary, project) "
                    "VALUES (now(), 's-mid', 'live for now', %s) RETURNING id", (hidden,))
        ep = cur.fetchone()[0]
        cur.execute("INSERT INTO commitments (text, project, owner, content_hash, opened_episode) "
                    "VALUES ('soon forgotten', %s, 'user', 'h-mid', %s) RETURNING id", (hidden, ep))
        fact_seed.adopt(cur, "commitment", cur.fetchone()[0])
        cur.execute("INSERT INTO project_aliases (alias, project) VALUES ('old-secret', %s)", (hidden,))
    hub_conn.commit()
    con = sqlite3.connect(hs.snapshot_path())
    hs._create_schema(con)
    con.execute("INSERT INTO episodes(id, summary, deleted_at) VALUES (?, 'live for now', NULL)", (ep,))
    con.execute("INSERT INTO commitments(id, text, project, opened_episode, status, owner) "
                "VALUES (1, 'soon forgotten', ?, ?, 'open', 'user')", (hidden, ep))
    con.execute("INSERT INTO project_aliases(alias, project) VALUES ('old-secret', ?)", (hidden,))
    certify(con)
    con.commit()
    con.close()

    def fence_then_disconnect(*a, **k):
        # The hub already read OLD-SECRET as secret/leaf; now the forget (or
        # a queued erasure) lands on the replica and the hub goes away.
        if kind == "forgotten":
            with sqlite3.connect(hs.snapshot_path()) as rcon:
                rcon.execute("UPDATE episodes SET deleted_at = '2026-10-27' WHERE id = ?", (ep,))
        else:
            hs._queue_erasure(ep)
        raise psycopg.OperationalError("connection is closed")

    asked = "OLD-SECRET"
    with mock.patch.object(commitments, "fetch_owed_rows", side_effect=fence_then_disconnect), \
         mock.patch.object(srv, "_routing_direction_rows", return_value=[]):
        if surface == "listing":
            out = srv.owed_listing({"project": asked})
            assert out["project"] == asked and "asked" not in out and out["results"] == []
            rendered = json.dumps(out, default=str)
        elif surface == "mcp":
            rendered = json.dumps(_mcp_owed(asked))
        else:
            args = cli.build_parser().parse_args(["owed", "--project", asked, "--json"])
            assert args.func(args) == 0
            rendered = capsys.readouterr().out
            assert json.loads(rendered)["project"] == asked
    assert hidden not in rendered and "soon forgotten" not in rendered


# --- a fence landing between name resolution and the row read ---------------
# Each backend resolves the name and reads the rows in one snapshot, so the
# response is wholly from before the fence (canonical + its row) or wholly
# after it (the name as typed, empty) -- never a canonical over hidden rows.

def _surface(surface, asked, capsys):
    from khipu import cli

    if surface == "listing":
        out = srv.owed_listing({"project": asked})
        return out, json.dumps(out, default=str)
    if surface == "mcp":
        response = _mcp_owed(asked)
        return json.loads(response["result"]["content"][0]["text"]), json.dumps(response)
    args = cli.build_parser().parse_args(["owed", "--project", asked, "--json"])
    assert args.func(args) == 0
    text = capsys.readouterr().out
    return json.loads(text), text


def _assert_one_snapshot(out, rendered, hidden, asked, row_text):
    if hidden in rendered:
        assert out["project"] == hidden and [r["text"] for r in out["results"]] == [row_text]
    else:
        assert out["project"] == asked and out["results"] == []


@pytest.mark.parametrize("surface", ["listing", "mcp", "cli"])
def test_hub_forget_between_resolution_and_rows_never_names_the_project(hub, hub_conn, capsys, surface):  # noqa: F811
    hidden, asked, text = "secret/leaf", "OLD-SECRET", "visible until now"
    with hub_conn.cursor() as cur:
        fact_seed.announce(cur)
        cur.execute("INSERT INTO episodes (ts, session_id, summary, project) "
                    "VALUES (now(), 's-rr', 'live', %s) RETURNING id", (hidden,))
        ep = cur.fetchone()[0]
        cur.execute("INSERT INTO commitments (text, project, owner, content_hash, opened_episode) "
                    "VALUES (%s, %s, 'user', 'h-rr', %s) RETURNING id", (text, hidden, ep))
        cid = cur.fetchone()[0]
        fact_seed.adopt(cur, "commitment", cid)
        cur.execute("INSERT INTO project_aliases (alias, project) VALUES ('old-secret', %s)", (hidden,))
    hub_conn.commit()
    real = identity.resolve_project

    def resolve_then_forget(cur, project):
        answer = real(cur, project)
        other = hub.connect()
        with other.cursor() as oc:
            oc.execute("DELETE FROM fact_claims WHERE kind = 'commitment' AND fact_id = %s", (cid,))
            oc.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (ep,))
        other.commit()
        other.close()
        return answer

    with mock.patch.object(identity, "resolve_project", side_effect=resolve_then_forget), \
         mock.patch.object(srv, "_routing_direction_rows", return_value=[]):
        out, rendered = _surface(surface, asked, capsys)
    _assert_one_snapshot(out, rendered, hidden, asked, text)
    # The forget did land: the next read is wholly after it.
    with mock.patch.object(srv, "_routing_direction_rows", return_value=[]):
        after = srv.owed_listing({"project": asked})
    assert after["project"] == asked and after["results"] == []


@pytest.mark.parametrize("surface", ["listing", "mcp", "cli"])
@pytest.mark.parametrize("kind", ["forgotten", "pending"])
def test_replica_fence_between_resolution_and_rows_never_names_the_project(replica_dir, capsys, kind, surface):
    import threading

    hidden, asked, text = "secret/leaf", "OLD-SECRET", "visible until now"
    con = sqlite3.connect(hs.snapshot_path())
    hs._create_schema(con)
    con.execute("INSERT INTO episodes(id, summary, deleted_at) VALUES (7, 'live', NULL)")
    con.execute("INSERT INTO commitments(id, text, project, opened_episode, status, owner) "
                "VALUES (1, ?, ?, 7, 'open', 'user')", (text, hidden))
    con.execute("INSERT INTO project_aliases(alias, project) VALUES ('old-secret', ?)", (hidden,))
    certify(con)
    con.commit()
    con.close()
    real = hs.snapshot_resolve_project_known
    late: list[threading.Thread] = []

    def forget(timeout):
        with sqlite3.connect(hs.snapshot_path(), timeout=timeout) as w:
            w.execute("UPDATE episodes SET deleted_at = '2026-10-27' WHERE id = 7")

    def resolve_then_fence(con, project, **kw):
        answer = real(con, project, **kw)
        if kind == "pending":
            hs._queue_erasure(7)
        else:
            try:
                forget(0)
            except sqlite3.OperationalError:
                # The read holds the replica: the writer waits it out, as a pull would.
                t = threading.Thread(target=forget, args=(30,))
                t.start()
                late.append(t)
        return answer

    with mock.patch.object(db, "connect", side_effect=psycopg.OperationalError("connection is closed")), \
         mock.patch.object(hs, "snapshot_resolve_project_known", side_effect=resolve_then_fence), \
         mock.patch.object(srv, "_routing_direction_rows", return_value=[]):
        out, rendered = _surface(surface, asked, capsys)
    for t in late:
        t.join(30)
    _assert_one_snapshot(out, rendered, hidden, asked, text)
    with mock.patch.object(db, "connect", side_effect=psycopg.OperationalError("connection is closed")), \
         mock.patch.object(srv, "_routing_direction_rows", return_value=[]):
        after = srv.owed_listing({"project": asked})
    assert after["project"] == asked and after["results"] == []
