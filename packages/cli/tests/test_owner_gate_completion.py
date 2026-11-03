"""Legacy persistence and same-turn owner request regressions; synthetic state only."""
import json

import pytest

from khipu import accounts, ledger_import, needs_you, owed_tidy, owner_act, owner_direction, routing
from tests.test_owner_needs_you import (
    _accounts_request, _cap_request, _earlier_ask, _owner_types, _priority_request,
    _rule_added, _rule_changed, _setting_request, _tier_request,
)
from tests.test_t3_owner_confirm import world

assert world

KINDS = ('priority', 'priority_undo', 'rule', 'rule_undo', 'proposal', 'setting', 'tier',
         'seeded_rule_undo', 'tracker', 'owed_plan', 'cap_proposal')


def _legacy(world, monkeypatch, kind):
    if kind.startswith('priority'):
        _priority_request(world)
        action, args = 'route.priority.' + ('undo' if kind.endswith('undo') else 'confirm'), {'expect': routing.priority_version()}
    elif kind == 'rule':
        _rule_added(world)
        action, args = 'route.rule.confirm', {'rule': routing.read_view()['rules'][0]['n']}
    elif kind == 'rule_undo':
        number = _rule_changed(world)
        action, args = 'route.rule.undo', {'rule': number}
    elif kind == 'proposal':
        _accounts_request(world)
        action, args = 'accounts.request.confirm', {'expect': accounts.status()['version']}
    elif kind == 'setting':
        _setting_request(world)
        action, args = 'config.answer_request.confirm', {'key': 'routing_replication_live'}
    elif kind == 'tier':
        _tier_request(world)
        action, args = 'route.tier_table.confirm', {}
    elif kind == 'seeded_rule_undo':
        routing._write({'type': 'rule_add', 'n': 1, 'text': 'Old seed', 'seeded': True,
                        'written_by': {'kind': 'seed', 'file': 'rules.json'}, 'via': 'seed'})
        action, args = 'route.rule.retire', {'rule': 1}
    elif kind == 'cap_proposal':
        _cap_request(world)
        action, args = 'route.cap.set', {'account': 'Codex', 'kind': 'share', 'window': 'weekly', 'pct': 40,
                                         'revision': 'conv1', 'purpose': 'job'}
    else:
        path = world['tmp'] / (kind + '.json')
        path.write_text(json.dumps({'rows': {'reviewed': {'title': 'Reviewed', 'kind': 'task', 'state': 'todo'}}}
                                   if kind == 'tracker' else {'project': 'p', 'items': []}))
        action, args = ('ledger.import', {'file': str(path), 'project': 'p'}) if kind == 'tracker' else (
            'owed.tidy', {'project': 'p', 'plan': str(path)})
        monkeypatch.setattr(ledger_import, 'done_imports', lambda *a: [])
    _owner_types(world, 'prepare the old change')
    # Produce the old persisted shape: no content pins, review seal or disclosure metadata.
    row = owner_direction.hold(action, args, ask=owner_direction.ask_line(action, args))
    with owner_direction._requests_locked() as path:
        state = owner_direction._load(path)
        saved = next(r for r in state['requests'] if r['id'] == row['id'])
        for key in ('files', 'disclosed', 'review'):
            saved.pop(key, None)
        owner_direction._save(path, state, owner_direction._clock())
    return row


@pytest.mark.parametrize('kind', KINDS)
def test_legacy_hold_never_applies_after_a_full_read_and_later_yes(world, monkeypatch, kind):
    row = _legacy(world, monkeypatch, kind)
    seen = []
    if kind == 'tracker':
        def run_import(file, project, **kw):
            seen.append(owner_direction.import_source(file, project))
            return {'ok': True}
        monkeypatch.setattr(ledger_import, 'run_import', run_import)
        # The path now contains data the old ask never showed.
        from pathlib import Path
        Path(row['args']['file']).write_text(json.dumps({'rows': {'hidden': {'title': 'Hidden', 'kind': 'task', 'state': 'done'}}}))
    if kind == 'owed_plan':
        monkeypatch.setattr(owed_tidy, 'apply', lambda doc, **kw: seen.append(doc) or {'ok': True})
    before_events = routing.read_events()[0]
    from khipu.config import load_config
    before_config = load_config()
    _owner_types(world, 'show the whole held change')
    needs_you.waiting(key='held:' + row['id'])
    _owner_types(world, 'yes')
    ok, out = owner_act.act('direction.confirm', {'request': row['id']})
    assert not ok, {'kind': kind, 'result': out, 'sink': seen}
    assert not seen
    assert routing.read_events()[0] == before_events
    assert load_config() == before_config



def _own_request(kind):
    if kind == 'rule':
        return routing.request_rule('Docs go to Haiku', owner_request='add a rule',
                                    author=routing.conversation_author(), via='mcp')
    shown = routing.conversation_view()
    return routing.request_priority_amend('Codex', role='Docs go to Codex', owner_request='change Codex role',
                                           author=routing.conversation_author(), via='mcp',
                                           expect=shown['priority']['version'], base=shown['priority_owner_revision'])


@pytest.mark.parametrize('kind', ['rule', 'priority'])
def test_own_request_spends_only_its_creation_turn_and_leaves_older_question_pending(world, kind):
    if kind == 'priority':
        from tests.test_owner_needs_you import _owner_priority
        _owner_priority()
    _earlier_ask(world)
    older = next(r['id'] for r in owner_direction._read_requests(owner_direction.requests_path())
                 if r['state'] == 'pending')
    _owner_types(world, 'add the new routing direction')
    out = _own_request(kind)
    done = owner_act.apply_request(kind, out)
    assert done.get('applied') is True and not done.get('pending'), done
    ok, refused = owner_act.act('direction.confirm', {'request': older})
    assert not ok and refused['code'] == 'message_used', refused
    assert next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r['id'] == older)['state'] == 'pending'
    ok, refused = owner_act.act('route.note.set', {'text': 'unrelated extra'})
    assert not ok or refused.get('pending'), refused
    _owner_types(world, 'yes to the older pause now')
    ok, result = owner_act.act('direction.confirm', {'request': older})
    assert ok, result


@pytest.mark.parametrize('kind', ['rule', 'priority'])
def test_old_own_request_cannot_borrow_a_later_typed_turn(world, kind):
    if kind == 'priority':
        from tests.test_owner_needs_you import _owner_priority
        _owner_priority()
    _earlier_ask(world)
    _owner_types(world, 'make a request on this turn')
    out = _own_request(kind)
    _owner_types(world, 'unrelated later message')
    before = routing.read_events()[0]
    done = owner_act.apply_request(kind, out)
    assert not done.get('applied'), done
    assert routing.read_events()[0] == before


def test_own_request_on_first_proven_t3_turn_needs_no_question(world):
    _owner_types(world, 'add a rule')
    out = _own_request('rule')
    done = owner_act.apply_request('rule', out)
    assert done.get('applied') is True and not done.get('pending'), done


def test_changed_request_cannot_use_an_older_receipt_from_the_same_turn(world):
    _owner_types(world, 'add a rule')
    old = _own_request('rule')
    shown = next(r for r in routing.conversation_view()['rules'] if r['n'] == old['n'])
    newer = routing.request_rule('Changed pending words', n=old['n'], base=shown['owner_revision'],
                                 author=routing.conversation_author(), owner_request='different request')
    before = routing.read_events()[0]
    assert not owner_act.apply_request('rule', old).get('applied')
    assert routing.read_events()[0] == before
    assert not owner_act.apply_request('rule', newer).get('applied')
    assert routing.read_view()['rules'][0]['pending'] is True


@pytest.mark.parametrize('field', ['review', 'files', 'args', 'ask'])
def test_damaged_review_contract_cannot_be_repaired_by_listing(world, field):
    _owner_types(world, 'prepare a setting')
    row = owner_direction.hold('direction.set', {'key': 'latest_window_h', 'value': '1000'})
    with owner_direction._requests_locked() as path:
        state = owner_direction._load(path)
        saved = next(r for r in state['requests'] if r['id'] == row['id'])
        saved.pop(field)
        owner_direction._save(path, state, owner_direction._clock())
    _owner_types(world, 'show the full item')
    item = needs_you.waiting(key='held:' + row['id'])['items'][0]
    assert item['settle']['confirm'] is None
    _owner_types(world, 'yes')
    ok, out = owner_act.act('direction.confirm', {'request': row['id']})
    assert not ok and out['code'] == 'legacy_hold', out


def test_turn_switch_at_mutation_gate_cannot_retarget_own_request(world, monkeypatch):
    _owner_types(world, 'add a rule')
    out = _own_request('rule')
    real_gate = owner_direction.gate
    def interleaved(*args, **kwargs):
        _owner_types(world, 'an unrelated later turn')
        return real_gate(*args, **kwargs)
    monkeypatch.setattr(owner_direction, 'gate', interleaved)
    done = owner_act.apply_request('rule', out)
    assert not done.get('applied') and done['instant']['code'] == 'request_turn_moved', done
    assert routing.read_view()['rules'][0]['pending'] is True


def test_legacy_refusal_can_be_replaced_only_by_a_freshly_shown_hold_and_reply(world, monkeypatch):
    row = _legacy(world, monkeypatch, 'setting')
    _owner_types(world, 'yes to the old question')
    ok, out = owner_act.act('direction.confirm', {'request': row['id']})
    assert not ok and out['code'] == 'legacy_hold', out
    ok, fresh = owner_act.act('direction.request', {'action': row['action'], 'args': row['args']})
    assert ok and fresh['pending'] and 'copy' in fresh['ask'].lower(), fresh
    ok, out = owner_act.act('direction.confirm', {'request': fresh['request']})
    assert not ok and out['code'] == 'not_confirmed', out
    _owner_types(world, 'yes to the full new request')
    ok, out = owner_act.act('direction.confirm', {'request': fresh['request']})
    assert ok, out


@pytest.mark.parametrize('kind', KINDS)
def test_missing_content_pin_or_snapshot_in_sealed_hold_refuses(world, monkeypatch, kind):
    old = _legacy(world, monkeypatch, kind)
    args = dict(old['args'])
    if kind == 'seeded_rule_undo':
        args['expect'] = routing.read_view()['rules'][0]['revision']
    _owner_types(world, 'hold a fully disclosed new request')
    ok, modern = owner_act.act('direction.request', {'action': old['action'], 'args': args})
    assert ok and modern['pending'], modern
    with owner_direction._requests_locked() as path:
        state = owner_direction._load(path)
        row = next(r for r in state['requests'] if r['id'] == modern['request'])
        assert owner_direction.reviewed_hold(row)
        if kind in ('tracker', 'owed_plan'):
            row['files'].clear()
        elif kind == 'cap_proposal':
            row['args']['pct'] = 1
        else:
            assert row['args'].pop('expect')
        owner_direction._save(path, state, owner_direction._clock())
    _owner_types(world, 'show the held item')
    item = needs_you.waiting(key='held:' + modern['request'])['items'][0]
    assert item['settle']['confirm'] is None
    _owner_types(world, 'yes')
    ok, out = owner_act.act('direction.confirm', {'request': modern['request']})
    assert not ok and out['code'] == 'legacy_hold', out


@pytest.mark.parametrize('front', ['claude_code:cli', 'codex:codex_desktop'])
def test_native_own_request_cannot_also_answer_the_old_question(world, front):
    from tests.test_t3_owner_confirm import _hold_by_request, _front_end_turn, _be_front
    session = 's-native-owner'
    older = _hold_by_request(world, front, session, ago=60)
    _front_end_turn(world, front, session)
    _be_front(world, front, session)
    out = _own_request('rule')
    done = owner_act.apply_request('rule', out)
    assert done.get('applied') is True and not done.get('pending'), done
    ok, out = owner_act.act('direction.confirm', {'request': older})
    assert not ok and out['code'] == 'message_used', out


def test_creation_of_own_request_reserves_turn_before_old_question_can_claim_it(world):
    _earlier_ask(world)
    older = next(r['id'] for r in owner_direction._read_requests(owner_direction.requests_path())
                 if r['state'] == 'pending')
    _owner_types(world, 'add the new rule')
    out = _own_request('rule')
    ok, refused = owner_act.act('direction.confirm', {'request': older})
    assert not ok and refused['code'] == 'message_used', refused
    assert owner_act.apply_request('rule', out).get('applied') is True


def test_same_turn_read_does_not_turn_own_direction_into_an_answer(world):
    _earlier_ask(world)
    older = next(r['id'] for r in owner_direction._read_requests(owner_direction.requests_path())
                 if r['state'] == 'pending')
    _owner_types(world, 'add a new rule, while checking the old question')
    needs_you.waiting()
    out = _own_request('rule')
    done = owner_act.apply_request('rule', out)
    assert done.get('applied') is True, done
    ok, out = owner_act.act('direction.confirm', {'request': older})
    assert not ok and out['code'] == 'not_confirmed', out
    assert next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r['id'] == older)['state'] == 'pending'
