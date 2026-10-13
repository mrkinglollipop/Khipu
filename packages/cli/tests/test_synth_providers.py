"""Provider routing, requests, accounting and keys: fakes/temp only."""
import io
import json
import socket
import urllib.error
from contextlib import ExitStack, redirect_stdout
from unittest import mock

import pytest
from khipu import cli, config, embed, extract, keychain, models, rerank
from tests.test_cli_recall_quality import _green_doctor_patches


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    for key in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY',
                'KHIPU_EXTRACT_MODEL', 'KHIPU_SYNTH_PROVIDER', 'KHIPU_TEST'):
        monkeypatch.delenv(key, raising=False)
    def blocked(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)


class Reply:
    def __init__(self, data):
        self.data = data
    def read(self):
        return json.dumps(self.data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


@pytest.mark.parametrize('provider,model', [('gemini', 'gemini-2.5-flash'),
                                           ('anthropic', 'claude-haiku-5-5'), ('openai', 'explicit')])
def test_request_prompt_usage_and_cost(provider, model):
    prompt = extract.PROMPT.format(cwd='/fixture', transcript='USER: Keep the decision.')
    data = {
        'gemini': {'candidates': [{'content': {'parts': [{'text': 'a'}, {'text': 'b'}]}}],
                   'usageMetadata': {'promptTokenCount': 1000, 'candidatesTokenCount': 200,
                                     'cachedContentTokenCount': 400}},
        'anthropic': {'content': [{'text': 'a'}, {'type': 'tool_use'}, {'type': 'text', 'text': 'b'}],
                      'usage': {'input_tokens': 1000, 'output_tokens': 200, 'cache_read_input_tokens': 400}},
        'openai': {'choices': [{'message': {'content': 'ab'}}],
                   'usage': {'prompt_tokens': 1000, 'completion_tokens': 200,
                             'prompt_tokens_details': {'cached_tokens': 400}}},
    }[provider]
    models.set_models_merge_role('synth', provider=provider, model_id=model)
    with mock.patch.object(extract, '_provider_key', return_value='fake-secret'), \
         mock.patch.object(extract, '_key', return_value='fake-secret'), \
         mock.patch.object(extract.urllib.request, 'urlopen', return_value=Reply(data)) as post:
        assert extract._generate(prompt, retries=0, caller='fixture') == 'ab'
    request = post.call_args.args[0]
    body = json.loads(request.data)
    headers = dict((k.lower(), v) for k, v in request.header_items())
    assert headers['content-type'] == 'application/json'
    assert 'fake-secret' not in request.full_url
    if provider == 'gemini':
        assert request.full_url == f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
        assert body['contents'][0]['parts'][0]['text'].encode() == prompt.encode()
        assert body['generationConfig'] == {'temperature': .2, 'maxOutputTokens': 4096,
                                             'responseMimeType': 'application/json',
                                             'thinkingConfig': {'thinkingBudget': 0}}
        assert headers['x-goog-api-key'] == 'fake-secret'
    else:
        assert body == {'model': model, 'max_tokens': 4096, 'temperature': .2,
                        'messages': [{'role': 'user', 'content': prompt}]}
        assert body['messages'][0]['content'].encode() == prompt.encode()
        if provider == 'anthropic':
            assert request.full_url == 'https://api.anthropic.com/v1/messages'
            assert headers['x-api-key'] == 'fake-secret'
            assert headers['anthropic-version'] == '2023-06-01'
        else:
            assert request.full_url == 'https://api.openai.com/v1/chat/completions'
            assert headers['authorization'] == 'Bearer fake-secret'
    row = json.loads(extract.synth_usage_path().read_text())
    assert (row['provider'], row['cached_tokens'], row['prompt_tokens'], row['output_tokens']) == (provider, 400, 1000, 200)
    usage = extract.synth_usage_today()
    assert usage['by_provider'][provider]['by_model'][model]['calls'] == 1
    cost = {'gemini': .000692, 'anthropic': .000204, 'openai': None}[provider]
    assert usage['estimated_usd'] == cost
    assert usage['by_provider'][provider]['estimated_usd'] == cost
    assert cli._budget_status_all()['synth']['usage_by_provider'] == usage['by_provider']


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai'])
@pytest.mark.parametrize('code,attempts', [(429, 3), (500, 3), (400, 1), (401, 1), (403, 1)])
def test_http_retry_accounting_and_redaction(provider, code, attempts):
    fn = getattr(extract, '_generate_' + provider)
    error = urllib.error.HTTPError('https://fixture', code, 'error', {}, io.BytesIO(b'fake-secret echoed body'))
    with mock.patch.object(extract, '_provider_key', return_value='fake-secret'), \
         mock.patch.object(extract, '_key', return_value='fake-secret'), \
         mock.patch.object(extract.time, 'sleep') as sleep, \
         mock.patch.object(extract.urllib.request, 'urlopen', side_effect=error) as post:
        with pytest.raises(RuntimeError, match=f'HTTP {code}') as exc:
            fn('p', model_id='m', timeout=1, retries=2)
    assert 'fake-secret' not in str(exc.value)
    assert post.call_count == attempts
    assert embed.budget_status('synth')['calls'] == attempts
    assert sleep.call_count == (attempts if attempts == 3 else 0)


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai'])
def test_budget_ceiling_unwrapped_before_http(provider):
    fn = getattr(extract, '_generate_' + provider)
    config.set_int_setting('synth_daily_calls', 1)
    with mock.patch.object(extract, '_provider_key', return_value='fake'), \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract.time, 'sleep'), \
         mock.patch.object(extract.urllib.request, 'urlopen', side_effect=OSError('fake')) as post:
        with pytest.raises(embed.BudgetExhausted):
            fn('p', model_id='m', timeout=1, retries=2)
    assert post.call_count == 1


def test_models_alias_defaults_switch_and_role_semantics():
    models.set_models_merge_role('synth', provider='cloud', model_id='custom-gemini')
    assert models.show_models()['synth']['provider'] == 'gemini'
    assert config.load_config()['models']['synth']['provider'] == 'gemini'
    assert models.show_models()['embed']['provider'] == 'cloud'
    assert models.set_models_merge_role('synth', provider='anthropic')['synth']['model_id'] == 'claude-haiku-5-5'
    assert models.set_models_merge_role('synth', provider='gemini')['synth']['model_id'] == 'gemini-2.5-flash'
    before = config.load_config()
    with pytest.raises(ValueError, match='requires --model'):
        models.set_models_merge_role('synth', provider='openai')
    assert config.load_config() == before
    args = cli.build_parser().parse_args(['models', 'set', '--role', 'synth', '--provider', 'openai', '--model', 'explicit'])
    with redirect_stdout(io.StringIO()):
        assert args.func(args) == 0
    with pytest.raises(ValueError, match='requires --model'):
        models.set_models_merge_role('synth', provider='openai')
    for role in ('embed', 'vision'):
        with pytest.raises(ValueError, match='unknown provider'):
            models.set_models_merge_role(role, provider='anthropic', model_id='m')


def test_test_override_changes_model_and_env_model_wins(monkeypatch):
    monkeypatch.setenv('KHIPU_TEST', '1')
    monkeypatch.setenv('KHIPU_SYNTH_PROVIDER', 'anthropic')
    assert models.synth_settings()['model_id'] == 'claude-haiku-5-5'
    monkeypatch.setenv('KHIPU_EXTRACT_MODEL', 'override')
    assert models.cloud_model_id({'provider': 'openai'}) == 'override'


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_key_resolution_and_secret_set(provider, tmp_path, monkeypatch):
    resolve = getattr(keychain, 'resolve_' + provider + '_key')
    account = provider + '_api_key'
    env = provider.upper() + '_API_KEY'
    with pytest.raises(RuntimeError, match='No '):
        resolve()
    path = tmp_path / 'fake-key'
    path.write_text('file-fixture\n')
    config.set_path_setting(provider + '_key_file', str(path))
    assert resolve() == 'file-fixture'
    with mock.patch.object(keychain, 'get_password', return_value='kc-fixture'):
        assert resolve() == 'kc-fixture'
        monkeypatch.setenv(env, 'env-fixture')
        assert resolve() == 'env-fixture'
    monkeypatch.delenv(env)
    assert keychain.secrets_status()[provider + '_file_key_present']
    args = cli.build_parser().parse_args(['secrets', '--set', account])
    with mock.patch.object(keychain, 'set_password') as setter, \
         mock.patch.object(keychain, 'secrets_status', return_value={provider + '_in_keychain': True}), \
         mock.patch('sys.stdin', io.StringIO('stdin-fixture\n')), redirect_stdout(io.StringIO()) as out:
        assert args.func(args) == 0
    setter.assert_called_once_with(account, 'stdin-fixture')
    assert json.loads(out.getvalue())['stored']
    assert 'stdin-fixture' not in out.getvalue()


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai', 'local'])
@pytest.mark.parametrize('present', [True, False])
def test_doctor_selected_provider_key(provider, present, tmp_path):
    options = {'model_id': 'm'}
    if provider == 'local':
        options['endpoint'] = 'http://localhost:1'
    models.set_models_merge_role('synth', provider=provider, **options)
    with ExitStack() as stack:
        for patch in _green_doctor_patches():
            if patch.attribute != 'secrets_status':
                stack.enter_context(patch)
        stack.enter_context(mock.patch.object(keychain, 'secrets_status', return_value={
            'dsn_file': {'ok': True}, provider + '_in_keychain': present}))
        stack.enter_context(mock.patch('khipu.probe.status', return_value={'ok': True}))
        stack.enter_context(mock.patch('khipu.recall_prompt.prompt_recall_outcomes', return_value={'ok': True}))
        stack.enter_context(mock.patch('khipu.drift.recall_quality', return_value={}))
        output = stack.enter_context(redirect_stdout(io.StringIO()))
        cli.cmd_doctor(cli.build_parser().parse_args(['doctor']))
    doc = json.loads(output.getvalue())
    assert doc['synth_provider'] == {'provider': provider, 'model': 'm', 'key_present': present and provider != 'local'}
    assert doc['synth_provider_ok'] == (present or provider == 'local')
    if not present and provider != 'local':
        assert not doc['ok']


def test_empty_key_file_does_not_count_as_key(tmp_path):
    path = tmp_path / 'empty'
    path.write_text(' \n')
    config.set_path_setting('anthropic_key_file', str(path))
    status = keychain.secrets_status()
    assert status['anthropic_file_present']
    assert not status['anthropic_file_key_present']
    with pytest.raises(RuntimeError):
        keychain.resolve_anthropic_key()


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_rerank_dispatch_and_missing_key_mapping(provider):
    models.set_models_merge_role('synth', provider=provider, model_id='m')
    with mock.patch.object(extract, '_provider_key', return_value='fake'), \
         mock.patch.object(extract, '_generate', return_value='[0]') as gen:
        assert rerank._call_model('p', timeout=1) == ('[0]', 'm')
    gen.assert_called_once_with('p', timeout=1, retries=0, caller='rerank')
    with pytest.raises(rerank._Unavailable):
        rerank._call_model('p', timeout=1)


def test_provider_grouping_legacy_rows_and_unknown_cost():
    extract._record_usage({'usageMetadata': {'promptTokenCount': 1000}}, model_id='gemini-2.5-flash', caller='test')
    extract._record_usage({'usage': {'input_tokens': 1000, 'cache_read_input_tokens': 400}}, provider='anthropic', model_id='claude-haiku-5-5', caller='test')
    path = extract.synth_usage_path()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0].pop('provider')  # pre-phase-1 Gemini row
    rows.append({**rows[1], 'provider': 'openai', 'model': 'unknown'})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    usage = extract.synth_usage_today()
    assert usage['by_provider']['gemini']['estimated_usd'] == .0003
    assert usage['by_provider']['anthropic']['estimated_usd'] == .000104
    assert usage['by_provider']['openai']['estimated_usd'] is None
    assert usage['estimated_usd'] is None
    assert usage['calls'] == 3
    assert usage['cached_tokens'] == 800
    budget = cli._budget_status_all()['synth']
    assert budget['estimated_usd_today'] is None
    assert budget['usage_by_provider']['openai']['estimated_usd'] is None


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai'])
def test_transport_retry_redaction_and_missing_key_no_charge(provider):
    fn = getattr(extract, '_generate_' + provider)
    with mock.patch.object(extract, '_provider_key', return_value='fake'), \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract.time, 'sleep'), \
         mock.patch.object(extract.urllib.request, 'urlopen', side_effect=OSError('secret-in-transport')) as post:
        with pytest.raises(RuntimeError) as exc:
            fn('p', model_id='m', timeout=1, retries=1)
    assert 'secret-in-transport' not in str(exc.value)
    assert post.call_count == 2
    with pytest.raises(RuntimeError):
        fn('p', model_id='m', timeout=1, retries=0)
    assert embed.budget_status('synth')['calls'] == 2


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_rerank_configured_model_reaches_adapter(provider):
    models.set_models_merge_role('synth', provider=provider, model_id='custom-model')
    with mock.patch.object(extract, '_provider_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_' + provider, return_value='[1]') as adapter:
        assert rerank._call_model('unchanged-prompt', timeout=1.5) == ('[1]', 'custom-model')
    adapter.assert_called_once_with('unchanged-prompt', model_id='custom-model', timeout=1.5,
                                   retries=0, caller='rerank')


def test_extraction_fixture_prompt_is_byte_identical_across_adapters():
    transcript = 'USER: Keep append-only history.\nASSISTANT: Implemented and tested.'
    answer = json.dumps({'summary': 'Kept append-only history.', 'topics': ['history'],
                         'decisions': ['Keep append-only history.'], 'preferences': [],
                         'scope': 'fixture', 'open_loops': [], 'closed_loops': []})
    prompts = []
    for provider, model in [('gemini', 'gemini-2.5-flash'),
                             ('anthropic', 'claude-haiku-5-5'), ('openai', 'explicit')]:
        models.set_models_merge_role('synth', provider=provider, model_id=model)
        def respond(request, **kwargs):
            body = json.loads(request.data)
            if provider == 'gemini':
                prompts.append(body['contents'][0]['parts'][0]['text'].encode())
                return Reply({'candidates': [{'content': {'parts': [{'text': answer}]}}]})
            prompts.append(body['messages'][0]['content'].encode())
            if provider == 'anthropic':
                return Reply({'content': [{'type': 'text', 'text': answer}]})
            return Reply({'choices': [{'message': {'content': answer}}]})
        with mock.patch.object(extract, '_provider_key', return_value='fake'), \
             mock.patch.object(extract, '_key', return_value='fake'), \
             mock.patch.object(extract.urllib.request, 'urlopen', side_effect=respond):
            result = extract.extract_memory(transcript, cwd='/fixture')
        assert result['summary'] == 'Kept append-only history.'
        assert result['decisions'] == ['Keep append-only history.']
    assert len(prompts) == 3
    assert prompts[0] == prompts[1] == prompts[2]
    assert transcript.encode() in prompts[0]


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_keychain_lookup_account_and_khipu_service(provider):
    with mock.patch.object(keychain, 'keychain_available', return_value=True), \
         mock.patch.object(keychain, '_security', return_value=mock.Mock(returncode=0, stdout='fake-key\n')) as security:
        assert getattr(keychain, 'get_' + provider + '_key')() == 'fake-key'
    security.assert_called_once_with('find-generic-password', '-s', keychain.SERVICE,
                                    '-a', provider + '_api_key', '-w')
    assert keychain.SERVICE == 'Khipu'
