"""Topic path/wiki persist, search enrich, and graph alias expand."""
from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import graph_sync as gs
from khipu.mirror import _upsert_topic, backfill_topic_graph, parse_topic_file
from khipu.topic_graph import (
    KHIPU_BUCKET,
    LIVES_IN_EDGE,
    MEMORY_TOPIC_PREFIX,
    PATH_PREFIX,
    TOPIC_PREFIX,
    assert_mintable_id,
    collapse_semantic_topic_hits,
    enrich_search_results,
    extract_paths,
    graph_query_aliases,
    parse_frontmatter_links,
    persist_topic_graph,
    topic_aliases,
    topic_slug_from_label,
)

UNSEEN_BODY = """## A-cut fill + Klein corpus (2026-07-26)

- Fal fill dir: `sojourn/art-samples/uw-intro-acut-fill-2026-07-26/` (CHOOSE_WAVE1–3, LEB verses)
- Klein style corpus (local, pre-train): `sojourn_art/nephilim/klein_style_lora/corpus/nphlm_style/` — 27 pairs
- Ellipsis skip: `art-samples/.../CONTEXT_GUIDE.md`
"""

LINKS_FM = """title: Sojourn - Unseen War Art
status: in_progress
links:
  - sojourn-unseen-war-project
"""


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


class ParserTest(unittest.TestCase):
    def test_links_grammar_skips_the_key_line(self) -> None:
        self.assertEqual(
            parse_frontmatter_links(LINKS_FM),
            ["sojourn-unseen-war-project"],
        )
        self.assertNotIn("links", parse_frontmatter_links(LINKS_FM))

    def test_unseen_war_paths_and_ellipsis_reject(self) -> None:
        paths = extract_paths(UNSEEN_BODY)
        joined = " ".join(paths)
        self.assertIn("uw-intro-acut-fill-2026-07-26", joined)
        self.assertIn("sojourn_art/nephilim/klein_style_lora/corpus/nphlm_style", joined)
        self.assertFalse(any("..." in p or "…" in p for p in paths))
        self.assertNotIn("art-samples/.../CONTEXT_GUIDE.md", paths)

    def test_rejects_urls_and_volume_root(self) -> None:
        self.assertEqual(extract_paths("see https://example.com/foo/bar"), [])
        from khipu import topic_graph as tg

        with (
            mock.patch.dict(os.environ, {"KHIPU_VOLUME_ROOT": "/Volumes/Example"}),
            mock.patch.object(tg, "volume_root", return_value="/Volumes/Example"),
        ):
            self.assertEqual(tg.extract_paths("`/Volumes/Example`"), [])

    def test_real_example_topic_file_if_present(self) -> None:
        path = Path("/Volumes/Example/Memory/conversations/topics/example-topic.md")
        if not path.is_file():
            self.skipTest("fixture topic file not on this machine")
        parsed = parse_topic_file(path)
        assert parsed is not None
        self.assertEqual(parsed["links"], ["sojourn-unseen-war-project"])
        self.assertFalse(any("..." in p for p in extract_paths(parsed["body"])))


class AliasExpandTest(unittest.TestCase):
    def test_peel_then_union(self) -> None:
        expected = {
            "unseen-war-intro",
            "topic:unseen-war-intro",
            "memory_topic:unseen-war-intro",
        }
        self.assertEqual(set(topic_aliases("unseen-war-intro")), expected)
        self.assertEqual(set(topic_aliases("topic:unseen-war-intro")), expected)
        self.assertEqual(set(topic_aliases("memory_topic:unseen-war-intro")), expected)

    def test_code_node_stays_singleton(self) -> None:
        nid = "module:skills__shared_predictive_gates_scripts_signals_py"
        self.assertEqual(graph_query_aliases(nid), [nid])

    def test_digit_id_is_not_a_topic_slug(self) -> None:
        self.assertEqual(graph_query_aliases("9320"), [])

    def test_topic_slug_from_label_folds_case(self) -> None:
        self.assertEqual(topic_slug_from_label("OpenBot"), "openbot")
        self.assertEqual(topic_slug_from_label("CopilotKit OpenBot"), "copilotkit-openbot")

    def test_persist_capture_graph_stars_from_first_topic(self) -> None:
        from khipu import topic_graph as tg

        with mock.patch.object(
            tg, "persist_topic_graph", return_value={"nodes_minted": 1, "edges_minted": 1}
        ) as persist:
            stats = tg.persist_capture_graph(
                mock.Mock(),
                {"topics": ["OpenBot", "khipu"], "summary": "see `AI/Guides/x.md`"},
            )
        self.assertEqual(persist.call_count, 2)
        first = persist.call_args_list[0].args[1]
        self.assertEqual(first["slug"], "openbot")
        self.assertEqual(first["links"], ["khipu"])
        self.assertEqual(stats["nodes_minted"], 2)

    def test_never_double_prefix(self) -> None:
        aliases = topic_aliases("topic:unseen-war-intro")
        self.assertNotIn("topic:topic:unseen-war-intro", aliases)


class SemanticSurvivorTest(unittest.TestCase):
    def test_max_score_then_min_chunk_episodes_unchanged(self) -> None:
        ep = {"kind": "episode", "id": "1", "score": 0.9, "chunk_idx": 0, "snippet": "x"}
        low = {"kind": "topic", "id": "unseen-war-intro", "score": 0.5, "chunk_idx": 0}
        high_late = {"kind": "topic", "id": "unseen-war-intro", "score": 0.8, "chunk_idx": 3}
        tie_early = {"kind": "topic", "id": "unseen-war-intro", "score": 0.8, "chunk_idx": 1}
        rows = [ep, low, high_late, tie_early]
        out = collapse_semantic_topic_hits(rows)
        topics = [r for r in out if r["kind"] == "topic"]
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["chunk_idx"], 1)
        self.assertEqual(topics[0]["score"], 0.8)
        self.assertEqual(out[0], ep)


class OwnershipTest(unittest.TestCase):
    def test_refuses_graphify_ids(self) -> None:
        with self.assertRaises(ValueError):
            assert_mintable_id("memory_topic:unseen-war-intro")
        with self.assertRaises(ValueError):
            assert_mintable_id("unseen-war-intro")

    def test_insert_sql_does_not_overwrite_foreign_bucket(self) -> None:
        from khipu import topic_graph as tg

        src = inspect.getsource(tg._insert_khipu_node)
        self.assertIn("WHERE nodes.bucket = %s", src)
        self.assertIn(KHIPU_BUCKET, inspect.getsource(tg))

    def test_graphify_delete_spares_conversation_memory(self) -> None:
        self.assertIn("conversation-memory", gs.KHIPU_OWNED_NODE_SQL)
        sync_src = inspect.getsource(gs.sync_from_sqlite)
        self.assertIn("should_delete_graphify_node", sync_src)
        self.assertIn("KHIPU_OWNED_NODE_SQL", sync_src)

    def test_persist_never_mints_memory_topic(self) -> None:
        cur = mock.Mock()
        cur.fetchall.return_value = []
        parsed = {
            "slug": "unseen-war-intro",
            "title": "Unseen War Intro",
            "body": UNSEEN_BODY,
            "links": ["sojourn-unseen-war-project"],
        }
        persist_topic_graph(cur, parsed, dry_run=False)
        for call in cur.execute.call_args_list:
            sql = call.args[0] if call.args else ""
            params = call.args[1] if len(call.args) > 1 else ()
            blob = sql + json.dumps(params, default=str)
            self.assertNotIn(MEMORY_TOPIC_PREFIX, blob)
            if "INSERT INTO nodes" in sql:
                self.assertTrue(
                    str(params[0]).startswith(TOPIC_PREFIX)
                    or str(params[0]).startswith(PATH_PREFIX)
                )


class UpsertConflictTest(unittest.TestCase):
    def test_on_conflict_updates_links_and_frontmatter(self) -> None:
        cur = mock.Mock()
        cur.fetchone.return_value = ("oldhash",)
        cur.fetchall.return_value = []
        parsed = {
            "slug": "unseen-war-intro",
            "title": "Unseen War Intro",
            "status": "stub",
            "body": UNSEEN_BODY,
            "digest": "newhash",
            "links": ["sojourn-unseen-war-project"],
            "frontmatter": {"title": "Unseen War Intro", "links": ["sojourn-unseen-war-project"]},
        }
        _upsert_topic(
            cur,
            parsed,
            "/tmp/unseen-war-intro.md",
            source="test",
            note="test",
            sync_graph=False,
        )
        upsert_sql = cur.execute.call_args_list[1].args[0]
        self.assertIn("ON CONFLICT (slug) DO UPDATE SET", upsert_sql)
        self.assertIn("frontmatter = EXCLUDED.frontmatter", upsert_sql)
        self.assertIn("links = EXCLUDED.links", upsert_sql)
        params = cur.execute.call_args_list[1].args[1]
        self.assertIn("sojourn-unseen-war-project", params[5])


class EnrichTest(unittest.TestCase):
    def test_additive_keys_and_union_neighbors(self) -> None:
        cur = mock.Mock()

        def _execute(sql, params=None):
            cur._sql = sql

        def _fetchall():
            if "FROM topics" in cur._sql:
                return [("unseen-war-intro", UNSEEN_BODY)]
            return [
                (
                    "topic:unseen-war-intro",
                    "path:sojourn/art-samples/uw-intro-acut-fill-2026-07-26",
                    LIVES_IN_EDGE,
                )
            ]

        cur.execute.side_effect = _execute
        cur.fetchall.side_effect = _fetchall
        rows = [
            {
                "kind": "topic",
                "id": "unseen-war-intro",
                "label": "Unseen War Intro",
                "snippet": "short",
                "score": 0.7,
                "chunk_idx": 0,
            }
        ]
        out = enrich_search_results(cur, rows)
        self.assertEqual(len(out), 1)
        for key in ("kind", "id", "label", "snippet"):
            self.assertIn(key, out[0])
        self.assertTrue(any("uw-intro-acut-fill-2026-07-26" in p for p in out[0]["paths"]))
        self.assertEqual(out[0]["neighbors"][0]["type"], LIVES_IN_EDGE)


class DryRunBackfillTest(unittest.TestCase):
    def test_dry_run_reports_column_and_graph_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            topics = root / "topics"
            topics.mkdir()
            (topics / "demo.md").write_text(
                "---\ntitle: Demo\nlinks:\n  - other\n---\n\nSee `foo/bar/baz/` here.\n",
                encoding="utf-8",
            )
            fake_conn = mock.MagicMock()
            cur = mock.Mock()
            cur.fetchone.return_value = ([], {})
            cur.fetchall.return_value = []
            fake_conn.cursor.return_value.__enter__.return_value = cur
            fake_conn.cursor.return_value.__exit__.return_value = False

            with mock.patch("khipu.db.connect") as connect:
                connect.return_value.__enter__.return_value = fake_conn
                connect.return_value.__exit__.return_value = False
                stats = backfill_topic_graph(root, dry_run=True)
        self.assertTrue(stats["dry_run"])
        self.assertEqual(stats["topics"], 1)
        self.assertGreaterEqual(stats["column_updates"], 1)
        self.assertGreaterEqual(stats["nodes_minted"], 1)
        self.assertGreaterEqual(stats["edges_minted"], 1)
        fake_conn.rollback.assert_called()
        fake_conn.commit.assert_not_called()


class ConversationMemoryToggleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.dir)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_persist_no_ops_when_conversation_memory_disabled(self):
        from khipu import sources

        sources.set_enabled("conversation_memory", False)
        cur = mock.Mock()
        cur.fetchall.return_value = []
        parsed = {
            "slug": "demo-topic",
            "title": "Demo",
            "body": "See `foo/bar/baz/` here.",
            "links": [],
        }
        stats = persist_topic_graph(cur, parsed, dry_run=False)
        self.assertEqual(stats["nodes_minted"], 0)
        self.assertEqual(stats["edges_minted"], 0)
        for call in cur.execute.call_args_list:
            sql = call.args[0] if call.args else ""
            self.assertNotIn("INSERT INTO nodes", sql)


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live path/graph probe")
class LiveFillDirProbeTest(unittest.TestCase):
    """Laptop CI, not Hub acceptance. Transaction is rolled back after graph mint."""

    def test_search_and_graph_name_the_fill_dir(self) -> None:
        from khipu.cli import _graph_query, _search_query
        from khipu.db import connect

        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT slug, title, body, links FROM topics "
                    "WHERE slug = %s AND deleted_at IS NULL",
                    ("unseen-war-intro",),
                )
                row = cur.fetchone()
                if row is None:
                    self.skipTest("unseen-war-intro not in PG")
                slug, title, body, links = row
                parsed = {
                    "slug": slug,
                    "title": title or slug,
                    "body": body or "",
                    "links": list(links or []),
                }
                hits = enrich_search_results(
                    cur,
                    _search_query(cur, "sojourn unseen war art fill", 50),
                )
                intro = next((h for h in hits if h.get("id") == "unseen-war-intro"), None)
                if intro is None:
                    intro = next(
                        (
                            h
                            for h in enrich_search_results(
                                cur, _search_query(cur, "unseen-war-intro", 12)
                            )
                            if h.get("id") == "unseen-war-intro"
                        ),
                        None,
                    )
                self.assertIsNotNone(intro, "search did not return unseen-war-intro")
                assert intro is not None
                self.assertTrue(
                    any("uw-intro-acut-fill-2026-07-26" in p for p in intro.get("paths") or []),
                    intro.get("paths"),
                )
                persist_topic_graph(cur, parsed, dry_run=False)
                graph = _graph_query(cur, "unseen-war-intro", 1, 50)
                blob = json.dumps(graph)
                self.assertIn("uw-intro-acut-fill-2026-07-26", blob)
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()


if __name__ == "__main__":
    unittest.main()


class VolumeRootDerivationTest(unittest.TestCase):
    def test_env_wins_then_memory_root_then_empty(self):
        from khipu import topic_graph as tg

        with mock.patch.dict(os.environ, {"KHIPU_VOLUME_ROOT": "/Volumes/FromEnv/"}):
            self.assertEqual(tg.volume_root(), "/Volumes/FromEnv")
        with mock.patch.dict(os.environ, {"KHIPU_VOLUME_ROOT": ""}), \
                mock.patch("khipu.config.path_setting", return_value="/Volumes/Example/Memory/conversations"):
            self.assertEqual(tg.volume_root(), "/Volumes/Example")
        with mock.patch.dict(os.environ, {"KHIPU_VOLUME_ROOT": ""}), \
                mock.patch("khipu.config.path_setting", return_value=None):
            self.assertEqual(tg.volume_root(), "")
            self.assertEqual(tg._volume_prefixes(), ())
