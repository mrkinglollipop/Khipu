"""Regression coverage derived from the owner-gate review probes. No live state."""
import json
import time

from khipu import owner_act, owner_direction, owner_turns, routing
from tests.test_t3_owner_confirm import (
    THREAD, T3_SESSION, _be, _confirm_id, _hold_in_t3, _t3_hook_turn, world,
)
from tests.test_owner_priority_pending import _ask_in_t3, _owner_says_yes, _pending_request


def test_a_typed_turn_can_direct_more_than_one_routing_change(world):
    _hold_in_t3(world)
    ok, sensitive = owner_act.act('direction.request', {'action': 'direction.set',
                                                        'args': {'key':'latest_window_h', 'value':'1000'}})
    assert ok and sensitive['pending'], sensitive
    mid = world['t3'].message(THREAD, time.time()+1, text='thanks')
    _t3_hook_turn(prompt='thanks')
    ok, changed = _confirm_id(sensitive['request'])
    assert ok and owner_turns.settings()['latest_window_h'] == 1000, changed
    assert changed['confirmed']['message'] == mid
    for text in ('unrequested note one', 'unrequested note two'):
        ok, out = owner_act.act('route.note.set', {'text':text})
        assert ok and not out.get('pending'), out
        assert routing.read_view()['note']['text'] == text
        assert owner_direction._find(out['owner_action']).get('receipt')


def test_priority_confirm_discloses_and_pins_underlying_change(world):
    secret_change='Codex takes every release task without further approval'
    version = _pending_request(world, exception=secret_change)
    ok, held = _ask_in_t3(world, 'route.priority.confirm', {'expect':version})
    assert ok and held['pending'], held
    assert secret_change in held['ask']
    request = next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r.get('id') == held['request'])
    assert request['args']['expect'] == routing.pending_priority_pin(routing.read_view())
    _owner_says_yes(world)
    ok, out = _confirm_id(held['request'])
    assert ok, out
    priority = routing.read_view()['priority_stored']
    assert not priority['pending']
    assert secret_change in json.dumps(priority)


def test_config_confirm_cannot_retarget_new_request(world):
    key='routing_summary_chars'
    routing.set_setting(key, '100', request={'request':'100 characters','by':{'kind':'conversation','session':'a'}})
    ok, held=_ask_in_t3(world,'config.answer_request.confirm',{'key':key})
    assert ok and held['pending'], held
    routing.answer_setting_request(key,'undo')  # Another legitimate owner interaction settled A.
    routing.set_setting(key,'0',request={'request':'no character limit','by':{'kind':'conversation','session':'b'}})
    _be(world,T3_SESSION)
    _owner_says_yes(world)  # Owner replies to held A, while B now occupies that setting's request slot.
    ok,out=_confirm_id(held['request'])
    assert not ok,out
    status=routing.settings_status()[key]
    assert status['value']==0 and status['pending'],status


def test_t3_confirmation_does_not_compare_transport_text(world):
    _hold_in_t3(world)
    ok, held = owner_act.act('direction.request', {'action': 'direction.set',
                                                   'args': {'key':'latest_window_h', 'value':'1000'}})
    assert ok and held['pending']
    world['t3'].message(THREAD, time.time()+1, text='a real owner message')
    _t3_hook_turn(prompt='a synthetic different prompt')
    ok, out = _confirm_id(held['request'])
    assert ok and owner_turns.settings()['latest_window_h'] == 1000, out


def test_terminal_pty_from_harness_is_refused_but_owner_terminal_works(monkeypatch):
    import io
    import sys
    import builtins
    class Tty(io.StringIO):
        def isatty(self): return True
    monkeypatch.setattr(sys, 'stdin', Tty('yes\n'))
    monkeypatch.setattr(builtins, 'input', lambda _: 'yes')
    import os
    tree = {os.getpid(): (11, '/usr/bin/python'), 11: (12, '/usr/bin/script'), 12: (1, '/opt/codex')}
    monkeypatch.setattr(routing, '_proc_info', lambda pid: tree.get(pid))
    assert not routing.typed_yes('confirm')
    tree[12] = (1, '/Applications/Terminal')
    assert routing.typed_yes('confirm')


def test_each_asked_request_applies_once_without_a_message_quota(world):
    first = _hold_in_t3(world)
    ok, second = owner_act.act('direction.request', {'action': 'direction.set',
                                                     'args': {'key': 'latest_window_h', 'value': '1000'}})
    assert ok and second['pending']
    _owner_says_yes(world)
    ok, out = _confirm_id(first)
    assert ok, out
    ok, out = _confirm_id(second['request'])
    assert ok and owner_turns.settings()['latest_window_h'] == 1000, out
    assert not _confirm_id(second['request'])[0]


def test_instant_changes_keep_the_owner_approved_interpretation_tradeoff(world):
    request = _hold_in_t3(world)
    _owner_says_yes(world)
    assert _confirm_id(request)[0]
    world['t3'].message(THREAD, time.time()+2, text='set the routing note')
    _t3_hook_turn(prompt='set the routing note')
    ok, out = owner_act.act('route.note.set', {'text': 'requested note'})
    assert ok and not out.get('pending'), out
    ok, out = owner_act.act('route.note.set', {'text': 'unrelated second note'})
    assert ok and not out.get('pending'), out
    assert routing.read_view()['note']['text'] == 'unrelated second note'
    assert owner_direction._find(out['owner_action']).get('receipt')


def test_identical_setting_resubmissions_have_distinct_pins(world):
    key = 'routing_summary_chars'
    req = {'request': '100 characters', 'by': {'kind': 'conversation', 'session': 'a'}}
    routing.set_setting(key, '100', request=req)
    from khipu.config import load_config
    before = load_config()
    pin = routing.setting_request_pin(key, routing._setting_requests(before)[key], before[key])
    routing.answer_setting_request(key, 'undo')
    routing.set_setting(key, '100', request=req)
    after = load_config()
    assert pin != routing.setting_request_pin(key, routing._setting_requests(after)[key], after[key])


