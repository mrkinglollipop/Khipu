# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.briefs — staleness fingerprint, claim validation, source hygiene,
idempotent and crash-safe builds, the single-writer lock, the forget and
decision cascades, the ``khipu_brief`` MCP tool, and migration 0025's text.

No database and no network: an in-memory hub answers the SQL khipu.briefs
issues (dispatching on its leading text), and the model provider is always a
stub. The real-SQL half lives in tests/test_briefs_scratch.py.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from khipu import briefs, decisions, forget, migrate, recall_prompt

_T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_BRIEFS_COLUMNS = (
    "id", "topic_slug", "project", "body", "claims", "source_episode_ids", "source_hash",
    "derivation_version", "model", "state", "fail_reason", "created_at", "superseded_at",
)


class _Hub:
    """In-memory stand-in for topics, episodes, decisions and briefs. Only
    ``briefs`` is transactional (commit/rollback/savepoint); tests mutate the
    rest directly."""

    def __init__(self, *, briefs_table=True, decisions_table=True, evidence=True):
        self.briefs_table = briefs_table
        self.decisions_table = decisions_table
        self.evidence = evidence
        self.topics: set[str] = set()
        self.episodes: dict[int, dict] = {}
        self.decisions: dict[int, dict] = {}
        self.briefs: list[dict] = []
        self.next_brief_id = 1
        self.lock_free = True
        self.raise_on: str | None = None
        self.statements: list[str] = []
        self._committed = (copy.deepcopy(self.briefs), self.next_brief_id)
        self._savepoint = None

    # -- seeding ------------------------------------------------------------------

    def episode(self, eid, summary, *, topics=("alpha",), decisions=(), project="acme/widget",
                minutes=0, ingested_minutes=0):
        self.topics.update(topics)
        self.episodes[eid] = {
            "id": eid, "ts": _T0 + timedelta(minutes=minutes),
            "ingested_at": _T0 + timedelta(minutes=ingested_minutes),
            "summary": summary, "decisions": list(decisions), "project": project,
            "deleted": False, "topics": list(topics),
        }
        return eid

    def decision(self, did, episode_id, text, *, retracted=False, superseded_by=None):
        self.decisions[did] = {
            "id": did, "episode_id": episode_id, "text": text,
            "superseded_by": superseded_by, "retracted_at": "2026-09-02" if retracted else None,
        }
        return did

    def current(self, slug):
        rows = [b for b in self.briefs if b["topic_slug"] == slug and b["superseded_at"] is None
                and b["state"] in ("current", "stale")]
        return rows[-1] if rows else None

    def seed_brief(self, slug, *, body="OLD BRIEF BODY", state="current", source_hash="old", ids=(1,),
                   version=None, superseded=False, recent=True):
        row = {
            "id": self.next_brief_id, "topic_slug": slug, "project": None, "body": body,
            "claims": [{"text": "old claim", "episode_ids": list(ids)}],
            "source_episode_ids": list(ids), "source_hash": source_hash,
            "derivation_version": briefs.DERIVATION_VERSION if version is None else version,
            "model": "cloud:test", "state": state, "fail_reason": None,
            "created_at": _T0, "superseded_at": _T0 if superseded else None, "recent": recent,
        }
        self.next_brief_id += 1
        self.briefs.append(row)
        return row

    # -- connection surface -------------------------------------------------------

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self._committed = (copy.deepcopy(self.briefs), self.next_brief_id)

    def rollback(self):
        self.briefs, self.next_brief_id = copy.deepcopy(self._committed[0]), self._committed[1]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def columns(self, table):
        if table == "briefs":
            return list(_BRIEFS_COLUMNS) if self.briefs_table else []
        if table == "decisions":
            if not self.decisions_table:
                return []
            base = ["id", "project", "text", "rationale", "decided_at", "episode_id",
                    "session_id", "superseded_by", "created_at"]
            if self.evidence:
                base += ["source_kind", "evidence", "superseded_at", "supersede_source",
                         "supersede_reason", "retracted_at", "retract_reason"]
            return base
        if table == "episodes":
            return ["id", "deleted_at", "project"]
        if table == "topics":
            return ["slug", "deleted_at"]
        return []


class _Cur:
    def __init__(self, hub: _Hub):
        self.hub = hub
        self.rowcount = 0
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def _live(self, slug=None):
        return sorted(
            (e for e in self.hub.episodes.values()
             if not e["deleted"] and (slug is None or slug in e["topics"])),
            key=lambda e: (e["ts"], e["id"]), reverse=True,
        )

    def execute(self, sql, params=None):
        h = self.hub
        s = " ".join(sql.split())
        params = params or ()
        if h.raise_on and h.raise_on in s:
            raise RuntimeError("injected failure")
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            self._rows = [(c,) for c in h.columns(params[0])]
            return
        h.statements.append(s)
        self._rows = []
        self.rowcount = 0
        if s.startswith("SELECT t.slug, e.id, e.ingested_at, md5(e.summary)"):
            self._rows = [
                (slug, e["id"], e["ingested_at"], hashlib.md5(e["summary"].encode()).hexdigest(),
                 len(e["summary"]))
                for slug in sorted(h.topics) for e in self._live(slug)
            ]
        elif s.startswith("SELECT episode_id, id,"):
            with_text = "length(text)" not in s
            ids = set(params[0])
            self._rows = [
                (d["episode_id"], d["id"], d["text"] if with_text else len(d["text"]),
                 d["superseded_by"], d["retracted_at"] if h.evidence else None)
                for d in sorted(h.decisions.values(), key=lambda d: d["id"])
                if d["episode_id"] in ids
            ]
        elif s.startswith("SELECT topic_slug, source_hash, state, derivation_version FROM briefs"):
            self._rows = [
                (b["topic_slug"], b["source_hash"], b["state"], b["derivation_version"])
                for b in h.briefs if b["superseded_at"] is None and b["state"] in ("current", "stale")
            ]
        elif s.startswith("SELECT topic_slug, source_hash FROM briefs WHERE state = 'failed'"):
            self._rows = [(b["topic_slug"], b["source_hash"]) for b in h.briefs
                          if b["state"] == "failed" and b.get("recent", True)]
        elif s.startswith("SELECT e.id, e.ts, e.ingested_at, e.summary, e.decisions,"):
            self._rows = [
                (e["id"], e["ts"], e["ingested_at"], e["summary"], e["decisions"], e["project"])
                for e in self._live(params[0])
            ]
        elif s.startswith("SELECT id, source_hash, state, derivation_version FROM briefs"):
            b = h.current(params[0])
            self._rows = [(b["id"], b["source_hash"], b["state"], b["derivation_version"])] if b else []
        elif s.startswith("INSERT INTO briefs"):
            (slug, project, body, claims, ids, shash, version, model, state, reason) = params
            row = {
                "id": h.next_brief_id, "topic_slug": slug, "project": project, "body": body,
                "claims": json.loads(claims), "source_episode_ids": list(ids), "source_hash": shash,
                "derivation_version": version, "model": model, "state": state, "fail_reason": reason,
                "created_at": _T0, "superseded_at": None, "recent": True,
            }
            h.next_brief_id += 1
            h.briefs.append(row)
            self._rows = [(row["id"],)]
        elif s.startswith("UPDATE briefs SET superseded_at = now()"):
            slug, new_id = params
            for b in h.briefs:
                if (b["topic_slug"] == slug and b["id"] != new_id and b["superseded_at"] is None
                        and b["state"] in ("current", "stale")):
                    b["superseded_at"] = _T0
                    self.rowcount += 1
        elif s.startswith("UPDATE briefs SET state = 'stale'"):
            ids = set(params[0])
            for b in h.briefs:
                if b["state"] == "current" and b["superseded_at"] is None and ids & set(b["source_episode_ids"]):
                    b["state"] = "stale"
                    self.rowcount += 1
        elif s.startswith("SELECT episode_id FROM decisions WHERE id"):
            d = h.decisions.get(params[0])
            self._rows = [(d["episode_id"],)] if d else []
        elif s.startswith("SELECT id, body, claims, source_episode_ids, state, derivation_version, model,"):
            b = h.current(params[0])
            self._rows = [(
                b["id"], b["body"], b["claims"], b["source_episode_ids"], b["state"],
                b["derivation_version"], b["model"], b["created_at"], 3600,
            )] if b else []
        elif s.startswith("SELECT id FROM episodes WHERE id = ANY"):
            self._rows = [(i,) for i in params[0] if h.episodes.get(i, {}).get("deleted")]
        elif s.startswith("SELECT pg_try_advisory_lock"):
            self._rows = [(h.lock_free,)]
        elif s.startswith("SELECT pg_advisory_unlock"):
            self._rows = [(True,)]
        elif s == "SAVEPOINT briefs_swap":
            h._savepoint = (copy.deepcopy(h.briefs), h.next_brief_id)
        elif s == "RELEASE SAVEPOINT briefs_swap":
            h._savepoint = None
        elif s == "ROLLBACK TO SAVEPOINT briefs_swap":
            h.briefs, h.next_brief_id = copy.deepcopy(h._savepoint[0]), h._savepoint[1]
        else:
            raise AssertionError(f"unexpected SQL: {s}")


def _answer(claims, body="A short body."):
    return json.dumps({"body": body, "claims": claims})


def _claim(text, *ids):
    return {"text": text, "episode_ids": list(ids)}


def _seeded(**kw) -> _Hub:
    hub = _Hub(**kw)
    hub.episode(1, "Chose cursor pagination for the widget list.", minutes=0)
    hub.episode(2, "Shipped the widget list behind a flag.", minutes=5)
    return hub


def _build(hub, slug="alpha", answer=None):
    answer = answer or _answer([_claim("Pagination is cursor based.", 1), _claim("Shipped behind a flag.", 2)])
    with mock.patch("khipu.extract._generate", return_value=answer) as gen:
        with hub.cursor() as cur:
            out = briefs.build_one(cur, slug)
    hub.commit()
    return out, gen


def _plan(hub, **kw):
    with hub.cursor() as cur:
        return briefs.plan(cur, **kw)


def _planned(hub, slug="alpha"):
    return next((p for p in _plan(hub)["topics"] if p["topic"] == slug), None)


class StalenessFingerprintTest(unittest.TestCase):
    def test_a_topic_without_a_brief_is_planned_as_missing(self):
        hub = _seeded()
        entry = _planned(hub)
        self.assertEqual(entry["reason"], "missing")
        self.assertEqual(entry["sources"], 2)
        self.assertGreater(entry["characters"], 0)

    def test_a_built_brief_is_not_planned_again(self):
        hub = _seeded()
        _build(hub)
        self.assertIsNone(_planned(hub))

    def test_the_hash_is_stable_when_nothing_changes(self):
        hub = _seeded()
        first = _planned(hub)["source_hash"]
        hub.episode(9, "Unrelated work.", topics=("beta",))
        self.assertEqual(_planned(hub)["source_hash"], first)

    def test_a_new_episode_makes_it_stale(self):
        hub = _seeded()
        _build(hub)
        before = hub.current("alpha")["source_hash"]
        hub.episode(3, "Added an export button.", minutes=9)
        entry = _planned(hub)
        self.assertEqual(entry["reason"], "stale")
        self.assertNotEqual(entry["source_hash"], before)

    def test_a_changed_revision_makes_it_stale(self):
        hub = _seeded()
        _build(hub)
        hub.episodes[2]["summary"] = "Shipped the widget list, then rolled it back."
        self.assertEqual(_planned(hub)["reason"], "stale")

    def test_a_new_ingest_time_makes_it_stale(self):
        hub = _seeded()
        _build(hub)
        hub.episodes[1]["ingested_at"] += timedelta(hours=1)
        self.assertEqual(_planned(hub)["reason"], "stale")

    def test_a_forgotten_episode_makes_it_stale(self):
        hub = _seeded()
        _build(hub)
        hub.episodes[2]["deleted"] = True
        entry = _planned(hub)
        self.assertEqual(entry["reason"], "stale")
        self.assertEqual(entry["sources"], 1)

    def test_a_retracted_decision_makes_it_stale(self):
        hub = _seeded()
        hub.decision(10, 1, "Use cursor pagination")
        _build(hub)
        self.assertIsNone(_planned(hub))
        hub.decisions[10]["retracted_at"] = "2026-09-03"
        self.assertEqual(_planned(hub)["reason"], "stale")

    def test_a_superseded_decision_makes_it_stale(self):
        hub = _seeded()
        hub.decision(10, 1, "Use cursor pagination")
        hub.decision(11, 2, "Use offset pagination")
        _build(hub)
        hub.decisions[10]["superseded_by"] = 11
        self.assertEqual(_planned(hub)["reason"], "stale")

    def test_a_brief_marked_stale_or_older_than_the_derivation_is_replanned(self):
        hub = _seeded()
        _build(hub)
        hub.current("alpha")["state"] = "stale"
        self.assertEqual(_planned(hub)["reason"], "stale")
        hub.current("alpha")["state"] = "current"
        self.assertIsNone(_planned(hub))
        hub.current("alpha")["derivation_version"] = briefs.DERIVATION_VERSION - 1
        self.assertEqual(_planned(hub)["reason"], "stale")

    def test_the_hash_is_order_independent(self):
        a = briefs.source_hash([(2, "r2", ""), (1, "r1", "5:current")])
        b = briefs.source_hash([(1, "r1", "5:current"), (2, "r2", "")])
        self.assertEqual(a, b)
        self.assertNotEqual(a, briefs.source_hash([(1, "r1", "5:retracted"), (2, "r2", "")]))

    def test_a_recent_failure_on_the_same_sources_is_skipped_unless_retried(self):
        hub = _seeded()
        _build(hub, answer=_answer([_claim("Nothing cited.")]))
        self.assertEqual(hub.briefs[-1]["state"], "failed")
        out = _plan(hub)
        self.assertEqual(out["topics"], [])
        self.assertEqual(out["skipped_failed"], 1)
        self.assertEqual(_plan(hub, retry_failed=True)["topics"][0]["topic"], "alpha")
        hub.episode(3, "New information.", minutes=9)
        self.assertEqual(_planned(hub)["reason"], "missing")


class ClaimValidationTest(unittest.TestCase):
    def test_an_uncited_claim_is_removed(self):
        kept, removed = briefs.validate_claims([_claim("No citation"), _claim("Cited", 1)], {1, 2})
        self.assertEqual(kept, [{"text": "Cited", "episode_ids": [1]}])
        self.assertEqual(removed, 1)

    def test_an_unknown_id_is_removed_and_a_claim_left_with_none_goes_too(self):
        kept, removed = briefs.validate_claims(
            [_claim("Partly grounded", 1, 99), _claim("Ungrounded", 99)], {1, 2},
        )
        self.assertEqual(kept, [{"text": "Partly grounded", "episode_ids": [1]}])
        self.assertEqual(removed, 1)

    def test_malformed_claims_never_raise(self):
        kept, removed = briefs.validate_claims(
            ["text", None, {"text": "x", "episode_ids": "1"}, {"text": " ", "episode_ids": [1]},
             {"text": "ok", "episode_ids": [True, "1", 1.0, "bad"]}], {1},
        )
        self.assertEqual(kept, [{"text": "ok", "episode_ids": [1]}])
        self.assertEqual(removed, 4)
        self.assertEqual(briefs.validate_claims("nope", {1}), ([], 0))

    def test_a_claim_is_redacted_and_bounded(self):
        kept, _ = briefs.validate_claims(
            [_claim("token = abcdefghijklmnop123456 " + "x" * 900, 1)], {1},
        )
        self.assertNotIn("abcdefghijklmnop123456", kept[0]["text"])
        self.assertLessEqual(len(kept[0]["text"]), briefs.MAX_CLAIM_CHARS)

    def test_a_dropped_claim_does_not_survive_in_the_body(self):
        hub = _seeded()
        out, _ = _build(hub, answer=_answer(
            [_claim("Grounded fact.", 1), _claim("Made-up fact.")], body="Grounded fact. Made-up fact.",
        ))
        self.assertEqual(out["status"], "built")
        self.assertEqual(out["claims_removed"], 1)
        body = hub.current("alpha")["body"]
        self.assertIn("Grounded fact.", body)
        self.assertNotIn("Made-up fact.", body)

    def test_the_models_body_is_kept_when_every_claim_survives(self):
        hub = _seeded()
        _build(hub, answer=_answer([_claim("Fact.", 1)], body="The model's own summary."))
        self.assertEqual(hub.current("alpha")["body"], "The model's own summary.")

    def test_all_claims_removed_records_a_failure_and_keeps_the_previous_brief(self):
        hub = _seeded()
        _build(hub)
        previous = hub.current("alpha")
        hub.episode(3, "Added an export button.", minutes=9)
        out, _ = _build(hub, answer=_answer([_claim("Uncited"), _claim("Wrong id", 42)]))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(hub.current("alpha")["id"], previous["id"])
        self.assertEqual(hub.current("alpha")["body"], previous["body"])
        failed = [b for b in hub.briefs if b["state"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["fail_reason"])
        with hub.cursor() as cur:
            self.assertEqual(briefs.read_brief(cur, "alpha")["brief_id"], previous["id"])

    def test_a_non_json_answer_is_a_recorded_failure(self):
        hub = _seeded()
        out, _ = _build(hub, answer="not json at all")
        self.assertEqual(out["status"], "failed")
        self.assertEqual([b["state"] for b in hub.briefs], ["failed"])

    def test_a_provider_error_writes_nothing(self):
        hub = _seeded()
        with mock.patch("khipu.extract._generate", side_effect=RuntimeError("quota")):
            with hub.cursor() as cur:
                out = briefs.build_one(cur, "alpha")
        self.assertEqual(out["status"], "error")
        self.assertEqual(hub.briefs, [])


class SourceHygieneTest(unittest.TestCase):
    def _prompt(self, hub, slug="alpha"):
        _, gen = _build(hub, slug)
        return gen.call_args.args[0]

    def test_a_forgotten_episode_is_never_offered(self):
        hub = _seeded()
        hub.episode(3, "SECRET forgotten material.", minutes=9)
        hub.episodes[3]["deleted"] = True
        prompt = self._prompt(hub)
        self.assertNotIn("SECRET forgotten material.", prompt)
        self.assertNotIn("[episode 3,", prompt)

    def test_a_retracted_decisions_text_is_never_offered(self):
        hub = _seeded()
        hub.decision(10, 1, "RETRACTED choice of offset pagination", retracted=True)
        hub.decision(11, 1, "Use cursor pagination")
        prompt = self._prompt(hub)
        self.assertNotIn("RETRACTED choice", prompt)
        self.assertIn("Use cursor pagination", prompt)

    def test_a_superseded_decision_is_labelled(self):
        hub = _seeded()
        hub.decision(10, 1, "Use offset pagination", superseded_by=11)
        hub.decision(11, 2, "Use cursor pagination")
        self.assertIn("(superseded) Use offset pagination", self._prompt(hub))

    def test_without_the_decisions_table_the_episode_strings_are_used(self):
        hub = _Hub(decisions_table=False)
        hub.episode(1, "Chose pagination.", decisions=["Use cursor pagination"])
        self.assertIn("Use cursor pagination", self._prompt(hub))

    def test_an_injected_prior_work_block_is_stripped(self):
        hub = _seeded()
        injected = f"{recall_prompt._HEADING}\n- 812 (episode) DERIVED-LINE\n{recall_prompt._FOOTER}"
        hub.episodes[2]["summary"] = "Shipped the list.\n" + injected + "\nThen tested it."
        prompt = self._prompt(hub)
        self.assertNotIn("DERIVED-LINE", prompt)
        self.assertNotIn(recall_prompt._HEADING, prompt)
        self.assertIn("Shipped the list.", prompt)
        self.assertIn("Then tested it.", prompt)

    def test_an_episode_that_is_only_derived_material_is_not_a_source(self):
        hub = _seeded()
        hub.episode(3, f"{recall_prompt._HEADING}\n- DERIVED-ONLY\n{recall_prompt._FOOTER}", minutes=9)
        prompt = self._prompt(hub)
        self.assertNotIn("DERIVED-ONLY", prompt)
        self.assertNotIn("[episode 3,", prompt)
        self.assertNotIn(3, hub.current("alpha")["source_episode_ids"])

    def test_an_existing_brief_is_never_a_source(self):
        hub = _seeded()
        hub.seed_brief("alpha", body="OLD BRIEF BODY", source_hash="stale-hash")
        self.assertNotIn("OLD BRIEF BODY", self._prompt(hub))

    def test_secrets_are_redacted_before_they_reach_the_model(self):
        hub = _seeded()
        hub.episodes[1]["summary"] = "Configured api_key=abcdefghij1234567890 for the widget."
        self.assertNotIn("abcdefghij1234567890", self._prompt(hub))

    def test_the_offered_text_is_bounded_and_newest_first(self):
        hub = _Hub()
        for i in range(1, 40):
            hub.episode(i, f"Episode {i} " + "y" * 1400, minutes=i)
        _, gen = _build(hub, answer=_answer([_claim("Fact.", 39)]))
        prompt = gen.call_args.args[0]
        self.assertLessEqual(len(prompt), briefs.MAX_SOURCE_CHARS + 2_000)
        self.assertIn("[episode 39,", prompt)
        self.assertNotIn("[episode 1,", prompt)
        # The fingerprint still covers every live episode, offered or not.
        hub.episodes[1]["summary"] = "changed"
        self.assertEqual(_planned(hub)["reason"], "stale")

    def test_citations_are_limited_to_the_offered_set(self):
        hub = _seeded()
        hub.episode(3, "Forgotten.", minutes=9)
        hub.episodes[3]["deleted"] = True
        out, _ = _build(hub, answer=_answer([_claim("Cites the forgotten one.", 3), _claim("Fine.", 1)]))
        self.assertEqual(out["claims"], 1)
        self.assertEqual(hub.current("alpha")["source_episode_ids"], [1])


class BuildLifecycleTest(unittest.TestCase):
    def test_a_retry_with_the_same_sources_does_no_work(self):
        hub = _seeded()
        first, gen = _build(hub)
        self.assertEqual(first["status"], "built")
        with mock.patch("khipu.extract._generate") as gen2:
            with hub.cursor() as cur:
                second = briefs.build_one(cur, "alpha")
        self.assertEqual(second["status"], "unchanged")
        gen2.assert_not_called()
        self.assertEqual(len(hub.briefs), 1)

    def test_a_rebuild_inserts_first_and_supersedes_the_old_one(self):
        hub = _seeded()
        _build(hub)
        old_id = hub.current("alpha")["id"]
        hub.episode(3, "Added an export button.", minutes=9)
        out, _ = _build(hub, answer=_answer([_claim("Export exists.", 3)]))
        self.assertEqual(out["status"], "built")
        self.assertEqual(hub.current("alpha")["id"], out["brief_id"])
        old = next(b for b in hub.briefs if b["id"] == old_id)
        self.assertIsNotNone(old["superseded_at"])
        self.assertEqual(sum(1 for b in hub.briefs if b["superseded_at"] is None), 1)
        insert = next(i for i, s in enumerate(hub.statements) if s.startswith("INSERT INTO briefs") and i > 0)
        supersede = max(i for i, s in enumerate(hub.statements) if s.startswith("UPDATE briefs SET superseded_at"))
        self.assertLess(insert, supersede)

    def test_a_crash_between_insert_and_supersede_leaves_the_old_brief_current(self):
        hub = _seeded()
        _build(hub)
        before = copy.deepcopy(hub.briefs)
        hub.episode(3, "Added an export button.", minutes=9)
        hub.raise_on = "UPDATE briefs SET superseded_at"
        with mock.patch("khipu.extract._generate", return_value=_answer([_claim("Export exists.", 3)])):
            with hub.cursor() as cur:
                with self.assertRaises(RuntimeError):
                    briefs.build_one(cur, "alpha")
        self.assertEqual(hub.briefs, before)
        self.assertEqual(hub.current("alpha")["source_hash"], before[0]["source_hash"])
        hub.raise_on = None
        out, _ = _build(hub, answer=_answer([_claim("Export exists.", 3)]))
        self.assertEqual(out["status"], "built")

    def test_a_crash_between_topics_loses_nothing_already_committed(self):
        hub = _Hub()
        hub.episode(1, "Alpha work.", topics=("alpha",))
        hub.episode(2, "Beta work.", topics=("beta",))
        answers = iter([_answer([_claim("Alpha fact.", 1)]), RuntimeError("provider died")])

        def _gen(*a, **k):
            value = next(answers)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch("khipu.extract._generate", side_effect=_gen):
            results = briefs.build_many(hub, [{"topic": "alpha"}, {"topic": "beta"}])
        self.assertEqual([r["status"] for r in results], ["built", "error"])
        hub.rollback()
        self.assertIsNotNone(hub.current("alpha"))
        self.assertIsNone(hub.current("beta"))
        with mock.patch("khipu.extract._generate", return_value=_answer([_claim("Beta fact.", 2)])) as gen:
            again = briefs.build_many(hub, [{"topic": "alpha"}, {"topic": "beta"}])
        self.assertEqual([r["status"] for r in again], ["unchanged", "built"])
        self.assertEqual(gen.call_count, 1)

    def test_an_unexpected_exception_rolls_that_topic_back_and_the_batch_continues(self):
        hub = _Hub()
        hub.episode(1, "Alpha work.", topics=("alpha",))
        hub.episode(2, "Beta work.", topics=("beta",))
        hub.raise_on = "SELECT id, source_hash, state, derivation_version FROM briefs WHERE topic_slug"
        with mock.patch("khipu.extract._generate", return_value=_answer([_claim("Fact.", 1, 2)])):
            results = briefs.build_many(hub, [{"topic": "alpha"}, {"topic": "beta"}])
        self.assertEqual([r["status"] for r in results], ["error", "error"])
        self.assertEqual(hub.briefs, [])

    def test_a_topic_with_no_live_sources_is_reported_not_built(self):
        hub = _seeded()
        for e in hub.episodes.values():
            e["deleted"] = True
        out, gen = _build(hub)
        self.assertEqual(out["status"], "no_sources")
        gen.assert_not_called()

    def test_the_brief_records_project_model_and_version(self):
        hub = _seeded()
        _build(hub)
        row = hub.current("alpha")
        self.assertEqual(row["project"], "acme/widget")
        self.assertEqual(row["derivation_version"], briefs.DERIVATION_VERSION)
        self.assertEqual(sorted(row["source_episode_ids"]), [1, 2])


class SingleWriterAndCliTest(unittest.TestCase):
    def _run(self, hub, argv_ns, *, switch="1"):
        out, err = io.StringIO(), io.StringIO()
        env = {"KHIPU_FEATURE_BRIEFS": switch} if switch is not None else {}
        with mock.patch.dict(os.environ, env), \
             mock.patch("khipu.db.connect", return_value=hub), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = briefs.cli_main(SimpleNamespace(**argv_ns))
        return rc, out.getvalue(), err.getvalue()

    def _many(self, n=3):
        hub = _Hub()
        for i in range(1, n + 1):
            hub.episode(i, f"Work on topic {i}.", topics=(f"topic-{i}",))
        return hub

    def test_a_second_builder_is_refused_before_any_model_call(self):
        hub = self._many()
        hub.lock_free = False
        with mock.patch("khipu.extract._generate") as gen:
            rc, out, _ = self._run(hub, {"briefs_cmd": "build", "topic": None, "limit": 5})
        self.assertEqual(rc, 1)
        self.assertIn("another `khipu briefs build` is running", out)
        gen.assert_not_called()
        self.assertEqual(hub.briefs, [])

    def test_the_lock_is_a_session_advisory_lock_released_after_the_build(self):
        hub = self._many(1)
        with mock.patch("khipu.extract._generate", return_value=_answer([_claim("Fact.", 1)])):
            self._run(hub, {"briefs_cmd": "build", "topic": None, "limit": 5})
        kinds = [s.split("(")[0] for s in hub.statements if "advisory" in s]
        self.assertEqual(kinds, ["SELECT pg_try_advisory_lock", "SELECT pg_advisory_unlock"])

    def test_build_refuses_when_the_switch_is_off(self):
        hub = self._many()
        with mock.patch("khipu.extract._generate") as gen:
            rc, out, _ = self._run(hub, {"briefs_cmd": "build", "topic": None, "limit": 5}, switch="0")
        self.assertEqual(rc, 2)
        self.assertIn("switch is off", out)
        gen.assert_not_called()

    def test_build_prints_the_cost_and_honours_the_limit(self):
        hub = self._many(4)

        def _gen(prompt, **_):
            return _answer([_claim("Fact.", int(prompt.split("[episode ")[1].split(",")[0]))])

        with mock.patch("khipu.extract._generate", side_effect=_gen) as gen:
            rc, out, err = self._run(hub, {"briefs_cmd": "build", "topic": None, "limit": 2})
        self.assertEqual(rc, 0)
        self.assertEqual(gen.call_count, 2)
        self.assertIn("sending 2 topic(s)", err)
        report = json.loads(out)
        self.assertEqual(report["cost"]["topics"], 2)
        self.assertGreater(report["cost"]["characters"], 0)
        self.assertEqual([r["status"] for r in report["results"]], ["built", "built"])

    def test_build_topic_takes_only_that_topic(self):
        hub = self._many(3)
        with mock.patch("khipu.extract._generate", return_value=_answer([_claim("Fact.", 2)])):
            rc, out, _ = self._run(hub, {"briefs_cmd": "build", "topic": "topic-2", "limit": 5})
        self.assertEqual(rc, 0)
        self.assertEqual([r["topic"] for r in json.loads(out)["results"]], ["topic-2"])

    def test_build_topic_that_is_not_planned_says_so(self):
        hub = self._many(1)
        with mock.patch("khipu.extract._generate") as gen:
            rc, out, _ = self._run(hub, {"briefs_cmd": "build", "topic": "nope", "limit": 5})
        self.assertEqual(rc, 0)
        gen.assert_not_called()
        self.assertIn("'nope'", json.loads(out)["note"])

    def test_build_without_the_table_says_so(self):
        hub = _Hub(briefs_table=False)
        rc, out, _ = self._run(hub, {"briefs_cmd": "build", "topic": None, "limit": 5})
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["available"])

    def test_plan_and_show_print_json(self):
        hub = _seeded()
        _build(hub)
        rc, out, _ = self._run(hub, {"briefs_cmd": "plan"})
        self.assertEqual((rc, json.loads(out)["topics"]), (0, []))
        rc, out, _ = self._run(hub, {"briefs_cmd": "show", "slug": "alpha"})
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["found"])
        rc, _, _ = self._run(hub, {"briefs_cmd": "show", "slug": "missing"})
        self.assertEqual(rc, 1)

    def test_the_cli_parser_takes_the_documented_flags(self):
        from khipu import cli

        args = cli.build_parser().parse_args(["briefs", "build", "--topic", "alpha", "--limit", "3"])
        self.assertEqual((args.briefs_cmd, args.topic, args.limit), ("build", "alpha", 3))
        self.assertEqual(cli.build_parser().parse_args(["briefs", "build"]).limit, briefs.DEFAULT_BUILD_LIMIT)
        self.assertEqual(cli.build_parser().parse_args(["briefs", "show", "alpha"]).slug, "alpha")
        self.assertEqual(cli.build_parser().parse_args(["briefs", "plan"]).briefs_cmd, "plan")


class CascadeTest(unittest.TestCase):
    def test_marking_episodes_stales_only_briefs_that_cite_them(self):
        hub = _seeded()
        hub.episode(5, "Beta work.", topics=("beta",))
        _build(hub)
        _build(hub, "beta", answer=_answer([_claim("Beta fact.", 5)]))
        with hub.cursor() as cur:
            self.assertEqual(briefs.mark_stale_for_episodes(cur, [2]), 1)
        self.assertEqual(hub.current("alpha")["state"], "stale")
        self.assertEqual(hub.current("beta")["state"], "current")

    def test_a_decision_change_stales_briefs_citing_its_episode(self):
        hub = _seeded()
        hub.decision(10, 1, "Use cursor pagination")
        _build(hub)
        with hub.cursor() as cur:
            self.assertEqual(briefs.mark_stale_for_decision(cur, 10), 1)
        self.assertEqual(hub.current("alpha")["state"], "stale")
        self.assertEqual(_planned(hub)["reason"], "stale")

    def test_an_unknown_or_episodeless_decision_marks_nothing(self):
        hub = _seeded()
        hub.decision(10, None, "Orphan decision")
        _build(hub)
        with hub.cursor() as cur:
            self.assertEqual(briefs.mark_stale_for_decision(cur, 10), 0)
            self.assertEqual(briefs.mark_stale_for_decision(cur, 404), 0)
        self.assertEqual(hub.current("alpha")["state"], "current")

    def test_a_superseded_or_failed_brief_is_left_alone(self):
        hub = _seeded()
        hub.seed_brief("alpha", superseded=True, ids=(1,))
        hub.seed_brief("alpha", state="failed", ids=(1,))
        with hub.cursor() as cur:
            self.assertEqual(briefs.mark_stale_for_episodes(cur, [1]), 0)

    def test_decision_lifecycle_writes_call_the_cascade(self):
        cur = mock.MagicMock()
        cur.rowcount = 1
        cur.fetchone.return_value = None
        with mock.patch.object(decisions, "_evidence_ready", return_value=True), \
             mock.patch.object(decisions, "_mirror_to_snapshot"), \
             mock.patch("khipu.briefs.mark_stale_for_decision") as stale:
            decisions.retract(cur, 7, "wrong")
            decisions.unretract(cur, 8)
        self.assertEqual([c.args[1] for c in stale.call_args_list], [7, 8])

    def test_supersede_and_restore_call_the_cascade(self):
        cur = mock.MagicMock()
        cur.rowcount = 1
        rows = {1: {"id": 1, "project": "p", "superseded_by": None},
                2: {"id": 2, "project": "p", "superseded_by": None}}
        with mock.patch.object(decisions, "_fetch_decision_row", side_effect=lambda c, i: rows[i]), \
             mock.patch.object(decisions, "_chain_leads_to", return_value=False), \
             mock.patch.object(decisions, "_evidence_ready", return_value=False), \
             mock.patch.object(decisions, "_links_ready", return_value=False), \
             mock.patch.object(decisions, "_mirror_to_snapshot"), \
             mock.patch("khipu.briefs.mark_stale_for_decision") as stale:
            self.assertTrue(decisions.supersede(cur, 1, 2))
            self.assertTrue(decisions.restore(cur, 1))
        self.assertEqual([c.args[1] for c in stale.call_args_list], [1, 1])

    def test_a_failing_cascade_never_fails_the_decision_write(self):
        cur = mock.MagicMock()
        cur.rowcount = 1
        with mock.patch.object(decisions, "_evidence_ready", return_value=True), \
             mock.patch.object(decisions, "_mirror_to_snapshot"), \
             mock.patch("khipu.briefs._ready", side_effect=RuntimeError("boom")):
            self.assertTrue(decisions.retract(cur, 7, "wrong"))


class _ForgetCur:
    def __init__(self, *, briefs_table):
        self.briefs_table = briefs_table
        self.sql: list[tuple[str, tuple]] = []
        self.rowcount = 1
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "information_schema.columns" in s:
            table = params[0]
            self._rows = [(c,) for c in _BRIEFS_COLUMNS] if (table == "briefs" and self.briefs_table) else []
            return
        self.sql.append((s, params))

    def fetchone(self):
        return (_T0, "Shipped the widget", "claude_code:abc")

    def fetchall(self):
        return list(self._rows)


class ForgetCascadeTest(unittest.TestCase):
    def test_forgetting_an_episode_stales_the_briefs_citing_it(self):
        cur = _ForgetCur(briefs_table=True)
        out = forget.forget_episode(cur, 41)
        self.assertEqual(out["briefs_staled"], 1)
        sql, params = next((s, p) for s, p in cur.sql if s.startswith("UPDATE briefs"))
        self.assertIn("state = 'stale'", sql)
        self.assertIn("source_episode_ids &&", sql)
        self.assertEqual(params, ([41],))

    def test_without_the_table_the_forget_touches_no_brief_sql(self):
        cur = _ForgetCur(briefs_table=False)
        out = forget.forget_episode(cur, 41)
        self.assertEqual(out["briefs_staled"], 0)
        self.assertFalse(any("briefs" in s for s, _ in cur.sql))

    def test_a_forgotten_source_withholds_the_brief_body(self):
        hub = _seeded()
        _build(hub)
        hub.episodes[2]["deleted"] = True
        with hub.cursor() as cur:
            payload = briefs.read_brief(cur, "alpha")
        self.assertEqual(payload["body"], "")
        self.assertEqual(payload["claims"], [])
        self.assertEqual(payload["state"], "stale")
        self.assertIn("forgotten", payload["withheld"])


class ReaderAndToolTest(unittest.TestCase):
    def _call(self, hub, args, *, switch="1"):
        from khipu import mcp_server as srv

        env = {"KHIPU_FEATURE_BRIEFS": switch} if switch is not None else {}
        with mock.patch.dict(os.environ, env), mock.patch("khipu.db.connect", return_value=hub):
            return srv._tool_brief(args)

    def test_the_tool_is_declared_read_only(self):
        from khipu import mcp_server as srv

        tool = next(t for t in srv.TOOLS if t["name"] == "khipu_brief")
        self.assertIs(tool["annotations"]["readOnlyHint"], True)
        self.assertEqual(tool["inputSchema"]["required"], ["topic"])
        self.assertIn("khipu_brief", srv.TOOL_FUNCS)

    def test_switch_off_is_the_unavailable_shape_without_touching_the_database(self):
        with mock.patch("khipu.db.connect", side_effect=AssertionError("no connection")):
            with mock.patch.dict(os.environ, {"KHIPU_FEATURE_BRIEFS": "0"}):
                from khipu import mcp_server as srv

                out = srv._tool_brief({"topic": "alpha"})
        self.assertEqual(out, {"available": False, "reason": briefs.REASON_SWITCH_OFF})

    def test_table_absent_is_the_unavailable_shape(self):
        out = self._call(_Hub(briefs_table=False), {"topic": "alpha"})
        self.assertEqual(out, {"available": False, "reason": briefs.REASON_TABLE_MISSING})

    def test_a_topic_with_no_brief_is_found_false(self):
        out = self._call(_seeded(), {"topic": "alpha"})
        self.assertTrue(out["available"])
        self.assertFalse(out["found"])

    def test_a_brief_reads_back_with_claims_state_age_and_count(self):
        hub = _seeded()
        _build(hub)
        out = self._call(hub, {"topic": "alpha"})
        self.assertTrue(out["found"])
        self.assertEqual(out["state"], "current")
        self.assertEqual(out["source_count"], 2)
        self.assertEqual(out["age_seconds"], 3600)
        self.assertTrue(out["derived"])
        self.assertEqual(out["claims"][0], {"text": "Pagination is cursor based.", "episode_ids": [1]})
        self.assertIn("Pagination", out["body"] + out["claims"][0]["text"])

    def test_a_stale_brief_still_reads_and_says_so(self):
        hub = _seeded()
        _build(hub)
        hub.episode(3, "New.", minutes=9)
        with hub.cursor() as cur:
            briefs.mark_stale_for_episodes(cur, [1])
        self.assertEqual(self._call(hub, {"topic": "alpha"})["state"], "stale")

    def test_a_missing_topic_argument_is_a_tool_error(self):
        from khipu import mcp_server as srv

        with self.assertRaises(ValueError):
            srv._tool_brief({"topic": "  "})
        reply = srv.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "khipu_brief", "arguments": {}},
        })
        self.assertTrue(reply["result"]["isError"])

    def test_every_function_survives_a_missing_table(self):
        hub = _Hub(briefs_table=False)
        with hub.cursor() as cur:
            self.assertFalse(briefs.plan(cur)["available"])
            self.assertEqual(briefs.plan(cur)["topics"], [])
            self.assertEqual(briefs.build_one(cur, "alpha")["status"], "unavailable")
            self.assertFalse(briefs.read_brief(cur, "alpha")["available"])
            self.assertEqual(briefs.mark_stale_for_episodes(cur, [1]), 0)
            self.assertEqual(briefs.mark_stale_for_decision(cur, 1), 0)
            self.assertFalse(briefs.select_for_build(cur, None, 5)["available"])

    def test_the_capability_is_advertised(self):
        from khipu import features

        self.assertIn("briefs.read", features.capabilities())


class MigrationTextTest(unittest.TestCase):
    def _sql(self) -> str:
        for version, path in migrate.available():
            if version == "0025_briefs":
                return path.read_text(encoding="utf-8")
        self.fail("0025_briefs.sql not found under ops/migrations")

    def test_it_self_records(self):
        sql = self._sql()
        self.assertIn("INSERT INTO schema_migrations", sql)
        self.assertIn("'0025_briefs'", sql)
        self.assertIn("ON CONFLICT (version) DO NOTHING", sql)

    def test_it_is_additive_and_idempotent(self):
        sql = self._sql()
        for stmt in ("CREATE TABLE IF NOT EXISTS briefs", "CREATE INDEX IF NOT EXISTS"):
            self.assertIn(stmt, sql)
        for forbidden in ("DROP ", "ALTER TABLE", "UNIQUE", "TRUNCATE", "DELETE "):
            self.assertNotIn(forbidden, sql)

    def test_it_carries_every_column_the_code_reads_and_writes(self):
        sql = self._sql()
        for col in _BRIEFS_COLUMNS:
            with self.subTest(col=col):
                self.assertRegex(sql, rf"(?m)^\s+{col}\s")


if __name__ == "__main__":
    unittest.main()
