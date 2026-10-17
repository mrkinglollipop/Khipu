"""Owner and origin at capture (scope docs/plans/2026-10-16-owed-and-ledger-ssot.md,
slice 1, with Matt's decisions 2 and 3).

An item's owner is the grammatical subject who commits, as its speaker
states it, read with the session's lineage (third cross-family review): the
speaker's own "I will" (conditional or not) is the speaker's; "Bob will
review" or "the reviewer must" names a third party (owner ``agent`` with that
label), whoever says it and in any session; and a dispatched job's "user" is
the orchestrator that wrote its brief, so a job's questions, its "Matt to
decide X" and its user's "I will" belong to the orchestrator. Nothing is
dropped: only the owner and where an item shows change.

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
from tests.test_commitments import _CommitmentsCursor, _alias_env_patch, _no_cosine


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


def _open(cur, base, *loops, episode=1):
    payload = dict(base, open_loops=list(loops))
    with mock.patch.object(co, "_cosine_scores", _no_cosine), \
         mock.patch.object(co, "_has_commitment_embeddings", lambda cur, ids: False):
        return co.open_from_episode(cur, payload, episode)


def _by_text(cur, text):
    return next(r for r in cur.rows.values() if r["text"] == text)


class NamedThirdPartyTest(unittest.TestCase):
    """A named third party as the subject owns the item, whoever says it, in
    a job and in the owner's own conversation alike."""

    def test_the_orchestrator_must_resolve_the_rebase_conflict_is_agent(self):
        text = "The orchestrator must resolve the rebase conflict"
        self.assertEqual(co.resolve_owner_and_label(text, origin="job"), ("agent", "orchestrator"))
        cur = _OwnerCursor()
        self.assertEqual(_open(cur, JOB, text), 1)
        row = _by_text(cur, text)
        self.assertEqual((row["owner"], row["owner_label"]), ("agent", "orchestrator"))

    def test_pr_author_to_renumber_migration_is_agent(self):
        text = "PR #234 author to renumber migration 115"
        self.assertEqual(co.resolve_owner_and_label(text, origin="job"), ("agent", "PR author"))
        cur = _OwnerCursor()
        self.assertEqual(_open(cur, JOB, text), 1)
        row = _by_text(cur, text)
        self.assertEqual((row["owner"], row["owner_label"]), ("agent", "PR author"))

    def test_the_owner_of_a_pr_is_the_pr_author_not_the_user(self):
        self.assertEqual(co.resolve_owner_and_label("The owner of PR #12 to rebase onto main", origin="job"),
                         ("agent", "PR author"))

    def test_in_a_conversation_the_named_third_party_decides(self):
        text = "The orchestrator must resolve the rebase conflict"
        cur = _OwnerCursor()
        self.assertEqual(_open(cur, CONVERSATION, {"text": text, "kind": "blocker", "owner": "other",
                                                   "owner_label": "orchestrator"}), 1)
        row = _by_text(cur, text)
        self.assertEqual((row["owner"], row["owner_label"]), ("agent", "orchestrator"))
        self.assertEqual(co.resolve_owner_and_label(text, declared="user"), ("agent", "orchestrator"))


class JobRulesTest(unittest.TestCase):
    def test_a_jobs_unnamed_question_is_the_orchestrators(self):
        text = "Should the export include archived rows or only live ones?"
        cur = _OwnerCursor()
        _open(cur, JOB, {"text": text, "kind": "question", "owner": "user"})
        row = _by_text(cur, text)
        self.assertEqual((row["owner"], row["owner_label"], row["origin"]),
                         ("agent", "orchestrator", "job"))

    def test_a_jobs_matt_to_decide_is_the_orchestrators(self):
        text = "Matt to decide whether 0.3.17 ships with the seal fix or holds"
        cur = _OwnerCursor()
        _open(cur, JOB, {"text": text, "kind": "followup", "owner": "user"})
        row = _by_text(cur, text)
        self.assertEqual((row["owner"], row["owner_label"]), ("agent", "orchestrator"))

    def test_khipu_job_marks_a_capture_without_lineage_as_a_job(self):
        payload = {"project": "acme/widget", "session_id": "codex:abc", "session_origin": "job"}
        self.assertEqual(co.capture_origin(payload), "job")
        self.assertEqual(co.capture_origin(CONVERSATION), "conversation")
        self.assertEqual(co.capture_origin(JOB), "job")

    def test_a_conversations_question_stays_the_users(self):
        text = "Should the export include archived rows or only live ones?"
        cur = _OwnerCursor()
        _open(cur, CONVERSATION, {"text": text, "kind": "question", "owner": "user"})
        row = _by_text(cur, text)
        self.assertEqual((row["owner"], row["owner_label"], row["origin"]),
                         ("user", None, "conversation"))

    def test_no_item_is_dropped_counts_in_equal_counts_out(self):
        loops = [
            {"text": "Matt to decide whether the release ships with the seal fix", "kind": "question"},
            {"text": "The orchestrator must resolve the rebase conflict", "kind": "blocker"},
            {"text": "Approve the AGPL CLA wording", "kind": "followup", "owner": "user"},
            {"text": "Send Matt the restore-drill numbers next session", "kind": "promise",
             "owner": "assistant"},
            {"text": "Which port should the gateway listen on?", "kind": "question"},
        ]
        for base, origin in ((JOB, "job"), (CONVERSATION, "conversation")):
            cur = _OwnerCursor()
            self.assertEqual(_open(cur, base, *loops), len(loops))
            self.assertEqual({r["origin"] for r in cur.rows.values()}, {origin})
            # Read back: every row is listed, none filtered out by its owner.
            listed = co.list_owed(cur, project="acme/widget", limit=None)
            self.assertEqual(len(listed), len(loops))
        # From a job nothing stays the user's; a promise stays the job's own.
        owners = {r["text"]: r["owner"] for r in co.list_owed(_seeded_job_cursor(loops),
                                                              project="acme/widget", limit=None)}
        self.assertNotIn("user", owners.values())
        self.assertEqual(owners["Send Matt the restore-drill numbers next session"], "assistant")


def _seeded_job_cursor(loops):
    cur = _OwnerCursor()
    _open(cur, JOB, *loops)
    return cur


class RelayTest(unittest.TestCase):
    def test_a_conversation_item_closes_the_matching_job_item_not_itself(self):
        cur = _OwnerCursor()
        job_text = "Matt to decide whether the export includes archived rows"
        _open(cur, JOB, {"text": job_text, "kind": "question"}, episode=1)
        conv_text = "Matt to decide whether the export includes archived rows or not"
        self.assertEqual(_open(cur, CONVERSATION, {"text": conv_text, "kind": "question"}, episode=2), 1)
        job_row, conv_row = _by_text(cur, job_text), _by_text(cur, conv_text)
        self.assertEqual(conv_row["status"], "open")
        self.assertEqual(conv_row["owner"], "user")
        self.assertEqual(job_row["status"], "closed")
        self.assertEqual(job_row["close_reason"], f"relayed-by {conv_row['id']}")

    def test_an_identical_relay_closes_the_job_item_and_opens_the_users(self):
        cur = _OwnerCursor()
        text = "Matt to decide whether the export includes archived rows"
        _open(cur, JOB, {"text": text, "kind": "question"}, episode=1)
        self.assertEqual(_open(cur, CONVERSATION, {"text": text, "kind": "question"}, episode=2), 1)
        rows = sorted(cur.rows.values(), key=lambda r: r["id"])
        self.assertEqual([r["status"] for r in rows], ["closed", "open"])
        self.assertEqual(rows[0]["close_reason"], f"relayed-by {rows[1]['id']}")
        self.assertEqual(rows[1]["origin"], "conversation")

    def test_a_job_item_never_closes_a_conversation_item(self):
        cur = _OwnerCursor()
        conv_text = "Matt to decide whether the export includes archived rows"
        _open(cur, CONVERSATION, {"text": conv_text, "kind": "question"}, episode=1)
        _open(cur, JOB, {"text": conv_text + " or not", "kind": "question"}, episode=2)
        self.assertEqual(_by_text(cur, conv_text)["status"], "open")
        self.assertFalse(any(r["status"] == "closed" for r in cur.rows.values()))


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

    stored = {} if cap is None else {"owed_orchestrator_slice_max": cap}
    with mock.patch.object(activity, "connect", return_value=_NullConn()), \
         mock.patch("khipu.commitments.list_owed", return_value=[dict(r) for r in rows]), \
         mock.patch("khipu.decisions.standing_decisions", return_value=[]), \
         mock.patch("khipu.embed._episode_schema_flags",
                    return_value={"project": True, "deleted_at": True, "parent_session_id": True}), \
         mock.patch.object(config, "load_config", return_value=stored):
        return activity.project_slice(project="acme/widget", job=job)


class SessionStartSliceTest(unittest.TestCase):
    ROWS = ([_owed_row(i, f"Approve release candidate number {i}", "user") for i in (1, 2)]
            + [_owed_row(10 + i, f"Rebase the PR #{i} stack onto main", "agent", label="orchestrator",
                         origin="job") for i in range(5)])

    def test_a_conversation_sees_at_most_three_orchestrator_items_marked(self):
        from khipu import recall_rule

        data = _slice(self.ROWS)
        agents = [c for c in data["commitments"] if c["owner"] == "agent"]
        self.assertEqual(len(agents), 3)
        text = recall_rule._render_project_slice("acme/widget", data)
        items = [line for line in text.splitlines() if line.startswith("- [")]
        self.assertEqual(sum("(for the orchestrator)" in line for line in items), 3)
        self.assertEqual(sum("(needs the user)" in line for line in items), 2)
        for line in text.splitlines():
            if "PR #" in line:
                self.assertNotIn("needs the user", line)

    def test_a_job_session_sees_no_orchestrator_items(self):
        data = _slice(self.ROWS, job=True)
        self.assertEqual([c["owner"] for c in data["commitments"]], ["user", "user"])

    def test_zero_lifts_the_orchestrator_cap(self):
        data = _slice(self.ROWS, cap=0)
        self.assertEqual(sum(c["owner"] == "agent" for c in data["commitments"]), 5)

    def test_the_cap_is_a_count_setting_with_a_default_of_three(self):
        from khipu import config

        with mock.patch.object(config, "load_config", return_value={}):
            self.assertEqual(config.count_setting("owed_orchestrator_slice_max"), 3)
        with self.assertRaises(ValueError):
            config.parse_count_setting("owed_orchestrator_slice_max", "-1")


class WaitingOnYouTest(unittest.TestCase):
    def test_waiting_on_you_counts_only_user_items(self):
        from khipu import hub_snapshot, work

        cur = _OwnerCursor()
        _open(cur, JOB, {"text": "Matt to decide whether the release ships with the seal fix",
                         "kind": "question"}, episode=1)
        _open(cur, JOB, {"text": "The orchestrator must resolve the rebase conflict",
                         "kind": "blocker"}, episode=1)
        _open(cur, CONVERSATION, {"text": "Matt must supply the second Mac's hostname",
                                  "kind": "blocker"}, episode=2)

        class Conn:
            def cursor(self):
                class Ctx:
                    def __enter__(self_inner):
                        return cur

                    def __exit__(self_inner, *a):
                        return False
                return Ctx()

            def close(self):
                pass

        with mock.patch.object(hub_snapshot, "try_hub_connect", lambda **kw: Conn()), \
             mock.patch.object(co, "_not_forgotten_clause", lambda cur: "TRUE"):
            got = work.read_owed()
        self.assertIsNone(got["error"])
        self.assertEqual([i["text"] for i in got["items"]], ["Matt must supply the second Mac's hostname"])


class ExtractorPromptTest(unittest.TestCase):
    def test_the_prompt_asks_for_other_and_owner_label(self):
        from khipu import extract

        self.assertIn('"other"', extract.PROMPT)
        self.assertIn("owner_label", extract.PROMPT)

    def test_a_job_transcript_is_told_it_is_a_job(self):
        from khipu import extract

        with mock.patch("khipu.features.enabled", return_value=False):
            plain = extract._build_prompt(cwd="/x", transcript="t")
            job = extract._build_prompt(cwd="/x", transcript="t", job=True)
        self.assertEqual(plain, extract.PROMPT.format(cwd="/x", transcript="t"))
        self.assertIn("dispatched job", job)
        self.assertNotIn("dispatched job", plain)

    def test_owner_label_rides_through_and_the_rule_still_wins(self):
        from khipu import extract

        loops = extract._as_open_loops([{"text": "Reviewer to check the migration order",
                                         "kind": "followup", "owner": "other",
                                         "owner_label": "reviewer"}])
        self.assertEqual(loops[0]["owner_label"], "reviewer")
        # A model's "other" with no rule signal keeps its label; a rule signal wins.
        self.assertEqual(co.resolve_owner_and_label("Check the migration order before tagging",
                                                    declared="other", declared_label="reviewer"),
                         ("agent", "reviewer"))
        self.assertEqual(co.resolve_owner_and_label("Matt must approve the migration order",
                                                    declared="other", declared_label="reviewer"),
                         ("user", None))


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
                    mock.patch("khipu.capture.capture", lambda p, mode=None: captured.append(p) or 0), \
                    mock.patch("khipu.config.capture_mode", lambda: "dual"):
                ac.drain()
        self.assertEqual(calls, [{"job": True}])
        self.assertEqual(co.capture_origin(captured[0]), "job")


# ---- a real hub without migration 0033 -------------------------------------

def _episode(cur) -> int:
    cur.execute("INSERT INTO episodes (ts, summary, raw) VALUES (now(), 's', %s::jsonb) RETURNING id",
                (json.dumps({}),))
    return int(cur.fetchone()[0])


def _pg_open(cur, base, text, kind, episode):
    with mock.patch.object(co, "_cosine_scores", _no_cosine), \
         mock.patch.object(co, "_has_commitment_embeddings", lambda cur, ids: False):
        return co.open_from_episode(cur, dict(base, open_loops=[{"text": text, "kind": kind}]), episode)


def test_a_pre_migration_hub_still_answers_reads_and_writes(hub):  # noqa: F811
    from khipu import db

    conn = hub.connect()
    with conn.cursor() as cur:
        assert not db.has_columns(cur, "commitments", "origin")
        ep = _episode(cur)
        assert _pg_open(cur, JOB, "Matt to decide whether the release ships with the seal fix",
                        "question", ep) == 1
        assert _pg_open(cur, CONVERSATION, "Matt must supply the second Mac's hostname", "blocker", ep) == 1
        rows = co.list_owed(cur, project="acme/widget", limit=None)
        assert sorted(r["owner"] for r in rows) == ["agent", "user"]
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    hub.migrate()
    # Safe to run twice.
    with conn.cursor() as cur:
        cur.execute(migration_sql("0033_commitment_owner.sql"))
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    with conn.cursor() as cur:
        assert db.has_columns(cur, "commitments", "origin", "owner_label")
        ep = _episode(cur)
        assert _pg_open(cur, JOB, "The orchestrator must resolve the rebase conflict", "blocker", ep) == 1
        cur.execute("SELECT origin, owner, owner_label FROM commitments ORDER BY id")
        assert cur.fetchall() == [("conversation", "agent", None), ("conversation", "user", None),
                                  ("job", "agent", "orchestrator")]
        rows = co.list_owed(cur, project="acme/widget", limit=None)
        assert len(rows) == 3
    conn.commit()


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


@pytest.mark.parametrize("text,kind,declared,label,origin,expected", _LINEAGE_CASES)
def test_the_owner_comes_from_the_session_not_the_words(text, kind, declared, label, origin, expected):
    monkey = {"KHIPU_USER_ALIASES": "Matt"}
    with mock.patch.dict(os.environ, monkey):
        co.reset_user_patterns()
        try:
            assert co.resolve_owner_and_label(text, kind=kind, declared=declared, origin=origin,
                                              declared_label=label) == expected
            cur = _OwnerCursor()
            loop = {"text": text, "kind": kind, **({"owner": declared} if declared else {}),
                    **({"owner_label": label} if label else {})}
            assert _open(cur, JOB if origin == "job" else CONVERSATION, loop) == 1
            row = _by_text(cur, text)
            assert (row["owner"], row.get("owner_label")) == expected
        finally:
            co.reset_user_patterns()


# ---- the third cross-family review's probes ---------------------------------

def test_real_owner_conditional_i_will_remains_the_owner():
    text = "When the reviewer finishes, I will test the installation on my phone"
    with mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "Matt"}):
        co.reset_user_patterns()
        assert co.has_future_trigger(text)
        assert co.rejection_reason(text, owner="user", kind="promise") is None
        assert co.resolve_owner_and_label(text, kind="promise", declared="user", origin="conversation") == (
            "user", None)


def test_an_assistant_duplicate_cannot_swallow_the_owners_new_promise():
    with mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "Matt"}):
        co.reset_user_patterns()
        cur = _OwnerCursor()
        _open(cur, CONVERSATION, {"text": "Review the deployment checklist before release", "kind": "followup",
                                  "owner": "assistant"})
        _open(cur, CONVERSATION, {"text": "I will review the deployment checklist before release",
                                  "kind": "promise", "owner": "user"}, episode=2)
        user_rows = [r for r in co.list_owed(cur, project="acme/widget", limit=None) if r["owner"] == "user"]
        assert user_rows, "The owner made a real promise, but dedup kept only an assistant-owned row"
        assert any("I will" in r["text"] for r in user_rows), "His promise text must survive"


def test_reviewer_work_in_a_conversation_must_not_die_with_the_session(monkeypatch):
    monkeypatch.setenv("KHIPU_USER_ALIASES", "Matt")
    co.reset_user_patterns()
    text = "The reviewer must rebase the release branch"
    cur = _OwnerCursor()
    _open(cur, CONVERSATION, {"text": text, "kind": "followup"})
    monkeypatch.setattr(co, "_session_open_commitments", lambda cur, sid: list(cur.rows.values()))
    closed = co.close_session_plan(cur, dict(CONVERSATION, event="sessionend"), episode_id=2)
    assert closed == 0, "A third party still owes this work after the conversation ends"
    assert next(iter(cur.rows.values()))["status"] == "open"


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


def test_similar_items_of_different_owners_both_stay_and_same_owner_restatements_fold():
    cur = _OwnerCursor()
    _open(cur, CONVERSATION, {"text": "Review the deployment checklist before release", "kind": "followup",
                              "owner": "assistant"})
    # The same words, the owner's own: a second row, not a fold.
    assert _open(cur, CONVERSATION, {"text": "Review the deployment checklist before release",
                                     "kind": "followup", "owner": "user"}, episode=2) == 1
    owners = sorted(r["owner"] for r in cur.rows.values())
    assert owners == ["assistant", "user"]
    # The assistant restating its own item folds into it.
    assert _open(cur, CONVERSATION, {"text": "Review the deployment checklist before the release",
                                     "kind": "followup", "owner": "assistant"}, episode=3) == 0
    assert len(cur.rows) == 2


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
            episode, reason, by, key, evidence, cid, checked_key = params
            row = self.rows[cid]
            self.rowcount = int(row["status"] == "open" and checked_key not in row.get("reopened_rule_keys", []))
            if self.rowcount:
                row.update(status="closed", closed_episode=episode, close_reason=reason, closed_by=by,
                           close_rule_key=key)
            return
        if s.startswith("UPDATE commitments SET status = %s"):
            status, reason, by, cid = params
            self.rows[cid].update(status=status, close_reason=reason, closed_by=by, close_rule_key=None)
            self.rowcount = 1
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


def test_reopened_job_relay_does_not_swallow_a_renewed_owner_question(monkeypatch):
    monkeypatch.setattr(co, "_close_columns_ready", lambda cur: True)
    monkeypatch.setattr(co, "_lineage_columns_ready", lambda cur: False)
    monkeypatch.setattr(co, "_reassigned_column_ready", lambda cur: False)
    cur = _RelayCursor()
    text = "Matt to decide whether the export includes archived rows"
    assert _open(cur, JOB, dict(text=text, kind="question", owner="user")) == 1
    assert _open(cur, CONVERSATION, dict(text=text, kind="question", owner="user"), episode=2) == 1
    assert cur.rows[1]["status"] == "closed"
    assert co.set_status(cur, 2, "closed", reason="Owner closed the relayed item", by=co.BY_APP)
    assert co.set_status(cur, 1, "open", by=co.BY_APP)
    assert cur.rows[1]["reopened_rule_keys"] == [co.evidence_key("relayed-by", text)]
    inserted = _open(cur, CONVERSATION, dict(text=text, kind="question", owner="user"), episode=4)
    own = [r for r in cur.rows.values() if r["owner"] == "user" and r["status"] == "open"]
    assert inserted == 1 and own, f"the owner question was swallowed: inserted={inserted}, rows={cur.rows}"
    # The reopened job item stays open beside it.
    assert cur.rows[1]["status"] == "open"


def test_a_renewed_owner_question_after_a_reopened_relay_is_visible_on_the_hub(hub):  # noqa: F811
    from khipu import db

    conn = hub.connect()
    hub.migrate(conn=conn)
    db._TABLE_COLUMNS_CACHE.clear()
    text = "Matt to decide whether the export includes archived rows"

    def _ep(cur, n):
        cur.execute("INSERT INTO episodes (ts, summary, raw) VALUES (now(), %s, '{}'::jsonb) RETURNING id",
                    (f"episode {n}",))
        return int(cur.fetchone()[0])

    with conn.cursor() as cur:
        assert _pg_open(cur, JOB, text, "question", _ep(cur, 1)) == 1
        assert _pg_open(cur, CONVERSATION, text, "question", _ep(cur, 2)) == 1
        cur.execute("SELECT id, origin, status FROM commitments ORDER BY id")
        (job_id, _, job_status), (conv_id, _, _) = cur.fetchall()
        assert job_status == "closed"
        assert co.set_status(cur, conv_id, "closed", reason="answered", by=co.BY_APP)
        assert co.set_status(cur, job_id, "open", by=co.BY_APP)
        assert _pg_open(cur, CONVERSATION, text, "question", _ep(cur, 3)) == 1
    conn.commit()
    with conn.cursor() as cur:
        rows = co.list_owed(cur, project="acme/widget", limit=None)
        mine = [r for r in rows if r["owner"] == co.OWNER_USER and r["text"] == text]
        assert len(mine) == 1, rows
        assert any(r["id"] == job_id for r in rows), "the reopened job item stays open too"
