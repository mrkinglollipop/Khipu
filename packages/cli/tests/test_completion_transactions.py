"""Nightly completion's transaction boundary; recording SQL only, no services."""
from __future__ import annotations

import copy
import json
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from khipu import close_rules, commitments, db, jobs

NOW = datetime.now(timezone.utc)


def item(**extra):
    return dict(id=1, text='Finish PR #41', project='acme/widget', kind='followup',
                opened_episode=1, opened_at=NOW-timedelta(days=1), prs=[41],
                reopened_rule_keys=[], pr_check={}, settle_suggestion=None,
                owner='agent', origin='job', owner_label='reviewer', future_trigger=None,
                owner_reassigned_at=None, latest_text=None, status='open', forgotten=False,
                **extra)


class Database:
    def __init__(self):
        self.rows = {1: item()}
        self.active = 0
        self.events = []
        self.ledger = []
        self.episodes = []
        self.decisions = []
        self.writes = []
        self.fail_apply = False

    @contextmanager
    def connect(self):
        conn = Connection(self)
        self.events.append('connect')
        try:
            yield conn
            conn.commit()
        finally:
            conn.end()
            self.events.append('close')


class Connection:
    def __init__(self, database):
        self.db = database
        self.active = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.end()
        self.db.events.append('commit')

    def rollback(self):
        self.end()
        self.db.events.append('rollback')

    def end(self):
        if self.active:
            self.db.active -= 1
            self.active = False


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        database = self.conn.db
        if not self.conn.active:
            database.active += 1
            self.conn.active = True
        self.rows = []
        self.rowcount = 0
        if sql.startswith(('SAVEPOINT', 'RELEASE', 'ROLLBACK')):
            return
        if 'FROM ledger_rows' in sql:
            self.rows = copy.deepcopy(database.ledger)
            return
        if 'FROM episodes e' in sql:
            self.rows = copy.deepcopy(database.episodes)
            return
        if 'FROM decisions d' in sql:
            self.rows = copy.deepcopy(database.decisions)
            return
        if sql.startswith('SELECT') and 'FROM commitments' in sql:
            cols = sql.split('SELECT ', 1)[1].split(' FROM commitments', 1)[0].split(', ')
            candidates = list(database.rows.values())
            if 'id = %s' in sql:
                candidates = [r for r in candidates if r['id'] == params[-1]]
            if "status = 'open'" in sql:
                candidates = [r for r in candidates if r['status'] == 'open' and r['project']]
            if 'NOT EXISTS' in sql:
                candidates = [r for r in candidates if not r['forgotten']]
            if 'FOR UPDATE' in sql:
                database.events.append(('lock', [r['id'] for r in candidates]))
                if database.fail_apply:
                    raise RuntimeError('synthetic apply failure')
            self.rows = [tuple(copy.deepcopy(r.get(c)) for c in cols) for r in candidates]
            return
        if sql.startswith('UPDATE commitments'):
            database.writes.append((sql, params))
            if "SET status = 'closed'" in sql:
                row = database.rows[params[5]]
                if row['status'] == 'open' and params[6] not in row['reopened_rule_keys']:
                    row['status'] = 'closed'
                    row['close_evidence'] = json.loads(params[4])
                    self.rowcount = 1
            elif 'SET pr_check' in sql:
                database.rows[params[1]]['pr_check'] = json.loads(params[0])
            elif 'SET settle_suggestion = NULL' in sql:
                database.rows[params[0]]['settle_suggestion'] = None
            elif 'SET settle_suggestion = %s' in sql:
                row = database.rows[params[1]]
                if len(params) < 3 or params[2] not in row['reopened_rule_keys']:
                    row['settle_suggestion'] = json.loads(params[0])
                    self.rowcount = 1
            else:
                raise AssertionError(sql)
            return
        raise AssertionError(sql)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


@pytest.fixture
def harness(monkeypatch):
    database = Database()
    monkeypatch.setattr(db, 'connect', database.connect)
    monkeypatch.setattr(db, 'has_columns', lambda *a: True)
    for name in ('_close_columns_ready', '_future_trigger_ready', '_lineage_columns_ready',
                 '_reassigned_column_ready', '_owner_columns_ready'):
        monkeypatch.setattr(commitments, name, lambda *a: True)
    monkeypatch.setattr(close_rules, 'ready', lambda *a: True)
    monkeypatch.setattr(jobs, '_provenance_backfill', lambda conn: {'ok': True})
    monkeypatch.setattr(close_rules, 'backfill_prs', lambda cur: 0)
    monkeypatch.setattr(close_rules, '_runner_states', lambda project: {})
    monkeypatch.setattr(jobs, '_nightly_log', lambda *a: None)
    monkeypatch.setattr('khipu.config.choice_setting', lambda name: ('on', None))
    monkeypatch.setattr('khipu.config.float_setting', lambda name: .9)
    return database


def test_network_has_no_open_connection_or_transaction_and_preparation_committed(harness, monkeypatch):
    def state(self, repo, number):
        assert harness.active == 0, 'GitHub invoked while SQL transaction is active'
        assert harness.events[-1] == 'close', 'gather connection is still open'
        assert 'commit' in harness.events
        self.asked += 1
        return 'merged', None
    monkeypatch.setattr(close_rules.GitHub, 'state', state)
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert harness.rows[1]['status'] == 'closed'
    assert out['closed_pr'] == 1 and out['github_asked'] == 1
    assert ('lock', [1]) in harness.events


@pytest.mark.parametrize('changes', [
    {'text': 'Finish a different task'}, {'latest_text': 'Also finish PR #42'},
    {'project': 'acme/other'}, {'prs': [41, 42]},
    {'pr_check': {'refs': ['acme/other#41']}}, {'pr_check': {'needs_owner': True}},
    {'owner': 'user', 'owner_reassigned_at': NOW}, {'owner_label': 'maintainer'},
    {'status': 'closed'}, {'forgotten': True}, {'reopened_rule_keys': ['pr:41=merged']},
    {'settle_suggestion': {'looks_done': True, 'key': 'owner:new', 'rule': 'decision'}},
])
def test_changed_candidate_is_not_written_from_stale_snapshot(harness, monkeypatch, changes):
    def state(self, repo, number):
        harness.rows[1].update(copy.deepcopy(changes))
        return 'merged', None
    monkeypatch.setattr(close_rules.GitHub, 'state', state)
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert harness.writes == []
    assert harness.rows[1]['status'] == changes.get('status', 'open')


def test_fresh_sql_reopen_revokes_earlier_github_result(harness, monkeypatch):
    def state(self, repo, number):
        # This capture arrives after GitHub's observation and before apply.
        harness.episodes = [(2, NOW+timedelta(days=1), '✓ Reopened pull request #41', [], [])]
        return 'merged', None
    monkeypatch.setattr(close_rules.GitHub, 'state', state)
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert harness.rows[1]['status'] == 'open'
    assert 'still open' in harness.rows[1]['pr_check']['waiting']['41']


def test_unrequested_frozen_lookup_does_not_call_network_in_apply(harness, monkeypatch):
    # Gather needs no GitHub. The ledger reopens before the apply connection.
    harness.ledger = [('41', 'MERGED', 'done', NOW)]
    original = harness.connect
    count = 0
    @contextmanager
    def connect():
        nonlocal count
        count += 1
        if count >= 3:
            harness.ledger = [('41', 'OPEN', 'active', NOW+timedelta(seconds=1))]
        with original() as conn:
            yield conn
    monkeypatch.setattr(db, 'connect', connect)
    state = Mock(side_effect=AssertionError('unexpected GitHub lookup'))
    monkeypatch.setattr(close_rules.GitHub, 'state', state)
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert harness.rows[1]['status'] == 'open'
    assert harness.rows[1]['pr_check']['waiting']
    state.assert_not_called()


@pytest.mark.parametrize('why', ['GitHub did not answer within 10 seconds', 'gh is not installed',
                                  'GitHub could not say: synthetic failure'])
def test_handled_github_miss_remains_success_with_waiting(harness, monkeypatch, why):
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: (None, why))
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert harness.rows[1]['status'] == 'open'
    assert why in harness.rows[1]['pr_check']['waiting']['41']


def test_apply_failure_is_failed_completion_receipt(harness, monkeypatch):
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    harness.fail_apply = True
    out = jobs._commitment_close_rules()
    assert out['ok'] is False
    assert 'synthetic apply failure' in out['reason']


def test_provenance_failure_is_not_buried_under_success(harness, monkeypatch):
    monkeypatch.setattr(jobs, '_provenance_backfill', lambda conn: {'ok': False, 'reason': 'synthetic witness failure'})
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: (None, 'synthetic unavailable'))
    out = jobs._commitment_close_rules()
    assert out['ok'] is False
    assert out['provenance']['ok'] is False
    assert 'synthetic witness failure' in out['reason']


@pytest.mark.parametrize('driver_rc, expected', [(0, 1), (3, 3), (124, 124)])
def test_completion_failure_fails_nightly_but_later_steps_run(tmp_path, monkeypatch, driver_rc, expected):
    monkeypatch.setattr(jobs, 'ensure_data_dir', lambda: tmp_path)
    monkeypatch.setattr(jobs, '_drain_sessions', lambda: {'ok': True})
    later = []
    for name in ('_reconcile_notes_if_due', '_embed_backfill', '_prune_query_cache',
                 '_mark_stale_commitments', '_hygiene_commitments', '_ledger_nightly',
                 '_project_renames_sweep', '_refresh_snapshot', '_briefs_build_if_on'):
        monkeypatch.setattr(jobs, name, lambda name=name: later.append(name) or {'ok': True})
    monkeypatch.setattr(jobs, '_commitment_close_rules', lambda: {'ok': False, 'reason': 'synthetic failure'})
    # _run_script takes a positional script argument too.
    monkeypatch.setattr(jobs, '_run_script', lambda *a, **kw: driver_rc)
    assert jobs.run_nightly() == expected
    assert '_refresh_snapshot' in later and '_briefs_build_if_on' in later
    steps = jobs.nightly_last()['steps']
    assert next(s for s in steps if s['name'] == 'commitments_close_rules')['ok'] is False
    assert json.loads((tmp_path/'state/job-nightly.json').read_text())['exit'] == expected


@pytest.mark.parametrize('outcome', ['timeout', 'missing', 'failed'])
def test_real_github_handler_runs_only_outside_sql(harness, monkeypatch, outcome):
    monkeypatch.setattr('khipu.synth_cli.binary', lambda name: '/synthetic/gh')
    def run(*args, **kwargs):
        assert harness.active == 0
        assert harness.events[-1] == 'close'
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(args[0], 10)
        if outcome == 'missing':
            raise FileNotFoundError('synthetic missing executable')
        return Mock(returncode=1, stderr='synthetic remote failure')
    monkeypatch.setattr(subprocess, 'run', run)
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert out['github_asked'] == 1
    assert harness.rows[1]['status'] == 'open'
    assert harness.rows[1]['pr_check']['waiting']['41']


@pytest.mark.parametrize('guard', ['owner', 'needs_owner', 'reopened'])
def test_unchanged_candidates_keep_existing_close_guards(harness, monkeypatch, guard):
    row = harness.rows[1]
    if guard == 'owner':
        row.update(owner='user', owner_reassigned_at=NOW)
    elif guard == 'needs_owner':
        row['pr_check'] = {'needs_owner': True}
    else:
        row['reopened_rule_keys'] = ['pr:41=merged']
    monkeypatch.setattr(close_rules.GitHub, 'state', lambda *a: ('merged', None))
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert row['status'] == 'open'
    if guard == 'owner':
        assert row['settle_suggestion']['looks_done'] is True
    elif guard == 'needs_owner':
        assert row['pr_check']['needs_owner'] is True
    else:
        assert row['settle_suggestion'] is None


@pytest.mark.parametrize('withdrawn', [False, True])
def test_decisions_are_read_at_apply_and_use_existing_close_guards(harness, monkeypatch, withdrawn):
    harness.rows[1].update(text='Decide release color', kind='question', prs=[])
    harness.decisions = [(5, 'Resolved item #1', 2, NOW)]
    original = harness.connect
    count = 0
    @contextmanager
    def connect():
        nonlocal count
        count += 1
        if withdrawn and count >= 3:
            harness.decisions = []
        with original() as conn:
            yield conn
    monkeypatch.setattr(db, 'connect', connect)
    monkeypatch.setattr(close_rules.GitHub, 'state', Mock(side_effect=AssertionError('no PR to ask')))
    out = jobs._commitment_close_rules()
    assert out['ok'], out
    assert harness.rows[1]['status'] == ('open' if withdrawn else 'closed')
    assert out['closed_decision'] == (0 if withdrawn else 1)
