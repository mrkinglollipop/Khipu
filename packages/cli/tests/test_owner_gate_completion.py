"""Legacy persistence and same-turn owner request regressions; synthetic state only."""
import json


from khipu import accounts, needs_you, owner_act, owner_direction, routing
from tests.test_owner_needs_you import (
    _accounts_request, _cap_request, _earlier_ask, _owner_types, _priority_request,
    _rule_added, _rule_changed, _setting_request, _tier_request,
)
from tests.test_t3_owner_confirm import world

assert world

KINDS = ('priority', 'priority_undo', 'rule', 'rule_undo', 'proposal', 'setting', 'tier',
         'seeded_rule_undo', 'cap_proposal')


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
        path.write_text(json.dumps({'project': 'p', 'items': []}))
        raise AssertionError(f'unknown fixture kind {kind}')
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




def _own_request(kind):
    if kind == 'rule':
        return routing.request_rule('Docs go to Haiku', owner_request='add a rule',
                                    author=routing.conversation_author(), via='mcp')
    shown = routing.conversation_view()
    return routing.request_priority_amend('Codex', role='Docs go to Codex', owner_request='change Codex role',
                                           author=routing.conversation_author(), via='mcp',
                                           expect=shown['priority']['version'], base=shown['priority_owner_revision'])


def test_own_request_on_first_proven_t3_turn_needs_no_question(world):
    _owner_types(world, 'add a rule')
    out = _own_request('rule')
    done = owner_act.apply_request('rule', out)
    assert done.get('applied') is True and not done.get('pending'), done


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
