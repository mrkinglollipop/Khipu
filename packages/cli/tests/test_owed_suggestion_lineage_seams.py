from contextlib import nullcontext

import pytest

from khipu import activity, commitments as co, config, hygiene, close_rules, owed_tidy, recall_rule
from tests.test_commitment_owner import _NullConn
from tests.test_owed_tidy import _probe_row, _NowCursor


def test_tidy_close_duplicate_is_suggested_by_hygiene(monkeypatch):
    monkeypatch.setenv('KHIPU_USER_ALIASES', 'matt')
    co.reset_user_patterns()
    rows = {i: _probe_row(i, 'user', None, 'conversation', text='Matt will review the deployment checklist before release') for i in (1, 2)}
    monkeypatch.setattr(owed_tidy, '_select', lambda cur, project: rows)
    monkeypatch.setattr(close_rules, 'ready', lambda cur: False)
    plan = owed_tidy._plan(_NowCursor(), 'p', judge=None, use_model=False)
    assert plan['items'][1]['proposed']['close'] == 'duplicate'
    monkeypatch.setattr(hygiene, '_select_open_commitments', lambda *a, **kw: list(rows.values()))
    monkeypatch.setattr(hygiene, '_commitment_pair_scores', lambda *a: {})
    monkeypatch.setattr('khipu.db.has_columns', lambda *a: False)
    suggestions = []
    monkeypatch.setattr(co, 'suggest_looks_done', lambda cur, cid, **kw: suggestions.append(cid) or True)
    report = hygiene.run_commitments_hygiene(_NowCursor(), project='p', apply=True,
                                             judge=lambda batch: (_ for _ in ()).throw(AssertionError('no model')))
    assert report['counts']['duplicate'] == 1
    assert suggestions == [2], f'tidy proposes duplicate close but hygiene publishes no Looks done: {suggestions}'


def test_slice_does_not_call_a_sibling_assistants_promise_yours(monkeypatch):
    rows = [dict(id=1, owner='assistant', kind='promise', origin='job', project='p',
                 text='I will send the final migration report', opened_episode=44,
                 session_id='codex:sibling', parent_session_id='codex:other-root')]
    monkeypatch.setattr(activity, 'connect', lambda: _NullConn())
    monkeypatch.setattr(co, 'list_owed', lambda *a, **kw: rows)
    monkeypatch.setattr('khipu.decisions.standing_decisions', lambda *a, **kw: [])
    monkeypatch.setattr('khipu.embed._episode_schema_flags', lambda *a, **kw: dict(project=True, deleted_at=True, parent_session_id=True))
    monkeypatch.setattr(config, 'load_config', lambda: {})
    data = activity.project_slice(project='p', host_session_id='codex:this-root')
    rendered = recall_rule._render_project_slice('p', data)
    assert '(yours)' not in rendered, rendered


@pytest.mark.parametrize(('session_id', 'parent_session_id', 'expected'), [
    ('codex:this-root', None, True),
    ('codex:child', 'codex:this-root', True),
    ('codex:sibling', 'codex:other-root', False),
    ('codex:unrelated', None, False),
    (None, None, False),
])
def test_slice_marks_only_the_current_lineages_assistant_items_yours(
    monkeypatch, session_id, parent_session_id, expected,
):
    rows = [dict(id=1, owner='assistant', kind='promise', origin='conversation', project='p',
                 text='I will send the final migration report', opened_episode=44,
                 session_id=session_id, parent_session_id=parent_session_id)]
    monkeypatch.setattr(activity, 'connect', lambda: _NullConn())
    monkeypatch.setattr(co, 'list_owed', lambda *a, **kw: rows)
    monkeypatch.setattr('khipu.decisions.standing_decisions', lambda *a, **kw: [])
    monkeypatch.setattr('khipu.embed._episode_schema_flags', lambda *a, **kw: dict(project=True, deleted_at=True, parent_session_id=True))
    monkeypatch.setattr(config, 'load_config', lambda: {})
    rendered = recall_rule._render_project_slice(
        'p', activity.project_slice(project='p', host_session_id='codex:this-root'),
    )
    assert ('(yours)' in rendered) is expected


@pytest.mark.parametrize('owner', ['user', 'assistant', 'agent'])
@pytest.mark.parametrize('apply', [False, True])
def test_duplicate_proposals_suggest_only_for_the_owner_and_never_mutate_his_items(monkeypatch, owner, apply):
    monkeypatch.setenv('KHIPU_USER_ALIASES', 'matt')
    co.reset_user_patterns()
    text = {'user': 'Matt will review the deployment checklist before release',
            'assistant': 'I will review the deployment checklist before release',
            'agent': 'The builder will review the deployment checklist before release'}[owner]
    rows = [_probe_row(i, owner, None, 'conversation', text=text) for i in (1, 2)]
    monkeypatch.setattr(hygiene, '_select_open_commitments', lambda *a, **kw: rows)
    monkeypatch.setattr(hygiene, '_commitment_pair_scores', lambda *a: {})
    monkeypatch.setattr('khipu.db.has_columns', lambda *a: False)
    suggestions, closes = [], []
    monkeypatch.setattr(co, 'suggest_looks_done', lambda cur, cid, **kw: suggestions.append((cid, kw)) or True)
    monkeypatch.setattr(co, 'close_by_rule', lambda cur, cid, **kw: closes.append(cid) or True)
    report = hygiene.run_commitments_hygiene(
        _NowCursor(), project='p', apply=apply,
        judge=lambda batch: [{'keep': True} for _ in batch])
    assert report['counts']['duplicate'] == 1
    assert [cid for cid, _ in suggestions] == ([2] if apply and owner == 'user' else [])
    assert closes == ([2] if apply and owner != 'user' else [])
    assert all(row['status'] == 'open' for row in rows)
    if suggestions:
        assert suggestions[0][1] == {'rule': 'duplicate', 'reason': 'duplicate-of-1',
                                     'key': 'duplicate-of:1', 'evidence': {'rule': 'duplicate', 'keeper': 1}}


@pytest.mark.parametrize(('session', 'parent', 'has_parent', 'expected'), [
    ('codex:this-root', None, True, True),
    ('codex:child', 'codex:this-root', True, True),
    ('codex:sibling', 'codex:other-root', True, False),
    ('codex:this-root', None, False, True),
    ('codex:child', None, False, False),
    (None, None, True, False),
])
def test_lineage_comes_from_the_opening_episode_when_owed_rows_have_no_session(
    monkeypatch, session, parent, has_parent, expected,
):
    queries = []

    class Cursor:
        def execute(self, sql, params=()):
            self.sql = sql
            queries.append(sql)

        def fetchall(self):
            return [(1, session, parent)] if self.sql.startswith('SELECT c.id, e.session_id') else []

    cur = Cursor()
    conn = type('Conn', (), {'cursor': lambda self: nullcontext(cur)})()
    monkeypatch.setattr(activity, 'connect', lambda: nullcontext(conn))
    rows = [dict(id=1, owner='assistant', kind='promise', text='I will send the final report', opened_episode=44)]
    monkeypatch.setattr(co, 'list_owed', lambda *a, **kw: rows)
    monkeypatch.setattr('khipu.decisions.standing_decisions', lambda *a, **kw: [])
    monkeypatch.setattr('khipu.embed._episode_schema_flags', lambda *a: dict(
        project=True, deleted_at=True, parent_session_id=has_parent))
    monkeypatch.setattr(config, 'load_config', lambda: {})
    rendered = recall_rule._render_project_slice(
        'p', activity.project_slice(project='p', host_session_id='codex:this-root'))
    assert ('(yours)' in rendered) is expected
    assert 'I will send the final report' in rendered
    assert ('e.parent_session_id' in queries[0]) is has_parent
