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
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.next_id = 1
        self.rowcount = 0
        self._result: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("INSERT INTO commitments"):
            # due_after is spliced into the SQL text itself (fix 1): either a
            # bound "%s::timestamptz" placeholder, or a literal NULL / "now()
            # + interval '...'" with no placeholder at all — so the param
            # count varies with what the SQL contains.
            if "%s::timestamptz" in s:
                text, project, owner, kind, opened_episode, due_after, h = params
            else:
                text, project, owner, kind, opened_episode, h = params
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
            }
            self.rowcount = 1
            return
        if s.startswith("SELECT 1 FROM commitments WHERE status = 'open' AND content_hash"):
            h, project = params
            hit = any(
                r["project"] == project and r["content_hash"] == h and r["status"] == "open"
                for r in self.rows.values()
            )
            self._result = [(1,)] if hit else []
            return
        if s.startswith("SELECT id, text FROM commitments WHERE status = 'open'"):
            project = params[0]
            self._result = [
                (r["id"], r["text"]) for r in self.rows.values()
                if r["status"] == "open" and r["project"] == project
            ]
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
            self._result = [
                (r["id"], r["text"], r["project"], r["owner"], r["kind"], r["opened_episode"],
                 r["opened_at"], r["due_after"], r["status"], r["closed_episode"], r["closed_at"],
                 r["close_reason"])
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
            {"text": "check the deploy", "kind": "blocker", "owner": "assistant"},
        ]}
        n = co.open_from_episode(cur, payload, 42)
        self.assertEqual(n, 2)
        texts = {r["text"] for r in cur.rows.values()}
        self.assertEqual(texts, {"follow up with Matt", "check the deploy"})
        blocker = next(r for r in cur.rows.values() if r["text"] == "check the deploy")
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
            {"text": "  "}, {"text": "x", "kind": "nonsense"},
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
            {"text": "follow up", "due_after": "in 5 days"},
        ]}
        co.open_from_episode(cur, payload, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["due_after"], "interval")
        self.assertNotIn("(due:", row["text"], "a parsed due date is not appended to text")

    def test_open_from_episode_binds_iso_date_as_the_due_after_value(self):
        cur = _CommitmentsCursor()
        payload = {"project": "acme/widget", "open_loops": [
            {"text": "renew the cert", "due_after": "2026-12-01"},
        ]}
        co.open_from_episode(cur, payload, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["due_after"], "2026-12-01")


class ScopeCoalescingTest(unittest.TestCase):
    """fix 3: when project is NULL, open/dedup/auto_close/list scope by
    COALESCE(project, parent_session_id, session_id)."""

    def test_opens_under_parent_session_id_when_project_missing(self):
        cur = _CommitmentsCursor()
        payload = {"parent_session_id": "claude_code:host-1", "open_loops": ["follow up"]}
        co.open_from_episode(cur, payload, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["project"], "claude_code:host-1")

    def test_falls_back_to_session_id_when_neither_project_nor_parent_known(self):
        cur = _CommitmentsCursor()
        payload = {"session_id": "claude_code:abc123", "open_loops": ["follow up"]}
        co.open_from_episode(cur, payload, 1)
        row = list(cur.rows.values())[0]
        self.assertEqual(row["project"], "claude_code:abc123")

    def test_auto_close_matches_using_the_same_coalesced_scope(self):
        cur = _CommitmentsCursor()
        co.open_from_episode(
            cur, {"parent_session_id": "claude_code:host-1", "open_loops": ["ship the fix"]}, 1
        )
        payload = {"parent_session_id": "claude_code:host-1",
                   "closed_loops": [{"text": "done: ship the fix"}]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            n = co.auto_close(cur, payload, 2)
        self.assertEqual(n, 1)

    def test_a_different_lineage_never_closes_across_scopes(self):
        cur = _CommitmentsCursor()
        co.open_from_episode(
            cur, {"parent_session_id": "claude_code:host-1", "open_loops": ["ship the fix"]}, 1
        )
        payload = {"parent_session_id": "claude_code:host-2",
                   "closed_loops": [{"text": "done: ship the fix"}]}
        with mock.patch.object(co, "_has_commitment_embeddings", _has_no_embeddings):
            n = co.auto_close(cur, payload, 2)
        self.assertEqual(n, 0)

    def test_list_owed_accepts_parent_session_id_as_the_scope_key(self):
        cur = _CommitmentsCursor()
        co.open_from_episode(
            cur, {"parent_session_id": "claude_code:host-1", "open_loops": ["a"]}, 1
        )
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
        co.open_from_episode(cur, {"project": project, "open_loops": [text]}, episode)

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
        co.open_from_episode(cur, {"project": "p", "open_loops": ["a", "b"]}, 1)
        n = co.mark_stale(cur)
        self.assertEqual(n, 2)
        self.assertTrue(all(r["status"] == "stale" for r in cur.rows.values()))


class ListOwedAndSetStatusTest(unittest.TestCase):
    def test_list_owed_filters_by_project_and_status(self):
        cur = _CommitmentsCursor()
        co.open_from_episode(cur, {"project": "acme/widget", "open_loops": ["a"]}, 1)
        co.open_from_episode(cur, {"project": "other/repo", "open_loops": ["b"]}, 1)
        rows = co.list_owed(cur, project="acme/widget", status="open")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "a")

    def test_set_status_reopens_and_closes(self):
        cur = _CommitmentsCursor()
        co.open_from_episode(cur, {"project": "p", "open_loops": ["a"]}, 1)
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


if __name__ == "__main__":
    unittest.main()
