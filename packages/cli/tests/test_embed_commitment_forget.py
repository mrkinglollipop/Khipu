"""Commitment vectors must serialize with completed forget; chunk ceilings are literal.

All database/provider boundaries are synthetic; forget runs its real cascade.
"""
from contextlib import nullcontext
from unittest import mock
import pytest
from khipu import embed
from tests import test_forget_ownership as fo


class WorldCursor(fo._WorldCur):
    def execute(self, sql, params=None):
        s = ' '.join(sql.split())
        if s.startswith("DELETE FROM memory_embeddings WHERE kind = 'commitment'"):
            self.rowcount = len(self.world['vectors'])
            self.world['vectors'].clear()
        elif s.startswith("UPDATE commitments SET status = 'closed'"):
            self.world['commitment_open'] = False
            self.rowcount = 1
        else:
            super().execute(sql, params)


class EmbedConnection:
    def __init__(self, world):
        self.world = world
        self.rows = []
        self.rowcount = 0
        self.sql = []
        self.locks = set()
        self.pending_forget = None
        self.on_insert = None
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def commit(self):
        self.locks.clear()
        if self.pending_forget:
            forget, self.pending_forget = self.pending_forget, None
            forget()
    def execute(self, sql, params=None):
        s = ' '.join(sql.split())
        self.sql.append((s, params))
        self.rows = []
        self.rowcount = 0
        if s.startswith('SELECT c.id, c.text FROM commitments') or s.startswith('SELECT id, text FROM commitments'):
            self.rows = [(42, 'SYNTHETIC-FORGOTTEN-COMMITMENT')] if self.world['commitment_open'] else []
        elif s.startswith('SELECT id, opened_episode FROM commitments'):
            if self.world['commitment_open']:
                self.rows = [(42, self.world.get('opened_episode', 7))]
                if 'FOR SHARE' in s:
                    self.locks.add('commitment')
        elif s.startswith('SELECT id FROM episodes'):
            self.rows = [(eid,) for eid in params[0]
                         if eid in self.world['episodes'] and not self.world['episodes'][eid]['deleted']]
            if self.rows and 'FOR SHARE' in s:
                self.locks.add('episode')
        elif s.startswith('INSERT INTO memory_embeddings'):
            if self.on_insert:
                self.on_insert()
            self.world['vectors'].append(params)
            self.rows = [('2026-10-22T12:00:00Z',)]
    def fetchall(self): return list(self.rows)
    def fetchone(self): return self.rows[0] if self.rows else None


def commitment_probe(tmp_path, lane, timing='provider'):
    payload = {'ts':'2026-10-01T09:00:00Z', 'summary':'SYNTHETIC-EPISODE',
               'harness':'codex', 'session_id':'codex:synthetic-audit', 'transcript_range':'0:10'}
    world = {'episodes': {7:dict(payload, id=7, ts=fo.BASE, raw=dict(payload), deleted=False)},
             'sources':[fo.ForgetOwnershipTest._source(None, 7, payload)],
             'sources_enabled':True, 'vectors':[], 'commitment_open':True}
    fo.write_lines(tmp_path, [payload])
    conn = EmbedConnection(world)
    forget_results = []
    def forget():
        result = fo.run_forget(world, 7, tmp_path,
            connect=lambda *a, **kw:fo._Conn(WorldCursor(world)))
        assert result['complete'], result
        assert world['vectors'] == []
        assert world['commitment_open'] is False
        forget_results.append(result)
    def request_forget():
        if conn.locks:
            assert conn.locks == {'episode', 'commitment'}
            conn.pending_forget = forget
        else:
            forget()
    conn.on_insert = request_forget if timing == 'insert' else None
    def provider(texts, **kwargs):
        if timing == 'provider':
            request_forget()
        return [[1.0, 0.0] for _ in texts]
    with mock.patch('khipu.db.connect', return_value=conn), \
         mock.patch.object(embed, '_active_profile', return_value=embed.PROFILE_001), \
         mock.patch.object(embed, '_resolve_profile', return_value=embed.PROFILE_001), \
         mock.patch.object(embed, '_existing_hashes', return_value={}), \
         mock.patch.object(embed, '_existing_hashes_for_refs', return_value={}), \
         mock.patch.object(embed, '_changed_topic_refs', return_value=[]), \
         mock.patch.object(embed, 'embed_batch', side_effect=provider), \
         mock.patch.object(embed._profiles, 'cached_spec', return_value=None), \
         mock.patch.object(embed._profiles, 'BulkLoad', return_value=nullcontext(mock.Mock())), \
         mock.patch.object(embed._profiles, 'ensure_profile_index'), \
         mock.patch('khipu.hub_snapshot.upsert_embeddings', return_value={'ok':True}):
        result = embed.backfill(kind='commitment') if lane=='backfill' else embed.embed_recent_missing()
    return world, forget_results, result


def zero_limit_probe():
    conn=EmbedConnection({'vectors':[]})
    sources=[('episode', str(i), f'Synthetic text {i}', '') for i in range(7)]
    with mock.patch('khipu.db.connect', return_value=conn), \
         mock.patch.object(embed, '_resolve_profile', return_value=embed.PROFILE_001), \
         mock.patch.object(embed, '_existing_hashes', return_value={}), \
         mock.patch.object(embed, '_iter_sources', return_value=iter(sources)):
        result=embed.backfill(limit=0, dry_run=True)
    return result


@pytest.mark.parametrize("lane", ["backfill", "catchup"])
def test_completed_forget_cannot_be_followed_by_commitment_vector_insert(tmp_path, lane):
    world, forgotten, result = commitment_probe(tmp_path, lane)
    assert forgotten[0]["complete"]
    assert world["episodes"][7]["deleted"]
    assert world["vectors"] == [], "Writer recreated a commitment vector after completed forget"
    assert result["embedded" if lane == "backfill" else "commitments_embedded"] == 0


def test_zero_chunk_ceiling_schedules_no_chunks():
    assert zero_limit_probe()["would_embed"] == 0, "Zero ceiling scheduled provider work"


@pytest.mark.parametrize('lane', ['backfill', 'catchup'])
def test_commitment_writer_holds_parent_and_commitment_until_commit(tmp_path, lane):
    world, forgotten, result = commitment_probe(tmp_path, lane, timing='insert')
    assert forgotten[0]['complete']
    assert world['vectors'] == []
    assert result['embedded' if lane == 'backfill' else 'commitments_embedded'] == 1


@pytest.mark.parametrize('dry_run', [False, True])
def test_zero_chunk_ceiling_never_calls_provider_or_sweeps(dry_run):
    conn = EmbedConnection({'vectors': []})
    with mock.patch('khipu.db.connect', return_value=conn), \
            mock.patch.object(embed, '_resolve_profile', return_value=embed.PROFILE_001), \
            mock.patch.object(embed, '_iter_sources') as sources, \
            mock.patch.object(embed, 'embed_batch') as provider:
        result = embed.backfill(limit=0, dry_run=dry_run)
    assert result['embedded'] == 0
    assert conn.sql == []
    sources.assert_not_called()
    provider.assert_not_called()


def test_negative_chunk_ceiling_is_rejected_before_database_access():
    with mock.patch('khipu.db.connect') as connect:
        with pytest.raises(ValueError, match='non-negative'):
            embed.backfill(limit=-1)
    connect.assert_not_called()


@pytest.mark.parametrize('parent', [7, None])
@pytest.mark.parametrize('open_commitment', [True, False])
@pytest.mark.parametrize('parent_live', [True, False])
def test_lock_accepts_only_open_commitments_with_live_parents(parent, open_commitment, parent_live):
    world = {'episodes': {7: {'deleted': not parent_live}}, 'commitment_open': open_commitment,
             'opened_episode': parent}
    conn = EmbedConnection(world)
    live = embed._lock_live_embedding_refs(conn, [('commitment', '42')])
    assert (('commitment', '42') in live) is (open_commitment and (parent is None or parent_live))
    lock_sql = [sql for sql, _ in conn.sql if 'FOR SHARE' in sql]
    if parent is not None and open_commitment:
        assert 'FROM episodes' in lock_sql[0]
        assert 'FROM commitments' in lock_sql[-1]


def test_changed_or_missing_opening_episode_is_refused():
    class ChangedParent(EmbedConnection):
        def execute(self, sql, params=None):
            if sql.startswith('SELECT id, opened_episode') and 'FOR SHARE' in sql:
                self.world['opened_episode'] = 8
            super().execute(sql, params)

    world = {'episodes': {7: {'deleted': False}, 8: {'deleted': False}}, 'commitment_open': True}
    assert embed._lock_live_embedding_refs(ChangedParent(world), [('commitment', '42')]) == set()
    world = {'episodes': {}, 'commitment_open': True}
    assert embed._lock_live_embedding_refs(EmbedConnection(world), [('commitment', '42')]) == set()
