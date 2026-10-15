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
        self.queries: list[str] = []

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self)


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
        elif "md5(string_agg" in sql:
            live = sorted((t["slug"], t.get("content_hash")) for t in h.topics if t.get("deleted_at") is None)
            text = "\n".join(f"{s}\t{c or ''}" for s, c in live)
            rows = [(hashlib.md5(text.encode()).hexdigest() if live else None,)]
        elif sql.startswith("SELECT slug, content_hash"):
            rows = [(t["slug"], t.get("content_hash"), _ny(t["updated_at"]))
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
            rows = [(PROFILE,)]
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
        con.execute("INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, built_at) "
                    "VALUES (?, 'episode', '1', 0, ?)", (PROFILE, _ts(before)))
        con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('refreshed_at', ?)", (_ts(DUMP),))
        hs._dump_cursors(con, _ts(DUMP))
        con.commit()
        con.close()
        (self.dir / "meta.json").write_text(json.dumps({"refreshed_at": _ts(DUMP)}))
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
        self.assertEqual(lag["since"], _ts(DUMP - timedelta(seconds=hs.FRESHNESS_OVERLAP_S)))


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
            self._write({**record, "measured_at": _ts(real_now - timedelta(minutes=5))})
            fresh, health = hs.snapshot_is_fresh()
            self.assertTrue(fresh)
            self.assertEqual(health["trusted_by"], "measurement")
            self._write({**record, "measured_at": _ts(real_now - timedelta(seconds=hs.LAST_KNOWN_TRUST_S + 60))})
            self.assertFalse(hs.snapshot_is_fresh()[0], "too old a measurement")
            self._write({**record, "measured_at": _ts(real_now), "in_step": False})
            self.assertFalse(hs.snapshot_is_fresh()[0], "a measurement that found a gap")
            self._write({**record, "measured_at": _ts(real_now), "refreshed_at": _ts(DUMP - timedelta(days=1))})
            self.assertFalse(hs.snapshot_is_fresh()[0], "a measurement of an earlier dump")

    def test_a_young_dump_measured_behind_is_not_trusted(self) -> None:
        self.hub.episodes.append({"id": 2, "ingested_at": NOW - timedelta(hours=2)})
        young = self.health(age_s=60)
        out = hs.snapshot_freshness(None, young, lag=hs.measure_lag(self.hub.cursor()))
        self.assertEqual(out["state"], "behind")
        record = json.loads((self.dir / "freshness.json").read_text())
        real_now = datetime.now(timezone.utc)
        with mock.patch.object(hs, "snapshot_health", side_effect=lambda: dict(young)):
            self._write({**record, "measured_at": _ts(real_now - timedelta(minutes=5))})
            fresh, health = hs.snapshot_is_fresh()
            self.assertFalse(fresh, "recall must not trust a replica measured as behind")
            self.assertEqual(health["distrusted_by"], "measurement")
            self._write({**record, "measured_at": _ts(real_now - timedelta(seconds=hs.LAST_KNOWN_TRUST_S + 60))})
            self.assertTrue(hs.snapshot_is_fresh()[0], "an old verdict gives way to the dump's age")
            self._write({**record, "measured_at": _ts(real_now), "refreshed_at": _ts(DUMP - timedelta(days=1))})
            self.assertTrue(hs.snapshot_is_fresh()[0], "a verdict on an earlier dump says nothing of this one")

    def test_a_young_dump_is_trusted_and_a_missing_one_never(self) -> None:
        with mock.patch.object(hs, "snapshot_health", return_value=self.health(age_s=60)):
            self.assertTrue(hs.snapshot_is_fresh()[0])
        with mock.patch.object(hs, "snapshot_health", return_value={"exists": False}):
            self.assertFalse(hs.snapshot_is_fresh()[0])


if __name__ == "__main__":
    unittest.main()
