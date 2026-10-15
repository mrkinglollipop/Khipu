"""Replica freshness, phase 1 (docs/plans/2026-10-11-replica-freshness.md):
per-table cursors, lag per kind of change against a mocked hub, and the
fresh / behind / stale / unknown states. The audit's five scenarios (remote
insertion, topic-only correction, deletion, failed refresh, capture without
embeddings) each assert the reported state. Replica-only: no Postgres."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from khipu import hub_snapshot as hs

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)
# The last full dump: four hours before NOW.
DUMP = NOW - timedelta(hours=4)
PROFILE = "gemini-embedding-2@768"

_HUB_COLUMNS = {
    "episodes": {"id", "ingested_at", "deleted_at"},
    "topics": {"slug", "content_hash", "updated_at", "deleted_at"},
    "decisions": {"id", "created_at", "superseded_at", "retracted_at", "superseded_by"},
    "deliverables": {"id", "created_at", "episode_id"},
    "embedding_profiles": {"id", "is_active"},
}


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _ny(dt: datetime) -> datetime:
    """A hub session in New York returns -04:00 offsets: any string
    comparison where an instant comparison belongs would show."""
    return dt.astimezone(timezone(timedelta(hours=-4)))


class FakeHub:
    """Just enough of the hub for ``measure_lag``: each query it sends is
    matched by its shape and answered from these lists."""

    def __init__(self) -> None:
        self.episodes: list[dict] = []
        self.topics: list[dict] = []
        self.decisions: list[dict] = []
        self.deliverables: list[dict] = []
        self.vectors: list[dict] = []
        self.profile: dict = {"id": PROFILE}
        self.queries: list[str] = []
        self.page_reads = 0

    def live_vectors(self) -> list[dict]:
        gone = {str(e["id"]) for e in self.episodes if e.get("deleted_at")}
        return [v for v in self.vectors if not (v["kind"] == "episode" and v["ref"] in gone)]

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self)


def _bucket(v: dict) -> str:
    return hashlib.md5(f"{v['kind']}\t{v['ref']}".encode()).hexdigest()[:3]


def _micro(dt: datetime | None) -> str:
    """The hub's build time as the vector digest spells it: whole
    microseconds since the epoch."""
    if dt is None:
        return ""
    return str((dt - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(microseconds=1))


def _select_list(sql: str) -> list[str]:
    return [c.strip() for c in sql[len("SELECT "):sql.index(" FROM ")].split(",")]


def _ep_line(e: dict, sql: str) -> str:
    """An episode as ``_episode_line_sql`` spells it, for the columns that
    SQL names."""
    parts = [str(e["id"])]
    if "md5(COALESCE(summary" in sql:
        parts.append(hashlib.md5((e.get("summary") or "").encode()).hexdigest())
    for col in ("project", "repo_root"):
        if f"COALESCE({col}::text" in sql:
            parts.append(e.get(col) or "")
    if "merged_from" in sql:
        parts.append(str(len((e.get("raw") or {}).get("merged_from") or [])))
    return "\t".join(parts)


def _topic_line(t: dict, sql: str) -> str:
    parts = [t["slug"], t.get("content_hash") or ""]
    for col in ("status", "superseded_by"):
        if f"COALESCE({col}::text" in sql:
            parts.append(t.get(col) or "")
    if "extract(epoch FROM event_at)" in sql:
        parts.append(_micro(t.get("event_at")))
    return "\t".join(parts)


def _vec_row(v: dict) -> tuple:
    return (v["kind"], v["ref"], v.get("chunk_idx", 0), v.get("content_hash"),
            _ny(v["built_at"]) if v["built_at"] else None)


class FakeCursor:
    def __init__(self, hub: FakeHub) -> None:
        self.hub = hub
        self._rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        h = self.hub
        h.queries.append(sql)
        p = list(params)
        if sql.startswith("SELECT now()"):
            rows = [(_ny(NOW),)]
        elif "FROM episodes WHERE ingested_at >" in sql:
            rows = sorted(((e["id"], _ny(e["ingested_at"])) for e in h.episodes
                           if e["ingested_at"] > p[0] and e.get("deleted_at") is None),
                          key=lambda r: (r[1], r[0]))[: p[1]]
        elif "FROM episodes WHERE deleted_at >" in sql:
            rows = sorted(((e["id"], _ny(e["deleted_at"])) for e in h.episodes
                           if e.get("deleted_at") and e["deleted_at"] > p[0]), key=lambda r: r[1])[: p[1]]
        elif "FROM topics WHERE deleted_at >" in sql:
            rows = sorted(((t["slug"], _ny(t["deleted_at"])) for t in h.topics
                           if t.get("deleted_at") and t["deleted_at"] > p[0]), key=lambda r: r[1])[: p[1]]
        elif "FROM memory_embeddings" in sql and "GROUP BY 1" in sql:
            by_bucket: dict[str, list[tuple]] = {}
            for v in h.live_vectors():
                key = (v["kind"], v["ref"], v.get("chunk_idx", 0), v.get("content_hash") or "", _micro(v["built_at"]))
                by_bucket.setdefault(_bucket(v), []).append(key)
            rows = [(b, hashlib.md5("\n".join(f"{k}\t{r}\t{i}\t{c}\t{t}" for k, r, i, c, t in sorted(keys))
                                    .encode()).hexdigest()) for b, keys in by_bucket.items()]
        elif "FROM memory_embeddings" in sql and "= ANY(%s)" in sql:
            h.page_reads += 1
            after, n = tuple(p[2:6]), p[6]
            rows = sorted((_bucket(v), *_vec_row(v)) for v in h.live_vectors() if _bucket(v) in set(p[1]))
            rows = [r for r in rows if r[:4] > after][:n]
        elif "FROM memory_embeddings" in sql and "ORDER BY built_at, kind" in sql:
            rows = sorted((_vec_row(v) for v in h.live_vectors() if v["built_at"] and v["built_at"] > p[1]),
                          key=lambda r: (r[4], r[0], r[1], r[2]))[: p[2]]
        elif "FROM episodes WHERE id <> ALL" in sql:
            skip = set(p[0])
            live = sorted((e for e in h.episodes if e.get("deleted_at") is None and e["id"] not in skip),
                          key=lambda e: e["id"])
            if "md5(string_agg" in sql:
                by_bucket: dict[int, list[str]] = {}
                for e in live:
                    by_bucket.setdefault(e["id"] // 256, []).append(_ep_line(e, sql))
                rows = [(b, hashlib.md5("\n".join(lines).encode()).hexdigest()) for b, lines in by_bucket.items()]
            else:
                rows = [(e["id"], _ny(e["ingested_at"]), _ep_line(e, sql)) for e in live
                        if e["id"] // 256 in set(p[1])][: p[2]]
        elif "FROM episodes WHERE id = ANY" in sql:
            rows = [(e["id"], _ny(e["deleted_at"]) if e.get("deleted_at") else None)
                    for e in h.episodes if e["id"] in set(p[0])]
        elif "FROM topics WHERE slug = ANY" in sql:
            rows = [(t["slug"], _ny(t["deleted_at"]) if t.get("deleted_at") else None)
                    for t in h.topics if t["slug"] in set(p[0])]
        elif "md5(string_agg" in sql and "FROM decisions" in sql:
            text = "\n".join("\t".join([str(d["id"]), _micro(d.get("superseded_at")),
                                         _micro(d.get("retracted_at")), str(d.get("superseded_by") or "")])
                             for d in sorted(h.decisions, key=lambda d: d["id"]))
            rows = [(hashlib.md5(text.encode()).hexdigest() if h.decisions else None,)]
        elif "md5(string_agg" in sql and "memory_embeddings" not in sql:
            live = sorted((t for t in h.topics if t.get("deleted_at") is None), key=lambda t: t["slug"])
            text = "\n".join(_topic_line(t, sql) for t in live)
            rows = [(hashlib.md5(text.encode()).hexdigest() if live else None,)]
        elif sql.startswith("SELECT slug, content_hash"):
            cols = _select_list(sql)
            rows = [tuple(_ny(t[c]) if c.endswith("_at") and t.get(c) else t.get(c) for c in cols)
                    for t in h.topics if t.get("deleted_at") is None]
        elif "FROM decisions WHERE" in sql:
            out = []
            for d in h.decisions:
                wm = max(x for x in (d["created_at"], d.get("superseded_at"), d.get("retracted_at")) if x)
                if wm > p[0]:
                    out.append((d["id"], d.get("superseded_at"), d.get("retracted_at"),
                                d.get("superseded_by"), _ny(wm)))
            rows = sorted(out, key=lambda r: (r[-1], r[0]))[: p[1]]
        elif "FROM deliverables WHERE created_at >" in sql:
            rows = [(d["id"], _ny(d["created_at"])) for d in h.deliverables if d["created_at"] > p[0]][: p[1]]
        elif "FROM embedding_profiles WHERE is_active" in sql:
            rows = [tuple(h.profile.get(c) for c in _select_list(sql))]
        elif "FROM memory_embeddings" in sql and "string_agg" not in sql:
            # The presence-only probe this module had before chunk identity
            # was compared, kept so the re-embed tests show what it missed.
            latest: dict[tuple, datetime] = {}
            for v in h.vectors:
                if v["built_at"] > p[1]:
                    key = (v["kind"], v["ref"])
                    latest[key] = max(latest.get(key, v["built_at"]), v["built_at"])
            rows = sorted(((k, r, _ny(b)) for (k, r), b in latest.items()), key=lambda r: r[2])[: p[2]]
        elif "FROM memory_embeddings" in sql:
            # The per-ref signature probe of f0ac7b6, kept so the manifest
            # tests show what it missed.
            recent = {(v["kind"], v["ref"]) for v in h.vectors if v["built_at"] > p[2]}
            by_ref: dict[tuple, list[dict]] = {}
            for v in h.vectors:
                if (v["kind"], v["ref"]) in recent:
                    by_ref.setdefault((v["kind"], v["ref"]), []).append(v)
            rows = sorted(((k, r, _ny(max(c["built_at"] for c in cs)),
                            ",".join(f"{c.get('chunk_idx', 0)}:{c.get('content_hash') or ''}"
                                     for c in sorted(cs, key=lambda c: c.get("chunk_idx", 0))))
                           for (k, r), cs in by_ref.items()), key=lambda r: r[2])[: p[3]]
        else:
            raise AssertionError(f"unexpected hub query: {sql}")
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class ReplicaCase(unittest.TestCase):
    """A replica dumped at DUMP holding episode 1 (with a vector), topic
    ``alpha`` and decision 10; a hub in step with it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.snap = self.dir / "hub_snapshot.sqlite"
        con = sqlite3.connect(self.snap)
        hs._create_schema(con)
        before = DUMP - timedelta(hours=1)
        con.execute("INSERT INTO episodes (id, summary, ingested_at) VALUES (1, 'one', ?)", (_ts(before),))
        con.execute("INSERT INTO topics (slug, body, content_hash, updated_at) VALUES ('alpha', 'a', 'h1', ?)",
                    (_ts(before),))
        con.execute("INSERT INTO decisions (id, text, created_at) VALUES (10, 'd', ?)", (_ts(before),))
        con.execute("INSERT INTO embedding_profiles (id, provider, model, dim, is_active) "
                    "VALUES (?, 'gemini', 'gemini-embedding-2', 768, 1)", (PROFILE,))
        con.execute("INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, built_at) "
                    "VALUES (?, 'episode', '1', 0, ?)", (PROFILE, _ts(before)))
        con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('refreshed_at', ?)", (_ts(DUMP),))
        hs._dump_cursors(con, _ts(DUMP))
        con.commit()
        con.close()
        (self.dir / "meta.json").write_text(json.dumps({"refreshed_at": _ts(DUMP), "hub_dumped_at": _ts(DUMP)}))
        self.hub = FakeHub()
        self.hub.episodes.append({"id": 1, "ingested_at": before})
        self.hub.topics.append({"slug": "alpha", "content_hash": "h1", "updated_at": before})
        self.hub.decisions.append({"id": 10, "created_at": before})
        self.hub.vectors.append({"kind": "episode", "ref": "1", "built_at": before})
        self._patches = [
            mock.patch.object(hs, "snapshot_path", return_value=self.snap),
            mock.patch.object(hs, "meta_path", return_value=self.dir / "meta.json"),
            mock.patch.object(hs, "freshness_path", return_value=self.dir / "freshness.json"),
            mock.patch.object(hs, "_pg_columns", side_effect=lambda _cur, t: _HUB_COLUMNS.get(t, set())),
            mock.patch.object(hs, "_utcnow_iso", side_effect=lambda: _ts(NOW)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def health(self, age_s: int = 4 * 3600) -> dict:
        return {"ok": True, "exists": True, "refreshed_at": _ts(DUMP), "age_seconds": age_s}

    def measure(self) -> dict:
        lag = hs.measure_lag(self.hub.cursor())
        self.assertTrue(lag["measured"], lag)
        return hs.snapshot_freshness(None, self.health(), lag=lag)


class InStepTest(ReplicaCase):
    def test_a_replica_holding_everything_the_hub_has_is_fresh(self) -> None:
        out = self.measure()
        self.assertEqual(out["state"], "fresh")
        self.assertTrue(out["ok"])
        self.assertEqual(set(out["tables"]), set(hs.FRESHNESS_TABLES))
        self.assertEqual(out["tables"]["episodes"]["lag_seconds"], 0.0)
        self.assertEqual(out["tables"]["topics"]["missing"], 0)
        self.assertEqual(out["tables"]["episodes"]["synced_at"], _ts(DUMP))
        self.assertEqual(out["tables"]["topics"]["reconciled_at"], _ts(DUMP))

    def test_only_changes_since_the_dump_less_the_overlap_are_asked_for(self) -> None:
        lag = hs.measure_lag(self.hub.cursor())
        self.assertEqual(hs._utc(lag["since"]), DUMP - timedelta(seconds=hs.FRESHNESS_OVERLAP_S))


class RemoteInsertionTest(ReplicaCase):
    """Scenario 1: another machine captured an episode two hours ago."""

    def test_an_episode_only_the_hub_has_is_behind_by_its_age_and_never_red(self) -> None:
        self.hub.episodes.append({"id": 2, "ingested_at": NOW - timedelta(hours=2)})
        out = self.measure()
        self.assertEqual(out["state"], "behind")
        self.assertEqual(out["reason"], "snapshot_behind")
        self.assertTrue(out["ok"], "behind the hub between dumps is not a fault (decision 3)")
        self.assertEqual(out["tables"]["episodes"]["missing"], 1)
        self.assertEqual(out["tables"]["episodes"]["lag_seconds"], 7200.0)
        self.assertEqual(out["behind_ingest_seconds"], 7200.0)

    def test_the_incremental_write_clears_it_and_advances_the_episode_cursor(self) -> None:
        ingested = NOW - timedelta(hours=2)
        self.hub.episodes.append({"id": 2, "ingested_at": ingested})
        res = hs.upsert_episode({"id": 2, "summary": "two", "ingested_at": _ny(ingested)}, [])
        self.assertTrue(res["ok"], res)
        out = self.measure()
        self.assertEqual(out["state"], "fresh")
        cursor = out["tables"]["episodes"]
        self.assertEqual(cursor["newest_id"], 2)
        self.assertEqual(cursor["newest"], _ts(ingested))
        self.assertEqual(cursor["synced_at"], _ts(NOW))

    def test_a_young_gap_is_still_fresh(self) -> None:
        self.hub.episodes.append({"id": 2, "ingested_at": NOW - timedelta(minutes=5)})
        out = self.measure()
        self.assertEqual(out["state"], "fresh")
        self.assertEqual(out["tables"]["episodes"]["missing"], 1)


class TopicCorrectionTest(ReplicaCase):
    """Scenario 2: a topic's body changed on the hub, its updated_at
    backdated by the file mirror to before the dump."""

    def test_a_backdated_correction_is_seen_and_dated_no_earlier_than_the_dump(self) -> None:
        self.hub.topics[0]["content_hash"] = "h2"
        self.hub.topics[0]["updated_at"] = DUMP - timedelta(days=6)
        out = self.measure()
        self.assertEqual(out["state"], "behind")
        topics = out["tables"]["topics"]
        self.assertEqual(topics["missing"], 1)
        self.assertEqual(topics["lag_seconds"], 4 * 3600.0)

    def test_a_new_topic_counts_too_and_matching_digests_skip_the_manifest(self) -> None:
        self.measure()
        self.assertFalse(any(q.startswith("SELECT slug, content_hash") for q in self.hub.queries))
        self.hub.topics.append({"slug": "beta", "content_hash": "b", "updated_at": NOW - timedelta(hours=1)})
        out = self.measure()
        self.assertEqual(out["tables"]["topics"]["missing"], 1)
        self.assertEqual(out["tables"]["topics"]["lag_seconds"], 3600.0)
        self.assertEqual(out["state"], "behind")


class DeletionTest(ReplicaCase):
    """Scenario 3: episode 1 was forgotten on another machine an hour ago."""

    def test_a_tombstone_the_replica_lacks_is_behind_until_it_lands(self) -> None:
        self.hub.episodes[0]["deleted_at"] = NOW - timedelta(hours=1)
        out = self.measure()
        self.assertEqual(out["state"], "behind")
        self.assertEqual(out["tables"]["tombstones"]["missing"], 1)
        self.assertEqual(out["tables"]["tombstones"]["lag_seconds"], 3600.0)
        res = hs.forget_episode_in_snapshot(1)
        self.assertTrue(res["ok"], res)
        out = self.measure()
        self.assertEqual(out["state"], "fresh")
        self.assertEqual(out["tables"]["tombstones"]["missing"], 0)
        self.assertEqual(out["tables"]["tombstones"]["synced_at"], _ts(NOW))

    def test_a_queued_erasure_counts_as_applied(self) -> None:
        self.hub.episodes[0]["deleted_at"] = NOW - timedelta(hours=1)
        with mock.patch.object(hs, "_pending_erasure_ids", return_value={1}):
            out = self.measure()
        self.assertEqual(out["tables"]["tombstones"]["missing"], 0)


class FailedRefreshTest(ReplicaCase):
    """Scenario 4: the nightly full dump could not reach the hub."""

    def test_a_dump_that_failed_after_the_last_success_is_stale_and_red(self) -> None:
        with mock.patch.object(hs, "try_hub_connect", side_effect=OSError("connection refused")):
            with self.assertRaises(OSError):
                hs.refresh()
        self.assertTrue(self.snap.is_file(), "a failed dump never touches the replica")
        out = self.measure()
        self.assertEqual(out["state"], "stale")
        self.assertEqual(out["reason"], "snapshot_refresh_failed")
        self.assertFalse(out["ok"])
        self.assertIn("connection refused", out["refresh_error"]["error"])

    def test_an_overdue_dump_is_stale_even_when_the_hub_agrees(self) -> None:
        lag = hs.measure_lag(self.hub.cursor())
        out = hs.snapshot_freshness(None, self.health(age_s=hs.SNAPSHOT_MAX_AGE_S + 60), lag=lag)
        self.assertEqual(out["state"], "stale")
        self.assertEqual(out["reason"], "snapshot_refresh_overdue")
        self.assertFalse(out["ok"])

    def test_with_the_hub_down_the_state_is_unknown_with_the_last_measurement(self) -> None:
        self.hub.episodes.append({"id": 2, "ingested_at": NOW - timedelta(hours=2)})
        self.measure()
        out = hs.snapshot_freshness(None, self.health())
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["reason"], "hub_not_measured")
        self.assertTrue(out["ok"])
        self.assertEqual(out["last_known"]["state"], "behind")
        self.assertEqual(out["last_known"]["tables"]["episodes"]["missing"], 1)


class CaptureWithoutEmbeddingsTest(ReplicaCase):
    """Scenario 5: episode 2's embedding failed, so nothing wrote it to the
    replica; episode 3, captured later, embedded and was written. The
    replica's newest episode is current, yet it is missing one."""

    def test_the_unembedded_episode_is_missing_though_a_later_one_arrived(self) -> None:
        self.hub.episodes.append({"id": 2, "ingested_at": NOW - timedelta(hours=3)})
        third = NOW - timedelta(minutes=1)
        self.hub.episodes.append({"id": 3, "ingested_at": third})
        self.hub.vectors.append({"kind": "episode", "ref": "3", "built_at": third, "content_hash": "c"})
        res = hs.upsert_episode({"id": 3, "summary": "three", "ingested_at": _ts(third)},
                                [{"profile": PROFILE, "kind": "episode", "ref": 3, "chunk_idx": 0,
                                  "chunk_text": "three", "content_hash": "c", "embedding": [0.1],
                                  "built_at": _ts(third)}])
        self.assertTrue(res["ok"], res)
        out = self.measure()
        self.assertEqual(out["state"], "behind")
        episodes = out["tables"]["episodes"]
        self.assertEqual(episodes["missing"], 1)
        self.assertEqual(episodes["lag_seconds"], 3 * 3600.0)
        self.assertEqual(out["tables"]["vectors"]["missing"], 0)


class ReembedTest(ReplicaCase):
    """The hub holds newer vectors for a ref the replica already has: the
    replica still answers recall with the old ones."""

    def _hub_chunk(self, **kw) -> None:
        self.hub.vectors.append({"kind": "episode", "ref": "1", "chunk_idx": 0,
                                 "built_at": NOW - timedelta(hours=2), **kw})

    def test_changed_text_is_behind(self) -> None:
        self.hub.vectors.clear()
        self._hub_chunk(content_hash="new")
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)
        self.assertEqual(out["tables"]["vectors"]["lag_seconds"], 7200.0)
        self.assertEqual(out["state"], "behind")

    def test_an_added_chunk_is_behind(self) -> None:
        self.hub.vectors.append({"kind": "episode", "ref": "1", "chunk_idx": 1,
                                 "built_at": NOW - timedelta(hours=2)})
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)
        self.assertEqual(out["state"], "behind")

    def test_a_rebuild_of_the_same_text_is_behind(self) -> None:
        self.hub.vectors.clear()
        self._hub_chunk()
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)

    def test_writing_the_hub_rows_clears_it(self) -> None:
        self.hub.vectors.clear()
        built = NOW - timedelta(hours=2)
        self._hub_chunk(content_hash="new")
        res = hs.upsert_episode({"id": 1, "summary": "one", "ingested_at": _ts(DUMP - timedelta(hours=1))},
                                [{"profile": PROFILE, "kind": "episode", "ref": 1, "chunk_idx": 0,
                                  "chunk_text": "one", "content_hash": "new", "embedding": [0.1],
                                  "built_at": _ts(built)}])
        self.assertTrue(res["ok"], res)
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 0)
        self.assertEqual(out["state"], "fresh")


class VectorCase(ReplicaCase):
    def _replica(self, *rows: tuple) -> None:
        con = sqlite3.connect(self.snap)
        con.execute("DELETE FROM memory_embeddings")
        con.executemany("INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, content_hash, built_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", [(PROFILE, *r) for r in rows])
        con.commit()
        con.close()

    def _hub(self, *rows: tuple) -> None:
        self.hub.vectors[:] = [{"kind": k, "ref": r, "chunk_idx": i, "content_hash": c, "built_at": b}
                               for k, r, i, c, b in rows]



class VectorManifestTest(VectorCase):
    """Chunk-by-chunk reconciliation: a stale chunk beside a newer one, a
    chunk pruned or a ref deleted on the hub with no newer build to point at
    it, and a replica row with no build time."""

    def test_a_newer_chunk_does_not_mask_a_stale_one_beside_it(self) -> None:
        self._hub(("episode", "1", 0, "a", NOW - timedelta(hours=2)), ("episode", "1", 1, "b", NOW - timedelta(hours=1)))
        self._replica(("episode", "1", 0, "a", _ts(DUMP - timedelta(hours=1))),
                      ("episode", "1", 1, "b", _ts(NOW - timedelta(hours=1))))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)
        self.assertEqual(out["tables"]["vectors"]["lag_seconds"], 7200.0)
        self.assertEqual(out["state"], "behind")

    def test_a_chunk_pruned_on_the_hub_is_behind_from_the_dump(self) -> None:
        before = DUMP - timedelta(hours=1)
        self._hub(("episode", "1", 0, "a", before))
        self._replica(("episode", "1", 0, "a", _ts(before)), ("episode", "1", 1, "b", _ts(before)))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)
        self.assertEqual(out["tables"]["vectors"]["lag_seconds"], 4 * 3600.0)
        self.assertEqual(out["state"], "behind")

    def test_a_ref_deleted_on_the_hub_is_behind(self) -> None:
        before = DUMP - timedelta(hours=1)
        self._hub(("episode", "1", 0, "a", before))
        self._replica(("episode", "1", 0, "a", _ts(before)), ("topic", "beta", 0, "t", _ts(before)))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)
        self.assertEqual(out["state"], "behind")

    def test_a_replica_row_with_no_build_time_is_unknown_not_fresh(self) -> None:
        self._hub(("episode", "1", 0, "a", NOW - timedelta(hours=1)))
        self._replica(("episode", "1", 0, "a", None))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 0)
        self.assertEqual(out["tables"]["vectors"]["unverified"], 1)
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["reason"], "version_evidence_missing")
        self.assertFalse(json.loads((self.dir / "freshness.json").read_text())["in_step"])

    def test_matching_manifests_fetch_no_bucket(self) -> None:
        before = DUMP - timedelta(hours=1)
        self._hub(("episode", "1", 0, "a", before), ("episode", "1", 1, "b", before))
        self._replica(("episode", "1", 0, "a", _ts(before)), ("episode", "1", 1, "b", _ts(before)))
        out = self.measure()
        self.assertEqual(out["state"], "fresh")
        self.assertEqual(out["tables"]["vectors"]["buckets_differing"], 0)
        self.assertFalse(any("= ANY(%s)" in q for q in self.hub.queries))


class CursorAndErrorEvidenceTest(ReplicaCase):
    def test_a_write_skipped_by_a_running_dump_is_noted_against_its_tables(self) -> None:
        with mock.patch.object(hs, "_acquire_refresh_lock", return_value=None):
            res = hs.upsert_episode({"id": 2, "summary": "two"}, [], decision_rows=[{"id": 11}])
        self.assertFalse(res["ok"])
        out = hs.snapshot_freshness(None, self.health())
        for name in ("episodes", "vectors", "decisions"):
            self.assertEqual(out["tables"][name]["last_error"]["error"], "hub snapshot refresh in progress")
        self.assertNotIn("last_error", out["tables"]["topics"])

    def test_a_dump_clears_errors_noted_before_it_and_keeps_those_noted_during_it(self) -> None:
        clock = iter(NOW + timedelta(minutes=m) for m in range(-10, 50))

        def tick() -> str:
            return _ts(next(clock))

        side = json.loads((self.dir / "meta.json").read_text())
        side.update({"last_decision_sync_at": "2026-10-14T00:00:00+00:00", "last_decision_sync_id": 77})
        (self.dir / "meta.json").write_text(json.dumps(side))

        def during(_cur, _con):
            hs._record_write_error(("vectors",), "refresh in progress")
            return 0

        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        names = ("_insert_episodes", "_insert_topics", "_insert_topic_revisions", "_insert_nodes",
                 "_insert_edges", "_insert_profiles", "_insert_decisions", "_insert_deliverables")
        with mock.patch.object(hs, "_utcnow_iso", side_effect=tick):
            hs._record_write_error(("episodes",), "database is locked")
            with mock.patch.object(hs, "try_hub_connect", return_value=conn), \
                    mock.patch.object(hs, "_insert_memory_embeddings", side_effect=during), \
                    mock.patch.multiple(hs, **{n: mock.Mock(return_value=0) for n in names}):
                self.assertTrue(hs.refresh()["ok"])
        side = json.loads((self.dir / "meta.json").read_text())
        self.assertEqual(set(side["write_errors"]), {"vectors"})
        self.assertEqual(side["last_decision_sync_id"], 77, "a dump must carry the decision cursor")
        self.assertNotIn("refresh_error", side)
        con = sqlite3.connect(self.snap)
        cursors = hs._read_cursors(con)
        con.close()
        self.assertEqual(set(cursors), set(hs.FRESHNESS_TABLES))
        self.assertEqual(cursors["topics"]["reconciled_at"], side["refreshed_at"])

    def test_cursors_never_move_backwards(self) -> None:
        con = sqlite3.connect(self.snap)
        hs._advance_cursor(con, "episodes", synced_at=_ts(NOW), newest=_ts(DUMP - timedelta(days=30)),
                           newest_id=0)
        row = hs._read_cursors(con)["episodes"]
        con.close()
        self.assertEqual(row["newest"], _ts(DUMP - timedelta(hours=1)))
        self.assertEqual(row["newest_id"], 1)
        self.assertEqual(row["synced_at"], _ts(NOW))

    def test_an_old_replica_without_snapshot_meta_takes_writes_without_cursors(self) -> None:
        con = sqlite3.connect(self.dir / "v1.sqlite")
        hs._create_schema(con, version=1)
        hs._advance_cursor(con, "episodes", synced_at=_ts(NOW), newest=_ts(NOW))
        self.assertEqual(hs._read_cursors(con), {})
        con.close()


class MeasurementLimitsTest(ReplicaCase):
    def test_a_cut_list_with_no_gap_found_is_unknown_not_fresh(self) -> None:
        for i in range(2, 6):
            when = NOW - timedelta(minutes=i)
            self.hub.episodes.append({"id": i, "ingested_at": when})
            hs.upsert_episode({"id": i, "summary": "x", "ingested_at": _ts(when)}, [])
        lag = hs.measure_lag(self.hub.cursor(), limit=3)
        out = hs.snapshot_freshness(None, self.health(), lag=lag)
        self.assertTrue(out["tables"]["episodes"]["truncated"])
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["reason"], "measurement_truncated")

    def test_a_failing_table_leaves_the_rest_unmeasured_and_the_state_unknown(self) -> None:
        real = FakeCursor.execute

        def broken(cur, sql, params=()):
            if "FROM decisions WHERE" in sql:
                raise RuntimeError("relation decisions does not exist")
            return real(cur, sql, params)

        with mock.patch.object(FakeCursor, "execute", broken):
            lag = hs.measure_lag(self.hub.cursor())
        tables = lag["tables"]
        self.assertIn("relation decisions", tables["decisions"]["error"])
        self.assertIn("decisions failed first", tables["vectors"]["error"])
        self.assertEqual(tables["episodes"]["missing"], 0)
        out = hs.snapshot_freshness(None, self.health(), lag=lag)
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["reason"], "table_unmeasured")

    def test_no_replica_or_no_dump_time_is_reported_not_raised(self) -> None:
        with mock.patch.object(hs, "snapshot_path", return_value=self.dir / "absent.sqlite"):
            self.assertEqual(hs.measure_lag(self.hub.cursor()), {"measured": False, "reason": "replica missing"})
        with mock.patch.object(hs, "meta", return_value={}):
            self.assertEqual(hs.measure_lag(self.hub.cursor())["reason"], "no full dump on record")


class RecallTrustTest(ReplicaCase):
    """snapshot_is_fresh: the dump's age is one input, not the gate."""

    def _write(self, record: dict) -> None:
        (self.dir / "freshness.json").write_text(json.dumps(record))

    def test_an_overdue_dump_is_trusted_only_on_a_recent_in_step_measurement(self) -> None:
        overdue = self.health(age_s=hs.SNAPSHOT_MAX_AGE_S + 3600)
        hs.snapshot_freshness(None, overdue, lag=hs.measure_lag(self.hub.cursor()))
        record = json.loads((self.dir / "freshness.json").read_text())
        self.assertEqual(record["state"], "stale")
        self.assertTrue(record["in_step"])
        real_now = datetime.now(timezone.utc)
        with mock.patch.object(hs, "snapshot_health", side_effect=lambda: dict(overdue)):
            self._write({**record, "checked_at": _ts(real_now - timedelta(minutes=5))})
            fresh, health = hs.snapshot_is_fresh()
            self.assertTrue(fresh)
            self.assertEqual(health["trusted_by"], "measurement")
            self._write({**record, "checked_at": _ts(real_now - timedelta(seconds=hs.LAST_KNOWN_TRUST_S + 60))})
            self.assertFalse(hs.snapshot_is_fresh()[0], "too old a measurement")
            self._write({**record, "checked_at": _ts(real_now), "in_step": False})
            self.assertFalse(hs.snapshot_is_fresh()[0], "a measurement that found a gap")
            self._write({**record, "checked_at": _ts(real_now), "refreshed_at": _ts(DUMP - timedelta(days=1))})
            self.assertFalse(hs.snapshot_is_fresh()[0], "a measurement of an earlier dump")

    def test_a_young_dump_measured_behind_is_not_trusted(self) -> None:
        self.hub.episodes.append({"id": 2, "ingested_at": NOW - timedelta(hours=2)})
        young = self.health(age_s=60)
        out = hs.snapshot_freshness(None, young, lag=hs.measure_lag(self.hub.cursor()))
        self.assertEqual(out["state"], "behind")
        record = json.loads((self.dir / "freshness.json").read_text())
        real_now = datetime.now(timezone.utc)
        with mock.patch.object(hs, "snapshot_health", side_effect=lambda: dict(young)):
            self._write({**record, "checked_at": _ts(real_now - timedelta(minutes=5))})
            fresh, health = hs.snapshot_is_fresh()
            self.assertFalse(fresh, "recall must not trust a replica measured as behind")
            self.assertEqual(health["distrusted_by"], "measurement")
            self._write({**record, "checked_at": _ts(real_now - timedelta(seconds=hs.LAST_KNOWN_TRUST_S + 60))})
            self.assertTrue(hs.snapshot_is_fresh()[0], "an old verdict gives way to the dump's age")
            self._write({**record, "checked_at": _ts(real_now), "refreshed_at": _ts(DUMP - timedelta(days=1))})
            self.assertTrue(hs.snapshot_is_fresh()[0], "a verdict on an earlier dump says nothing of this one")

    def test_a_young_dump_is_trusted_and_a_missing_one_never(self) -> None:
        with mock.patch.object(hs, "snapshot_health", return_value=self.health(age_s=60)):
            self.assertTrue(hs.snapshot_is_fresh()[0])
        with mock.patch.object(hs, "snapshot_health", return_value={"exists": False}):
            self.assertFalse(hs.snapshot_is_fresh()[0])


if __name__ == "__main__":
    unittest.main()


class VectorGenerationTest(VectorCase):
    """A replica chunk is the hub's generation only when it carries the hub's
    own build time: the dump copies it and a catch-up gets it back from the
    write. Ordering this Mac's clock against the hub's gave both false fresh
    and false behind."""

    def test_a_copy_stamped_later_than_the_hub_build_is_another_generation(self) -> None:
        # A fast Mac stamped an old vector after the hub rebuilt the same text.
        self._hub(("episode", "1", 0, "a", NOW - timedelta(minutes=90)))
        self._replica(("episode", "1", 0, "a", _ts(NOW - timedelta(minutes=75))))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)
        self.assertEqual(out["state"], "behind")

    def test_the_hub_build_time_in_another_offset_is_the_same_generation(self) -> None:
        built = NOW - timedelta(hours=1)
        self._hub(("episode", "1", 0, "a", built))
        self._replica(("episode", "1", 0, "a", _ny(built).isoformat()))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 0)
        self.assertEqual(out["state"], "fresh")

    def test_upsert_chunks_returns_the_hubs_build_time_per_row(self) -> None:
        from khipu import embed as em

        stamps = [NOW - timedelta(minutes=5), NOW - timedelta(minutes=5)]
        cur = mock.Mock()
        cur.fetchone.side_effect = [(s,) for s in stamps]
        with mock.patch.object(em._profiles, "cached_spec", return_value=None):
            got = em._upsert_chunks(cur, PROFILE, [("episode", "1", 0, "a", "h0", [0.1]),
                                                   ("episode", "1", 1, "b", "h1", [0.2])])
        self.assertEqual(got, stamps)
        self.assertTrue(all("RETURNING built_at" in c.args[0] for c in cur.execute.call_args_list))

    def test_the_catch_up_copies_the_hub_build_time_whatever_this_macs_clock_says(self) -> None:
        from khipu import embed as em

        hub_built = NOW - timedelta(hours=1)

        class Cur:
            rows: list = []

            def execute(self, sql, params=None):
                if sql.startswith("SELECT id FROM episodes") and "FOR SHARE" in sql:
                    self.rows = [(1,)] if 1 in params[0] else []
                else:
                    self.rows = [(1, "same", [], [], [], [])] if "FROM episodes e WHERE" in sql else []

            def fetchall(self):
                return list(self.rows)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Conn:
            def __init__(self):
                self.cur = Cur()

            def cursor(self):
                return self.cur

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class SlowMacClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return hub_built - timedelta(minutes=1)

        self._replica()
        with mock.patch("khipu.db.connect", return_value=Conn()), \
                mock.patch("khipu.db.has_columns", return_value=False), \
                mock.patch.object(em, "_active_profile", return_value=PROFILE), \
                mock.patch.object(em, "_existing_hashes_for_refs", return_value={}), \
                mock.patch.object(em, "episode_text", return_value="same"), \
                mock.patch.object(em, "chunk_text", return_value=["same"]), \
                mock.patch.object(em, "_api_texts", side_effect=lambda _p, pairs: [t for _, t in pairs]), \
                mock.patch.object(em, "embed_batch", return_value=[[1.0, 0.0]]), \
                mock.patch.object(em, "_upsert_chunks", return_value=[hub_built]), \
                mock.patch("datetime.datetime", SlowMacClock):
            em.embed_recent_missing(limit=1)
        self._hub(("episode", "1", 0, em._md5("same"), hub_built))
        con = sqlite3.connect(self.snap)
        (held,) = con.execute("SELECT built_at FROM memory_embeddings").fetchone()
        con.close()
        self.assertEqual(hs._utc(held), hub_built)
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 0)
        self.assertEqual(out["state"], "fresh")

    def test_no_replica_writer_stamps_a_vector_with_this_macs_clock(self) -> None:
        import inspect

        from khipu import embed as em

        self.assertNotIn("built_at = datetime.now", inspect.getsource(em))


class VectorBoundTest(VectorCase):
    """Differing buckets are read in pages no further than the probe's limit;
    a replica chunk is judged deleted on the hub only in a bucket read to its
    end."""

    def _big_bucket(self, ref: str, n: int, *, extra: bool) -> tuple[list, list]:
        before = DUMP - timedelta(hours=1)
        hub = [("episode", ref, i, f"h{i}", before) for i in range(n)]
        rep = [("episode", ref, 0, "old", _ts(before))] + [("episode", ref, i, f"h{i}", _ts(before))
                                                            for i in range(1, n)]
        if extra:
            rep.append(("episode", ref, n, "gone?", _ts(before)))
        return hub, rep

    def vectors(self, limit: int) -> dict:
        return hs.measure_lag(self.hub.cursor(), limit=limit)["tables"]["vectors"]

    def test_one_large_bucket_is_read_no_further_than_the_limit(self) -> None:
        hub, rep = self._big_bucket("1", 30, extra=True)
        self._hub(*hub)
        self._replica(*rep)
        v = self.vectors(10)
        self.assertLessEqual(v["checked"], 10)
        self.assertTrue(v["truncated"])
        # Chunk 0 differs; the replica-only chunk 30 sits past the cut, unjudged.
        self.assertEqual(v["missing"], 1)

    def test_a_bucket_read_in_pages_to_its_end_infers_deletions(self) -> None:
        hub, rep = self._big_bucket("1", 30, extra=True)
        self._hub(*hub)
        self._replica(*rep)
        with mock.patch.object(hs, "_VEC_PAGE_ROWS", 7):
            v = self.vectors(1000)
        self.assertFalse(v["truncated"])
        self.assertEqual(v["checked"], 30)
        self.assertEqual(v["missing"], 2)
        self.assertEqual(self.hub.page_reads, 5)

    def test_buckets_before_the_cut_are_whole_and_the_rest_unjudged(self) -> None:
        refs = sorted((f"r{i}" for i in range(40)), key=lambda r: hs._vec_bucket("episode", r))
        small, big = refs[0], refs[-1]
        self.assertLess(hs._vec_bucket("episode", small), hs._vec_bucket("episode", big))
        before = DUMP - timedelta(hours=1)
        small_hub = [("episode", small, i, f"s{i}", before) for i in range(3)]
        small_rep = [(k, r, i, c, _ts(b)) for k, r, i, c, b in small_hub] + [("episode", small, 3, "s3", _ts(before))]
        big_hub, big_rep = self._big_bucket(big, 20, extra=True)
        self._hub(*small_hub, *big_hub)
        self._replica(*small_rep, *big_rep)
        v = self.vectors(10)
        self.assertTrue(v["truncated"])
        self.assertLessEqual(v["checked"], 10)
        # The small bucket's pruned chunk and the big bucket's changed chunk 0;
        # the big bucket's replica-only chunk is past the cut.
        self.assertEqual(v["missing"], 2)


class ClockSkewTest(VectorCase):
    """No Mac-side time is ever compared with a hub-side one. The dump keeps
    the hub's own clock at its start; every cutoff and floor is taken from
    that, so a Mac clock ahead of or behind the hub's cannot hide a change."""

    HUB_DUMP = DUMP - timedelta(minutes=45)  # the hub's clock when this Mac said DUMP

    def _hub_dumped_at(self, when: datetime | None, *, mac: datetime = DUMP) -> None:
        side = {"refreshed_at": _ts(mac)}
        if when is not None:
            side["hub_dumped_at"] = _ts(when)
        (self.dir / "meta.json").write_text(json.dumps(side))
        con = sqlite3.connect(self.snap)
        con.execute("DELETE FROM snapshot_meta WHERE key LIKE 'cursor:%'")
        hs._dump_cursors(con, _ts(mac))
        con.commit()
        con.close()

    def test_a_rebuild_after_the_dump_is_seen_whatever_this_macs_clock_said(self) -> None:
        # Codex round 4: the dump is stamped 10:00 by this Mac while the hub
        # reads 09:15; the replica holds the 09:00 build and the hub rebuilds
        # the same text at 09:30.
        self._hub_dumped_at(self.HUB_DUMP)
        held = DUMP - timedelta(hours=1)
        rebuilt = DUMP - timedelta(minutes=30)
        self._hub(("episode", "1", 0, "a", rebuilt))
        self._replica(("episode", "1", 0, "a", _ts(held)))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)
        self.assertEqual(out["tables"]["vectors"]["lag_seconds"], (NOW - rebuilt).total_seconds())
        self.assertEqual(out["state"], "behind")

    def test_generation_evidence_is_compared_with_no_cutoff_at_all(self) -> None:
        # A rebuild long before any cutoff still makes the bucket differ.
        old_held, old_rebuilt = DUMP - timedelta(days=3), DUMP - timedelta(days=2)
        self._hub(("episode", "1", 0, "a", old_rebuilt))
        self._replica(("episode", "1", 0, "a", _ts(old_held)))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["buckets_differing"], 1)
        self.assertEqual(out["tables"]["vectors"]["missing"], 1)
        self.assertFalse(any("ORDER BY built_at" in q for q in self.hub.queries))

    def test_a_replica_row_with_no_build_time_is_unknown_however_old_the_hub_build(self) -> None:
        self._hub(("episode", "1", 0, "a", DUMP - timedelta(days=2)))
        self._replica(("episode", "1", 0, "a", None))
        out = self.measure()
        self.assertEqual(out["tables"]["vectors"]["unverified"], 1)
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["reason"], "version_evidence_missing")

    def test_changes_after_the_hubs_dump_time_are_asked_for_in_every_table(self) -> None:
        self._hub_dumped_at(self.HUB_DUMP)
        after = DUMP - timedelta(minutes=30)  # after the hub's dump, before this Mac's stamp less the overlap
        cases = {
            "episodes": lambda: self.hub.episodes.append({"id": 2, "ingested_at": after}),
            "tombstones": lambda: self.hub.episodes[0].update(deleted_at=after),
            "decisions": lambda: self.hub.decisions.append({"id": 11, "created_at": after}),
            "deliverables": lambda: self.hub.deliverables.append({"id": 5, "created_at": after}),
        }
        for name, change in cases.items():
            with self.subTest(table=name):
                self.setUp_hub()
                change()
                lag = hs.measure_lag(self.hub.cursor())
                self.assertEqual(lag["tables"][name]["missing"], 1, lag["tables"][name])
                self.assertEqual(lag["tables"][name]["lag_seconds"], (NOW - after).total_seconds())

    def setUp_hub(self) -> None:
        before = DUMP - timedelta(hours=1)
        self.hub.episodes[:] = [{"id": 1, "ingested_at": before}]
        self.hub.decisions[:] = [{"id": 10, "created_at": before}]
        self.hub.deliverables[:] = []

    def test_changes_with_no_hub_time_are_dated_from_the_hubs_dump_not_this_macs(self) -> None:
        # This Mac's clock runs four hours ahead: it stamped the dump five
        # minutes before NOW, though on the hub's clock it ran at DUMP.
        self._hub_dumped_at(DUMP, mac=NOW - timedelta(minutes=5))
        before = DUMP - timedelta(hours=1)
        self._hub(("episode", "1", 0, "a", before))
        self._replica(("episode", "1", 0, "a", _ts(before)), ("episode", "1", 1, "b", _ts(before)))
        self.hub.topics[0].update(content_hash="h2")
        lag = hs.measure_lag(self.hub.cursor())
        self.assertEqual(lag["tables"]["vectors"]["lag_seconds"], (NOW - DUMP).total_seconds())
        self.assertEqual(lag["tables"]["topics"]["lag_seconds"], (NOW - DUMP).total_seconds())

    def test_a_replica_with_no_hub_dump_time_is_not_measured(self) -> None:
        self._hub_dumped_at(None)
        lag = hs.measure_lag(self.hub.cursor())
        self.assertFalse(lag["measured"])
        self.assertEqual(lag["reason"], "no hub dump time on record")
        out = hs.snapshot_freshness(None, self.health(), lag=lag)
        self.assertEqual(out["state"], "unknown")

    def test_the_dump_records_the_hubs_clock_before_reading_any_table(self) -> None:
        order: list[str] = []
        hub_start = DUMP - timedelta(minutes=45)

        class Cur:
            def execute(self, sql, params=None):
                order.append(sql)

            def fetchone(self):
                return (_ny(hub_start),)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = Cur()
        names = ("_insert_topics", "_insert_topic_revisions", "_insert_nodes", "_insert_edges",
                 "_insert_profiles", "_insert_memory_embeddings", "_insert_decisions", "_insert_deliverables")

        def episodes(_cur, _con):
            order.append("episodes")
            return 0

        with mock.patch.object(hs, "try_hub_connect", return_value=conn), \
                mock.patch.object(hs, "_insert_episodes", side_effect=episodes), \
                mock.patch.multiple(hs, **{n: mock.Mock(return_value=0) for n in names}):
            res = hs.refresh()
        self.assertTrue(res["ok"])
        self.assertEqual(order[:2], ["SELECT now()", "episodes"])
        side = json.loads((self.dir / "meta.json").read_text())
        self.assertEqual(hs._utc(side["hub_dumped_at"]), hub_start)
        self.assertEqual(hs._utc(res["hub_dumped_at"]), hub_start)

    def test_recall_trust_ages_a_verdict_by_this_macs_clock_alone(self) -> None:
        overdue = self.health(age_s=hs.SNAPSHOT_MAX_AGE_S + 3600)
        hs.snapshot_freshness(None, overdue, lag=hs.measure_lag(self.hub.cursor()))
        record = json.loads((self.dir / "meta.json").parent.joinpath("freshness.json").read_text())
        self.assertTrue(record["in_step"])
        real_now = datetime.now(timezone.utc)
        # The hub's clock runs four minutes ahead of this Mac's; the check
        # itself is older than the trust window on this Mac's clock.
        stale_check = {**record, "measured_at": _ts(real_now + timedelta(minutes=4) - timedelta(
            seconds=hs.LAST_KNOWN_TRUST_S - 60)), "checked_at": _ts(real_now - timedelta(
                seconds=hs.LAST_KNOWN_TRUST_S + 60))}
        (self.dir / "freshness.json").write_text(json.dumps(stale_check))
        with mock.patch.object(hs, "snapshot_health", side_effect=lambda: dict(overdue)):
            self.assertFalse(hs.snapshot_is_fresh()[0])
            no_mac_time = {k: v for k, v in record.items() if k != "checked_at"}
            no_mac_time["measured_at"] = _ts(real_now)
            (self.dir / "freshness.json").write_text(json.dumps(no_mac_time))
            self.assertFalse(hs.snapshot_is_fresh()[0], "a verdict with no time on this Mac's clock")

    def test_a_decision_sync_never_stamps_its_hub_cursor_with_this_macs_clock(self) -> None:
        import inspect

        src = inspect.getsource(hs.sync_decision_changes)
        self.assertNotIn("_utcnow_iso()", src)


class OutsideTheCutoffTest(VectorCase):
    """A cutoff only narrows where a change is looked for: whatever lies
    outside it is still caught by a digest of the whole table."""

    _hub_dumped_at = ClockSkewTest._hub_dumped_at

    def test_a_decision_restored_after_the_dump_is_not_fresh(self) -> None:
        # The dump held decision 10 superseded; the hub restored it since,
        # which clears the times the window is keyed on.
        superseded = DUMP - timedelta(hours=2)
        con = sqlite3.connect(self.snap)
        con.execute("UPDATE decisions SET superseded_by = 99, superseded_at = ? WHERE id = 10", (_ts(superseded),))
        con.commit()
        con.close()
        lag = hs.measure_lag(self.hub.cursor())
        self.assertEqual(lag["tables"]["decisions"]["missing"], 1)
        self.assertEqual(lag["tables"]["decisions"]["lag_seconds"], (NOW - DUMP).total_seconds())

    def test_a_change_inside_the_window_is_dated_by_its_own_time_alone(self) -> None:
        when = NOW - timedelta(minutes=5)
        self.hub.decisions[0].update(superseded_by=12, superseded_at=when)
        lag = hs.measure_lag(self.hub.cursor())
        self.assertEqual(lag["tables"]["decisions"]["missing"], 1)
        self.assertEqual(lag["tables"]["decisions"]["lag_seconds"], 300.0)

    def test_the_older_ingest_figure_is_taken_from_the_hubs_dump_time(self) -> None:
        self._hub_dumped_at(DUMP, mac=NOW - timedelta(minutes=5))
        health = {**self.health(), "refreshed_at": _ts(NOW - timedelta(minutes=5))}
        out = hs.snapshot_freshness(_ts(DUMP + timedelta(hours=1)), health, lag={"measured": False})
        self.assertEqual(out["behind_ingest_seconds"], 3600.0)

    def test_an_episode_ref_that_is_not_an_ascii_number_is_measured(self) -> None:
        self._hub(("episode", "\u00b2", 0, "a", DUMP - timedelta(hours=1)))
        self._replica(("episode", "\u00b2", 0, "a", _ts(DUMP - timedelta(hours=1))))
        lag = hs.measure_lag(self.hub.cursor())
        self.assertNotIn("error", lag["tables"]["vectors"])
        self.assertEqual(lag["tables"]["vectors"]["missing"], 0)


class ChangeKindTest(VectorCase):
    """Every kind of change the replica can miss, each seen by some check.
    Added: episodes and deliverables by the cutoff, the rest by digest.
    Edited in place: episodes, topics (content, status, supersession, event
    time), decisions and vectors by digest. Deleted or tombstoned: the
    tombstone check, and the episode and topic digests for anything the
    hub no longer holds live (a forget's deliverable deletes ride on the
    episode's tombstone). Purged or restored: the digests. Re-embedded: the
    vector generation. Profile switched or respecified: the vector check.
    Sidecar lost: the dump times are read from the replica itself."""

    EPISODES = {*_HUB_COLUMNS["episodes"], "summary", "project", "repo_root", "raw"}
    TOPICS = {*_HUB_COLUMNS["topics"], "status", "superseded_by", "event_at"}
    PROFILES = {*_HUB_COLUMNS["embedding_profiles"], "provider", "model", "dim", "normalize"}

    def columns(self, **tables: set):
        merged = {**_HUB_COLUMNS, **tables}
        return mock.patch.object(hs, "_pg_columns", side_effect=lambda _cur, t: merged.get(t, set()))

    def table(self, name: str) -> dict:
        return hs.measure_lag(self.hub.cursor())["tables"][name]

    def test_an_episode_edited_on_the_hub_is_behind_until_written(self) -> None:
        self.hub.episodes[0]["summary"] = "one"
        edits = {"merged summary": {"summary": "one\n\ntwo"}, "project": {"project": "khipu"},
                 "repo_root": {"repo_root": "repo"}, "merged capture": {"raw": {"merged_from": [{"source_id": 7}]}}}
        with self.columns(episodes=self.EPISODES):
            self.assertEqual(self.measure()["state"], "fresh")
            for name, edit in edits.items():
                with self.subTest(edit=name):
                    self.hub.episodes[0] = {"id": 1, "summary": "one", "ingested_at": DUMP - timedelta(hours=1),
                                            **edit}
                    episodes = self.table("episodes")
                    self.assertEqual(episodes["missing"], 1, episodes)
                    self.assertEqual(episodes["lag_seconds"], (NOW - DUMP).total_seconds())
            res = hs.upsert_episode({k: _ny(v) if isinstance(v, datetime) else v
                                     for k, v in self.hub.episodes[0].items()}, [])
            self.assertTrue(res["ok"], res)
            self.assertEqual(self.measure()["state"], "fresh")

    def test_an_episode_written_outside_the_window_or_restored_is_behind(self) -> None:
        self.hub.episodes.append({"id": 2, "ingested_at": DUMP - timedelta(days=2)})
        episodes = self.table("episodes")
        self.assertEqual((episodes["missing"], episodes["lag_seconds"]), (1, (NOW - DUMP).total_seconds()))
        self.hub.episodes.pop()
        con = sqlite3.connect(self.snap)
        con.execute("UPDATE episodes SET deleted_at = ? WHERE id = 1", (_ts(DUMP - timedelta(days=1)),))
        con.commit()
        con.close()
        self.assertEqual(self.table("episodes")["missing"], 1)

    def test_an_episode_the_hub_no_longer_holds_live_is_behind(self) -> None:
        self.hub.episodes[0]["deleted_at"] = NOW - timedelta(minutes=10)
        episodes = self.table("episodes")
        self.assertEqual((episodes["missing"], episodes["lag_seconds"]), (1, 600.0))
        self.hub.episodes.clear()
        self.hub.vectors.clear()
        episodes = self.table("episodes")
        self.assertEqual((episodes["missing"], episodes["lag_seconds"]), (1, (NOW - DUMP).total_seconds()))
        self.assertEqual(self.measure()["state"], "behind")

    def test_a_topic_purged_on_the_hub_is_behind(self) -> None:
        # Codex round 5: tombstoned and purged elsewhere, still live here.
        self.hub.topics.clear()
        out = self.measure()
        self.assertEqual(out["tables"]["topics"]["missing"], 1)
        self.assertEqual(out["tables"]["topics"]["lag_seconds"], (NOW - DUMP).total_seconds())
        self.assertEqual(out["state"], "behind")
        self.hub.topics.append({"slug": "alpha", "content_hash": "h1", "updated_at": DUMP,
                                "deleted_at": NOW - timedelta(minutes=10)})
        self.assertEqual(self.table("topics")["lag_seconds"], 600.0, "a tombstone is dated by its own time")

    def test_a_topic_restored_on_the_hub_is_behind(self) -> None:
        con = sqlite3.connect(self.snap)
        con.execute("UPDATE topics SET deleted_at = ? WHERE slug = 'alpha'", (_ts(DUMP - timedelta(days=1)),))
        con.commit()
        con.close()
        self.assertEqual(self.table("topics")["missing"], 1)

    def test_a_topic_superseded_or_redated_on_the_hub_is_behind(self) -> None:
        edits = {"status": {"status": "superseded"}, "superseded_by": {"superseded_by": "beta"},
                 "event_at": {"event_at": DUMP - timedelta(days=30)}}
        with self.columns(topics=self.TOPICS):
            self.assertEqual(self.measure()["state"], "fresh")
            for name, edit in edits.items():
                with self.subTest(edit=name):
                    self.hub.topics[0] = {"slug": "alpha", "content_hash": "h1",
                                          "updated_at": DUMP - timedelta(hours=1), **edit}
                    topics = self.table("topics")
                    self.assertEqual((topics["missing"], topics["lag_seconds"]), (1, (NOW - DUMP).total_seconds()))

    def test_the_active_profile_switched_on_the_hub_is_behind(self) -> None:
        # Codex round 5: the replica holds P2's vectors, matching the hub's,
        # but still activates P1, so local recall searches P1.
        other = "openai-3-small@1536"
        before = DUMP - timedelta(hours=1)
        con = sqlite3.connect(self.snap)
        con.execute("INSERT INTO embedding_profiles (id, provider, model, dim, is_active) "
                    "VALUES (?, 'openai', 'text-embedding-3-small', 1536, 0)", (other,))
        con.execute("INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, content_hash, built_at) "
                    "VALUES (?, 'episode', '1', 0, 'a', ?)", (other, _ts(before)))
        con.commit()
        con.close()
        self._hub(("episode", "1", 0, "a", before))
        self.hub.profile = {"id": other}
        out = self.measure()
        vectors = out["tables"]["vectors"]
        self.assertEqual((vectors["missing"], vectors["replica_profile"]), (1, PROFILE))
        self.assertEqual(vectors["lag_seconds"], (NOW - DUMP).total_seconds())
        self.assertEqual(out["state"], "behind")

    def test_the_active_profile_respecified_on_the_hub_is_behind(self) -> None:
        spec = {"id": PROFILE, "provider": "gemini", "model": "gemini-embedding-2", "dim": 768, "normalize": None}
        with self.columns(embedding_profiles=self.PROFILES):
            self.hub.profile = dict(spec)
            self.assertEqual(self.table("vectors")["missing"], 0)
            self.hub.profile = {**spec, "dim": 1536}
            self.assertEqual(self.table("vectors")["missing"], 1)

    def test_a_lost_sidecar_keeps_the_hubs_dump_time(self) -> None:
        # Codex round 5: the replica's own record outlives the sidecar.
        con = sqlite3.connect(self.snap)
        con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('hub_dumped_at', ?)", (_ts(DUMP),))
        con.commit()
        con.close()
        (self.dir / "meta.json").unlink()
        lag = hs.measure_lag(self.hub.cursor())
        self.assertTrue(lag["measured"], lag)
        self.assertEqual(hs._utc(lag["since"]), DUMP - timedelta(seconds=hs.FRESHNESS_OVERLAP_S))
        res = hs.upsert_embeddings([{"profile": PROFILE, "kind": "episode", "ref": "1", "chunk_idx": 0,
                                     "embedding": [0.1], "built_at": _ts(DUMP)}])
        self.assertTrue(res["ok"], res)
        side = json.loads((self.dir / "meta.json").read_text())
        self.assertEqual(hs._utc(side["hub_dumped_at"]), DUMP)

    def test_a_deliverable_deleted_by_a_forget_on_the_hub_is_caught_by_its_tombstone(self) -> None:
        con = sqlite3.connect(self.snap)
        con.execute("INSERT INTO deliverables (id, project, kind, episode_id, created_at) "
                    "VALUES (5, 'p', 'file', 1, ?)", (_ts(DUMP - timedelta(hours=1)),))
        con.commit()
        con.close()
        self.hub.episodes[0]["deleted_at"] = NOW - timedelta(hours=1)
        out = self.measure()
        self.assertEqual(out["tables"]["tombstones"]["missing"], 1)
        self.assertEqual(out["state"], "behind")
