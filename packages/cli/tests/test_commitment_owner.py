"""Owner and origin at capture (scope docs/plans/2026-10-16-owed-and-ledger-ssot.md,
slice 1, with Matt's decisions 2 and 3).

An item's owner is the grammatical subject who commits, as its speaker
states it, read with the session's lineage (third cross-family review): the
speaker's own "I will" (conditional or not) is the speaker's; "Bob will
review" or "the reviewer must" names a third party (owner ``agent`` with that
label), whoever says it and in any session; and a dispatched job's "user" is
the orchestrator that wrote its brief, so a job's questions, its "Matt to
decide X" and its user's "I will" belong to the orchestrator. Inferred owner
work is now dropped from conversation captures; agent work remains.

No live database except the last class, which uses the throwaway PostgreSQL
of tests/hermetic_pg.py (a hub at migration 0029, so without 0033's columns).
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import pytest

from khipu import commitments as co
from tests.hermetic_pg import hub, migration_sql, pg_cluster  # noqa: F401
from tests.test_commitments import _CommitmentsCursor, _alias_env_patch
from tests.fact_seed import announce


def setUpModule():
    _alias_env_patch.start()
    co.reset_user_patterns()


def tearDownModule():
    _alias_env_patch.stop()
    co.reset_user_patterns()


@pytest.fixture(autouse=True)
def _aliases():
    """pytest-style tests in this module get the same alias as the classes."""
    with mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "matt"}):
        co.reset_user_patterns()
        yield
    co.reset_user_patterns()


class _OwnerCursor(_CommitmentsCursor):
    """The fake hub with migration 0033 applied: ``origin`` and
    ``owner_label`` columns, and ``RETURNING id`` on the insert."""

    def __init__(self, **kw):
        kw.setdefault("migrated", True)
        kw.setdefault("trigger", True)
        super().__init__(**kw)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = tuple(params or ())
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            super().execute(sql, params)
            self._result += [("origin",), ("owner_label",)]
            return
        if s.startswith("INSERT INTO commitments") and "owner_label" in s:
            *rest, origin, label = params
            before = set(self.rows)
            super().execute(sql.replace(", origin, owner_label", ""), tuple(rest))
            new = set(self.rows) - before
            for cid in new:
                self.rows[cid].update(origin=origin, owner_label=label)
            self._result = [(cid,) for cid in new]
            return
        if s.startswith("UPDATE commitments SET close_reason = %s WHERE id = ANY(%s)"):
            reason, ids = params
            for cid in ids:
                self.rows[cid]["close_reason"] = reason
            self.rowcount = len(ids)
            return
        if s.startswith("SELECT id, text, project, owner, kind") and "owner_label" in s:
            super().execute(sql, params)
            by_id = self.rows
            self._result = [tuple(t) + (by_id[t[0]].get("origin") or "conversation",
                                        by_id[t[0]].get("owner_label"))
                            for t in self._result]
            return
        super().execute(sql, params)


JOB = {"project": "acme/widget", "parent_session_id": "claude_code:host-1",
       "session_id": "claude_code:child-1"}
CONVERSATION = {"project": "acme/widget", "session_id": "claude_code:host-1"}




def _by_text(cur, text):
    return next(r for r in cur.rows.values() if r["text"] == text)


class NamedThirdPartyTest(unittest.TestCase):
    """A named third party as the subject owns the item, whoever says it, in
    a job and in the owner's own conversation alike."""



    def test_the_owner_of_a_pr_is_the_pr_author_not_the_user(self):
        self.assertEqual(co.resolve_owner_and_label("The owner of PR #12 to rebase onto main", origin="job"),
                         ("agent", "PR author"))



class JobRulesTest(unittest.TestCase):


    def test_khipu_job_marks_a_capture_without_lineage_as_a_job(self):
        payload = {"project": "acme/widget", "session_id": "codex:abc", "session_origin": "job"}
        self.assertEqual(co.capture_origin(payload), "job")
        self.assertEqual(co.capture_origin(CONVERSATION), "conversation")
        self.assertEqual(co.capture_origin(JOB), "job")






class RelayTest(unittest.TestCase):
    pass


def _owed_row(cid, text, owner, *, kind="followup", label=None, origin="conversation"):
    return {"id": cid, "text": text, "kind": kind, "owner": owner, "owner_label": label,
            "origin": origin, "opened_at": None}


class _NullCursor:
    rowcount = 0

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _NullConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        cur = _NullCursor()
        conn = self

        class _Ctx:
            def __enter__(self_inner):
                return cur

            def __exit__(self_inner, *a):
                return False

        _ = conn
        return _Ctx()


def _slice(rows, *, job=False, cap=None):
    from khipu import activity, config

    stored = {}
    with mock.patch.object(activity, "connect", return_value=_NullConn()), \
         mock.patch("khipu.commitments.list_owed", return_value=[dict(r) for r in rows]), \
         mock.patch("khipu.embed._episode_schema_flags",
                    return_value={"project": True, "deleted_at": True, "parent_session_id": True}), \
         mock.patch.object(config, "load_config", return_value=stored):
        return activity.project_slice(project="acme/widget", job=job)


class SessionStartSliceTest(unittest.TestCase):
    ROWS = ([_owed_row(i, f"Approve release candidate number {i}", "user") for i in (1, 2)]
            + [_owed_row(10 + i, f"Rebase the PR #{i} stack onto main", "agent", label="orchestrator",
                         origin="job") for i in range(5)])

    def test_conversation_and_job_slices_do_not_fetch_commitments(self):
        for job in (False, True):
            with self.subTest(job=job):
                data = _slice(self.ROWS, job=job)
            self.assertEqual(data["commitments"], [])
            # Neither conversation nor job sessions render the removed block.
            from khipu import recall_rule
            self.assertNotIn("Open commitments", recall_rule._render_project_slice("acme/widget", data))



class WaitingOnYouTest(unittest.TestCase):
    pass


class ExtractorPromptTest(unittest.TestCase):
    pass




class JobDetectionTest(unittest.TestCase):
    def test_capture_job_markers(self):
        from khipu import session_capture as sc

        self.assertEqual(sc.capture_origin({}, {"KHIPU_JOB": "1"}, harness="codex", sid="a"), "job")
        self.assertEqual(sc.capture_origin({}, {"KHIPU_PARENT_SESSION": "codex:p"}, harness="codex",
                                           sid="a"), "job")
        self.assertEqual(sc.capture_origin({}, {"CLAUDE_CODE_HOST_SESSION_ID": "host"},
                                           harness="claude_code", sid="child"), "job")
        self.assertEqual(sc.capture_origin({}, {"CLAUDE_CODE_HOST_SESSION_ID": "same"},
                                           harness="claude_code", sid="same"), "conversation")
        self.assertEqual(sc.capture_origin({}, {}, harness="codex", sid="a"), "conversation")


    def test_a_job_session_start_asks_for_a_job_slice(self):
        from khipu import recall_rule as rr

        seen = []
        with mock.patch.dict(os.environ, {"KHIPU_JOB": "1"}), \
             mock.patch.object(rr, "_pushed_memory_slice",
                               lambda cwd, *, job=False: seen.append(job) or ""):
            token = rr._START_IS_JOB.set(rr._session_is_job({"session_id": "s1"}))
            try:
                rr.session_start_context("/repo")
            finally:
                rr._START_IS_JOB.reset(token)
            rr.session_start_context("/repo")
        self.assertEqual(seen, [True, False])


    def test_a_khipu_job_capture_reaches_the_extractor_and_commitments_as_a_job(self):
        import tempfile
        from pathlib import Path

        from khipu import aegis_capture as ac

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_AEGIS_HOME": str(Path(td) / "kh")}):
            ac.enqueue({"harness": "aegis", "session_id": "j1", "cwd": "/x", "event": "stop",
                        "ts": ac._mint_ts(), "turns": 1, "transcript": "USER: durable",
                        "project": "acme/widget", "origin": "job"})
            calls, captured = [], []

            def fake_extract(t, *, cwd="", **kw):
                calls.append(kw)
                return {"summary": "s", "topics": [], "decisions": [], "preferences": [],
                        "scope": "", "edges": [], "topic_pages": []}

            with mock.patch("khipu.extract.extract_memory", fake_extract), \
                    mock.patch("khipu.capture.capture", lambda p, mode=None, **_: captured.append(p) or 0), \
                    mock.patch("khipu.config.capture_mode", lambda: "dual"):
                ac.drain()
        self.assertEqual(calls, [{"job": True}])
        self.assertEqual(co.capture_origin(captured[0]), "job")


# ---- a real hub without migration 0033 -------------------------------------

def _episode(cur) -> int:
    announce(cur)
    cur.execute("INSERT INTO episodes (ts, summary, raw) VALUES (now(), 's', %s::jsonb) RETURNING id",
                (json.dumps({}),))
    return int(cur.fetchone()[0])






# ---- the review's probe: a phrase never discards a real commitment ----------

def test_supported_reviewer_actor_is_stored_not_dropped_for_agent_work():
    """Slice 1: a supported third-party actor survives agent-word filtering."""
    text = "The reviewer must fix agent attribution before PR #41 merges"
    owner, label = co.resolve_owner_and_label(text, origin="job")
    assert (owner, label) == (co.OWNER_AGENT, "reviewer")
    assert co.rejection_reason(text, owner=owner) is None


def test_the_agent_classifier_reassigns_and_never_discards(monkeypatch):
    monkeypatch.setenv("KHIPU_USER_ALIASES", "matt")
    co.reset_user_patterns()
    try:
        # The owner's own commitment mentioning agents stays the owner's.
        assert co.resolve_owner_and_label("Matt to fix agent attribution in the capture hook") == (
            co.OWNER_USER, None)
        assert co.rejection_reason("Matt to fix agent attribution in the capture hook") is None
        # Waiting on an agent's output goes by lineage, and is kept.
        text = "Did the phase 2 agent's report land?"
        assert co.resolve_owner_and_label(text, kind="question", origin="job") == (
            co.OWNER_AGENT, co.ORCHESTRATOR)
        assert co.resolve_owner_and_label(text, kind="question") == (co.OWNER_USER, None)
        assert co.rejection_reason(text, owner=co.OWNER_AGENT, kind="question") is None
        # The word alone moves nothing.
        assert co.resolve_owner_and_label("Should we rename agent attribution?", kind="question") == (
            co.OWNER_USER, None)
    finally:
        co.reset_user_patterns()


# ---- owners come from lineage, never from keywords (second cross-family review) ----

_LINEAGE_CASES = [
    # (text, kind, declared, declared_label, origin, expected)
    # The review's probes: the owner's own words in his own session.
    ("I will ask the orchestrator to approve the release", "promise", "user", None, "conversation",
     ("user", None)),
    ("Matt, should we enable desktop notifications?", "question", "user", None, "conversation",
     ("user", None)),
    ("Matt, what is your verdict on the release?", "question", "user", None, "conversation",
     ("user", None)),
    # Their mirrors: the same words in a job, whose "user" is its orchestrator.
    ("I will ask the orchestrator to approve the release", "promise", "user", None, "job",
     ("agent", "orchestrator")),
    ("Matt, should we enable desktop notifications?", "question", "user", None, "job",
     ("agent", "orchestrator")),
    ("Matt, what is your verdict on the release?", "question", "user", None, "job",
     ("agent", "orchestrator")),
    # Keywords that once moved an item, in a conversation and in a job. A
    # third party as the subject owns it, whoever says it.
    ("Did the background agent's report land?", "question", None, None, "conversation", ("user", None)),
    ("Did the background agent's report land?", "question", None, None, "job", ("agent", "orchestrator")),
    ("The orchestrator must resolve the rebase conflict", "blocker", "user", None, "conversation",
     ("agent", "orchestrator")),
    ("The orchestrator must resolve the rebase conflict", "blocker", None, None, "conversation",
     ("agent", "orchestrator")),
    ("The orchestrator must resolve the rebase conflict", "blocker", "other", "orchestrator", "conversation",
     ("agent", "orchestrator")),
    ("The orchestrator must resolve the rebase conflict", "blocker", "user", None, "job",
     ("agent", "orchestrator")),
    ("The reviewer must fix agent attribution before PR #41 merges", "followup", "user", None, "conversation",
     ("agent", "reviewer")),
    ("The reviewer must fix agent attribution before PR #41 merges", "followup", "user", None, "job",
     ("agent", "reviewer")),
    ("Matt to fix agent attribution in the capture hook", "followup", None, None, "conversation",
     ("user", None)),
    ("Matt to fix agent attribution in the capture hook", "followup", None, None, "job",
     ("agent", "orchestrator")),
    ("Receive report from the phase 1-2 agent", "followup", "assistant", None, "conversation",
     ("assistant", None)),
    ("Receive report from the phase 1-2 agent", "followup", "assistant", None, "job", ("assistant", None)),
    # The third review's probes and their mirrors: the subject who commits,
    # as the speaker states it, with the session's lineage.
    ("When the reviewer finishes, I will test the installation on my phone", "promise", "user", None,
     "conversation", ("user", None)),
    ("When the reviewer finishes, I will test the installation on my phone", "promise", "user", None,
     "job", ("agent", "orchestrator")),
    ("When the reviewer finishes, I will test the installation on my phone", "promise", "assistant", None,
     "conversation", ("assistant", None)),
    ("I will review the deployment checklist before release", "promise", "user", None, "conversation",
     ("user", None)),
    ("I will review the deployment checklist before release", "promise", "user", None, "job",
     ("agent", "orchestrator")),
    ("I will review the deployment checklist before release", "promise", "assistant", None, "conversation",
     ("assistant", None)),
    ("I will review the deployment checklist before release", "promise", "assistant", None, "job",
     ("assistant", None)),
    ("The reviewer must rebase the release branch", "followup", None, None, "conversation",
     ("agent", "reviewer")),
    ("The reviewer must rebase the release branch", "followup", "user", None, "conversation",
     ("agent", "reviewer")),
    ("The reviewer must rebase the release branch", "followup", "assistant", None, "job",
     ("agent", "reviewer")),
    ("Bob will review the migration plan", "followup", "user", None, "conversation", ("agent", "Bob")),
    ("Bob will review the migration plan", "followup", "assistant", None, "conversation", ("agent", "Bob")),
    ("Bob will review the migration plan", "followup", "user", None, "job", ("agent", "Bob")),
    ("Matt will review the migration plan", "followup", "assistant", None, "conversation", ("user", None)),
    ("Matt will review the migration plan", "followup", "assistant", None, "job", ("agent", "orchestrator")),
    # No subject: an imperative or a passive names nobody, so the speaker's.
    ("Merge the release branch once the reviewer approves", "followup", "assistant", None, "conversation",
     ("assistant", None)),
    ("I'll rerun the oracle after the reviewer approves the fix", "promise", "assistant", None,
     "conversation", ("assistant", None)),
]




# ---- the third cross-family review's probes ---------------------------------

def test_real_owner_conditional_i_will_remains_the_owner():
    text = "When the reviewer finishes, I will test the installation on my phone"
    with mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "Matt"}):
        co.reset_user_patterns()
        assert co.has_future_trigger(text)
        assert co.rejection_reason(text, owner="user", kind="promise") is None
        assert co.resolve_owner_and_label(text, kind="promise", declared="user", origin="conversation") == (
            "user", None)




def test_owner_conversation_job_and_orchestrator_attribution(monkeypatch):
    monkeypatch.setenv("KHIPU_USER_ALIASES", "Matt")
    co.reset_user_patterns()
    assert co.resolve_owner_and_label("I will accept the developer agreement", declared="user",
                                      origin="conversation")[0] == "user"
    assert co.resolve_owner_and_label("Matt, should we ship the next version?", kind="question",
                                      declared="assistant", origin="conversation")[0] == "user"
    assert co.resolve_owner_and_label("Matt to decide whether to ship the next version", kind="question",
                                      declared="user", origin="job") == ("agent", "orchestrator")
    assert co.resolve_owner_and_label("The reviewer must rebase the release branch",
                                      origin="conversation") == ("agent", "reviewer")


def test_supported_reviewer_actor_is_stored_in_any_session():
    """The round-1 probe as written: no lineage given, the reviewer still owns it."""
    text = "The reviewer must fix agent attribution before PR #41 merges"
    owner, label = co.resolve_owner_and_label(text)
    assert (owner, label) == (co.OWNER_AGENT, "reviewer")
    assert co.rejection_reason(text, owner=owner) is None




# ---- the fourth review ------------------------------------------------------

def test_capture_does_not_reject_a_third_partys_reporting_shaped_commitment():
    text = "The reviewer will provide the evidence for the release decision"
    owner, label = co.resolve_owner_and_label(text, origin="conversation")
    assert (owner, label) == ("agent", "reviewer")
    assert co.rejection_reason(text, owner=owner, kind="followup") is None
    # The speaker's own reporting duty is still not a commitment.
    assert co.rejection_reason("Provide the evidence for the release decision", owner=co.OWNER_ASSISTANT) \
        == "reporting"


class _RelayCursor(_OwnerCursor):
    """The migrated close rule and the partial unique index on (project,
    content_hash) for open rows, with the real SQL's parameters."""

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = tuple(params or ())
        if s.startswith("INSERT INTO commitments") and ", prs)" in s:
            super().execute(sql.replace(", prs)", ")"), params[:-1])
            return
        if s.startswith("UPDATE commitments SET status = 'closed'") and "reopened_rule_keys" in s:
            episode, reason, by, key, evidence, cid, checked_key, *same = params
            row = self.rows[cid]
            self.rowcount = int(row["status"] == "open" and checked_key not in row.get("reopened_rule_keys", [])
                                and (not same or same[0] == row["owner"]))
            if self.rowcount:
                row.update(status="closed", closed_episode=episode, close_reason=reason, closed_by=by,
                           close_rule_key=key)
            return
        if s.startswith("UPDATE commitments SET status = %s"):
            status, reason, by, *_suggestion_reason, cid = params
            self.rows[cid].update(status=status, close_reason=reason, closed_by=by, close_rule_key=None)
            self.rowcount = 1
            return
        if "settle_suggestion = NULL" in s:
            # A reopen withdraws a "Looks done" suggestion; this fake keeps none.
            self.rowcount = 0
            return
        if s.startswith("UPDATE commitments SET status = 'open'"):
            cid, = params
            row = self.rows[cid]
            blocked = any(other["id"] != cid and other["status"] == "open" and other["project"] == row["project"]
                          and other["content_hash"] == row["content_hash"] for other in self.rows.values())
            self.rowcount = int(not blocked)
            if not blocked:
                if row.get("close_rule_key"):
                    row.setdefault("reopened_rule_keys", []).append(row["close_rule_key"])
                row.update(status="open", close_rule_key=None, close_reason=None)
            return
        super().execute(sql, params)




