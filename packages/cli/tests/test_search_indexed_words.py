"""Indexed owner-word retrieval at realistic corpus size; hermetic PostgreSQL."""
import json
from pathlib import Path
import time

from khipu import cli, embed, features, migrate
from khipu.search_text import explicit_search_tokens, search_tokens, order_search_evidence
from tests.hermetic_pg import hub, pg_cluster
from tests.test_audit_recall_search import configure_hub


def install_words(hub, tmp_path):
    directory = tmp_path / 'migration'
    directory.mkdir(exist_ok=True)
    source = Path(__file__).resolve().parents[3] / 'ops/migrations/0051_owner_words_trgm.sql'
    (directory / source.name).write_text(source.read_text())
    return hub.migrate(directory)


def test_40000_indexed_and_unindexed(hub, monkeypatch, tmp_path):
    configure_hub(monkeypatch, hub)
    conn = hub.connect()
    conn.execute("""INSERT INTO episodes (ts,summary,raw,verbatim,topics,decisions,preferences,people)
      SELECT now(), repeat('Project implementation review: tests and captures need reliable delivery. ',8) || md5(i::text),
      '{}'::jsonb, jsonb_build_object('quotes',jsonb_build_array(
        CASE WHEN i % 5000 = 0 THEN 'keep the violet reset intact' ELSE repeat('Owner asks for clear evidence before the next build. ',8) || md5((i+1)::text) END)),
      '["project","review"]'::jsonb, '["Keep current implementation until measured"]'::jsonb, '[]'::jsonb, '[]'::jsonb
      FROM generate_series(1,40000) i""")
    conn.execute('ANALYZE episodes'); conn.commit()
    class Recorder:
        def __init__(self, cur): self.cur=cur; self.statements=[]
        def __getattr__(self,n): return getattr(self.cur,n)
        def execute(self,sql,params=None):
            if 'FROM episodes' in sql: self.statements.append((sql,params))
            return self.cur.execute(sql,params)
    for indexed in (False,True):
        if indexed: install_words(hub,tmp_path)
        with conn.cursor() as cur:
            rec=Recorder(cur); started=time.perf_counter()
            rows=cli._literal_candidates(rec,'keep the violet reset intact',50,kind='episode',fast=True,owner_words=True)
            elapsed=(time.perf_counter()-started)*1000
            print('CORPUS_PROFILE',json.dumps(dict(indexed=indexed,rows=len(rows),ms=round(elapsed,2))))
            assert len(rows)==(8 if indexed else 0)
            for sql,params in rec.statements:
                cur.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '+sql,params)
                plan=cur.fetchone()[0][0]
                print('CORPUS_EXPLAIN',json.dumps(plan))
                assert 'Bitmap Index Scan' in json.dumps(plan)
            assert elapsed < 200
        conn.commit()
    # The migration runner adopts the valid index idempotently.
    assert not install_words(hub,tmp_path)['ran']
    conn.execute("UPDATE episodes SET deleted_at=now() WHERE id % 5000=0"); conn.commit()
    with conn.cursor() as cur:
        assert not cli._literal_candidates(cur,'keep the violet reset intact',50,kind='episode',fast=True,owner_words=True)


def test_stopwords_and_post_fair_fill():
    assert explicit_search_tokens('what did we decide about the Owed reset') == ['decide','owed','reset']
    assert search_tokens('what about reset') == ['what','about','reset']  # prompt unchanged
    rows=[dict(kind='episode',id='cos',score=10,lexical_hits=0),
          dict(kind='episode',id='coverage',score=1,lexical_hits=2),
          dict(kind='episode',id='session',score=.1,exact_phrase=True)]
    ordered=order_search_evidence(embed._fair_fill(order_search_evidence(rows,3),3),3)
    assert [r['id'] for r in ordered] == ['session','coverage','cos']
    assert embed.search_confidence(ordered[1:],token_count=3)=='strong'
    rows.extend([dict(kind='episode',id='extra',score=2),dict(kind='topic',id='tail',score=.01)])
    assert order_search_evidence(embed._fair_fill(order_search_evidence(rows,3),4),3)[0]['id']=='session' 


def test_hybrid_skip_without_index(hub,monkeypatch):
    configure_hub(monkeypatch,hub)
    conn=hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw,verbatim) VALUES (now(),'unrelated','{}','{\"quotes\":[\"violet reset\"]}')")
    conn.commit()
    with conn.cursor() as cur:
        assert not cli._literal_candidates(cur,'violet reset',50,fast=True,owner_words=True)
        assert cli._literal_candidates(cur,'violet reset',50)


def test_agent_session_identity(hub,monkeypatch):
    configure_hub(monkeypatch,hub)
    conn=hub.connect()
    sid='claude_code:27916820-7db2-4539-918d-9eec66980f42:agent:a99687aa85370997d'
    conn.execute("INSERT INTO episodes (ts,summary,raw,session_id) VALUES (now(),'invented session','{}',%s)",(sid,)); conn.commit()
    monkeypatch.setattr(embed,'_cosine_candidates',lambda *a,**k: [])
    for q in (sid,sid.partition(':')[2]):
        for mode in ("hybrid","literal","semantic"):
            out=embed.hybrid_search(q,mode=mode)
            assert len(out['results'])==1 and out['results'][0]['exact_phrase']


def test_hybrid_session_id_survives_the_relevance_floor(hub,monkeypatch):
    # 2026-11-07: with relevance_floor on, hybrid abstained on a bare session id
    # (no cosine, id not in the text as tokens) while literal returned the row.
    configure_hub(monkeypatch,hub)
    # configure_hub pins every feature off; turn on only the floor.
    monkeypatch.setattr(features,'enabled',lambda name: name=='relevance_floor')
    conn=hub.connect()
    sid='claude_code:27916820-7db2-4539-918d-9eec66980f42'
    conn.execute("INSERT INTO episodes (ts,summary,raw,session_id) VALUES (now(),'invented session','{}',%s)",(sid,)); conn.commit()
    monkeypatch.setattr(embed,'_cosine_candidates',lambda *a,**k: [])
    for q in (sid,sid.partition(':')[2]):
        out=embed.hybrid_search(q,mode='hybrid')
        assert len(out['results'])==1 and out['results'][0]['exact_phrase']
        assert out['relevance_floor']['applied'] and not out['relevance_floor']['abstained']
        assert out['relevance_floor']['evidence_rows']==1


def test_quotes(hub,monkeypatch,tmp_path):
    configure_hub(monkeypatch,hub); install_words(hub,tmp_path)
    conn=hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw,verbatim) VALUES (now(),'invented owner','{}','{\"quotes\":[\"violet reset\"]}')"); conn.commit()
    monkeypatch.setattr(embed,'_cosine_candidates',lambda *a,**k: [])
    for mode in ('literal','hybrid'):
        out=embed.hybrid_search('"violet reset"',mode=mode)
        assert out['results'][0]['exact_phrase'] and out['confidence']=='strong'
