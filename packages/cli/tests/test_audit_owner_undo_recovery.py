"""Recovery variants of the owner audit's pending-state and inverse defects."""
import pytest

from khipu import config, owner_direction as od, owner_turns as ot, routing
from tests.test_owner_direction import world  # noqa: F401
from tests.test_audit_owner_regressions import act, run, lose_completion


@pytest.mark.parametrize('answer', ['confirm', 'undo'])
def test_undo_request_answer_restores_pending_replication(world, monkeypatch, answer):
    monkeypatch.setattr(routing, '_caller', lambda: {'app': True, 'chain': ['fake app'], 'where': 'fake app'})
    key = 'routing_replication_live'
    with config.mutate_config() as data:
        data[key] = False
    run('config', '--set', key, 'on', '--at-owner-request', 'Please turn on the copy')
    before = config.load_config()[routing.SETTINGS_REQUEST_KEY][key]
    run('config', '--from-app', '--answer-request', key, answer)
    action = od.actions()[-1]
    run('owner-direction', 'undo', '--from-app', action['id'])
    assert routing._replication_live() is False
    assert config.load_config()[key] is True
    assert config.load_config()[routing.SETTINGS_REQUEST_KEY][key] == before


def test_retry_legacy_unconfirmed_inverse_reuses_its_recorded_operation(world, monkeypatch):
    world.say()
    added = act('route.hold.add', {'text': 'maintenance', 'reason': 'keep', 'scope': 'all'})
    ended = act('route.hold.end', {'hold': added['id']})
    real_ahead, real_inverse = od._ahead_line, od._run_inverse

    def legacy_ahead(*args):
        line = real_ahead(*args)
        line.pop('inverse_id', None)
        return line

    def legacy_inverse(*args):
        # The old writer used the pending undo line id as its routing op.
        token = routing._OP.set(od._PENDING.get()['line']['id'])
        try:
            return real_inverse(*args)
        finally:
            routing._OP.reset(token)

    with monkeypatch.context() as mp:
        mp.setattr(od, '_ahead_line', legacy_ahead)
        mp.setattr(od, '_run_inverse', legacy_inverse)
        lose_completion(mp)
        act('direction.undo', {'id': ended['owner_action']})
    assert len(routing.read_view()['holds']) == 1
    undo_line, = [x for x in od.read_log() if x.get('kind') == 'undo']
    assert any(x.get('op') == undo_line['id'] for x in routing.read_events()[0])
    act('direction.undo', {'id': ended['owner_action']})
    assert len(routing.read_view()['holds']) == 1


def test_uncertain_setting_inverse_cannot_overwrite_a_newer_owner_choice(world, monkeypatch):
    world.say()
    first = act('direction.set', {'key': 'latest_window_h', 'value': '12'})
    with monkeypatch.context() as mp:
        lose_completion(mp)
        act('direction.undo', {'id': first['owner_action']})
    assert ot.settings()['latest_window_h'] == 6
    act('direction.set', {'key': 'latest_window_h', 'value': '8'})
    why = od._undo_refusal(od._find(first['owner_action']), first['owner_action'])
    assert why and 'confirm' in why
    with pytest.raises(ValueError, match='confirm'):
        od.undo(first['owner_action'], 'direction')
    assert ot.settings()['latest_window_h'] == 8


def test_legacy_replication_undo_cannot_invent_missing_confirmation(world):
    key = 'routing_replication_live'
    with config.mutate_config() as data:
        data[key] = False
    # Before pending metadata was recorded, true could mean an unconfirmed request.
    legacy = {'target': key, 'before': {'value': True, 'set': True}}
    with pytest.raises(ValueError, match='confirm'):
        od._undo_setting(legacy, 'app')
    assert config.load_config()[key] is False
    assert routing._replication_live() is False
