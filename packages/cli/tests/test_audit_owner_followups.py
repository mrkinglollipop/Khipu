"""Owner audit follow-ups: missing hub writes, uncertain Undo, damaged snapshots."""
import psycopg
import pytest

from khipu import commitments as co, db, mcp_server as srv
from khipu import owner_act, owner_direction as od, owner_turns as ot, owner_writes as ow
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests import fact_seed
from tests.test_owner_direction import world  # noqa: F401
from tests.test_audit_owner_regressions import act, lose_completion, pg_world  # noqa: F401


def close_items(pg_world, world):
    pg_world.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    with pg_world.connect() as conn:
        ids = [conn.execute(
            "INSERT INTO commitments (text,project,content_hash,owner,status) "
            "VALUES (%s,'synthetic/project',%s,'user','open') RETURNING id",
            (f'synthetic follow-up {i}', f'owner-follow-up-{i}')).fetchone()[0] for i in range(2)]
        with conn.cursor() as cur:
            fact_seed.adopt(cur, 'commitment', ids)
    world.say('close the synthetic items')
    return ids


@pytest.mark.parametrize('initially_away', [False, True])
def test_unsent_owner_write_settles_as_failed(pg_world, world, monkeypatch, initially_away):
    ids = close_items(pg_world, world)

    def before_send(*args):
        raise psycopg.OperationalError('synthetic connection failed before sending')

    with monkeypatch.context() as mp:
        mp.setattr(ow, '_apply', before_send)
        if initially_away:
            mp.setattr(ow, '_landed', before_send)
        with pytest.raises(psycopg.OperationalError, match='before sending'):
            srv.owed_change(ids, 'close', by=co.BY_CONVERSATION,
                            reason='owner request', by_direction=True)
        line, = od.actions()
        if initially_away:
            assert line['state'] == 'unconfirmed'
            assert 'cannot confirm' in od._undo_refusal(line, line['id'])
        else:
            assert line['state'] == 'failed'
    line, = od.actions()
    why = od._undo_refusal(line, line['id'])
    assert 'did not happen' in why
    assert od.line_state(line['id']) == 'failed'
    assert od._undo_refusal(od._find(line['id']), line['id']) == why
    with pg_world.connect() as conn:
        assert conn.execute('SELECT status FROM commitments WHERE id=ANY(%s)', (ids,)).fetchall() == [('open',)] * 2
        assert conn.execute('SELECT count(*) FROM owner_writes_applied').fetchone() == (0,)


@pytest.mark.parametrize('applied', [False, True])
def test_uncertain_hub_undo_settles_without_reapplying(pg_world, world, monkeypatch, applied):
    ids = close_items(pg_world, world)
    changed = srv.owed_change(ids, 'close', by=co.BY_CONVERSATION,
                              reason='owner request', by_direction=True)
    action_id = changed['owner_action']
    if applied:
        with monkeypatch.context() as mp:
            lose_completion(mp)
            act('direction.undo', {'id': action_id})
        # A later, explicit redo is a newer owner choice. Recovery must not undo it.
        from khipu import owed_tidy
        assert owed_tidy.redo(changed['batches'][0], owner_gate='app')['ok']
    else:
        # The ahead line is durable, but the process dies before invoking the inverse.
        od.gate(od.intent('direction.undo', action_id))
        od._PENDING.set(None)
    assert od.line_state(action_id) == 'unconfirmed'
    undo_line, = [r for r in od.read_log() if r.get('kind') == 'undo']
    real_connect = db.connect

    def away(**kwargs):
        raise psycopg.OperationalError('synthetic hub unavailable')

    with monkeypatch.context() as mp:
        mp.setattr(db, 'connect', away)
        with pytest.raises(ValueError, match='confirm'):
            od.undo(action_id, 'direction')
        assert od.line_state(undo_line['id']) == 'unconfirmed'
    assert db.connect is real_connect
    if applied:
        with pytest.raises(ValueError, match='already undone'):
            od.undo(action_id, 'direction')
        assert od.line_state(undo_line['id']) == 'applied'
        assert od.line_state(action_id) == 'undone'
    else:
        act('direction.undo', {'id': action_id})
        assert od.line_state(undo_line['id']) == 'failed'
        assert od.line_state(action_id) == 'undone'
    with pg_world.connect() as conn:
        assert conn.execute('SELECT status FROM commitments WHERE id=ANY(%s)', (ids,)).fetchall() == [(
            'closed' if applied else 'open',)] * 2


@pytest.mark.parametrize('snapshot', [{}, {'settings': {}}, {'settings': None}])
def test_malformed_direction_snapshot_returns_clear_refusal(world, snapshot):
    world.say()
    changed = act('direction.set', {'key': 'latest_window_h', 'value': '12'})
    od._append({'kind': 'state', 'id': od._new_id(), 'at': od._iso(od._clock()),
                'target': changed['owner_action'], 'state': 'applied', 'before': snapshot})
    ok, out = owner_act.act('direction.undo', {'id': changed['owner_action']})
    assert not ok
    assert 'snapshot' in out['error'] and 'latest_window_h' in out['error']
    assert ot.settings()['latest_window_h'] == 12


def test_setting_undo_crash_before_inverse_can_retry(world):
    world.say()
    changed = act('direction.set', {'key': 'latest_window_h', 'value': '12'})
    od.gate(od.intent('direction.undo', changed['owner_action']))
    od._PENDING.set(None)
    assert ot.settings()['latest_window_h'] == 12
    act('direction.undo', {'id': changed['owner_action']})
    assert ot.settings()['latest_window_h'] == 6
    assert od.line_state(changed['owner_action']) == 'undone'


def test_partial_hub_undo_skips_committed_batch_on_retry(pg_world, world, monkeypatch):
    from khipu import owed_tidy

    ids = close_items(pg_world, world)
    with pg_world.connect() as conn:
        conn.execute("UPDATE commitments SET project='synthetic/second' WHERE id=%s", (ids[1],))
    changed = srv.owed_change(ids, 'close', by=co.BY_CONVERSATION,
                              reason='owner request', by_direction=True)
    first, second = changed['batches']
    real_undo = owed_tidy.undo

    def stop_second(batch, **kwargs):
        if batch == second:
            raise psycopg.OperationalError('synthetic second batch failure')
        return real_undo(batch, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(owed_tidy, 'undo', stop_second)
        lose_completion(mp)
        with pytest.raises(psycopg.OperationalError, match='second batch'):
            act('direction.undo', {'id': changed['owner_action']})
    assert od.line_state(changed['owner_action']) == 'unconfirmed'
    assert owed_tidy.redo(first, owner_gate='app')['reapplied'] == 1
    act('direction.undo', {'id': changed['owner_action']})
    assert od.line_state(changed['owner_action']) == 'undone'
    with pg_world.connect() as conn:
        statuses = conn.execute('SELECT status FROM commitments WHERE id=ANY(%s)', (ids,)).fetchall()
        assert sorted(statuses) == [('closed',), ('open',)]
        assert conn.execute("SELECT count(*) FROM owner_writes_applied WHERE tool='owner_undo'").fetchone() == (2,)


@pytest.mark.parametrize('verb', ['undo', 'redo'])
def test_refused_batch_inverse_has_no_applied_receipt(pg_world, world, monkeypatch, verb):
    from khipu import owed_tidy

    ids = close_items(pg_world, world)
    changed = srv.owed_change(ids, 'close', by=co.BY_CONVERSATION,
                              reason='owner request', by_direction=True)
    batch, = changed['batches']
    if verb == 'redo':
        assert owed_tidy.undo(batch, owner_gate='app')['restored'] == 2
    with monkeypatch.context() as mp:
        mp.setattr(owed_tidy, '_gate', lambda *args, **kwargs: (False, None, ids))
        out = getattr(owed_tidy, verb)(batch, write_id='synthetic-refused-inverse')
    assert out['ok'] is False
    assert ow._landed('owner_undo', {'write_id': 'synthetic-refused-inverse'}) is None
    out = getattr(owed_tidy, verb)(batch, owner_gate='app', write_id='synthetic-refused-inverse')
    assert out['ok'] is True
    assert out['restored' if verb == 'undo' else 'reapplied'] == 2
    assert ow._landed('owner_undo', {'write_id': 'synthetic-refused-inverse'}) == ('applied', None)
