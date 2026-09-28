# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Real-SQL integration tests against an isolated, throwaway PostgreSQL 16
(Phase 2, session A addendum). These run ONLY when ``KHIPU_SCRATCH_DSN`` is
set — every other test in this suite is a fake-cursor unit test with no
database and no network; this file is the one place that talks to a real
Postgres, and it is gated hard so it can never accidentally reach anything
but the throwaway scratch database:

  cd packages/cli && S=$(mktemp -d) && KHIPU_SCRATCH_DSN='postgresql://khipu_scratch@/khipu_scratch?host=<socket dir>&port=54329' \\
      PYTHONPYCACHEPREFIX="$S/pyc" PYTHONPATH="$PWD:$PWD/../../.python_libs" \\
      python3.11 -m pytest -q -p no:cacheprovider tests/test_pg_scratch.py

The scratch database holds Khipu's relational schema through migration
0023 (no pgvector, no property graph; ``memory_embeddings`` is a stand-in
with a real[] column) — migration 0024 (this phase's decisions evidence/
lifecycle columns + ``decision_links``) is applied here, once, idempotently,
before every test. Every test cleans up the rows it creates.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from khipu import forget

SCRATCH_DSN = os.environ.get("KHIPU_SCRATCH_DSN")


def _scratch_dbname(dsn: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    return str((conninfo_to_dict(dsn) or {}).get("dbname") or "")


if SCRATCH_DSN:
    # Fail LOUDLY (a collection error, not a quiet skip) if this ever points
    # anywhere but a database whose name says "scratch" — this file applies
    # a migration and writes/deletes real rows.
    _dbname = _scratch_dbname(SCRATCH_DSN)
    assert "scratch" in _dbname.lower(), (
        f"refusing to run tests/test_pg_scratch.py against database {_dbname!r} — "
        "KHIPU_SCRATCH_DSN must name a database containing 'scratch'"
    )

pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN,
    reason="KHIPU_SCRATCH_DSN not set — this file only runs against the isolated scratch database",
)


def _direct_connect():
    """A connection made with EXACTLY the given DSN — never khipu.db's own
    Keychain/config-file resolution, so there is no path by which this
    could silently reach a different database."""
    import psycopg

    return psycopg.connect(SCRATCH_DSN)


def _migration_0024_sql() -> str:
    from khipu import migrate

    for version, path in migrate.available():
        if version == "0024_decision_evidence":
            return path.read_text(encoding="utf-8")
    raise AssertionError("0024_decision_evidence.sql not found under ops/migrations")


def _apply_migration_0024() -> None:
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_migration_0024_sql())
        conn.commit()


def _drop_migration_0024() -> None:
    """Undo 0024's additions for the "before the migration" tests. Only
    ever touches columns/table this migration itself added."""
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS decision_links")
            for col in ("source_kind", "evidence", "superseded_at", "supersede_source",
                        "supersede_reason", "retracted_at", "retract_reason"):
                cur.execute(f"ALTER TABLE decisions DROP COLUMN IF EXISTS {col}")
            cur.execute(
                "DELETE FROM schema_migrations WHERE version = '0024_decision_evidence'"
            )
        conn.commit()
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.clear()


@pytest.fixture(autouse=True)
def _ensure_migration_applied():
    """0024 is idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING
    throughout), so re-applying it before every test makes test order
    irrelevant — including after the dedicated "before migration" test,
    which restores it itself but this is a second, harmless guarantee."""
    _apply_migration_0024()
    from khipu import db as _db

    _db._TABLE_COLUMNS_CACHE.clear()
    yield


def _seed_project() -> str:
    """An invented, non-private project name (test_repo_hygiene.py enforces
    this) — never a real one."""
    return "acme/scratch-widget"


class DecisionsApiScratchTest(unittest.TestCase):
    """Every khipu.decisions API function against real Postgres."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []
        self.episode_id: int | None = None

    def tearDown(self):
        # Never trust a half-finished test to have cleaned up: roll back any
        # aborted transaction first (a raised refusal can leave one), then
        # delete every row this test created, by id, regardless of what
        # happened above.
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute(
                "DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,)
            )
        if self.episode_id is not None:
            self.cur.execute("DELETE FROM episodes WHERE id = %s", (self.episode_id,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()

    def _seed(self, text: str, **kw) -> int:
        from khipu import decisions as de

        did = de.create_decision(self.cur, project=_seed_project(), text=text, **kw)
        self.created_ids.append(did)
        self.conn.commit()
        return did

    def test_create_dedups_within_the_window(self):
        first = self._seed("Ship the scratch widget with cursor pagination")
        from khipu import decisions as de

        second = de.create_decision(
            self.cur, project=_seed_project(),
            text="Ship the scratch widget with cursor pagination",
        )
        self.conn.commit()
        self.assertEqual(first, second)

    def test_state_of_and_list_decisions_status_filters(self):
        from khipu import decisions as de

        old = self._seed("Use Apache 2.0 for the scratch widget")
        new = self._seed("Use AGPL-3.0 + CLA for the scratch widget")
        self.assertTrue(de.supersede(self.cur, old, new, reason="licensing pivot"))
        self.conn.commit()

        standing = de.list_decisions(self.cur, project=_seed_project(), status="standing")
        self.assertIn(new, {r["id"] for r in standing})
        self.assertNotIn(old, {r["id"] for r in standing})

        superseded = de.list_decisions(self.cur, project=_seed_project(), status="superseded")
        self.assertIn(old, {r["id"] for r in superseded})
        row = next(r for r in superseded if r["id"] == old)
        self.assertEqual(row["state"], "superseded")
        self.assertEqual(row["supersede_reason"], "licensing pivot")

    def test_supersede_refusals_are_real_sql_too(self):
        from khipu import decisions as de

        a = self._seed("Refusal case A")
        b = self._seed("Refusal case B")
        with self.assertRaises(ValueError):
            de.supersede(self.cur, a, a)
        with self.assertRaises(ValueError):
            de.supersede(self.cur, a, 2_000_000_000)  # no such decision
        self.assertTrue(de.supersede(self.cur, a, b))
        self.conn.commit()
        self.assertFalse(de.supersede(self.cur, a, b))  # already superseded -> False, not raise
        with self.assertRaises(ValueError):
            de.supersede(self.cur, b, a)  # would close a cycle

    def test_restore_and_retract_roundtrip(self):
        from khipu import decisions as de

        old = self._seed("Restore/retract case old")
        new = self._seed("Restore/retract case new")
        de.supersede(self.cur, old, new)
        self.conn.commit()
        self.assertTrue(de.restore(self.cur, old))
        self.conn.commit()
        self.assertTrue(de.retract(self.cur, old, "turned out to be wrong"))
        self.conn.commit()
        row = de.list_decisions(self.cur, project=_seed_project(), status="retracted")
        self.assertIn(old, {r["id"] for r in row})
        self.assertTrue(de.unretract(self.cur, old))
        self.conn.commit()

    def test_decision_states_for_episode_via_its_own_connection(self):
        """decision_states_for_episode opens ITS OWN connection — this only
        sees committed rows, unlike the rest of this class. decisions.
        episode_id has a real FK to episodes(id), so this needs an actual
        (invented) episode row, cleaned up alongside the decisions."""
        from khipu import decisions as de

        project = _seed_project()
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, scope, project, topics, "
            "people, decisions, preferences) VALUES "
            "(now(), 'claude_code:scratch', 'decision_states_for_episode scratch episode', "
            "%s, %s, '[]', '[]', '[]', '[]') RETURNING id",
            (project, project),
        )
        episode_id = self.cur.fetchone()[0]
        self.episode_id = episode_id
        old = self._seed("Episode-scoped decision old")
        new = self._seed("Episode-scoped decision new")
        self.cur.execute("UPDATE decisions SET episode_id = %s WHERE id = ANY(%s)",
                          (episode_id, [old, new]))
        de.supersede(self.cur, old, new)
        self.conn.commit()

        with mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN}):
            out = de.decision_states_for_episode(episode_id)
        self.assertEqual(out["validity"], {"current": 1, "superseded": 1, "retracted": 0})
        ids = {s["id"] for s in out["decision_states"]}
        self.assertEqual(ids, {old, new})


class LinkLifecycleScratchTest(unittest.TestCase):
    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []

    def tearDown(self):
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
            self.conn.commit()
        self.cur.close()
        self.conn.close()

    def _seed(self, text: str) -> int:
        from khipu import decisions as de

        did = de.create_decision(self.cur, project=_seed_project(), text=text)
        self.created_ids.append(did)
        self.conn.commit()
        return did

    def test_add_link_list_confirm_applies_the_supersession(self):
        from khipu import decisions as de

        old = self._seed("Link lifecycle old")
        new = self._seed("Link lifecycle new")
        link_id = de.add_link(self.cur, old, new, source="auto", reason="detected reversal")
        self.conn.commit()
        self.assertIsNotNone(link_id)

        candidates = de.list_links(self.cur, state="candidate", project=_seed_project())
        self.assertTrue(any(link["id"] == link_id for link in candidates))

        out = de.resolve_link(self.cur, link_id, "confirm", source="agent")
        self.conn.commit()
        self.assertTrue(out["ok"])
        self.assertTrue(out["applied"])

        row = de.list_decisions(self.cur, project=_seed_project(), status="superseded")
        self.assertIn(old, {r["id"] for r in row})

        with self.assertRaises(ValueError):
            de.resolve_link(self.cur, link_id, "confirm")  # not a candidate any more

    def test_add_link_reject_never_touches_the_decisions(self):
        from khipu import decisions as de

        old = self._seed("Reject case old")
        new = self._seed("Reject case new")
        link_id = de.add_link(self.cur, old, new)
        self.conn.commit()
        out = de.resolve_link(self.cur, link_id, "reject")
        self.conn.commit()
        self.assertTrue(out["ok"])
        standing = de.list_decisions(self.cur, project=_seed_project(), status="standing")
        self.assertIn(old, {r["id"] for r in standing})


class ForgetCascadeScratchTest(unittest.TestCase):
    """forget.forget_everywhere against real SQL: episode, commitment,
    deliverable and decision rows all cascade. Uses khipu.db.connect()
    (via KHIPU_DATABASE_URL) rather than a hand-held cursor, so this is the
    one test exercising the actual write path a real forget goes through."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.episode_id: int | None = None
        self.commitment_id: int | None = None
        self.deliverable_id: int | None = None
        self.decision_id: int | None = None

    def tearDown(self):
        self.conn.rollback()
        for table, col, val in (
            ("deliverables", "id", self.deliverable_id),
            ("commitments", "id", self.commitment_id),
            ("decisions", "id", self.decision_id),
            ("memory_embeddings", "ref", str(self.episode_id) if self.episode_id else None),
            ("episodes", "id", self.episode_id),
        ):
            if val is not None:
                self.cur.execute(f"DELETE FROM {table} WHERE {col} = %s", (val,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()

    def test_forget_everywhere_cascades_and_readers_stop_seeing_it(self):
        from khipu import activity, decisions as de, deliverables as dl

        project = _seed_project()
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, scope, project, topics, "
            "people, decisions, preferences) VALUES "
            "(now(), 'claude_code:scratch', 'a scratch forget-cascade episode', %s, %s, "
            "'[]', '[]', '[]', '[]') RETURNING id",
            (project, project),
        )
        self.episode_id = self.cur.fetchone()[0]
        self.cur.execute(
            "INSERT INTO commitments (text, project, kind, opened_episode, content_hash) "
            "VALUES ('follow up on the scratch cascade', %s, 'followup', %s, %s) RETURNING id",
            (project, self.episode_id, f"scratch-hash-{self.episode_id}"),
        )
        self.commitment_id = self.cur.fetchone()[0]
        self.cur.execute(
            "INSERT INTO deliverables (project, kind, path, episode_id) "
            "VALUES (%s, 'file', 'packages/cli/khipu/scratch_cascade.py', %s) RETURNING id",
            (project, self.episode_id),
        )
        self.deliverable_id = self.cur.fetchone()[0]
        self.decision_id = de.create_decision(
            self.cur, project=project, text="scratch cascade decision", session_id=None,
        )
        self.cur.execute(
            "UPDATE decisions SET episode_id = %s WHERE id = %s",
            (self.episode_id, self.decision_id),
        )
        self.conn.commit()

        with mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN}):
            out = forget.forget_everywhere(self.episode_id)

        self.assertTrue(out["ok"])
        self.assertTrue(out["soft_deleted"])
        self.assertEqual(out["commitments_closed"], 1)
        self.assertEqual(out["deliverables_removed"], 1)
        self.assertEqual(out["decisions_retracted"], 1)

        with mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN}):
            self.assertIsNone(activity.episode_detail(self.episode_id))

        standing = de.list_decisions(self.cur, project=project, status="all")
        self.assertNotIn(self.decision_id, {r["id"] for r in standing})

        remaining = dl.recent_deliverables(self.cur, project=project)
        self.assertNotIn(self.deliverable_id, {r["id"] for r in remaining})


class PreMigrationDegradeScratchTest(unittest.TestCase):
    """The same readers/writers, real SQL, with migration 0024 dropped —
    every gate degrades instead of raising. Restores 0024 in tearDown
    regardless of outcome (belt-and-suspenders with the autouse fixture on
    the NEXT test, which re-applies it unconditionally too)."""

    def setUp(self):
        _drop_migration_0024()
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []

    def tearDown(self):
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
            self.conn.commit()
        self.cur.close()
        self.conn.close()
        _apply_migration_0024()

    def test_gates_report_not_ready(self):
        from khipu import decisions as de

        self.assertFalse(de._evidence_ready(self.cur))
        self.assertFalse(de._links_ready(self.cur))

    def test_supersede_still_works_via_the_original_column_alone(self):
        from khipu import decisions as de

        old = de.create_decision(self.cur, project=_seed_project(), text="pre-migration old")
        new = de.create_decision(self.cur, project=_seed_project(), text="pre-migration new")
        self.created_ids += [old, new]
        self.conn.commit()
        self.assertTrue(de.supersede(self.cur, old, new, reason="ignored pre-migration"))
        self.conn.commit()
        row = de.list_decisions(self.cur, project=_seed_project(), status="superseded")
        hit = next(r for r in row if r["id"] == old)
        self.assertEqual(hit["state"], "superseded")
        self.assertNotIn("supersede_reason", hit)  # no evidence columns to carry it

    def test_retract_is_a_clean_no_op(self):
        from khipu import decisions as de

        did = de.create_decision(self.cur, project=_seed_project(), text="pre-migration retract")
        self.created_ids.append(did)
        self.conn.commit()
        self.assertFalse(de.retract(self.cur, did, "wrong"))

    def test_add_link_is_none_with_no_decision_links_table(self):
        from khipu import decisions as de

        old = de.create_decision(self.cur, project=_seed_project(), text="pre-migration link old")
        new = de.create_decision(self.cur, project=_seed_project(), text="pre-migration link new")
        self.created_ids += [old, new]
        self.conn.commit()
        self.assertIsNone(de.add_link(self.cur, old, new))
        self.assertEqual(de.list_links(self.cur), [])


class ToolPairScratchTest(unittest.TestCase):
    """khipu_decisions / khipu_decisions_update through handle_message —
    the actual JSON-RPC dispatch, against real Postgres."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []
        self.env_patch = mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
            self.conn.commit()
        self.cur.close()
        self.conn.close()

    def _call(self, name: str, arguments: dict) -> dict:
        import json

        from khipu.mcp_server import handle_message

        resp = handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(resp["result"]["isError"], body)
        return body

    def test_supersede_then_list_through_the_tool_pair(self):
        from khipu import decisions as de

        old = de.create_decision(self.cur, project=_seed_project(), text="tool pair old")
        new_seed = de.create_decision(self.cur, project=_seed_project(), text="tool pair new")
        self.created_ids += [old, new_seed]
        self.conn.commit()

        out = self._call(
            "khipu_decisions_update",
            {"action": "supersede", "id": old, "by": new_seed, "reason": "via the tool"},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["superseded_by"], new_seed)

        listed = self._call("khipu_decisions", {"project": _seed_project(), "status": "superseded"})
        self.assertIn(old, {r["id"] for r in listed["results"]})

    def test_supersede_with_new_text_creates_a_row_through_the_tool(self):
        old = None
        from khipu import decisions as de

        old = de.create_decision(self.cur, project=_seed_project(), text="tool pair new-text old")
        self.created_ids.append(old)
        self.conn.commit()

        out = self._call(
            "khipu_decisions_update",
            {"action": "supersede", "id": old, "new_text": "tool pair new-text replacement",
             "project": _seed_project(), "source_kind": "user"},
        )
        self.assertTrue(out["ok"])
        self.created_ids.append(out["superseded_by"])

        listed = self._call("khipu_decisions", {"project": _seed_project(), "status": "all"})
        by_id = {r["id"]: r for r in listed["results"]}
        self.assertEqual(by_id[out["superseded_by"]]["text"], "tool pair new-text replacement")
        self.assertEqual(by_id[out["superseded_by"]]["source_kind"], "user")


class ValidityScratchTest(unittest.TestCase):
    """Phase 2, session B: hub-side validity counts (decisions.enrich_
    search_results' three-way breakdown) and the replica's decision export/
    sync (hub_snapshot), both against real Postgres."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []
        self.episode_id: int | None = None
        self.env_patch = mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
        if self.episode_id is not None:
            self.cur.execute("DELETE FROM episodes WHERE id = %s", (self.episode_id,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()

    def _seed(self, text: str, **kw) -> int:
        from khipu import decisions as de

        did = de.create_decision(self.cur, project=_seed_project(), text=text, **kw)
        self.created_ids.append(did)
        self.conn.commit()
        return did

    def _seed_episode(self) -> int:
        project = _seed_project()
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, scope, project, topics, "
            "people, decisions, preferences) VALUES "
            "(now(), 'claude_code:scratch', 'validity scratch episode', "
            "%s, %s, '[]', '[]', '[]', '[]') RETURNING id",
            (project, project),
        )
        episode_id = self.cur.fetchone()[0]
        self.episode_id = episode_id
        self.conn.commit()
        return episode_id

    def test_enrich_search_results_three_way_breakdown_is_real_sql(self):
        from khipu import decisions as de

        episode_id = self._seed_episode()
        current = self._seed("current decision")
        superseder = self._seed("superseder decision")
        old = self._seed("old decision")
        retracted = self._seed("retracted decision")
        self.cur.execute(
            "UPDATE decisions SET episode_id = %s WHERE id = ANY(%s)",
            (episode_id, [current, superseder, old, retracted]),
        )
        self.conn.commit()
        self.assertTrue(de.supersede(self.cur, old, superseder))
        self.assertTrue(de.retract(self.cur, retracted, "wrong"))
        self.conn.commit()

        out = de.enrich_search_results(
            self.cur, [{"kind": "episode", "id": str(episode_id)}]
        )
        row = out[0]
        self.assertEqual(row["decisions_current"], 2)  # current + superseder
        self.assertEqual(row["decisions_superseded"], 1)  # old
        self.assertEqual(row["decisions_retracted"], 1)  # retracted

    def test_supersede_mirrors_to_the_local_replica_at_once(self):
        """decisions.supersede's best-effort _mirror_to_snapshot: a
        correction made on THIS machine reaches a real sqlite replica
        without waiting for sync_decision_changes."""
        from khipu import decisions as de
        from khipu import hub_snapshot as hs

        old = self._seed("mirror case old")
        new = self._seed("mirror case new")
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "hub_snapshot.sqlite"
            con = sqlite3.connect(str(snap))
            hs._create_schema(con)
            con.commit()
            con.close()
            meta_file = Path(tmp) / "meta.json"
            meta_file.write_text("{}", encoding="utf-8")
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                    mock.patch.object(hs, "meta_path", return_value=meta_file):
                self.assertTrue(de.supersede(self.cur, old, new, reason="mirrored"))
                self.conn.commit()
                con2 = sqlite3.connect(str(snap))
                row = con2.execute(
                    "SELECT superseded_by FROM decisions WHERE id = ?", (old,)
                ).fetchone()
        self.assertEqual(row[0], new)

    def test_sync_decision_changes_pulls_a_hub_supersession_into_the_replica(self):
        """"Corrections made elsewhere": a supersession already committed on
        the hub (no local mirror call involved) reaches a fresh replica via
        sync_decision_changes alone."""
        from khipu import decisions as de
        from khipu import hub_snapshot as hs

        old = self._seed("sync case old")
        new = self._seed("sync case new")
        self.assertTrue(de.supersede(self.cur, old, new, reason="synced from elsewhere"))
        self.conn.commit()

        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "hub_snapshot.sqlite"
            con = sqlite3.connect(str(snap))
            hs._create_schema(con)
            con.commit()
            con.close()
            meta_file = Path(tmp) / "meta.json"
            meta_file.write_text("{}", encoding="utf-8")
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                    mock.patch.object(hs, "meta_path", return_value=meta_file), \
                    mock.patch.object(hs, "try_hub_connect", side_effect=_direct_connect):
                out = hs.sync_decision_changes()
                self.assertTrue(out["ok"])
                self.assertGreaterEqual(out["decisions"], 2)  # at least old + new
                con2 = sqlite3.connect(str(snap))
                row = con2.execute(
                    "SELECT superseded_by FROM decisions WHERE id = ?", (old,)
                ).fetchone()
        self.assertEqual(row[0], new)


if __name__ == "__main__":
    unittest.main()
