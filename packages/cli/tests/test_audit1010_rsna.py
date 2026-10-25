"""Hermetic lifecycle regressions, using SQL against an in-memory registry."""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from khipu import commitments as co, decisions as de, validity


class _RegistryCursor:
    """Execute registry SQL in SQLite; translate only PostgreSQL syntax."""

    def __init__(self, *, modern=True):
        self.conn = sqlite3.connect(":memory:")
        self.conn.create_function("regexp_replace", 4, lambda s, p, r, flags: re.sub(p, r, s))
        self.conn.create_function("greatest", -1, lambda *xs: max(x for x in xs if x is not None))
        self.conn.executescript("""
            CREATE TABLE project_aliases (alias TEXT PRIMARY KEY, project TEXT NOT NULL);
            CREATE TABLE episodes (
                id INTEGER PRIMARY KEY, session_id TEXT, parent_session_id TEXT,
                deleted_at TEXT, ts TEXT, ingested_at TEXT, summary TEXT,
                decisions TEXT, project TEXT
            );
            CREATE TABLE commitments (
                id INTEGER PRIMARY KEY, text TEXT, project TEXT, owner TEXT,
                kind TEXT DEFAULT 'followup', opened_episode INTEGER,
                opened_at TEXT DEFAULT CURRENT_TIMESTAMP, due_after TEXT,
                status TEXT DEFAULT 'open', closed_episode INTEGER, closed_at TEXT,
                close_reason TEXT, content_hash TEXT, last_seen_at TEXT,
                seen_count INTEGER DEFAULT 1, future_trigger INTEGER DEFAULT 0,
                trigger_text TEXT
            );
            CREATE UNIQUE INDEX uq_commitments_open_content
                ON commitments(project, content_hash) WHERE status = 'open';
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY, project TEXT, text TEXT, rationale TEXT,
                decided_at TEXT DEFAULT CURRENT_TIMESTAMP, episode_id INTEGER,
                session_id TEXT, superseded_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, source_kind TEXT,
                evidence TEXT, superseded_at TEXT, supersede_source TEXT,
                supersede_reason TEXT, retracted_at TEXT, retract_reason TEXT
            );
            CREATE TABLE decision_links (
                id INTEGER PRIMARY KEY, old_id INTEGER, new_id INTEGER, kind TEXT,
                confidence REAL, source TEXT, reason TEXT, state TEXT DEFAULT 'candidate',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT,
                UNIQUE(old_id, new_id, kind)
            );
        """)
        if not modern:
            for column in ("last_seen_at", "seen_count", "future_trigger", "trigger_text"):
                self.conn.execute(f"ALTER TABLE commitments DROP COLUMN {column}")
            for column in ("source_kind", "evidence", "superseded_at", "supersede_source",
                           "supersede_reason", "retracted_at", "retract_reason"):
                self.conn.execute(f"ALTER TABLE decisions DROP COLUMN {column}")
        self.rowcount = 0
        self.result = []
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        if "information_schema.columns" in sql:
            self.result = [(r[1],) for r in self.conn.execute(f"PRAGMA table_info({params[0]})")]
            return
        sql = " ".join(sql.split())
        sql = sql.replace("IS NOT DISTINCT FROM", "IS")
        sql = re.sub(r"::(?:timestamptz|jsonb)", "", sql)
        sql = re.sub(r"\bnow\(\)", "CURRENT_TIMESTAMP", sql)
        sql = re.sub(r"COALESCE\((\? |%s), CURRENT_TIMESTAMP\) - interval '(\d+) days'",
                     lambda m: f"datetime(COALESCE({m[1]}, CURRENT_TIMESTAMP), '-{m[2]} days')", sql)
        sql = re.sub(r"CURRENT_TIMESTAMP - \( (CASE.*?END)\)", r"datetime(CURRENT_TIMESTAMP, \1)", sql)
        sql = re.sub(r"interval '(\d+) days'", lambda m: f"'-{m[1]} days'", sql)
        sql = sql.replace("%s", "?")
        sql = re.sub(r" FOR UPDATE(?: OF \w+)?", "", sql)
        sql = sql.replace("EXCLUDED.", "excluded.")
        if "LIMIT ?" in sql and params[-1] is None:
            params = (*params[:-1], -1)
        cursor = self.conn.execute(sql, params)
        self.rowcount = cursor.rowcount
        self.result = cursor.fetchall()

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)

    def episode(self, eid, session="session-a", parent=None, deleted=None):
        self.conn.execute("INSERT INTO episodes(id, session_id, parent_session_id, deleted_at) VALUES (?, ?, ?, ?)",
                          (eid, session, parent, deleted))

    def commitment(self, text, *, project="p", episode=1, status="open", owner="assistant",
                   kind="followup", age=0, due_age=None):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        opened = (now - timedelta(days=age)).strftime("%Y-%m-%d %H:%M:%S")
        due = None if due_age is None else (now - timedelta(days=due_age)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.execute(
            "INSERT INTO commitments(text, project, owner, kind, opened_episode, opened_at, due_after, status, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (text, project, owner, kind, episode, opened, due, status, co.content_hash(project, text)),
        )
        return cursor.lastrowid

    def value(self, table, cid, column):
        return self.conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (cid,)).fetchone()[0]


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.cur = _RegistryCursor()
        self.addCleanup(self.cur.conn.close)
        for target, value in (("khipu.commitments._has_commitment_embeddings", False),
                              ("khipu.decisions._trgm_available", False)):
            patch = mock.patch(target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        for target in ("khipu.decisions._stale_briefs",):
            patch = mock.patch(target)
            patch.start()
            self.addCleanup(patch.stop)


class DecisionLifecycleTest(RegistryTest):
    def test_cap23_a_b_a_capture_reversals(self):
        ids = []
        with mock.patch("khipu.features.enabled", return_value=True):
            for eid, text, reverse in ((1, "choose option alpha", None),
                                       (2, "choose option beta", "choose option alpha"),
                                       (3, "choose option alpha", "choose option beta")):
                self.cur.episode(eid)
                payload = {"project": "p", "ts": f"2026-10-0{eid} 00:00:00",
                           "decisions": [text], "decision_details": [
                               {"text": text, "by": "user", "reverses": reverse}]}
                self.assertEqual(de.insert_decisions_from_episode(self.cur, payload, eid), 1)
                de.detect_reversals_from_episode(self.cur, payload, eid)
                ids.append(self.cur.conn.execute("SELECT id FROM decisions WHERE episode_id = ?", (eid,)).fetchone()[0])
        self.assertEqual(self.cur.value("decisions", ids[0], "superseded_by"), ids[1])
        self.assertEqual(self.cur.value("decisions", ids[1], "superseded_by"), ids[2])
        self.assertIsNone(self.cur.value("decisions", ids[2], "superseded_by"))

    def test_rsn15_renew_retracted_superseded_and_forgotten(self):
        for state in ("retracted", "superseded", "forgotten"):
            with self.subTest(state=state):
                text = "choose " + state
                old = de.create_decision(self.cur, project="p", text=text)
                if state == "retracted":
                    de.retract(self.cur, old)
                elif state == "superseded":
                    new = de.create_decision(self.cur, project="p", text="replacement")
                    de.supersede(self.cur, old, new)
                else:
                    self.cur.episode(1, deleted="2026-10-01")
                    self.cur.conn.execute("UPDATE decisions SET episode_id = 1 WHERE id = ?", (old,))
                renewed = de.create_decision(self.cur, project="p", text=text)
                self.assertNotEqual(old, renewed)
                self.assertIsNone(self.cur.value("decisions", renewed, "superseded_by"))
                self.assertIsNone(self.cur.value("decisions", renewed, "retracted_at"))

    def test_standing_restatement_still_deduplicates(self):
        old = de.create_decision(self.cur, project="p", text="choose alpha")
        self.assertEqual(de.create_decision(self.cur, project="p", text="CHOOSE   ALPHA"), old)

    def test_rsn17_stale_candidate_does_not_become_applied(self):
        a, b, c = [de.create_decision(self.cur, project="p", text=text) for text in ("alpha", "beta", "gamma")]
        link = de.add_link(self.cur, a, b)
        self.assertTrue(de.supersede(self.cur, a, c))
        out = de.resolve_link(self.cur, link, "confirm")
        self.assertFalse(out["ok"])
        self.assertEqual(self.cur.value("decision_links", link, "state"), "candidate")
        self.assertEqual(self.cur.value("decisions", a, "superseded_by"), c)

    def test_rsn17_confirm_existing_exact_edge_is_idempotent(self):
        a, b = [de.create_decision(self.cur, project="p", text=text) for text in ("alpha", "beta")]
        link = de.add_link(self.cur, a, b)
        self.cur.conn.execute("UPDATE decisions SET superseded_by = ? WHERE id = ?", (b, a))
        self.assertTrue(de.resolve_link(self.cur, link, "confirm")["ok"])
        self.assertEqual(self.cur.value("decision_links", link, "state"), "applied")

    def test_cap2_gw2_capture_details_are_redacted_at_writer(self):
        secret = "sk-ant-" + "x" * 30
        text = "choose " + secret
        payload = {"project": "p", "decisions": [text], "decision_details": [
            {"text": text, "by": "user", "rationale": secret, "reverses": secret}]}
        self.assertEqual(de.insert_decisions_from_episode(self.cur, payload, 1), 1)
        stored = self.cur.conn.execute("SELECT text, rationale, evidence FROM decisions").fetchone()
        self.assertTrue(all(secret not in str(value) for value in stored))
        self.assertEqual(stored[1], "[REDACTED]")
        self.assertEqual(json.loads(stored[2])["reverses"], "[REDACTED]")

    def test_gw3_replacement_and_lifecycle_reasons_are_redacted(self):
        secret = "sk-ant-" + "x" * 30
        old = de.create_decision(self.cur, project="p", text="old")
        new = de.create_decision(self.cur, project="p", text="new " + secret)
        de.supersede(self.cur, old, new, reason=secret)
        de.retract(self.cur, new, reason=secret)
        self.assertNotIn(secret, self.cur.value("decisions", new, "text"))
        self.assertEqual(self.cur.value("decisions", old, "supersede_reason"), "[REDACTED]")
        self.assertEqual(self.cur.value("decisions", new, "retract_reason"), "[REDACTED]")
        self.assertEqual(self.cur.conn.execute("SELECT reason FROM decision_links").fetchone()[0], "[REDACTED]")


class CommitmentClosureTest(RegistryTest):
    def test_rv4_03_plain_completion_closes_unique_task(self):
        for semantic in (False, True):
            with self.subTest(semantic=semantic):
                self.cur.conn.execute("DELETE FROM commitments")
                cid = self.cur.commitment("Verify the installed app")
                with mock.patch.object(co, "_has_commitment_embeddings", return_value=semantic), \
                        mock.patch.object(co, "_cosine_scores", return_value={cid: 1.0}):
                    self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": [
                        "Verified the installed app"]}, 3), 1)
                self.assertEqual(self.cur.value("commitments", cid, "status"), "closed")

    def test_rv4_03_tense_does_not_remove_destination_or_ambiguity(self):
        ids = [self.cur.commitment(text) for text in (
            "Verify the installed production app", "Verify the installed staging app")]
        with mock.patch.object(co, "_has_commitment_embeddings", return_value=True), \
                mock.patch.object(co, "_cosine_scores", return_value=dict.fromkeys(ids, 1.0)):
            self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": [
                "Verified the installed app"]}, 3), 0)
        self.assertEqual(self.cur.conn.execute("SELECT count(*) FROM commitments WHERE status = 'open'").fetchone()[0], 2)

    def test_arch11_ambiguous_publish_abstains_in_both_orders(self):
        for texts in (("Publish legal privacy notice", "Publish release 1.2"),
                      ("Publish release 1.2", "Publish legal privacy notice")):
            with self.subTest(texts=texts):
                self.cur.conn.execute("DELETE FROM commitments")
                for text in texts:
                    self.cur.commitment(text)
                self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": ["publish"]}, 3), 0)
                self.assertEqual(self.cur.conn.execute("SELECT count(*) FROM commitments WHERE status = 'open'").fetchone()[0], 2)
                self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": ["Publish release 1.2"]}, 4), 1)

    def test_rsn12_staging_completion_preserves_production(self):
        cid = self.cur.commitment("deploy app to production")
        self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": ["deploy app to staging"]}, 2), 0)
        self.assertEqual(self.cur.value("commitments", cid, "status"), "open")
        self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": ["deploy app to production"]}, 3), 1)

    def test_rsn12_shared_long_description_keeps_distinguishing_qualifier(self):
        original = "deploy the approved widget application release with the latest changes to production"
        completion = original.replace("production", "staging")
        cid = self.cur.commitment(original)
        with mock.patch.object(co, "_has_commitment_embeddings", return_value=True), \
                mock.patch.object(co, "_cosine_scores", return_value={cid: 0.99}):
            self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": [completion]}, 2), 0)

    def test_arch11_stable_reference_targets_only_its_own_scope(self):
        privacy = self.cur.commitment("publish legal privacy notice")
        release = self.cur.commitment("publish release 1.2")
        other = self.cur.commitment("publish other notice", project="other")
        payload = {"project": "p", "closed_loops": [{"text": "publish", "commitment_id": release}]}
        self.assertEqual(co.auto_close(self.cur, payload, 2), 1)
        self.assertEqual(self.cur.value("commitments", privacy, "status"), "open")
        payload["closed_loops"][0]["commitment_id"] = other
        self.assertEqual(co.auto_close(self.cur, payload, 2), 0)

    def test_ambiguous_explicit_done_and_cosine_ties_abstain(self):
        ids = [self.cur.commitment(text) for text in ("publish legal privacy notice", "publish release 1.2")]
        self.assertEqual(co.auto_close(self.cur, {"project": "p", "decisions": ["done: publish"]}, 3), 0)
        with mock.patch.object(co, "_has_commitment_embeddings", return_value=True), \
                mock.patch.object(co, "_cosine_scores", return_value=dict.fromkeys(ids, 0.96)):
            self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": ["the publication finished"]}, 3), 0)
        with mock.patch.object(co, "_has_commitment_embeddings", return_value=True), \
                mock.patch.object(co, "_cosine_scores", return_value={ids[0]: 0.97, ids[1]: 0.94}):
            self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": ["the publication finished"]}, 3), 0)

    def test_rsn11_projectless_sessions_open_and_close_independently(self):
        text = "Rebuild the deployment index"  # the assistant's: the owner's own never closes by rule
        for eid, session in ((1, "scratch-a"), (2, "scratch-b"), (3, "scratch-a")):
            self.cur.episode(eid, session)
        for eid, session in ((1, "scratch-a"), (2, "scratch-b")):
            self.assertEqual(co.open_from_episode(self.cur, {"session_id": session, "open_loops": [text]}, eid), 1)
        self.assertEqual(co.auto_close(self.cur, {"session_id": "scratch-a", "closed_loops": [text]}, 3), 1)
        rows = self.cur.conn.execute("SELECT opened_episode, status, project FROM commitments ORDER BY id").fetchall()
        self.assertEqual(rows, [(1, "closed", None), (2, "open", None)])
        self.assertEqual([r["opened_episode"] for r in co.list_owed(self.cur, session_id="scratch-b")], [2])
        self.assertEqual(co.list_owed(self.cur, session_id="scratch-a"), [])

    def test_rsn11_parent_lineage_restatements_and_legacy_hash(self):
        text = "Approve the privacy notice wording"
        self.cur.episode(1, "root")
        self.cur.episode(2, "child", "root")
        self.cur.episode(3, "unrelated")
        self.cur.commitment(text, project=None, episode=1)
        # The child is a job, so its restatement is the orchestrator's: a
        # different owner from the root's item, kept beside it and never
        # folded into it (third cross-family review).
        self.assertEqual(co.open_from_episode(self.cur, {"session_id": "child", "parent_session_id": "root", "open_loops": [text]}, 2), 1)
        self.assertEqual(co.open_from_episode(self.cur, {"session_id": "unrelated", "open_loops": [text]}, 3), 1)
        self.assertEqual(co.auto_close(self.cur, {"session_id": "child", "parent_session_id": "root", "closed_loops": [text]}, 2), 1)

    def test_rsn11_no_lineage_does_not_close_other_episode(self):
        self.cur.episode(1, None)
        self.cur.episode(2, None)
        text = "Rebuild the deployment index"  # the assistant's: the owner's own never closes by rule
        self.assertEqual(co.open_from_episode(self.cur, {"open_loops": [text]}, 1), 1)
        self.assertEqual(co.open_from_episode(self.cur, {"open_loops": [text]}, 2), 1)
        self.assertEqual(co.auto_close(self.cur, {"closed_loops": [text]}, 2), 1)
        self.assertEqual(self.cur.conn.execute("SELECT status FROM commitments WHERE opened_episode = 1").fetchone()[0], "open")


class CommitmentAgingTest(RegistryTest):
    def test_rsn13_elapsed_snooze_starts_full_stale_window(self):
        for modern in (True, False):
            with self.subTest(modern=modern):
                cur = self.cur if modern else _RegistryCursor(modern=False)
                if not modern:
                    self.addCleanup(cur.conn.close)
                from khipu import db
                db._TABLE_COLUMNS_CACHE.clear()
                fresh = cur.commitment("freshly due snooze", age=90, due_age=1)
                elapsed = cur.commitment("elapsed snooze", age=90, due_age=15)
                co.mark_stale(cur)
                self.assertEqual(cur.value("commitments", fresh, "status"), "open")
                self.assertEqual(cur.value("commitments", elapsed, "status"), "stale")

    def test_rsn14_oldest_user_blocker_precedes_limit(self):
        urgent = self.cur.commitment("User must approve the release", owner="user", kind="blocker", age=60)
        for i in range(51):
            self.cur.commitment(f"review change {i}", age=i / 100)
        rows = co.list_owed(self.cur, project="p", limit=50)
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["id"], urgent)

    def test_rsn23_reopen_refreshes_silence_on_both_schemas(self):
        for modern in (True, False):
            with self.subTest(modern=modern):
                cur = self.cur if modern else _RegistryCursor(modern=False)
                if not modern:
                    self.addCleanup(cur.conn.close)
                from khipu import db
                db._TABLE_COLUMNS_CACHE.clear()
                cid = cur.commitment("Approve the release", status="stale", age=90)
                self.assertTrue(co.set_status(cur, cid, "open"))
                co.mark_stale(cur)
                self.assertEqual(cur.value("commitments", cid, "status"), "open")

    def test_rsn23_duplicate_reopen_returns_conflict_without_unique_error(self):
        old = self.cur.commitment("Approve the release", status="closed")
        replacement = self.cur.commitment("Approve the release")
        self.assertFalse(co.set_status(self.cur, old, "open"))
        self.assertEqual(self.cur.value("commitments", old, "status"), "closed")
        self.assertEqual(self.cur.value("commitments", replacement, "status"), "open")

    def test_gw7_forgotten_commitment_readers_and_mutations_abstain(self):
        self.cur.episode(1, deleted="2026-10-01")
        closed = self.cur.commitment("Private approval text", status="closed")
        self.assertEqual(co.list_owed(self.cur, project="p", status="closed"), [])
        self.assertFalse(co.set_status(self.cur, closed, "open"))
        opened = self.cur.commitment("Other private task", status="open")
        self.assertEqual(co.auto_close(self.cur, {"project": "p", "closed_loops": ["Other private task"]}, 2), 0)
        self.assertEqual(self.cur.value("commitments", opened, "status"), "open")


class ValidityBoundaryTest(unittest.TestCase):
    def test_rec8_canonical_supersession_is_already_annotated(self):
        rows = validity.annotate([{"kind": "topic", "id": "old", "score": 0.5}],
                                 topic_meta={"old": {"status": "active", "superseded_by": "new"}})
        self.assertEqual(rows[0]["validity"]["state"], "superseded")

    @pytest.mark.xfail(reason="REC-17 requires session dedup changes in recall_prompt.py", strict=False)
    def test_rec17_current_superseded_restored_recall(self):
        from khipu import recall_prompt as rp

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("khipu.session_capture.khipu_home", return_value=Path(temp)):
            emitted = []
            for state in ("current", "superseded", "current"):
                hits = [{"kind": "episode", "id": "1", "score": 0.9, "snippet": "choose alpha",
                         "validity": {"state": state}}]
                with mock.patch.object(rp, "_search_hits", return_value={"hits": hits, "legs": [], "degraded": None}):
                    out = rp.prior_work_for_prompt("what did we decide about alpha", session_id="rsna-restoration")
                emitted.append(bool(out["context"]))
            self.assertEqual(emitted, [True, True, True])

    @pytest.mark.xfail(reason="RSN-19 requires canonical fallback in briefs.py and reflect.py", strict=False)
    def test_rsn19_retracted_canonical_choice_is_not_offered_from_repeat(self):
        from khipu import briefs, reflect
        from tests.test_reflect import _ReflectHub

        hub = _ReflectHub()
        for eid in (1, 2):
            hub.episode(eid, "Discussion of widget options.", decisions=["Choose alpha"])
        hub.decision(1, 1, "Choose alpha", retracted=True)
        offered = briefs._gather(hub.cursor(), "alpha")["offered"]
        sources = reflect._episode_sources(hub.cursor(), [1, 2])
        self.assertEqual(sources[2]["decisions"], [])
        self.assertFalse(any("Choose alpha" in item["text"] for item in offered))
