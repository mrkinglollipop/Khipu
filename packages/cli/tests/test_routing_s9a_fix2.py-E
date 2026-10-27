"""Round-two regressions for persisted lane identities and provider diagnostics."""
import json
from datetime import timedelta

import pytest

from khipu import config, routing
from tests.routing_s4_cost_workload import T0, build
from tests.test_routing_cursor import NOW, runner
from tests.test_owner_direction_mcp import _act

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_routing_cursor", "tests.test_owner_direction_mcp")


def test_login_home_never_persisted_in_observation_or_conversation(mac):
    cfg = json.loads(config.config_file().read_text())
    cfg['usage_accounts'] += [{'harness': 'claude', 'label': 'Claude'},
                              {'harness': 'claude', 'label': 'Second', 'home': '/synthetic/second'}]
    config.config_file().write_text(json.dumps(cfg))
    runner(mac, {'secondary-haiku': {'kind': 'claude', 'login_home': '/synthetic/second'}},
           '2026-10-20T15:59:00Z job LIMITED lane=secondary-haiku until=2026-10-20T18:00:00Z\n')
    assert next(a for a in routing.read_view()['accounts'] if a['label'] == 'Second')['state'] == 'limited'
    rows = [json.loads(line) for line in routing.events_file().read_text().splitlines()]
    observations = [row for row in rows if row['type'] == 'observation' and row.get('lane') == 'secondary-haiku']
    assert observations and observations[0]['account'].startswith('claude:')
    assert '/synthetic/second' not in json.dumps([r for r in rows if r['type'] != 'account_migration'])
    assert '/synthetic/second' not in json.dumps(routing._conversation_view(now=NOW), default=str)
    assert next(a for a in routing.read_view()['accounts'] if a['label'] == 'Second')['state'] == 'limited'


@pytest.mark.parametrize('runner_items', [0, 1])
def test_refold_resolves_only_runner_evidence(replay, monkeypatch, runner_items):
    events = build(20_000, routing)
    if runner_items:
        events.append(dict(events[-1], id='runner-evidence', runner='worker', lane='grok'))
    events = [dict(event, _seq=index) for index, event in enumerate(events)]
    calls = []
    resolve = routing.resolve_lane
    def counted(*args, **kwargs):
        calls.append(args[:2])
        return resolve(*args, **kwargs)
    monkeypatch.setattr(routing, 'resolve_lane', counted)
    view = routing.fold(events, cfg=routing.settings(), known=routing.accounts(), now=T0 + timedelta(seconds=1))
    assert view['accounts']
    assert all(runner == 'worker' for runner, lane in calls)
    assert len(calls) == runner_items


@pytest.mark.parametrize('mapping', ['config', 'definition'])
def test_doctor_uses_owner_lane_map(mac, monkeypatch, mapping):
    routing.set_definition('provider', {'id': 'deepseek', 'label': 'DeepSeek', 'family': 'DeepSeek',
                                      'models_allowed': ['deepseek-flash']}, via='app')
    routing.set_definition('account', {'id': 'deepseek:main', 'label': 'DeepSeek', 'provider': 'deepseek'}, via='app')
    runner(mac, {'tier': {'kind': 'grok', 'model': 'v4-pro'}}, '')
    value = {'runner': 'runner', 'lane': 'tier', 'account': 'DeepSeek'}
    if mapping == 'config':
        cfg = json.loads(config.config_file().read_text())
        cfg['routing_lane_map'] = {'runner': {'tier': 'DeepSeek'}}
        config.config_file().write_text(json.dumps(cfg))
    else:
        routing.set_definition('lane_map', value, via='app')
    monkeypatch.setattr(routing, 'prompt_budget_block', lambda cfg=None: {'warn': False})
    checks = routing.doctor_block()['rule_checks']
    assert any(c['lane'] == 'tier' and c['status'] == 'broken' and 'models_allowed' in c['text'] for c in checks)


@pytest.mark.parametrize('label', ['GLM https://example.invalid/?token=synthetic',
                                   'sk-live-0123456789abcdef0123456789abcdef',
                                   'GLM abcdefgh1234567890', 'GLM xai-abcdefghijklmnop'])
def test_owner_provider_label_refuses_url_or_token(world, label):
    world.say('Track GLM', session='owner')
    world.server('owner')
    before = routing.events_file().read_bytes() if routing.events_file().exists() else b''
    error, result = _act('route.definition.set', {'definition': 'provider', 'value': {
        'id': 'glm', 'label': label, 'family': 'Zhipu'}})
    assert error and result['code'] == 'bad_args'
    assert (routing.events_file().read_bytes() if routing.events_file().exists() else b'') == before
