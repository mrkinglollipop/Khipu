"""Round-one review regressions; runner fixtures contain declaration shapes only."""
import json

import pytest

from khipu import cli, config, routing, routing_delegation, routing_entries, routing_providers, routing_sources
from tests.test_routing_cursor import runner
from tests.test_owner_direction_mcp import _act

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_routing_cursor", "tests.test_owner_direction_mcp")


def catalog():
    return {'glm': {'id': 'glm', 'label': 'GLM', 'family': 'Zhipu', 'lane_names': ['glm']},
            'deepseek': {'id': 'deepseek', 'label': 'DeepSeek', 'family': 'DeepSeek',
                         'models_allowed': ['deepseek-flash']}}


def known():
    return [{'key': p + '|', 'label': p.title(), 'harness': p} for p in ('glm', 'deepseek', 'claude', 'grok')] + [
        {'key': 'claude:second', 'label': 'Claude second', 'harness': 'claude'}]


@pytest.mark.parametrize('spec,want', [
    ({'kind': 'deepseek', 'model': 'glm-5.3'}, 'glm|'),
    ({'kind': 'deepseek', 'model': 'deepseek-flash'}, 'deepseek|'),
    ({'kind': 'claude', 'budget_account': 'glm|'}, 'glm|'),
    ({'kind': 'claude', 'budget_account': 'deepseek|'}, 'deepseek|'),
    ({'kind': 'claude', 'model': 'glm-5.3'}, 'glm|'),
    ({'kind': 'claude', 'model': 'deepseek-flash'}, 'deepseek|'),
    ({'kind': 'deepseek', 'model': 'glm-5.3', 'budget_account': 'deepseek|'}, None),
    ({'kind': 'deepseek', 'model': 'glm-5.3', 'provider': 'deepseek'}, None),
    ({'kind': 'cursor', 'model': 'auto', 'budget_account': 'grok|'}, 'grok|'),
    ({'kind': 'cursor', 'model': 'auto'}, 'grok|'),
    ({'kind': 'cursor', 'model': 'claude-sonnet-4', 'budget_account': 'grok|'}, 'grok|'),
])
def test_transport_model_and_explicit_payer(spec, want):
    match = routing.resolve_lane('runner', 'tier', {}, known(), spec, catalog())
    assert match['key'] == want
    if want is None:
        assert 'conflict' in match['reason']
    assert routing_sources.model_family('auto') == 'unknown'


def test_claude_transport_login_can_bind_glm():
    accounts = [dict(key='glm:main', harness='glm', home='/synthetic/login', label='GLM')]
    assert routing.resolve_lane('r', 'tier', {}, accounts, {
        'kind': 'claude', 'model': 'glm-5.3', 'login_home': '/synthetic/login'}, catalog())['key'] == 'glm:main'


def test_cached_declaration_resolves_after_owner_creates_account(mac):
    runner(mac, {'tier': {'kind': 'deepseek', 'budget_account': 'deepseek:main'}}, '')
    assert routing.read_view()['sources']['runners'][0]['unmatched_lanes'] == ['tier']
    routing.set_definition('provider', catalog()['deepseek'], via='app')
    routing.set_definition('account', {'id': 'deepseek:main', 'label': 'DeepSeek', 'provider': 'deepseek'}, via='app')
    assert routing.read_view()['sources']['runners'][0]['lane_matches']['tier']['account'] == 'DeepSeek'


def test_retained_config_declaration_resolves_after_owner_creates_account(mac, monkeypatch):
    folder = runner(mac, {'tier': {'kind': 'deepseek', 'budget_account': 'deepseek:main'}}, '')
    assert routing.read_view()['sources']['runners'][0]['unmatched_lanes'] == ['tier']
    read = routing_sources.read_only
    def unreadable(path, *args, **kwargs):
        if path == folder / 'config.json':
            raise PermissionError('synthetic unavailable config')
        return read(path, *args, **kwargs)
    monkeypatch.setattr(routing_sources, 'read_only', unreadable)
    routing.set_definition('provider', catalog()['deepseek'], via='app')
    routing.set_definition('account', {'id': 'deepseek:main', 'label': 'DeepSeek', 'provider': 'deepseek'}, via='app')
    status = routing.read_view()['sources']['runners'][0]
    assert status['files']['config']['error']
    assert status['lane_matches']['tier']['account'] == 'DeepSeek'


@pytest.mark.parametrize("main_label", ["Main", "Claude"])
def test_bare_provider_word_stays_ambiguous_after_cache(replay, main_label):
    replay.configure(usage_accounts=[{'harness': 'claude', 'label': main_label},
                                    {'harness': 'claude', 'label': 'Second', 'home': '/synthetic/second'}])
    spec = routing_sources._parse_config(json.dumps({'lanes': {'secondary-haiku': {
        'kind': 'claude', 'budget_account': 'claude'}}}).encode())['lanes']['secondary-haiku']
    accounts = routing.accounts()
    match = routing.resolve_lane('r', 'secondary-haiku', {}, accounts, spec)
    assert match['key'] is None
    assert match['candidates'] == sorted(a['key'] for a in accounts)
    assert routing.resolve_lane('r', 'main', {'r': {'main': main_label}}, accounts)['key'] == 'claude|'


def test_provider_family_is_required():
    assert routing_entries.checked_definition('provider', {'id': 'custom', 'label': 'Custom'}) is None


@pytest.mark.parametrize('model', ['deepseek-reasoner', 'deepseek-chat', 'v4-pro'])
def test_deepseek_flash_only_fact_reaches_dispatch(replay, model):
    from tests.test_routing_s9a_providers import facts
    facts(replay)
    assert 'models_allowed' in ' '.join(routing_delegation.provider_advisory({
        'tool_name': 'spawn_agent', 'tool_input': {'provider': 'deepseek', 'model': model}}))


def test_missing_model_is_unknown_not_a_policy_violation():
    fact = {'id': 'muse', 'label': 'Muse', 'models_allowed': ['muse-wonder-full-1.0']}
    assert routing_providers.advisory(fact, None) == []


def test_requested_use_reminder_does_not_claim_owner_request_is_absent(replay):
    routing.set_definition('provider', {'id': 'antigravity', 'label': 'Antigravity',
                                      'family': 'Google', 'use': 'on_request'}, via='app')
    texts = routing_delegation.provider_advisory({'tool_name': 'spawn_agent',
        'tool_input': {'provider': 'antigravity', 'model': 'gemini-3'}})
    assert texts and 'on_request' in texts[0]
    assert 'needed for this dispatch' not in texts[0]


def test_name_step_normalizes_provider_and_account():
    match = routing.resolve_lane('r', 'small', {}, known(), providers={
        'cursor': {'id': 'cursor', 'family': 'unknown', 'lane_names': ['small']}})
    assert match['key'] == 'grok|'


def test_rule13_budget_and_concurrency_remain_outside_provider_schema(replay):
    words = 'DeepSeek uses deepseek-flash only, at most $35 per rolling 7 days. GLM runs one request at a time.'
    routing.add_rule(words, via='app')
    for field in ('spend', 'concurrency'):
        assert routing_providers.checked({**catalog()['deepseek'], field: 1}) is None
    for fact in catalog().values():
        routing.set_definition('provider', fact, via='app')
    assert replay.view()['rules'][0]['text'] == words


@pytest.mark.parametrize('surface', ['mcp', 'cli'])
def test_owner_definition_writer_creates_matching_real_lane_shapes(mcp_world, surface):
    mcp_world.say('Track GLM and DeepSeek and their model restrictions.', session='owner')
    mcp_world.server('owner')
    for ident, fact in catalog().items():
        for name, value in [('provider', fact), ('account', {
            'id': ident + ':main', 'label': ident.title(), 'provider': ident})]:
            if surface == 'mcp':
                error, out = _act('route.definition.set', {'definition': name, 'value': value})
                assert not error, out
            else:
                args = cli.build_parser().parse_args(['route', 'definition', 'set', name,
                                                      '--value=' + json.dumps(value), '--owner-direction'])
                assert args.func(args) == 0
    for ident, spec in [('glm', {'kind': 'deepseek', 'model': 'glm-5.3'}),
                        ('deepseek', {'kind': 'deepseek', 'model': 'deepseek-flash'})]:
        assert routing.resolve_lane('r', ident, {}, routing.accounts(), spec, catalog())['key'] == ident + ':main'
    mcp_world.server(None)
    assert _act('route.definition.set', {'definition': 'provider', 'value': catalog()['glm']})[0]


@pytest.mark.parametrize("definition", ["provider", "account", "lane_map"])
def test_definition_owner_undo_restores_prior_fact_and_removes_new_fact(mcp_world, definition):
    from khipu import owner_direction
    mcp_world.say('Track GLM.', session='owner')
    mcp_world.server('owner')
    fact = {'provider': catalog()['glm'],
            'account': {'id': 'glm:main', 'label': 'GLM', 'provider': 'glm'},
            'lane_map': {'runner': 'r', 'lane': 'tier', 'account': 'codex:main'}}[definition]
    changed = dict(fact, **({'account': 'grok:main'} if definition == 'lane_map' else {'label': 'Work GLM'}))
    def current():
        definitions = routing.read_view()['definitions']
        if definition == 'lane_map':
            account = (definitions['lane_maps'].get('r') or {}).get('tier')
            return dict(fact, account=account) if account else None
        return definitions['providers' if definition == 'provider' else 'accounts'].get(fact['id'])
    error, out = _act('route.definition.set', {'definition': definition, 'value': fact})
    assert not error, out
    first = out['owner_action']
    error, out = _act('route.definition.set', {'definition': definition, 'value': changed})
    assert not error, out
    assert not _act('direction.undo', {'id': out['owner_action']})[0]
    assert current() == fact
    assert not _act('direction.undo', {'id': first})[0]
    assert current() is None
    assert all(not line.get('no_undo') for line in owner_direction.read_log() if line.get('kind') == 'action')


def test_real_church_lane_shapes_and_owner_secondary_override(mac):
    cfg = json.loads(config.config_file().read_text())
    cfg['usage_accounts'] += [{'harness': 'claude', 'label': 'Claude'},
                              {'harness': 'claude', 'label': 'Claude second', 'home': '/synthetic/second'}]
    cfg['routing_lane_map'] = {'runner': {'secondary-haiku': 'Claude second'}}
    config.config_file().write_text(json.dumps(cfg))
    for ident, fact in catalog().items():
        routing.set_definition('provider', fact, via='app')
        routing.set_definition('account', {'id': ident + ':main', 'label': ident.title(), 'provider': ident}, via='app')
    runner(mac, {'glm': {'kind': 'deepseek', 'model': 'glm-5.3'},
                 'deepseek': {'kind': 'deepseek', 'model': 'deepseek-flash'},
                 'cursor-auto': {'kind': 'cursor', 'budget_account': 'grok|'},
                 'cursor-grok': {'kind': 'cursor', 'budget_account': 'grok|'},
                 'main-haiku': {'kind': 'claude', 'budget_account': 'claude|'},
                 'secondary-haiku': {'kind': 'claude'}}, '')
    matches = routing.read_view()['sources']['runners'][0]['lane_matches']
    assert {name: value['account'] for name, value in matches.items()} == {
        'glm': 'Glm', 'deepseek': 'Deepseek', 'cursor-auto': 'Grok', 'cursor-grok': 'Grok',
        'main-haiku': 'Claude', 'secondary-haiku': 'Claude second'}


def test_app_definition_argv_reaches_the_same_owner_writer(mcp_world, khipu_app):
    args = ['route', 'definition', 'set', 'provider', '--value=' + json.dumps(catalog()['glm']), '--from-app']
    ns = cli.build_parser().parse_args(args)
    assert ns.func(ns) == 2
    khipu_app.as_app()
    ns = cli.build_parser().parse_args(args)
    assert ns.func(ns) == 0
    assert routing.read_view()['definitions']['providers']['glm'] == catalog()['glm']


@pytest.mark.parametrize('definition', [[], {}, 'other'])
def test_definition_action_rejects_invalid_kind_before_value(definition):
    from khipu import owner_act
    ok, out = owner_act.act('route.definition.set', {'value': catalog()['glm'], 'definition': definition})
    assert not ok and out['code'] == 'bad_args'
