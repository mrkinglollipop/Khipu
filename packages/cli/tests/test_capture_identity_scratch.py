"""Capture identity phase 2 against a real scratch Postgres: the two gaps the
audit named (merge-then-reconcile must not resurrect a merged line; forget after
an edit must erase every legacy line) and, for each, the counterpart with the
episode_sources probe forced false, which documents the pre-0027 behaviour.
Same guard and environment as test_pg_scratch.py."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from tests import connect_scratch, validate_scratch_dsn

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


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _line(payload: dict) -> str:
    return json.dumps({"ts": payload["ts"], "summary": payload["summary"]}) + "\n"


class MergedEpisodeScratchTest(unittest.TestCase):
    PROJECT = "acme/scratch-identity"

    def setUp(self):
        self._truncate()
        for p in (mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN}),
                  mock.patch("khipu.db.connect", side_effect=lambda *a, **k: _direct_connect()),
                  mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}),
                  mock.patch("khipu.config.path_setting", return_value=None),
                  # No embedding: the Jaccard dedup path alone decides the merge.
                  mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key"))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._truncate)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "memory"
        self.root.mkdir()
        self.a, self.b, self.a_id = self._write_a_then_merged_b()

    def _truncate(self):
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE episodes CASCADE")
            conn.commit()

    def _write_a_then_merged_b(self):
        from khipu import capture

        base = ("Rebuilt the scratch widget exporter so the nightly report keeps every column "
                "and the retry loop stops after three attempts on the staging queue")
        n1, n2 = os.urandom(4).hex(), os.urandom(4).hex()
        a = {"ts": "2026-10-11T12:00:00Z", "summary": base, "project": self.PROJECT,
             "harness": "claude_code", "session_id": f"claude_code:{n1}",
             "transcript_range": "0:100", "window_id": f"w{n1}", "part": "1/1"}
        b = {"ts": "2026-10-11T12:02:00Z", "summary": base + " today", "project": self.PROJECT,
             "harness": "codex", "session_id": f"codex:{n2}",
             "transcript_range": "0:50", "window_id": f"w{n2}", "part": "1/1"}
        keep_a = {"ts": a["ts"], "summary": a["summary"]}
        keep_b = {"ts": b["ts"], "summary": b["summary"]}
        sa = capture.write_pg(dict(a))
        self.assertTrue(sa["episode_inserted"])
        sb = capture.write_pg(dict(b))
        self.assertEqual(sb["dedup"]["action"], "merge")
        self.assertEqual(sb["episode_id"], sa["episode_id"])
        return keep_a, keep_b, int(sa["episode_id"])

    def _count(self) -> int:
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM episodes WHERE deleted_at IS NULL")
                return int(cur.fetchone()[0])

    def _keys(self, *payloads):
        return [(p["ts"], _md5(p["summary"])) for p in payloads]

    def _missing(self, *payloads):
        from khipu.drift import episodes_missing_in_pg

        with _direct_connect() as conn:
            with conn.cursor() as cur:
                return episodes_missing_in_pg(cur, self._keys(*payloads))

    def _write_file(self, *lines):
        (self.root / "episodes.jsonl").write_text("".join(lines), encoding="utf-8")

    # 1. merge-then-reconcile does not resurrect

    def test_reconcile_and_tail_sync_do_not_resurrect_a_merged_line(self):
        from khipu import mirror

        self._write_file(_line(self.a), _line(self.b))
        before = self._count()
        self.assertEqual(before, 1)
        self.assertEqual(self._missing(self.a, self.b), [])
        self.assertEqual(mirror.reconcile_memory_root(self.root)["episodes_new"], 0)
        self.assertEqual(self._count(), before)
        self.assertEqual(mirror.sync_recent_episodes(self.root)["inserted"], 0)
        self.assertEqual(self._count(), before)

    def test_merged_line_is_reported_missing_when_the_probe_is_false(self):
        self._write_file(_line(self.a), _line(self.b))
        with mock.patch("khipu.capture_identity.sources_ready", return_value=False):
            missing = self._missing(self.a, self.b)
        # The merge rewrote the live summary, so the old anti-join no longer
        # recognises B's line (ARCH-10); reconcile would re-insert it.
        self.assertIn(self._keys(self.b)[0], missing)

    # 2. forget after an edit erases both legacy lines

    def _edit_a_and_write_file(self):
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE episodes SET summary = %s WHERE id = %s",
                            ("An operator rewrote this summary entirely", self.a_id))
            conn.commit()
        other = {"ts": "2026-10-11T09:00:00Z", "summary": "An unrelated line that must stay"}
        self._write_file(_line(self.a), _line(self.b), _line(other))
        return other

    def _forget(self):
        from khipu import forget, hub_snapshot

        outbox = Path(self.tmp.name) / "outbox"
        with mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(outbox)}), \
                mock.patch.object(hub_snapshot, "snapshot_path",
                                  return_value=Path(self.tmp.name) / "hub_snapshot.sqlite"):
            return forget.forget_everywhere(self.a_id, memory_root=self.root)

    def _file_summaries(self):
        text = (self.root / "episodes.jsonl").read_text(encoding="utf-8")
        return [json.loads(ln)["summary"] for ln in text.splitlines() if ln.strip()]

    def test_forget_after_an_edit_erases_both_legacy_lines(self):
        other = self._edit_a_and_write_file()
        out = self._forget()
        self.assertTrue(out["complete"], out.get("partial_errors"))
        self.assertEqual(self._file_summaries(), [other["summary"]])

    def test_forget_after_an_edit_leaves_the_merged_line_when_the_probe_is_false(self):
        other = self._edit_a_and_write_file()
        with mock.patch("khipu.capture_identity.sources_ready", return_value=False):
            self._forget()
        left = self._file_summaries()
        self.assertIn(self.b["summary"], left)
        self.assertIn(other["summary"], left)


if __name__ == "__main__":
    unittest.main()
