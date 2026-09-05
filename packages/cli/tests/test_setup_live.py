# --bypass-harness (sonnet lane): authored by a dispatched on-sub
# Agent(model="sonnet") subagent for the "gaps become oracles" layer 2 of
# docs/plans/2026-09-05-setup-that-cannot-strand-you.md — the "stranger's
# first run" no reviewer has to remember to do by hand. Requires Docker; see
# test_setup_contract.py for the pure-unit layer 1.
"""First-run end to end against a real, scratch Postgres 19 + pgvector
cluster: connect an EMPTY database, seed a couple of rows through the real
capture writer, move the whole database to a second scratch database (dry
run, then for real), and verify identical counts on both ends.

One Docker container, two databases inside it (``khipu_a``, ``khipu_b``) —
not two containers; the drill-cluster helpers in ``components_backup`` only
know how to run one at a time, and one container is all this needs since a
"move" only cares about two different *databases*, not two different hosts.

Skips itself, loudly, naming exactly what is missing, when Docker is not
available or the khipu Postgres image cannot be pulled or built. Every DSN
this file touches is asserted to point at 127.0.0.1 on the scratch port
before any test body runs — this must never be able to reach the
maintainer's real hub.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest import mock
from urllib.parse import quote

from khipu import capture, dbmove, migrate, setup
from khipu.components_backup import (
    _cleanup_drill,
    _start_drill_cluster,
    _wait_drill_ready,
)
from khipu.components_postgres import (
    _generate_password,
    _local_dsn,
    docker_available,
    ensure_postgres_image,
)

DRILL_READY_TIMEOUT_S = 120.0
SCRATCH_PORT = 54391
ADMIN_DB = "khipu"
DB_A = "khipu_a"
DB_B = "khipu_b"


def _log(msg: str) -> None:
    print(f"[test_setup_live] {msg}", file=sys.stderr, flush=True)


def _bundled_postgres_image() -> str | None:
    """The exact image the local install builds (``ensure_postgres_image`` /
    ``build_postgres_image`` in ``components_postgres.py`` take this same
    string from the compat matrix) — read directly from the bundled JSON
    rather than through ``select_compat_row``, which additionally filters on
    ``khipu_app_min`` against ``khipu_app_version()`` and would find nothing
    in a bare test environment with no ``KHIPU_APP_VERSION`` set.
    """
    from khipu.components_matrix import bundled_matrix_path

    try:
        data = json.loads(bundled_matrix_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for row in data.get("matrix", []):
        image = str(row.get("postgres_image") or "").strip()
        if image:
            return image
    return None


def _skip_reason() -> str | None:
    """None when this suite can run; otherwise exactly what is missing, so
    the skip message never leaves a reader guessing."""
    docker = docker_available()
    if not docker.get("ok"):
        return f"Docker is not available: {docker.get('error')}"
    image = _bundled_postgres_image()
    if not image:
        return "docs/compat/khipu-graphify-postgres.json has no postgres_image row"
    return None


_SKIP_REASON = _skip_reason()


def _dsn_for(port: int, password: str, dbname: str) -> str:
    user = quote("khipu", safe="")
    pw = quote(password, safe="")
    return f"postgresql://{user}:{pw}@127.0.0.1:{port}/{dbname}?sslmode=disable"


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def _connect_when_stable(dsn: str, *, timeout_s: float = 60.0):
    """Connect over TCP once the server stays up: the postgres entrypoint
    restarts once after initdb, so the first successful connect can land on
    the temporary server. Two connects a second apart, then keep the second."""
    import time

    import psycopg

    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            first = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
            first.close()
            time.sleep(1)
            return psycopg.connect(dsn, autocommit=True, connect_timeout=5)
        except Exception as exc:  # noqa: BLE001 — retry until the deadline
            last = exc
            time.sleep(1)
    raise RuntimeError(f"scratch cluster never became stable: {last}")


def _table_names(dsn: str) -> set[str]:
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            return {r[0] for r in cur.fetchall()}


def _count(dsn: str, table: str) -> int:
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608 — fixed table names only
            return int(cur.fetchone()[0])


def _schema_migrations_versions(dsn: str) -> set[str]:
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations")
            return {r[0] for r in cur.fetchall()}


@unittest.skipUnless(_SKIP_REASON is None, _SKIP_REASON or "")
class SetupLiveTest(unittest.TestCase):
    """One test method, one linear story — connect empty, seed, move dry,
    move real, verify both ends — because every step depends on the state
    the step before it left behind; splitting this into independent test_
    methods would only buy fragile inter-test ordering, not isolation."""

    @classmethod
    def setUpClass(cls) -> None:
        image = _bundled_postgres_image()
        assert image, "guarded by the class-level skipUnless above"
        cls.image = image

        t0 = time.monotonic()
        image_result = ensure_postgres_image(image)
        cls.image_prep_seconds = round(time.monotonic() - t0, 1)
        _log(f"image {image!r} ready in {cls.image_prep_seconds}s "
             f"(source={image_result.get('source')})")
        if not image_result.get("ok"):
            raise unittest.SkipTest(
                f"khipu postgres image {image!r} could not be pulled or built "
                f"within the timeout: {image_result}"
            )

        password = _generate_password()
        t0 = time.monotonic()
        started = _start_drill_cluster(image, SCRATCH_PORT, password)
        if not started.get("ok"):
            raise unittest.SkipTest(f"scratch cluster failed to start: {started}")
        # Registered the moment the container exists, so any failure between
        # here and the end of the class still tears it down.
        cls.addClassCleanup(_cleanup_drill)
        if not _wait_drill_ready(SCRATCH_PORT, password, timeout_s=DRILL_READY_TIMEOUT_S):
            raise unittest.SkipTest("scratch cluster never became ready")
        cls.cluster_start_seconds = round(time.monotonic() - t0, 1)
        _log(f"scratch cluster ready in {cls.cluster_start_seconds}s on port {SCRATCH_PORT}")

        admin_dsn = _local_dsn(SCRATCH_PORT, password)
        with _connect_when_stable(admin_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{DB_A}"')
                cur.execute(f'CREATE DATABASE "{DB_B}"')

        cls.dsn_a = _dsn_for(SCRATCH_PORT, password, DB_A)
        cls.dsn_b = _dsn_for(SCRATCH_PORT, password, DB_B)
        for dsn in (cls.dsn_a, cls.dsn_b):
            assert "127.0.0.1" in dsn and f":{SCRATCH_PORT}/" in dsn, (
                f"refusing to run against a non-scratch DSN: {dsn!r}"
            )

    def test_first_run_connect_seed_move_and_verify(self) -> None:
        dsn_a, dsn_b = self.dsn_a, self.dsn_b

        # No embedding model key is used anywhere in this test — vectors are
        # not required to prove the connect/move machinery, and this must
        # never place a real network call against a real provider.
        no_model_key = mock.patch.multiple(
            "khipu.keychain", get_gemini_key=lambda: None, get_openai_compat_key=lambda: None,
        )
        # move_database's post-copy "switch" runs the full connect pipeline
        # against the target with install_jobs=True — never let a test touch
        # this Mac's real LaunchAgents / launchctl state.
        no_real_launchd = mock.patch(
            "khipu.launchd_gen.ensure_scheduled_jobs",
            return_value={"ok": True, "installed": [], "refreshed": [], "current": [], "external": []},
        )

        # --- 1. connect_database against the EMPTY database, store off ---
        t0 = time.monotonic()
        connect_out = setup.connect_database(dsn_a, store=False, install_jobs=False, prove=False)
        connect_seconds = round(time.monotonic() - t0, 2)
        _log(f"connect_database(empty dsn_a) in {connect_seconds}s")
        self.assertTrue(connect_out["ok"], connect_out)
        for stage in connect_out["stages"]:
            if stage.get("status") == "skipped":
                continue
            self.assertTrue(stage.get("ok"), stage)

        available_versions = {v for v, _ in migrate.available()}
        self.assertTrue(available_versions, "expected at least one migration file")
        applied_versions = _schema_migrations_versions(dsn_a)
        self.assertEqual(applied_versions, available_versions)

        graph_stage = next(s for s in connect_out["stages"] if s["id"] == "graph")
        self.assertTrue(graph_stage.get("ok"), graph_stage)
        self.assertEqual(connect_out["summary"].get("episodes"), 0)

        # --- 2. seed a couple of rows through the real writer ---
        with mock.patch("khipu.db.resolve_dsn", return_value=dsn_a), \
             mock.patch.dict(os.environ, {"KHIPU_HUB_FILE_MIRROR": "0"}):
            r1 = capture.write_pg(
                {
                    "session_id": "test:setup-live",
                    "ts": "2026-01-01T00:00:00Z",
                    "summary": "Khipu setup-live seed episode one — safe to delete.",
                    "topics": [],
                }
            )
            r2 = capture.write_pg(
                {
                    "session_id": "test:setup-live",
                    "ts": "2026-01-02T00:00:00Z",
                    "summary": "Khipu setup-live seed episode two, with an open loop attached.",
                    "topics": [],
                    "open_loops": ["follow up with Matt on pricing"],
                }
            )
        self.assertTrue(r1["episode_inserted"], r1)
        self.assertTrue(r2["episode_inserted"], r2)

        seeded_episodes = _count(dsn_a, "episodes")
        seeded_commitments = _count(dsn_a, "commitments")
        self.assertGreaterEqual(seeded_episodes, 2)
        self.assertGreaterEqual(seeded_commitments, 1)

        # --- 3. dry-run move: preflight ok, counts reported, nothing switched ---
        recorder: list[str] = []
        with mock.patch("khipu.db.resolve_dsn", return_value=dsn_a), \
             mock.patch("khipu.keychain.set_dsn", side_effect=recorder.append):
            dry = dbmove.move_database(dsn_b, dry_run=True)
        self.assertTrue(dry["ok"], dry)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(dry["switched"])
        self.assertTrue(dry["tables"])
        self.assertEqual(recorder, [])
        self.assertEqual(_count(dsn_b, "episodes"), 0)

        # --- 4. the real move ---
        recorder = []
        t0 = time.monotonic()
        with mock.patch("khipu.db.resolve_dsn", return_value=dsn_a), \
             mock.patch("khipu.keychain.set_dsn", side_effect=recorder.append), \
             no_model_key, no_real_launchd:
            moved = dbmove.move_database(dsn_b)
        move_seconds = round(time.monotonic() - t0, 2)
        _log(f"move_database(dsn_a -> dsn_b) in {move_seconds}s")
        self.assertTrue(moved["ok"], moved)
        self.assertTrue(moved["switched"], moved)
        self.assertEqual(recorder, [dsn_b])

        table_names = [t["name"] for t in moved["tables"]]
        existing = _table_names(dsn_b)
        expected_order = dbmove._copy_order(existing, existing)
        self.assertEqual(table_names, expected_order)
        for row in moved["tables"]:
            self.assertEqual(row["source_rows"], row["target_rows"], row)

        # --- 5. verify: dsn_b has the same session count as dsn_a did ---
        verify = setup.connect_database(dsn_b, store=False, install_jobs=False, prove=False)
        self.assertTrue(verify["ok"], verify)
        self.assertEqual(verify["summary"].get("episodes"), seeded_episodes)

        # --- 6. dsn_a is untouched — the source is never written to ---
        self.assertEqual(_count(dsn_a, "episodes"), seeded_episodes)
        self.assertEqual(_count(dsn_a, "commitments"), seeded_commitments)


if __name__ == "__main__":
    unittest.main()
