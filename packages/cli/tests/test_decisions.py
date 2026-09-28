# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.decisions — the evidence/lifecycle API added in Phase 2, session A
(migration 0024: source_kind/evidence/superseded_at/supersede_source/
supersede_reason/retracted_at/retract_reason on ``decisions``, plus the
``decision_links`` table). Every function is exercised BEFORE and AFTER the
migration, against an in-memory fake — no live database, same posture as
every other decisions test in this suite (test_commitments_contract.py).
"""
from __future__ import annotations

import unittest

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
            # dedup lookup (create_decision / insert_decisions_from_episode)
            project, norm_text = params[0], params[1]
            hit = None
            for r in self.rows.values():
                if r["project"] == project and de.normalize_text(r["text"]) == norm_text:
                    hit = r["id"]
                    break
            self._result = [(hit,)] if hit is not None else []
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


class SupersedeTest(unittest.TestCase):
    def test_supersede_succeeds_pre_migration(self):
        cur = _Cursor(evidence=False)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        self.assertTrue(de.supersede(cur, old, new))
        self.assertEqual(cur.rows[old]["superseded_by"], new)
        self.assertIsNone(cur.rows[old]["superseded_at"])  # no evidence columns pre-migration

    def test_supersede_records_evidence_post_migration(self):
        cur = _Cursor(evidence=True)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        self.assertTrue(de.supersede(cur, old, new, reason="user reversed it"))
        self.assertEqual(cur.rows[old]["supersede_source"], "manual")
        self.assertEqual(cur.rows[old]["supersede_reason"], "user reversed it")

    def test_self_reference_raises(self):
        cur = _Cursor()
        did = cur.seed(project="acme/widget", text="x")
        with self.assertRaises(ValueError):
            de.supersede(cur, did, did)

    def test_unknown_id_raises(self):
        cur = _Cursor()
        did = cur.seed(project="acme/widget", text="x")
        with self.assertRaises(ValueError):
            de.supersede(cur, did, 9999)
        with self.assertRaises(ValueError):
            de.supersede(cur, 9999, did)

    def test_already_superseded_returns_false_not_raise(self):
        cur = _Cursor()
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        self.assertTrue(de.supersede(cur, old, new))
        self.assertFalse(de.supersede(cur, old, new))

    def test_cross_project_needs_force(self):
        cur = _Cursor()
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/rocket", text="new")
        with self.assertRaises(ValueError):
            de.supersede(cur, old, new)
        self.assertTrue(de.supersede(cur, old, new, force=True))

    def test_cycle_is_refused(self):
        cur = _Cursor()
        a = cur.seed(project="acme/widget", text="a")
        b = cur.seed(project="acme/widget", text="b")
        self.assertTrue(de.supersede(cur, a, b))  # a -> b
        with self.assertRaises(ValueError):
            de.supersede(cur, b, a)  # would close the loop b -> a -> b

    def test_records_applied_link_when_table_exists(self):
        cur = _Cursor(evidence=True, links=True)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        de.supersede(cur, old, new, reason="because")
        link = next(iter(cur.links.values()))
        self.assertEqual(link["state"], "applied")
        self.assertEqual(link["old_id"], old)
        self.assertEqual(link["new_id"], new)


class RestoreRetractTest(unittest.TestCase):
    def test_restore_clears_supersession(self):
        cur = _Cursor(evidence=True)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        de.supersede(cur, old, new)
        self.assertTrue(de.restore(cur, old))
        self.assertIsNone(cur.rows[old]["superseded_by"])

    def test_restore_unknown_id_raises(self):
        cur = _Cursor()
        with self.assertRaises(ValueError):
            de.restore(cur, 9999)

    def test_retract_pre_migration_is_a_noop(self):
        cur = _Cursor(evidence=False)
        did = cur.seed(project="acme/widget", text="x")
        self.assertFalse(de.retract(cur, did, "wrong"))

    def test_retract_and_unretract_post_migration(self):
        cur = _Cursor(evidence=True)
        did = cur.seed(project="acme/widget", text="x")
        self.assertTrue(de.retract(cur, did, "wrong"))
        self.assertIsNotNone(cur.rows[did]["retracted_at"])
        self.assertTrue(de.unretract(cur, did))
        self.assertIsNone(cur.rows[did]["retracted_at"])


class CreateDecisionTest(unittest.TestCase):
    def test_creates_a_new_row(self):
        cur = _Cursor(evidence=True)
        did = de.create_decision(cur, project="acme/widget", text="Ship it", source_kind="user")
        self.assertEqual(cur.rows[did]["text"], "Ship it")
        self.assertEqual(cur.rows[did]["source_kind"], "user")

    def test_dedups_within_the_window(self):
        cur = _Cursor(evidence=True)
        first = de.create_decision(cur, project="acme/widget", text="Ship it",
                                    decided_at="2026-09-01T00:00:00+00:00")
        second = de.create_decision(cur, project="acme/widget", text="Ship it",
                                     decided_at="2026-09-05T00:00:00+00:00")
        self.assertEqual(first, second)
        self.assertEqual(len(cur.rows), 1)

    def test_empty_text_raises(self):
        cur = _Cursor()
        with self.assertRaises(ValueError):
            de.create_decision(cur, project="acme/widget", text="   ")


class LinkLifecycleTest(unittest.TestCase):
    def test_add_link_is_none_pre_migration(self):
        cur = _Cursor(links=False)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        self.assertIsNone(de.add_link(cur, old, new))

    def test_add_link_then_list_and_resolve_confirm(self):
        cur = _Cursor(evidence=True, links=True)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        link_id = de.add_link(cur, old, new, source="auto", reason="detected reversal")
        self.assertIsNotNone(link_id)
        candidates = de.list_links(cur, state="candidate")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["old_text"], "old")
        out = de.resolve_link(cur, link_id, "confirm", source="agent")
        self.assertTrue(out["ok"])
        self.assertTrue(out["applied"])
        self.assertEqual(cur.rows[old]["superseded_by"], new)
        self.assertEqual(cur.links[link_id]["state"], "applied")

    def test_resolve_reject(self):
        cur = _Cursor(links=True)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        link_id = de.add_link(cur, old, new)
        out = de.resolve_link(cur, link_id, "reject")
        self.assertTrue(out["ok"])
        self.assertEqual(cur.links[link_id]["state"], "rejected")
        self.assertIsNone(cur.rows[old]["superseded_by"])

    def test_resolve_link_twice_raises(self):
        cur = _Cursor(links=True)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        link_id = de.add_link(cur, old, new)
        de.resolve_link(cur, link_id, "reject")
        with self.assertRaises(ValueError):
            de.resolve_link(cur, link_id, "reject")

    def test_restore_marks_the_applied_link_restored(self):
        cur = _Cursor(evidence=True, links=True)
        old = cur.seed(project="acme/widget", text="old")
        new = cur.seed(project="acme/widget", text="new")
        de.supersede(cur, old, new)
        de.restore(cur, old)
        link = next(iter(cur.links.values()))
        self.assertEqual(link["state"], "restored")


class ListDecisionsStatusTest(unittest.TestCase):
    def _cursor(self):
        cur = _Cursor(evidence=True)
        current = cur.seed(project="acme/widget", text="current one",
                            decided_at="2026-09-10T00:00:00+00:00")
        old = cur.seed(project="acme/widget", text="superseded one",
                        decided_at="2026-09-01T00:00:00+00:00")
        new = cur.seed(project="acme/widget", text="superseder",
                        decided_at="2026-09-09T00:00:00+00:00")
        cur.rows[old]["superseded_by"] = new
        retracted = cur.seed(project="acme/widget", text="retracted one",
                              decided_at="2026-09-02T00:00:00+00:00",
                              retracted_at="2026-09-03T00:00:00+00:00")
        return cur, current, old, new, retracted

    def test_standing_excludes_superseded_and_retracted(self):
        cur, current, old, new, retracted = self._cursor()
        rows = de.list_decisions(cur, project="acme/widget", status="standing")
        ids = {r["id"] for r in rows}
        self.assertEqual(ids, {current, new})

    def test_superseded_status(self):
        cur, current, old, new, retracted = self._cursor()
        rows = de.list_decisions(cur, project="acme/widget", status="superseded")
        self.assertEqual({r["id"] for r in rows}, {old})

    def test_retracted_status(self):
        cur, current, old, new, retracted = self._cursor()
        rows = de.list_decisions(cur, project="acme/widget", status="retracted")
        self.assertEqual({r["id"] for r in rows}, {retracted})

    def test_retracted_status_pre_migration_is_empty(self):
        cur = _Cursor(evidence=False)
        cur.seed(project="acme/widget", text="x")
        rows = de.list_decisions(cur, project="acme/widget", status="retracted")
        self.assertEqual(rows, [])

    def test_all_status_returns_everything(self):
        cur, current, old, new, retracted = self._cursor()
        rows = de.list_decisions(cur, project="acme/widget", status="all")
        self.assertEqual({r["id"] for r in rows}, {current, old, new, retracted})

    def test_bad_status_raises(self):
        cur, *_ = self._cursor()
        with self.assertRaises(ValueError):
            de.list_decisions(cur, project="acme/widget", status="bogus")

    def test_rows_carry_state(self):
        cur, current, old, new, retracted = self._cursor()
        rows = de.list_decisions(cur, project="acme/widget", status="all")
        by_id = {r["id"]: r["state"] for r in rows}
        self.assertEqual(by_id[current], "current")
        self.assertEqual(by_id[old], "superseded")
        self.assertEqual(by_id[retracted], "retracted")

    def test_default_include_superseded_true_is_unchanged(self):
        """The pre-Phase-2A calling convention (no `status`) still works."""
        cur, current, old, new, retracted = self._cursor()
        rows = de.list_decisions(cur, project="acme/widget")
        self.assertIn(old, {r["id"] for r in rows})

    def test_include_superseded_false_is_unchanged(self):
        cur, current, old, new, retracted = self._cursor()
        rows = de.list_decisions(cur, project="acme/widget", include_superseded=False)
        self.assertNotIn(old, {r["id"] for r in rows})


class ForgottenEpisodeExclusionTest(unittest.TestCase):
    def test_list_decisions_excludes_a_forgotten_episodes_rows(self):
        cur = _Cursor(evidence=True, episodes_deleted_at=True)
        live = cur.seed(project="acme/widget", text="live episode decision", episode_id=1)
        gone = cur.seed(project="acme/widget", text="forgotten episode decision", episode_id=2)
        no_episode = cur.seed(project="acme/widget", text="no episode at all", episode_id=None)
        cur.forgotten_episodes = {2}
        rows = de.list_decisions(cur, project="acme/widget", status="all")
        ids = {r["id"] for r in rows}
        self.assertIn(live, ids)
        self.assertIn(no_episode, ids)
        self.assertNotIn(gone, ids)

    def test_pre_migration_hub_with_no_deleted_at_excludes_nothing(self):
        cur = _Cursor(episodes_deleted_at=False)
        gone = cur.seed(project="acme/widget", text="forgotten episode decision", episode_id=2)
        cur.forgotten_episodes = {2}
        rows = de.list_decisions(cur, project="acme/widget", status="all")
        self.assertIn(gone, {r["id"] for r in rows})

    def test_standing_decisions_also_excludes_forgotten(self):
        cur = _Cursor(evidence=True)
        gone = cur.seed(project="acme/widget", text="forgotten", episode_id=2,
                         decided_at="2026-09-10T00:00:00+00:00")
        cur.forgotten_episodes = {2}
        rows = de.standing_decisions(cur, project="acme/widget", since="2026-01-01T00:00:00+00:00")
        self.assertNotIn(gone, {r["id"] for r in rows})


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
        cur = _Cursor(evidence=True)
        cur.seed(project="acme/widget", text="current", episode_id=7)
        old = cur.seed(project="acme/widget", text="old", episode_id=7)
        new = cur.seed(project="acme/widget", text="new", episode_id=7)
        cur.rows[old]["superseded_by"] = new
        cur.seed(project="acme/widget", text="bad", episode_id=7, retracted_at="t")
        from unittest import mock

        fake_conn = mock.MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.cursor.return_value.__enter__.return_value = cur
        with mock.patch("khipu.db.connect", return_value=fake_conn):
            out = de.decision_states_for_episode(7)
        self.assertEqual(out["validity"], {"current": 2, "superseded": 1, "retracted": 1})
        self.assertEqual(len(out["decision_states"]), 4)

    def test_pre_migration_hub_is_empty_not_an_error(self):
        cur = _Cursor(evidence=False)
        cur.next_id = 1  # decisions table itself not "ready" (no base probe seeded)

        from unittest import mock

        fake_conn = mock.MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.cursor.return_value.__enter__.return_value = cur
        with mock.patch("khipu.db.connect", return_value=fake_conn), \
                mock.patch.object(de, "_decisions_ready", return_value=False):
            out = de.decision_states_for_episode(7)
        self.assertEqual(out, {"decision_states": [], "validity": {"current": 0, "superseded": 0, "retracted": 0}})


if __name__ == "__main__":
    unittest.main()
