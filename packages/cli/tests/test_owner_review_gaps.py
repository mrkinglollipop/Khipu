import time
import pytest
from khipu import needs_you, owner_act, owner_direction, routing
from tests.test_t3_owner_confirm import world, THREAD, T3_SESSION, OTHER_SESSION, OTHER_THREAD, _be, _t3_hook_turn
from tests.test_owner_needs_you import (_owner_types, _tier_request, _setting_request,
    _rule_added, _priority_request, _earlier_ask, _settle, _act_on, _setting, TIERS, OTHER)


def test_old_tier_undo_cannot_overwrite_new_owner_table_after_same_proposal_returns(world):
    old = dict(TIERS, docs='haiku')
    new = dict(TIERS, docs='opus')
    routing.set_tier_table(old, via='app')
    _tier_request(world)
    _owner_types(world, 'what is waiting?')
    undo = _settle('tier_table', 'undo')
    routing.set_tier_table(new, via='app')
    _tier_request(world)
    _owner_types(world, 'no to the proposal I saw')
    ok, out = _act_on(undo)
    actual = routing.read_view()['tier_table']['tiers']
    assert not ok and actual == new, {'ok': ok, 'actual_docs': actual['docs'], 'out': out}

@pytest.mark.parametrize('source', ['web', 'mobile'])
@pytest.mark.parametrize('kind', ['priority', 'setting', 'tier'])
def test_one_yes_for_t3_sources(world, source, kind):
    {'priority': _priority_request, 'setting': _setting_request, 'tier': _tier_request}[kind](world)
    world['t3'].message(THREAD, time.time() - 1, source=source, text='what needs confirmation?')
    _t3_hook_turn(prompt='what needs confirmation?')
    _be(world, T3_SESSION)
    settle = _settle({'tier': 'tier_table'}.get(kind, kind))
    world['t3'].message(THREAD, time.time() + 1, source=source, text='yes')
    _t3_hook_turn(prompt='yes')
    ok, out = _act_on(settle)
    assert ok and not out.get('pending'), out

def test_other_thread_read_cannot_authorize_priority(world):
    _priority_request(world)
    _owner_types(world, 'show pending')
    settle = _settle('priority')
    world['t3'].message(OTHER_THREAD, time.time() - 1, text='earlier')
    _t3_hook_turn(session=OTHER_SESSION)
    _be(world, OTHER_SESSION)
    ok, held = owner_act.act('route.pause', {'account': 'Grok'})
    assert ok and held.get('pending'), held
    world['t3'].message(OTHER_THREAD, time.time() + 1, text='yes')
    _t3_hook_turn(session=OTHER_SESSION, prompt='yes')
    ok, out = _act_on(settle)
    assert ok and out.get('pending'), out
    assert routing.read_view()['priority']['pending']

def test_no_new_owner_message_after_full_read_never_applies(world):
    _priority_request(world)
    _earlier_ask(world)
    _owner_types(world, 'what is for lunch')
    settle = _settle('priority')
    ok, out = _act_on(settle)
    assert ok and out.get('pending'), out
    ok, refused = owner_act.act('direction.confirm', {'request': out['request']})
    assert not ok and refused.get('code') == 'not_confirmed', refused

def test_cap_words_show_scope_and_slack(world):
    _be(world, OTHER)
    routing.request_cap('Codex', kind='pace_target', window='weekly', pct=40, slack=25, revision='probe',
                        purpose='check', tier='opus', sensitive_only=True,
                        author=routing.conversation_author())
    _owner_types(world, 'show cap')
    item = next(i for i in needs_you.waiting()['items'] if i['kind'] == 'cap_proposal')
    assert not item.get('cut'), item
    assert all(word in item['words'] for word in ['check', 'opus', 'sensitive', '25']), item

def test_seeded_rule_undo_cannot_retire_newer_owner_words(world):
    routing._write({'type': 'rule_add', 'n': 1, 'text': 'Old seed', 'seeded': True,
                    'written_by': {'kind': 'seed', 'file': 'rules.json'}, 'via': 'seed'})
    _owner_types(world, 'what rule is waiting?')
    undo = _settle('rule', 'undo')
    routing.edit_rule(1, 'NEW OWNER RULE', via='app')
    _owner_types(world, 'no to the old seed')
    ok, out = _act_on(undo)
    rules = routing.read_view()['rules']
    assert not ok and any(r['text'] == 'NEW OWNER RULE' for r in rules), {'ok': ok, 'out': out, 'rules': rules}
