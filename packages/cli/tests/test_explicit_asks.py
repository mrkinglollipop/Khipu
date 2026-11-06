"""Explicit asks and reset proofs against throwaway PostgreSQL only."""
import hashlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from khipu import commitments as co, explicit_asks, mcp_server, needs_you, owed_reset, owed_tidy
from tests.test_owed_tidy import owed, Flood, PROJECT, FIELDS  # noqa: F401
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_commitments import _CommitmentsCursor


def test_summary_question_does_not_create_owner_work():
    cur = _CommitmentsCursor()
    with mock.patch.dict('os.environ', KHIPU_USER_ALIASES='matt'):
        co.reset_user_patterns()
        assert co.open_from_episode(cur, {"project": PROJECT, "open_loops": [
            {"text": "Matt to decide whether to approve the new design", "kind": "question", "owner": "user"}]}, 42) == 0
    assert not cur.rows
    co.reset_user_patterns()


def test_filed_ask_is_visible_and_has_no_authority(owed, monkeypatch):
    eid = Flood(owed).episode()
    out = mcp_server._tool_owed_update({"action": "ask", "requester": "Builder", "yes": "use the new design",
                                      "no": "keep the current design", "episode_id": eid})
    assert out["ok"], out
    with owed.transaction(), owed.cursor() as cur:
        rows = co.list_owed(cur, project=PROJECT)
        assert next(r for r in rows if r["id"] == out["id"])["owner"] == "user"
        items = explicit_asks.needs_you(cur)
    assert len(items) == 1
    assert 'Builder asks' in items[0]["words"]
    assert items[0]["settle"] == {"confirm": None, "undo": None}
    monkeypatch.setattr(needs_you, '_gather', lambda now: (items, [], None))
    answer = needs_you.waiting(record_read=False)
    assert answer['items'][0]['kind'] == 'ask'
    with pytest.raises(ValueError):
        mcp_server._tool_owed_update({"action": "ask", "requester": "Builder", "yes": "x", "no": "y",
                                      "episode_id": eid, "owner_gate": "direction"})


def test_reset_dry_run_export_apply_and_whole_batch_undo(owed, tmp_path, capsys, monkeypatch):
    flood = Flood(owed)
    eid = flood.episode()
    ids = [flood.item('old', f'Matt to approve design number {n}', episode=eid) for n in range(3)]
    # Span projects: one reset must still be one reversible batch.
    with owed.cursor() as cur:
        cur.execute('UPDATE commitments SET project = %s WHERE id = %s', ('other/repo', ids[-1]))
    ask = mcp_server._tool_owed_update({'action': 'ask', 'requester': 'Reviewer', 'yes': 'change design',
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
    late = mcp_server._tool_owed_update({'action': 'ask', 'requester': 'Builder', 'yes': 'use another design',
                                       'no': 'keep this design', 'episode_id': eid})
    assert late['ok'], late
    out = owed_tidy.apply(doc, owner_gate='direction')
    assert out['changed'] == 3, out
    assert len(out['batches']) == 1
    undo = owed_tidy.undo(out['batches'][0], owner_gate='direction')
    assert undo['restored'] == 3, undo
    with owed.cursor() as cur:
        cur.execute('SELECT id, status FROM commitments ORDER BY id')
        assert all(status == 'open' for _, status in cur.fetchall())

    with owed.cursor() as cur:
        cur.execute('SELECT to_jsonb(c) FROM commitments c ORDER BY id')
        after = {r[0]['id']: r[0] for r in cur.fetchall()}
    for prior, in before:
        assert {f: after[prior['id']].get(f) for f in FIELDS} == {f: prior.get(f) for f in FIELDS}
