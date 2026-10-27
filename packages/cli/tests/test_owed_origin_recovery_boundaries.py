"""Additional durable/uncertain/rolled-back acknowledgement boundaries."""
import io
from contextlib import redirect_stdout

import pytest

from khipu import cli, db, owner_direction, owner_turns, routing
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_owed_origin_correction import origin_db, seed, command, allow_app  # noqa: F401
from tests.test_owed_origin_recheck import pty_cli


def correction_action():
    return next(a for a in owner_direction.actions() if a['action'] == 'owed.correct_job_origin.confirm')


@pytest.mark.parametrize('durable', [False, True])
def test_unknown_commit_reply_resolves_before_undo(origin_db, hub, monkeypatch, durable):
    ids = [seed(origin_db), seed(origin_db, project='example/other')]
    _, pending = command('--request')
    allow_app(monkeypatch)
    original = db.connect
    calls = []

    class LostReply:
        def __init__(self):
            self.conn = hub.connect()
        def __enter__(self):
            return self.conn.__enter__()
        def __exit__(self, *args):
            if durable:
                self.conn.__exit__(*args)
            else:
                self.conn.rollback()
                self.conn.close()
            raise db.psycopg.OperationalError('synthetic lost COMMIT reply')

    def connect(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            return LostReply()
        raise db.psycopg.OperationalError('synthetic reconciliation unavailable')

    monkeypatch.setattr(db, 'connect', connect)
    with pytest.raises(db.psycopg.OperationalError, match='lost COMMIT'):
        command('--confirm', pending['request'], '--from-app')
    line = correction_action()
    assert line['state'] == 'unconfirmed'
    assert 'cannot confirm' in owner_direction._undo_refusal(line, line['id'])
    monkeypatch.setattr(db, 'connect', original)
    if durable:
        batches = line['before']['batches']
        assert len(batches) == 2
        _, retry = command('--confirm', pending['request'], '--from-app')
        assert retry['already_applied'] and retry['changed'] == 0 and retry['batches'] == batches
        # The original uncertain action reconciles and actually restores both
        # durable projects; no second application is needed to enable Undo.
        assert owner_direction.undo(line['id'], 'app')['restored'] == 2
        assert correction_action()['before']['batches'] == batches
    else:
        assert 'did not happen' in owner_direction._undo_refusal(line, line['id'])
        assert correction_action()['state'] == 'failed'
        with origin_db.cursor() as cur:
            cur.execute('SELECT count(*) FROM commitment_tidy_batches')
            assert cur.fetchone()[0] == 0
    with origin_db.cursor() as cur:
        cur.execute('SELECT owner,origin FROM commitments WHERE id=ANY(%s) ORDER BY id', (ids,))
        assert cur.fetchall() == [('user','conversation')] * 2


def test_owner_actions_correction_undo_rejects_fake_terminal(origin_db, monkeypatch):
    cid = seed(origin_db)
    _, pending = command('--request')
    allow_app(monkeypatch)
    command('--confirm', pending['request'], '--from-app')
    line = correction_action()
    monkeypatch.setattr(routing, '_caller', lambda: {'app':False,'chain':['job']})
    rc, output = pty_cli(monkeypatch, ['owner-direction', 'undo', line['id']])
    assert rc != 0, output
    with origin_db.cursor() as cur:
        cur.execute('SELECT owner,origin FROM commitments WHERE id=%s', (cid,))
        assert cur.fetchone() == ('agent','job')


@pytest.mark.parametrize('verb', ['on','off'])
def test_auth_enable_disable_rejects_fake_terminal(origin_db, monkeypatch, verb):
    before = owner_turns.settings()['enabled']
    rc, output = pty_cli(monkeypatch, ['owner-direction',verb])
    assert rc != 0, output
    assert owner_turns.settings()['enabled'] == before
