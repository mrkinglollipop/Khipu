"""Tests for khipu.commitments — W3 first-class open loops.

No live database: a small fake cursor backs an in-memory commitments table
so historical commitment capture / historical closing / historical aging / list_owed / set_status can
be exercised deterministically. Cosine matching is forced to fail (no key in
tests) so historical closing falls back to the pure-Python Jaccard path, same as the
capture.py dedup tests.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from khipu import commitments as co

# The owner/actor regexes are alias-driven (2026-09-05): "Matt" is only
# recognised as the user when "matt" is a configured alias — it is no
# longer a hardcoded name. Every fixture in this file was written when
# "matt" WAS hardcoded, so pin it here for the whole module (mirrors how a
# real deployment would run `khipu config --set user_aliases matt`) and
# reset the compiled patterns on the way in and out so no other test module
# inherits this module's alias configuration.
_alias_env_patch = mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "matt"})


def setUpModule():
    _alias_env_patch.start()
    co.reset_user_patterns()


def tearDownModule():
    _alias_env_patch.stop()
    co.reset_user_patterns()


_CAPTURE = object()  # `_seed(owner=...)`: store the owner capture would decide


class _CommitmentsCursor:
    """In-memory stand-in for the commitments table.

    ``migrated`` says whether migration 0012 (last_seen_at / seen_count) has
    been applied on this fake hub, and ``trigger`` whether 0013
    (future_trigger) has — both branches of both are exercised, because a
    pre-migration hub must keep working (the code gates every write on
    ``db.has_columns`` and derives the field from the text when reading).
    """

    def __init__(self, *, migrated: bool = False, trigger: bool = False,
                 trigger_text: bool = False):
        self.rows: dict[int, dict] = {}
        self.episode_sessions: dict[int, str] = {}
        self.migrated = migrated
        self.trigger = trigger
        self.trigger_text = trigger_text
        self.next_id = 1
        self.rowcount = 0
        self.statements: list[str] = []
        self._result: list[tuple] = []
        # db.table_columns caches per process; a fake hub in another test must
        # not decide this one's schema.
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.pop("commitments", None)

    def _seed(self, text, *, project="acme/widget", kind="followup", owner=_CAPTURE,
              episode=1, session_id=None, status="open", future_trigger=False,
              due_after=None, opened_at="t0"):
        """Insert a row WITHOUT going through historical commitment capture's filter — for
        tests about historical closing / stale / listing, whose fixtures predate the
        precision filter and are not what those tests are about. The stored
        owner is the one capture would have decided for the text, unless the
        test gives one (``None`` stores none, like a legacy row)."""
        if owner is _CAPTURE:
            owner = co.resolve_owner(text, kind=kind)
        cid = self.next_id
        self.next_id += 1
        self.rows[cid] = {
            "id": cid, "text": text, "project": project, "owner": owner, "kind": kind,
            "opened_episode": episode, "opened_at": opened_at, "due_after": due_after,
            "status": status, "closed_episode": None, "closed_at": None,
            "close_reason": None, "content_hash": co.content_hash(project, text),
            "last_seen_at": None, "seen_count": 1, "future_trigger": future_trigger,
        }
        if session_id:
            self.episode_sessions[episode] = session_id
        return cid

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.statements.append(s)
        params = params or ()
        # No aliases are seeded: the canonical-group left join returns the
        # supplied project and a null alias inside the reader's savepoint.
        if s.startswith(("SAVEPOINT ", "RELEASE SAVEPOINT ", "ROLLBACK TO SAVEPOINT ")):
            return
        if s.startswith("WITH c AS (SELECT COALESCE((SELECT project FROM project_aliases"):
            project, fallback = params
            assert project == fallback
            self._result = [(project, None)]
            return
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            cols = ["id", "text", "project", "owner", "kind", "opened_episode",
                    "opened_at", "due_after", "status", "closed_episode",
                    "closed_at", "close_reason", "content_hash"]
            if self.migrated:
                cols += ["last_seen_at", "seen_count"]
            if self.trigger:
                cols += ["future_trigger"]
            if self.trigger_text:
                cols += ["trigger_text"]
            self._result = [(c,) for c in cols]
            return
        if s.startswith("INSERT INTO commitments"):
            # due_after is spliced into the SQL text itself (fix 1): either a
            # bound "%s::timestamptz" placeholder, or a literal NULL / "now()
            # + interval '...'" with no placeholder at all — so the param
            # count varies with what the SQL contains.
            rest = list(params)
            trigger_text = rest.pop() if "trigger_text" in s else None
            future_trigger = bool(rest.pop()) if "future_trigger" in s else False
            if "%s::timestamptz" in s:
                text, project, owner, kind, opened_episode, due_after, h = rest
            else:
                text, project, owner, kind, opened_episode, h = rest
                due_after = "interval" if "now() + interval" in s else None
            dup = any(
                r["project"] == project and r["content_hash"] == h and r["status"] == "open"
                for r in self.rows.values()
            )
            if dup:
                self.rowcount = 0
                return
            cid = self.next_id
            self.next_id += 1
            self.rows[cid] = {
                "id": cid, "text": text, "project": project, "owner": owner, "kind": kind,
                "opened_episode": opened_episode, "opened_at": "t0", "due_after": due_after,
                "status": "open", "closed_episode": None, "closed_at": None,
                "close_reason": None, "content_hash": h,
                "last_seen_at": None, "seen_count": 1,
                "future_trigger": future_trigger, "trigger_text": trigger_text,
            }
            self.rowcount = 1
            return
        if s.startswith("SELECT id FROM commitments WHERE status = 'open' AND content_hash"):
            h, project = params
            hit = [
                r["id"] for r in self.rows.values()
                if r["project"] == project and r["content_hash"] == h and r["status"] == "open"
            ]
            self._result = [(hit[0],)] if hit else []
            return
        if s.startswith("SELECT id, text") and " FROM commitments WHERE status = 'open' AND project" in s:
            # The open items of a scope, with whatever columns were asked for.
            project = params[0]
            cols = s[len("SELECT "):s.index(" FROM commitments")].split(", ")
            defaults = {"origin": "conversation"}
            self._result = [
                tuple(r.get(c) if r.get(c) is not None else defaults.get(c) for c in cols)
                for r in self.rows.values()
                if r["status"] == "open" and r["project"] == project
            ]
            return
        if s.startswith("SELECT c.id, c.text, c.kind, c.owner") and "FROM commitments c JOIN episodes" in s:
            (session_id,) = params
            want_seen = "c.last_seen_at" in s
            want_latest = "c.latest_text" in s
            out = []
            for r in self.rows.values():
                if r["status"] != "open":
                    continue
                if self.episode_sessions.get(r["opened_episode"]) != session_id:
                    continue
                row = [r["id"], r["text"], r["kind"], r["owner"]]
                if want_latest:
                    row.append(r.get("latest_text"))
                if want_seen:
                    row.extend([r.get("last_seen_at"), r.get("seen_count") or 1])
                out.append(tuple(row))
            self._result = out
            return
        if s.startswith("UPDATE commitments SET last_seen_at = now(), seen_count"):
            (cid,) = params
            r = self.rows.get(cid)
            if r and r["status"] == "open":
                r["last_seen_at"] = "now"
                r["seen_count"] = int(r.get("seen_count") or 1) + 1
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if s.startswith("SELECT id, text, owner, kind, status") and "WHERE id = %s" in s and "FOR UPDATE" in s:
            # commitments.row_at_write: one item, as it is now.
            cols = s[len("SELECT "):s.index(" FROM commitments")].split(", ")
            r = self.rows.get(params[0])
            self._result = [tuple(r.get(c) for c in cols)] if r else []
            return
        if s.startswith("UPDATE commitments") and "SET status = 'closed'" in s:
            # The stored owner the write read must still be the stored one.
            episode_id, close_reason, cid, *same = params
            r = self.rows.get(cid)
            if r and r["status"] == "open" and (not same or same[0] == r["owner"]):
                r["status"] = "closed"
                r["closed_episode"] = episode_id
                r["close_reason"] = close_reason
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if s.startswith("SELECT id FROM commitments WHERE") and "due_after <= now()" in s:
            # historical aging's candidates; this fake ignores the silence window.
            self._result = [(r["id"],) for r in self.rows.values() if r["status"] == "open"]
            return
        if s.startswith("UPDATE commitments") and "'stale'" in s:
            cid, *same = params
            r = self.rows.get(cid)
            self.rowcount = 0
            if r and r["status"] == "open" and (not same or same[0] == r["owner"]):
                r["status"] = "stale"
                self.rowcount = 1
            return
        if s.startswith("SELECT id, text, project, owner, kind"):
            status = params[0]
            rest = params[1:-1]
            project = rest[0] if rest else None
            out = [r for r in self.rows.values() if r["status"] == status]
            if project:
                out = [r for r in out if r["project"] == project]
            if "due_after IS NULL OR due_after <= now()" in s:
                from datetime import datetime, timezone

                def _not_snoozed(r):
                    da = r["due_after"]
                    if not da:
                        return True
                    try:
                        when = datetime.fromisoformat(str(da).replace("Z", "+00:00"))
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=timezone.utc)
                        return when <= datetime.now(timezone.utc)
                    except Exception:
                        return True

                out = [r for r in out if _not_snoozed(r)]
            wide = "last_seen_at" in s
            with_trigger = "future_trigger" in s
            self._result = [
                tuple(
                    [r["id"], r["text"], r["project"], r["owner"], r["kind"],
                     r["opened_episode"], r["opened_at"], r["due_after"], r["status"],
                     r["closed_episode"], r["closed_at"], r["close_reason"]]
                    + ([r["last_seen_at"], r["seen_count"]] if wide else [])
                    + ([r["future_trigger"]] if with_trigger else [])
                )
                for r in out
            ]
            return
        if s.startswith("UPDATE commitments SET status = 'open'"):
            (cid,) = params
            r = self.rows.get(cid)
            if r:
                r["status"] = "open"
                r["closed_episode"] = None
                r["closed_at"] = None
                r["close_reason"] = None
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if s.startswith("UPDATE commitments SET status = %s"):
            status, *reason, cid = params
            r = self.rows.get(cid)
            if r:
                r["status"] = status
                if reason:
                    r["close_reason"] = reason[0]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


def _no_cosine(cur, text, ids):
    return {}


def _has_no_embeddings(cur, ids):
    return False


def _has_embeddings(cur, ids):
    return True


class OpenFromEpisodeTest(unittest.TestCase):
    pass

class DueAfterParsingTest(unittest.TestCase):
    """fix 1: due_after is free text from the model — lenient parsing, and
    NEVER bind unparseable text into the timestamptz column (that used to
    raise InvalidDatetimeFormat and kill the whole commitments step,
    reproduced live on episode 11308)."""

    def test_iso_date_parses_to_a_bound_timestamptz_param(self):
        sql, param = co._parse_due_after("2026-09-10")
        self.assertEqual(sql, "%s::timestamptz")
        self.assertEqual(param, "2026-09-10")

    def test_full_iso_8601_with_z_parses(self):
        sql, param = co._parse_due_after("2026-09-10T12:00:00Z")
        self.assertEqual(sql, "%s::timestamptz")
        self.assertEqual(param, "2026-09-10T12:00:00Z")

    def test_bare_n_days_becomes_a_sql_interval_not_a_python_value(self):
        sql, param = co._parse_due_after("3 days")
        self.assertIn("now() + interval", sql)
        self.assertIn("3 days", sql)
        self.assertIsNone(param)

    def test_in_n_days_phrasing_also_parses(self):
        sql, param = co._parse_due_after("in 10 days")
        self.assertIn("now() + interval", sql)
        self.assertIn("10 days", sql)
        self.assertIsNone(param)

    def test_weeks_and_months_units(self):
        sql, _ = co._parse_due_after("2 weeks")
        self.assertIn("2 weeks", sql)
        sql, _ = co._parse_due_after("1 month")
        self.assertIn("1 months", sql)

    def test_free_text_and_empty_both_degrade_to_null(self):
        self.assertEqual(co._parse_due_after("next week"), ("NULL", None))
        self.assertEqual(co._parse_due_after("after the release"), ("NULL", None))
        self.assertEqual(co._parse_due_after(""), ("NULL", None))
        self.assertEqual(co._parse_due_after(None), ("NULL", None))


class ScopeCoalescingTest(unittest.TestCase):
    """K6 (2026-09-14): the scope key is the episode's resolved ``project``
    ONLY — a session id is not a project, so open/dedup/historical closing/list never
    fall back to parent_session_id/session_id any more. A NULL project stays
    NULL (real behaviour: a capture with no resolved project groups with
    every other such capture, not with itself alone by session lineage)."""


    def test_list_owed_accepts_parent_session_id_as_the_scope_key(self):
        from tests.test_audit1010_rsna import _RegistryCursor

        cur = _RegistryCursor()
        self.addCleanup(cur.conn.close)
        cur.episode(1, "child-session", "claude_code:host-1")
        cur.commitment("a", project=None)
        rows = co.list_owed(cur, parent_session_id="claude_code:host-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "a")



class AutoCloseTest(unittest.TestCase):
    def _open(self, cur, text, project="acme/widget", episode=1):
        cur._seed(text, project=project, episode=episode)


class MarkStaleTest(unittest.TestCase):
    pass

class ListOwedAndSetStatusTest(unittest.TestCase):
    def test_list_owed_filters_by_project_and_status(self):
        cur = _CommitmentsCursor()
        cur._seed("a", project="acme/widget")
        cur._seed("b", project="other/repo")
        rows = co.list_owed(cur, project="acme/widget", status="open")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "a")

    def test_set_status_reopens_and_closes(self):
        cur = _CommitmentsCursor()
        cur._seed("a", project="p")
        cid = list(cur.rows.keys())[0]
        self.assertTrue(co.set_status(cur, cid, "closed"))
        self.assertEqual(cur.rows[cid]["status"], "closed")
        self.assertTrue(co.set_status(cur, cid, "open"))
        self.assertEqual(cur.rows[cid]["status"], "open")
        self.assertIsNone(cur.rows[cid]["closed_episode"])

    def test_set_status_rejects_bad_value(self):
        cur = _CommitmentsCursor()
        with self.assertRaises(ValueError):
            co.set_status(cur, 1, "bogus")


class ContentHashTest(unittest.TestCase):
    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(
            co.content_hash("p", "Follow  Up"),
            co.content_hash("p", "follow up"),
        )

    def test_different_project_different_hash(self):
        self.assertNotEqual(co.content_hash("p1", "x"), co.content_hash("p2", "x"))


# ===========================================================================
# Quality fixes (2026-09-04). Measured problem: 328 open commitments in 7
# days, dominated by in-flight status, the assistant's own same-session plan
# steps, coordination chatter and paraphrase restatements — Owed unusable.
# ===========================================================================


class PrecisionFilterTest(unittest.TestCase):
    """Item 1: the deterministic post-filter. Every REJECT string here is a
    real shape measured on the live hub."""

    def test_in_flight_status_is_rejected(self):
        for text in (
            "Drive 46 is still running",
            "Lease mechanism is running",
            "The nightly job is currently building the index",
            "Waiting on the second Mac before soak can start",
            "Awaiting the migration to finish",
            "Confirmation of the deploy is pending",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(co.rejection_reason(text), text)

    def test_agent_and_infra_chatter_is_kept_and_never_the_users(self):
        # Status chatter about an agent is still rejected as status.
        for text in (
            "Visual check agent's verdict is pending",
            "Wait for notifications from the background job",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(co.rejection_reason(text), text)
        # The agent shape never drops an item and never decides its owner:
        # the assistant's own stays the assistant's (and ends with its
        # session); a question goes by lineage (the orchestrator's in a job).
        for text in (
            "Receive report from the phase 1-2 agent",
            "Poll the drive for the finished export",
        ):
            with self.subTest(text=text):
                self.assertIsNone(co.rejection_reason(text), text)
                self.assertEqual(co.resolve_owner(text), co.OWNER_ASSISTANT)
                self.assertTrue(co.is_session_plan_text(text))
        self.assertEqual(co.resolve_owner_and_label("Did the background agent's report land?", origin="job"),
                         (co.OWNER_AGENT, co.ORCHESTRATOR))

    def test_coordination_messages_are_rejected(self):
        for text in (
            "Send 'screen free' message to Khipu session",
            "Ping the orchestrator session when the build lands",
            "Forward the handoff message to the other agent",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(co.rejection_reason(text), text)

    def test_vague_and_actionless_items_are_rejected(self):
        self.assertEqual(co.rejection_reason("the thing"), "too-short")
        self.assertEqual(co.rejection_reason("x"), "too-short")
        self.assertEqual(co.rejection_reason("the widget colour scheme overall"), "no-action")
        self.assertEqual(co.rejection_reason(""), "empty")

    def test_real_commitments_survive(self):
        for text in (
            "Matt to decide whether 0.3.17 ships with the seal fix",
            "Matt must supply the second Mac hostname before soak",
            "Send Matt the Linode restore-drill numbers next session",
            "Approve the AGPL CLA wording",
            "Renew the TLS cert for the hub before December",
        ):
            with self.subTest(text=text):
                self.assertIsNone(co.rejection_reason(text), text)



class OpenDedupTest(unittest.TestCase):
    """Item 2: zero EXACT duplicates were measured — the noise is paraphrase
    across successive captures of one session."""

    payload = {"project": "acme/widget", "open_loops": [
        "Check whether the seal fix ships in 0.3.17",
    ]}


class StaleRuleTest(unittest.TestCase):
    """Item 3: expiry by SILENCE, per kind, and never before a live due date."""







class SessionPlanClosureTest(unittest.TestCase):
    """Item 4: a session's own in-flight/plan items are retired when THAT
    session ends — no later capture ever says 'done: <phrase>' about them."""

    def _cur(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Drive 46 is still running", episode=7, session_id="claude:s1")
        cur._seed("Generate the UI mocks for the Owed screen", episode=7, session_id="claude:s1")
        return cur


class OwedOutputTest(unittest.TestCase):
    """Item 6: last_seen_at / seen_count / priority on every listed row."""

    def test_priority_ranks_user_owed_first_then_triggers_then_kind(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Matt must decide whether to ship", kind="blocker")       # user
        cur._seed("Matt must answer the licence query", kind="question")    # user
        cur._seed("Matt must merge the stack himself", kind="followup")     # user
        cur._seed("Send the numbers next session", kind="promise")          # trigger
        cur._seed("Rebuild the index", kind="blocker")                      # rest
        cur._seed("Rebuild the search index later", kind="followup")        # rest
        rows = co.list_owed(cur, project="acme/widget")
        by_text = {r["text"]: r["priority"] for r in rows}
        self.assertEqual(by_text["Matt must decide whether to ship"], 0)
        self.assertEqual(by_text["Matt must answer the licence query"], 1)
        self.assertEqual(by_text["Matt must merge the stack himself"], 2)
        self.assertEqual(by_text["Send the numbers next session"], 3)
        self.assertEqual(by_text["Rebuild the index"], 4)
        self.assertEqual(by_text["Rebuild the search index later"], 7)
        # and the list itself comes back in that order
        self.assertEqual([r["priority"] for r in rows], sorted(r["priority"] for r in rows))

    def test_owner_and_future_trigger_are_on_every_row(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Matt must merge the stack himself")
        cur._seed("Run oracle.sh once Matt says the lane is re-authed")
        rows = {r["text"]: r for r in co.list_owed(cur, project="acme/widget")}
        self.assertEqual(rows["Matt must merge the stack himself"]["owner"], "user")
        self.assertFalse(rows["Matt must merge the stack himself"]["future_trigger"])
        trig = rows["Run oracle.sh once Matt says the lane is re-authed"]
        self.assertEqual(trig["owner"], "assistant")
        self.assertTrue(trig["future_trigger"])

    def test_a_legacy_model_owner_is_normalised_not_echoed(self):
        """Rows opened before this change carry whatever the model said
        ("Peer 1", "ASSISTANT", nothing); the desktop's "Needs you" section
        is owner == 'user', so the listed value is always one of the owners,
        read from the stored value alone: a name that is no owner is the
        user's, the one default that no automatic rule acts on, and the text
        never overrules the stored owner."""
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Critique the draft plan for the app", owner="Peer 1")
        cur._seed("Matt reviews and merges the stack himself", owner="ASSISTANT")
        cur._seed("Rebuild the search index", owner=None)
        rows = {r["text"]: r["owner"] for r in co.list_owed(cur, project="acme/widget")}
        self.assertEqual(rows["Critique the draft plan for the app"], "user")
        self.assertEqual(rows["Matt reviews and merges the stack himself"], "assistant")
        self.assertEqual(rows["Rebuild the search index"], "user")

    def test_a_stored_future_trigger_column_is_read_when_present(self):
        cur = _CommitmentsCursor(migrated=True, trigger=True)
        cur._seed("Ship the packaging change", future_trigger=True)
        row = co.list_owed(cur, project="acme/widget")[0]
        self.assertTrue(row["future_trigger"])
        self.assertEqual(row["priority"], 3)

    def test_seen_fields_are_present_on_a_migrated_hub(self):
        cur = _CommitmentsCursor(migrated=True)
        cid = cur._seed("send Matt the restore numbers")
        cur.rows[cid]["seen_count"] = 4
        cur.rows[cid]["last_seen_at"] = "2026-09-04"
        row = co.list_owed(cur, project="acme/widget")[0]
        self.assertEqual(row["seen_count"], 4)
        self.assertEqual(row["last_seen_at"], "2026-09-04")

    def test_pre_migration_hub_gets_the_same_shape_with_defaults(self):
        cur = _CommitmentsCursor(migrated=False)
        cur._seed("send Matt the restore numbers")
        row = co.list_owed(cur, project="acme/widget")[0]
        self.assertIsNone(row["last_seen_at"])
        self.assertEqual(row["seen_count"], 1)
        self.assertEqual(row["owner"], "assistant")
        self.assertFalse(row["future_trigger"])
        self.assertEqual(row["priority"], 7)

    def test_existing_fields_are_unchanged(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("send Matt the restore numbers")
        row = co.list_owed(cur, project="acme/widget")[0]
        for field in ("id", "text", "project", "owner", "kind", "opened_episode",
                      "opened_at", "due_after", "status", "closed_episode",
                      "closed_at", "close_reason"):
            self.assertIn(field, row)


class OwnerInferenceTest(unittest.TestCase):
    """Item 1: every commitment gets an owner, decided deterministically.
    Matt's bar — the desktop's "Needs you" section is exactly owner == user,
    so a false positive there is as bad as a missed one."""

    def test_the_user_named_as_actor_is_user_owed(self):
        for text in (
            "Matt's action: merge Cursor PRs #859/#860.",
            "Matt reviews and merges the stack himself.",
            "Matt must supply the second Mac hostname before soak.",
            "User needs to quit the dev GUI or authorize killing PID 30401.",
            "Physical phone testing is still outstanding and owned by Matt.",
            "Ask Matt for the Linode credentials.",
        ):
            with self.subTest(text=text):
                self.assertEqual(co.resolve_owner(text), co.OWNER_USER, text)

    def test_a_decision_or_approval_owed_is_user_owed(self):
        for text in (
            "A decision is owed regarding Driver A, rather than another cycle.",
            "Decide on the remaining `.cursor/` tree in the public repo.",
            "Confirm whether USER or ASSISTANT runs the relay commands.",
            "Approve the AGPL CLA wording.",
            "The licence choice needs a decision before the release.",
        ):
            with self.subTest(text=text):
                self.assertEqual(co.resolve_owner(text), co.OWNER_USER, text)

    def test_a_question_is_user_owed(self):
        self.assertEqual(co.resolve_owner("Which lane should the release use?"), co.OWNER_USER)
        self.assertEqual(
            co.resolve_owner("Scope a rule for peer screen holds", kind="question"),
            co.OWNER_USER,
        )

    def test_assistant_work_is_not_user_owed_just_because_matt_is_mentioned(self):
        for text in (
            "Ensure `f2-matt-gates.md` is runnable by Matt, with all commands "
            "executed by the assistant.",
            "Implement JOB 4: add the fastlane Fastfile and run each lane once.",
            "Decide and implement ONE of the two inbox-scope options.",
            "Run an Opus live drive on a scratch build of main.",
        ):
            with self.subTest(text=text):
                self.assertEqual(co.resolve_owner(text), co.OWNER_ASSISTANT, text)

    def test_a_model_owner_is_only_a_fallback_and_is_normalised(self):
        # No deterministic signal: the model's word decides, mapped onto the
        # two values.
        self.assertEqual(co.resolve_owner("Rebuild the index", declared="Matt"), co.OWNER_USER)
        self.assertEqual(co.resolve_owner("Rebuild the index", declared="Peer 1"), co.OWNER_ASSISTANT)
        self.assertEqual(co.normalize_owner("ASSISTANT"), co.OWNER_ASSISTANT)
        self.assertIsNone(co.normalize_owner("Peer 1"))

    def test_the_deterministic_signal_beats_the_model(self):
        self.assertEqual(
            co.resolve_owner("Matt must merge the stack", declared="assistant"),
            co.OWNER_USER,
        )

    def test_a_name_inside_the_trigger_clause_does_not_make_it_user_owed(self):
        """"When Matt says the lane is re-authed, run oracle.sh" is the
        ASSISTANT's promise — Matt is the condition, not the actor."""
        text = ("When Matt says the xAI lane is re-authed, run `oracle.sh fast` "
                "once more and attach the green record to the task-5 PR.")
        self.assertTrue(co.has_future_trigger(text))
        self.assertEqual(co.resolve_owner(text, declared="Matt"), co.OWNER_ASSISTANT)


class FutureTriggerTest(unittest.TestCase):
    """Item 1: the explicit cross-session condition — the only thing that
    keeps an assistant promise alive past its own session."""

    def test_explicit_triggers_are_detected(self):
        for text in (
            "When Matt says the xAI lane is re-authed, run the oracle again.",
            "Send Matt the restore-drill numbers next session.",
            "Once the seal fix ships, re-run the notarisation check.",
            "Provide the paste-ready note after the oracle-speed wave merges.",
            "Investigate the SIGTERM source, especially if attempt six dies.",
            "Pick this back up in a future session.",
        ):
            with self.subTest(text=text):
                self.assertTrue(co.has_future_trigger(text), text)

    def test_same_session_sequencing_is_not_a_trigger(self):
        for text in (
            "Provide SHAs and evidence after completing Task 5c.",
            "Fix HIGH 1, before and after the mid-stream WS kill.",
            "Rebuild the index and measure the timings.",
            "Resume and measure W131.",
        ):
            with self.subTest(text=text):
                self.assertFalse(co.has_future_trigger(text), text)


class ReportingRuleTest(unittest.TestCase):
    """Item 3: within-session reporting duties are never durable — unless the
    USER is the one who owes the report."""

    REPORTS = (
        "Reply with the SHA each PR now points at and the oracle record path.",
        "Reply to Matt confirming compaction is done.",
        "Tell user the moment their app relaunches on the new build.",
        "Notify Matt when the install finishes.",
        "Provide SHAs, transcript row-count evidence, and anything not done.",
    )

    def test_assistant_reporting_duties_are_rejected(self):
        for text in self.REPORTS:
            with self.subTest(text=text):
                self.assertEqual(co.rejection_reason(text), "reporting", text)

    def test_report_back_is_reporting_even_though_an_older_rule_catches_it_first(self):
        text = "Report back with the four evidence paths."
        self.assertTrue(co.is_reporting_text(text))
        self.assertIsNotNone(co.rejection_reason(text))

    def test_the_same_text_survives_when_the_user_owes_it(self):
        text = "Matt to reply with the SHA each PR now points at."
        self.assertEqual(co.resolve_owner(text), co.OWNER_USER)
        self.assertIsNone(co.rejection_reason(text))

    def test_real_commitments_are_not_reporting(self):
        for text in (
            "Send Matt the Linode restore-drill numbers next session",
            "Renew the TLS cert for the hub before December",
            "Provide a paste-ready note for Cursor after the wave merges",
        ):
            with self.subTest(text=text):
                self.assertIsNone(co.rejection_reason(text), text)



class OpenStoresOwnerAndTriggerTest(unittest.TestCase):
    pass

class DroppedStatusTest(unittest.TestCase):
    def test_set_status_accepts_dropped_and_still_rejects_junk(self):
        cur = _CommitmentsCursor(migrated=True)
        cid = cur._seed("send Matt the restore numbers")
        self.assertTrue(co.set_status(cur, cid, "dropped"))
        self.assertEqual(cur.rows[cid]["status"], "dropped")
        self.assertTrue(co.set_status(cur, cid, "open"))
        with self.assertRaises(ValueError):
            co.set_status(cur, cid, "bogus")


class ConfigurableUserAliasesTest(unittest.TestCase):
    """USER_OWNERS / the actor and decision regexes are built from
    `commitments.user_aliases()`, never a hardcoded name — this is a public
    product and other users have other names (2026-09-05)."""

    def tearDown(self):
        # Every other test in this module runs with KHIPU_USER_ALIASES=matt
        # (see setUpModule); restore that after each override here.
        co.reset_user_patterns()

    def test_a_configured_alias_other_than_matt_is_recognised(self):
        with mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "priya"}):
            co.reset_user_patterns()
            self.assertEqual(
                co.resolve_owner("Priya to decide whether the release ships"),
                co.OWNER_USER,
            )

    def test_matt_stays_the_user_and_a_configured_alias_adds_to_him(self):
        with mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "priya"}):
            co.reset_user_patterns()
            # Matt's names are built in (the same on every Mac); a name no one
            # configured is a third party named as the subject, an agent's item.
            self.assertEqual(co.resolve_owner_and_label("Matt to decide whether the release ships"),
                             (co.OWNER_USER, None))
            self.assertEqual(co.resolve_owner_and_label("Sam to decide whether the release ships"),
                             (co.OWNER_AGENT, "Sam"))


if __name__ == "__main__":
    unittest.main()
