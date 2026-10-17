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


def test_ledger_pull_limit_only_refuses_the_ledger(world):
    world.ledger.execute("UPDATE ledger_rows SET title='Foreign', version=2")
    row = {**_replica_row('second', 1), 'project': 'p'}
    world.ledger.execute(f"INSERT INTO ledger_rows ({', '.join(hs._LEDGER_ROW_COLS)}) VALUES ({','.join('?' for _ in hs._LEDGER_ROW_COLS)})",
                         [hs._ledger_value(c, row[c]) for c in hs._LEDGER_ROW_COLS])
    out = hs.pull_changes(limit=1)
    assert not out['ok'] and out['failed'] == ['ledger']
    assert out['ledger']['needs_refresh'] and not out.get('needs_dump')
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute("SELECT body FROM topics WHERE slug='probe'").fetchone() == ('corrected',)
    assert hs._utc(hs.caught_up_at()) > hs._utc(hs.ledger_read_snapshot('p')['as_of'])
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


@pytest.mark.parametrize('failure', ['missing_gone', 'locked_read', 'write_error', 'busy_write'])
def test_ledger_failure_does_not_stall_memory_or_its_freshness(world, monkeypatch, failure):
    before = hs.ledger_read_snapshot('p')['as_of']
    world.episodes.append({'id': 2, 'summary': 'foreign memory',
                           'ingested_at': datetime.now(timezone.utc), 'deleted_at': None})
    if failure == 'missing_gone':
        with sqlite3.connect(hs.snapshot_path()) as con:
            con.execute('DROP TABLE ledger_gone')
    elif failure == 'locked_read':
        def locked(*_):
            raise sqlite3.OperationalError('database is locked')
        monkeypatch.setattr(hs, '_read_ledger_pull', locked)
    elif failure == 'write_error':
        def broken(*_, **__):
            raise sqlite3.OperationalError('synthetic ledger write failed')
        monkeypatch.setattr(hs, '_write_ledger_pull', broken)
    else:
        monkeypatch.setattr(hs, '_write_ledger_pull',
                            lambda *_, **__: {'ok': False, 'error': 'hub snapshot refresh in progress'})
    out = hs.pull_changes()
    assert not out['ok'] and out['failed'] == ['ledger'], out
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT summary FROM episodes WHERE id=2').fetchone() == ('foreign memory',)
        assert con.execute("SELECT body FROM topics WHERE slug='probe'").fetchone() == ('corrected',)
        assert con.execute('SELECT status FROM commitments WHERE id=1').fetchone() == ('closed',)
    assert hs.ledger_read_snapshot('p')['as_of'] == before
    assert hs._utc(hs.caught_up_at()) > hs._utc(before)
    assert hs._read_sidecar()['pull_episodes_after']
    if failure == 'missing_gone':
        assert out['ledger']['needs_refresh']


def test_old_write_through_uses_hub_read_time_and_cannot_rewind_a_newer_pull(world, monkeypatch):
    with ledger._replica_pending() as pending:
        pending['p#'] = 'synthetic pending rename'
    original = hs.ledger_write_snapshot
    newer_read = []

    def delayed_write(*args, **kwargs):
        world.ledger.execute("UPDATE ledger_projects SET name='Newer', hidden=1")
        assert hs.pull_changes()['ok']
        with sqlite3.connect(hs.snapshot_path()) as con:
            newer_read.append(hs._held_read_at(con, 'ledger', '*'))
        return original(*args, **kwargs)

    monkeypatch.setattr(hs, 'ledger_write_snapshot', delayed_write)
    # Pull writes use the original writer, avoiding recursion through the delayed catch-up.
    monkeypatch.setattr(hs, '_write_ledger_pull',
                        lambda payload, *, read_at: original('', None, {}, [], [], [],
                                                             _pull=payload, read_at=read_at))
    assert ledger.replica_catch_up(world)['left'] == 0
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name,hidden FROM ledger_projects').fetchall() == [('Newer', 1)]
        assert hs._held_read_at(con, 'ledger_projects', 'p') <= newer_read[0]


def test_unchanged_pull_has_no_project_payload_or_ledger_data_writes(world, monkeypatch):
    # Seed nonempty vocabulary so deleting/reinserting equal words is observable.
    world.ledger.execute("INSERT INTO ledger_vocab VALUES ('p','kind','task','',0)")
    assert hs.pull_changes()['ok']
    with world.cursor() as cur:
        payload = hs._read_ledger_pull(cur, hs.PULL_LIMIT)
    assert payload['projects'] == {} and payload['vocab'] == {}
    assert payload['rows'] == {} and payload['history'] == {} and payload['gone'] == {}
    events = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        con = connect(*args, **kwargs)
        con.set_trace_callback(events.append)
        return con

    monkeypatch.setattr(hs.sqlite3, 'connect', traced)
    assert hs.pull_changes()['ok']
    writes = [q for q in events if q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
    assert not any(re.search(r'\bledger_(projects|vocab|rows|history|gone)\b', q) for q in writes), writes
    assert any('ledger_caught_up_at' in q for q in writes)


def test_identical_ledger_snapshot_does_not_commit(world, monkeypatch):
    payload = {'projects': {}, 'vocab': {}, 'rows': {}, 'history': {}, 'gone': {}}
    at = datetime.now(timezone.utc).timestamp()
    assert hs._write_ledger_pull(payload, read_at=at)['ok']
    commits = []
    connect = sqlite3.connect

    class CountCommits(sqlite3.Connection):
        def commit(self):
            commits.append(True)
            return super().commit()

    monkeypatch.setattr(hs.sqlite3, 'connect', lambda *a, **kw: connect(*a, factory=CountCommits, **kw))
    assert hs._write_ledger_pull(payload, read_at=at)['ok']
    assert commits == []


def test_delayed_write_through_cannot_make_a_newer_rename_pull_look_stale(world, monkeypatch):
    with ledger._replica_pending() as pending:
        pending['p#'] = 'synthetic pending rename'
    original = hs.ledger_write_snapshot

    def publish_old_then_finish_newer_pull(*args, **kwargs):
        world.ledger.execute("UPDATE ledger_projects SET name='Newer', hidden=1")
        read_at = datetime.now(timezone.utc).timestamp()
        with world.cursor() as cur:
            payload = hs._read_ledger_pull(cur, hs.PULL_LIMIT)
        result = original(*args, **kwargs)
        assert original('', None, {}, [], [], [], _pull=payload, read_at=read_at)['ok']
        assert hs._utc(hs.ledger_read_snapshot('p')['as_of']).timestamp() == read_at
        return result

    monkeypatch.setattr(hs, 'ledger_write_snapshot', publish_old_then_finish_newer_pull)
    assert ledger.replica_catch_up(world)['left'] == 0
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name,hidden FROM ledger_projects').fetchall() == [('Newer', 1)]


def test_row_only_pull_preserves_unchanged_vocabulary_and_metadata(world):
    world.ledger.execute("INSERT INTO ledger_vocab VALUES ('p','state','GO','active',0)")
    assert hs.pull_changes()['ok']
    world.ledger.execute("UPDATE ledger_rows SET version=2, title='Changed row'")
    assert hs.pull_changes()['ok']
    got = hs.ledger_read_snapshot('p')
    assert got['vocab']['state'] == [['GO', 'active']]
    assert got['rows'][0]['title'] == 'Changed row'
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name FROM ledger_projects').fetchone() == ('Old',)
    world.ledger.execute('DELETE FROM ledger_vocab')
    assert hs.pull_changes()['ok']
    assert hs.ledger_read_snapshot('p')['vocab'] == {'state': [], 'kind': []}


def test_unchanged_pull_still_fences_older_write_through_metadata(world):
    old_read = datetime.now(timezone.utc).timestamp()
    assert hs.pull_changes()['ok']
    assert hs.ledger_write_snapshot('p', ('Stale', True, None), {}, [], [], [], read_at=old_read)['ok']
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT name,hidden FROM ledger_projects').fetchall() == [('Old', 0)]
