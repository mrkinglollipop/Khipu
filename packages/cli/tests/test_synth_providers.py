"""Provider routing, requests, accounting and keys: fakes/temp only."""
import io
import json
import socket
import subprocess
import urllib.error
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock
from uuid import UUID

import pytest
from khipu import cli, config, embed, extract, integrations, keychain, models, rerank, synth_cli
from khipu import session_capture as sc
from tests.test_cli_recall_quality import _green_doctor_patches


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'capture'))
    for key in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'GEMINI_API_KEY',
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
                                           ('anthropic', 'claude-haiku-5-5'), ('openai', 'explicit'),
                                           ('openrouter', 'openai/gpt-6-luna')])
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
        'openrouter': {'choices': [{'message': {'content': 'ab'}}],
                       'usage': {'prompt_tokens': 1000, 'completion_tokens': 200,
                                 'prompt_tokens_details': {'cached_tokens': 400}, 'cost': .001}},
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
        expected = {'model': model, 'max_tokens': 4096, 'temperature': .2,
                    'messages': [{'role': 'user', 'content': prompt}]}
        if provider == 'openrouter':
            expected['usage'] = {'include': True}
        assert body == expected
        assert body['messages'][0]['content'].encode() == prompt.encode()
        if provider == 'anthropic':
            assert request.full_url == 'https://api.anthropic.com/v1/messages'
            assert headers['x-api-key'] == 'fake-secret'
            assert headers['anthropic-version'] == '2023-06-01'
        elif provider == 'openrouter':
            assert request.full_url == 'https://openrouter.ai/api/v1/chat/completions'
            assert headers['authorization'] == 'Bearer fake-secret'
            assert headers['http-referer'] == 'https://github.com/mrkinglollipop/Khipu'
            assert headers['x-title'] == 'Khipu'
        else:
            assert request.full_url == 'https://api.openai.com/v1/chat/completions'
            assert headers['authorization'] == 'Bearer fake-secret'
    row = json.loads(extract.synth_usage_path().read_text())
    assert (row['provider'], row['cached_tokens'], row['prompt_tokens'], row['output_tokens']) == (provider, 400, 1000, 200)
    if provider == 'openrouter':
        assert row['cost_usd'] == .001
    else:
        assert 'cost_usd' not in row
    usage = extract.synth_usage_today()
    assert usage['by_provider'][provider]['by_model'][model]['calls'] == 1
    cost = {'gemini': .000692, 'anthropic': .000204, 'openai': None, 'openrouter': .001}[provider]
    assert usage['estimated_usd'] == cost
    assert usage['by_provider'][provider]['estimated_usd'] == cost
    assert cli._budget_status_all()['synth']['usage_by_provider'] == usage['by_provider']


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai', 'openrouter'])
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


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai', 'openrouter'])
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
    with pytest.raises(ValueError, match='requires --model'):
        models.set_models_merge_role('synth', provider='openrouter')
    assert config.load_config() == before
    models.set_models_merge_role('synth', provider='openrouter', model_id='google/gemini-2.5-flash')
    assert models.show_models()['synth'] == {
        'provider': 'openrouter', 'endpoint': '', 'model_id': 'google/gemini-2.5-flash'
    }
    args = cli.build_parser().parse_args(
        ['models', 'set', '--role', 'synth', '--provider', 'openrouter']
    )
    before_cli = config.load_config()
    with redirect_stdout(io.StringIO()) as out:
        assert args.func(args) == 2
    assert json.loads(out.getvalue()) == {
        'ok': False, 'error': 'synth: openrouter provider requires --model (model_id)'
    }
    assert config.load_config() == before_cli
    args = cli.build_parser().parse_args(
        ['models', 'set', '--role', 'synth', '--provider', 'openrouter',
         '--model', 'openai/gpt-6-luna']
    )
    with redirect_stdout(io.StringIO()) as out:
        assert args.func(args) == 0
    assert json.loads(out.getvalue())['models']['synth'] == {
        'provider': 'openrouter', 'endpoint': '', 'model_id': 'openai/gpt-6-luna'
    }
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


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'openrouter'])
def test_key_resolution_and_secret_set(provider, tmp_path, monkeypatch):
    resolve = getattr(keychain, 'resolve_' + provider + '_key')
    account = provider + '_api_key'
    env = provider.upper() + '_API_KEY'
    with pytest.raises(RuntimeError, match='No '):
        resolve()
    path = tmp_path / 'fake-key'
    path.write_text('file-fixture\n')
    path.chmod(0o600)
    config.set_path_setting(provider + '_key_file', str(path))
    assert 'file-fixture' not in config.config_file().read_text()
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


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai', 'openrouter', 'local'])
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
    fallback = doc['synth_provider'].pop('fallback')
    assert fallback == {'setting': 'gemini', 'source': 'default', 'gemini_key': provider == 'gemini' and present}
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


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'openrouter'])
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


def test_usage_prefers_reported_cost_per_row_and_keeps_unknown_unpriced_null():
    extract._record_usage(
        {'usage': {'prompt_tokens': 1_000_000, 'completion_tokens': 1_000_000, 'cost': 0.0}},
        provider='openrouter', model_id='gpt-6-luna', caller='test',
    )
    extract._record_usage(
        {'usage': {'prompt_tokens': 1_000_000, 'completion_tokens': 0, 'cost': .25}},
        provider='openrouter', model_id='gpt-6-luna', caller='test',
    )
    extract._record_usage(
        {'usage': {'prompt_tokens': 1_000_000, 'completion_tokens': 0}},
        provider='openrouter', model_id='gpt-6-luna', caller='test',
    )
    extract._record_usage(
        {'usage': {'prompt_tokens': 1_000_000, 'completion_tokens': 0}},
        provider='openai', model_id='gpt-6-luna', caller='test',
    )
    usage = extract.synth_usage_today()
    assert usage['by_provider']['openrouter']['by_model']['gpt-6-luna']['estimated_usd'] == .35
    assert usage['by_provider']['openai']['estimated_usd'] == .1
    assert usage['by_model']['gpt-6-luna']['estimated_usd'] == .45
    assert usage['estimated_usd'] == .45
    extract._record_usage(
        {'usage': {'prompt_tokens': 1, 'completion_tokens': 1}},
        provider='openrouter', model_id='unpriced', caller='test',
    )
    usage = extract.synth_usage_today()
    assert usage['by_provider']['openrouter']['by_model']['gpt-6-luna']['estimated_usd'] == .35
    assert usage['by_model']['gpt-6-luna']['estimated_usd'] == .45
    assert usage['by_provider']['openrouter']['estimated_usd'] is None
    assert usage['estimated_usd'] is None


def test_openrouter_status_has_all_key_presence_flags():
    status = keychain.secrets_status()
    fields = ('in_keychain', 'env', 'file_present', 'file_key_present')
    assert {f'openrouter_{field}' for field in fields} <= set(status)


@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai', 'openrouter'])
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


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'openrouter'])
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
                             ('anthropic', 'claude-haiku-5-5'), ('openai', 'explicit'),
                             ('openrouter', 'openai/gpt-6-luna'),
                             ('claude-cli', 'haiku'), ('codex-cli', 'explicit')]:
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
        def respond_cli(argv, **kwargs):
            if provider == 'claude-cli':
                prompts.append(kwargs['input'].encode())
                return subprocess.CompletedProcess(argv, 0, json.dumps({'result': answer}), '')
            prompts.append(kwargs['input'])  # codex-cli: strict UTF-8 bytes
            Path(argv[argv.index('-o') + 1]).write_text(answer)
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        with mock.patch.object(extract, '_provider_key', return_value='fake'), \
             mock.patch.object(extract, '_key', return_value='fake'), \
             mock.patch.object(extract.urllib.request, 'urlopen', side_effect=respond), \
             mock.patch.object(extract.subprocess, 'run', side_effect=respond_cli):
            result = extract.extract_memory(transcript, cwd='/fixture')
        assert result['summary'] == 'Kept append-only history.'
        assert result['decisions'] == ['Keep append-only history.']
    assert len(prompts) == 6
    assert all(prompt == prompts[0] for prompt in prompts)
    assert transcript.encode() in prompts[0]


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'openrouter'])
def test_keychain_lookup_account_and_khipu_service(provider):
    with mock.patch.object(keychain, 'keychain_available', return_value=True), \
         mock.patch.object(keychain, '_security', return_value=mock.Mock(returncode=0, stdout='fake-key\n')) as security:
        assert getattr(keychain, 'get_' + provider + '_key')() == 'fake-key'
    security.assert_called_once_with('find-generic-password', '-s', keychain.SERVICE,
                                    '-a', provider + '_api_key', '-w')
    assert keychain.SERVICE == 'Khipu'


def cli_adapter(provider):
    return getattr(extract, '_generate_' + provider.replace('-', '_'))


def cli_reply(provider, argv, *, text='ab', error=False, code=0, usage=True):
    if provider == 'claude-cli':
        data = {'result': text, 'is_error': error}
        if usage:
            data['usage'] = {'input_tokens': 1000, 'output_tokens': 200, 'cache_read_input_tokens': 400}
        return subprocess.CompletedProcess(argv, code, json.dumps(data), 'ignored stderr')
    Path(argv[argv.index('-o') + 1]).write_text(text)
    events = [{'type': 'thread.started', 'thread_id': '00000000-0000-4000-8000-000000000001'}]
    if usage:
        events.append({'type': 'turn.completed', 'usage': {
            'input_tokens': 1000, 'output_tokens': 200, 'cached_input_tokens': 400}})
    if error:
        events.append({'type': 'turn.failed', 'error': {'message': text}})
    # codex-cli runs through lines.run_text: bytes in, bytes out.
    return subprocess.CompletedProcess(argv, code, '\n'.join(json.dumps(e) for e in events).encode(),
                                       b'ignored stderr')


def test_codex_cli_event_with_a_raw_line_separator_is_one_event(monkeypatch):
    # codex exec --json may leave U+2028 raw inside a string; splitlines()
    # cut that event in two and the half failed to parse, failing the call.
    monkeypatch.setattr(synth_cli, 'binary', lambda name: name)
    def run(argv, **kw):
        reply = cli_reply('codex-cli', argv)
        item = json.dumps({'type': 'item.completed', 'item': {'text': 'a\u2028b'}}, ensure_ascii=False)
        return subprocess.CompletedProcess(argv, 0, item.encode() + b'\n' + reply.stdout + b'\n', b'')
    with mock.patch.object(extract.subprocess, 'run', side_effect=run):
        assert extract._generate_codex_cli('p', model_id='m', timeout=7, retries=0, caller='fixture') == 'ab'
    row = json.loads(extract.synth_usage_path().read_text())
    assert (row['prompt_tokens'], row['output_tokens']) == (1000, 200)


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
def test_cli_exact_argv_stdin_env_usage_budget_and_no_capture(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(synth_cli, 'binary', lambda name: name)
    prompt = 'USER: retain every decision.\nUnicode: café'
    monkeypatch.setenv('CLAUDECODE', 'nested')
    monkeypatch.setenv('CLAUDE_CODE_ENTRYPOINT', 'cli')
    monkeypatch.setenv('KHIPU_CAPTURE_QUEUE_ONLY', '1')
    monkeypatch.setenv('KHIPU_OTHER_CAPTURE_VARIABLE', 'nested')
    monkeypatch.setenv('UNRELATED_ENV', 'preserved')
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        cwd = Path(kw['cwd'])
        assert cwd.is_relative_to(tmp_path / 'state' / 'synth-cli')
        assert list(cwd.iterdir()) == []
        if provider == 'claude-cli':
            assert kw['input'].encode() == prompt.encode() and kw['text']
        else:
            assert kw['input'] == prompt.encode() and not kw.get('text')
        assert kw['capture_output'] and not kw['check']
        assert kw['timeout'] == 7
        assert not any(k.startswith('KHIPU_') for k in kw['env'])
        assert 'CLAUDECODE' not in kw['env'] and 'CLAUDE_CODE_ENTRYPOINT' not in kw['env']
        assert kw['env']['UNRELATED_ENV'] == 'preserved'
        if provider == 'claude-cli':
            sid = argv[-1]
            assert str(UUID(sid)) == sid
            assert sid in synth_cli.session_ids()
            assert argv == ['claude', '-p', '--setting-sources', '', '--model', 'm',
                            '--system-prompt', extract.CLAUDE_CLI_SYSTEM_PROMPT,
                            '--output-format', 'json', '--max-turns', '1', '--disallowedTools',
                            'Bash,Edit,Write,Read,Glob,Grep,WebSearch,WebFetch,Agent', '--session-id', sid]
            transcript = tmp_path / 's.jsonl'
            transcript.write_text(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': prompt * 10}}) + '\n')
            assert sc.hook_main(json.dumps({'session_id': sid, 'hook_event_name': 'SessionEnd',
                                            'transcript_path': str(transcript)}), 'claude_code')['reason'] == 'synth-cli'
        else:
            assert argv == ['codex', 'exec', '--json', '--ephemeral', '-s', 'read-only',
                            '--skip-git-repo-check', '-m', 'm', '-o', argv[10], '-C', kw['cwd'],
                            '--ignore-user-config', '-c', 'model_reasoning_effort=medium', '-']
            assert not Path(argv[10]).is_relative_to(cwd)
        return cli_reply(provider, argv)
    with mock.patch.object(extract.subprocess, 'run', side_effect=run), \
         mock.patch.object(extract, '_provider_key', side_effect=AssertionError('no key lookup')):
        assert cli_adapter(provider)(prompt, model_id='m', timeout=7, retries=0, caller='fixture') == 'ab'
    assert len(calls) == 1
    assert embed.budget_status('synth')['calls'] == 1
    row = json.loads(extract.synth_usage_path().read_text())
    assert (row['provider'], row['model'], row['caller']) == (provider, 'm', 'fixture')
    assert (row['prompt_tokens'], row['output_tokens'], row['cached_tokens']) == (1000, 200, 400)
    usage = cli._budget_status_all()['synth']['usage_by_provider'][provider]
    assert usage['estimated_usd'] == 0.0 and usage['billing'] == 'subscription'
    assert sc.queued_jobs() == [] and not sc.state_dir().exists()
    assert not Path(calls[0][1]['cwd']).exists()
    if provider == 'codex-cli':
        assert not Path(calls[0][0][10]).exists()
        assert '00000000-0000-4000-8000-000000000001' in synth_cli.session_ids()


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
@pytest.mark.parametrize('failure', ['is_error', 'exit', 'missing', 'timeout', 'logged_out', 'rate_limit'])
def test_cli_retry_failure_redaction_and_budget(provider, failure, monkeypatch, caplog):
    prompt = 'private prompt for failure fixture'
    secret = 'private-environment-value'
    monkeypatch.setenv('FIXTURE_SECRET', secret)
    def run(argv, **kwargs):
        if failure == 'missing':
            raise FileNotFoundError(prompt + secret)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 1, output=prompt + secret)
        text = {'logged_out': 'Not logged in', 'rate_limit': 'Rate limit exceeded'}.get(
            failure, 'failure ' + prompt + secret + 'z' * 500)
        return cli_reply(provider, argv, text=text, error=failure == 'is_error',
                         code=1 if failure == 'exit' else 0)
    with mock.patch.object(extract.subprocess, 'run', side_effect=run) as call, \
         mock.patch.object(extract.time, 'sleep') as sleep:
        with pytest.raises(RuntimeError, match='extract: model call failed:') as exc:
            cli_adapter(provider)(prompt, model_id='m', timeout=1, retries=2)
    assert call.call_count == 3 and embed.budget_status('synth')['calls'] == 3
    assert [c.args[0] for c in sleep.call_args_list] == [1.5, 3.0, 4.5]
    assert prompt not in str(exc.value) and secret not in str(exc.value)
    assert prompt not in caplog.text and secret not in caplog.text
    assert len(str(exc.value)) <= len('extract: model call failed: ') + 300
    if failure == 'logged_out':
        assert 'Not logged in' in str(exc.value)


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
def test_cli_failed_attempt_then_success_and_ceiling(provider):
    config.set_int_setting('synth_daily_calls', 2)
    def run(argv, **kwargs):
        return cli_reply(provider, argv, text='ok')
    with mock.patch.object(extract.subprocess, 'run') as call, \
         mock.patch.object(extract.time, 'sleep'):
        def attempts(argv, **kwargs):
            if call.call_count == 1:
                raise FileNotFoundError()
            return run(argv, **kwargs)
        call.side_effect = attempts
        assert cli_adapter(provider)('p', model_id='m', retries=1) == 'ok'
        assert call.call_count == 2
        with pytest.raises(embed.BudgetExhausted):
            cli_adapter(provider)('p', model_id='m', retries=0)
        assert call.call_count == 2
    assert len(extract.synth_usage_path().read_text().splitlines()) == 1


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
def test_cli_dispatch_default_timeout_and_missing_usage(provider):
    models.set_models_merge_role('synth', provider=provider, model_id='custom')
    with mock.patch.object(extract, '_generate_' + provider.replace('-', '_'), return_value='answer') as adapter:
        assert extract._generate('prompt') == 'answer'
        adapter.assert_called_once_with('prompt', model_id='custom', timeout=60, retries=2, caller='synth')
        extract._generate('prompt', timeout=9, retries=0)
        assert adapter.call_args.kwargs['timeout'] == 9
    with mock.patch.object(extract.subprocess, 'run', side_effect=lambda argv, **kw: cli_reply(provider, argv, usage=False)):
        assert extract._generate('prompt', retries=0) == 'ab'
    row = json.loads(extract.synth_usage_path().read_text())
    assert row['provider'] == provider and row['prompt_tokens'] == row['output_tokens'] == 0
    assert extract.synth_usage_today()['calls'] == 1


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
def test_cli_rerank_refusal_before_key_or_adapter(provider):
    models.set_models_merge_role('synth', provider=provider, model_id='m')
    with mock.patch.object(extract, '_provider_key') as key, mock.patch.object(extract, '_generate') as adapter:
        with pytest.raises(rerank._Unavailable, match='rerank needs an API provider'):
            rerank._call_model('p', timeout=1)
    key.assert_not_called()
    adapter.assert_not_called()


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
@pytest.mark.parametrize('present', [True, False])
def test_cli_doctor_subscription_path_only(provider, present):
    models.set_models_merge_role('synth', provider=provider, model_id='m')
    with ExitStack() as stack:
        for patch in _green_doctor_patches():
            stack.enter_context(patch)
        stack.enter_context(mock.patch.object(synth_cli, 'binary', return_value='/fake/bin' if present else None))
        stack.enter_context(mock.patch('khipu.probe.status', return_value={'ok': True}))
        stack.enter_context(mock.patch('khipu.recall_prompt.prompt_recall_outcomes', return_value={'ok': True}))
        stack.enter_context(mock.patch('khipu.drift.recall_quality', return_value={}))
        output = stack.enter_context(redirect_stdout(io.StringIO()))
        cli.cmd_doctor(cli.build_parser().parse_args(['doctor']))
    doc = json.loads(output.getvalue())
    # The green doctor patches report a Gemini key, so the fallback can run.
    assert doc['synth_provider'].pop('fallback') == {'setting': 'gemini', 'source': 'default', 'gemini_key': True}
    assert doc['synth_provider'] == {'provider': provider, 'model': 'm', 'key_present': None, 'auth': 'subscription',
                                     'binary': '/fake/bin' if present else None}
    assert doc['synth_provider_ok'] is present


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
@pytest.mark.parametrize('answer', ['pong', 'Not logged in'])
def test_cli_verify_fake_adapter_once_and_capture_watch(provider, answer):
    models.set_models_merge_role('synth', provider=provider, model_id='m')
    def adapter(prompt, **kw):
        assert prompt == 'Reply with exactly the word pong.'
        assert kw == {'model_id': 'm', 'timeout': 60, 'retries': 0, 'caller': 'verify'}
        synth_cli.new_session()
        return answer
    with mock.patch.object(extract, '_generate_' + provider.replace('-', '_'), side_effect=adapter) as call, \
         mock.patch.object(integrations.time, 'monotonic', side_effect=[0, .1, .2, .3, 3.4]):
        result = integrations.verify_synth_provider()
    assert call.call_count == 1
    assert result['ok'] is (answer == 'pong') and result['self_capture'] is False
    assert result['ms'] == 100
    if answer != 'pong':
        assert result['error'] == 'Not logged in'


@pytest.mark.parametrize('artifact', ['queue', 'state'])
def test_verify_detects_self_capture(artifact):
    models.set_models_merge_role('synth', provider='claude-cli')
    def adapter(*args, **kwargs):
        sid = synth_cli.new_session()
        if artifact == 'queue':
            sc.enqueue({'ts': sc._mint_ts(), 'session_id': sid, 'harness': 'claude_code'})
        else:
            sc.save_state('claude_code', sid, {})
        return 'pong'
    with mock.patch.object(extract, '_generate_claude_cli', side_effect=adapter):
        result = integrations.verify_synth_provider()
    assert not result['ok'] and result['self_capture'] is True


def test_integrations_verify_check_wiring_and_api_no_probe():
    assert integrations.verify_synth_provider() is None
    models.set_models_merge_role('synth', provider='claude-cli')
    args = cli.build_parser().parse_args(['integrations', 'verify', 'codex'])
    check = {'ok': True, 'ms': 123, 'self_capture': False}
    with mock.patch.object(integrations, 'verify', return_value={'detected': True, 'ok': True}), \
         mock.patch.object(integrations, 'verify_synth_provider', return_value=check) as probe, \
         redirect_stdout(io.StringIO()) as output:
        assert cli.cmd_integrations(args) == 0
    probe.assert_called_once_with()
    assert json.loads(output.getvalue())[0]['components']['synth_provider'] == check


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
def test_marker_write_failure_prevents_launch(provider):
    with mock.patch.object(synth_cli, 'new_session', side_effect=PermissionError('private detail')), \
         mock.patch.object(extract.subprocess, 'run') as run, mock.patch.object(extract.time, 'sleep'):
        with pytest.raises(RuntimeError, match='extract: model call failed: PermissionError'):
            cli_adapter(provider)('private prompt', model_id='m', retries=0)
    run.assert_not_called()


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
def test_cli_failure_keeps_drain_job_byte_for_byte(provider):
    models.set_models_merge_role('synth', provider=provider, model_id='m')
    path = sc.enqueue({'ts': sc._mint_ts(), 'harness': 'codex', 'session_id': 'ordinary-session',
                       'transcript': 'USER: This decision must survive a failed CLI call.'})
    before = path.read_bytes()
    with mock.patch.object(extract.subprocess, 'run', side_effect=FileNotFoundError()), \
         mock.patch.object(extract.time, 'sleep'), \
         mock.patch('khipu.capture.capture') as capture:
        result = sc.drain(limit=1)
    assert result['failed'] == 1 and result['captured'] == 0
    assert path.read_bytes() == before and sc.queued_jobs() == [path]
    assert embed.budget_status('synth')['calls'] == 3
    capture.assert_not_called()


def test_subscription_cost_does_not_reprice_anthropic_alias():
    for provider in ('anthropic', 'claude-cli', 'codex-cli'):
        extract._record_usage({'usage': {'input_tokens': 1000, 'output_tokens': 200,
                                         'cache_read_input_tokens': 400, 'cached_input_tokens': 400}},
                              provider=provider, model_id='claude-haiku-5-5', caller='test')
    result = extract.synth_usage_today()
    assert result['by_provider']['anthropic']['estimated_usd'] == .000204
    for provider in ('claude-cli', 'codex-cli'):
        assert result['by_provider'][provider]['estimated_usd'] == 0.0
        assert result['by_provider'][provider]['billing'] == 'subscription'
    assert result['estimated_usd'] == .000204


def test_cli_binary_resolves_without_path_and_honours_the_override(tmp_path, monkeypatch):
    # The launchd drain job has a bare PATH; the known install locations are tried next.
    monkeypatch.delenv('KHIPU_CLAUDE_CLI', raising=False)
    monkeypatch.setattr('shutil.which', lambda name: None)
    fake = tmp_path / 'claude'
    fake.write_text('#!/bin/sh\n')
    monkeypatch.setattr(synth_cli, '_CANDIDATES', {'claude': (str(fake),)})
    assert synth_cli.binary('claude') == str(fake)
    monkeypatch.setattr(synth_cli, '_CANDIDATES', {'claude': (str(tmp_path / 'missing'),)})
    assert synth_cli.binary('claude') is None
    monkeypatch.setenv('KHIPU_CLAUDE_CLI', str(fake))
    assert synth_cli.binary('claude') == str(fake)
    monkeypatch.setenv('KHIPU_CLAUDE_CLI', str(tmp_path / 'nope'))
    assert synth_cli.binary('claude') is None
    monkeypatch.delenv('KHIPU_CLAUDE_CLI')
    monkeypatch.setattr('shutil.which', lambda name: '/on/path/claude')
    assert synth_cli.binary('claude') == '/on/path/claude'
    with mock.patch.object(extract.subprocess, 'run', side_effect=lambda argv, **kw: cli_reply('claude-cli', argv)) as call, \
         mock.patch.object(synth_cli, 'binary', return_value='/resolved/claude'):
        extract._generate_claude_cli('p', model_id='m', retries=0)
    assert call.call_args.args[0][0] == '/resolved/claude'


def test_codex_cli_effort_defaults_low_and_is_raisable(monkeypatch):
    monkeypatch.delenv('KHIPU_CODEX_CLI_EFFORT', raising=False)
    assert extract._codex_cli_effort() == 'medium'
    monkeypatch.setenv('KHIPU_CODEX_CLI_EFFORT', 'High')
    assert extract._codex_cli_effort() == 'high'
    monkeypatch.setenv('KHIPU_CODEX_CLI_EFFORT', 'turbo; rm -rf')
    assert extract._codex_cli_effort() == 'medium'


def test_empty_record_for_a_real_window_is_retried_once_and_a_short_one_is_not():
    models.set_models_merge_role('synth', provider='codex-cli', model_id='gpt-6-luna')
    empty = json.dumps({'summary': '', 'decisions': [], 'open_loops': [], 'closed_loops': []})
    good = json.dumps({'summary': 'Chose the watch rules.', 'decisions': ['Use rule A.']})
    big = 'USER: decide the watch rules\nASSISTANT: ' + 'detail ' * 400
    with mock.patch.object(extract, '_generate_codex_cli', return_value=empty), \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_gemini', return_value=good) as gem:
        out = extract.extract_memory(big, cwd='/p')
    assert out['summary'] == 'Chose the watch rules.' and gem.call_count == 1
    # A short fragment is legitimately empty: no retry.
    with mock.patch.object(extract, '_generate_codex_cli', return_value=empty), \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_gemini', return_value=good) as gem:
        extract.extract_memory('ASSISTANT: ok, noted.', cwd='/p')
    assert gem.call_count == 0
    # The retry failing to parse never turns a valid empty record into an error.
    with mock.patch.object(extract, '_generate_codex_cli', return_value=empty), \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_gemini', return_value='not json') as gem:
        extract.extract_memory(big, cwd='/p')
    assert gem.call_count == 1
    # Gemini is the default provider and has no other model to fall back to, so
    # an empty record for a real window (>= 600 chars) gets exactly one
    # same-provider retry: two calls in total, never a loop.
    models.set_models_merge_role('synth', provider='gemini', model_id='gemini-2.5-flash')
    with mock.patch.object(extract, '_generate_gemini', return_value=empty) as gem:
        extract.extract_memory(big, cwd='/p')
    assert gem.call_count == 2


def test_wrong_task_reply_from_a_cli_provider_falls_back_to_gemini_once():
    models.set_models_merge_role('synth', provider='claude-cli', model_id='haiku')
    good = json.dumps({'summary': 'Reviewed the plan.', 'decisions': ['Ship it.']})
    with mock.patch.object(extract, '_generate_claude_cli', return_value='{"relied_on": ["x"]}') as cli_call, \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_gemini', return_value=good) as gem:
        out = extract.extract_memory('USER: summarise this please\nASSISTANT: ok', cwd='/p')
    assert out['summary'] == 'Reviewed the plan.'
    assert cli_call.call_count == 1 and gem.call_count == 1
    assert gem.call_args.kwargs['caller'] == 'capture-fallback'
    # No Gemini key: the original failure surfaces, nothing is swallowed.
    with mock.patch.object(extract, '_generate_claude_cli', return_value='not json'), \
         mock.patch.object(extract, '_key', side_effect=RuntimeError('no key')), \
         mock.patch.object(extract, '_generate_gemini') as gem, \
         pytest.raises(RuntimeError, match='invalid JSON'):
        extract.extract_memory('USER: hi\nASSISTANT: hello', cwd='/p')
    assert gem.call_count == 0


def test_openrouter_invalid_capture_falls_back_only_when_gemini_key_resolves():
    models.set_models_merge_role('synth', provider='openrouter', model_id='openai/gpt-6-luna')
    good = json.dumps({'summary': 'Kept the decision.', 'decisions': ['Keep it.']})
    with (
        mock.patch.object(extract, '_generate_openrouter', return_value='not json') as adapter,
        mock.patch.object(extract, '_key', return_value='fake'),
        mock.patch.object(extract, '_generate_gemini', return_value=good) as gem,
    ):
        assert extract.extract_memory('USER: hi\nASSISTANT: done', cwd='/p')['summary'] == 'Kept the decision.'
    assert adapter.call_count == gem.call_count == 1
    with (
        mock.patch.object(extract, '_generate_openrouter', return_value='not json'),
        mock.patch.object(extract, '_key', side_effect=RuntimeError('no key')),
        mock.patch.object(extract, '_generate_gemini') as gem,
        pytest.raises(RuntimeError, match='invalid JSON'),
    ):
        extract.extract_memory('USER: hi\nASSISTANT: done', cwd='/p')
    assert gem.call_count == 0


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
def test_valid_summary_about_cli_errors_is_not_rejected(provider):
    answer = json.dumps({'summary': 'Resolved Not logged in and rate limit errors.',
                         'decisions': ['Keep CLI failures retryable.']})
    with mock.patch.object(extract.subprocess, 'run', side_effect=lambda argv, **kw: cli_reply(provider, argv, text=answer)) as call:
        assert cli_adapter(provider)('private transcript', model_id='m', retries=0) == answer
    assert call.call_count == 1


def test_provider_failure_falls_back_to_gemini_and_a_failed_retry_keeps_the_first_error():
    models.set_models_merge_role('synth', provider='claude-cli', model_id='haiku')
    good = json.dumps({'summary': 'Closed the loop.', 'decisions': ['Done.']})
    limit = RuntimeError("extract: model call failed: You've hit your session limit · resets 4:50am")
    with mock.patch.object(extract, '_generate_claude_cli', side_effect=limit) as cli_call, \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_gemini', return_value=good) as gem:
        out = extract.extract_memory('USER: wrap up\nASSISTANT: closed it', cwd='/p')
    assert out['summary'] == 'Closed the loop.'
    assert cli_call.call_count == 1 and gem.call_count == 1
    assert gem.call_args.kwargs['caller'] == 'capture-fallback'
    # The retry failing too surfaces the provider's error, so the drain keeps the job.
    with mock.patch.object(extract, '_generate_claude_cli', side_effect=limit), \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_gemini', side_effect=RuntimeError('gemini down')), \
         pytest.raises(RuntimeError, match='session limit'):
        extract.extract_memory('USER: wrap up\nASSISTANT: closed it', cwd='/p')
    # The synth ceiling is never worked around by a fallback call.
    from khipu.embed import BudgetExhausted
    with mock.patch.object(extract, '_generate_claude_cli', side_effect=BudgetExhausted('synth: 5/5')), \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_gemini') as gem, \
         pytest.raises(BudgetExhausted):
        extract.extract_memory('USER: wrap up\nASSISTANT: closed it', cwd='/p')
    assert gem.call_count == 0


def test_synth_fallback_setting_off_keeps_the_failure_and_is_set_through_config():
    from khipu import config as cfg
    models.set_models_merge_role('synth', provider='claude-cli', model_id='haiku')
    assert cfg.choice_setting('synth_fallback') == ('gemini', 'default')
    cfg.set_choice_setting('synth_fallback', 'None')
    assert cfg.choice_setting('synth_fallback') == ('none', 'config')
    with pytest.raises(ValueError, match='gemini, none'):
        cfg.set_choice_setting('synth_fallback', 'claude')
    with mock.patch.object(extract, '_generate_claude_cli', return_value='not json'), \
         mock.patch.object(extract, '_key', return_value='fake'), \
         mock.patch.object(extract, '_generate_gemini') as gem, \
         pytest.raises(RuntimeError, match='invalid JSON'):
        extract.extract_memory('USER: hi\nASSISTANT: hello', cwd='/p')
    assert gem.call_count == 0
    cfg.set_choice_setting('synth_fallback', None)
    assert cfg.choice_setting('synth_fallback') == ('gemini', 'default')
    # The CLI route the app uses, and the doctor field Settings reads.
    from khipu.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(['config', '--set', 'synth_fallback', 'none'])
    assert rc == 0 and json.loads(buf.getvalue())['synth_fallback'] == 'none'
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(['config', '--set', 'synth_fallback', 'bogus']) == 2
    assert 'gemini, none' in json.loads(buf.getvalue())['error']
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(['config', '--unset', 'synth_fallback']) == 0
    assert cfg.choice_setting('synth_fallback') == ('gemini', 'default')


def test_cli_failure_recognises_the_session_limit_diagnostic():
    assert extract._cli_failure("You've hit your session limit · resets 4:50am (America/New_York)")
    assert extract._cli_failure("API Error: You've hit your limit")
    assert not extract._cli_failure('{"summary": "You\'ve hit your limit was the error we fixed."}')


def test_cli_environment_uses_the_default_claude_login(monkeypatch):
    # A capture queued by a session on a second account must not bill that
    # account: CLAUDE_CONFIG_DIR is dropped with the harness markers.
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/tmp/second-account')
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('KHIPU_HOOK_STDIN', 'x')
    monkeypatch.setenv('HOME', '/tmp/home-kept')
    env = synth_cli.environment()
    assert 'CLAUDE_CONFIG_DIR' not in env and 'CLAUDECODE' not in env and 'KHIPU_HOOK_STDIN' not in env
    assert env['HOME'] == '/tmp/home-kept'


@pytest.mark.parametrize('provider', ['claude-cli', 'codex-cli'])
def test_cli_child_path_carries_the_cli_folder_under_a_bare_path(provider, tmp_path, monkeypatch):
    # The launchd drain runs with a bare system PATH; a CLI found under a Node
    # version manager needs its own folder (where node lives) on the child's PATH.
    folder = tmp_path / '.nvm' / 'versions' / 'node' / 'v22.0.0' / 'bin'
    folder.mkdir(parents=True)
    name = provider.removesuffix('-cli')
    fake = folder / name
    fake.write_text('#!/bin/sh\nexit 0\n')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')
    models.set_models_merge_role('synth', provider=provider, model_id='m')
    seen = {}

    def fake_run(argv, **kw):
        seen['argv0'] = argv[0]
        seen['path'] = kw['env']['PATH']
        raise RuntimeError('stop')

    with mock.patch.object(synth_cli, 'binary', return_value=str(fake)), \
         mock.patch.object(extract.subprocess, 'run', side_effect=fake_run), \
         mock.patch.object(extract.time, 'sleep'), \
         pytest.raises(RuntimeError):
        getattr(extract, f'_generate_{name}_cli')('p', model_id='m', retries=0)
    assert seen['argv0'] == str(fake)
    assert str(folder) in seen['path'].split(':')
    assert seen['path'].split(':')[:4] == ['/usr/bin', '/bin', '/usr/sbin', '/sbin']
