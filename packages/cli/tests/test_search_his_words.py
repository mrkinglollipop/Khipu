"""Synthetic retrieval grades and M1 phase profile; never opens the live hub."""
import importlib
import json
import time
from unittest import mock

from khipu import cli, embed
from tests.hermetic_pg import hub, pg_cluster
from tests.test_audit_recall_search import configure_hub


def test_profile_and_grades(hub, monkeypatch):
    configure_hub(monkeypatch, hub)
    conn = hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw,session_id,project,verbatim) VALUES (now(),%s,'{}',%s,'invented',%s::jsonb)",
                 ('synthetic exact', 'codex:12345678-abcd-1234-abcd-123456789abc', json.dumps({'quotes': ['keep the violet reset intact']})))
    conn.execute("INSERT INTO episodes (ts,summary,raw,project) VALUES (now(),'preserve purple restart','{}','invented'), (now(),'unrelated sermon rules','{}','other')")
    conn.commit()
    ids = [str(r[0]) for r in conn.execute('SELECT id FROM episodes ORDER BY id')]
    def cosine(q, **kw):
        kw['timing'].update(embed_ms=0.0, cosine_ms=0.0, embed_cache='synthetic')
        return [dict(kind='episode', id=i, score=s, rank_text=t, label=t, snippet=t) for i,s,t in
                [(ids[2],.91,'unrelated sermon rules'), (ids[1],.87,'preserve purple restart'), (ids[0],.79,'synthetic exact')]]
    monkeypatch.setattr(embed, '_cosine_candidates', cosine)
    start = time.perf_counter()
    out = embed.hybrid_search('keep the violet reset intact', limit=3)
    print('PROFILE', json.dumps(out['timing']), 'wall_ms', round((time.perf_counter()-start)*1000,2))
    print('GRADE exact_top', out['results'][0]['id'] == ids[0], 'confidence', out['confidence'])
    assert out['results'][0]['id'] == ids[0]
    assert out['confidence'] == 'strong'
    for q in ('12345678-abcd-1234-abcd-123456789abc', 'codex:12345678-abcd-1234-abcd-123456789abc'):
        assert [r['id'] for r in embed.hybrid_search(q, mode='literal')['results']] == [ids[0]]
    assert [r['id'] for r in embed.hybrid_search('violet', mode='literal', session_id='12345678-abcd')['results']] == [ids[0]]
    conn.execute('UPDATE episodes SET deleted_at=now() WHERE id=%s', (int(ids[0]),))
    conn.commit()
    assert not embed.hybrid_search('keep the violet reset intact', mode='literal')['results']


def test_confidence_grades():
    sample = [('exact', [{'lexical_hits':4,'cosine':.79}], 'strong'),
              ('paraphrase', [{'lexical_hits':0,'cosine':.88}], 'weak'),
              ('off_topic_top', [{'lexical_hits':0,'cosine':.61},{'lexical_hits':4,'cosine':.92}], 'none')]
    grades = [(name, embed.search_confidence(rows, token_count=4), expected) for name,rows,expected in sample]
    print('CONFIDENCE_GRADES', grades)
    assert all(actual == expected for _,actual,expected in grades)


def test_default_excludes_library():
    from tests.test_library_search import FakeCur, _hub, _cos_row
    cur = FakeCur(cosine=[_cos_row()])
    with _hub(cur), mock.patch('khipu.library_search.library_candidates', side_effect=AssertionError('default library work')):
        assert embed.hybrid_search('divine council')['results'] == []


def test_project_agreement():
    rows = [dict(id=str(i), kind='episode', project=p, lexical_hits=h, score=s) for i,p,h,s in
            [(1,'other',0,.05),(2,'invented',2,.049),(3,'invented',2,.048)]]
    ranked = embed._rank_search_rows(rows, None)
    assert ranked[0]['project'] == 'invented'


def test_snapshot_session(hub, monkeypatch, tmp_path):
    from tests.test_audit_recall_search import snapshot
    from khipu import hub_snapshot as hs
    con = snapshot(monkeypatch, tmp_path)
    sid = 'codex:12345678-abcd-1234-abcd-123456789abc'
    con.execute("INSERT INTO episodes (id,ts,summary,session_id) VALUES (1,'2026-11-06','synthetic',?)", (sid,))
    con.commit()
    assert [r['id'] for r in hs.search_stale_payload(sid.split(':')[1],12)['results']] == ['1']
    con.execute("UPDATE episodes SET deleted_at='2026-11-06' WHERE id=1")
    con.commit()
    assert not hs.search_stale_payload(sid,12)['results']
