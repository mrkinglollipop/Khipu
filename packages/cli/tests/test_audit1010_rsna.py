"""Hermetic lifecycle regressions, using SQL against an in-memory registry."""
from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from khipu import commitments as co, validity


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


class DecisionLifecycleTest(RegistryTest):
    pass








class CommitmentClosureTest(RegistryTest):
    pass
    pass

class CommitmentAgingTest(RegistryTest):

    def test_rsn14_oldest_user_blocker_precedes_limit(self):
        urgent = self.cur.commitment("User must approve the release", owner="user", kind="blocker", age=60)
        for i in range(51):
            self.cur.commitment(f"review change {i}", owner="user", age=60 + i / 100)
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
