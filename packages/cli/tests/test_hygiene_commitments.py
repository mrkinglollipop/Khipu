"""Tests for `khipu hygiene commitments` — the Owed cleanup job.

No live database and no live model: a small fake cursor backs an in-memory
commitments table, and the "judge" is a canned callable. What is being tested
is the ORDER of the three passes (deterministic filter → model → paraphrase
merge), the cost guard, and the never-DELETE contract.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from khipu import commitments as co
from khipu import hygiene

# The owner/actor regexes are alias-driven (2026-09-05): "Matt" is only
# recognised as the user when "matt" is a configured alias. This file's
# fixtures were written when "matt" was hardcoded, so pin it for the module
# (mirrors `khipu config --set user_aliases matt`) and reset on the way out.
_alias_env_patch = mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "matt"})


def setUpModule():
    _alias_env_patch.start()
    co.reset_user_patterns()


def tearDownModule():
    _alias_env_patch.stop()
    co.reset_user_patterns()


class _Cursor:
    def __init__(self, rows, *, migrated: bool = True):
        # rows: list of (id, text, project, kind, owner, opened_at)
        self.rows = {
            r[0]: {"id": r[0], "text": r[1], "project": r[2], "kind": r[3],
                   "owner": r[4], "opened_at": r[5], "status": "open",
                   "close_reason": None, "seen_count": 1}
            for r in rows
        }
        self.migrated = migrated
        self.rowcount = 0
        self.statements: list[str] = []
        self._result: list[tuple] = []
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.pop("commitments", None)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.statements.append(s)
        params = params or ()
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            cols = ["id", "text", "project", "kind", "owner", "opened_at", "status"]
            if self.migrated:
                cols += ["last_seen_at", "seen_count"]
            self._result = [(c,) for c in cols]
            return
        if s.startswith("SELECT id, text, project, kind, owner, opened_at FROM commitments"):
            out = [r for r in self.rows.values() if r["status"] == "open"]
            if "project = %s" in s:
                out = [r for r in out if r["project"] == params[0]]
            out.sort(key=lambda r: r["id"])
            if " LIMIT %s" in s:
                out = out[: int(params[-1])]
            self._result = [
                (r["id"], r["text"], r["project"], r["kind"], r["owner"], r["opened_at"])
                for r in out
            ]
            return
        if s.startswith("UPDATE commitments SET status = 'dropped'"):
            reason, cid = params
            r = self.rows.get(cid)
            self.rowcount = 0
            if r and r["status"] == "open":
                r["status"] = "dropped"
                r["close_reason"] = reason
                self.rowcount = 1
            return
        if s.startswith("UPDATE commitments SET seen_count = seen_count +"):
            src, dst = params
            self.rows[dst]["seen_count"] += self.rows[src]["seen_count"]
            self.rowcount = 1
            return
        if s.startswith("UPDATE commitments SET status = 'closed'"):
            reason, cid = params
            r = self.rows.get(cid)
            self.rowcount = 0
            if r and r["status"] == "open":
                r["status"] = "closed"
                r["close_reason"] = reason
                self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


def _rows(*texts, project="acme/widget"):
    return [(i + 1, t, project, "followup", None, f"t{i}") for i, t in enumerate(texts)]


class _Judge:
    """Canned verdicts by text substring; records what it was asked."""

    def __init__(self, drop_substrings=()):
        self.drop = tuple(drop_substrings)
        self.calls: list[list[str]] = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        return [
            {"keep": not any(d in t for d in self.drop), "reason": "canned"}
            for t in texts
        ]


class FilterFirstTest(unittest.TestCase):
    def test_obvious_junk_never_reaches_the_model(self):
        cur = _Cursor(_rows(
            "Drive 46 is still running",
            "Send 'screen free' message to Khipu session",
            "Complete the audit of the licence wording",
        ))
        judge = _Judge()
        report = hygiene.run_commitments_hygiene(cur, judge=judge)
        self.assertEqual(judge.calls, [["Complete the audit of the licence wording"]])
        self.assertEqual(report["counts"]["drop"], 2)
        self.assertEqual(report["counts"]["keep"], 1)

    def test_a_user_owed_row_never_reaches_the_model_either(self):
        """2026-09-05: the deterministic owner rule (not the filter) is the
        other reason an item never reaches the model — it is kept outright
        as "user-owed"."""
        cur = _Cursor(_rows("Matt to decide whether 0.3.17 ships with the seal fix"))
        judge = _Judge()
        report = hygiene.run_commitments_hygiene(cur, judge=judge)
        self.assertEqual(judge.calls, [])
        self.assertEqual(report["counts"]["keep"], 1)
        self.assertEqual(report["verdicts"][0]["reason"], "user-owed")

    def test_filter_reason_is_reported_verbatim(self):
        cur = _Cursor(_rows("Drive 46 is still running"))
        report = hygiene.run_commitments_hygiene(cur, judge=_Judge())
        verdict = report["verdicts"][0]
        self.assertEqual(verdict["verdict"], "drop")
        self.assertTrue(verdict["reason"].startswith("filter:"), verdict["reason"])
        self.assertEqual(verdict["text"], "Drive 46 is still running")


class ModelVerdictTest(unittest.TestCase):
    def test_model_rejects_are_dropped_with_a_reason(self):
        cur = _Cursor(_rows(
            "Matt to decide whether 0.3.17 ships with the seal fix",
            "Complete the audit of the licence wording",
        ))
        judge = _Judge(drop_substrings=("Complete the audit",))
        report = hygiene.run_commitments_hygiene(cur, judge=judge)
        drops = [v for v in report["verdicts"] if v["verdict"] == "drop"]
        self.assertEqual(len(drops), 1)
        self.assertTrue(drops[0]["reason"].startswith("model:"))

    def test_a_failed_judge_keeps_everything(self):
        cur = _Cursor(_rows("Matt to decide whether 0.3.17 ships with the seal fix"))

        def _boom(texts):
            return [{"keep": True, "reason": "judge unavailable: RuntimeError"} for _ in texts]

        report = hygiene.run_commitments_hygiene(cur, judge=_boom)
        self.assertEqual(report["counts"]["keep"], 1)
        self.assertEqual(report["counts"].get("drop", 0), 0)

    def test_cost_guard_caps_model_calls_and_reports_unjudged(self):
        texts = [f"Ship item number {i} before the release" for i in range(90)]
        cur = _Cursor(_rows(*texts))
        judge = _Judge()
        report = hygiene.run_commitments_hygiene(cur, judge=judge, max_calls=1)
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(len(judge.calls[0]), hygiene.JUDGE_BATCH)
        self.assertEqual(report["model_calls"], 1)
        self.assertEqual(report["counts"]["unjudged"], 90 - hygiene.JUDGE_BATCH)


class DuplicateMergeTest(unittest.TestCase):
    def test_paraphrases_fold_into_the_oldest_row(self):
        cur = _Cursor(_rows(
            "Matt to decide whether 0.3.17 ships with the seal fix",
            "Matt to decide whether 0.3.17 ships with the seal fix or waits",
            "Renew the TLS cert for the hub before December",
        ))
        report = hygiene.run_commitments_hygiene(cur, judge=_Judge())
        dupes = [v for v in report["verdicts"] if v["verdict"] == "duplicate"]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0]["id"], 2)
        self.assertEqual(dupes[0]["reason"], "duplicate-of-1")

    def test_duplicates_never_cross_projects(self):
        rows = _rows("Matt to decide whether 0.3.17 ships with the seal fix")
        rows += [(2, "Matt to decide whether 0.3.17 ships with the seal fix", "other/repo",
                  "followup", None, "t1")]
        cur = _Cursor(rows)
        report = hygiene.run_commitments_hygiene(cur, judge=_Judge())
        self.assertEqual(report["counts"].get("duplicate", 0), 0)


class ApplyTest(unittest.TestCase):
    def _cur(self):
        return _Cursor(_rows(
            "Drive 46 is still running",
            "Matt to decide whether 0.3.17 ships with the seal fix",
            "Matt to decide whether 0.3.17 ships with the seal fix or waits",
        ))

    def test_dry_run_writes_nothing_and_lists_every_verdict(self):
        cur = self._cur()
        report = hygiene.run_commitments_hygiene(cur, judge=_Judge(), apply=False)
        self.assertTrue(report["dry_run"])
        self.assertEqual(len(report["verdicts"]), 3)
        self.assertTrue(all(r["status"] == "open" for r in cur.rows.values()))
        self.assertFalse(any(s.startswith("UPDATE") for s in cur.statements))

    def test_apply_drops_rejects_and_closes_duplicates_without_deleting(self):
        cur = self._cur()
        report = hygiene.run_commitments_hygiene(cur, judge=_Judge(), apply=True)
        self.assertFalse(report["dry_run"])
        self.assertEqual(cur.rows[1]["status"], "dropped")
        self.assertTrue(cur.rows[1]["close_reason"].startswith("hygiene-"))
        self.assertEqual(cur.rows[2]["status"], "open")
        self.assertEqual(cur.rows[3]["status"], "closed")
        self.assertEqual(cur.rows[3]["close_reason"], "duplicate-of-2")
        self.assertEqual(report["applied"], 2)
        self.assertFalse(any(s.startswith("DELETE") for s in cur.statements))

    def test_apply_folds_seen_count_into_the_keeper(self):
        cur = self._cur()
        cur.rows[3]["seen_count"] = 5
        hygiene.run_commitments_hygiene(cur, judge=_Judge(), apply=True)
        self.assertEqual(cur.rows[2]["seen_count"], 6)

    def test_pre_migration_hub_skips_the_seen_count_fold(self):
        cur = self._cur()
        cur.migrated = False
        hygiene.run_commitments_hygiene(cur, judge=_Judge(), apply=True)
        self.assertFalse(any("seen_count" in s for s in cur.statements))
        self.assertEqual(cur.rows[3]["status"], "closed")


class ScopeTest(unittest.TestCase):
    def test_project_and_limit_are_pushed_into_the_query(self):
        rows = _rows("Matt to decide the first item before release")
        rows += [(2, "Matt to decide the second item before release", "other/repo",
                  "followup", None, "t1")]
        cur = _Cursor(rows)
        report = hygiene.run_commitments_hygiene(
            cur, judge=_Judge(), project="other/repo", limit=5
        )
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["project"], "other/repo")


class BackupTest(unittest.TestCase):
    def test_backup_writes_a_binary_copy_and_a_restore_md(self):
        import tempfile
        from pathlib import Path

        class _Copy:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                yield b"PGCOPY\n"

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def copy(self, sql):
                assert "COPY commitments TO STDOUT (FORMAT BINARY)" in sql
                return _Copy()

        class _Conn:
            def cursor(self):
                return _Cur()

        with tempfile.TemporaryDirectory() as tmp:
            dest = hygiene.backup_commitments(_Conn(), dest_root=Path(tmp))
            self.assertTrue((dest / "commitments.bin").exists())
            self.assertEqual((dest / "commitments.bin").read_bytes(), b"PGCOPY\n")
            restore = (dest / "RESTORE.md").read_text()
            self.assertIn("khipu owed --reopen", restore)
            self.assertIn("FORMAT BINARY", restore)


class _SessionCursor:
    """Fake hub for the session-ended pass: commitments joined to the session
    that opened them, with that session's age and sessionend flag."""

    def __init__(self, rows, *, trigger_col: bool = False):
        # rows: (id, text, kind, owner, session_id, aged, end_event)
        self.rows = {
            r[0]: {"id": r[0], "text": r[1], "kind": r[2], "owner": r[3],
                   "session_id": r[4], "aged": r[5], "end_event": r[6],
                   "project": "acme/widget", "opened_at": "t0", "status": "open",
                   "close_reason": None, "future_trigger": False}
            for r in rows
        }
        self.trigger_col = trigger_col
        self.rowcount = 0
        self.statements: list[str] = []
        self._result: list[tuple] = []
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.pop("commitments", None)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.statements.append(s)
        params = params or ()
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            cols = ["id", "text", "project", "kind", "owner", "opened_at", "status"]
            if self.trigger_col:
                cols.append("future_trigger")
            self._result = [(c,) for c in cols]
            return
        if s.startswith("SELECT c.id, c.text, c.project, c.kind, c.owner, c.opened_at"):
            out = [r for r in self.rows.values() if r["status"] == "open"]
            if "c.project = %s" in s:
                out = [r for r in out if r["project"] == params[0]]
            out.sort(key=lambda r: r["id"])
            if " LIMIT %s" in s:
                out = out[: int(params[-1])]
            self._result = [
                (r["id"], r["text"], r["project"], r["kind"], r["owner"], r["opened_at"],
                 r["session_id"], "t9", r["aged"], r["end_event"])
                for r in out
            ]
            return
        if s.startswith("UPDATE commitments SET owner = %s, future_trigger = %s"):
            owner, trigger, cid = params
            self.rows[cid]["owner"] = owner
            self.rows[cid]["future_trigger"] = trigger
            self.rowcount = 1
            return
        if s.startswith("UPDATE commitments SET owner = %s WHERE"):
            owner, cid = params
            self.rows[cid]["owner"] = owner
            self.rowcount = 1
            return
        if s.startswith("UPDATE commitments SET status = 'closed'"):
            (cid,) = params
            r = self.rows.get(cid)
            self.rowcount = 0
            if r and r["status"] == "open":
                r["status"] = "closed"
                r["close_reason"] = "session-ended"
                self.rowcount = 1
            return
        if s.startswith("UPDATE commitments SET status = 'dropped'"):
            reason, cid = params
            r = self.rows.get(cid)
            self.rowcount = 0
            if r and r["status"] == "open":
                r["status"] = "dropped"
                r["close_reason"] = reason
                self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


ENDED = True
LIVE = False


class SessionEndedPassTest(unittest.TestCase):
    """`khipu hygiene commitments --session-ended`: the model-free ownership
    pass over rows that are already in the table."""

    def _cur(self, **kw):
        return _SessionCursor([
            (1, "Matt's action: merge Cursor PRs #859/#860.", "followup", None, "s1", ENDED, False),
            (2, "Run an Opus live drive on a scratch build of main.", "promise", None, "s1", ENDED, False),
            (3, "When Matt says the lane is re-authed, run the oracle once more.",
             "followup", "Matt", "s1", ENDED, False),
            (4, "Reply with the SHA each PR now points at.", "promise", None, "s2", LIVE, False),
            (5, "Implement the fastlane Fastfile and run each lane once.",
             "followup", None, "s2", LIVE, False),
        ], **kw)

    def test_verdicts_and_reasons(self):
        report = hygiene.run_session_ended_pass(self._cur())
        by_id = {v["id"]: v for v in report["verdicts"]}
        self.assertEqual(by_id[1]["verdict"], "keep")
        self.assertEqual(by_id[1]["owner"], "user")
        self.assertEqual(by_id[2]["verdict"], "close")
        self.assertEqual(by_id[2]["reason"], "session-ended")
        self.assertEqual(by_id[3]["verdict"], "keep")
        self.assertTrue(by_id[3]["future_trigger"])
        self.assertEqual(by_id[4]["verdict"], "drop")
        self.assertEqual(by_id[5]["verdict"], "keep")
        self.assertEqual(by_id[5]["reason"], "session still open")
        self.assertEqual(report["counts"], {"keep": 3, "close": 1, "drop": 1})

    def test_every_row_carries_the_report_fields(self):
        report = hygiene.run_session_ended_pass(self._cur())
        for v in report["verdicts"]:
            for field in ("id", "owner", "future_trigger", "session_ended",
                          "verdict", "reason", "text"):
                self.assertIn(field, v)

    def test_a_sessionend_event_ends_the_session_even_when_it_is_recent(self):
        cur = _SessionCursor([
            (1, "Implement the fastlane Fastfile and run each lane once.",
             "followup", None, "s1", LIVE, True),
        ])
        report = hygiene.run_session_ended_pass(cur)
        self.assertEqual(report["verdicts"][0]["verdict"], "close")

    def test_dry_run_writes_nothing(self):
        cur = self._cur()
        report = hygiene.run_session_ended_pass(cur, apply=False)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["applied"], 0)
        self.assertTrue(all(r["status"] == "open" for r in cur.rows.values()))
        self.assertFalse(any(s.startswith("UPDATE") for s in cur.statements))

    def test_apply_closes_drops_and_fills_the_columns(self):
        cur = self._cur(trigger_col=True)
        report = hygiene.run_session_ended_pass(cur, apply=True)
        self.assertEqual(report["applied"], 2)
        self.assertEqual(cur.rows[1]["status"], "open")
        self.assertEqual(cur.rows[1]["owner"], "user")
        self.assertEqual(cur.rows[2]["status"], "closed")
        self.assertEqual(cur.rows[2]["close_reason"], "session-ended")
        self.assertTrue(cur.rows[3]["future_trigger"])
        self.assertEqual(cur.rows[4]["status"], "dropped")
        self.assertTrue(cur.rows[4]["close_reason"].startswith("hygiene-"))
        self.assertFalse(any(s.startswith("DELETE") for s in cur.statements))

    def test_a_pre_migration_hub_computes_the_field_and_never_writes_it(self):
        cur = self._cur(trigger_col=False)
        report = hygiene.run_session_ended_pass(cur, apply=True)
        self.assertFalse(report["future_trigger_column"])
        self.assertTrue(next(v for v in report["verdicts"] if v["id"] == 3)["future_trigger"])
        self.assertFalse(any("future_trigger = %s" in s for s in cur.statements))

    def test_the_hours_threshold_reaches_the_query(self):
        cur = self._cur()
        hygiene.run_session_ended_pass(cur, hours=12)
        select = next(s for s in cur.statements if s.startswith("SELECT c.id"))
        self.assertIn("interval '12 hours'", select)

    def test_project_and_limit_are_pushed_into_the_query(self):
        cur = self._cur()
        report = hygiene.run_session_ended_pass(cur, project="acme/widget", limit=2)
        self.assertEqual(report["scanned"], 2)
        select = next(s for s in cur.statements if s.startswith("SELECT c.id"))
        self.assertIn("c.project = %s", select)
        self.assertIn("LIMIT %s", select)

    def test_never_closes_a_user_owed_row_however_old_its_session(self):
        cur = _SessionCursor([
            (1, "Matt reviews and merges the stack himself.", "followup", None, "s1", ENDED, True),
            (2, "Decide on the remaining `.cursor/` tree in the public repo.",
             "question", "user", "s1", ENDED, True),
        ])
        report = hygiene.run_session_ended_pass(cur, apply=True)
        self.assertEqual(report["counts"], {"keep": 2, "close": 0, "drop": 0})
        self.assertTrue(all(r["status"] == "open" for r in cur.rows.values()))


if __name__ == "__main__":
    unittest.main()
