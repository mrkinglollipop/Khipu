import json, socket, sqlite3
from contextlib import contextmanager
from pathlib import Path
import pytest
import psycopg
from psycopg.conninfo import conninfo_to_dict
from tests.hermetic_pg import hub, pg_cluster
from khipu import hub_snapshot as hs, mcp_server as mcp, db
from khipu.cli import _graph_query, _search_query

@pytest.fixture
def world(hub, tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KHIPU_KEYCHAIN','0')
    monkeypatch.setattr(mcp,'_ensure_path',lambda:None)
    snap=tmp_path/'snapshot.sqlite'
    con=sqlite3.connect(snap); hs._create_schema(con)
    monkeypatch.setattr(hs,'snapshot_path',lambda:snap)
    monkeypatch.setattr(hs,'meta_path',lambda:tmp_path/'meta.json')
    pg=hub.connect()
    # Deliberately reverse lexical neighbor/type order.
    for node in ('node:root','node:alpha','node:zulu'):
        pg.execute('INSERT INTO nodes (id,type,name) VALUES (%s,\'concept\',%s)',(node,node))
    for dst,kind in [('node:alpha','z_related'),('node:zulu','a_related')]:
        pg.execute('INSERT INTO edges (src,dst,type,weight) VALUES (%s,%s,%s,1)',('node:root',dst,kind))
        con.execute('INSERT INTO edges (src,dst,type,weight) VALUES (?,?,?,1)',('node:root',dst,kind))
    for eid,summary,deleted in [(11,'lookup live sentinel',None),(12,'lookup forgotten sentinel','2026-10-20T00:00:00Z')]:
        pg.execute('INSERT INTO episodes (id,ts,summary,topics,deleted_at) VALUES (%s,now(),%s,%s::jsonb,%s::timestamptz)',(eid,summary,'["alpha"]',deleted))
        con.execute('INSERT INTO episodes (id,ts,summary,topics,deleted_at) VALUES (?,\'2026-10-20T00:00:00Z\',?,?,?)',(eid,summary,'["alpha"]',deleted))
    pg.commit(); con.commit(); con.close()
    monkeypatch.setattr(db,'connect',hub.connect)
    monkeypatch.setattr(hs,'try_hub_connect',hub.connect)
    return pg

def test_graph_same_limit_selects_same_neighbor(world):
    hub_result=mcp._tool_graph({'id':'node:root','hops':1,'limit':1})
    replica=hs.graph_neighbors_snapshot('node:root',1,1)
    print('hub:',json.dumps(hub_result)); print('replica:',json.dumps(replica))
    assert replica['edges']==hub_result['edges']

def test_replica_increasing_hops_retains_nearest_limited_neighbor(world):
    one=hs.graph_neighbors_snapshot('node:root',1,1)
    two=hs.graph_neighbors_snapshot('node:root',2,1)
    print('hops1:',json.dumps(one));print('hops2:',json.dumps(two))
    assert {e['dst'] for e in one['edges']} <= {e['node_id'] for e in two['walk']}

def test_live_and_forgotten_get_graph_controls(world, monkeypatch):
    from khipu import activity
    with world.cursor() as cur:
        hub_hits = _search_query(cur, 'lookup', 10, kind='episode')
    replica_hits = hs.search_snapshot('lookup', 10, kind='episode')
    for hits in (hub_hits, replica_hits):
        assert {str(r['id']) for r in hits} == {'11'}, hits
    print('Hub and replica literal search: only live control 11')
    for offline in (False,True):
        if offline:
            def disconnected(*a,**k): raise psycopg.OperationalError('connection refused')
            monkeypatch.setattr(db,'connect',disconnected)
            monkeypatch.setattr(activity,'connect',disconnected)
            monkeypatch.setattr(hs,'try_hub_connect',disconnected)
        assert mcp._tool_get({'id':'11','kind':'episode'})['summary']=='lookup live sentinel'
        with pytest.raises(ValueError,match='episode not found'):
            mcp._tool_get({'id':'12','kind':'episode'})
        assert not mcp._tool_graph({'id':'11'})['episode']['missing']
        assert mcp._tool_graph({'id':'12'})['episode']['missing']

@pytest.mark.parametrize('probe', ['cached', 'limited', 'fusion'])
def test_forgotten_cached_evidence_opens_relevance_gate(tmp_path,monkeypatch,probe):
    from khipu import recall_prompt as rp, relevance, features
    monkeypatch.setenv('KHIPU_DATA_DIR',str(tmp_path/'state'))
    snap=tmp_path/'snapshot.sqlite'
    monkeypatch.setattr(hs,'snapshot_path',lambda:snap)
    monkeypatch.setattr(hs,'meta_path',lambda:tmp_path/'meta.json')
    monkeypatch.setattr(features,'enabled',lambda name:name=='relevance_floor')
    con=sqlite3.connect(snap);hs._create_schema(con)
    for eid,text,vector in [(1,'electric purple otters',[1.0,0.0]),(2,'unrelated forgotten-control gardening',[0.6,0.8])]:
        con.execute('INSERT INTO episodes(id,ts,summary,topics,people,decisions,preferences) VALUES (?,\'2026-10-23T00:00:00Z\',?,\'[]\',\'[]\',\'[]\',\'[]\')',(eid,text))
        con.execute('INSERT INTO memory_embeddings(profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES (\'p\',\'episode\',?,0,?,?)',(str(eid),text,hs._vector_to_blob(vector)))
    con.commit()
    hs._MATRIX_CACHE.clear()
    warm=hs.cosine_candidates_snapshot([1,0],'p',limit=8)
    assert {r['id'] for r in warm}=={'1','2'}
    con.execute('UPDATE episodes SET deleted_at=\'2026-10-23T12:00:00Z\' WHERE id=1')
    con.commit();con.close()
    stale = warm if probe == 'fusion' else hs.cosine_candidates_snapshot([1,0],'p',limit=1 if probe == 'limited' else 8)
    # Cached generations must fence current tombstones before candidate limits.
    if probe != 'fusion':
        assert {r['id'] for r in stale} == {'2'}
    output=rp._fuse_and_annotate('electric purple otters',tokens=['electric','purple','otters'],project=None,tz=None,lexical_rows=[],cosine_rows=stale,legs=['snapshot_cosine'],degraded=None,graph=False)
    print('cached candidates:',[(r['id'],r['score']) for r in stale])
    print('post-forget recall:',json.dumps(output,default=str))
    assert output['hits']==[], 'Forgotten-only evidence admitted an unrelated live row'


@pytest.mark.parametrize("kind", ["episode", "topic"])
@pytest.mark.parametrize("replace_file", [False, True])
def test_cached_reader_fences_current_tombstones_and_replacement(tmp_path, monkeypatch, kind, replace_file):
    snap = tmp_path / "reader.sqlite"
    monkeypatch.setattr(hs, "snapshot_path", lambda: snap)
    con = sqlite3.connect(snap)
    hs._create_schema(con)
    for ref, text, vector in [("1", "electric purple otters", [1., 0.]),
                              ("2", "live peer", [.6, .8])]:
        if kind == "episode":
            con.execute("INSERT INTO episodes(id,summary) VALUES (?,?)", (int(ref), text))
        else:
            con.execute("INSERT INTO topics(slug,title,body) VALUES (?,?,?)", (ref, text, text))
        con.execute("INSERT INTO memory_embeddings(profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('p',?,?,0,?,?)",
                    (kind, ref, text, hs._vector_to_blob(vector)))
    con.commit()
    assert {r["id"] for r in hs.cosine_candidates_snapshot([1., 0.], "p", limit=2)} == {"1", "2"}
    if replace_file:
        replacement = tmp_path / "replacement.sqlite"
        target = sqlite3.connect(replacement)
        con.backup(target)
        con.close()
    else:
        target = con
    table, key = ("episodes", "id") if kind == "episode" else ("topics", "slug")
    target.execute(f"UPDATE {table} SET deleted_at='2026-10-23' WHERE {key} = ?", ("1",))
    target.commit()
    target.close()
    if replace_file:
        replacement.replace(snap)
    assert [r["id"] for r in hs.cosine_candidates_snapshot([1., 0.], "p", limit=1)] == ["2"]
    # The retained observer is shared only under this lock and holds no read
    # transaction. Cache eviction owns closing it, including after replacement.
    with hs._MATRIX_LOCK:
        reader = hs._MATRIX_CACHE["reader"]["con"]
        assert not reader.in_transaction
        hs._MATRIX_CACHE.clear()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            reader.execute("SELECT 1")


def test_an_empty_capable_source_table_can_gain_a_tombstone(tmp_path, monkeypatch):
    snap = tmp_path / "empty-source.sqlite"
    monkeypatch.setattr(hs, "snapshot_path", lambda: snap)
    con = sqlite3.connect(snap)
    hs._create_schema(con)
    for ref, vector in [("1", [1., 0.]), ("2", [.6, .8])]:
        con.execute("INSERT INTO memory_embeddings(profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('p','episode',?,0,'text',?)",
                    (ref, hs._vector_to_blob(vector)))
    con.commit()
    assert len(hs.cosine_candidates_snapshot([1., 0.], "p", limit=2)) == 2
    con.execute("INSERT INTO episodes(id,summary,deleted_at) VALUES (1,'forgotten','2026-10-23'),(2,'live',NULL)")
    con.commit()
    con.close()
    assert [r["id"] for r in hs.cosine_candidates_snapshot([1., 0.], "p", limit=1)] == ["2"]


def test_cached_reader_learns_new_tombstone_columns(tmp_path, monkeypatch):
    snap = tmp_path / "old-schema.sqlite"
    monkeypatch.setattr(hs, "snapshot_path", lambda: snap)
    con = sqlite3.connect(snap)
    con.execute("CREATE TABLE episodes(id INTEGER PRIMARY KEY, summary TEXT)")
    con.execute("CREATE TABLE memory_embeddings(profile TEXT,kind TEXT,ref TEXT,chunk_idx INTEGER,chunk_text TEXT,embedding BLOB)")
    for ref, vector in [("1", [1., 0.]), ("2", [.6, .8])]:
        con.execute("INSERT INTO episodes(id,summary) VALUES (?,'text')", (int(ref),))
        con.execute("INSERT INTO memory_embeddings VALUES ('p','episode',?,0,'text',?)",
                    (ref, hs._vector_to_blob(vector)))
    con.commit()
    assert len(hs.cosine_candidates_snapshot([1., 0.], "p", limit=2)) == 2
    con.execute("ALTER TABLE episodes ADD COLUMN deleted_at TEXT")
    con.execute("UPDATE episodes SET deleted_at='2026-10-23' WHERE id=1")
    con.commit()
    con.close()
    assert [r["id"] for r in hs.cosine_candidates_snapshot([1., 0.], "p", limit=1)] == ["2"]

