"""Tests for khipu.capture + khipu.config — P3 step 2 (dual-write entrypoint).

Pure tests cover config precedence, payload validation, ts minting, and mode
routing (capture_v2 and PG are stubbed). One live test performs a real
hub-mode write against Postgres with an unmistakable probe summary and deletes
it afterwards; it skips when PG is unreachable.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import os
import tempfile
import unittest
from unittest import mock

from khipu import capture as cap
from khipu import config as cfg


def _pg_available() -> bool:
    try:
        from khipu.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        return False
    return True


PG_AVAILABLE = _pg_available()


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khipu-cfg-")
        self._env = dict(os.environ)
        os.environ["KHIPU_DATA_DIR"] = self.tmp
        os.environ.pop("KHIPU_CAPTURE_MODE", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_default_is_dual(self):
        self.assertEqual(cfg.capture_mode(), "dual")

    def test_set_persists_and_reads_back(self):
        path = cfg.set_capture_mode("hub")
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text())["capture_mode"], "hub")
        self.assertEqual(cfg.capture_mode(), "hub")

    def test_env_overrides_file(self):
        cfg.set_capture_mode("hub")
        os.environ["KHIPU_CAPTURE_MODE"] = "legacy"
        self.assertEqual(cfg.capture_mode(), "legacy")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            cfg.set_capture_mode("yolo")

    def test_garbage_file_falls_back(self):
        cfg.config_file().parent.mkdir(parents=True, exist_ok=True)
        cfg.config_file().write_text("{not json")
        self.assertEqual(cfg.capture_mode(), "dual")


class LoadPayloadTest(unittest.TestCase):
    def test_empty_is_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cap.load_payload("")
        self.assertEqual(ctx.exception.code, cap.EX_USAGE)

    def test_bad_json_is_data_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cap.load_payload("{nope")
        self.assertEqual(ctx.exception.code, cap.EX_DATAERR)

    def test_missing_summary_is_data_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cap.load_payload(json.dumps({"topics": ["x"]}))
        self.assertEqual(ctx.exception.code, cap.EX_DATAERR)

    def test_ts_minted_once_seconds_z(self):
        p = cap.load_payload(json.dumps({"summary": "hi"}))
        self.assertRegex(p["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_existing_ts_preserved(self):
        p = cap.load_payload(json.dumps({"summary": "hi", "ts": "2026-01-01T00:00:00Z"}))
        self.assertEqual(p["ts"], "2026-01-01T00:00:00Z")


class RoutingTest(unittest.TestCase):
    """capture() routing with both legs stubbed — asserts WHO gets called and
    whether the mirror-suppression flag is set. Contract since 2026-08-17
    (outbox): a PG failure is queued, the file leg still runs, and the call
    returns 0 because the capture is durable in two places."""

    def setUp(self):
        import tempfile
        self.calls: list[tuple[str, dict]] = []
        self.p_pg = mock.patch.object(
            cap, "write_pg",
            side_effect=lambda payload: self.calls.append(("pg", {})) or {"episode_inserted": True, "topics_written": 0},
        )
        self.p_v2 = mock.patch.object(
            cap, "run_capture_v2",
            side_effect=lambda payload, *, suppress_mirror: self.calls.append(("v2", {"suppress": suppress_mirror})) or 0,
        )
        self.p_pg.start()
        self.p_v2.start()
        # Never let a routing test touch the real outbox directory.
        self.outbox_dir = tempfile.mkdtemp(prefix="khipu-routing-outbox-")
        self.p_env = mock.patch.dict(os.environ, {"KHIPU_OUTBOX": self.outbox_dir})
        self.p_env.start()

    def tearDown(self):
        self.p_pg.stop()
        self.p_v2.stop()
        self.p_env.stop()

    def _pending(self) -> int:
        from khipu.outbox import status
        return status()["pending"]

    def test_trivial_skips_everything(self):
        self.assertEqual(cap.capture({"summary": "x", "scope": "trivial"}, mode="dual"), 0)
        self.assertEqual(self.calls, [])

    def test_legacy_only_v2_mirror_allowed(self):
        cap.capture({"summary": "x"}, mode="legacy")
        self.assertEqual(self.calls, [("v2", {"suppress": False})])

    def test_dual_pg_first_then_v2_with_mirror_suppressed(self):
        cap.capture({"summary": "x"}, mode="dual")
        self.assertEqual(self.calls, [("pg", {}), ("v2", {"suppress": True})])

    def test_hub_pg_first_then_reverse_mirror_to_file(self):
        # hub = PG is the record; the file is its reverse mirror (plan wording),
        # so the legacy consumers keep working. Off only via KHIPU_HUB_FILE_MIRROR=0.
        cap.capture({"summary": "x"}, mode="hub")
        self.assertEqual(self.calls, [("pg", {}), ("v2", {"suppress": True})])
        self.calls.clear()
        with mock.patch.dict(os.environ, {"KHIPU_HUB_FILE_MIRROR": "0"}):
            cap.capture({"summary": "x"}, mode="hub")
        self.assertEqual(self.calls, [("pg", {})])

    def test_pg_failure_is_queued_file_still_written_and_returns_zero(self):
        for mode in ("dual", "hub"):
            self.calls.clear()
            self.p_pg.stop()
            with mock.patch.object(cap, "write_pg", side_effect=RuntimeError("pg down")):
                rc = cap.capture({"summary": "x", "ts": "2026-08-17T18:00:00Z"}, mode=mode)
            self.p_pg.start()
            self.assertEqual(rc, 0, mode)                                   # durable: outbox + file
            self.assertEqual(self.calls, [("v2", {"suppress": False})], mode)  # file leg, own mirror ON
        self.assertEqual(self._pending(), 1)                                # same identity -> one job

    def test_pg_failure_with_unwritable_outbox_is_a_real_exit(self):
        self.p_pg.stop()
        with mock.patch.object(cap, "write_pg", side_effect=RuntimeError("pg down")), \
                mock.patch("khipu.outbox.enqueue", side_effect=OSError("disk full")):
            rc = cap.capture({"summary": "x"}, mode="hub")
        self.p_pg.start()
        self.assertEqual(rc, cap.EX_SOFTWARE)                               # nowhere durable but the file
        self.assertEqual(self.calls, [("v2", {"suppress": False})])


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live capture round-trip")
class LiveHubWriteTest(unittest.TestCase):
    """Real PG write in hub mode with a probe row, verified by identity, then deleted."""

    def test_hub_write_lands_and_is_idempotent(self):
        from khipu.db import connect

        summary = f"khipu-test-probe {os.getpid()} — safe to delete"
        payload = cap.load_payload(json.dumps({"summary": summary, "scope": "general"}))
        md = hashlib.md5(summary.encode()).hexdigest()
        # hub now reverse-mirrors to the legacy file (2026-08-17); a test must
        # never write a probe line into the real episodes.jsonl, so stub the leg.
        file_leg = mock.patch.object(cap, "run_capture_v2", lambda p, suppress_mirror: 0)
        file_leg.start()
        self.addCleanup(file_leg.stop)
        try:
            self.assertEqual(cap.capture(payload, mode="hub"), 0)
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s",
                    (payload["ts"], md),
                )
                self.assertEqual(cur.fetchone()[0], 1)
            # Same identity again → no second row (uq index + ON CONFLICT DO NOTHING).
            self.assertEqual(cap.capture(payload, mode="hub"), 0)
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM episodes WHERE md5(summary) = %s", (md,))
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            with connect() as conn, conn.cursor() as cur:
                # Vectors ride the capture now (P3 step 3): remove the probe's
                # embedding too, or coverage over-reports until the next sweep.
                cur.execute(
                    "DELETE FROM memory_embeddings WHERE kind='episode' AND ref IN "
                    "(SELECT id::text FROM episodes WHERE md5(summary) = %s)",
                    (md,),
                )
                cur.execute("DELETE FROM episodes WHERE md5(summary) = %s", (md,))
                conn.commit()



class PayloadRobustnessTest(unittest.TestCase):
    """Regressions from the 2026-08-17 audit: a malformed payload must exit with
    the documented code, and a hung file leg must not throw out of the hook."""

    def test_non_string_summary_exits_dataerr_instead_of_crashing(self):
        for bad in (123, ["a"], {"x": 1}, None):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as e:
                cap.load_payload(json.dumps({"summary": bad, "ts": "2026-08-17T18:00:00Z"}))
            self.assertEqual(e.exception.code, cap.EX_DATAERR)

    def test_non_string_session_id_is_ignored_not_fatal(self):
        out = cap.load_payload(json.dumps({"summary": "real", "session_id": 42}))
        self.assertEqual(out["summary"], "real")

    def test_blank_input_and_bad_json_keep_their_documented_codes(self):
        with self.assertRaises(SystemExit) as e:
            cap.load_payload("   ")
        self.assertEqual(e.exception.code, cap.EX_USAGE)
        with self.assertRaises(SystemExit) as e:
            cap.load_payload("{not json")
        self.assertEqual(e.exception.code, cap.EX_DATAERR)

    def test_a_hung_capture_v2_returns_a_code_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            fake = pathlib.Path(td) / "capture_v2.py"
            fake.write_text("import time\ntime.sleep(30)\n")
            with mock.patch.object(cap, "_capture_v2", return_value=fake), \
                 mock.patch("subprocess.run",
                            side_effect=subprocess.TimeoutExpired(cmd="capture_v2", timeout=300)):
                rc = cap.run_capture_v2({"summary": "s"}, suppress_mirror=True)
        self.assertEqual(rc, cap.EX_SOFTWARE)

    def test_an_unrunnable_capture_v2_returns_a_code_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            fake = pathlib.Path(td) / "capture_v2.py"
            fake.write_text("x")
            with mock.patch.object(cap, "_capture_v2", return_value=fake), \
                 mock.patch("subprocess.run", side_effect=OSError("exec format error")):
                rc = cap.run_capture_v2({"summary": "s"}, suppress_mirror=True)
        self.assertEqual(rc, cap.EX_SOFTWARE)

class _EpisodesFakeCursor:
    """Enough of a Postgres cursor to exercise write_pg's own orchestration
    (dedup routing, topic classification wiring, commitments wiring) without
    a live database. Answers only the SQL shapes write_pg's episode-table
    calls issue; every jsonb column round-trips as native Python (list/dict),
    matching psycopg's automatic jsonb adapter."""

    def __init__(self, episodes: dict[int, dict] | None = None, *, pre_migration: bool = False):
        self.episodes = episodes or {}
        self._existing_keys = {
            (e["ts"], hashlib.md5(e["summary"].encode()).hexdigest())
            for e in self.episodes.values()
        }
        self.next_id = (max(self.episodes) + 1) if self.episodes else 1
        self.rowcount = 0
        self._result: list[tuple] = []
        self.last_inserted_id: int | None = None
        self.updated_ids: list[int] = []
        # Fully-migrated hub by default (0008+0009+0010 all applied) so
        # existing tests that mock hygiene.classify_topics directly keep
        # exercising it. pre_migration=True simulates topic_aliases /
        # episodes.tags not existing yet (HygieneSavepointGateTest).
        self.pre_migration = pre_migration
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        self.calls.append((s, params))
        if s.startswith("SAVEPOINT") or s.startswith("ROLLBACK TO SAVEPOINT") \
                or s.startswith("RELEASE SAVEPOINT"):
            return
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            (table,) = params
            if self.pre_migration:
                self._result = [(c,) for c in (
                    "id", "ts", "session_id", "summary", "topics", "people",
                    "decisions", "preferences", "scope", "edges", "raw",
                )] if table == "episodes" else []
            elif table == "episodes":
                self._result = [(c,) for c in (
                    "id", "ts", "session_id", "summary", "topics", "people",
                    "decisions", "preferences", "scope", "edges", "raw",
                    "harness", "repo_root", "project", "parent_session_id",
                    "transcript_range", "tags", "deleted_at",
                )]
            elif table == "topic_aliases":
                self._result = [("alias",), ("slug",)]
            else:
                self._result = []
            return
        if s.startswith("SELECT id FROM episodes WHERE harness"):
            harness, sid, tr = params
            hits = [eid for eid, e in self.episodes.items()
                    if e.get("harness") == harness and e.get("session_id") == sid
                    and e.get("transcript_range") == tr]
            self._result = [(hits[0],)] if hits else []
            return
        if s.startswith("SELECT id, summary, decisions, preferences, topics FROM episodes"):
            val = params[0]
            group_col = "parent_session_id" if "parent_session_id = %s" in s else "project"
            self._result = [
                (eid, e["summary"], e["decisions"], e["preferences"], e["topics"])
                for eid, e in self.episodes.items() if e.get(group_col) == val
            ]
            return
        if s.startswith("SELECT project, repo_root FROM episodes"):
            parent = params[0]
            hits = [
                (e.get("project"), e.get("repo_root"))
                for e in sorted(self.episodes.values(), key=lambda e: e["ts"], reverse=True)
                if e.get("project")
                and (e.get("parent_session_id") == parent or e.get("session_id") == parent)
            ]
            self._result = [hits[0]] if hits else []
            return
        if s.startswith("INSERT INTO episodes"):
            # Column list is schema-gated now (mirror._upsert_episode names
            # identity/tags columns only when they exist), so read it off the
            # SQL instead of unpacking a fixed 16-tuple.
            names = [c.strip() for c in
                     s.split("INSERT INTO episodes (", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(names, params))
            ts, session_id = row["ts"], row["session_id"]
            summary, scope = row["summary"], row.get("scope")
            topics_json, people_json = row["topics"], row["people"]
            decisions_json, preferences_json = row["decisions"], row["preferences"]
            raw_json = row["raw"]
            harness, repo_root = row.get("harness"), row.get("repo_root")
            project = row.get("project")
            parent_session_id = row.get("parent_session_id")
            transcript_range = row.get("transcript_range")
            tags_json = row.get("tags") or "[]"
            key = (ts, hashlib.md5(summary.encode()).hexdigest())
            if key in self._existing_keys:
                self.rowcount = 0
                return
            eid = self.next_id
            self.next_id += 1
            self.episodes[eid] = {
                "ts": ts, "session_id": session_id, "summary": summary,
                "topics": json.loads(topics_json), "decisions": json.loads(decisions_json),
                "preferences": json.loads(preferences_json), "raw": json.loads(raw_json),
                "harness": harness, "repo_root": repo_root, "project": project,
                "parent_session_id": parent_session_id, "transcript_range": transcript_range,
                "tags": json.loads(tags_json),
            }
            self._existing_keys.add(key)
            self.rowcount = 1
            self.last_inserted_id = eid
            return
        if s.startswith("SELECT id FROM episodes WHERE ts"):
            ts, md = params
            hits = [eid for eid, e in self.episodes.items()
                    if e["ts"] == ts and hashlib.md5(e["summary"].encode()).hexdigest() == md]
            self._result = [(hits[0],)] if hits else []
            return
        if s.startswith("SELECT topics, decisions, preferences, people, raw, ts, summary"):
            (target_id,) = params
            e = self.episodes.get(target_id)
            if not e:
                self._result = []
                return
            row = [e["topics"], e["decisions"], e["preferences"], e.get("people") or [],
                   e["raw"], e["ts"], e["summary"]]
            if ", tags" in s:
                row.append(e.get("tags") or [])
            self._result = [tuple(row)]
            return
        if s.startswith("UPDATE episodes SET topics"):
            target_id = params[-1]
            e = self.episodes[target_id]
            e["topics"] = json.loads(params[0])
            e["decisions"] = json.loads(params[1])
            e["preferences"] = json.loads(params[2])
            e["people"] = json.loads(params[3])
            e["raw"] = json.loads(params[4])
            if "tags = %s::jsonb" in s:
                e["tags"] = json.loads(params[5])
            self.updated_ids.append(target_id)
            return
        raise AssertionError(f"unexpected SQL in fake cursor: {s[:120]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class WritePgOrchestrationTest(unittest.TestCase):
    """write_pg's own routing: W1.4 dedup (skip/merge/none), W5.1 topic
    classification applied to the payload before insert, and W3.3
    commitments wired in with the real episode id after insert. persist_
    capture_graph and hygiene.classify_topics/commitments are the concerns
    of their own test modules — stubbed here so this stays focused on
    write_pg's orchestration."""

    def setUp(self):
        self.p_graph = mock.patch("khipu.topic_graph.persist_capture_graph", return_value={
            "nodes_minted": 0, "edges_minted": 0})
        self.p_graph.start()
        self.addCleanup(self.p_graph.stop)
        self.p_path = mock.patch("khipu.config.path_setting", return_value=None)
        self.p_path.start()
        self.addCleanup(self.p_path.stop)
        # Force the cosine leg to bail so dedup falls back to Jaccard deterministically.
        self.p_embed = mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key in test"))
        self.p_embed.start()
        self.addCleanup(self.p_embed.stop)

    def _connect(self, cur):
        return mock.patch("khipu.db.connect", return_value=_FakeConn(cur))

    def test_exact_window_duplicate_is_skipped_not_inserted(self):
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:abc", "summary": "first",
            "topics": [], "decisions": [], "preferences": [], "raw": {},
            "harness": "claude_code", "repo_root": "/r", "project": "acme/widget",
            "parent_session_id": None, "transcript_range": "0:100", "tags": [],
        }}
        cur = _EpisodesFakeCursor(existing)
        payload = {
            "ts": "2026-09-03T00:05:00Z", "session_id": "claude_code:abc",
            "summary": "same window retried", "harness": "claude_code",
            "transcript_range": "0:100", "project": "acme/widget",
        }
        with self._connect(cur), mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            stats = cap.write_pg(payload)
        self.assertFalse(stats["episode_inserted"])
        self.assertEqual(stats["dedup"]["action"], "skip")
        self.assertEqual(stats["dedup"]["matched_episode"], 1)
        self.assertEqual(len(cur.episodes), 1, "no second row for the same window")

    def test_similar_capture_in_window_merges_instead_of_inserting(self):
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:fb929043", "summary":
            "user experiencing slow performance from oracle processes running in cursor worktree",
            "topics": ["performance"], "decisions": ["Stop running two baseline oracles at once"],
            "preferences": [], "raw": {},
            "harness": "claude_code", "repo_root": "/r", "project": "acme/widget",
            "parent_session_id": None, "transcript_range": "0:100", "tags": [],
        }}
        cur = _EpisodesFakeCursor(existing)
        # Same summary (the actual signal); the decisions differ, as they do
        # for two real captures of one conversation from different windows.
        payload = {
            "ts": "2026-09-03T00:02:00Z", "session_id": "claude_code:c631b166",
            "summary": "user experiencing slow performance from oracle processes running in cursor worktree",
            "decisions": ["Recorded mobile followup task"],
            "harness": "claude_code", "transcript_range": "50:200", "project": "acme/widget",
        }
        with self._connect(cur), \
                mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)), \
                mock.patch.object(cap, "_dedup_text", side_effect=lambda p: p.get("summary") or ""):
            stats = cap.write_pg(payload)
        self.assertFalse(stats["episode_inserted"])
        self.assertEqual(stats["dedup"]["action"], "merge")
        self.assertEqual(stats["dedup"]["matched_via"], "jaccard")
        self.assertEqual(len(cur.episodes), 1, "merged, not a second row")
        merged = cur.episodes[1]
        self.assertIn("Recorded mobile followup task", merged["decisions"])
        self.assertEqual(len(merged["raw"]["merged_from"]), 1)
        self.assertEqual(merged["raw"]["merged_from"][0]["session_id"], "claude_code:c631b166")

    def test_a_merge_unions_people_and_tags_opens_commitments_and_reembeds(self):
        """Audit 2026-09-04: the merge folded topics/decisions/preferences and
        dropped everything else. ``people``/``tags`` from the merged capture
        vanished, its ``open_loops`` never became commitments (the insert path
        opens them), and the target row kept vectors built from its pre-merge
        text."""
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:fb929043",
            "summary": "one conversation captured twice",
            "topics": ["performance"], "decisions": [], "preferences": [],
            "people": ["matt"], "tags": ["recap-chip"], "raw": {},
            "harness": "claude_code", "repo_root": "/r", "project": "acme/widget",
            "parent_session_id": None, "transcript_range": "0:100",
        }}
        cur = _EpisodesFakeCursor(existing)
        payload = {
            "ts": "2026-09-03T00:02:00Z", "session_id": "claude_code:c631b166",
            "summary": "one conversation captured twice",
            "people": ["matt", "ana"], "tags": ["followup-chip"],
            "open_loops": [{"text": "ship the merge fix", "kind": "followup"}],
            "harness": "claude_code", "transcript_range": "50:200", "project": "acme/widget",
        }
        opened: list[tuple] = []
        reembedded: list[tuple] = []
        with self._connect(cur), \
                mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)), \
                mock.patch("khipu.commitments.open_from_episode",
                           side_effect=lambda c, pl, eid: opened.append((pl, eid)) or 1), \
                mock.patch("khipu.commitments.auto_close", return_value=0), \
                mock.patch.object(cap, "_reembed_merged_episode",
                                  side_effect=lambda ts, sm, m: reembedded.append((ts, sm, m))), \
                mock.patch.object(cap, "_dedup_text", side_effect=lambda p: p.get("summary") or ""):
            stats = cap.write_pg(payload)
        self.assertEqual(stats["dedup"]["action"], "merge")
        merged = cur.episodes[1]
        self.assertEqual(merged["people"], ["matt", "ana"])
        self.assertEqual(merged["tags"], ["recap-chip", "followup-chip"])
        # Commitments opened for the MERGED payload against the TARGET id.
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0][1], 1)
        self.assertEqual(opened[0][0]["open_loops"], payload["open_loops"])
        # And the target row is re-embedded from its post-merge text.
        self.assertEqual(len(reembedded), 1)
        self.assertEqual(reembedded[0][0], "2026-09-03T00:00:00Z")
        self.assertEqual(reembedded[0][2]["people"], ["matt", "ana"])

    def test_merge_reembed_looks_the_target_row_up_by_its_own_identity(self):
        """``embed_on_capture`` finds a row by (ts, md5(summary)); a merge
        changes neither, so the TARGET's ts/summary — not the merged
        capture's — are what must be handed to it."""
        seen: list[dict] = []
        with mock.patch("khipu.embed.embed_on_capture", side_effect=lambda p: seen.append(p)):
            cap._reembed_merged_episode(
                "2026-09-03T00:00:00Z", "target summary",
                {"summary": "ignored", "topics": ["t"], "decisions": ["d"],
                 "preferences": [], "people": []},
            )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["ts"], "2026-09-03T00:00:00Z")
        self.assertEqual(seen[0]["summary"], "target summary")
        self.assertEqual(seen[0]["decisions"], ["d"])

    def test_merge_reembed_never_raises_when_embedding_is_unavailable(self):
        with mock.patch("khipu.embed.embed_on_capture", side_effect=RuntimeError("no profile")):
            cap._reembed_merged_episode("2026-09-03T00:00:00Z", "s", {"summary": "s"})

    def test_decisions_folded_into_the_similarity_text_can_pull_below_threshold(self):
        """Real-world caveat, not a bug: the Jaccard fallback scores summary
        AND decisions together, so two captures of the SAME conversation with
        DIFFERENT decisions can legitimately fall under the 0.6 default even
        when the summary alone is identical — cosine (when available) is the
        one meant to catch this; Jaccard is the degraded fallback."""
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:fb929043", "summary":
            "user experiencing slow performance from oracle processes running in cursor worktree",
            "topics": [], "decisions": ["Stop running two baseline oracles at once"],
            "preferences": [], "raw": {},
            "harness": "claude_code", "repo_root": "/r", "project": "acme/widget",
            "parent_session_id": None, "transcript_range": "0:100", "tags": [],
        }}
        cur = _EpisodesFakeCursor(existing)
        payload = {
            "ts": "2026-09-03T00:02:00Z", "session_id": "claude_code:c631b166",
            "summary": "user experiencing slow performance from oracle processes running in cursor worktree",
            "decisions": ["Recorded mobile followup task"],
            "harness": "claude_code", "transcript_range": "50:200", "project": "acme/widget",
        }
        with self._connect(cur), mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"], "documents the real gap: no cosine key -> no merge here")
        self.assertEqual(stats["dedup"]["action"], "none")

    def test_unrelated_project_never_merges(self):
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "totally unrelated content here",
            "topics": [], "decisions": [], "preferences": [], "raw": {},
            "harness": "claude_code", "repo_root": "/r", "project": "acme/widget",
            "parent_session_id": None, "transcript_range": "0:1", "tags": [],
        }}
        cur = _EpisodesFakeCursor(existing)
        payload = {
            "ts": "2026-09-03T00:01:00Z", "session_id": "s2", "summary": "totally unrelated content here",
            "harness": "claude_code", "transcript_range": "0:1", "project": "other/repo",
        }
        with self._connect(cur), mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(stats["dedup"]["action"], "none")
        self.assertEqual(len(cur.episodes), 2)

    def test_topics_split_into_resolved_and_tags_before_insert(self):
        cur = _EpisodesFakeCursor()
        payload = {
            "ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "did a thing",
            "topics": ["real-topic", "dangling-slug"],
        }
        with self._connect(cur), mock.patch(
            "khipu.hygiene.classify_topics",
            return_value=(["real-topic"], ["dangling-slug"], False),
        ):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"])
        eid = stats["episode_id"]
        self.assertEqual(cur.episodes[eid]["topics"], ["real-topic"])
        self.assertEqual(cur.episodes[eid]["tags"], ["dangling-slug"])

    def test_commitments_wired_in_with_the_real_episode_id_after_insert(self):
        cur = _EpisodesFakeCursor()
        payload = {
            "ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "did a thing",
            "open_loops": [{"text": "follow up", "kind": "followup"}],
        }
        calls = []
        with self._connect(cur), \
                mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)), \
                mock.patch("khipu.commitments.open_from_episode",
                            side_effect=lambda c, p, eid: calls.append(("open", eid)) or 1), \
                mock.patch("khipu.commitments.auto_close",
                            side_effect=lambda c, p, eid: calls.append(("close", eid)) or 0):
            stats = cap.write_pg(payload)
        eid = stats["episode_id"]
        self.assertIsNotNone(eid)
        self.assertEqual(calls, [("open", eid), ("close", eid)])

    def test_commitments_failure_does_not_lose_the_episode(self):
        cur = _EpisodesFakeCursor()
        payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "did a thing"}
        with self._connect(cur), \
                mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)), \
                mock.patch("khipu.commitments.open_from_episode", side_effect=RuntimeError("boom")):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(len(cur.episodes), 1)


class HygieneSavepointGateTest(unittest.TestCase):
    """fix 2: hygiene.classify_topics is gated on topic_aliases + episodes.
    tags existing (migration 0010) and wrapped in its own SAVEPOINT, exactly
    like the topic-graph and commitments steps — a missing table must never
    abort the whole write."""

    def setUp(self):
        self.p_graph = mock.patch("khipu.topic_graph.persist_capture_graph", return_value={
            "nodes_minted": 0, "edges_minted": 0})
        self.p_graph.start()
        self.addCleanup(self.p_graph.stop)
        self.p_path = mock.patch("khipu.config.path_setting", return_value=None)
        self.p_path.start()
        self.addCleanup(self.p_path.stop)

    def _connect(self, cur):
        return mock.patch("khipu.db.connect", return_value=_FakeConn(cur))

    def test_pre_migration_hub_skips_classify_topics_entirely(self):
        """topic_aliases / episodes.tags not applied yet: classify_topics
        must never be called (calling it is exactly what used to raise and
        abort the transaction) — topics pass through unchanged, no tags."""
        cur = _EpisodesFakeCursor(pre_migration=True)
        payload = {
            "ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "did a thing",
            "topics": ["Some-Topic", "some-topic"],
        }
        with self._connect(cur), \
                mock.patch("khipu.hygiene.classify_topics") as m_classify:
            stats = cap.write_pg(payload)
        m_classify.assert_not_called()
        self.assertTrue(stats["episode_inserted"])
        eid = stats["episode_id"]
        self.assertEqual(cur.episodes[eid]["topics"], ["some-topic"])
        self.assertEqual(cur.episodes[eid]["tags"], [])

    def test_classify_topics_failure_rolls_back_to_savepoint_not_the_whole_write(self):
        """classify_topics raising must not lose the episode: capture.py
        takes its own SAVEPOINT, rolls back to it on failure, and still
        inserts the episode (topics passed through unchanged)."""
        cur = _EpisodesFakeCursor()
        payload = {
            "ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "did a thing",
            "topics": ["a-topic"],
        }
        with self._connect(cur), mock.patch(
            "khipu.hygiene.classify_topics", side_effect=RuntimeError("topic_aliases missing")
        ):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"])
        eid = stats["episode_id"]
        self.assertEqual(cur.episodes[eid]["topics"], ["a-topic"])
        statements = [s for s, _ in cur.calls]
        self.assertIn("SAVEPOINT capture_hygiene", statements)
        self.assertIn("ROLLBACK TO SAVEPOINT capture_hygiene", statements)
        self.assertNotIn("RELEASE SAVEPOINT capture_hygiene", statements)

    def test_classify_topics_reporting_unresolved_also_rolls_back(self):
        """classify_topics can swallow its own PG error and return
        topics_unresolved=True without raising — that still leaves PG's
        transaction aborted server-side, so capture.py must roll back to
        the savepoint even when no Python exception propagated."""
        cur = _EpisodesFakeCursor()
        payload = {
            "ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "did a thing",
            "topics": ["a-topic"],
        }
        with self._connect(cur), mock.patch(
            "khipu.hygiene.classify_topics", return_value=(["a-topic"], [], True)
        ):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"])
        statements = [s for s, _ in cur.calls]
        self.assertIn("ROLLBACK TO SAVEPOINT capture_hygiene", statements)
        self.assertNotIn("RELEASE SAVEPOINT capture_hygiene", statements)


class ProjectInheritanceAndDedupGroupingTest(unittest.TestCase):
    """Task 2 of the memory-reliability build: a scratchpad/`/tmp` cwd never
    resolves repo_root/project (identity.resolve_repo_root), so a dispatched
    child session needs to inherit project from lineage instead — and dedup
    needs to keep grouping candidates sensibly once project is (or is not)
    known. Three cases: inherited via parent_session_id match, dedup grouped
    by parent_session_id when project stays unknown, and neither known so
    only the exact-window skip applies."""

    def setUp(self):
        self.p_graph = mock.patch("khipu.topic_graph.persist_capture_graph", return_value={
            "nodes_minted": 0, "edges_minted": 0})
        self.p_graph.start()
        self.addCleanup(self.p_graph.stop)
        self.p_path = mock.patch("khipu.config.path_setting", return_value=None)
        self.p_path.start()
        self.addCleanup(self.p_path.stop)
        self.p_embed = mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key in test"))
        self.p_embed.start()
        self.addCleanup(self.p_embed.stop)

    def _connect(self, cur):
        return mock.patch("khipu.db.connect", return_value=_FakeConn(cur))

    def test_project_and_repo_root_inherited_from_parent_session_id_sibling(self):
        """A prior episode shares THIS payload's parent_session_id (a sibling
        dispatched from the same host) — its project/repo_root ride onto the
        new scratchpad-cwd episode."""
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:host-1",
            "summary": "host session work", "topics": [], "decisions": [], "preferences": [],
            "raw": {}, "harness": "claude_code", "repo_root": "/repo/khipu", "project": "acme/khipu",
            "parent_session_id": None, "transcript_range": "0:1", "tags": [],
        }}
        cur = _EpisodesFakeCursor(existing)
        payload = {
            "ts": "2026-09-03T00:05:00Z", "session_id": "claude_code:child-1",
            "summary": "dispatched child work from a scratchpad cwd", "harness": "claude_code",
            "transcript_range": "0:1", "project": None, "repo_root": None,
            "parent_session_id": "claude_code:host-1",
        }
        with self._connect(cur), mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(payload["project"], "acme/khipu")
        self.assertEqual(payload["repo_root"], "/repo/khipu")
        eid = stats["episode_id"]
        self.assertEqual(cur.episodes[eid]["project"], "acme/khipu")

    def test_project_inherited_when_this_payloads_parent_id_is_the_parent_episodes_own_session_id(self):
        """The parent episode itself (not a sibling) is the match: its
        session_id equals this payload's parent_session_id."""
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:host-2",
            "summary": "the host conversation", "topics": [], "decisions": [], "preferences": [],
            "raw": {}, "harness": "claude_code", "repo_root": "/repo/khipu", "project": "acme/khipu",
            "parent_session_id": None, "transcript_range": "0:1", "tags": [],
        }}
        cur = _EpisodesFakeCursor(existing)
        payload = {
            "ts": "2026-09-03T00:05:00Z", "session_id": "claude_code:child-2",
            "summary": "another dispatched child", "harness": "claude_code",
            "transcript_range": "0:1", "project": None, "repo_root": None,
            "parent_session_id": "claude_code:host-2",
        }
        with self._connect(cur), mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            cap.write_pg(payload)
        self.assertEqual(payload["project"], "acme/khipu")

    def test_inheritance_skipped_when_no_match_in_the_last_24h(self):
        """dedup_candidates SQL bounds the window server-side (untestable
        against the fake cursor's flat dict); this documents the
        fallback-shape half — no matching parent/session row at all still
        leaves project/repo_root None rather than raising."""
        cur = _EpisodesFakeCursor()
        payload = {
            "ts": "2026-09-03T00:05:00Z", "session_id": "claude_code:child-3",
            "summary": "orphaned dispatched child, no lineage match", "harness": "claude_code",
            "transcript_range": "0:1", "project": None, "repo_root": None,
            "parent_session_id": "claude_code:nobody-knows-this-host",
        }
        with self._connect(cur), mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"])
        self.assertIsNone(payload["project"])
        self.assertIsNone(payload["repo_root"])

    def test_dedup_groups_by_parent_session_id_when_project_stays_unknown(self):
        """No project ever resolves for either row (both scratchpad-cwd
        children with no lineage match to inherit from) — dedup still finds
        the similar prior capture by grouping on their shared
        parent_session_id instead of leaving dedup blind."""
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:child-a",
            "summary": "same scratchpad conversation captured twice",
            "topics": [], "decisions": [], "preferences": [], "raw": {},
            "harness": "claude_code", "repo_root": None, "project": None,
            "parent_session_id": "claude_code:orphan-host", "transcript_range": "0:100", "tags": [],
        }}
        cur = _EpisodesFakeCursor(existing)
        payload = {
            "ts": "2026-09-03T00:02:00Z", "session_id": "claude_code:child-b",
            "summary": "same scratchpad conversation captured twice",
            "harness": "claude_code", "transcript_range": "100:200",
            "parent_session_id": "claude_code:orphan-host",
        }
        with self._connect(cur), \
                mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)), \
                mock.patch.object(cap, "_dedup_text", side_effect=lambda p: p.get("summary") or ""):
            stats = cap.write_pg(payload)
        self.assertEqual(stats["dedup"]["action"], "merge")
        self.assertEqual(stats["dedup"]["matched_episode"], 1)
        self.assertEqual(len(cur.episodes), 1)

    def test_dedup_candidates_empty_when_neither_project_nor_parent_known(self):
        """Unit-level: with nothing to group on, the similarity leg must
        return no candidates at all rather than scanning ungrouped (which
        would risk matching two unrelated captures) — this is what makes
        `dedup_before_insert` fall through to 'none' below."""
        cur = _EpisodesFakeCursor({1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:solo-1",
            "summary": "isolated capture with no lineage or project at all",
            "topics": [], "decisions": [], "preferences": [], "raw": {},
            "harness": "claude_code", "repo_root": None, "project": None,
            "parent_session_id": None, "transcript_range": "0:1", "tags": [],
        }})
        payload = {
            "ts": "2026-09-03T00:00:05Z", "session_id": "claude_code:solo-2",
            "summary": "isolated capture with no lineage or project at all",
            "harness": "claude_code", "transcript_range": "1:2",
            "project": None, "parent_session_id": None,
        }
        self.assertEqual(cap._dedup_candidates(cur, payload), [])

    def test_neither_project_nor_parent_session_id_only_exact_window_skip_applies(self):
        """End-to-end: no project, no lineage at all — the similarity leg
        finds nothing to group on, so the row inserts as a new episode
        (action 'none'); only the exact (harness, session_id,
        transcript_range) skip could still have fired, and it does not here
        because the transcript_range differs."""
        existing = {1: {
            "ts": "2026-09-03T00:00:00Z", "session_id": "claude_code:solo-1",
            "summary": "isolated capture with no lineage or project at all",
            "topics": [], "decisions": [], "preferences": [], "raw": {},
            "harness": "claude_code", "repo_root": None, "project": None,
            "parent_session_id": None, "transcript_range": "0:1", "tags": [],
        }}
        cur = _EpisodesFakeCursor(existing)
        payload = {
            "ts": "2026-09-03T00:00:05Z", "session_id": "claude_code:solo-2",
            "summary": "isolated capture with no lineage or project at all",
            "harness": "claude_code", "transcript_range": "1:2",
        }
        with self._connect(cur), mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)):
            stats = cap.write_pg(payload)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(stats["dedup"]["action"], "none")
        self.assertEqual(len(cur.episodes), 2)


if __name__ == "__main__":
    unittest.main()
