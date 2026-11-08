"""Explicit asks and reset proofs against throwaway PostgreSQL only."""
import hashlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from khipu import commitments as co, explicit_asks, mcp_server, needs_you, owed_reset, owed_changes
from tests.test_owed_changes import owed, Flood, PROJECT, FIELDS  # noqa: F401
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_commitments import _CommitmentsCursor


def test_filed_ask_is_visible_and_has_no_authority(owed, monkeypatch):
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


def test_reset_dry_run_export_apply_and_whole_batch_undo(owed, tmp_path, capsys, monkeypatch):
    flood = Flood(owed)
    eid = flood.episode()
    ids = [flood.item('old', f'Matt to approve design number {n}', episode=eid) for n in range(3)]
    # Span projects: one reset must still be one reversible batch.
    with owed.cursor() as cur:
        cur.execute('UPDATE commitments SET project = %s WHERE id = %s', ('other/repo', ids[-1]))
    ask = mcp_server._tool_owed_update({'action': 'ask', 'requester': 'Reviewer', 'question': 'Change the design?', 'yes': 'change design',
                                      'no': 'keep design', 'episode_id': eid})
    assert ask['ok'], ask
    recent = flood.item('recent', 'Matt to approve a real recent request', episode=eid)
    with owed.cursor() as cur:
        cur.execute("UPDATE commitments SET owner_reassigned_at = now(), owner_reassigned_by = 'direction' WHERE id = %s", (recent,))
        cur.execute('SELECT to_jsonb(c) FROM commitments c ORDER BY id')
        before = cur.fetchall()
    path = tmp_path / 'archive.json'
    assert owed_reset.command(SimpleNamespace(dry_run=True, export=str(path))) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry['groups']['explicit_asks'] == [ask['id']]
    assert dry['groups']['recent_owner_asks'] == [recent]
    assert not path.exists()
    with owed.cursor() as cur:
        cur.execute('SELECT to_jsonb(c) FROM commitments c ORDER BY id')
        assert cur.fetchall() == before
        doc = owed_reset.preview(cur)
    digest = owed_reset.export(doc, path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest in path.with_suffix('.json.sha256').read_text()
    monkeypatch.setattr(co, 'bulk_owner_gate', lambda n: None)
    assert not owed_reset.apply(doc)['ok']
    late = mcp_server._tool_owed_update({'action': 'ask', 'requester': 'Builder', 'question': 'Use another design?', 'yes': 'use another design',
                                       'no': 'keep this design', 'episode_id': eid})
    assert late['ok'], late
    out = owed_changes.apply(doc, owner_gate='direction')
    assert out['changed'] == 3, out
    assert len(out['batches']) == 1
    undo = owed_changes.undo(out['batches'][0], owner_gate='direction')
    assert undo['restored'] == 3, undo
    with owed.cursor() as cur:
        cur.execute('SELECT id, status FROM commitments ORDER BY id')
        assert all(status == 'open' for _, status in cur.fetchall())

    with owed.cursor() as cur:
        cur.execute('SELECT to_jsonb(c) FROM commitments c ORDER BY id')
        after = {r[0]['id']: r[0] for r in cur.fetchall()}
    for prior, in before:
        assert {f: after[prior['id']].get(f) for f in FIELDS} == {f: prior.get(f) for f in FIELDS}


@pytest.mark.parametrize('project', [None, PROJECT])
def test_ask_without_episode_and_duplicate_are_visible(owed, project):
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


def test_ask_requires_question_and_live_supplied_episode(owed):
    args = {'action': 'ask', 'requester': 'Builder', 'yes': 'use it', 'no': 'keep it'}
    with pytest.raises(ValueError, match='question'):
        mcp_server._tool_owed_update(args)
    with pytest.raises(ValueError, match='missing or forgotten'):
        mcp_server._tool_owed_update(dict(args, question='Use it?', episode_id=999999))


def test_ask_project_resolves_bare_name_and_alias(owed):
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


def test_export_output_and_help_disclose_the_unreachable_copy(owed, tmp_path, capsys):
    import argparse
    path = tmp_path / 'archive.json'
    owed_reset.command(SimpleNamespace(dry_run=False, export=str(path)))
    out = json.loads(capsys.readouterr().out)
    assert out['forget_reaches_export'] is False
    assert 'copy a forget will not reach' in out['warning']
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    owed_reset.add_parser(sub)
    assert 'copy a forget will not reach' in ' '.join(sub.choices['reset'].format_help().split())
