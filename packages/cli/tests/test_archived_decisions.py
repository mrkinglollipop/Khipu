"""Archived validity readers retain their original strict coverage."""
from __future__ import annotations


import unittest

from unittest import mock

from khipu import decisions as de

class _Cursor:
    """In-memory stand-in for ``decisions`` (+ optionally ``decision_links``
    and ``episodes.deleted_at``). ``evidence``/``links``/``episodes_col``
    control which migration 0024 surfaces are "applied" on this fake hub —
    every decisions.py function gates on ``khipu.db.has_columns``, so a
    fake that reports fewer columns exercises the exact same pre-migration
    code path a real un-migrated hub would take.
    """

    _BASE_COLS = ("id", "project", "text", "rationale", "decided_at",
                  "episode_id", "session_id", "superseded_by", "created_at")
    _EVIDENCE_COLS = ("source_kind", "evidence", "superseded_at",
                       "supersede_source", "supersede_reason",
                       "retracted_at", "retract_reason")
    _LINK_COLS = ("id", "old_id", "new_id", "kind", "confidence", "source",
                  "reason", "state", "created_at", "resolved_at")

    def __init__(self, *, evidence: bool = False, links: bool = False,
                 episodes_deleted_at: bool = True):
        self.rows: dict[int, dict] = {}
        self.links: dict[int, dict] = {}
        self.forgotten_episodes: set[int] = set()
        self.evidence = evidence
        self.links_ready = links
        self.episodes_deleted_at = episodes_deleted_at
        self.next_id = 1
        self.next_link_id = 1
        self.rowcount = 0
        self._result: list[tuple] = []
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.clear()

    # -- seeding -------------------------------------------------------------

    def seed(self, *, project=None, text="", episode_id=None, decided_at="2026-09-01T00:00:00+00:00",
              superseded_by=None, retracted_at=None) -> int:
        did = self.next_id
        self.next_id += 1
        self.rows[did] = {
            "id": did, "project": project, "text": text, "rationale": None,
            "decided_at": decided_at, "episode_id": episode_id, "session_id": None,
            "superseded_by": superseded_by, "created_at": decided_at,
            "source_kind": None, "evidence": None, "superseded_at": None,
            "supersede_source": None, "supersede_reason": None,
            "retracted_at": retracted_at, "retract_reason": None,
        }
        return did

    # -- SQL surface -----------------------------------------------------------

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()

        # Alias grouping reads inside a savepoint. This fixture has no aliases:
        # the real left join returns the supplied project and a null alias.
        if s.startswith(("SAVEPOINT ", "RELEASE SAVEPOINT ", "ROLLBACK TO SAVEPOINT ")):
            return
        if s.startswith("WITH c AS (SELECT COALESCE((SELECT project FROM project_aliases"):
            project, fallback = params
            assert project == fallback
            self._result = [(project, None)]
            return

        if s.startswith("SELECT column_name FROM information_schema.columns"):
            (table,) = params
            if table == "decisions":
                cols = list(self._BASE_COLS) + (list(self._EVIDENCE_COLS) if self.evidence else [])
            elif table == "decision_links":
                cols = list(self._LINK_COLS) if self.links_ready else []
            elif table == "episodes":
                cols = ["id", "deleted_at"] if self.episodes_deleted_at else ["id"]
            else:
                cols = []
            self._result = [(c,) for c in cols]
            return

        if s.startswith("SELECT id FROM decisions WHERE project IS NOT DISTINCT FROM"):
            # dedup lookup (historical owner decision writer / historical decision extraction)
            project, norm_text = params[0], params[1]
            hit = None
            for r in self.rows.values():
                if r["project"] == project and de.normalize_text(r["text"]) == norm_text:
                    hit = r["id"]
                    break
            self._result = [(hit,)] if hit is not None else []
            return

        if s.startswith("SELECT id, project, superseded_by, retracted_at FROM decisions WHERE id"):
            (did,) = params
            r = self.rows.get(did)
            self._result = (
                [(r["id"], r["project"], r["superseded_by"], r["retracted_at"])] if r else []
            )
            return

        if s.startswith("SELECT id, project, superseded_by FROM decisions WHERE id"):
            (did,) = params
            r = self.rows.get(did)
            self._result = [(r["id"], r["project"], r["superseded_by"])] if r else []
            return

        if s.startswith("SELECT superseded_by FROM decisions WHERE id"):
            (did,) = params
            r = self.rows.get(did)
            self._result = [(r["superseded_by"],)] if r else []
            return

        if s.startswith("UPDATE decisions SET superseded_by = %s, superseded_at = now()"):
            new_id, source, reason, old_id = params
            r = self.rows[old_id]
            if r["superseded_by"] is None:
                r["superseded_by"] = new_id
                r["superseded_at"] = "now"
                r["supersede_source"] = source
                r["supersede_reason"] = reason
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if s.startswith("UPDATE decisions SET superseded_by = %s WHERE id"):
            new_id, old_id = params
            r = self.rows[old_id]
            if r["superseded_by"] is None:
                r["superseded_by"] = new_id
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if s.startswith("UPDATE decisions SET superseded_by = NULL, superseded_at = NULL"):
            (did,) = params
            r = self.rows[did]
            if r["superseded_by"] is not None:
                r["superseded_by"] = None
                r["superseded_at"] = None
                r["supersede_source"] = None
                r["supersede_reason"] = None
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if s.startswith("UPDATE decisions SET superseded_by = NULL WHERE id"):
            (did,) = params
            r = self.rows[did]
            if r["superseded_by"] is not None:
                r["superseded_by"] = None
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if s.startswith("UPDATE decisions SET retracted_at = now()"):
            reason, did = params
            r = self.rows[did]
            if r["retracted_at"] is None:
                r["retracted_at"] = "now"
                r["retract_reason"] = reason
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if s.startswith("UPDATE decisions SET retracted_at = NULL"):
            (did,) = params
            r = self.rows[did]
            if r["retracted_at"] is not None:
                r["retracted_at"] = None
                r["retract_reason"] = None
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if s.startswith("UPDATE decisions SET retracted_at = now(), retract_reason = 'forgotten'"):
            (episode_id,) = params
            n = 0
            for r in self.rows.values():
                if r["episode_id"] == episode_id and r["retracted_at"] is None:
                    r["retracted_at"] = "now"
                    r["retract_reason"] = "forgotten"
                    n += 1
            self.rowcount = n
            return

        if s.startswith("INSERT INTO decisions (project, text, session_id, decided_at, source_kind, evidence)"):
            project, text, session_id, decided_at, source_kind, evidence = params
            did = self.next_id
            self.next_id += 1
            self.rows[did] = {
                "id": did, "project": project, "text": text, "rationale": None,
                "decided_at": decided_at or "now", "episode_id": None, "session_id": session_id,
                "superseded_by": None, "created_at": decided_at or "now",
                "source_kind": source_kind, "evidence": evidence, "superseded_at": None,
                "supersede_source": None, "supersede_reason": None,
                "retracted_at": None, "retract_reason": None,
            }
            self._result = [(did,)]
            return

        if s.startswith("INSERT INTO decisions (project, text, session_id, decided_at) VALUES"):
            project, text, session_id, decided_at = params
            did = self.next_id
            self.next_id += 1
            self.rows[did] = {
                "id": did, "project": project, "text": text, "rationale": None,
                "decided_at": decided_at or "now", "episode_id": None, "session_id": session_id,
                "superseded_by": None, "created_at": decided_at or "now",
                "source_kind": None, "evidence": None, "superseded_at": None,
                "supersede_source": None, "supersede_reason": None,
                "retracted_at": None, "retract_reason": None,
            }
            self._result = [(did,)]
            return

        if s.startswith("SELECT id, project, text, rationale, decided_at"):
            *clause_params, limit = params
            out = list(self.rows.values())
            i = 0
            if "project = %s" in s:
                out = [r for r in out if r["project"] == clause_params[i]]
                i += 1
            if "decided_at >= %s" in s:
                since = clause_params[i]
                out = [r for r in out if str(r["decided_at"]) >= str(since)]
                i += 1
            if "superseded_by IS NULL" in s:
                out = [r for r in out if r["superseded_by"] is None]
            if "retracted_at IS NULL" in s:
                out = [r for r in out if r.get("retracted_at") is None]
            if "superseded_by IS NOT NULL" in s:
                out = [r for r in out if r["superseded_by"] is not None]
            if "retracted_at IS NOT NULL" in s:
                out = [r for r in out if r.get("retracted_at") is not None]
            if "FALSE" in s.split("WHERE", 1)[-1]:
                out = []
            if "episode_id IS NULL OR NOT EXISTS" in s:
                out = [r for r in out
                       if r["episode_id"] is None or r["episode_id"] not in self.forgotten_episodes]
            out.sort(key=lambda r: r["decided_at"], reverse=True)
            out = out[:limit]
            cols = list(self._BASE_COLS) + (list(self._EVIDENCE_COLS) if self.evidence else [])
            self._result = [tuple(r[c] for c in cols) for r in out]
            return

        if s.startswith("SELECT episode_id, COUNT(*) FILTER"):
            # Three mutually-exclusive buckets since Phase 2, session B
            # (decisions_retracted added to enrich_search_results) — retracted
            # wins over superseded, same precedence as state_of().
            (episode_ids,) = params
            counts: dict[int, list[int]] = {}
            for r in self.rows.values():
                if r["episode_id"] not in episode_ids:
                    continue
                if "episode_id IS NULL OR NOT EXISTS" in s and r["episode_id"] in self.forgotten_episodes:
                    continue
                bucket = counts.setdefault(r["episode_id"], [0, 0, 0])
                if r.get("retracted_at"):
                    bucket[2] += 1
                elif r["superseded_by"] is None:
                    bucket[0] += 1
                else:
                    bucket[1] += 1
            self._result = [
                (eid, cur_n, sup_n, ret_n) for eid, (cur_n, sup_n, ret_n) in counts.items()
            ]
            return

        if s.startswith("SELECT id, text, superseded_by FROM decisions WHERE episode_id"):
            (episode_id,) = params
            out = sorted([r for r in self.rows.values() if r["episode_id"] == episode_id],
                         key=lambda r: r["id"])
            self._result = [(r["id"], r["text"], r["superseded_by"]) for r in out]
            return

        if s.startswith("SELECT id, text, superseded_by, retracted_at FROM decisions WHERE episode_id"):
            (episode_id,) = params
            out = sorted([r for r in self.rows.values() if r["episode_id"] == episode_id],
                         key=lambda r: r["id"])
            self._result = [(r["id"], r["text"], r["superseded_by"], r.get("retracted_at")) for r in out]
            return

        if s.startswith("INSERT INTO decision_links") and "DO NOTHING" in s:
            old_id, new_id, kind, confidence, source, reason = params
            existing = next(
                (lid for lid, lk in self.links.items()
                 if lk["old_id"] == old_id and lk["new_id"] == new_id and lk["kind"] == kind),
                None,
            )
            if existing is not None:
                self._result = []
                return
            lid = self.next_link_id
            self.next_link_id += 1
            self.links[lid] = {
                "id": lid, "old_id": old_id, "new_id": new_id, "kind": kind,
                "confidence": confidence, "source": source, "reason": reason,
                "state": "candidate", "created_at": "t0", "resolved_at": None,
            }
            self._result = [(lid,)]
            return

        if s.startswith("SELECT id FROM decision_links WHERE old_id"):
            old_id, new_id, kind = params
            hit = next(
                (lid for lid, lk in self.links.items()
                 if lk["old_id"] == old_id and lk["new_id"] == new_id and lk["kind"] == kind),
                None,
            )
            self._result = [(hit,)] if hit is not None else []
            return

        if s.startswith("SELECT old_id, new_id, kind, state FROM decision_links WHERE id"):
            (lid,) = params
            lk = self.links.get(lid)
            self._result = [(lk["old_id"], lk["new_id"], lk["kind"], lk["state"])] if lk else []
            return

        if s.startswith("UPDATE decision_links SET state = 'rejected'"):
            (lid,) = params
            self.links[lid]["state"] = "rejected"
            self.links[lid]["resolved_at"] = "now"
            return

        if s.startswith("UPDATE decision_links SET state = 'applied'") and "resolved_at = now() WHERE id" in s:
            (lid,) = params
            self.links[lid]["state"] = "applied"
            self.links[lid]["resolved_at"] = "now"
            return

        if s.startswith("UPDATE decision_links SET state = 'restored'"):
            old_id, new_id = params
            for lk in self.links.values():
                if lk["old_id"] == old_id and lk["new_id"] == new_id and lk["kind"] == "supersedes" and lk["state"] == "applied":
                    lk["state"] = "restored"
                    lk["resolved_at"] = "now"
            return

        if "INSERT INTO decision_links (old_id, new_id, kind, source, reason, state, resolved_at)" in s:
            old_id, new_id, source, reason = params
            existing = next(
                (lk for lk in self.links.values()
                 if lk["old_id"] == old_id and lk["new_id"] == new_id and lk["kind"] == "supersedes"),
                None,
            )
            if existing:
                existing.update(state="applied", source=source, reason=reason, resolved_at="now")
            else:
                lid = self.next_link_id
                self.next_link_id += 1
                self.links[lid] = {
                    "id": lid, "old_id": old_id, "new_id": new_id, "kind": "supersedes",
                    "confidence": None, "source": source, "reason": reason,
                    "state": "applied", "created_at": "t0", "resolved_at": "now",
                }
            return

        if s.startswith("SELECT l.id, l.old_id, l.new_id"):
            clauses_params = list(params[:-1])
            limit = params[-1]
            out = list(self.links.values())
            idx = 0
            if "l.state = %s" in s:
                state = clauses_params[idx]
                idx += 1
                out = [lk for lk in out if lk["state"] == state]
            if "o.project = %s OR n.project = %s" in s:
                project = clauses_params[idx]
                out = [
                    lk for lk in out
                    if self.rows[lk["old_id"]]["project"] == project
                    or self.rows[lk["new_id"]]["project"] == project
                ]
            out = out[:limit]
            self._result = [
                (lk["id"], lk["old_id"], lk["new_id"], lk["kind"], lk["confidence"], lk["source"],
                 lk["reason"], lk["state"], lk["created_at"], lk["resolved_at"],
                 self.rows[lk["old_id"]]["text"], self.rows[lk["new_id"]]["text"])
                for lk in out
            ]
            return

        # -- Phase 2, session C: historical decision extraction / historical reversal detection --

        if s.startswith(
            "INSERT INTO decisions (project, text, episode_id, session_id, decided_at, "
            "source_kind, rationale, evidence)"
        ):
            project, text, episode_id, session_id, decided_at, source_kind, rationale, evidence = params
            did = self.next_id
            self.next_id += 1
            self.rows[did] = {
                "id": did, "project": project, "text": text, "rationale": rationale,
                "decided_at": decided_at or "now", "episode_id": episode_id, "session_id": session_id,
                "superseded_by": None, "created_at": decided_at or "now",
                "source_kind": source_kind, "evidence": evidence, "superseded_at": None,
                "supersede_source": None, "supersede_reason": None,
                "retracted_at": None, "retract_reason": None,
            }
            self.rowcount = 1
            return

        if s.startswith("INSERT INTO decisions (project, text, episode_id, session_id, decided_at)"):
            project, text, episode_id, session_id, decided_at = params
            did = self.next_id
            self.next_id += 1
            self.rows[did] = {
                "id": did, "project": project, "text": text, "rationale": None,
                "decided_at": decided_at or "now", "episode_id": episode_id, "session_id": session_id,
                "superseded_by": None, "created_at": decided_at or "now",
                "source_kind": None, "evidence": None, "superseded_at": None,
                "supersede_source": None, "supersede_reason": None,
                "retracted_at": None, "retract_reason": None,
            }
            self.rowcount = 1
            return

        if s.startswith("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"):
            # This fake has no pg_extension catalog at all — every test using
            # it exercises the Python token-overlap fallback, never the SQL
            # similarity() path (that path is real-Postgres-only, covered by
            # tests/test_pg_scratch.py).
            self._result = []
            return

        if s.startswith("SELECT id, project, decided_at FROM decisions WHERE episode_id"):
            episode_id, text = params
            hit = next(
                (r for r in self.rows.values()
                 if r["episode_id"] == episode_id and r["text"] == text),
                None,
            )
            self._result = [(hit["id"], hit["project"], hit["decided_at"])] if hit else []
            return

        if s.startswith("SELECT COUNT(*) FROM decision_links WHERE state = 'candidate'"):
            self._result = [(sum(1 for lk in self.links.values() if lk["state"] == "candidate"),)]
            return

        if s.startswith("SELECT id, text FROM decisions WHERE project IS NOT DISTINCT FROM"):
            project, exclude_id, before = params
            out = [
                r for r in self.rows.values()
                if r["project"] == project and r["id"] != exclude_id
                and str(r["decided_at"]) < str(before) and r["superseded_by"] is None
            ]
            if "retracted_at IS NULL" in s:
                out = [r for r in out if r.get("retracted_at") is None]
            if "episode_id IS NULL OR NOT EXISTS" in s:
                out = [r for r in out
                       if r["episode_id"] is None or r["episode_id"] not in self.forgotten_episodes]
            out.sort(key=lambda r: r["decided_at"], reverse=True)
            self._result = [(r["id"], r["text"]) for r in out]
            return

        raise AssertionError(f"unexpected SQL: {s[:160]} params={params}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

class StateOfTest(unittest.TestCase):
    def test_current(self):
        self.assertEqual(de.state_of({}), "current")

    def test_superseded(self):
        self.assertEqual(de.state_of({"superseded_by": 2}), "superseded")

    def test_retracted_wins_over_superseded(self):
        self.assertEqual(de.state_of({"superseded_by": 2, "retracted_at": "t"}), "retracted")

class EnrichSearchResultsTest(unittest.TestCase):
    def test_counts_current_and_superseded(self):
        cur = _Cursor()
        old = cur.seed(project="acme/widget", text="old", episode_id=101)
        new = cur.seed(project="acme/widget", text="new", episode_id=101)
        cur.rows[old]["superseded_by"] = new
        results = [{"kind": "episode", "id": "101"}, {"kind": "topic", "id": "x"}]
        out = de.enrich_search_results(cur, results)
        episode_row = next(r for r in out if r["kind"] == "episode")
        self.assertEqual(episode_row["decisions_current"], 1)
        self.assertEqual(episode_row["decisions_superseded"], 1)
        topic_row = next(r for r in out if r["kind"] == "topic")
        self.assertNotIn("decisions_current", topic_row)

class DecisionStatesForEpisodeTest(unittest.TestCase):
    def test_returns_states_and_counts(self):
        rows = [
            {"id": 1, "text": "current", "state": "current", "superseded_by": None},
            {"id": 2, "text": "old", "state": "superseded", "superseded_by": 3},
            {"id": 3, "text": "new", "state": "current", "superseded_by": None},
            {"id": 4, "text": "bad", "state": "retracted", "superseded_by": None},
        ]
        with mock.patch("khipu.decisions.decision_rows_for_episodes", return_value={7: rows}), \
                mock.patch.object(de, "_decisions_ready", return_value=True), \
                mock.patch("khipu.db.connect") as connect:
            fake_conn = connect.return_value.__enter__.return_value
            fake_conn.cursor.return_value.__enter__.return_value = mock.Mock()
            out = de.decision_states_for_episode(7)
        self.assertEqual(out["validity"], {"current": 2, "superseded": 1, "retracted": 1})
        self.assertEqual(len(out["decision_states"]), 4)

    def test_pre_migration_hub_is_empty_not_an_error(self):
        fake_conn = mock.MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.cursor.return_value.__enter__.return_value = _Cursor(evidence=False)
        with mock.patch("khipu.db.connect", return_value=fake_conn), \
                mock.patch.object(de, "_decisions_ready", return_value=False):
            out = de.decision_states_for_episode(7)
        self.assertEqual(out, {"decision_states": [], "validity": {"current": 0, "superseded": 0, "retracted": 0}})
