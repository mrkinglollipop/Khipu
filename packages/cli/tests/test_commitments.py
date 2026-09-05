"""Tests for khipu.commitments — W3 first-class open loops.

No live database: a small fake cursor backs an in-memory commitments table
so open_from_episode / auto_close / mark_stale / list_owed / set_status can
be exercised deterministically. Cosine matching is forced to fail (no key in
tests) so auto_close falls back to the pure-Python Jaccard path, same as the
capture.py dedup tests.
"""
from __future__ import annotations

import unittest
from unittest import mock

from khipu import commitments as co


class _CommitmentsCursor:
    """In-memory stand-in for the commitments table.

    ``migrated`` says whether migration 0012 (last_seen_at / seen_count) has
    been applied on this fake hub, and ``trigger`` whether 0013
    (future_trigger) has — both branches of both are exercised, because a
    pre-migration hub must keep working (the code gates every write on
    ``db.has_columns`` and derives the field from the text when reading).
    """

    def __init__(self, *, migrated: bool = False, trigger: bool = False):
        self.rows: dict[int, dict] = {}
        self.episode_sessions: dict[int, str] = {}
        self.migrated = migrated
        self.trigger = trigger
        self.next_id = 1
        self.rowcount = 0
        self.statements: list[str] = []
        self._result: list[tuple] = []
        # db.table_columns caches per process; a fake hub in another test must
        # not decide this one's schema.
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.pop("commitments", None)

    def _seed(self, text, *, project="acme/widget", kind="followup", owner=None,
              episode=1, session_id=None, status="open", future_trigger=False):
        """Insert a row WITHOUT going through open_from_episode's filter — for
        tests about auto_close / stale / listing, whose fixtures predate the
        precision filter and are not what those tests are about."""
        cid = self.next_id
        self.next_id += 1
        self.rows[cid] = {
            "id": cid, "text": text, "project": project, "owner": owner, "kind": kind,
            "opened_episode": episode, "opened_at": "t0", "due_after": None,
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
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            cols = ["id", "text", "project", "owner", "kind", "opened_episode",
                    "opened_at", "due_after", "status", "closed_episode",
                    "closed_at", "close_reason", "content_hash"]
            if self.migrated:
                cols += ["last_seen_at", "seen_count"]
            if self.trigger:
                cols += ["future_trigger"]
            self._result = [(c,) for c in cols]
            return
        if s.startswith("INSERT INTO commitments"):
            # due_after is spliced into the SQL text itself (fix 1): either a
            # bound "%s::timestamptz" placeholder, or a literal NULL / "now()
            # + interval '...'" with no placeholder at all — so the param
            # count varies with what the SQL contains.
            rest = list(params)
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
                "future_trigger": future_trigger,
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
        if s.startswith("SELECT id, text FROM commitments WHERE status = 'open'"):
            project = params[0]
            self._result = [
                (r["id"], r["text"]) for r in self.rows.values()
                if r["status"] == "open" and r["project"] == project
            ]
            return
        if s.startswith("SELECT c.id, c.text, c.kind, c.owner FROM commitments c JOIN episodes"):
            (session_id,) = params
            self._result = [
                (r["id"], r["text"], r["kind"], r["owner"])
                for r in self.rows.values()
                if r["status"] == "open"
                and self.episode_sessions.get(r["opened_episode"]) == session_id
            ]
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
        if s.startswith("UPDATE commitments") and "SET status = 'closed'" in s:
            episode_id, close_reason, cid = params
            r = self.rows.get(cid)
            if r and r["status"] == "open":
                r["status"] = "closed"
                r["closed_episode"] = episode_id
                r["close_reason"] = close_reason
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if s.startswith("UPDATE commitments") and "'stale'" in s:
            n = 0
            for r in self.rows.values():
                if r["status"] == "open":
                    r["status"] = "stale"
                    n += 1
            self.rowcount = n
            return
        if s.startswith("SELECT id, text, project, owner, kind"):
            status = params[0]
            rest = params[1:-1]
            project = rest[0] if rest else None
            out = [r for r in self.rows.values() if r["status"] == status]
            if project:
                out = [r for r in out if r["project"] == project]
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
            status, cid = params
            r = self.rows.get(cid)
            if r:
                r["status"] = status
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
    def test_string_and_object_open_loops_are_opened(self):
        cur = _CommitmentsCursor()
        payload = {"project": "acme/widget", "open_loops": [
            "follow up with Matt",
            {"text": "check the deploy plan with Matt", "kind": "blocker", "owner": "assistant"},
        ]}
        n = co.open_from_episode(cur, payload, 42)
        self.assertEqual(n, 2)
        texts = {r["text"] for r in cur.rows.values()}
        self.assertEqual(texts, {"follow up with Matt", "check the deploy plan with Matt"})
        blocker = next(r for r in cur.rows.values() if r["text"] == "check the deploy plan with Matt")
        self.assertEqual(blocker["kind"], "blocker")
        self.assertEqual(blocker["opened_episode"], 42)

    def test_dedup_by_content_hash_within_project(self):
        cur = _CommitmentsCursor()
        payload = {"project": "acme/widget", "open_loops": ["Follow Up With Matt "]}
        co.open_from_episode(cur, payload, 1)
        n2 = co.open_from_episode(cur, payload, 2)
        self.assertEqual(n2, 0)
        self.assertEqual(len(cur.rows), 1)

    def test_no_open_loops_is_a_noop(self):
        cur = _CommitmentsCursor()
        self.assertEqual(co.open_from_episode(cur, {}, 1), 0)
        self.assertEqual(cur.rows, {})

    def test_blank_and_bad_kind_are_handled(self):
        cur = _CommitmentsCursor()
        payload = {"project": "p", "open_loops": [
            {"text": "  "}, {"text": "review the migration checklist", "kind": "nonsense"},
        ]}
        n = co.open_from_episode(cur, payload, 1)
        self.assertEqual(n, 1)
        self.assertEqual(list(cur.rows.values())[0]["kind"], "followup")


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

    def test_open_from_episode_never_raises_on_free_text_due_after(self):
        """Integration: the exact reproduction shape — a model-written
        due_after phrase that is not a date must not blow up the insert."""
        cur = _CommitmentsCursor()
        payload = {"project": "acme/widget", "open_loops": [
            {"text": "ship the release notes", "due_after": "after the release"},
        ]}
        n = co.open_from_episode(cur, payload, 11308)
        self.assertEqual(n, 1)
        row = list(cur.rows.values())[0]
        self.assertIsNone(row["due_after"])
        # The original phrase is not lost — it survives on the commitment text.
        self.assertIn("after the release", row["text"])

    def test_open_from_episode_stores_relative_interval_marker(self):
        cur = _CommitmentsCursor()
        payload = {"project": "acme/widget", "open_loops": [
            {"text": "follow up with Matt on pricing", "due_after": "in 5 days"},
        ]}
        co.open_from_episode(cur, payload, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["due_after"], "interval")
        self.assertNotIn("(due:", row["text"], "a parsed due date is not appended to text")

    def test_open_from_episode_binds_iso_date_as_the_due_after_value(self):
        cur = _CommitmentsCursor()
        payload = {"project": "acme/widget", "open_loops": [
            {"text": "renew the TLS cert for the hub", "due_after": "2026-12-01"},
        ]}
        co.open_from_episode(cur, payload, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["due_after"], "2026-12-01")


class ScopeCoalescingTest(unittest.TestCase):
    """fix 3: when project is NULL, open/dedup/auto_close/list scope by
    COALESCE(project, parent_session_id, session_id)."""

    def test_opens_under_parent_session_id_when_project_missing(self):
        cur = _CommitmentsCursor()
        payload = {"parent_session_id": "claude_code:host-1", "open_loops": ["follow up with Matt on pricing"]}
        co.open_from_episode(cur, payload, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["project"], "claude_code:host-1")

    def test_falls_back_to_session_id_when_neither_project_nor_parent_known(self):
        cur = _CommitmentsCursor()
        payload = {"session_id": "claude_code:abc123", "open_loops": ["follow up with Matt on pricing"]}
        co.open_from_episode(cur, payload, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["project"], "claude_code:abc123")

    def test_auto_close_matches_using_the_same_coalesced_scope(self):
        cur = _CommitmentsCursor()
        cur._seed("ship the fix", project="claude_code:host-1")
        payload = {"parent_session_id": "claude_code:host-1",
                   "closed_loops": [{"text": "done: ship the fix"}]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            n = co.auto_close(cur, payload, 2)
        self.assertEqual(n, 1)

    def test_a_different_lineage_never_closes_across_scopes(self):
        cur = _CommitmentsCursor()
        cur._seed("ship the fix", project="claude_code:host-1")
        payload = {"parent_session_id": "claude_code:host-2",
                   "closed_loops": [{"text": "done: ship the fix"}]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            n = co.auto_close(cur, payload, 2)
        self.assertEqual(n, 0)

    def test_list_owed_accepts_parent_session_id_as_the_scope_key(self):
        cur = _CommitmentsCursor()
        cur._seed("a", project="claude_code:host-1")
        rows = co.list_owed(cur, parent_session_id="claude_code:host-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "a")

    def test_null_scope_dedup_falls_back_to_the_in_code_check(self):
        """No project, no parent_session_id, no session_id at all: the
        partial unique index can't dedup two NULL-project rows, so the
        explicit in-code check (fix 3) must catch it."""
        cur = _CommitmentsCursor()
        payload = {"open_loops": ["truly unscoped follow up"]}
        n1 = co.open_from_episode(cur, payload, 1)
        n2 = co.open_from_episode(cur, payload, 2)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0, "in-code dedup must catch the NULL-scope duplicate")
        self.assertEqual(len(cur.rows), 1)


class AutoCloseTest(unittest.TestCase):
    def _open(self, cur, text, project="acme/widget", episode=1):
        cur._seed(text, project=project, episode=episode)

    def test_explicit_done_prefix_closes_unconditionally(self):
        cur = _CommitmentsCursor()
        self._open(cur, "totally unrelated wording")
        payload = {"project": "acme/widget", "closed_loops": [{"text": "done: totally unrelated wording"}]}
        with mock.patch.object(co, "_cosine_scores", _no_cosine):
            n = co.auto_close(cur, payload, 99)
        self.assertEqual(n, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["closed_episode"], 99)
        self.assertIn("explicit done", row["close_reason"])

    def test_jaccard_fallback_closes_a_similar_closed_loop(self):
        cur = _CommitmentsCursor()
        self._open(cur, "follow up with Matt about the deploy plan")
        payload = {"project": "acme/widget",
                   "closed_loops": [{"text": "follow up with Matt about the deploy plan"}]}
        with mock.patch.object(co, "_cosine_scores", _no_cosine):
            n = co.auto_close(cur, payload, 5)
        self.assertEqual(n, 1)

    def test_dissimilar_text_does_not_close(self):
        cur = _CommitmentsCursor()
        self._open(cur, "follow up with Matt about the deploy plan")
        payload = {"project": "acme/widget", "closed_loops": [{"text": "completely different subject"}]}
        with mock.patch.object(co, "_cosine_scores", _no_cosine):
            n = co.auto_close(cur, payload, 5)
        self.assertEqual(n, 0)
        self.assertEqual(list(cur.rows.values())[0]["status"], "open")

    def test_short_closed_loop_phrase_closes_longer_commitment(self):
        # Regression: a closed_loop is a short phrase ("crimson lantern audit")
        # that is a subset of a longer commitment text. Plain Jaccard scores it
        # ~0.27, below commitment_close_similarity (0.85), so it must match by
        # containment as an explicit close signal — not the strict path.
        cur = _CommitmentsCursor()
        self._open(cur, "run the crimson lantern audit after the widget-42 release ships")
        payload = {"project": "acme/widget", "closed_loops": [{"text": "crimson lantern audit"}]}
        with mock.patch.object(co, "_cosine_scores", _no_cosine):
            n = co.auto_close(cur, payload, 12)
        self.assertEqual(n, 1)
        self.assertEqual(list(cur.rows.values())[0]["status"], "closed")

    def test_decision_with_done_prefix_also_closes(self):
        cur = _CommitmentsCursor()
        self._open(cur, "ship the fix")
        payload = {"project": "acme/widget", "decisions": ["done: ship the fix"]}
        with mock.patch.object(co, "_cosine_scores", _no_cosine):
            n = co.auto_close(cur, payload, 7)
        self.assertEqual(n, 1)

    def test_no_candidates_or_no_open_commitments_is_a_noop(self):
        cur = _CommitmentsCursor()
        self.assertEqual(co.auto_close(cur, {"project": "p"}, 1), 0)
        self._open(cur, "something")
        self.assertEqual(co.auto_close(cur, {"project": "p"}, 1), 0)

    def test_different_project_never_closes(self):
        cur = _CommitmentsCursor()
        self._open(cur, "follow up", project="acme/widget")
        payload = {"project": "other/repo", "closed_loops": [{"text": "follow up"}]}
        with mock.patch.object(co, "_cosine_scores", _no_cosine):
            n = co.auto_close(cur, payload, 1)
        self.assertEqual(n, 0)

    def test_each_open_commitment_closes_at_most_once(self):
        cur = _CommitmentsCursor()
        self._open(cur, "ship the fix")
        payload = {"project": "acme/widget", "closed_loops": [
            {"text": "done: ship the fix"}, {"text": "done: ship the fix"},
        ]}
        with mock.patch.object(co, "_cosine_scores", _no_cosine):
            n = co.auto_close(cur, payload, 1)
        self.assertEqual(n, 1)

    def test_explicit_done_closes_the_best_match_not_the_first_row(self):
        """fix 4: multiple open commitments — an explicit 'done:' close must
        pick the one whose text actually matches, not whatever the DB
        happened to return first."""
        cur = _CommitmentsCursor()
        self._open(cur, "unrelated topic entirely", episode=1)
        self._open(cur, "ship the mobile release fix", episode=1)
        self._open(cur, "another unrelated item", episode=1)
        payload = {"project": "acme/widget",
                   "closed_loops": [{"text": "done: ship the mobile release fix"}]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            n = co.auto_close(cur, payload, 99)
        self.assertEqual(n, 1)
        closed = [r for r in cur.rows.values() if r["status"] == "closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["text"], "ship the mobile release fix")

    def test_explicit_done_closes_nothing_when_no_candidate_clears_the_bar(self):
        """fix 4: if the done-phrase doesn't resemble ANY open commitment,
        nothing closes — an explicit done: prefix is not a license to close
        an arbitrary row."""
        cur = _CommitmentsCursor()
        self._open(cur, "totally different subject matter here")
        payload = {"project": "acme/widget",
                   "closed_loops": [{"text": "done: zzz qqq xyz nonsense words"}]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            n = co.auto_close(cur, payload, 1)
        self.assertEqual(n, 0)
        self.assertEqual(list(cur.rows.values())[0]["status"], "open")

    def test_explicit_done_never_calls_the_embed_api(self):
        """fix 5a: an explicit done: close is text-matched only — no cosine/
        embed API call, ever."""
        cur = _CommitmentsCursor()
        self._open(cur, "ship the fix")
        payload = {"project": "acme/widget", "closed_loops": [{"text": "done: ship the fix"}]}
        with mock.patch.object(co, "_cosine_scores") as m_cosine, \
                mock.patch.object(co, "_has_commitment_embeddings") as m_has_emb:
            n = co.auto_close(cur, payload, 1)
        self.assertEqual(n, 1)
        m_cosine.assert_not_called()
        m_has_emb.assert_not_called()

    def test_non_explicit_close_skips_cosine_when_no_embeddings_exist(self):
        """fix 5b: for a non-explicit closed_loop, cosine is only attempted
        when commitment embeddings actually exist for the scope; otherwise
        it falls straight to Jaccard with no API call."""
        cur = _CommitmentsCursor()
        self._open(cur, "follow up with Matt about the deploy plan")
        payload = {"project": "acme/widget",
                   "closed_loops": [{"text": "follow up with Matt about the deploy plan"}]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings), \
                mock.patch.object(co, "_cosine_scores") as m_cosine:
            n = co.auto_close(cur, payload, 5)
        self.assertEqual(n, 1)
        m_cosine.assert_not_called()

    def test_non_explicit_close_attempts_cosine_when_embeddings_exist(self):
        """fix 5b, positive case: a closed_loop whose wording does NOT overlap
        the commitment text (so the text/containment bar fails) still closes
        via cosine when the scope's commitments are embedded — here the cosine
        score is forced strong. This is the semantic-paraphrase path."""
        cur = _CommitmentsCursor()
        self._open(cur, "follow up with Matt about the deploy plan")
        cid = list(cur.rows.keys())[0]
        payload = {"project": "acme/widget", "closed_loops": [{"text": "completely different wording"}]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_embeddings), \
                mock.patch.object(co, "_cosine_scores", return_value={cid: 0.95}):
            n = co.auto_close(cur, payload, 5)
        self.assertEqual(n, 1)
        self.assertEqual(cur.rows[cid]["status"], "closed")


class MarkStaleTest(unittest.TestCase):
    def test_open_commitments_flip_to_stale(self):
        cur = _CommitmentsCursor()
        cur._seed("a", project="p")
        cur._seed("b", project="p")
        n = co.mark_stale(cur)
        self.assertEqual(n, 2)
        self.assertTrue(all(r["status"] == "stale" for r in cur.rows.values()))


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

    def test_agent_and_infra_chatter_is_rejected(self):
        for text in (
            "Visual check agent's verdict is pending",
            "Receive report from the phase 1-2 agent",
            "Poll the drive for the finished export",
            "Wait for notifications from the background job",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(co.rejection_reason(text), text)

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

    def test_open_from_episode_drops_the_rejects_before_insert(self):
        cur = _CommitmentsCursor()
        payload = {"project": "acme/widget", "open_loops": [
            "Drive 46 is still running",
            "Send 'screen free' message to Khipu session",
            "Matt to decide whether 0.3.17 ships with the seal fix",
        ]}
        self.assertEqual(co.open_from_episode(cur, payload, 1), 1)
        self.assertEqual(
            [r["text"] for r in cur.rows.values()],
            ["Matt to decide whether 0.3.17 ships with the seal fix"],
        )


class OpenDedupTest(unittest.TestCase):
    """Item 2: zero EXACT duplicates were measured — the noise is paraphrase
    across successive captures of one session."""

    payload = {"project": "acme/widget", "open_loops": [
        "Matt to decide whether the seal fix ships in 0.3.17",
    ]}

    def test_paraphrase_does_not_open_a_second_row(self):
        cur = _CommitmentsCursor(migrated=True)
        co.open_from_episode(cur, self.payload, 1)
        again = {"project": "acme/widget", "open_loops": [
            "Matt to decide whether the seal fix ships in 0.3.17 or waits",
        ]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            self.assertEqual(co.open_from_episode(cur, again, 2), 0)
        self.assertEqual(len(cur.rows), 1)

    def test_a_restatement_touches_last_seen_and_seen_count(self):
        cur = _CommitmentsCursor(migrated=True)
        co.open_from_episode(cur, self.payload, 1)
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            co.open_from_episode(cur, self.payload, 2)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["seen_count"], 2)
        self.assertIsNotNone(row["last_seen_at"])

    def test_pre_migration_hub_still_dedups_without_touching(self):
        cur = _CommitmentsCursor(migrated=False)
        co.open_from_episode(cur, self.payload, 1)
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            self.assertEqual(co.open_from_episode(cur, self.payload, 2), 0)
        self.assertEqual(len(cur.rows), 1)
        self.assertEqual(list(cur.rows.values())[0]["seen_count"], 1)

    def test_cosine_catches_a_paraphrase_that_shares_no_tokens(self):
        cur = _CommitmentsCursor(migrated=True)
        co.open_from_episode(cur, self.payload, 1)
        cid = list(cur.rows.keys())[0]
        other = {"project": "acme/widget", "open_loops": [
            "Matt should choose between shipping and holding the release",
        ]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_embeddings), \
                mock.patch.object(co, "_cosine_scores", return_value={cid: 0.93}):
            self.assertEqual(co.open_from_episode(cur, other, 2), 0)
        self.assertEqual(len(cur.rows), 1)

    def test_an_unrelated_item_still_opens(self):
        cur = _CommitmentsCursor(migrated=True)
        co.open_from_episode(cur, self.payload, 1)
        other = {"project": "acme/widget", "open_loops": [
            "Renew the TLS cert for the hub before December",
        ]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            self.assertEqual(co.open_from_episode(cur, other, 2), 1)
        self.assertEqual(len(cur.rows), 2)


class StaleRuleTest(unittest.TestCase):
    """Item 3: expiry by SILENCE, per kind, and never before a live due date."""

    def test_followups_and_promises_age_out_at_14_days(self):
        sql = " ".join(co.stale_sql(has_last_seen=True).split())
        self.assertIn("kind IN ('followup', 'promise')", sql)
        self.assertIn("interval '14 days'", sql)

    def test_blockers_and_questions_age_out_at_30_days(self):
        sql = " ".join(co.stale_sql(has_last_seen=True).split())
        self.assertIn("ELSE interval '30 days'", sql)

    def test_a_future_due_date_is_never_stale(self):
        sql = " ".join(co.stale_sql(has_last_seen=True).split())
        self.assertIn("(due_after IS NULL OR due_after <= now())", sql)

    def test_silence_is_measured_from_last_seen_when_the_column_exists(self):
        self.assertIn("COALESCE(last_seen_at, opened_at)", co.stale_sql(has_last_seen=True))

    def test_pre_migration_hub_falls_back_to_opened_at(self):
        sql = co.stale_sql(has_last_seen=False)
        self.assertNotIn("last_seen_at", sql)
        self.assertIn("opened_at <", " ".join(sql.split()))


class SessionPlanClosureTest(unittest.TestCase):
    """Item 4: a session's own in-flight/plan items are retired when THAT
    session ends — no later capture ever says 'done: <phrase>' about them."""

    def _cur(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Drive 46 is still running", episode=7, session_id="claude:s1")
        cur._seed("Generate the UI mocks for the Owed screen", episode=7, session_id="claude:s1")
        return cur

    def test_sessionend_closes_every_assistant_item_of_that_session(self):
        """The session-ended rule (2026-09-04): an assistant commitment with
        no cross-session trigger dies with its session — the in-flight status
        line AND the plain plan step, not just the plan-shaped one."""
        cur = self._cur()
        payload = {"project": "acme/widget", "session_id": "claude:s1", "event": "sessionend"}
        self.assertEqual(co.close_session_plan(cur, payload, 9), 2)
        closed = [r for r in cur.rows.values() if r["status"] == "closed"]
        self.assertEqual({r["close_reason"] for r in closed}, {"session-ended"})

    def test_a_future_trigger_promise_survives_its_own_sessionend(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Send Matt the restore numbers next session", kind="promise",
                  episode=7, session_id="claude:s1")
        cur._seed("Run oracle.sh when Matt says the lane is re-authed",
                  episode=7, session_id="claude:s1")
        cur._seed("Rebuild the index and paste the timings", episode=7, session_id="claude:s1")
        payload = {"project": "acme/widget", "session_id": "claude:s1", "event": "sessionend"}
        self.assertEqual(co.close_session_plan(cur, payload, 9), 1)
        still_open = {r["text"] for r in cur.rows.values() if r["status"] == "open"}
        self.assertEqual(still_open, {
            "Send Matt the restore numbers next session",
            "Run oracle.sh when Matt says the lane is re-authed",
        })

    def test_event_is_read_from_scope_when_the_payload_predates_the_field(self):
        cur = self._cur()
        payload = {"project": "acme/widget", "session_id": "claude:s1", "scope": "claude sessionend"}
        self.assertEqual(co.close_session_plan(cur, payload, 9), 2)

    def test_a_stop_capture_closes_nothing_on_its_own(self):
        cur = self._cur()
        payload = {"project": "acme/widget", "session_id": "claude:s1", "event": "stop"}
        self.assertEqual(co.close_session_plan(cur, payload, 9), 0)

    def test_a_mention_in_closed_loops_closes_it_without_sessionend(self):
        cur = self._cur()
        payload = {"project": "acme/widget", "session_id": "claude:s1", "event": "stop",
                   "closed_loops": [{"text": "Drive 46 is still running"}]}
        self.assertEqual(co.close_session_plan(cur, payload, 9), 1)

    def test_another_sessions_commitments_are_untouched(self):
        cur = self._cur()
        cur._seed("Drive 12 is still running", episode=8, session_id="claude:s2")
        payload = {"project": "acme/widget", "session_id": "claude:s1", "event": "sessionend"}
        co.close_session_plan(cur, payload, 9)
        other = next(r for r in cur.rows.values() if r["text"] == "Drive 12 is still running")
        self.assertEqual(other["status"], "open")

    def test_user_owed_items_are_never_closed_this_way(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Matt is waiting on the pricing sheet", kind="question",
                  episode=7, session_id="claude:s1")
        cur._seed("Matt is waiting on the licence decision", kind="followup",
                  owner="user", episode=7, session_id="claude:s1")
        payload = {"project": "acme/widget", "session_id": "claude:s1", "event": "sessionend"}
        self.assertEqual(co.close_session_plan(cur, payload, 9), 0)
        self.assertTrue(all(r["status"] == "open" for r in cur.rows.values()))


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
        ("Peer 1", "ASSISTANT"); the desktop's "Needs you" section is
        owner == 'user', so the listed value is always one of the two."""
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Critique the draft plan for the app", owner="Peer 1")
        cur._seed("Matt reviews and merges the stack himself", owner="ASSISTANT")
        rows = {r["text"]: r["owner"] for r in co.list_owed(cur, project="acme/widget")}
        self.assertEqual(rows["Critique the draft plan for the app"], "assistant")
        self.assertEqual(rows["Matt reviews and merges the stack himself"], "user")

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

    def test_open_from_episode_never_opens_one(self):
        cur = _CommitmentsCursor(migrated=True)
        payload = {"project": "acme/widget", "open_loops": [
            "Reply with the SHA each PR now points at.",
            "Matt to decide whether 0.3.17 ships with the seal fix",
        ]}
        self.assertEqual(co.open_from_episode(cur, payload, 1), 1)
        self.assertEqual(
            [r["text"] for r in cur.rows.values()],
            ["Matt to decide whether 0.3.17 ships with the seal fix"],
        )


class OpenStoresOwnerAndTriggerTest(unittest.TestCase):
    def test_owner_is_stored_normalised(self):
        cur = _CommitmentsCursor(migrated=True)
        payload = {"project": "acme/widget", "open_loops": [
            {"text": "Matt must merge the stacked PRs", "owner": "Matt"},
            {"text": "Rebuild the search index", "owner": "Peer 1"},
        ]}
        self.assertEqual(co.open_from_episode(cur, payload, 1), 2)
        owners = {r["text"]: r["owner"] for r in cur.rows.values()}
        self.assertEqual(owners["Matt must merge the stacked PRs"], "user")
        self.assertEqual(owners["Rebuild the search index"], "assistant")

    def test_future_trigger_is_stored_when_the_column_exists(self):
        cur = _CommitmentsCursor(migrated=True, trigger=True)
        payload = {"project": "acme/widget", "open_loops": [
            "Send Matt the restore-drill numbers next session",
            "Rebuild the search index",
        ]}
        self.assertEqual(co.open_from_episode(cur, payload, 1), 2)
        flags = {r["text"]: r["future_trigger"] for r in cur.rows.values()}
        self.assertTrue(flags["Send Matt the restore-drill numbers next session"])
        self.assertFalse(flags["Rebuild the search index"])

    def test_the_model_cannot_override_the_deterministic_trigger(self):
        cur = _CommitmentsCursor(migrated=True, trigger=True)
        payload = {"project": "acme/widget", "open_loops": [
            {"text": "Rebuild the search index", "future_trigger": True},
        ]}
        co.open_from_episode(cur, payload, 1)
        self.assertFalse(list(cur.rows.values())[0]["future_trigger"])

    def test_a_pre_migration_hub_opens_without_the_column(self):
        cur = _CommitmentsCursor(migrated=True, trigger=False)
        payload = {"project": "acme/widget", "open_loops": [
            "Send Matt the restore-drill numbers next session",
        ]}
        self.assertEqual(co.open_from_episode(cur, payload, 1), 1)
        stmt = next(s for s in cur.statements if s.startswith("INSERT INTO commitments"))
        self.assertNotIn("future_trigger", stmt)


class DroppedStatusTest(unittest.TestCase):
    def test_set_status_accepts_dropped_and_still_rejects_junk(self):
        cur = _CommitmentsCursor(migrated=True)
        cid = cur._seed("send Matt the restore numbers")
        self.assertTrue(co.set_status(cur, cid, "dropped"))
        self.assertEqual(cur.rows[cid]["status"], "dropped")
        self.assertTrue(co.set_status(cur, cid, "open"))
        with self.assertRaises(ValueError):
            co.set_status(cur, cid, "bogus")


if __name__ == "__main__":
    unittest.main()
