from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
import psycopg
from tests.test_completion_transactions import harness, Cursor, Connection, item, _REAL_PROVENANCE_BACKFILL
from khipu import close_rules, commitments, jobs, provenance, tombstones


def test_index_schema_failure_must_fail_completion(harness, monkeypatch):
    monkeypatch.setattr(jobs, '_provenance_backfill', _REAL_PROVENANCE_BACKFILL)
    monkeypatch.setattr(provenance, 'backfill', lambda conn: {'ok': True})
    monkeypatch.setattr(provenance, 'replay_gaps', lambda conn: {'ok': True, 'failed': 0})
    monkeypatch.setattr(provenance, '_copy_ready', Mock(side_effect=psycopg.errors.UndefinedColumn('synthetic missing identity column')))
    monkeypatch.setattr(provenance, 'prune_changes', lambda conn: {'ok': True})
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert out['provenance']['index']['ok'] is False
    assert out['ok'] is False, out


def test_apply_transient_stays_nonfatal(harness, monkeypatch):
    harness.rows[2] = {**item(), 'id': 2}
    original = close_rules._open_items
    def open_items(cur, project, **kwargs):
        if kwargs.get('commitment_id') == 1:
            raise psycopg.errors.SerializationFailure('synthetic retryable contention')
        return original(cur, project, **kwargs)
    monkeypatch.setattr(close_rules, '_open_items', open_items)
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert harness.rows[2]['status'] == 'closed'
    assert out['ok'] is True, out
    assert out['failed'] == 0 and out['deferred_transient'] == 1
    assert out['errors'][0]['phase'] == 'apply'
    assert out['errors'][0]['commitment_id'] == 1


def test_manual_close_reopen_during_github_is_deferred(harness, monkeypatch):
    # Use the real manual status function; implement only its two writes in
    # the recording cursor. A hand close has no automatic rule key to retain.
    monkeypatch.setattr(commitments, '_seen_columns_ready', lambda cur: True)
    original = Cursor.execute
    def execute(cur, sql, params=()):
        if sql.startswith('UPDATE commitments SET status = %s'):
            original(cur, 'SAVEPOINT manual_status')
            row = harness.rows[params[-1]]
            row.update(status=params[0], close_reason=params[1], closed_by=params[2],
                       close_rule_key=None, close_evidence=None)
            cur.rowcount = 1
        elif sql.startswith("UPDATE commitments SET status = 'open'"):
            original(cur, 'SAVEPOINT manual_status')
            row = harness.rows[params[0]]
            row.update(status='open', closed_episode=None, closed_at=None, close_reason=None,
                       closed_by=None, close_rule_key=None, close_evidence=None,
                       last_seen_at=datetime.now(timezone.utc))
            cur.rowcount = 1
        elif sql.startswith('UPDATE commitments SET reopened_rule_keys'):
            original(cur, 'SAVEPOINT manual_status')
            cur.rowcount = 0
        else:
            original(cur, sql, params)
    monkeypatch.setattr(Cursor, 'execute', execute)
    def state(*args):
        with harness.connect() as conn:
            with conn.cursor() as cur:
                assert commitments.set_status(cur, 1, 'closed', by='owner')
                assert commitments.set_status(cur, 1, 'open', by='owner')
        return 'merged', None
    monkeypatch.setattr(close_rules.GitHub, 'state', state)
    out = jobs._commitment_close_rules()
    assert harness.rows[1]['status'] == 'open', out


def _install_gap(harness, monkeypatch):
    monkeypatch.setattr(provenance, 'ready', lambda cur: True)
    monkeypatch.setattr(tombstones, 'hub_source_forgotten', lambda *a: False)
    original = Cursor.execute
    payload = {'project': 'acme/widget', 'open_loops': ['Finish PR #41']}
    def execute(cur, sql, params=()):
        if 'FROM fact_gaps' in sql and 'array_agg' in sql:
            original(cur, 'SAVEPOINT gap_probe')
            cur.rows = [(2, 'close rules', None, [99])]
        elif 'FROM project_aliases' in sql:
            cur.rows = []
        elif sql.startswith('SELECT raw, deleted_at FROM episodes'):
            original(cur, 'SAVEPOINT gap_probe')
            harness.events.append('episode_share_lock')
            cur.rows = [(payload, None)]
        elif sql.startswith('SELECT id FROM fact_gaps'):
            original(cur, 'SAVEPOINT gap_probe')
            if 'FOR UPDATE' in sql:
                harness.events.append('gap_update_lock')
            cur.rows = [(99,)]
        elif sql.startswith('UPDATE fact_gaps'):
            original(cur, 'SAVEPOINT gap_probe')
            cur.rowcount = 1
        else:
            original(cur, sql, params)
    monkeypatch.setattr(Cursor, 'execute', execute)


def test_gap_replay_must_not_call_github_while_locked(harness, monkeypatch):
    monkeypatch.setattr(jobs, '_provenance_backfill', _REAL_PROVENANCE_BACKFILL)
    monkeypatch.setattr(provenance, 'backfill', lambda conn: {'ok': True})
    monkeypatch.setattr(provenance, 'index_identities', lambda conn: {'ok': True})
    monkeypatch.setattr(provenance, 'prune_changes', lambda conn: {'ok': True})
    _install_gap(harness, monkeypatch)
    observed = []
    def state(*args):
        observed.append(harness.active)
        return 'merged', None
    monkeypatch.setattr(close_rules.GitHub, 'state', state)
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert 'episode_share_lock' in harness.events and 'gap_update_lock' in harness.events
    assert observed and all(active == 0 for active in observed), observed


def test_last_seen_at_is_selected_for_candidate_comparison(harness):
    seen = datetime.now(timezone.utc)
    harness.rows[1]['last_seen_at'] = seen
    with harness.connect() as conn, conn.cursor() as cur:
        assert close_rules._open_items(cur, None)[0]['last_seen_at'] == seen


@pytest.mark.parametrize('step, receipt, fatal', [
    ('gaps', {'ok': True, 'failed': 1}, True),
    ('gaps', {'ok': True, 'failed': 1, 'transient': True, 'reason': 'synthetic contention'}, False),
    ('index', {'ok': False, 'transient': True, 'error': 'synthetic contention'}, False),
    ('index', {'ok': False, 'transient': False, 'error': 'synthetic schema failure'}, True),
    ('changes', {'ok': False, 'error': 'synthetic cleanup failure'}, True),
])
def test_nested_cleanup_receipts_are_aggregated(harness, monkeypatch, step, receipt, fatal):
    monkeypatch.setattr(jobs, '_provenance_backfill', _REAL_PROVENANCE_BACKFILL)
    for name in ('backfill', 'replay_gaps', 'index_identities', 'prune_changes'):
        monkeypatch.setattr(provenance, name, lambda conn: {'ok': True})
    helper = {'gaps': 'replay_gaps', 'index': 'index_identities', 'changes': 'prune_changes'}[step]
    monkeypatch.setattr(provenance, helper, lambda conn: dict(receipt))
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert out['ok'] is not fatal, out
    assert out['provenance']['ok'] is False
    assert out['provenance']['transient'] is not fatal
    assert out['provenance'][step] == receipt
    assert harness.rows[1]['status'] == 'closed'


def test_fatal_nested_failure_survives_transient_sibling(harness, monkeypatch):
    monkeypatch.setattr(jobs, '_provenance_backfill', _REAL_PROVENANCE_BACKFILL)
    monkeypatch.setattr(provenance, 'backfill', lambda conn: {'ok': True})
    monkeypatch.setattr(provenance, 'replay_gaps', lambda conn: {
        'ok': False, 'failed': 1, 'transient': False, 'reason': 'synthetic replay schema failure'})
    monkeypatch.setattr(provenance, 'index_identities', lambda conn: {
        'ok': False, 'transient': True, 'error': 'synthetic later contention'})
    monkeypatch.setattr(provenance, 'prune_changes', lambda conn: {'ok': True})
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert out['ok'] is False and out['provenance']['transient'] is False, out
    assert 'synthetic replay schema failure' in out['reason']


def test_fatal_backfill_receipt_survives_later_transient_exception(harness, monkeypatch):
    monkeypatch.setattr(jobs, '_provenance_backfill', _REAL_PROVENANCE_BACKFILL)
    monkeypatch.setattr(provenance, 'backfill', lambda conn: {
        'ok': False, 'reason': 'synthetic fatal backfill'})
    monkeypatch.setattr(provenance, 'replay_gaps', Mock(side_effect=
        psycopg.errors.SerializationFailure('synthetic later contention')))
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert out['ok'] is False and out['provenance']['transient'] is False, out
    assert out['reason'] == 'synthetic fatal backfill'
    assert out['provenance']['exception']['transient'] is True


@pytest.mark.parametrize('helper', ['index_identities', 'replay_gaps'])
@pytest.mark.parametrize('error, transient', [
    (psycopg.errors.SerializationFailure('synthetic cleanup contention'), True),
    (psycopg.errors.UndefinedColumn('synthetic cleanup schema failure'), False),
])
def test_real_cleanup_helpers_preserve_classification(harness, monkeypatch, helper, error, transient):
    monkeypatch.setattr(jobs, '_provenance_backfill', _REAL_PROVENANCE_BACKFILL)
    monkeypatch.setattr(provenance, 'backfill', lambda conn: {'ok': True})
    monkeypatch.setattr(provenance, 'prune_changes', lambda conn: {'ok': True})
    if helper == 'index_identities':
        monkeypatch.setattr(provenance, 'replay_gaps', lambda conn: {'ok': True, 'failed': 0})
        monkeypatch.setattr(provenance, '_copy_ready', Mock(side_effect=error))
        step = 'index'
    else:
        _install_gap(harness, monkeypatch)
        monkeypatch.setattr(provenance, '_gap_step', Mock(side_effect=error))
        monkeypatch.setattr(provenance, 'index_identities', lambda conn: {'ok': True})
        step = 'gaps'
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert out['ok'] is transient, out
    assert out['provenance'][step]['ok'] is False
    assert out['provenance'][step]['transient'] is transient
    if step == 'gaps':
        assert out['provenance'][step]['failed'] == 1
    assert harness.rows[1]['status'] == 'closed'


@pytest.mark.parametrize('phase', ['preparation', 'gather', 'apply'])
@pytest.mark.parametrize('error, transient', [
    (psycopg.errors.SerializationFailure('synthetic phase contention'), True),
    (psycopg.errors.UndefinedColumn('synthetic phase schema failure'), False),
])
def test_completion_phases_classify_retryable_errors(harness, monkeypatch, phase, error, transient):
    harness.rows[2] = {**item(), 'id': 2}
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    if phase == 'preparation':
        monkeypatch.setattr(close_rules, 'backfill_prs', Mock(side_effect=error))
    else:
        original = close_rules._open_items
        def open_items(cur, project, **kwargs):
            if (phase == 'gather' and not kwargs) or kwargs.get('commitment_id') == 1:
                raise error
            return original(cur, project, **kwargs)
        monkeypatch.setattr(close_rules, '_open_items', open_items)
    out = jobs._commitment_close_rules()
    assert out['ok'] is transient, out
    if phase == 'preparation' and transient:
        assert out['preparation']['transient'] is True
        assert out['backfilled'] == 0
        assert harness.rows[1]['status'] == harness.rows[2]['status'] == 'closed'
    elif phase == 'apply':
        assert harness.rows[1]['status'] == 'open'
        assert harness.rows[2]['status'] == 'closed'
        assert out['closed_pr'] == 1
    else:
        assert harness.rows[1]['status'] == harness.rows[2]['status'] == 'open'


@pytest.mark.parametrize('error, transient', [
    (psycopg.errors.SerializationFailure('synthetic project gather contention'), True),
    (psycopg.errors.UndefinedColumn('synthetic project gather schema failure'), False),
])
def test_project_gather_failure_does_not_block_other_projects(harness, monkeypatch, error, transient):
    harness.rows[2] = {**item(), 'id': 2, 'project': 'acme/other'}
    original = close_rules._ledger_states
    def ledger_states(cur, project):
        if project == 'acme/widget':
            raise error
        return original(cur, project)
    monkeypatch.setattr(close_rules, '_ledger_states', ledger_states)
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert out['ok'] is transient, out
    assert harness.rows[1]['status'] == 'open' and harness.rows[2]['status'] == 'closed'
    assert out['closed_pr'] == 1
    assert out['errors'][0]['phase'] == 'gather'
    assert out['errors'][0]['project'] == 'acme/widget'


def test_nested_cleanup_failure_must_fail_nightly(harness, monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, '_provenance_backfill', _REAL_PROVENANCE_BACKFILL)
    monkeypatch.setattr(provenance, 'backfill', lambda conn: {'ok': True})
    monkeypatch.setattr(provenance, 'replay_gaps', lambda conn: {'ok': True, 'failed': 0})
    monkeypatch.setattr(provenance, '_copy_ready', Mock(side_effect=psycopg.errors.UndefinedColumn('synthetic missing identity column')))
    monkeypatch.setattr(provenance, 'prune_changes', lambda conn: {'ok': True})
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    monkeypatch.setattr(jobs, 'ensure_data_dir', lambda: tmp_path)
    monkeypatch.setattr(jobs, '_run_script', lambda *a, **kw: 0)
    for name in ('_drain_sessions', '_reconcile_notes_if_due', '_embed_backfill', '_prune_query_cache',
                 '_mark_stale_commitments', '_hygiene_commitments', '_ledger_nightly',
                 '_project_renames_sweep', '_refresh_snapshot', '_briefs_build_if_on'):
        monkeypatch.setattr(jobs, name, lambda: {'ok': True})
    assert jobs.run_nightly() != 0


def _install_nightly_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, 'ensure_data_dir', lambda: tmp_path)
    monkeypatch.setattr(jobs, '_run_script', lambda *a, **kw: 0)
    for name in ('_drain_sessions', '_reconcile_notes_if_due', '_embed_backfill', '_prune_query_cache',
                 '_mark_stale_commitments', '_hygiene_commitments', '_ledger_nightly',
                 '_project_renames_sweep', '_refresh_snapshot', '_briefs_build_if_on'):
        monkeypatch.setattr(jobs, name, lambda: {'ok': True})
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    monkeypatch.setattr(provenance, 'backfill', lambda conn: {'ok': True})
    monkeypatch.setattr(provenance, 'prune_changes', lambda conn: {'ok': True})
    receipts = []
    def backfill(conn):
        out = _REAL_PROVENANCE_BACKFILL(conn)
        receipts.append(out)
        return out
    monkeypatch.setattr(jobs, '_provenance_backfill', backfill)
    return receipts


def _install_two_gap_groups(harness, monkeypatch):
    _install_gap(harness, monkeypatch)
    original = Cursor.execute
    def execute(cur, sql, params=()):
        original(cur, sql, params)
        if 'FROM fact_gaps' in sql and 'array_agg' in sql:
            cur.rows = [(2, 'close rules', None, [99]), (3, 'close rules', None, [100])]
        elif sql.startswith('SELECT id FROM fact_gaps'):
            cur.rows = [(params[0][0],)]
    monkeypatch.setattr(Cursor, 'execute', execute)


def test_replay_fatal_then_dropped_connection_keeps_failure(harness, monkeypatch, tmp_path):
    receipts = _install_nightly_cleanup(monkeypatch, tmp_path)
    _install_two_gap_groups(harness, monkeypatch)
    index = Mock(return_value={'ok': True})
    prune = Mock(return_value={'ok': True})
    monkeypatch.setattr(provenance, 'index_identities', index)
    monkeypatch.setattr(provenance, 'prune_changes', prune)
    seen = []
    def step(cur, name, payload, episode):
        seen.append(episode)
        if episode == 2:
            raise psycopg.errors.UndefinedColumn('fatal schema error in replay')
        cur.conn.close()
        raise psycopg.errors.ConnectionFailure('connection lost on second group')
    monkeypatch.setattr(provenance, '_gap_step', step)
    assert jobs.run_nightly() != 0, 'fatal first replay failure was erased by later disconnection'
    assert seen == [2, 3]
    assert receipts[0]['transient'] is False
    assert 'UndefinedColumn: fatal schema error in replay' in receipts[0]['reason']
    assert receipts[0]['gaps']['failed'] == 2
    assert receipts[0]['connection_unusable'] is True
    index.assert_not_called()
    prune.assert_not_called()


def test_index_schema_failure_survives_rollback_disconnect(harness, monkeypatch, tmp_path):
    receipts = _install_nightly_cleanup(monkeypatch, tmp_path)
    monkeypatch.setattr(provenance, 'replay_gaps', lambda conn: {'ok': True})
    prune = Mock(return_value={'ok': True})
    monkeypatch.setattr(provenance, 'prune_changes', prune)
    seen = []
    failed_connections = []
    def copy_ready(cur):
        seen.append('UndefinedColumn')
        failed_connections.append(cur.conn)
        cur.conn.close()
        raise psycopg.errors.UndefinedColumn('fatal schema error in index')
    monkeypatch.setattr(provenance, '_copy_ready', copy_ready)
    prepared = []
    def prepare(cur):
        assert not cur.conn.closed
        prepared.append(cur.conn)
        return 0
    monkeypatch.setattr(close_rules, 'backfill_prs', prepare)
    assert jobs.run_nightly() != 0, 'fatal index failure was erased by rollback disconnection'
    assert seen == ['UndefinedColumn']
    assert receipts[0]['transient'] is False
    assert 'UndefinedColumn: fatal schema error in index' in receipts[0]['reason']
    assert receipts[0]['index']['transient'] is False
    assert receipts[0]['connection_unusable'] is True
    prune.assert_not_called()
    assert prepared and all(conn not in failed_connections for conn in prepared)


@pytest.mark.parametrize('disconnect', [False, True], ids=['rollback-ok', 'rollback-disconnect'])
@pytest.mark.parametrize('receipt_error', [
    psycopg.errors.SerializationFailure, psycopg.errors.UndefinedColumn,
], ids=['transient-receipt', 'fatal-receipt'])
@pytest.mark.parametrize('replay_error', [
    psycopg.errors.SerializationFailure, psycopg.errors.UndefinedColumn,
], ids=['transient-replay', 'fatal-replay'])
def test_replay_receipt_write_failure_classifies_before_rollback(
        harness, monkeypatch, tmp_path, replay_error, receipt_error, disconnect):
    receipts = _install_nightly_cleanup(monkeypatch, tmp_path)
    _install_gap(harness, monkeypatch)
    monkeypatch.setattr(provenance, 'index_identities', lambda conn: {'ok': True})
    monkeypatch.setattr(provenance, '_gap_step', Mock(side_effect=
        replay_error('synthetic replay failure')))
    original = Cursor.execute
    errors = []
    def execute(cur, sql, params=()):
        if sql.startswith('UPDATE fact_gaps') and str(params[0]).startswith('replay failed:'):
            errors.append(receipt_error.__name__)
            if disconnect:
                cur.conn.close()
            raise receipt_error('synthetic receipt failure')
        return original(cur, sql, params)
    monkeypatch.setattr(Cursor, 'execute', execute)
    fatal = psycopg.errors.UndefinedColumn in (replay_error, receipt_error)
    rc = jobs.run_nightly()
    assert errors == [receipt_error.__name__]
    assert (rc != 0) is fatal, receipts
    out = receipts[0]
    assert out['ok'] is False
    assert out['transient'] is not fatal
    assert out['gaps']['failed'] == 1
    reason = ('UndefinedColumn: synthetic receipt failure'
              if replay_error is psycopg.errors.SerializationFailure and fatal
              else f'{replay_error.__name__}: synthetic replay failure')
    assert out['reason'] == reason
    assert out['gaps']['reason'] == reason
    assert out.get('connection_unusable', False) is disconnect


def test_replay_receipt_write_rollback_failure_keeps_original_failure(harness, monkeypatch, tmp_path):
    receipts = _install_nightly_cleanup(monkeypatch, tmp_path)
    _install_two_gap_groups(harness, monkeypatch)
    index = Mock(return_value={'ok': True})
    prune = Mock(return_value={'ok': True})
    monkeypatch.setattr(provenance, 'index_identities', index)
    monkeypatch.setattr(provenance, 'prune_changes', prune)
    seen = []
    def step(cur, name, payload, episode):
        seen.append(episode)
        raise psycopg.errors.UndefinedColumn('fatal schema error before receipt')
    monkeypatch.setattr(provenance, '_gap_step', step)
    original = Cursor.execute
    def execute(cur, sql, params=()):
        if sql.startswith('UPDATE fact_gaps') and str(params[0]).startswith('replay failed:'):
            cur.conn.close()
            raise psycopg.errors.ConnectionFailure('connection lost writing failure receipt')
        original(cur, sql, params)
    monkeypatch.setattr(Cursor, 'execute', execute)
    assert jobs.run_nightly() != 0, 'fatal replay failure was erased by receipt rollback disconnection'
    assert seen == [2], 'replay continued using the connection after rollback failed'
    assert receipts[0]['transient'] is False
    assert 'UndefinedColumn: fatal schema error before receipt' in receipts[0]['reason']
    assert receipts[0]['gaps']['failed'] == 1
    assert receipts[0]['connection_unusable'] is True
    index.assert_not_called()
    prune.assert_not_called()


@pytest.mark.parametrize('phase', ['network', 'second_apply'])
def test_interruption_is_retryable_without_losing_committed_rows(harness, monkeypatch, phase):
    harness.rows[2] = {**item(), 'id': 2}
    original = close_rules._open_items
    def open_items(cur, project, **kwargs):
        if phase == 'second_apply' and kwargs.get('commitment_id') == 2:
            raise KeyboardInterrupt('synthetic process interruption')
        return original(cur, project, **kwargs)
    monkeypatch.setattr(close_rules, '_open_items', open_items)
    def state(*args):
        if phase == 'network':
            raise KeyboardInterrupt('synthetic process interruption')
        return 'merged', None
    monkeypatch.setattr(close_rules.GitHub, 'state', state)
    with pytest.raises(KeyboardInterrupt):
        jobs._commitment_close_rules()
    assert harness.rows[2]['status'] == 'open'
    assert harness.rows[1]['status'] == ('open' if phase == 'network' else 'closed')
    monkeypatch.setattr(close_rules, '_open_items', original)
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert all(row['status'] == 'closed' for row in harness.rows.values())
    assert out['closed_pr'] == (2 if phase == 'network' else 1)
