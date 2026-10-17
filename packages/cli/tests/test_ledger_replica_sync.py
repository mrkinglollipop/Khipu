"""Work slice 2 regressions. Every hub response and replica is synthetic."""
import re
import sqlite3
from datetime import datetime, timezone

import pytest
from psycopg import IsolationLevel

from khipu import hub_snapshot as hs, ledger
from tests.test_ledger import _replica_row
from tests.test_replica_pull_integrity import PullCursor, PullHub, replica  # noqa: F401


class LedgerCursor(PullCursor):
    def __init__(self, hub):
        super().__init__(hub)
        self.database = hub.ledger
        if hub.isolation_level == IsolationLevel.REPEATABLE_READ:
            hub.ledger.commit()
            self.database = sqlite3.connect(':memory:')
            hub.ledger.backup(self.database)

    def __exit__(self, *_):
        if self.database is not self.hub.ledger:
            self.database.close()
        return False

    def execute(self, sql, params=()):
        if 'ledger_' not in sql:
            return super().execute(sql, params)
        self.hub.queries.append(sql)
        values = iter(params)
        bound = []

        def placeholder(match):
            value = next(values)
            if match.group() == '= ANY(%s)':
                bound.extend(value)
                return 'IN (' + ','.join('?' for _ in value) + ')'
            bound.append(value)
            return '?'

        sql = re.sub(r'= ANY\(%s\)|%s', placeholder, sql)
        cursor = self.database.execute(sql, bound)
        self.rowcount = cursor.rowcount
        self._rows = cursor.fetchall()
        if self.hub.remove_after_rows and 'FROM ledger_rows WHERE removed_at IS NULL' in sql:
            self.hub.ledger.execute("UPDATE ledger_rows SET removed_at='removed', version=2")
            self.hub.remove_after_rows = False


class LedgerHub(PullHub):
    remove_after_rows = False
    isolation_level = None

    def cursor(self):
        return LedgerCursor(self)

    def rollback(self):
        pass

    def commit(self):
        self.ledger.commit()

    def close(self):
        pass


@pytest.fixture
def world(replica, monkeypatch):
    hub = LedgerHub()
    hub.__dict__.update(replica.__dict__)
    hub.ledger = sqlite3.connect(':memory:')
    hub.ledger.executescript(hs._LEDGER_SCHEMA_SQL)
    for col in ('removed_at', 'removed_by', 'removed_note'):
        hub.ledger.execute(f'ALTER TABLE ledger_rows ADD COLUMN {col} TEXT')
    hub.ledger.execute('CREATE TABLE ledger_removed(project TEXT, row_id TEXT, purged_at TEXT)')
    hub.ledger.execute("INSERT INTO ledger_projects VALUES ('p','Old',0,'2026-10-01')")
    row = {**_replica_row('r', 1), 'project': 'p'}
    hub.ledger.execute(f"INSERT INTO ledger_rows ({', '.join(hs._LEDGER_ROW_COLS)}) VALUES ({','.join('?' for _ in hs._LEDGER_ROW_COLS)})",
                       [hs._ledger_value(c, row[c]) for c in hs._LEDGER_ROW_COLS])
    hub.columns['ledger_rows'] = {*hs._LEDGER_ROW_COLS, 'removed_at'}
    monkeypatch.setattr(hs, 'try_hub_connect', lambda **_: hub)
    monkeypatch.setattr(ledger, '_connect', lambda: hub)
    with sqlite3.connect(hs.snapshot_path()) as con:
        hs._insert_ledger(hub.cursor(), con)
    yield hub
    hub.ledger.close()


def test_reader_suppresses_row_and_history_behind_newer_tombstone(world):
    with sqlite3.connect(hs.snapshot_path()) as con:
        con.execute("INSERT INTO ledger_gone VALUES ('p','r',2)")
        con.execute("INSERT INTO ledger_history(id,project,row_id) VALUES (1,'p','r')")
    got = hs.ledger_read_snapshot('p')
    assert got['rows'] == []
    assert got['history'] == []


def test_full_dump_uses_one_repeatable_read_snapshot(world, monkeypatch):
    world.remove_after_rows = True
    for name in ('_insert_episodes', '_insert_topics', '_insert_topic_revisions', '_insert_nodes',
                 '_insert_edges', '_insert_profiles', '_insert_memory_embeddings', '_insert_decisions',
                 '_insert_deliverables', '_insert_commitments'):
        monkeypatch.setattr(hs, name, lambda *_: 0)
    assert hs.refresh()['ok']
    assert world.isolation_level == IsolationLevel.REPEATABLE_READ
    got = hs.ledger_read_snapshot('p')
    assert world.ledger.execute('SELECT version FROM ledger_rows').fetchone() == (2,)
    assert [r['version'] for r in got['rows']] == [1]
    assert got['gone'] == {}


def test_general_pull_does_not_claim_ledger_freshness(world, monkeypatch):
    before = hs.ledger_read_snapshot('p')['as_of']
    # An older pull implementation can have a perfectly recent general stamp.
    side = hs._read_sidecar()
    side.update(pull_dump=side['hub_dumped_at'], pull_caught_up_at=datetime.now(timezone.utc).isoformat())
    hs._write_meta(side)
    assert hs.ledger_read_snapshot('p')['as_of'] == before


def test_incremental_pull_copies_foreign_version_history_metadata_and_removals(world):
    world.ledger.execute("UPDATE ledger_rows SET title='Foreign', version=2 WHERE row_id='r'")
    world.ledger.execute("INSERT INTO ledger_history(id,project,row_id,note) VALUES (1,'p','r','first')")
    world.ledger.execute("UPDATE ledger_projects SET name='New', hidden=1")
    world.ledger.execute("INSERT INTO ledger_vocab VALUES ('p','state','GO','active',0)")
    before = hs.ledger_read_snapshot('p')['as_of']
    assert hs.pull_changes()['ok']
    got = hs.ledger_read_snapshot('p')
    assert [(r['title'], r['version']) for r in got['rows']] == [('Foreign', 2)]
    assert [h['note'] for h in got['history']] == ['first']
    assert got['vocab']['state'] == [['GO', 'active']]
    assert hs._utc(got['as_of']) > hs._utc(before)
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name,hidden FROM ledger_projects').fetchall() == [('New', 1)]
    # History can change without incrementing the row version.
    world.ledger.execute("INSERT INTO ledger_history(id,project,row_id,note) VALUES (2,'p','r','late')")
    assert hs.pull_changes()['ok']
    assert len(hs.ledger_read_snapshot('p')['history']) == 2
    world.ledger.execute("UPDATE ledger_rows SET removed_at='removed', version=3")
    assert hs.pull_changes()['ok']
    assert hs.ledger_read_snapshot('p')['rows'] == []
    world.ledger.execute('DELETE FROM ledger_rows')
    world.ledger.execute("INSERT INTO ledger_removed VALUES ('p','r','purged')")
    assert hs.pull_changes()['ok']
    assert hs.ledger_read_snapshot('p')['gone']['r'] == hs.LEDGER_PURGED_VERSION


@pytest.mark.parametrize('changes', [{'name': 'Renamed'}, {'hidden': True}, {'hidden': False}])
def test_project_change_uses_retryable_replica_pending_path(world, monkeypatch, changes):
    # Stub only catch-up: a refresh can legitimately hold the non-blocking lock.
    monkeypatch.setattr(ledger, 'replica_catch_up', lambda _: {'busy': True})
    ledger.set_project('p', **changes)
    assert ledger.replica_pending() == 1
    with ledger._replica_pending() as pending:
        assert 'p#' in pending  # project-only marker, including an empty project


def test_project_change_retries_after_refresh_lock_is_released(world):
    lock = hs._acquire_refresh_lock()
    try:
        assert ledger.set_project('p', name='Renamed', hidden=True)['ok']
        assert ledger.replica_pending() == 1
    finally:
        hs._release_refresh_lock(lock)
    assert ledger.replica_catch_up(world)['left'] == 0
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name, hidden FROM ledger_projects').fetchall() == [('Renamed', 1)]


def test_ledger_pull_limit_refuses_before_any_replica_write(world):
    world.ledger.execute("UPDATE ledger_rows SET title='Foreign', version=2")
    row = {**_replica_row('second', 1), 'project': 'p'}
    world.ledger.execute(f"INSERT INTO ledger_rows ({', '.join(hs._LEDGER_ROW_COLS)}) VALUES ({','.join('?' for _ in hs._LEDGER_ROW_COLS)})",
                         [hs._ledger_value(c, row[c]) for c in hs._LEDGER_ROW_COLS])
    out = hs.pull_changes(limit=1)
    assert not out['ok'] and out['needs_dump']
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute("SELECT body FROM topics WHERE slug='probe'").fetchone() == ('original',)
    assert hs.ledger_read_snapshot('p')['rows'][0]['version'] == 1


def test_ledger_pull_keeps_newer_local_row_and_purge_marker(world):
    assert hs.ledger_write_snapshot('p', None, {}, [_replica_row('r', 4, 'Newer')], [], ['r'])['ok']
    assert hs.pull_changes()['ok']
    assert hs.ledger_read_snapshot('p')['rows'][0]['version'] == 4
    assert hs.ledger_write_snapshot('p', None, {}, [], [], ['r'], gone={'r': hs.LEDGER_PURGED_VERSION})['ok']
    assert hs.pull_changes()['ok']
    assert hs.ledger_read_snapshot('p')['rows'] == []


def test_failed_ledger_pull_does_not_advance_ledger_freshness(world, monkeypatch):
    before = hs.ledger_read_snapshot('p')['as_of']
    monkeypatch.setattr(hs, '_acquire_refresh_lock', lambda: None)
    world.ledger.execute("UPDATE ledger_rows SET version=2")
    out = hs.pull_changes()
    assert not out['ok'] and 'ledger' in out['failed']
    assert hs.ledger_read_snapshot('p')['as_of'] == before


def test_reader_keeps_a_restoration_newer_than_its_removal(world):
    with sqlite3.connect(hs.snapshot_path()) as con:
        con.execute("INSERT INTO ledger_gone VALUES ('p','r',1)")
        con.execute("UPDATE ledger_rows SET version=2")
    assert hs.ledger_read_snapshot('p')['rows'][0]['version'] == 2


def test_project_change_updates_an_empty_projects_metadata(world):
    world.ledger.execute('DELETE FROM ledger_rows')
    assert ledger.set_project('p', name='Empty', hidden=True)['ok']
    assert ledger.replica_pending() == 0
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name,hidden FROM ledger_projects').fetchall() == [('Empty', 1)]


def test_incremental_pull_fetches_only_rows_with_changed_versions_or_history(world):
    world.queries.clear()
    assert hs.pull_changes()['ok']
    assert not any('refs' in q and 'FROM ledger_rows' in q for q in world.queries)
    world.ledger.execute("UPDATE ledger_rows SET version=2")
    world.queries.clear()
    assert hs.pull_changes()['ok']
    assert any('refs' in q and 'FROM ledger_rows' in q for q in world.queries)


def test_older_pull_cannot_regress_write_through_project_metadata(world, monkeypatch):
    original = hs._write_ledger_pull if hasattr(hs, '_write_ledger_pull') else None
    if original is None:
        pytest.fail('incremental pulls have no ledger part')

    def newer_write_then_pull(payload, *, read_at):
        assert hs.ledger_write_snapshot('p', ('Newest', True, None), {}, [], [], [])['ok']
        return original(payload, read_at=read_at)

    monkeypatch.setattr(hs, '_write_ledger_pull', newer_write_then_pull)
    assert hs.pull_changes()['ok']
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name,hidden FROM ledger_projects').fetchall() == [('Newest', 1)]


def test_precommit_project_marker_consumed_by_older_catchup_is_requeued(world, monkeypatch):
    commit = world.commit

    def commit_after_older_catchup():
        # A catch-up on another connection read the old committed project.
        # It can consume this token before the rename's commit is visible.
        with ledger._replica_pending() as pending:
            assert 'p#' in pending
            pending.clear()
        commit()

    monkeypatch.setattr(world, 'commit', commit_after_older_catchup)
    assert ledger.set_project('p', name='Committed', hidden=True)['ok']
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name,hidden FROM ledger_projects').fetchall() == [('Committed', 1)]
