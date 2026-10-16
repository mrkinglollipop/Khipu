"""Deterministic transaction interleavings; no PostgreSQL or provider access."""
from contextlib import nullcontext
from unittest import mock

import pytest

from khipu import embed
from tests import test_forget_ownership as fo


class RaceConnection:
    """Model PostgreSQL's conflicting row locks through the writer's commit.

    Episode forget runs its real cascade against the synthetic ownership
    database. Topic purge is simulated. Both wait on a held source row lock.
    """
    def __init__(self, tmp_path, kind="episode", timing="insert"):
        self.kind, self.timing = kind, timing
        self.locked = self.pending = self.deleted = False
        self.inserts, self.sql = [], []
        self.result = []
        self.root = tmp_path
        (tmp_path / "replica").mkdir()
        self.payload = {"ts": "2026-10-01T09:00:00Z", "summary": "FORGOTTEN-RACE-TEXT",
                        "session_id": "codex:race", "harness": "codex", "transcript_range": "0:10"}
        self.source_row = dict(self.payload, id=7, deleted_at=None)
        self.world = {"episodes": {7: dict(self.payload, id=7, ts=fo.BASE,
                                           raw=dict(self.payload), deleted=False)},
                      "sources": [fo.ForgetOwnershipTest._source(None, 7, self.payload)],
                      "sources_enabled": True}
        fo.write_lines(tmp_path, [self.payload])
        self.forget_result = None

    def forget(self):
        if self.locked:
            self.pending = True
            return
        if self.kind == "episode":
            with mock.patch("khipu.hub_snapshot.snapshot_path",
                            return_value=self.root / "replica/hub_snapshot.sqlite"), \
                    mock.patch.dict("os.environ", {"KHIPU_OUTBOX": str(self.root / "outbox")}):
                self.forget_result = fo.run_forget(self.world, 7, self.root)
            assert self.forget_result["complete"], self.forget_result
        else:
            self.forget_result = {"complete": True}
        self.deleted = True
        self.inserts.clear()

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.sql.append(sql)
        self.result = []
        if sql.startswith("SELECT e.id, e.summary"):
            self.result = [(7, self.payload["summary"], [], [], [], [])] if not self.deleted else []
        elif sql.startswith("SELECT t.slug"):
            self.result = []
        elif "FROM episodes" in sql or "FROM topics" in sql:
            if "FOR SHARE" in sql or "FOR UPDATE" in sql:
                self.locked = not self.deleted
            if not self.deleted:
                if "row_to_json" in sql:
                    self.result = [(dict(self.source_row),)]
                elif "ANY(%s)" in sql:
                    self.result = [(ref,) for ref in params[0]]
                else:
                    self.result = [(1,)]
        elif sql.startswith("INSERT INTO memory_embeddings"):
            if self.timing == "insert":
                self.forget()
            self.inserts.append(params)
            self.result = [("2026-10-15T12:00:00Z",)]
        self.rowcount = 0

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)

    def cursor(self):
        return self

    def commit(self):
        self.locked = False
        if self.pending:
            self.pending = False
            self.forget()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.locked = False

    def provider(self, texts, **kwargs):
        if self.timing == "provider":
            self.forget()
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.parametrize("episode_id", [7, None])
@pytest.mark.parametrize("timing", ["insert", "provider"])
def test_embed_capture_serializes_with_real_forget(tmp_path, episode_id, timing):
    conn = RaceConnection(tmp_path, timing=timing)
    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch.object(embed, "_active_profile", return_value="test-profile"), \
            mock.patch.object(embed, "embed_batch", side_effect=conn.provider), \
            mock.patch.object(embed._profiles, "cached_spec", return_value=None), \
            mock.patch("khipu.deliverables.deliverables_for_episode", return_value=[]), \
            mock.patch("khipu.hub_snapshot.upsert_episode", return_value={"ok": True}):
        result = embed.embed_on_capture(dict(conn.payload), episode_id=episode_id)
    assert result is (timing == "insert")
    assert conn.forget_result["complete"]
    assert conn.inserts == [], "plaintext survived a completed forget"


@pytest.mark.parametrize("kind", ["episode", "topic"])
@pytest.mark.parametrize("timing", ["insert", "provider"])
def test_bulk_backfill_locks_sources_until_commit(tmp_path, kind, timing):
    conn = RaceConnection(tmp_path, kind=kind, timing=timing)
    ref = "7" if kind == "episode" else "test-topic"
    bulk = mock.MagicMock()
    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch.object(embed, "_resolve_profile", return_value="test-profile"), \
            mock.patch.object(embed, "_existing_hashes", return_value={}), \
            mock.patch.object(embed, "_iter_sources", return_value=iter([
                (kind, ref, conn.payload["summary"], "") ])), \
            mock.patch.object(embed, "embed_batch", side_effect=conn.provider), \
            mock.patch.object(embed._profiles, "cached_spec", return_value=None), \
            mock.patch.object(embed._profiles, "BulkLoad", return_value=nullcontext(bulk)), \
            mock.patch.object(embed._profiles, "ensure_profile_index"):
        embed.backfill(kind=kind)
    assert conn.forget_result["complete"]
    assert conn.inserts == [], "backfill recreated forgotten chunks"


@pytest.mark.parametrize("timing", ["insert", "provider"])
def test_episode_catchup_serializes_with_forget(tmp_path, timing):
    conn = RaceConnection(tmp_path, timing=timing)
    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch.object(embed, "_active_profile", return_value="test-profile"), \
            mock.patch.object(embed, "_existing_hashes_for_refs", return_value={}), \
            mock.patch.object(embed, "embed_batch", side_effect=conn.provider), \
            mock.patch.object(embed._profiles, "cached_spec", return_value=None), \
            mock.patch("khipu.hub_snapshot.upsert_embeddings", return_value={"ok": True}):
        embed.embed_recent_missing()
    assert conn.forget_result["complete"]
    assert conn.inserts == [], "catch-up recreated forgotten chunks"


def test_media_asset_lock_spans_chunk_insertion_and_commit(tmp_path):
    class MediaConnection(RaceConnection):
        def execute(self, sql, params=None):
            if "SELECT id, sha256 FROM media_assets" in sql:
                self.locked = "FOR UPDATE" in sql or "FOR SHARE" in sql
                self.result = [("media-id", "old-hash")]
            else:
                super().execute(sql, params)

    conn = MediaConnection(tmp_path, kind="media")
    with mock.patch.object(embed, "embed_batch_images", return_value=[[1.0, 0.0]]), \
            mock.patch.object(embed._profiles, "cached_spec", return_value=None):
        embed._flush_media_batch(conn, "test-profile", [
            ("source", tmp_path / "image.png", "image.png", "image/png", "new-hash", b"image")
        ], {"embedded": 0, "batches": 0})
    assert conn.locked
    assert conn.pending, "deletion must wait while the vector transaction holds the asset"
    assert conn.inserts
    conn.commit()
    assert conn.deleted
    assert conn.inserts == []
