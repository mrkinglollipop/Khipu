"""Two fail-closed rules from the final review. A config file that can't
be read or parsed leaves the login-folder table incomplete: the projection
withholds names and says so, and safe_read refuses every read until the
config is readable again. A catch-up that meets a row removed for good is
refused, as the first import is.

Hermetic: the data dir and the login folder are under ``tmp_path``; the
ledger cursor is a fake.
"""
from __future__ import annotations

import json

import pytest

from khipu import agent_view, claude_homes, config, routing, safe_read

@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('KHIPU_TEST', '1')
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'data'))
    for name in ('CODEX_HOME', 'GROK_HOME', 'CLAUDE_CONFIG_DIR'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(routing, '_load_view', lambda: {})
    monkeypatch.setattr(claude_homes, 'discover', lambda: [])
    agent_view._TABLE.update(sig=None, at=0, table=None)


def configure(tmp_path):
    name = 'FinalPrivateLoginName'
    login = tmp_path / name
    login.mkdir()
    path = config.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'usage_accounts': [{'harness': 'codex', 'home': str(login)}]}))
    return name, login, path


def test_config_read_failure_withholds_unknown_login_names(tmp_path):
    name, login, path = configure(tmp_path)
    assert name not in json.dumps(agent_view.project({'title': name}))
    path.write_text('{invalid-json')
    agent_view._TABLE.update(sig=None, at=0, table=None)
    result = agent_view.project({'title': name})
    assert name not in json.dumps(result), result
    assert result.get('incomplete') is True
    path.write_text('{}')
    assert agent_view.project({'title': 'plain words'}) == {'title': 'plain words'}


def test_config_read_failure_keeps_login_file_refused(tmp_path):
    name, login, path = configure(tmp_path)
    target = login / 'summary.json'
    target.write_text('synthetic private folder file')
    with pytest.raises(safe_read.Refused):
        safe_read.read_file(str(target))
    other = tmp_path / 'elsewhere.txt'
    other.write_text('synthetic plain file')
    path.write_text('{invalid-json')
    for each in (target, other):
        with pytest.raises(safe_read.Refused):
            safe_read.read_file(str(each))
    path.write_text(json.dumps({'usage_accounts': [{'harness': 'codex', 'home': str(login)}]}))
    assert safe_read.read_file(str(other))[0] == b'synthetic plain file'
    with pytest.raises(safe_read.Refused):
        safe_read.read_file(str(target))



