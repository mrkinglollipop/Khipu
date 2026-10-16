"""The forget ownership worlds of test_forget_ownership.py, run against a real
scratch Postgres: the held-elsewhere SQL must agree with the in-memory fake.
Same guard and environment as test_capture_identity_scratch.py."""
from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from tests import connect_scratch, validate_scratch_dsn
from tests.test_forget_ownership import (
    BASE, ForgetOwnershipTest, build_world, check_forget, md5, remaining_lines, run_forget,
    write_lines,
)

SCRATCH_DSN = os.environ.get("KHIPU_SCRATCH_DSN")

if SCRATCH_DSN and os.environ.get("KHIPU_LIVE_TESTS") == "1":
    validate_scratch_dsn(SCRATCH_DSN)

pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN or os.environ.get("KHIPU_LIVE_TESTS") != "1",
    reason="requires KHIPU_LIVE_TESTS=1 and a provisioned nonce-scoped KHIPU_SCRATCH_DSN",
)

VERSION = "0027_episode_sources"


def _direct_connect():
    return connect_scratch(SCRATCH_DSN)


@pytest.fixture(autouse=True)
def _ensure_migration_applied():
    from khipu import db as _db, migrate

    sql = next(p for v, p in migrate.available() if v == VERSION).read_text(encoding="utf-8")
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    _db._TABLE_COLUMNS_CACHE.clear()
    yield
    _db._TABLE_COLUMNS_CACHE.clear()


def _truncate():
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE episodes CASCADE")
        conn.commit()


def realizable(world: dict) -> bool:
    """Whether the database could hold this world: two live rows never share
    (ts, summary md5), and a source key names one row (the first wins, as the
    writer's conflict rule does, so the world is trimmed to match)."""
    live = [(ep["ts"].replace(microsecond=0), md5(ep["summary"]))
            for ep in world["episodes"].values()]
    seen: set[str] = set()
    kept = []
    for row in world["sources"]:
        if row["source_key"] not in seen:
            seen.add(row["source_key"])
            kept.append(row)
    world["sources"] = kept
    return len(set(live)) == len(live)


def seed(world: dict) -> None:
    """Write a world's episodes and source rows into the scratch database."""
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            for ep in world["episodes"].values():
                cur.execute(
                    "INSERT INTO episodes (id, ts, session_id, summary, raw, deleted_at) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, CASE WHEN %s THEN now() END)",
                    (ep["id"], ep["ts"], ep["session_id"], ep["summary"],
                     json.dumps(ep["raw"]), ep["deleted"]))
            for row in world["sources"]:
                cur.execute(
                    "INSERT INTO episode_sources (source_key, episode_id, role, ts, "
                    "summary_md5, payload_sha256) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (source_key) DO NOTHING",
                    (row["source_key"], row["episode_id"], row["role"], row["ts"],
                     row["summary_md5"], row["payload_sha256"]))
        conn.commit()


def refresh_deleted(world: dict) -> None:
    """Read each episode's deleted state back, as the next judgment needs it."""
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, deleted_at IS NOT NULL FROM episodes")
            for eid, deleted in cur.fetchall():
                world["episodes"][eid]["deleted"] = bool(deleted)


class ForgetOwnershipScratchTest(unittest.TestCase):
    def setUp(self):
        _truncate()
        self.addCleanup(_truncate)
        for p in (mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN}),
                  mock.patch("khipu.config.path_setting", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scenarios = ForgetOwnershipTest()
        self.scenarios.root = self.root
        self.connect = lambda *a, **k: _direct_connect()

    def _payload(self, ts, summary, session):
        return self.scenarios._payload(ts, summary, session)

    def _source(self, eid, p):
        return self.scenarios._source(eid, p)

    def test_a_live_identity_another_episodes_source_holds_is_kept(self):
        a = self._payload("2026-10-01T09:00:00Z", "A original", "codex:A")
        b = self._payload("2026-10-01T09:00:00Z", "B original", "codex:B")
        world = {"episodes": {
            1: {"id": 1, "ts": BASE, "summary": "B original", "session_id": "codex:A",
                "raw": dict(a), "deleted": False},
            2: {"id": 2, "ts": BASE, "summary": "B edited", "session_id": "codex:B",
                "raw": dict(b), "deleted": False}},
            "sources": [self._source(1, a), self._source(2, b)], "lines": [a, b],
            "sources_enabled": True}
        seed(world)
        write_lines(self.root, [a, b])
        out = run_forget(world, 1, self.root, connect=self.connect)
        left = [json.loads(ln)["summary"] for ln in remaining_lines(self.root)]
        self.assertIn("B original", left)
        self.assertNotIn("A original", left)
        self.assertFalse(out["complete"])
        self.assertIn("legacy_shared", out["partial"])

    def test_a_line_another_episode_holds_only_by_payload_hash_is_kept(self):
        for via in ("source", "receipt"):
            with self.subTest(via=via), tempfile.TemporaryDirectory() as td:
                _truncate()
                root = Path(td)
                world, a = self.scenarios.hash_only_world(via=via)
                seed(world)
                write_lines(root, world["lines"])
                out = run_forget(world, 1, root, connect=self.connect)
                left = [json.loads(ln) for ln in remaining_lines(root)]
                self.assertIn(a, left)
                self.assertFalse(out["complete"])
                self.assertIn("legacy_shared", out["partial"])

    def test_a_line_minted_with_an_offset_is_removed(self):
        c = self._payload("2026-10-01T11:00:00+02:00", "offset captured evidence", "codex:A")
        world = {"episodes": {1: {"id": 1, "ts": BASE, "summary": c["summary"],
                                  "session_id": "codex:A", "raw": dict(c), "deleted": False}},
                 "sources": [self._source(1, c)], "lines": [c], "sources_enabled": True}
        seed(world)
        write_lines(self.root, [c])
        out = run_forget(world, 1, self.root, connect=self.connect)
        self.assertEqual(remaining_lines(self.root), [])
        self.assertTrue(out["complete"], out.get("partial_errors"))

    def test_property_forget_never_takes_a_held_line_or_overclaims(self):
        skipped = 0
        for seed_no in range(60):
            rng = random.Random(seed_no)
            enabled = rng.random() < 0.8
            world = build_world(rng, sources_enabled=enabled)
            if not realizable(world):
                skipped += 1
                continue
            _truncate()
            seed(world)
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                write_lines(root, world["lines"])
                order = [e for e, ep in world["episodes"].items() if not ep["deleted"]]
                rng.shuffle(order)
                if not order:  # every episode already forgotten
                    continue
                for eid in order[:rng.randint(1, len(order))]:
                    before = (root / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
                    frozen = {**world, "episodes": {k: dict(v) for k, v in
                                                    world["episodes"].items()}}
                    if enabled:
                        out = run_forget(world, eid, root, connect=self.connect)
                    else:
                        with mock.patch("khipu.capture_identity.sources_ready",
                                        return_value=False):
                            out = run_forget(world, eid, root, connect=self.connect)
                    check_forget(self.scenarios, frozen, eid, root, out, before, (seed_no, eid))
                    refresh_deleted(world)
        self.assertLess(skipped, 30, "too few worlds the database could hold")


if __name__ == "__main__":
    unittest.main()
