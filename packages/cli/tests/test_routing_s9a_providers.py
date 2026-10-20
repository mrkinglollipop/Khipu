"""Provider facts, shared resolver order and identity-only confirmation."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from khipu import config, routing, routing_delegation, routing_entries
from tests.test_routing_acceptance import T0
from tests.test_routing_cursor import runner, NOW

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_routing_cursor")


@pytest.mark.parametrize('bad', [
    {'probe': 'shell'}, {'probe': '/bin/true'}, {'login': 'credentials'}, {'use': 'always'},
    {'models_allowed': 'muse'}, {'models_never': ['../../private']}, {'lane_names': [1]},
    {'host': 'https://example.invalid/path?token=secret'}, {'home': '/private'},
])
def test_closed_provider_schema(bad):
    assert routing_entries.checked_definition('provider', {'id': 'custom', 'label': 'Custom', 'family': 'Custom', **bad}) is None


def facts(replay):
    for ident, family, fields in [
        ('deepseek', 'DeepSeek', {'models_allowed': ['deepseek-flash']}),
        ('muse', 'Meta', {'models_allowed': ['muse-wonder-full-1.0']}),
        ('antigravity', 'Google', {'use': 'on_request'}),
    ]:
        routing.set_definition('provider', {'id': ident, 'label': ident.title(), 'family': family,
                               'login': 'none', 'probe': 'none', 'lane_names': [ident], **fields}, via='app')
        routing.set_definition('account', {'id': f'{ident}:main', 'label': ident.title(), 'provider': ident}, via='app')


def test_model_facts_reach_mcp_both_pushes_and_advisory(replay, monkeypatch):
    facts(replay)
    body = replay.pull()
    providers = {p['id']: p for p in body['providers']}
    assert providers['deepseek']['models_allowed'] == ['deepseek-flash']
    assert providers['muse']['models_allowed'] == ['muse-wonder-full-1.0']
    assert providers['antigravity']['use'] == 'on_request'
    summary = routing.summary()['text']
    assert all(word in summary for word in ['models_allowed: [deepseek-flash]', 'models_allowed: [muse-wonder-full-1.0]',
                                             'use: on_request'])
    # Both recall paths call these common production delivery functions.
    assert 'models_allowed' in routing.start_push(1.0)['text']
    for provider, model, field in [('deepseek', 'v4-pro', 'models_allowed'), ('muse', 'other', 'models_allowed'),
                                   ('antigravity', 'gemini-3', 'on_request')]:
        payload = {'tool_name': 'spawn_agent', 'tool_input': {'provider': provider, 'model': model}}
        assert field in ' '.join(routing_delegation.provider_advisory(payload))
    assert routing_delegation.provider_advisory({'tool_name': 'spawn_agent', 'tool_input': {
        'provider': 'muse', 'model': 'muse-wonder-full-1.0'}}) == []
    assert routing_delegation.provider_advisory({'tool_name': 'Bash', 'tool_input': {
        'command': 'muse exec --model other "do the task"'}})
    changed = routing.prompt_push({'version': 0}, budget=1.0, now=T0)
    assert 'models_allowed' in changed['text']


def test_provider_owner_boundary_and_words_unchanged(replay):
    words = '  Owner words stay byte-identical.  \n'
    routing.set_note(words, via='app')
    before = routing.read_events()[0][-1]['text']
    facts(replay)
    assert routing.read_view()['note']['text'] == before == words
    with pytest.raises(routing.RouteError):
        routing.conversation_write({'action': 'set_definition', 'definition': 'provider',
                                    'value': {'id': 'custom', 'label': 'Custom'}}, author={'kind': 'conversation'})


def known_accounts():
    return [dict(key='claude|/logins/main', label='Same', harness='claude', home='/logins/main', stable_id='claude:main'),
            dict(key='claude|/logins/second', label='Same', harness='claude', home='/logins/second', stable_id='claude:second')]


def test_resolver_order_conflicts_stale_bindings_and_confirmation():
    known = known_accounts()
    spec = {'provider': 'claude', 'budget_account': 'claude:second', 'login_home': '/logins/second'}
    match = routing.resolve_lane('r', 'main-sonnet', {}, known, spec)
    assert (match['key'], match['matched_by'], match['identity_state']) == (
        'claude|/logins/second', 'declaration', 'claimed')
    for identity in ({'source': 'usage', 'login_home': '/logins/second'}, {'source': 'limit'}):
        assert routing.resolve_lane('r', 'main-sonnet', {}, known, spec, identity=identity)['identity_state'] == 'claimed'
    assert routing.resolve_lane('r', 'main-sonnet', {}, known, spec, identity={
        'source': 'heartbeat', 'login_home': '/logins/main'})['key'] is None
    assert routing.resolve_lane('r', 'x', {}, known, {'budget_account': 'Same'})['candidates'] == sorted(a['key'] for a in known)
    for source in ('heartbeat', 'job_attachment'):
        identity = {'source': source, 'login_home': '/logins/second'}
        assert routing.resolve_lane('r', 'main-sonnet', {}, known, spec, identity=identity)['identity_state'] == 'confirmed'
    for changes in ({'account': 'claude:main'}, {'login_home': '/logins/main'}, {'login_home': '/old-login'},
                    {'budget_account': 'Same'}, {'provider': 'grok'}, {'home': '/logins/main'}):
        assert routing.resolve_lane('r', 'main-sonnet', {}, known, {**spec, **changes})['key'] is None
    assert routing.resolve_lane('r', 'x', {}, known, {'provider': 'claude', 'home': '/logins/main'})['matched_by'] == 'login_home'
    override = {'r': {'x': 'claude:main'}}
    assert routing.resolve_lane('r', 'x', override, known, spec)['key'] == 'claude|/logins/main'
    assert routing.resolve_lane('r', 'x', {'r': {'x': 'Same'}}, known, spec)['key'] is None


def test_fact_name_match_and_lane_report_share_resolver(mac):
    routing.set_definition('provider', {'id': 'deepseek', 'label': 'DeepSeek', 'family': 'DeepSeek', 'probe': 'none',
                           'lane_names': ['small']}, via='app')
    routing.set_definition('account', {'id': 'deepseek:main', 'label': 'DeepSeek', 'provider': 'deepseek'}, via='app')
    runner(mac, {'small': {}},
           '2026-10-20T15:59:00Z job LIMITED lane=small until=2026-10-20T17:00:00Z\n')
    view = routing.read_view()
    assert view['sources']['runners'][0]['lane_matches']['small']['matched_by'] == 'name'
    assert next(a for a in view['accounts'] if a['key'] == 'deepseek:main')['state'] == 'limited'
    routing.report(runner='runner', lane='small', state='open')
    assert routing.read_events()[0][-1]['account'] == 'deepseek:main'


def test_heartbeat_confirms_only_identity_and_never_keeps_paths(mac):
    cfg = json.loads(config.config_file().read_text())
    cfg['usage_accounts'] += [{'harness': 'claude', 'label': 'Main'},
                              {'harness': 'claude', 'label': 'Second', 'home': '/logins/private-second'}]
    config.config_file().write_text(json.dumps(cfg))
    folder = runner(mac, {'tier': {'kind': 'claude', 'budget_account': 'Second',
                                   'home': '/logins/private-second', 'model': 'sonnet'}}, '')
    view = routing.read_view()
    assert view['sources']['runners'][0]['lane_matches']['tier']['identity_state'] == 'claimed'
    beat = folder / 'state/heartbeat.json'
    beat.write_text(json.dumps({'time': NOW.timestamp(), 'interval': 20,
                               'lanes': {'tier': {'provider': 'claude', 'login_home': '/logins/private-second'}}}))
    view = routing.read_view()
    assert view['sources']['runners'][0]['lane_matches']['tier']['identity_state'] == 'confirmed'
    encoded = json.dumps(view['source_cache'])
    # Local account keys remain local; no separate raw home field is retained.
    assert 'login_home' not in encoded and '"home"' not in encoded
    assert '/logins/private-second' not in json.dumps(routing.conversation_view())
    beat.write_text(json.dumps({'time': (NOW - timedelta(hours=2)).timestamp(), 'interval': 20,
                               'lanes': {'tier': {'login_home': '/logins/private-second'}}}))
    assert routing.read_view()['sources']['runners'][0]['lane_matches']['tier']['identity_state'] == 'claimed'


def test_captured_job_confirms_by_its_own_binding(mac):
    from khipu import session_capture as sc
    runner(mac, {'tier': {'kind': 'grok', 'budget_account': 'Grok'}},
                    '2026-10-20T15:59:00Z job LAUNCH lane=tier harness=aegis session=job123\n')
    assert routing.read_view()['sources']['runners'][0]['lane_matches']['tier']['identity_state'] == 'claimed'
    sc.save_state('aegis', 'job123', {'origin': 'job', 'seen_ts': NOW.timestamp(), 'login_home': str(mac[0] / '.grok')})
    # Capture state is rejoined even when every runner file is unchanged.
    view = routing.read_view()
    assert view['sources']['runners'][0]['lane_matches']['tier']['identity_state'] == 'confirmed'
    assert 'job123' not in json.dumps(view['source_cache'])


def test_name_collision_counts_providers_even_without_an_account():
    providers = {'glm': {'lane_names': ['shared']}, 'deepseek': {'lane_names': ['shared']}}
    known = [{'key': 'glm:main', 'label': 'GLM', 'harness': 'glm'}]
    match = routing.resolve_lane('r', 'shared', {}, known, providers=providers)
    assert match['key'] is None and match['candidates'] == ['glm:main']


def test_definition_cannot_borrow_another_providers_login_binding(replay):
    replay.configure(routing_account_bindings={'deepseek:main': {'harness': 'claude', 'home': '/logins/main'}})
    routing.set_definition('account', {'id': 'deepseek:main', 'label': 'DeepSeek', 'provider': 'deepseek'}, via='app')
    known = routing.accounts()
    account = next(a for a in known if a['key'] == 'deepseek:main')
    assert account['home'] is None
    assert routing.resolve_lane('r', 'tier', {}, known, {'budget_account': 'deepseek:main',
                                'provider': 'deepseek', 'login_home': '/logins/main'})['key'] is None


def test_provider_advisory_hook_never_returns_a_permission_decision(replay, monkeypatch):
    from khipu import recall_prompt
    facts(replay)
    class Push:
        def begin(self, *args, **kwargs):
            return self
        def collect(self):
            return {}
        def add_to(self, *args):
            return '', {'status': 'none'}
    monkeypatch.setattr(recall_prompt, '_RoutingAtPrompt', Push)
    monkeypatch.setattr(routing_delegation, '_helper', lambda payload: False)
    payload = {'tool_name': 'spawn_agent', 'tool_input': {'model': 'muse-other'}}
    out, info, kind = routing_delegation.pre_tool_use(payload, {'KHIPU_FEATURE_ROUTING_PUSH': '1'})
    assert kind == 'agent' and info['status'] == 'none'
    context = out['hookSpecificOutput']['additionalContext']
    assert 'models_allowed' in context and 'guidance, not a gate' in context
    assert set(out['hookSpecificOutput']) == {'hookEventName', 'additionalContext'}


def test_provider_checks_flag_real_runner_model_settings(mac, monkeypatch):
    for ident, fields in [('deepseek', {'models_allowed': ['deepseek-flash']}), ('antigravity', {'use': 'on_request'})]:
        routing.set_definition('provider', {'id': ident, 'label': ident.title(), 'family': ident.title(), 'probe': 'none', **fields}, via='app')
        routing.set_definition('account', {'id': ident + ':main', 'label': ident.title(), 'provider': ident}, via='app')
    runner(mac, {'ds': {'provider': 'deepseek', 'model': 'v4-pro'},
                 'gemini': {'provider': 'antigravity', 'model': 'gemini-3'}}, '')
    monkeypatch.setattr(routing, 'prompt_budget_block', lambda cfg=None: {'warn': False})
    checks = routing.doctor_block()['rule_checks']
    assert any(c['lane'] == 'ds' and 'models_allowed' in c['text'] for c in checks)
    assert not any(c['lane'] == 'gemini' for c in checks)


def test_compact_provider_fact_roundtrips_and_conflicting_heartbeat_refuses_report(mac):
    routing._write_many([{'type': 'provider_set', 'id': 'deepseek', 'accounts': ['deepseek:main'], 'family': 'DeepSeek',
                          'written_by': {'kind': 'owner'}, 'lane_names': ['small']}])
    folder = runner(mac, {'small': {}}, '')
    assert routing.read_view()['sources']['runners'][0]['lane_matches']['small']['matched_by'] == 'name'
    # A name match has no declaration to confirm. A conflicting launcher
    # on an explicitly declared tier must not attribute limits or reports.
    (folder / 'config.json').write_text(json.dumps({'lanes': {'tier': {'kind': 'grok', 'budget_account': 'Grok'}}}))
    (folder / 'state/heartbeat.json').write_text(json.dumps({'time': NOW.timestamp(), 'interval': 20,
        'lanes': {'tier': {'provider': 'codex', 'login_home': str(mac[0] / '.codex')}}}))
    (folder / 'events.log').write_text('2026-10-20T15:59:00Z job LIMITED lane=tier until=2026-10-20T17:00:00Z\n')
    view = routing.read_view()
    assert view['sources']['runners'][0]['unmatched_lanes'] == ['tier']
    assert next(a for a in view['accounts'] if a['harness'] == 'grok')['state'] != 'limited'
    with pytest.raises(routing.RouteError):
        routing.report(runner='runner', lane='tier', state='limited')


def test_claude_compatible_job_uses_billing_login_binding(mac):
    from khipu import session_capture as sc
    login = str(mac[0] / '.glm-work')
    cfg = json.loads(config.config_file().read_text())
    cfg['routing_account_bindings'] = {'glm:work': {'harness': 'glm', 'home': login}}
    config.config_file().write_text(json.dumps(cfg))
    routing.set_definition('provider', {'id': 'glm', 'label': 'GLM', 'family': 'Zhipu', 'probe': 'none'}, via='app')
    routing.set_definition('account', {'id': 'glm:work', 'label': 'GLM work', 'provider': 'glm'}, via='app')
    runner(mac, {'tier': {'provider': 'glm', 'budget_account': 'glm:work'}},
           '2026-10-20T15:59:00Z job LAUNCH lane=tier harness=claude_code session=glm-job\n')
    sc.save_state('claude_code', 'glm-job', {'origin': 'job', 'seen_ts': NOW.timestamp(), 'login_home': login})
    match = routing.read_view()['sources']['runners'][0]['lane_matches']['tier']
    assert match['identity_state'] == 'confirmed'
