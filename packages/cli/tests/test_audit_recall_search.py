"""Synthetic, hermetic reproductions; failing assertions express the contract."""
import json
import sqlite3
from unittest import mock

import pytest
from khipu import db, embed, features, hub_snapshot as hs, profiles
from tests.hermetic_pg import hub, pg_cluster


def configure_hub(monkeypatch, hub):
    assert '@127.0.0.1:' in hub.dsn
    monkeypatch.setattr(db, 'connect', lambda **kw: hub.connect())
    monkeypatch.setattr(hs, 'try_hub_connect', lambda **kw: hub.connect())
    monkeypatch.setattr(features, 'enabled', lambda name: False)


def snapshot(monkeypatch, tmp_path):
    path = tmp_path / 'invented.sqlite'
    con = sqlite3.connect(path)
    hs._create_schema(con)
    monkeypatch.setattr(hs, 'snapshot_path', lambda: path)
    monkeypatch.setattr(hs, 'stale_fields', lambda: {'stale': True})
    return con


def test_topic_event_date_filter_parity(hub, monkeypatch, tmp_path):
    configure_hub(monkeypatch, hub)
    conn = hub.connect()
    conn.execute("INSERT INTO topics (slug,title,body,created_at,updated_at,event_at) VALUES ('invented','Zebrafish','zebrafish reference','2026-10-01','2026-10-20','2026-10-15')")
    conn.commit()
    con = snapshot(monkeypatch, tmp_path)
    con.execute("INSERT INTO topics (slug,title,body,created_at,updated_at,event_at) VALUES ('invented','Zebrafish','zebrafish reference','2026-10-01','2026-10-20','2026-10-15')")
    con.commit()
    online = embed.hybrid_search('zebrafish', mode='literal', kind='topic', until='2026-10-16', include_libraries=False)
    offline = hs.search_stale_payload('zebrafish', 12, kind='topic', until='2026-10-16')
    print('event-date online/offline:', json.dumps([online['results'], offline['results']]))
    assert [r['id'] for r in online['results']] == ['invented']
    assert [r['id'] for r in offline['results']] == ['invented']


def test_hub_chunk_limit_starves_entities(hub, monkeypatch, tmp_path):
    configure_hub(monkeypatch, hub)
    conn = hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw) VALUES ('2026-10-20','dominant made-up text','{}'), ('2026-10-20','second made-up text','{}')")
    eids = [str(r[0]) for r in conn.execute('SELECT id FROM episodes ORDER BY id').fetchall()]
    conn.execute("UPDATE embedding_profiles SET is_active=false WHERE is_active")
    conn.execute("INSERT INTO embedding_profiles (id,provider,model,dim,is_active) VALUES ('fake@2','openai-compatible','fake',2,true)")
    for i in range(50):
        conn.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,content_hash,embedding) VALUES ('fake@2','episode',%s,%s,'dominant made-up text','hash','[1,0]'::vector)", (eids[0], i))
    conn.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,content_hash,embedding) VALUES ('fake@2','episode',%s,0,'second made-up text','hash','[0.99,0.1]'::vector)", (eids[1],))
    conn.commit()
    monkeypatch.setattr(embed, '_active_profile', lambda cur: 'fake@2')
    monkeypatch.setattr(embed, '_query_vec', lambda *a: ([1., 0.], 'fake'))
    online = embed.hybrid_search('unmentionedword', mode='semantic', kind='episode', limit=2, include_libraries=False)
    print('chunk-starvation expected/online:', eids, [r['id'] for r in online['results']])
    assert [r['id'] for r in online['results']] == eids


def test_lower_chunk_must_not_erase_strong_lexical_rank(hub, monkeypatch):
    configure_hub(monkeypatch, hub)
    conn = hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw) VALUES ('2026-10-20','semantic-first invented text','{}'), ('2026-10-20','both-keywords invented text','{}'), ('2026-10-20','one-keyword invented text','{}')")
    eids = [str(r[0]) for r in conn.execute('SELECT id FROM episodes ORDER BY id').fetchall()]
    conn.execute("UPDATE embedding_profiles SET is_active=false WHERE is_active")
    conn.execute("INSERT INTO embedding_profiles (id,provider,model,dim,is_active) VALUES ('fake@2','openai-compatible','fake',2,true)")
    for eid, text, vector in zip(eids, ['irrelevant text', 'orchid shipping', 'orchid'], ['[1,0]', '[.99,.1]', '[.98,.15]']):
        conn.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,content_hash,embedding) VALUES ('fake@2','episode',%s,0,%s,'hash',%s::vector)", (eid,text,vector))
    conn.commit()
    monkeypatch.setattr(embed, '_active_profile', lambda cur: 'fake@2')
    monkeypatch.setattr(embed, '_query_vec', lambda *a: ([1., 0.], 'fake'))
    before = embed.hybrid_search('orchid shipping', mode='semantic', kind='episode', limit=3, include_libraries=False)
    conn.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,content_hash,embedding) VALUES ('fake@2','episode',%s,1,'irrelevant appendix','hash','[.9,.4]'::vector)", (eids[1],))
    conn.commit()
    after = embed.hybrid_search('orchid shipping', mode='semantic', kind='episode', limit=3, include_libraries=False)
    print('lexical-rank before/after:', json.dumps([before['results'], after['results']]))
    assert before['results'][0]['id'] == eids[1]
    assert after['results'][0]['id'] == eids[1]


@pytest.mark.parametrize("probe", ["semantic", "metadata"])
def test_replica_topic_event_date_all_paths(tmp_path, monkeypatch, probe):
    con = snapshot(monkeypatch, tmp_path)
    con.execute("INSERT INTO topics(slug,title,body,event_at,updated_at,created_at) VALUES ('invented','Zebrafish','zebrafish','2026-10-15','2026-10-20','2026-10-01')")
    con.execute("INSERT INTO memory_embeddings(profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('p','topic','invented',0,'zebrafish',?)", (hs._vector_to_blob([1.,0.]),))
    con.commit()
    if probe == "semantic":
        from khipu import models
        spec = profiles.ProfileSpec("p", "openai-compatible", "fake", 2, endpoint="http://127.0.0.1:11434")
        monkeypatch.setattr(hs, "active_snapshot_profile_spec", lambda: spec)
        monkeypatch.setattr(models, "show_models", lambda: {"embed": {"provider":"local","endpoint":spec.endpoint,"model_id":spec.model}})
        monkeypatch.setattr(embed, "embed_one", lambda *a, **kw: [1.,0.])
        rows = hs.semantic_search_snapshot("zebrafish", kind="topic", until="2026-10-16")
        assert [r["id"] for r in rows] == ["invented"]
    else:
        rows = hs.snapshot_row_metadata(con, [{"kind":"topic","id":"invented"}])
        assert rows[0]["ts"] == "2026-10-15"
    con.close()
