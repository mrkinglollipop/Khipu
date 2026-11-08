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
    pass

class CommitmentAgingTest(RegistryTest):

    def test_rsn14_oldest_user_blocker_precedes_limit(self):
        urgent = self.cur.commitment("User must approve the release", owner="user", kind="blocker", age=60)
        for i in range(51):
            self.cur.commitment(f"review change {i}", age=i / 100)
        rows = co.list_owed(self.cur, project="p", limit=50)
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["id"], urgent)


    def test_rsn23_duplicate_reopen_returns_conflict_without_unique_error(self):
        old = self.cur.commitment("Approve the release", status="closed")
        replacement = self.cur.commitment("Approve the release")
        self.assertFalse(co.set_status(self.cur, old, "open"))
        self.assertEqual(self.cur.value("commitments", old, "status"), "closed")
        self.assertEqual(self.cur.value("commitments", replacement, "status"), "open")



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
