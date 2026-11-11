"""Owner-approved simple conversation flow; no live state or providers."""
import io
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from khipu import commitments, ledger, needs_you, owner_act, owner_direction, project_group, routing
from tests.test_owner_needs_you import (
    _accounts_request, _owner_types, _priority_request, _setting_request, _settle, _act_on,
)
from tests.test_t3_owner_confirm import (
    THREAD, T3_SESSION, OTHER_THREAD, OTHER_SESSION, _be, _confirm_id,
    _front_end_turn, _be_front, _hold_in_t3, _hold_by_request, _t3_hook_turn, world,
)
assert world


def test_t3_real_owner_message_counts_even_when_hook_hash_differs(world):
    request = _hold_in_t3(world)
    world['t3'].message(THREAD, time.time()+1, text='yes, edited in the conversation')
    _t3_hook_turn(prompt='yes, hook transport prefix')
    ok, out = _confirm_id(request)
    assert ok, out
    assert not _confirm_id(request)[0]


@pytest.mark.parametrize('kind', ['priority', 'setting'])
def test_identical_reread_in_yes_turn_keeps_one_yes(world, kind):
    {'priority': _priority_request, 'setting': _setting_request}[kind](world)
    _owner_types(world, 'what needs me?')
    first = _settle(kind)
    _owner_types(world, 'yes')
    again = _settle(kind)
    assert again == first
    ok, out = _act_on(again)
    assert ok and not out.get('pending'), out


def test_identical_full_held_reread_keeps_yes(world):
    request = _hold_in_t3(world)
    _owner_types(world, 'show the held question')
    first = needs_you.waiting(key='held:'+request)['items'][0]
    _owner_types(world, 'yes')
    again = needs_you.waiting(key='held:'+request)['items'][0]
    assert first['words'] == again['words']
    assert _confirm_id(request)[0]


def test_rereading_an_identical_subset_keeps_other_shown_answers(world):
    _priority_request(world)
    _setting_request(world)
    _owner_types(world, 'show what needs me')
    items = needs_you.waiting()['items']
    priority = next(i for i in items if i['kind'] == 'priority')['settle']['confirm']
    setting = next(i for i in items if i['kind'] == 'setting')
    _owner_types(world, 'yes to the priority')
    needs_you.waiting(key=setting['key'])
    ok, out = _act_on(priority)
    assert ok and not out.get('pending'), out


def test_a_shown_question_does_not_expire_after_thirty_days(world):
    _priority_request(world)
    _owner_types(world, 'show the priority')
    answer = _settle('priority')
    path = owner_direction.requests_path()
    state = owner_direction._load(path)
    read = next(r for r in state['requests'] if r['action'] == owner_direction.READ_ACTION)
    read['settled_ts'] -= 40 * 86400
    owner_direction._save(path, state, time.time())
    assert any(r['id'] == read['id'] for r in owner_direction._read_requests(path))
    _owner_types(world, 'yes')
    assert _settle('priority') == answer
    ok, out = _act_on(answer)
    assert ok and not out.get('pending'), out


def test_changed_content_reread_requires_its_answer(world):
    _setting_request(world, key='routing_summary_chars', raw='100')
    _owner_types(world, 'what needs me?')
    _settle('setting')
    _owner_types(world, 'yes')
    routing.answer_setting_request('routing_summary_chars','undo')
    _setting_request(world, key='routing_summary_chars', raw='0')
    _be(world,T3_SESSION)
    fresh = _settle('setting')
    ok, out = _act_on(fresh)
    assert ok and out['pending'], out
    _owner_types(world,'yes to the changed content')
    assert _confirm_id(out['request'])[0]


@pytest.mark.parametrize('front', ['claude_code:cli', 'codex:codex_desktop'])
def test_native_answer_after_six_hours_still_counts(world, front):
    request = _hold_by_request(world, front, 'native-answer', ago=8*3600)
    _front_end_turn(world, front, 'native-answer', ago=7*3600)
    assert _confirm_id(request)[0]


@pytest.mark.parametrize('harness', ['codex','aegis','cursor','antigravity','grok_build'])
def test_t3_answer_has_no_provider_allowlist_or_process_challenge(world,harness):
    from khipu import owner_turns
    request = _hold_in_t3(world)
    world['t3'].message(THREAD,time.time()+1,text='yes')
    # The T3 owner-message record is the proof, independent of the hook provider.
    world['mp'].setattr(owner_turns,'t3_launched',lambda *a:False)
    path = owner_turns.session_file(T3_SESSION)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row['harness'] = harness
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    assert _confirm_id(request)[0]


def test_legacy_question_does_not_block_direct_note(world):
    from tests.test_owner_gate_completion import _legacy
    request = _legacy(world,world['mp'],'setting')['id']
    with owner_direction._requests_locked() as path:
        state = owner_direction._load(path)
        next(row for row in state['requests'] if row['id'] == request)['args'] = None
        owner_direction._save(path, state, owner_direction._clock())
    _owner_types(world,'set the note instead')
    ok,out=owner_act.act('route.note.set',{'text':'new direction'})
    assert ok and not out.get('pending'),out
    ok,out=_confirm_id(request)
    assert not ok and 'Decline' in out['error'] and 'reissued' in out['error'],out


def test_decline_and_new_direction_share_one_message_with_audit_and_receipts(world):
    request=_hold_in_t3(world)
    _owner_types(world,'no, set the note instead')
    ok,declined=owner_act.act('direction.decline',{'request':request})
    assert ok and declined['receipt'] and declined['owner_action'],declined
    ok,changed=owner_act.act('route.note.set',{'text':'use this direction'})
    assert ok and not changed.get('pending') and changed.get('owner_action'),changed
    assert [a['action'] for a in owner_direction.actions()]==['direction.decline','route.note.set']
    assert owner_direction.actions()[0]['facts']['request_session']==T3_SESSION
    note=owner_direction.actions()[1]
    assert not note.get('no_undo') and note['action'] in owner_direction._UNDO
    assert note.get('receipt')


def test_accounts_words_show_added_and_changed_home(world):
    from khipu import accounts
    _accounts_request(world)
    _owner_types(world,'show the accounts')
    item=next(i for i in needs_you.waiting(key='accounts')['items'] if i['kind']=='accounts')
    assert 'home' in item['words'] and 'default login folder' in item['words']
    from khipu.config import mutate_config
    with mutate_config() as data:
        data['usage_accounts']=[{'label':'Claude work','harness':'claude','home':'/logins/changed'}]
        data['usage_accounts_request']={'request':'change the login','by':routing.conversation_author(),
            'prior':[{'label':'Claude work','harness':'claude','home':'/logins/old'}]}
    item=needs_you.waiting(key='accounts')['items'][0]
    assert '/logins/changed' in item['words'] and '/logins/old' in item['words']


@pytest.mark.parametrize('name',['Cursor','T3 Code','claude'])
def test_terminal_refusal_has_an_explanation(world,monkeypatch,name,capsys):
    class Tty(io.StringIO):
        def isatty(self):return True
    monkeypatch.setattr(sys,'stdin',Tty())
    tree={os.getpid():(991,'/usr/bin/python3'),991:(992,'/usr/bin/script'),992:(1,'/Applications/'+name)}
    monkeypatch.setattr(routing,'_proc_info',lambda pid:tree.get(pid))
    answer=routing.typed_yes('confirm')
    assert not answer and name.lower() in answer.reason.lower()
    assert 'conversation or the app' in capsys.readouterr().err


@pytest.mark.parametrize('kind',['rule_add','rule_edit','priority_set','priority_amend'])
def test_own_request_paths_have_receipts_and_an_undo(world,kind):
    from tests.test_owner_needs_you import _owner_priority
    _owner_priority()
    n=routing.add_rule('Original docs rule',via='app')['n'] if kind=='rule_edit' else None
    _owner_types(world,'the owner directs the routing change')
    shown=routing.conversation_view()
    by=routing.conversation_author()
    if kind.startswith('rule'):
        rule=next((x for x in shown['rules'] if x['n']==n),None)
        out=routing.request_rule('New docs rule',n=n,base=rule['owner_revision'] if rule else None,
                                 author=by,owner_request='free text from the agent')
        family='rule'
    else:
        args={'author':by,'owner_request':'free text from the agent','expect':shown['priority']['version'],
              'base':shown['priority_owner_revision']}
        out=(routing.request_priority([{'account':'Codex','role':'docs'}],**args) if kind=='priority_set'
             else routing.request_priority_amend('Codex',role='docs',**args))
        family='priority'
    done=owner_act.apply_request(family,out)
    assert done.get('applied') and done['instant']['undo'],done
    line=owner_direction._find(done['instant']['owner_action'])
    assert line.get('receipt') and not line.get('no_undo')
    assert line['action'] in owner_direction._UNDO


def test_a_request_can_be_answered_from_another_conversation_with_one_yes(world):
    request=_hold_in_t3(world)
    world['t3'].message(OTHER_THREAD,time.time()+1,text='yes, the agent asked me here')
    _t3_hook_turn(session=OTHER_SESSION,prompt='yes')
    _be(world,OTHER_SESSION)
    ok,out=_confirm_id(request)
    assert ok,out
    assert not _confirm_id(request)[0]


def test_same_owner_message_is_not_a_global_quota(world):
    _owner_types(world,'add a routing rule')
    for text in ('first rule','second rule'):
        out=routing.request_rule(text,owner_request='the agent interprets the owner',author=routing.conversation_author())
        done=owner_act.apply_request('rule',out)
        assert done.get('applied') and done['instant']['undo'],done
        assert owner_direction._find(done['instant']['owner_action']).get('receipt')


@pytest.mark.parametrize('front', [('claude_code','cli'),('codex','codex_desktop'),('aegis','aegis'),
    ('cursor','ide'),('antigravity','app'),('aegis','aegis')],ids=['Claude Code','Codex','Aegis','Cursor','Antigravity','Grok Build'])
def test_one_yes_and_identical_reread_use_the_same_native_flow(world,front):
    from khipu import owner_turns
    session='unified-native'
    path=owner_turns.session_file(session)
    path.parent.mkdir(parents=True,exist_ok=True)
    def typed(ident,ts):
        row={'session':session,'turn':ident,'harness':front[0],'entrypoint':front[1],
             'typed':True,'machine':False,'ts':ts,'at':'2026-11-04T00:00:00Z'}
        with path.open('a') as f:f.write(json.dumps(row)+'\n')
    typed('question',time.time()-1)
    _be(world,session)
    ok,held=owner_act.act('direction.request',{'action':'route.pause','args':{'account':'Grok'}})
    assert ok and held['pending'],held
    needs_you.waiting(key='held:'+held['request'])
    typed('answer',time.time()+1)
    needs_you.waiting(key='held:'+held['request'])
    ok,out=_confirm_id(held['request'])
    assert ok,out
    assert not _confirm_id(held['request'])[0]


def test_decline_from_another_conversation_keeps_its_origin_and_receipt(world):
    request=_hold_in_t3(world)
    world['t3'].message(OTHER_THREAD,time.time()+1,text='no, do something else')
    _t3_hook_turn(session=OTHER_SESSION,prompt='no')
    _be(world,OTHER_SESSION)
    ok,out=owner_act.act('direction.decline',{'request':request})
    assert ok and 'another conversation' in out['receipt'],out
    line=owner_direction._find(out['owner_action'])
    assert line['facts']['request_session']==T3_SESSION
    assert line['session']==OTHER_SESSION and line['confirmed']['source']=='t3'


def test_bulk_caller_returns_the_terminal_reason(world,monkeypatch):
    from khipu import mcp_server
    class Tty(io.StringIO):
        def isatty(self):return True
    monkeypatch.setattr(sys,'stdin',Tty())
    tree={os.getpid():(991,'/usr/bin/python3'),991:(1,'/Applications/Cursor')}
    monkeypatch.setattr(routing,'_proc_info',lambda pid:tree.get(pid))
    monkeypatch.setattr(mcp_server,'_owed_owners_items',lambda ids:ids)
    out=mcp_server.owed_change([1,2],'close',by='pytest',reason='the owner requested this')
    assert out['ok'] is False and 'Cursor'.lower() in out['error'].lower(),out


@pytest.mark.parametrize('caller',['routing','ledger','project','bulk'])
def test_each_terminal_caller_shows_the_explanation(world,monkeypatch,capsys,caller):
    from khipu import cli
    class Tty(io.StringIO):
        def isatty(self):return True
    monkeypatch.setattr(sys,'stdin',Tty())
    tree={os.getpid():(991,'/usr/bin/python3'),991:(1,'/Applications/Cursor')}
    monkeypatch.setattr(routing,'_proc_info',lambda pid:tree.get(pid))
    if caller=='routing':
        assert cli.main(['route','note','set','new note'])!=0
        assert 'cursor' in capsys.readouterr().out.lower()
    elif caller=='ledger':
        with pytest.raises(ledger.LedgerError) as exc:
            ledger.require('ledger project')
        assert 'cursor' in str(exc.value).lower()
    elif caller=='project':
        rc=project_group._gate(SimpleNamespace(from_app=False,by_direction=False),
                               owner_direction.intent('project.keep_separate','one|two'))
        assert rc and 'cursor' in capsys.readouterr().out.lower()
    else:
        assert commitments.bulk_owner_gate(2) is None
        assert 'cursor' in commitments.bulk_owner_refusal().lower()


@pytest.mark.parametrize("failure", [False, OSError("fictional connection failure")])
def test_a_terminal_no_to_another_macs_request_that_could_not_be_recorded_is_not_sent(world, monkeypatch, failure):
    """``owner_act._decline``: a no for the Mac holding the request that could not be recorded
    is reported as not sent, and the request waits again (PR 538 review)."""
    from tests.test_owner_needs_you import _held_pause
    from khipu import routing_replication

    ident = _held_pause(world)
    path = owner_direction.requests_path()
    state = json.loads(path.read_text())
    next(r for r in state["requests"] if r["id"] == ident)["remote"] = {"mac": "m1-id", "request": "req-remote",
                                                                      "sig": "s" * 16}
    path.write_text(json.dumps(state))

    def refuse(*args):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(routing_replication, "answer_remote", refuse)
    ok, out = owner_act._decline({"request": ident}, terminal=True)
    assert not ok and out["code"] == "not_sent" and out["sent"] is False, out
    row = next(r for r in owner_direction._read_requests(path) if r["id"] == ident)
    assert row["state"] == "pending" and "declined" not in row and "owner_action" not in row
    lines = [a for a in owner_direction.actions() if a.get("action") == owner_direction.DECLINE_ACTION]
    assert lines and all(a["state"] == "failed" for a in lines)
    monkeypatch.setattr(routing_replication, "answer_remote", lambda *a: True)
    ok, out = owner_act._decline({"request": ident}, terminal=True)
    assert ok and out["sent"] is True, out


def _held_on_another_mac(world):
    from tests.test_owner_needs_you import _held_pause

    ident = _held_pause(world)
    path = owner_direction.requests_path()
    state = json.loads(path.read_text())
    next(r for r in state["requests"] if r["id"] == ident)["remote"] = {"mac": "m1-id", "request": "req-remote",
                                                                      "sig": "s" * 16}
    path.write_text(json.dumps(state))
    return ident


def _receipts_for(line_id):
    return [r for r in routing.recent_receipts(routing.read_view(), routing._now()) if r["id"] == line_id]


@pytest.mark.parametrize("sent", [True, False])
def test_a_terminal_no_to_another_macs_request_has_a_receipt_only_once_sent(world, monkeypatch, sent):
    """``owner_act._decline``: the routing record's "Declined the request..." is
    written after the no is recorded to send, once, and never for a no not sent."""
    from khipu import routing_replication

    ident = _held_on_another_mac(world)
    monkeypatch.setattr(routing_replication, "answer_remote", lambda *a: sent)
    ok, out = owner_act._decline({"request": ident}, terminal=True)
    assert ok is sent, out
    lines = [a for a in owner_direction.actions() if a.get("action") == owner_direction.DECLINE_ACTION]
    assert len(lines) == 1
    assert len(_receipts_for(lines[0]["id"])) == (1 if sent else 0)
    assert "receipt_due" not in next(r for r in owner_direction._read_requests(owner_direction.requests_path())
                                     if r["id"] == ident)


def test_a_direct_decline_of_another_macs_request_keeps_its_receipt(world):
    """Callers of ``owner_direction.decline`` other than ``owner_act._decline``
    (the old ``owner-direction decline --from-app`` command) write the receipt as before."""
    ident = _held_on_another_mac(world)
    request = owner_direction.decline(ident, app=True)
    assert "receipt_due" not in request
    assert len(_receipts_for(request["owner_action"])) == 1
