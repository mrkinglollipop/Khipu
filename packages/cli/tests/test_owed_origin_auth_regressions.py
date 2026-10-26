"""Hermetic regressions for correction authorization and evidence floors."""
import io
import json
import os
import pty
import sys
from contextlib import redirect_stdout

import pytest

from khipu import cli, db, hub_reach, owner_direction, owner_turns, provenance, routing
from khipu.db import connect as real_connect
REAL_TYPED_YES = routing.typed_yes
from tests.hermetic_pg import hub, pg_cluster, chain_dir  # noqa: F401
from tests.test_owed_origin_correction import origin_db, seed, command  # noqa: F401
from tests.test_owner_direction import World, OWNER, _SESSION_VARS
from tests.fact_seed import announce


@pytest.fixture
def owner_prompt(origin_db, monkeypatch, tmp_path):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'capture'))
    for name in _SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    owner_turns.write_settings({'enabled': True})
    world = World(tmp_path, monkeypatch)
    world.be(OWNER)
    return world


def test_agent_created_pty_cannot_confirm(origin_db, monkeypatch):
    cid = seed(origin_db)
    _, request = command('--request')
    # Do not replace the vulnerable TTY read: feed a real artificial terminal.
    monkeypatch.setattr(routing, 'typed_yes', REAL_TYPED_YES)
    monkeypatch.setattr(routing, '_caller', lambda: {'app': False, 'chain': ['agent']})
    master, slave = pty.openpty()
    try:
        os.write(master, b'yes\n')
        with os.fdopen(slave, 'r') as fake_terminal:
            monkeypatch.setattr(sys, 'stdin', fake_terminal)
            ns = cli.build_parser().parse_args(['owed', 'correct-job-origin', '--confirm', request['request']])
            with redirect_stdout(io.StringIO()):
                rc = ns.func(ns)
        assert rc != 0
    finally:
        os.close(master)


def test_real_recorded_prompt_confirms_and_records_request(origin_db, owner_prompt):
    cid = seed(origin_db)
    _, pending = command('--request')
    turn = owner_prompt.say('Apply job origin request ' + pending['request'])
    rc, result = command('--confirm', pending['request'], '--owner-direction')
    assert rc == 0 and result['changed'] == 1
    action = next(a for a in owner_direction.actions() if a['action'] == 'owed.correct_job_origin.confirm')
    assert action['auth'] == 'direction' and action['turn'] == turn
    assert action['target'] == pending['request']
    assert action['prompt_sha256'] and action['before']['batches'] == result['batches']
    with origin_db.cursor() as cur:
        cur.execute('SELECT owner, origin FROM commitments WHERE id=%s', (cid,))
        assert cur.fetchone() == ('agent', 'job')


@pytest.mark.parametrize('case', ['absent', 'stale', 'job', 'other_session', 'wrong_hook'])
def test_invalid_prompt_cannot_confirm(origin_db, owner_prompt, monkeypatch, case):
    seed(origin_db)
    _, pending = command('--request')
    if case == 'stale':
        owner_prompt.say(ago=7 * 3600)
    elif case == 'job':
        owner_prompt.say(entry='sdk-cli')
    elif case == 'other_session':
        owner_prompt.say(session='different-owner-conversation')
    elif case == 'wrong_hook':
        owner_prompt.say()
        monkeypatch.setattr(owner_direction, '_caller_chain', lambda: [(123, 2.0, 'codex')])
    rc, result = command('--confirm', pending['request'], '--owner-direction')
    assert rc != 0 and result['code'] in {'no_recorded_turn', 'stale', 'not_from_the_hook'}
    with origin_db.cursor() as cur:
        cur.execute('SELECT state FROM commitment_origin_requests WHERE id=%s', (pending['request'],))
        assert cur.fetchone()[0] == 'pending'


@pytest.mark.parametrize('which', ['opening', 'secondary'])
def test_all_witness_sessions_require_unambiguous_lineage(origin_db, which):
    cid = seed(origin_db)
    with origin_db.transaction(), origin_db.cursor() as cur:
        if which == 'opening':
            cur.execute('SELECT e.session_id FROM episodes e JOIN commitments c ON c.opened_episode=e.id WHERE c.id=%s', (cid,))
            session = cur.fetchone()[0]
        else:
            session = 'secondary-session'
            announce(cur)
            cur.execute("INSERT INTO episodes (ts,session_id,parent_session_id,summary,raw) VALUES (now(),%s,'session-parent','Synthetic witness','{}') RETURNING id", (session,))
            witness = cur.fetchone()[0]
            provenance.record_claim(cur, 'commitment', cid, 'text', 'Should the synthetic fixture use a queue?', episode_id=witness)
            provenance.project(cur, 'commitment', cid)
        announce(cur)
        cur.execute("INSERT INTO episodes (ts,session_id,parent_session_id,summary,raw) VALUES (now(),%s,NULL,'Contradictory peer','{}')", (session,))
    assert cid not in [r['id'] for r in command()[1]['items']]


@pytest.mark.parametrize('assigned', [False, True])
def test_pre0035_unestablished_ownership_is_quarantined(hub, monkeypatch, tmp_path, assigned):
    from khipu import commitments
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path))
    conn = hub.connect()
    hub.migrate(directory=chain_dir(tmp_path, '0033'), conn=conn)
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO episodes (ts,session_id,parent_session_id,summary,raw) VALUES (now(),'old-job','old-parent','Synthetic historical capture','{}') RETURNING id")
        eid = cur.fetchone()[0]
        cur.execute("INSERT INTO commitments (project,text,owner,kind,opened_episode,opened_at,content_hash) VALUES ('example/widget','Should the synthetic fixture use a queue?',%s,'question',%s,'2020-01-01','old-owner-claim') RETURNING id", ('assistant' if assigned else 'user', eid))
        cid = cur.fetchone()[0]
        if assigned:
            assert commitments.reassign(cur, cid, 'user', by='app', reason='I own this commitment')
    hub.migrate(conn=conn)
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, 'connect', lambda **kw: hub.connect())
    assert cid not in [r['id'] for r in command()[1]['items']]


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('existing', [False, True])
def test_preview_never_writes_reachability_memo(origin_db, hub, monkeypatch, failure, existing):
    seed(origin_db)
    memo = hub_reach.memo_path()
    if existing:
        memo.parent.mkdir(parents=True, exist_ok=True)
        memo.write_text('{"synthetic":"memo"}')
    before = {p.relative_to(memo.parent): p.read_bytes() for p in memo.parent.rglob('*') if p.is_file()}
    monkeypatch.setattr(db, 'connect', real_connect)
    monkeypatch.setattr(db, 'resolve_dsn', lambda **kw: hub.dsn)
    def connect(*a, **kw):
        if failure:
            raise db.psycopg.OperationalError('Synthetic unreachable hub')
        return hub.connect()
    monkeypatch.setattr(db.psycopg, 'connect', connect)
    if failure:
        with pytest.raises(db.psycopg.OperationalError):
            command()
    else:
        assert command()[0] == 0
    after = {p.relative_to(memo.parent): p.read_bytes() for p in memo.parent.rglob('*') if p.is_file()}
    assert after == before


def test_tidy_partial_undo_keeps_ids_in_inverse_metadata():
    want = owner_direction.intent('owed.tidy', 'example/widget', facts={
        'change': 'undo', 'batch': 'synthetic-batch', 'ids': [4, 8]})
    # A requested selector is not proof that those items were restored.
    # The transaction replaces the initially safe inverse scope only after
    # each successful restore; retain the owner's request independently.
    assert owner_direction._before(want)['ids'] == []
    assert want['facts']['ids'] == [4, 8]


def test_tidy_partial_undo_inverse_replays_only_selected_ids(monkeypatch):
    from khipu import owed_tidy
    seen = []
    monkeypatch.setattr(owed_tidy, 'redo', lambda batch, **kw: seen.append(kw) or {'ok': True, 'reapplied': 1})
    owner_direction._undo_owed_tidy({'id': 'synthetic-action', 'target': 'example/widget',
        'before': {'change': 'undo', 'batch': 'synthetic-batch', 'ids': [4, 8]}}, 'app')
    assert seen[0]['ids'] == [4, 8]


def test_read_only_connect_does_not_run_legacy_secret_migration(monkeypatch, tmp_path):
    from khipu import keychain, paths
    marker = tmp_path / 'unexpected-secret-migration'
    monkeypatch.setattr(db, '_MIGRATION_DONE', False)
    monkeypatch.setattr(paths, 'isolated_dir', lambda: None)
    monkeypatch.setattr(keychain, 'migrate_legacy_secrets', lambda: marker.write_text('mutated'))
    monkeypatch.setattr(keychain, 'get_password', lambda *a, **kw: 'postgresql://synthetic@127.0.0.1/synthetic')
    monkeypatch.setattr(db.psycopg, 'connect', lambda *a, **kw: object())
    db.connect(record_reachability=False)
    assert not marker.exists()


def test_read_only_dsn_lookup_does_not_copy_legacy_keychain_secret(monkeypatch, tmp_path):
    from khipu import keychain
    marker = tmp_path / 'unexpected-keychain-migration'
    synthetic = 'postgresql://synthetic@127.0.0.1/synthetic'
    def getter():
        marker.write_text('mutated')
        return synthetic
    monkeypatch.setattr(keychain, 'get_dsn', getter)
    monkeypatch.setattr(keychain, 'get_password', lambda *a, **kw: synthetic)
    assert db.resolve_dsn(migrate_legacy=False) == synthetic
    assert not marker.exists()
