"""Explicit asks and reset proofs against throwaway PostgreSQL only."""

import pytest

from khipu import commitments as co, explicit_asks, mcp_server, needs_you
from tests.owed_fixtures import owed, Flood, PROJECT, FIELDS  # noqa: F401
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


def test_filed_ask_is_visible_and_has_no_authority(owed, monkeypatch):  # noqa: F811
    eid = Flood(owed).episode()
    out = mcp_server._tool_owed_update({"action": "ask", "requester": "Builder", "question": "Use this design?", "yes": "use the new design",
                                      "no": "keep the current design", "episode_id": eid})
    assert out["ok"], out
    with owed.transaction(), owed.cursor() as cur:
        rows = co.list_owed(cur, project=PROJECT)
        assert next(r for r in rows if r["id"] == out["id"])["owner"] == "user"
        items = explicit_asks.needs_you(cur)
    assert len(items) == 1
    assert 'Builder asks: Use this design? Yes: use the new design. No: keep the current design.' in items[0]["words"]
    assert f"id={out['id']}" in items[0]['do']
    assert "cannot apply a change" in items[0]['do']
    assert items[0]["settle"] == {"confirm": None, "undo": None}
    monkeypatch.setattr(needs_you, '_gather', lambda now: (items, [], None))
    answer = needs_you.waiting(record_read=False)
    assert answer['items'][0]['kind'] == 'ask'
    with pytest.raises(ValueError):
        mcp_server._tool_owed_update({"action": "ask", "requester": "Builder", "question": "Use this design?", "yes": "x", "no": "y",
                                      "episode_id": eid, "owner_gate": "direction"})




@pytest.mark.parametrize('project', [None, PROJECT])
def test_ask_without_episode_and_duplicate_are_visible(owed, project):  # noqa: F811
    args = {'action': 'ask', 'requester': 'Builder', 'question': 'Use the revised layout?',
            'yes': 'use it', 'no': 'keep it', 'project': project}
    first = mcp_server._tool_owed_update(args)
    second = mcp_server._tool_owed_update(args)
    assert first['ok'] and second['ok'], (first, second)
    assert second['id'] == first['id'] and second['already_open'] is True
    with owed.transaction(), owed.cursor() as cur:
        cur.execute('SELECT opened_episode FROM commitments WHERE id = %s', (first['id'],))
        assert cur.fetchone() == (None,)
        rows = co.list_owed(cur, project=project)
        assert [(r['id'], r['owner']) for r in rows] == [(first['id'], 'user')]
        assert [r['key'] for r in explicit_asks.needs_you(cur)] == [f"ask:{first['id']}"]
        cur.execute("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'commitments' AND column_name = 'opened_episode'")
        assert cur.fetchone() == ('YES',)


def test_ask_requires_question_and_live_supplied_episode(owed):  # noqa: F811
    args = {'action': 'ask', 'requester': 'Builder', 'yes': 'use it', 'no': 'keep it'}
    with pytest.raises(ValueError, match='question'):
        mcp_server._tool_owed_update(args)
    with pytest.raises(ValueError, match='missing or forgotten'):
        mcp_server._tool_owed_update(dict(args, question='Use it?', episode_id=999999))


def test_ask_project_resolves_bare_name_and_alias(owed):  # noqa: F811
    flood = Flood(owed)
    eid = flood.episode()
    flood.item('visible', 'Assistant to rebuild the index', owner='assistant', episode=eid)
    with owed.cursor() as cur:
        cur.execute('INSERT INTO project_aliases (alias, project) VALUES (%s, %s)', ('old/widget', PROJECT))
    args = {'action': 'ask', 'requester': 'Builder', 'question': 'Use it?', 'yes': 'use it', 'no': 'keep it'}
    first = mcp_server._tool_owed_update(dict(args, project='widget'))
    second = mcp_server._tool_owed_update(dict(args, project='old/widget'))
    assert second['id'] == first['id'] and second['already_open'], (first, second)
    with owed.cursor() as cur:
        cur.execute('SELECT project FROM commitments WHERE id = %s', (first['id'],))
        assert cur.fetchone() == (PROJECT,)


