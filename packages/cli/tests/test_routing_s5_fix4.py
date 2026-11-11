"""Sessionless MCP receipts and canonical/provider notice identities."""
import os
import subprocess
import sys

import pytest

from khipu import mcp_server, routing
from tests.test_routing_s5_fix3 import home


@pytest.fixture
def sessionless(home, monkeypatch):
    for name in ('KHIPU_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mcp_server, '_ROUTING_RECEIPTS', {})
    return home


def propose(kind, base, expect=None):
    if kind == 'rule':
        return mcp_server._tool_route_rule(dict(text='PROPOSED', rule=1, owner_request='change', base=base))
    extra = {} if expect is None else {'expect': expect}
    return mcp_server._tool_route_priority(dict(entries=[{'account': 'Grok'}], owner_request='change', base=base, **extra))


def revision(kind, view):
    return view['rules'][0]['owner_revision'] if kind == 'rule' else view['priority_owner_revision']


@pytest.mark.parametrize('kind', ['rule', 'priority'])
def test_sessionless_mcp_proposal(sessionless, kind):
    # Adapted from Sonnet's test_sessionless_mcp_proposal probe.
    routing.add_rule('OWNER', via='app')
    base = revision(kind, mcp_server._tool_route({"full": True}))
    propose(kind, base)
    view = routing.read_view()
    if kind == 'rule':
        assert view['rules'][0]['text'] == 'PROPOSED' and view['rules'][0]['pending']
    else:
        assert [e['label'] for e in view['priority']['entries']] == ['Grok']
        assert view['priority']['pending']
    assert not list((routing.routing_dir() / 'reads').glob('*.json'))


def test_sessionless_existing_priority(sessionless):
    routing.set_priority([{'account': 'Claude'}], via='app', expect=routing.priority_version())
    shown = mcp_server._tool_route({"full": True})
    propose('priority', shown['priority_owner_revision'], shown['priority']['version'])
    assert [e['label'] for e in routing.read_view()['priority']['entries']] == ['Grok']


@pytest.mark.parametrize('kind', ['rule', 'priority'])
def test_unread_revision_refused(sessionless, kind):
    routing.add_rule('OWNER', via='app')
    with pytest.raises(routing.RouteError, match='connection has not read that owner revision'):
        propose(kind, revision(kind, routing.read_view()))
    assert routing.read_view()['rules'][0]['text'] == 'OWNER'
    assert routing.read_view()['priority'] is None


@pytest.mark.parametrize('boundary', ['connection', 'pid'])
def test_receipts_do_not_cross_boundary(sessionless, monkeypatch, boundary):
    routing.add_rule('OWNER', via='app')
    base = revision('rule', mcp_server._tool_route({"full": True}))
    if boundary == 'connection':
        monkeypatch.setattr(mcp_server, '_ROUTING_SESSION', 'different-connection')
    else:
        pid = os.getpid()
        monkeypatch.setattr(mcp_server.os, 'getpid', lambda: pid + 1)
    with pytest.raises(routing.RouteError, match='connection has not read'):
        propose('rule', base)
    mcp_server._tool_route({"full": True})
    propose('rule', base)
    assert routing.read_view()['rules'][0]['pending']


def test_receipts_do_not_cross_processes(sessionless):
    routing.add_rule('OWNER', via='app')
    base = revision('rule', mcp_server._tool_route({"full": True}))
    script = """
import sys
from khipu import mcp_server, routing
try:
    mcp_server._tool_route_rule(dict(text='OTHER PROCESS', rule=1, owner_request='change', base=sys.argv[1]))
except routing.RouteError as exc:
    assert 'connection has not read' in str(exc), str(exc)
else:
    raise AssertionError('another process accepted an unread revision')
"""
    result = subprocess.run([sys.executable, '-c', script, base], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert routing.read_view()['rules'][0]['text'] == 'OWNER'


def test_sessionless_cli_receipt_recording_never_locks_or_writes(sessionless, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('sessionless CLI receipt recording took a routing lock')
    monkeypatch.setattr(routing, '_lock', forbidden)
    monkeypatch.setattr(routing, 'lock_budget', forbidden)
    routing._record_read({'rules': [{'entity': 'rule:test', 'owner_revision': 'r'}],
                          'priority_owner_revision': 'p'}, rules=True, priority=True)
    assert routing._read_receipts() == {}
    assert not (routing.routing_dir() / 'reads').exists()


def test_unread_and_stale_owner_revision(sessionless):
    routing.add_rule('OWNER 1', via='app')
    first = revision('rule', mcp_server._tool_route({"full": True}))
    routing.edit_rule(1, 'OWNER 2', via='app')
    with pytest.raises(routing.RouteError, match='connection has not read'):
        propose('rule', revision('rule', routing.read_view()))
    propose('rule', first)
    assert routing.read_view()['rules'][0]['text'] == 'OWNER 2'
    assert any(p['status'] == 'crossed' for p in routing.read_view()['proposals'].values())

