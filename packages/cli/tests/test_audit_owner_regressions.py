"""Owner audit reproductions, with disposable state and fixture PostgreSQL."""
import contextvars
import io
import json
from contextlib import redirect_stdout

import psycopg
import pytest

from khipu import cli, config, owner_act, owner_direction as od, owner_turns as ot, routing
from khipu import db, owner_writes as ow, mcp_server as srv, commitments as co
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_owner_direction import world  # noqa: F401


def act(action, args):
    ok, out = owner_act.act(action, args)
    assert ok, out
    return out


def run(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(list(argv))
    assert rc == 0, out.getvalue()
    return json.loads(out.getvalue())


def lose_completion(monkeypatch):
    original = od._append

    def append(row):
        if row.get('kind') == 'state' and row.get('inverse_started') is not True:
            raise OSError('synthetic completion-write failure')
        return original(row)

    monkeypatch.setattr(od, '_append', append)


def test_undo_preserves_pending_replication_request(world, monkeypatch):
    monkeypatch.setattr(routing, '_caller', lambda: {'app': True, 'chain': ['synthetic app'], 'where': 'synthetic Khipu.app'})
    key = 'routing_replication_live'
    with config.mutate_config() as data:
        data[key] = False
    run('config', '--set', key, 'on', '--at-owner-request', 'Please turn on the copy')
    before = config.load_config()[routing.SETTINGS_REQUEST_KEY][key]
    run('config', '--from-app', '--set', key, 'off')
    action = od.actions()[-1]
    run('owner-direction', 'undo', '--from-app', action['id'])
    assert config.load_config()[routing.SETTINGS_REQUEST_KEY][key] == before
    assert routing.settings_status()[key]['pending'] is True
    assert routing.settings_status()[key]['value'] is False
    assert routing._replication_live() is False


def test_retry_unconfirmed_undo_does_not_duplicate_hold(world, monkeypatch):
    world.say()
    added = act('route.hold.add', {'text': 'maintenance', 'reason': 'keep', 'scope': 'all'})
    ended = act('route.hold.end', {'hold': added['id']})
    with monkeypatch.context() as mp:
        lose_completion(mp)
        act('direction.undo', {'id': ended['owner_action']})
    assert len(routing.read_view()['holds']) == 1
    assert od.line_state(ended['owner_action']) == 'unconfirmed'
    act('direction.undo', {'id': ended['owner_action']})
    assert len(routing.read_view()['holds']) == 1
    assert od.line_state(ended['owner_action']) == 'undone'


def test_unconfirmed_clear_undo_preserves_newer_clear(world, monkeypatch):
    world.say()
    with monkeypatch.context() as mp:
        lose_completion(mp)
        first = act('route.clear', {'account': 'Codex'})
    first_id = routing.current_clear('Codex')
    act('route.clear', {'account': 'Codex'})
    second_id = routing.current_clear('Codex')
    assert second_id != first_id
    act('direction.undo', {'id': first['owner_action']})
    assert routing.current_clear('Codex') == second_id
    events = routing.read_events()[0]
    assert any(e.get('type') == 'clear_undo' and e.get('target') == first_id for e in events)


def test_direction_setting_undo_preserves_later_setting(world):
    world.say()
    first = act('direction.set', {'key': 'latest_window_h', 'value': '12'})
    act('direction.set', {'key': 'session_binding', 'value': 'off'})
    act('direction.undo', {'id': first['owner_action']})
    assert ot.settings()['latest_window_h'] == 6
    assert ot.settings()['session_binding'] is False


def test_trust_undo_preserves_other_later_members(world):
    world.say()
    first = act('direction.set', {'key': 'trust', 'value': 'example:first'})
    act('direction.set', {'key': 'trust', 'value': 'example:second'})
    act('direction.undo', {'id': first['owner_action']})
    assert 'example:first' not in ot.settings()['trusted']
    assert 'example:second' in ot.settings()['trusted']


def test_concurrent_setting_save_records_actual_prior(world, monkeypatch):
    monkeypatch.setattr(routing, '_caller', lambda: {'app': True, 'chain': ['synthetic app'], 'where': 'synthetic Khipu.app'})
    key = 'routing_stale_after'
    with config.mutate_config() as data:
        data[key] = 100
    original = routing.set_setting

    def interleave(k, raw, **kwargs):
        if k == key and raw == '300':
            contextvars.Context().run(run, 'config', '--from-app', '--set', key, '200')
        return original(k, raw, **kwargs)

    monkeypatch.setattr(routing, 'set_setting', interleave)
    run('config', '--from-app', '--set', key, '300')
    action = next(x for x in od.actions() if x.get('result', {}).get(key, {}).get('value') == 300)
    run('owner-direction', 'undo', '--from-app', action['id'])
    assert config.load_config()[key] == 200
    assert action['before']['value'] == 200


@pytest.mark.parametrize('raw', ['1e309', 'inf', 'nan', '-inf'])
def test_nonfinite_window_is_refused_before_write(world, raw):
    world.say()
    ok, out = owner_act.act('direction.set', {'key': 'latest_window_h', 'value': raw})
    assert not ok, out
    assert ot.settings()['latest_window_h'] == 6
    assert od.actions() == []


def test_settings_read_repairs_nonfinite_window_without_invalid_json(world):
    ot.folder().mkdir(parents=True, exist_ok=True)
    (ot.folder() / 'settings.json').write_text('{"latest_window_h": Infinity, "session_binding": false}')
    assert ot.settings()['latest_window_h'] == 6
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(['owner-direction', 'show']) == 0
    def reject(value):
        raise AssertionError('nonstandard JSON: ' + value)
    decoded = json.loads(out.getvalue(), parse_constant=reject)
    assert decoded['settings']['session_binding'] is False


@pytest.fixture
def pg_world(hub, world, monkeypatch):
    monkeypatch.setattr(db, 'connect', lambda **kw: psycopg.Connection.connect(hub.dsn))
    return hub


def test_missing_migration_retains_write_until_replay(pg_world):
    with pg_world.connect() as conn:
        cid = conn.execute("INSERT INTO commitments (text,content_hash) VALUES ('synthetic item','audit-owner') RETURNING id").fetchone()[0]
    result = ow.run('owed_update', {'id': cid, 'action': 'close'})
    assert result['waiting_to_sync'] is True
    assert ow.status()['pending'] == 1
    job = ow._read(ow._jobs()[0])
    assert '0031' in job['reason']
    pg_world.migrate()
    assert ow.drain()['replayed'] == 1
    with pg_world.connect() as conn:
        assert conn.execute('SELECT status FROM commitments WHERE id=%s', (cid,)).fetchone()[0] == 'closed'


@pytest.mark.parametrize('hub_away', [False, True])
def test_lost_directed_commit_reply_keeps_batch_undo(pg_world, world, monkeypatch, hub_away):
    pg_world.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    with pg_world.connect() as conn:
        ids = [conn.execute("INSERT INTO commitments (text,project,content_hash,owner,status) VALUES (%s,'synthetic/project',%s,'user','open') RETURNING id", (f'owner item {i}', f'audit-owner-{i}')).fetchone()[0] for i in range(2)]
    world.say('close the two synthetic items')
    real_apply = ow._apply
    replies = []

    def lose_reply(tool, args):
        result = real_apply(tool, args)
        replies.append(result)
        raise psycopg.OperationalError('server closed the connection after COMMIT')

    with monkeypatch.context() as mp:
        mp.setattr(ow, '_apply', lose_reply)
        if hub_away:
            def unavailable(*args):
                raise psycopg.OperationalError('synthetic hub unavailable')
            mp.setattr(ow, '_landed', unavailable)
        with pytest.raises(psycopg.OperationalError, match='after COMMIT'):
            srv.owed_change(ids, 'close', by=co.BY_CONVERSATION, reason='owner request', by_direction=True)
        if hub_away:
            line, = od.actions()
            assert line['state'] == 'unconfirmed'
            assert 'cannot confirm' in od._undo_refusal(line, line['id'])
    line, = od.actions()
    assert line['state'] != 'failed'
    assert line['before']['batches'] == replies[0]['batches']
    assert line['owner_write']['args']['write_id']
    assert od._undo_refusal(line, line['id']) is None
    act('direction.undo', {'id': line['id']})
    with pg_world.connect() as conn:
        assert conn.execute('SELECT status FROM commitments WHERE id=ANY(%s)', (ids,)).fetchall() == [('open',), ('open',)]
        assert conn.execute('SELECT tool, outcome FROM owner_writes_applied ORDER BY tool').fetchall() == [
            ('owed_update', 'applied'), ('owner_undo', 'applied')]
        assert conn.execute("SELECT write_id FROM owner_writes_applied WHERE tool='owner_undo'").fetchall() == [
            (f"owner-undo:{line['id']}:{replies[0]['batches'][0]}",)]
