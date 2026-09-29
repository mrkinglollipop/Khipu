# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""The "You produced ..." line comes from the local replica, not the hub.

The per-prompt hook used to open a hub connection on every prompt to answer
this; on a busy machine that missed its limit and the line was dropped. The
replica now carries the ``deliverables`` table, and the hook only falls back to
the hub for a replica that cannot answer.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from khipu import deliverables as dl
from khipu import features
from khipu import hub_snapshot as hs
from khipu import recall_prompt as rp

_COLS = ("id", "project", "kind", "path", "url", "title", "episode_id", "created_at")
_HUB_DELIVERABLE_COLS = list(_COLS)
_EPISODE_COLS_WITH_DELETED = ["id", "summary", "project", "deleted_at"]


class _FakeHubCursor:
    """Just enough of a hub cursor for the deliverables reader and the export.

    Applies the same project filter, forgotten-episode exclusion, ordering and
    limit the real SQL does, so it stands in for the hub table."""

    def __init__(
        self,
        rows: list[dict],
        *,
        forgotten: set[int] = frozenset(),
        with_table: bool = True,
        with_deleted_at: bool = True,
    ) -> None:
        self.rows = rows
        self.forgotten = set(forgotten)
        self.with_table = with_table
        self.with_deleted_at = with_deleted_at
        self.sql: list[str] = []
        self._result: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s)
        params = params or ()
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            table = params[0]
            if table == "deliverables":
                cols = _HUB_DELIVERABLE_COLS if self.with_table else []
            elif table == "episodes":
                cols = _EPISODE_COLS_WITH_DELETED if self.with_deleted_at else ["id", "summary"]
            else:
                cols = []
            self._result = [(c,) for c in cols]
            return
        if " FROM deliverables" in s:
            select = s.split(" FROM deliverables")[0][len("SELECT "):]
            cols = [c.strip() for c in select.split(",")]
            out = list(self.rows)
            if "WHERE project = %s" in s:
                out = [r for r in out if r["project"] == params[0]]
            if "e.deleted_at IS NOT NULL" in s:
                out = [r for r in out if r["episode_id"] not in self.forgotten]
            if "ORDER BY created_at DESC" in s:
                out.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
                out = out[: params[-1]]
            else:
                out.sort(key=lambda r: r["id"])
            self._result = [tuple(r[c] for c in cols) for r in out]
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


def _row(id_, path, *, project="acme/widget", episode=1, day=1, kind="file", url=None, title=None):
    return {
        "id": id_,
        "project": project,
        "kind": kind,
        "path": path,
        "url": url,
        "title": title,
        "episode_id": episode,
        "created_at": datetime(2026, 9, day, 10, 0, tzinfo=timezone.utc),
    }


def _replica_with(rows: list[dict], *, forgotten: set[int] = frozenset(), version: int = 3):
    """An in-memory replica holding ``rows`` (and tombstoned episodes)."""
    con = sqlite3.connect(":memory:")
    hs._create_schema(con, version=version)
    episode_ids = {r["episode_id"] for r in rows if r["episode_id"] is not None}
    for eid in episode_ids:
        con.execute(
            "INSERT INTO episodes (id, summary, deleted_at) VALUES (?, 'ep', ?)",
            (eid, "2026-09-20T00:00:00+00:00" if eid in forgotten else None),
        )
    if version >= 3:
        for r in rows:
            con.execute(
                "INSERT INTO deliverables (id, project, kind, path, url, title, episode_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r["project"], r["kind"], r["path"], r["url"], r["title"],
                 r["episode_id"], hs._ts_text(r["created_at"])),
            )
    con.commit()
    return con


def _replica_file(rows: list[dict], *, version: int = 3) -> Path:
    """A replica on disk with a fresh meta file; returns its data dir."""
    data = Path(tempfile.mkdtemp(prefix="khipu-replica-deliv-"))
    con = _replica_with(rows, version=version)
    disk = sqlite3.connect(str(data / hs.SNAPSHOT_NAME))
    con.backup(disk)
    disk.close()
    con.close()
    (data / hs.META_NAME).write_text(
        json.dumps({"refreshed_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8"
    )
    return data


_CASES = {
    "newest_of_two_equal_matches": (
        [_row(1, "khipu/decisions.py", episode=1, day=1),
         _row(2, "khipu/decisions_v2.py", episode=2, day=9)],
        ["decisions", "khipu"],
        {},
    ),
    "more_hits_beats_newer": (
        [_row(1, "khipu/decisions.py", title="decisions registry", episode=1, day=1),
         _row(2, "khipu/decisions.py", episode=2, day=9)],
        ["decisions", "registry", "khipu"],
        {},
    ),
    "one_token_is_no_match": (
        [_row(1, "khipu/decisions.py")],
        ["decisions", "unrelated"],
        {},
    ),
    "other_project_is_ignored": (
        [_row(1, "khipu/decisions.py", project="other/repo")],
        ["decisions", "khipu"],
        {},
    ),
    "forgotten_episode_is_excluded": (
        [_row(1, "khipu/decisions.py", episode=7, day=9),
         _row(2, "khipu/decisions_old.py", episode=8, day=1)],
        ["decisions", "khipu"],
        {"forgotten": {7}},
    ),
    "url_only_row_has_no_text_to_match": (
        [_row(1, None, kind="pr", url="https://example.invalid/pull/1")],
        ["pull", "invalid"],
        {},
    ),
    "title_only_row_renders_the_title": (
        [_row(1, None, kind="release", title="v0.9.0 release notes", episode=3)],
        ["release", "notes"],
        {},
    ),
    "same_timestamp_breaks_on_id": (
        [_row(1, "khipu/decisions_a.py", episode=1, day=5),
         _row(2, "khipu/decisions_b.py", episode=2, day=5)],
        ["decisions", "khipu"],
        {},
    ),
}


class ReplicaReaderMatchesHubReaderTest(unittest.TestCase):
    def test_same_line_for_the_same_rows(self) -> None:
        for name, (rows, tokens, opts) in _CASES.items():
            with self.subTest(case=name):
                forgotten = opts.get("forgotten", set())
                hub_line = dl.deliverable_line_for_prompt(
                    _FakeHubCursor(rows, forgotten=forgotten), tokens, project="acme/widget"
                )
                con = _replica_with(rows, forgotten=forgotten)
                try:
                    replica_rows = hs.recent_deliverables_snapshot(con, project="acme/widget")
                finally:
                    con.close()
                assert replica_rows is not None
                self.assertEqual(dl.line_from_rows(replica_rows, tokens), hub_line)

    def test_the_cases_are_not_all_empty(self) -> None:
        """Guard against the comparison passing because every case is None."""
        lines = []
        for rows, tokens, opts in _CASES.values():
            lines.append(dl.deliverable_line_for_prompt(
                _FakeHubCursor(rows, forgotten=opts.get("forgotten", set())),
                tokens, project="acme/widget"))
        self.assertGreaterEqual(sum(1 for line in lines if line), 4)
        self.assertGreaterEqual(sum(1 for line in lines if not line), 2)

    def test_rows_come_back_newest_first_and_bounded(self) -> None:
        rows = [_row(i, f"khipu/f{i}.py", day=i) for i in range(1, 6)]
        con = _replica_with(rows)
        try:
            got = hs.recent_deliverables_snapshot(con, project="acme/widget", limit=3)
        finally:
            con.close()
        assert got is not None
        self.assertEqual([r["id"] for r in got], [5, 4, 3])

    def test_a_replica_without_the_table_cannot_answer(self) -> None:
        con = _replica_with([_row(1, "khipu/decisions.py")], version=2)
        try:
            self.assertIsNone(hs.recent_deliverables_snapshot(con, project="acme/widget"))
        finally:
            con.close()

    def test_a_replica_with_the_table_but_no_match_is_an_empty_answer(self) -> None:
        con = _replica_with([])
        try:
            self.assertEqual(hs.recent_deliverables_snapshot(con, project="acme/widget"), [])
        finally:
            con.close()


class ExportTest(unittest.TestCase):
    def _export(self, cur: _FakeHubCursor) -> tuple[int, sqlite3.Connection]:
        con = sqlite3.connect(":memory:")
        hs._create_schema(con)
        return hs._insert_deliverables(cur, con), con

    def test_rows_are_exported(self) -> None:
        rows = [_row(1, "khipu/decisions.py", episode=1), _row(2, "khipu/other.py", episode=2, day=3)]
        n, con = self._export(_FakeHubCursor(rows))
        try:
            self.assertEqual(n, 2)
            got = con.execute(
                "SELECT id, project, kind, path, episode_id, created_at FROM deliverables ORDER BY id"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(got[0][:5], (1, "acme/widget", "file", "khipu/decisions.py", 1))
        self.assertEqual(got[1][5], "2026-09-03T10:00:00+00:00")

    def test_a_forgotten_episodes_deliverables_are_not_exported(self) -> None:
        rows = [_row(1, "khipu/decisions.py", episode=7), _row(2, "khipu/other.py", episode=8)]
        n, con = self._export(_FakeHubCursor(rows, forgotten={7}))
        try:
            self.assertEqual(n, 1)
            self.assertEqual(
                [r[0] for r in con.execute("SELECT id FROM deliverables").fetchall()], [2]
            )
        finally:
            con.close()

    def test_a_hub_without_the_table_exports_an_empty_table(self) -> None:
        cur = _FakeHubCursor([_row(1, "khipu/decisions.py")], with_table=False)
        n, con = self._export(cur)
        try:
            self.assertEqual(n, 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM deliverables").fetchone()[0], 0)
        finally:
            con.close()
        self.assertFalse(any("FROM deliverables" in s for s in cur.sql))

    def test_a_hub_without_deleted_at_still_exports(self) -> None:
        rows = [_row(1, "khipu/decisions.py", episode=7)]
        n, con = self._export(_FakeHubCursor(rows, forgotten={7}, with_deleted_at=False))
        con.close()
        self.assertEqual(n, 1)

    def test_refresh_reports_the_count_and_writes_version_3(self) -> None:
        cur = _FakeHubCursor([_row(1, "khipu/decisions.py")])

        @contextmanager
        def _fake_hub(**_kw):
            pg = mock.MagicMock()
            pg.cursor.return_value.__enter__.return_value = cur
            yield pg

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            snap = data / "hub_snapshot.sqlite"
            patches = [
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
                mock.patch.object(hs, "try_hub_connect", _fake_hub),
            ] + [
                mock.patch.object(hs, name, return_value=0)
                for name in (
                    "_insert_episodes", "_insert_topics", "_insert_topic_revisions",
                    "_insert_nodes", "_insert_edges", "_insert_profiles",
                    "_insert_memory_embeddings", "_insert_decisions",
                )
            ]
            for p in patches:
                p.start()
            try:
                out = hs.refresh()
            finally:
                for p in patches:
                    p.stop()
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["schema_version"], 3)
            self.assertEqual(out["counts"]["deliverables"], 1)
            con = sqlite3.connect(str(snap))
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM deliverables").fetchone()[0], 1)
                self.assertEqual(
                    con.execute(
                        "SELECT value FROM snapshot_meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "3",
                )
            finally:
                con.close()


class ForgetInReplicaTest(unittest.TestCase):
    def _forget(self, version: int) -> sqlite3.Connection:
        rows = [_row(1, "khipu/decisions.py", episode=7), _row(2, "khipu/other.py", episode=8)]
        data = _replica_file(rows, version=version)
        snap = data / hs.SNAPSHOT_NAME
        with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                mock.patch.object(hs, "meta_path", return_value=data / hs.META_NAME):
            self.assertTrue(hs.forget_episode_in_snapshot(7)["ok"])
        return sqlite3.connect(str(snap))

    def test_forgetting_an_episode_drops_its_deliverables_from_the_replica(self) -> None:
        con = self._forget(3)
        try:
            self.assertEqual(
                [r[0] for r in con.execute("SELECT id FROM deliverables").fetchall()], [2]
            )
        finally:
            con.close()

    def test_forgetting_on_a_replica_without_the_table_still_works(self) -> None:
        con = self._forget(2)
        try:
            self.assertIsNotNone(
                con.execute("SELECT deleted_at FROM episodes WHERE id = 7").fetchone()[0]
            )
        finally:
            con.close()


class HookUsesTheReplicaTest(unittest.TestCase):
    def _line(self, data: Path, tokens: list[str]) -> str:
        with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data)}), \
                mock.patch("khipu.identity.resolve_repo_root", return_value={"project": "acme/widget"}), \
                mock.patch("khipu.db.connect", side_effect=AssertionError("hub touched")) as hub:
            out = rp._deliverable_context_line(tokens, cwd="/repo")
        hub.assert_not_called()
        return out

    def _via_hub(self, data: Path, hub_line: str | None) -> tuple[str, mock.MagicMock]:
        with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data)}), \
                mock.patch("khipu.identity.resolve_repo_root", return_value={"project": "acme/widget"}), \
                mock.patch("khipu.db.connect", return_value=mock.MagicMock()) as hub, \
                mock.patch("khipu.deliverables.deliverable_line_for_prompt", return_value=hub_line), \
                mock.patch.object(rp, "_log"):
            return rp._deliverable_context_line(["decisions", "khipu"], cwd="/repo"), hub

    def test_a_current_replica_answers_without_the_hub(self) -> None:
        data = _replica_file([_row(1, "khipu/decisions.py", episode=42, day=14)])
        self.assertEqual(
            self._line(data, ["decisions", "khipu"]),
            "You produced khipu/decisions.py on 2026-09-14 (episode 42)",
        )

    def test_a_current_replica_with_no_match_still_skips_the_hub(self) -> None:
        data = _replica_file([_row(1, "khipu/decisions.py")])
        self.assertEqual(self._line(data, ["unrelated", "words"]), "")

    def test_a_v2_replica_falls_back_to_the_hub(self) -> None:
        data = _replica_file([_row(1, "khipu/decisions.py")], version=2)
        out, hub = self._via_hub(data, "You produced hub.py on 2026-09-01 (episode 5)")
        hub.assert_called_once()
        self.assertEqual(out, "You produced hub.py on 2026-09-01 (episode 5)")

    def test_a_stale_replica_falls_back_to_the_hub(self) -> None:
        data = _replica_file([_row(1, "khipu/decisions.py")])
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        (data / hs.META_NAME).write_text(json.dumps({"refreshed_at": old}), encoding="utf-8")
        out, hub = self._via_hub(data, None)
        hub.assert_called_once()
        self.assertEqual(out, "")

    def test_an_unreadable_replica_falls_back_to_the_hub(self) -> None:
        data = _replica_file([_row(1, "khipu/decisions.py")])
        (data / hs.SNAPSHOT_NAME).write_bytes(b"this is not a sqlite file" * 40)
        out, hub = self._via_hub(data, None)
        hub.assert_called_once()
        self.assertEqual(out, "")

    def test_the_line_still_reaches_prior_work_for_prompt(self) -> None:
        data = _replica_file([_row(1, "khipu/decisions.py", episode=42, day=14)])
        hits = {"hits": [], "legs": ["lexical"], "degraded": None}
        with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data)}), \
                mock.patch.object(rp, "_search_hits", return_value=hits), \
                mock.patch("khipu.identity.resolve_repo_root", return_value={"project": "acme/widget"}), \
                mock.patch("khipu.db.connect", side_effect=AssertionError("hub touched")):
            out = rp.prior_work_for_prompt(
                "what did we build in khipu for decisions", cwd="/repo"
            )
        self.assertIn("You produced khipu/decisions.py on 2026-09-14 (episode 42)", out["context"])


class NoDriverOnTheReplicaPathTest(unittest.TestCase):
    def test_the_replica_path_imports_neither_psycopg_nor_the_db_module(self) -> None:
        data = _replica_file([_row(1, "khipu/decisions.py", episode=42, day=14)])
        code = (
            "import json, sys\n"
            "from unittest import mock\n"
            "from khipu import recall_prompt as rp\n"
            "with mock.patch('khipu.identity.resolve_repo_root', return_value={'project': 'acme/widget'}):\n"
            "    line = rp._deliverable_context_line(['decisions', 'khipu'], cwd='/repo')\n"
            "print(json.dumps({'line': line, 'psycopg': 'psycopg' in sys.modules,\n"
            "                  'db': 'khipu.db' in sys.modules}))\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KHIPU_", "ALZY_"))}
        env.update(
            KHIPU_DATA_DIR=str(data),
            KHIPU_KEYCHAIN="0",
            HOME=str(data),
            PYTHONPATH=os.pathsep.join(p for p in sys.path if p),
            PYTHONPYCACHEPREFIX=str(data / "pyc"),
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(got["line"], "You produced khipu/decisions.py on 2026-09-14 (episode 42)")
        self.assertFalse(got["psycopg"], "the replica path imported psycopg")
        self.assertFalse(got["db"], "the replica path imported khipu.db")


class IncrementalWriteTest(unittest.TestCase):
    """``upsert_episode`` carries the episode's deliverables in the same step."""

    _EPISODE = {"id": 42, "ts": "2026-09-14T10:00:00+00:00", "summary": "built the registry",
                "project": "acme/widget"}

    def _upsert(self, data: Path, rows) -> dict:
        with mock.patch.object(hs, "snapshot_path", return_value=data / hs.SNAPSHOT_NAME), \
                mock.patch.object(hs, "meta_path", return_value=data / hs.META_NAME):
            return hs.upsert_episode(dict(self._EPISODE), [], deliverable_rows=rows)

    def _rows(self, data: Path) -> list[dict] | None:
        con = sqlite3.connect(str(data / hs.SNAPSHOT_NAME))
        try:
            return hs.recent_deliverables_snapshot(con, project="acme/widget")
        finally:
            con.close()

    def test_an_incremental_write_makes_the_line_findable(self) -> None:
        data = _replica_file([])
        out = self._upsert(data, [_row(5, "khipu/decisions.py", episode=42, day=14)])
        self.assertTrue(out["ok"], out)
        rows = self._rows(data)
        assert rows is not None
        self.assertEqual(
            dl.line_from_rows(rows, ["decisions", "khipu"]),
            "You produced khipu/decisions.py on 2026-09-14 (episode 42)",
        )

    def test_a_recapture_replaces_and_does_not_duplicate(self) -> None:
        data = _replica_file([])
        first = [_row(5, "khipu/decisions.py", episode=42), _row(6, "khipu/other.py", episode=42)]
        self._upsert(data, first)
        self._upsert(data, first)
        self.assertEqual(sorted(r["id"] for r in self._rows(data) or []), [5, 6])
        self._upsert(data, [_row(6, "khipu/other.py", episode=42)])
        self.assertEqual([r["id"] for r in self._rows(data) or []], [6])

    def test_other_episodes_rows_are_left_alone(self) -> None:
        data = _replica_file([_row(1, "khipu/keep.py", episode=7)])
        self._upsert(data, [_row(5, "khipu/decisions.py", episode=42)])
        self.assertEqual(sorted(r["id"] for r in self._rows(data) or []), [1, 5])

    def test_no_rows_argument_leaves_deliverables_untouched(self) -> None:
        data = _replica_file([_row(5, "khipu/decisions.py", episode=42)])
        self._upsert(data, None)
        self.assertEqual([r["id"] for r in self._rows(data) or []], [5])

    def test_a_v2_replica_is_left_untouched(self) -> None:
        data = _replica_file([], version=2)
        out = self._upsert(data, [_row(5, "khipu/decisions.py", episode=42)])
        self.assertTrue(out["ok"], out)
        con = sqlite3.connect(str(data / hs.SNAPSHOT_NAME))
        try:
            self.assertEqual(hs._snapshot_table_columns(con, "deliverables"), set())
            self.assertEqual(con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)
        finally:
            con.close()


class HubRowsForAnEpisodeTest(unittest.TestCase):
    def test_reads_the_episodes_rows_in_a_savepoint(self) -> None:
        rows = [_row(1, "khipu/a.py", episode=42), _row(2, "khipu/b.py", episode=9)]

        class _Cur(_FakeHubCursor):
            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if "SAVEPOINT" in s:
                    self.sql.append(s)
                    return
                if "WHERE episode_id = %s" in s:
                    self.sql.append(s)
                    self._result = [
                        tuple(r[c] for c in _COLS) for r in self.rows if r["episode_id"] == params[0]
                    ]
                    return
                super().execute(sql, params)

        cur = _Cur(rows)
        got = dl.deliverables_for_episode(cur, 42)
        assert got is not None
        self.assertEqual([r["id"] for r in got], [1])
        self.assertTrue(any(s.startswith("SAVEPOINT") for s in cur.sql))

    def test_no_table_is_none(self) -> None:
        self.assertIsNone(dl.deliverables_for_episode(_FakeHubCursor([], with_table=False), 42))


class CapabilityTest(unittest.TestCase):
    def test_the_replica_deliverables_capability_is_advertised(self) -> None:
        self.assertIn("replica.deliverables", features.capabilities())


if __name__ == "__main__":
    unittest.main()
