"""The autocommit step marker and migration 0040 (idx_episodes_id_text):
absent is created, valid is adopted without a rebuild, invalid is rebuilt, a
different definition stops with a named error and records nothing."""
from __future__ import annotations

import pytest

from khipu import migrate
from tests.hermetic_pg import MIGRATIONS, hub, pg_cluster  # noqa: F401

VERSION = "0040_episodes_id_text_index"
NAME = "idx_episodes_id_text"


def _state(conn):
    row = conn.execute(
        "SELECT i.indexrelid::oid::bigint, i.indisvalid, pg_get_indexdef(i.indexrelid) "
        "FROM pg_index i WHERE i.indexrelid = to_regclass(%s)", (NAME,)).fetchone()
    conn.commit()
    return row


def _recorded(conn):
    n = conn.execute("SELECT count(*) FROM schema_migrations WHERE version = %s",
                     (VERSION,)).fetchone()[0]
    conn.commit()
    return n == 1


def _rewind(conn):
    """Back to the state before 0040 ran: version unrecorded, index dropped."""
    conn.execute("DELETE FROM schema_migrations WHERE version = %s", (VERSION,))
    conn.execute(f"DROP INDEX IF EXISTS {NAME}")
    conn.commit()


@pytest.fixture
def conn(hub):  # noqa: F811
    c = hub.connect()
    migrate.run(conn=c, directory=MIGRATIONS)
    yield c


def test_the_marker_makes_the_section_autocommit():
    sql = (MIGRATIONS / f"{VERSION}.sql").read_text(encoding="utf-8")
    modes = migrate.step_modes(sql)
    assert [m for m, _ in modes] == [True]
    assert migrate.steps("SELECT 1;\n-- khipu:step\nSELECT 2;\n") == [
        "SELECT 1;\n", "\nSELECT 2;\n"]
    assert [m for m, _ in migrate.step_modes("SELECT 1;\n-- khipu:step\nSELECT 2;\n")] == [
        False, False]


def test_absent_index_is_created_and_recorded(conn):
    assert _state(conn)[1] is True and _recorded(conn)
    _rewind(conn)
    assert _state(conn) is None
    assert migrate.run(conn=conn, directory=MIGRATIONS)["ran"] == [VERSION]
    state = _state(conn)
    assert state[1] is True and "(((id)::text))" in state[2]
    assert _recorded(conn)
    assert conn.autocommit is False


def test_valid_matching_index_is_adopted_without_a_rebuild(conn):
    before = _state(conn)
    conn.execute("DELETE FROM schema_migrations WHERE version = %s", (VERSION,))
    conn.commit()
    assert migrate.run(conn=conn, directory=MIGRATIONS)["ran"] == [VERSION]
    assert _state(conn) == before  # same relation oid: not dropped and rebuilt
    assert _recorded(conn)


def test_invalid_index_is_rebuilt(conn):
    before = _state(conn)
    conn.execute("DELETE FROM schema_migrations WHERE version = %s", (VERSION,))
    conn.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(%s)",
                 (NAME,))
    conn.commit()
    assert _state(conn)[1] is False
    migrate.run(conn=conn, directory=MIGRATIONS)
    after = _state(conn)
    assert after[1] is True and after[2] == before[2]
    assert after[0] != before[0]  # a new relation: dropped and rebuilt
    assert _recorded(conn)


def test_a_different_definition_stops_and_records_nothing(conn):
    _rewind(conn)
    conn.execute(f"CREATE INDEX {NAME} ON episodes (id)")
    conn.commit()
    before = _state(conn)
    with pytest.raises(migrate.MigrationIndexError, match=NAME):
        migrate.run(conn=conn, directory=MIGRATIONS)
    assert _state(conn) == before  # left exactly as found
    assert not _recorded(conn)
    assert conn.autocommit is False
