"""Unit tests for khipu.mirror + drift's directional gate — P2b (audit F1/F3).

Pure tests (topic parsing, episode-key extraction) need no database. Live tests
connect to the Khipu Postgres read-only — the anti-join and md5-parity checks
never write — and skip cleanly when it's unreachable.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import pathlib
import os
from unittest import mock
import unittest
from pathlib import Path

from khipu import mirror
from khipu.drift import file_episode_keys
from khipu.mirror import parse_topic_file


def _pg_available() -> bool:
    try:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception:
        return False
    return True


PG_AVAILABLE = _pg_available()


class ParseTopicFileTest(unittest.TestCase):
    """F3: one canonical topic shape for mirror AND reconcile. These pin the
    frontmatter handling both writers now share."""

    def _write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="topic-", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_frontmatter_parsed_and_stripped(self) -> None:
        text = '---\ntitle: "My Topic"\nstatus: archived\n---\n\nBody here.\n'
        path = self._write(text)
        parsed = parse_topic_file(path)
        assert parsed is not None
        self.assertEqual(parsed["title"], "My Topic")
        # W5.3: status is normalized to the canonical vocabulary at parse
        # time; the raw frontmatter value survives under status_raw.
        self.assertEqual(parsed["status"], "abandoned")
        self.assertEqual(parsed["frontmatter"]["status_raw"], "archived")
        self.assertEqual(parsed["body"], "Body here.\n")
        self.assertEqual(parsed["links"], [])
        self.assertEqual(parsed["frontmatter"]["title"], "My Topic")
        # Hash covers the FULL text (frontmatter included) so it stays
        # comparable with drift/conflict checks that hash the file as-is.
        self.assertEqual(
            parsed["digest"], hashlib.sha256(text.encode("utf-8")).hexdigest()
        )

    def test_yaml_ish_links_list_is_not_the_key_line(self) -> None:
        text = (
            "---\ntitle: Art\nstatus: in_progress\nlinks:\n"
            "  - sojourn-unseen-war-project\n---\n\nBody.\n"
        )
        parsed = parse_topic_file(self._write(text))
        assert parsed is not None
        self.assertEqual(parsed["links"], ["sojourn-unseen-war-project"])
        self.assertEqual(parsed["frontmatter"]["links"], ["sojourn-unseen-war-project"])

    def test_no_frontmatter_defaults(self) -> None:
        path = self._write("Just a body.\n")
        parsed = parse_topic_file(path)
        assert parsed is not None
        self.assertEqual(parsed["title"], path.stem)
        self.assertEqual(parsed["status"], "active")
        self.assertEqual(parsed["body"], "Just a body.\n")

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(parse_topic_file(Path("/nonexistent/topic.md")))


class FileEpisodeKeysTest(unittest.TestCase):
    """The drift gate's file-side identity extraction: md5 of the exact summary
    string the mirror inserts, malformed/blank/ts-less lines skipped."""

    def _write_jsonl(self, lines: list[str]) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_keys_and_skips(self) -> None:
        good = {"ts": "2026-08-10T12:00:00+00:00", "summary": "hello"}
        lines = [
            json.dumps(good),
            "",  # blank
            "not json {",  # malformed
            json.dumps({"summary": "no ts"}),  # ts-less
        ]
        path = self._write_jsonl(lines)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "episodes.jsonl").write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            keys = file_episode_keys(root)
        self.assertEqual(len(keys), 1)
        ts, md = keys[0]
        self.assertEqual(ts, good["ts"])
        self.assertEqual(md, hashlib.md5(b"hello").hexdigest())

    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(file_episode_keys(Path(d)), [])


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live drift checks")
class DirectionalDriftLiveTest(unittest.TestCase):
    """P2b exit AC: the gate must be able to FAIL. A fabricated episode key
    must come back missing; a real PG episode's key must not. Read-only."""

    def test_python_md5_matches_pg_md5(self) -> None:
        # The anti-join compares hashlib.md5 (file side) with PG md5() (row
        # side); both must hash identical UTF-8 bytes, unicode included.
        from khipu.db import connect

        for probe in ("plain ascii", "unicodé ✓ — khipu"):
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT md5(%s)", (probe,))
                    pg_md5 = cur.fetchone()[0]
            self.assertEqual(pg_md5, hashlib.md5(probe.encode("utf-8")).hexdigest())

    def test_fabricated_key_is_missing(self) -> None:
        from khipu.db import connect
        from khipu.drift import episodes_missing_in_pg

        fake = [("1999-01-01T00:00:00+00:00", hashlib.md5(b"no such episode").hexdigest())]
        with connect() as conn:
            with conn.cursor() as cur:
                missing = episodes_missing_in_pg(cur, fake)
        self.assertEqual(missing, fake)

    def test_real_episode_key_is_present(self) -> None:
        from khipu.db import connect
        from khipu.drift import episodes_missing_in_pg

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts::text, md5(summary) FROM episodes ORDER BY id LIMIT 1"
                )
                row = cur.fetchone()
                if row is None:
                    self.skipTest("episodes table empty")
                missing = episodes_missing_in_pg(cur, [(row[0], row[1])])
        self.assertEqual(missing, [])

    def test_empty_keys_is_green(self) -> None:
        from khipu.db import connect
        from khipu.drift import episodes_missing_in_pg

        with connect() as conn:
            with conn.cursor() as cur:
                self.assertEqual(episodes_missing_in_pg(cur, []), [])


class NormalizeTopicStatusTest(unittest.TestCase):
    """W5.3: evidence-based mapping over the 83 distinct topics.status values
    measured live 2026-09-03 (khipu-memory-system-53e7a0 audit)."""

    def test_seed_bucket(self):
        for raw in ("seedling", "Draft", "stub", "concept", "conceptual", "germ",
                    "proposal", "proposed", "plan", "planned", "prototype",
                    "nascent", "todo", "pending", "scratchpad", "thought", "🌱"):
            self.assertEqual(mirror.normalize_topic_status(raw), "seed", raw)

    def test_active_bucket_includes_unknown_defaults(self):
        for raw in ("active", "in-progress", "WIP", "stable", "alive",
                    "current", "operational", "healthy", "chosen-name", "OPEN"):
            self.assertEqual(mirror.normalize_topic_status(raw), "active", raw)

    def test_shipped_bucket(self):
        for raw in ("complete", "Completed", "SHIPPED", "implemented",
                    "resolved", "Initial Release", "partial-shipped", "wrapped"):
            self.assertEqual(mirror.normalize_topic_status(raw), "shipped", raw)

    def test_negated_shipped_does_not_become_shipped(self):
        self.assertNotEqual(mirror.normalize_topic_status("Staged, not shipped."), "shipped")

    def test_superseded_and_evergreen_and_abandoned(self):
        self.assertEqual(mirror.normalize_topic_status("superseded"), "superseded")
        self.assertEqual(mirror.normalize_topic_status("evergreen"), "evergreen")
        self.assertEqual(mirror.normalize_topic_status("permanent"), "evergreen")
        self.assertEqual(mirror.normalize_topic_status("Abandoned"), "abandoned")
        self.assertEqual(mirror.normalize_topic_status("retired"), "abandoned")

    def test_quotes_and_case_and_blank(self):
        self.assertEqual(mirror.normalize_topic_status('"active"'), "active")
        self.assertEqual(mirror.normalize_topic_status("'seed'"), "seed")
        self.assertEqual(mirror.normalize_topic_status(""), "active")
        self.assertEqual(mirror.normalize_topic_status(None), "active")

    def test_result_is_always_canonical(self):
        samples = ("active", "seedling", "draft", "stub", "superseded", "evergreen",
                   "complete", "in-progress", "permanent", "🌱", "P0a",
                   "LIVE - IWM bot deployed", "on_hold")
        for raw in samples:
            self.assertIn(mirror.normalize_topic_status(raw), mirror.CANONICAL_TOPIC_STATUSES, raw)


class ParseFrontmatterDateTest(unittest.TestCase):
    def test_date_only(self):
        out = mirror._parse_frontmatter_date("2026-05-28")
        self.assertTrue(out.startswith("2026-05-28"))

    def test_full_iso_with_z(self):
        out = mirror._parse_frontmatter_date("2026-05-28T12:30:00Z")
        self.assertIn("2026-05-28", out)

    def test_quoted_value(self):
        out = mirror._parse_frontmatter_date('"2026-05-28"')
        self.assertTrue(out.startswith("2026-05-28"))

    def test_blank_or_garbage_is_none(self):
        self.assertIsNone(mirror._parse_frontmatter_date(""))
        self.assertIsNone(mirror._parse_frontmatter_date(None))
        self.assertIsNone(mirror._parse_frontmatter_date("not a date"))


class ParseTopicFileDatesTest(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="topic-", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_created_and_last_updated_parsed(self):
        text = (
            "---\ntitle: Dated\nstatus: active\ncreated: 2026-01-05\n"
            "last_updated: 2026-08-30\n---\n\nBody.\n"
        )
        parsed = parse_topic_file(self._write(text))
        assert parsed is not None
        self.assertTrue(parsed["created_at"].startswith("2026-01-05"))
        self.assertTrue(parsed["updated_at"].startswith("2026-08-30"))

    def test_missing_dates_are_none(self):
        text = "---\ntitle: NoDates\nstatus: active\n---\n\nBody.\n"
        parsed = parse_topic_file(self._write(text))
        assert parsed is not None
        self.assertIsNone(parsed["created_at"])
        self.assertIsNone(parsed["updated_at"])


class UpsertEpisodeIdentityColumnsTest(unittest.TestCase):
    """W1.1/W1.3: _upsert_episode persists the new identity columns and the
    W5.1 tags column; a legacy capture_v2-shaped payload (none of these
    fields) must still insert cleanly."""

    def test_identity_and_tags_columns_are_written(self):
        cur = mock.Mock()
        cur.rowcount = 1
        payload = {
            "ts": "2026-09-03T00:00:00Z",
            "session_id": "claude_code:abc",
            "summary": "did a thing",
            "topics": ["resolved-topic"],
            "tags": ["dangling-slug"],
            "harness": "claude_code",
            "repo_root": "/Users/x/Code/Khipu",
            "project": "acme/khipu",
            "parent_session_id": "claude_code:parent",
            "transcript_range": "0:1234",
        }
        inserted = mirror._upsert_episode(cur, payload)
        self.assertTrue(inserted)
        sql, params = cur.execute.call_args.args
        self.assertIn("harness", sql)
        self.assertIn("repo_root", sql)
        self.assertIn("project", sql)
        self.assertIn("parent_session_id", sql)
        self.assertIn("transcript_range", sql)
        self.assertIn("tags", sql)
        self.assertIn("claude_code", params)
        self.assertIn("/Users/x/Code/Khipu", params)
        self.assertIn("acme/khipu", params)
        self.assertIn("claude_code:parent", params)
        self.assertIn("0:1234", params)
        self.assertIn(json.dumps(["dangling-slug"]), params)

    def test_legacy_payload_with_no_identity_fields_still_inserts(self):
        cur = mock.Mock()
        cur.rowcount = 1
        payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "legacy", "summary": "x"}
        self.assertTrue(mirror._upsert_episode(cur, payload))
        sql, params = cur.execute.call_args.args
        # harness/repo_root/project/parent_session_id/transcript_range -> None
        self.assertEqual(params[-6:-1], (None, None, None, None, None))


if __name__ == "__main__":
    unittest.main()


class _FakeCursor:
    """Records SQL. Answers the tombstone sweep's COUNT with `live_count`."""

    def __init__(self, live_count):
        self.live_count = live_count
        self.sql = []
        self._last = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self._last = self.sql[-1]

    def fetchone(self):
        if self._last and self._last.startswith("SELECT COUNT(*) FROM topics"):
            return (self.live_count,)
        return (0,)

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TombstoneCircuitBreakerTest(unittest.TestCase):
    """`topics_dir.is_dir()` was the only guard on the tombstone sweep, but a
    volume that mounts with an EMPTY topics/ passes it — `seen` comes back empty
    and `NOT (slug = ANY('{}'))` is true for every row, tombstoning the whole
    corpus. Every read filters deleted_at IS NULL and embed.backfill then
    DELETES the vectors, so recovery is a paid re-embed (audit 2026-08-17).
    No live database is touched here.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "topics").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, live_count, env=None):
        cur = _FakeCursor(live_count)
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
             mock.patch.dict(os.environ, env or {}):
            stats = mirror.reconcile_memory_root(self.root)
        return stats, cur

    def _updates(self, cur):
        return [q for q in cur.sql if q.startswith("UPDATE topics SET deleted_at")]

    def test_an_empty_topics_dir_never_tombstones(self):
        stats, cur = self._run(live_count=622)
        self.assertEqual(self._updates(cur), [], "the sweep must not run on an empty read")
        self.assertEqual(stats["tombstoned"], 0)
        self.assertIn("refusing", stats["tombstone_skipped"])

    def test_an_empty_root_with_an_empty_pg_is_a_no_op_not_an_alarm(self):
        stats, cur = self._run(live_count=0)
        self.assertEqual(stats["tombstoned"], 0)
        self.assertNotIn("tombstone_skipped", stats)

    def test_a_plausible_deletion_still_sweeps(self):
        for i in range(20):
            (self.root / "topics" / f"t{i:02d}.md").write_text(f"# T{i}\n\nbody")
        stats, cur = self._run(live_count=21)          # one topic genuinely gone
        self.assertEqual(len(self._updates(cur)), 1, "a normal deletion must still sweep")
        self.assertNotIn("tombstone_skipped", stats)

    def test_an_implausibly_large_deletion_is_refused(self):
        for i in range(5):
            (self.root / "topics" / f"t{i:02d}.md").write_text(f"# T{i}\n\nbody")
        stats, cur = self._run(live_count=600)         # 595 would vanish
        self.assertEqual(self._updates(cur), [])
        self.assertIn("KHIPU_ALLOW_MASS_TOMBSTONE", stats["tombstone_skipped"])

    def test_the_override_allows_a_real_bulk_retirement(self):
        for i in range(5):
            (self.root / "topics" / f"t{i:02d}.md").write_text(f"# T{i}\n\nbody")
        stats, cur = self._run(live_count=600, env={"KHIPU_ALLOW_MASS_TOMBSTONE": "1"})
        self.assertEqual(len(self._updates(cur)), 1)



class _MirrorGraphCursor:
    """Records every executed statement (SAVEPOINT included) for the
    mirror_episode graph-mint tests (fix 11)."""

    def __init__(self):
        self.executed: list[tuple] = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _MirrorGraphConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class MirrorEpisodeGraphMintTest(unittest.TestCase):
    """fix 11: mirror.mirror_episode (the legacy write path) mints the
    topic:/path: graph from the payload's topics, same as capture.write_pg's
    hub path — before this fix it minted nothing from a capture payload at
    all (only mirror_topic_file, driven by an actual topic FILE, did)."""

    def test_mints_the_topic_graph_via_persist_capture_graph(self):
        cur = _MirrorGraphCursor()
        payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                   "summary": "did a thing", "topics": ["a-topic"]}
        with mock.patch("khipu.db.connect", return_value=_MirrorGraphConn(cur)), \
                mock.patch.object(mirror, "persist_capture_graph") as m_persist:
            ok = mirror.mirror_episode(payload)
        self.assertTrue(ok)
        m_persist.assert_called_once_with(cur, payload)

    def test_graph_mint_failure_rolls_back_but_keeps_the_episode(self):
        cur = _MirrorGraphCursor()
        payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                   "summary": "did a thing", "topics": ["a-topic"]}
        conn = _MirrorGraphConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(mirror, "persist_capture_graph",
                                   side_effect=RuntimeError("boom")):
            ok = mirror.mirror_episode(payload)
        self.assertTrue(ok, "the episode insert must survive a graph-mint failure")
        self.assertEqual(conn.commits, 1)
        statements = [s for s, _ in cur.executed]
        self.assertIn("SAVEPOINT mirror_capture_graph", statements)
        self.assertIn("ROLLBACK TO SAVEPOINT mirror_capture_graph", statements)

    def test_blank_summary_never_reaches_the_graph_mint(self):
        with mock.patch.object(mirror, "persist_capture_graph") as m_persist:
            ok = mirror.mirror_episode({"summary": "   "})
        self.assertFalse(ok)
        m_persist.assert_not_called()


class UpsertEpisodeSchemaGateTest(unittest.TestCase):
    """Audit 2026-09-04: _upsert_episode named harness/repo_root/project/
    parent_session_id/transcript_range/tags UNCONDITIONALLY. On a hub that had
    not run migrations 0008/0010 every INSERT raised UndefinedColumn, so
    capture.write_pg treated a perfectly healthy hub as unreachable and sent
    every capture to the outbox."""

    class _Cur:
        def __init__(self, columns):
            self.columns = columns
            self.statements: list[str] = []
            self.params: list[tuple] = []
            self._result: list[tuple] = []
            self.rowcount = 1

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            self.statements.append(s)
            self.params.append(params)
            if "information_schema.columns" in s:
                self._result = [(c,) for c in self.columns]
            else:
                self._result = []

        def fetchall(self):
            return list(self._result)

    BASE = ("id", "ts", "session_id", "summary", "topics", "people", "decisions",
            "preferences", "scope", "edges", "raw")

    def _insert_sql(self, cur):
        return [s for s in cur.statements if s.startswith("INSERT INTO episodes")][0]

    def test_a_pre_migration_hub_gets_only_the_base_columns(self):
        cur = self._Cur(self.BASE)
        self.assertTrue(mirror._upsert_episode(cur, {
            "ts": "2026-09-03T00:00:00Z", "session_id": "s", "summary": "x",
            "harness": "claude_code", "tags": ["t"],
        }))
        sql = self._insert_sql(cur)
        for col in ("harness", "repo_root", "project", "parent_session_id",
                    "transcript_range", "tags"):
            self.assertNotIn(col, sql, col)
        # 10 base values, no more.
        self.assertEqual(len(cur.params[-1]), 10)

    def test_a_hub_with_0008_but_not_0010_omits_only_tags(self):
        cur = self._Cur(self.BASE + ("harness", "repo_root", "project",
                                     "parent_session_id", "transcript_range"))
        mirror._upsert_episode(cur, {"ts": "t", "session_id": "s", "summary": "x"})
        sql = self._insert_sql(cur)
        self.assertIn("harness", sql)
        self.assertNotIn("tags", sql)
        self.assertEqual(len(cur.params[-1]), 15)

    def test_a_fully_migrated_hub_is_unchanged(self):
        cur = self._Cur(self.BASE + ("harness", "repo_root", "project",
                                     "parent_session_id", "transcript_range", "tags"))
        mirror._upsert_episode(cur, {"ts": "t", "session_id": "s", "summary": "x"})
        sql = self._insert_sql(cur)
        self.assertIn("tags", sql)
        self.assertEqual(len(cur.params[-1]), 16)


class UnterminatedFrontmatterTest(unittest.TestCase):
    """A page that opens a ``---`` block and never closes it must parse (as a
    body with default metadata), not raise. 45 such pages in the memory tree
    killed the nightly reconcile from 2026-09-03 to 2026-09-05 and, with it,
    the embed backfill that ran after it."""

    def test_open_block_without_closer_parses_with_defaults(self) -> None:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="topic-", delete=False, encoding="utf-8"
        )
        text = "---\ntitle: capture-v2\nstatus: active\nlinks:\n  - gateway\n"
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        parsed = parse_topic_file(Path(tmp.name))
        assert parsed is not None
        self.assertEqual(parsed["status"], "active")
        self.assertEqual(parsed["title"], Path(tmp.name).stem)
        self.assertEqual(parsed["body"], text)
        self.assertEqual(parsed["frontmatter"], {})
