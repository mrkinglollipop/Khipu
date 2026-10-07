"""khipu migrate — the only supported way to apply the schema on a fresh
machine. Never touches a real database here: the cursor is faked.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import migrate


class FakeCursor:
    def __init__(self, applied, table_exists=True):
        self.applied = set(applied)
        self.table_exists = table_exists
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "to_regclass" in sql:
            self._rows = [(self.table_exists,)]
        elif sql.strip().startswith("SELECT version"):
            self._rows = [(v,) for v in sorted(self.applied)]
        elif sql.startswith("INSERT INTO schema_migrations"):
            self.applied.add(params[0])
        else:
            # a migration body: the file records itself
            for line in sql.splitlines():
                if line.startswith("-- version:"):
                    self.applied.add(line.split(":", 1)[1].strip())

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _dir(names):
    d = tempfile.mkdtemp()
    for n in names:
        (Path(d) / f"{n}.sql").write_text(f"-- version: {n}\nSELECT 1;\n")
    return Path(d)


class PlanTest(unittest.TestCase):
    def test_pending_is_available_minus_applied_in_order(self):
        d = _dir(["0000_a", "0001_b", "0002_c"])
        cur = FakeCursor(applied={"0000_a"})
        p = migrate.plan(cur, d)
        self.assertEqual(p["pending"], ["0001_b", "0002_c"])
        self.assertEqual(p["applied"], ["0000_a"])

    def test_no_schema_migrations_table_means_everything_is_pending(self):
        d = _dir(["0000_a", "0001_b"])
        p = migrate.plan(FakeCursor(applied=set(), table_exists=False), d)
        self.assertEqual(p["pending"], ["0000_a", "0001_b"])

    def test_files_that_do_not_look_like_migrations_are_ignored(self):
        d = _dir(["0000_a"])
        (d / "notes.sql").write_text("SELECT 1;")
        (d / "README.md").write_text("x")
        self.assertEqual([v for v, _ in migrate.available(d)], ["0000_a"])

    def test_versions_applied_but_absent_from_the_repo_are_reported(self):
        d = _dir(["0000_a"])
        p = migrate.plan(FakeCursor(applied={"0000_a", "0009_ghost"}), d)
        self.assertEqual(p["unknown_applied"], ["0009_ghost"])


class RunTest(unittest.TestCase):
    def test_dry_run_executes_no_migration(self):
        d = _dir(["0000_a", "0001_b"])
        cur = FakeCursor(applied={"0000_a"})
        conn = FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn):
            out = migrate.run(dry_run=True, directory=d)
        self.assertEqual(out["pending"], ["0001_b"])
        self.assertEqual(out["ran"], [])
        self.assertEqual(conn.commits, 0)
        self.assertFalse(any("SELECT 1" in s for s, _ in cur.executed))

    def test_a_real_run_applies_pending_in_order_and_commits_each(self):
        d = _dir(["0000_a", "0001_b", "0002_c"])
        cur = FakeCursor(applied={"0000_a"})
        conn = FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn):
            out = migrate.run(directory=d)
        self.assertEqual(out["ran"], ["0001_b", "0002_c"])
        self.assertEqual(out["pending"], [])
        self.assertEqual(conn.commits, 2)
        bodies = [s for s, _ in cur.executed if "SELECT 1" in s]
        self.assertEqual(len(bodies), 2)
        self.assertLess(bodies[0].index("0001_b"), 1 + bodies[1].index("0002_c"))

    def test_a_fully_applied_database_is_a_no_op(self):
        d = _dir(["0000_a"])
        cur = FakeCursor(applied={"0000_a"})
        conn = FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn):
            out = migrate.run(directory=d)
        self.assertEqual(out["ran"], [])
        self.assertEqual(conn.commits, 0)

    def test_the_runner_records_a_migration_that_forgot_to_record_itself(self):
        d = _dir(["0000_a"])
        (d / "0000_a.sql").write_text("SELECT 1;\n")  # no self-record line
        cur = FakeCursor(applied=set())
        conn = FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn):
            migrate.run(directory=d)
        self.assertIn("0000_a", cur.applied)


class RepoMigrationsTest(unittest.TestCase):
    def test_the_repo_migrations_are_discoverable_and_self_recording(self):
        files = migrate.available()
        self.assertGreaterEqual(len(files), 5)
        for version, path in files:
            self.assertIn("INSERT INTO schema_migrations", path.read_text(), version)


class LiteralTrgmMigrationTest(unittest.TestCase):
    """R8: 0015_literal_trgm.sql. No live Postgres fixture exists in this
    suite (every other migrate test above fakes the cursor too), so this
    checks the SQL text itself for the no-op-on-permission-failure guard —
    the one thing that must never be wrong, since a hub where the role
    cannot CREATE EXTENSION must still apply cleanly and record itself."""

    def _sql(self) -> str:
        for version, path in migrate.available():
            if version == "0015_literal_trgm":
                return path.read_text(encoding="utf-8")
        self.fail("0015_literal_trgm.sql not found under ops/migrations")

    def test_it_self_records(self):
        sql = self._sql()
        self.assertIn("INSERT INTO schema_migrations", sql)
        self.assertIn("0015_literal_trgm", sql)

    def test_extension_creation_is_guarded_against_insufficient_privilege(self):
        """The exact stop condition from the phase-1 brief: a permission
        gap must degrade to a no-op, not fail the whole migration run (and
        not block every migration queued behind it)."""
        sql = self._sql()
        self.assertIn("CREATE EXTENSION IF NOT EXISTS pg_trgm", sql)
        self.assertIn("EXCEPTION WHEN insufficient_privilege", sql)

    def test_index_creation_is_conditional_on_the_extension_actually_existing(self):
        """CREATE INDEX ... USING gin (col gin_trgm_ops) itself raises if
        pg_trgm never got created (the opclass would not exist) — the
        insufficient_privilege catch above is not enough on its own."""
        sql = self._sql()
        self.assertIn("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'", sql)
        self.assertIn("gin_trgm_ops", sql)

    def test_it_targets_the_columns_the_ilike_pass_actually_scans(self):
        """khipu.cli._search_query / _literal_candidates ILIKE episodes.summary,
        topics.title (via COALESCE, since it is nullable) and topics.body,
        and nodes.name (via COALESCE) — see _EPISODE_ILIKE_COLUMNS and the
        topic/node WHERE clauses."""
        sql = self._sql()
        for target in (
            "episodes USING gin (summary",
            "topics USING gin ((COALESCE(title, '')",
            "topics USING gin (body",
            "nodes USING gin ((COALESCE(name, '')",
        ):
            with self.subTest(target=target):
                self.assertIn(target, sql)


class LibraryMigrationTest(unittest.TestCase):
    """0026_library.sql (library sources + BYO embeddings, Session A). SQL-text
    checks like the neighbours; the real-Postgres check is
    packages/cli/scripts/scratch_pg.sh (pgvector-free: vector columns become
    real[] there, so only statements and constraints are exercised)."""

    def _sql(self) -> str:
        for version, path in migrate.available():
            if version == "0026_library":
                return path.read_text(encoding="utf-8")
        self.fail("0026_library.sql not found under ops/migrations")

    def test_it_is_listed_and_self_records(self):
        self.assertIn("0026_library", [v for v, _ in migrate.available()])
        sql = self._sql()
        self.assertIn("INSERT INTO schema_migrations", sql)
        self.assertIn("'0026_library'", sql)

    def test_every_statement_is_idempotent(self):
        sql = self._sql()
        self.assertIn("ADD COLUMN IF NOT EXISTS endpoint TEXT", sql)
        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.startswith("CREATE TABLE"):
                self.assertIn("IF NOT EXISTS", stripped)
            if stripped.startswith("CREATE INDEX"):
                self.assertIn("IF NOT EXISTS", stripped)

    def test_the_four_library_tables_exist(self):
        sql = self._sql()
        for table in ("library_sources", "library_documents", "library_chunks", "library_embeddings"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} (", sql)

    def _executable(self) -> str:
        return "\n".join(
            line for line in self._sql().splitlines() if not line.lstrip().startswith("--")
        )

    def test_the_embedding_column_is_an_untyped_vector(self):
        import re

        sql = self._executable()
        self.assertRegex(sql, r"embedding\s+vector\s+NOT NULL")
        self.assertIsNone(re.search(r"embedding\s+vector\(", sql))

    def test_memory_vectors_become_untyped_and_the_gemini_indexes_are_recreated(self):
        sql = self._executable()
        self.assertIn("ALTER TABLE memory_embeddings ALTER COLUMN embedding TYPE vector;", sql)
        self.assertIn("ALTER TABLE memory_query_cache ALTER COLUMN embedding TYPE vector;", sql)
        # dropped before the ALTER, recreated after it, same names as 0004/0005
        for name, pid in (
            ("idx_memory_embeddings_hnsw_gemini768", "gemini-embedding-001@768"),
            ("idx_memory_embeddings_hnsw_gemini2_768", "gemini-embedding-2@768"),
        ):
            with self.subTest(index=name):
                self.assertIn(f"DROP INDEX IF EXISTS {name};", sql)
                create = sql.index(f"CREATE INDEX IF NOT EXISTS {name}")
                self.assertGreater(create, sql.index("ALTER TABLE memory_embeddings"))
                self.assertLess(sql.index(f"DROP INDEX IF EXISTS {name};"),
                                sql.index("ALTER TABLE memory_embeddings"))
                tail = " ".join(sql[create:create + 260].split())
                self.assertIn("USING hnsw ((embedding::vector(768)) vector_cosine_ops)", tail)
                self.assertIn(f"WHERE profile = '{pid}'", tail)

    def test_the_memory_alter_is_guarded_so_a_rerun_is_a_no_op(self):
        sql = self._executable()
        self.assertEqual(sql.count("atttypmod"), 2)
        self.assertIn("to_regclass('public.memory_embeddings') IS NOT NULL", sql)

    def test_no_library_index_is_built_by_the_migration(self):
        sql = self._executable().lower()
        self.assertNotIn("on library_embeddings using hnsw", sql.replace("\n", " "))
        # the doc index and the chunk full-text (GIN tsvector) index; never a vector index
        self.assertEqual(sql.count("create index if not exists idx_library"), 2)
        self.assertIn("create index if not exists idx_library_chunks_tsv", sql)

    def test_documents_are_unique_per_source_and_path_and_chunks_cascade(self):
        sql = self._sql()
        self.assertIn("UNIQUE (source, rel_path)", sql)
        self.assertIn("PRIMARY KEY (document, chunk_idx)", sql)
        self.assertIn("PRIMARY KEY (profile, document, chunk_idx)", sql)
        self.assertGreaterEqual(sql.count("ON DELETE CASCADE"), 3)


class DecisionEvidenceMigrationTest(unittest.TestCase):
    """0024_decision_evidence.sql (Phase 2, session A). No live Postgres
    fixture here (same posture as every other migrate test in this file) —
    parses the SQL text and asserts the additive/idempotent shape every
    reader/writer in khipu.decisions relies on."""

    def _sql(self) -> str:
        for version, path in migrate.available():
            if version == "0024_decision_evidence":
                return path.read_text(encoding="utf-8")
        self.fail("0024_decision_evidence.sql not found under ops/migrations")

    def test_it_self_records(self):
        sql = self._sql()
        self.assertIn("INSERT INTO schema_migrations", sql)
        self.assertIn("0024_decision_evidence", sql)

    def test_every_add_column_is_nullable_or_defaulted(self):
        sql = self._sql()
        add_column_lines = [
            line.strip() for line in sql.splitlines()
            if "ADD COLUMN" in line
        ]
        self.assertGreaterEqual(len(add_column_lines), 7)
        for line in add_column_lines:
            with self.subTest(line=line):
                self.assertIn("IF NOT EXISTS", line)
                self.assertNotIn("NOT NULL", line)  # nullable (no DEFAULT needed either)

    def test_every_statement_is_idempotent(self):
        sql = self._sql()
        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.startswith("ALTER TABLE") and "ADD COLUMN" in stripped:
                self.assertIn("IF NOT EXISTS", stripped)
            elif stripped.startswith("CREATE TABLE"):
                self.assertIn("IF NOT EXISTS", stripped)
            elif stripped.startswith("CREATE INDEX"):
                self.assertIn("IF NOT EXISTS", stripped)
        self.assertIn("ON CONFLICT (version) DO NOTHING", sql)

    def test_no_unique_index_on_decisions_itself(self):
        """Production holds duplicate decision text (B2); a unique index on
        `decisions` would fail to apply there."""
        sql = self._sql()
        self.assertNotIn("UNIQUE", sql.split("CREATE TABLE IF NOT EXISTS decision_links")[0])

    def test_decision_links_has_the_documented_shape(self):
        sql = self._sql()
        for target in (
            "old_id", "new_id", "kind", "confidence", "source", "reason",
            "state", "created_at", "resolved_at",
            "UNIQUE (old_id, new_id, kind)",
            "ON DELETE CASCADE",
        ):
            with self.subTest(target=target):
                self.assertIn(target, sql)
