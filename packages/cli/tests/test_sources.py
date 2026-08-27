"""Tests for khipu.sources — membership store and resolved JSON contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import sources


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.dir)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _write_sources(self, rows: list[dict]) -> None:
        doc = {"schema_version": sources.SCHEMA_VERSION, "sources": rows}
        path = self.dir / "graph_sources.json"
        path.write_text(json.dumps(doc) + "\n", encoding="utf-8")

    def test_missing_file_is_empty_and_does_not_create_the_file(self):
        doc = sources.load_sources()
        self.assertFalse((self.dir / "graph_sources.json").exists())
        self.assertEqual(doc["sources"], [])
        self.assertTrue(sources.conversation_memory_enabled())
        self.assertNotIn("conversation_memory", doc)

    def test_membership_store_is_sources_list(self):
        doc = sources.default_document()
        self.assertIsInstance(doc["sources"], list)
        self.assertEqual(doc["sources"], [])
        self.assertEqual(doc["schema_version"], sources.SCHEMA_VERSION)
        self.assertNotIn("conversation_memory", doc)

    def test_disable_does_not_drop_the_row(self):
        self._write_sources([{"id": "reports:claude", "enabled": True}])
        sources.set_enabled("reports:claude", False)
        ids = {s["id"] for s in sources.load_sources()["sources"]}
        self.assertIn("reports:claude", ids)
        resolved = sources.resolve_for_graphify()
        self.assertFalse(resolved["collectors"]["reports"])

    def test_missing_root_is_unreachable_not_deleted_from_doc(self):
        sources.add_code_root(Path("/nope/khipu-missing-root"))
        resolved = sources.resolve_for_graphify()
        self.assertTrue(
            any(u["id"].startswith("code:") for u in resolved["unreachable"])
        )
        self.assertNotIn(
            Path("/nope/khipu-missing-root").as_posix(), resolved["code_roots"]
        )

    def test_resolved_missing_collector_key_defaults_true(self):
        raw = {"schema_version": 1, "collectors": {"reports": False}}
        flags = sources.collector_flags_from_resolved(raw)
        self.assertIs(flags["reports"], False)
        self.assertIs(flags["skills"], True)

    def test_export_failure_does_not_delete_existing_resolved_file(self):
        dest = Path(self.dir) / "graph_sources.resolved.json"
        prior = '{"schema_version": 1, "collectors": {"reports": true}}'
        dest.write_text(prior)
        with mock.patch.object(
            sources, "resolve_for_graphify", side_effect=OSError("disk")
        ):
            with self.assertRaises(OSError):
                sources.export_resolved(path=dest)
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_text(), prior)

    def test_cannot_remove_seeded_id(self):
        with self.assertRaises(ValueError):
            sources.remove_user_source("code:claude")

    def test_owned_source_ids_enabled_reachable_only(self):
        self._write_sources(
            [
                {
                    "id": "code:mac2",
                    "kind": "code_ast",
                    "root": str(self.dir),
                    "enabled": True,
                },
                {"id": "reports:claude", "enabled": False},
            ]
        )
        owned = sources.owned_source_ids()
        self.assertIn("code:mac2", owned)
        self.assertNotIn("reports:claude", owned)

    def test_owned_source_ids_excludes_unreachable(self):
        self._write_sources(
            [
                {
                    "id": "code:missing",
                    "kind": "code_ast",
                    "root": "/nope/missing-root",
                    "enabled": True,
                },
            ]
        )
        self.assertNotIn("code:missing", sources.owned_source_ids())

    def test_unmatched_code_path_not_delete_candidate(self):
        node = {
            "id": "module:orphan",
            "type": "module",
            "bucket": "code",
            "source_path": "/other/mac/project/foo.py",
        }
        self.assertIsNone(
            sources.source_id_for_delete(
                node_id=node["id"],
                type=node["type"],
                bucket=node["bucket"],
                source_path=node["source_path"],
            )
        )
        self.assertFalse(sources.should_delete_graphify_node(node, set()))

    def test_null_source_id_not_delete_candidate(self):
        node = {
            "id": "module:unknown",
            "type": "module",
            "bucket": "code",
            "source_path": None,
            "source_id": None,
        }
        self.assertFalse(sources.should_delete_graphify_node(node, set()))

    def test_add_code_root_sets_graph_producer(self):
        root = self.dir / "repo"
        root.mkdir()
        with mock.patch("khipu.components_matrix.set_graph_producer") as set_prod:
            sources.add_code_root(root)
            set_prod.assert_called_once_with(True)

    def test_upsert_unmatched_code_not_stamped_without_code_claude(self):
        self._write_sources(
            [
                {
                    "id": "code:mac2",
                    "kind": "code_ast",
                    "root": str(self.dir),
                    "enabled": True,
                },
            ]
        )
        sid = sources.source_id_for_graphify_node(
            node_id="module:orphan",
            type="module",
            bucket="code",
            source_path="/other/mac/project/foo.py",
        )
        self.assertIsNone(sid)

    def test_upsert_unmatched_code_stamped_when_code_claude_listed(self):
        self._write_sources(
            [
                {
                    "id": "code:claude",
                    "kind": "code_ast",
                    "root": str(self.dir),
                    "enabled": True,
                },
            ]
        )
        sid = sources.source_id_for_graphify_node(
            node_id="module:orphan",
            type="module",
            bucket="code",
            source_path="/other/mac/project/foo.py",
        )
        self.assertEqual(sid, "code:claude")


class ResolveContractTest(unittest.TestCase):
    def test_resolved_collector_keys_match_build_graph_contract(self):
        keys = set(sources.resolve_for_graphify()["collectors"])
        self.assertEqual(
            keys,
            {
                "tickers",
                "skills",
                "agents",
                "reports",
                "memory_topics",
                "predictive_gates",
                "frozen_tell",
                "hardcoded_data_sources",
                "hardcoded_notion_dbs",
                "biblical",
                "model_call_log",
                "code_ast",
                "code_semantic",
            },
        )


class SourcesCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {
                "KHIPU_DATA_DIR": str(self.dir),
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(Path(__file__).resolve().parents[2]),
                        os.environ.get("PYTHONPATH", ""),
                    ]
                ),
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env["KHIPU_DATA_DIR"] = str(self.dir)
        env["PYTHONPATH"] = os.pathsep.join([str(root), env.get("PYTHONPATH", "")])
        return subprocess.run(
            [sys.executable, "-m", "khipu.cli", *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def _write_sources(self, rows: list[dict]) -> None:
        doc = {"schema_version": sources.SCHEMA_VERSION, "sources": rows}
        path = self.dir / "graph_sources.json"
        path.write_text(json.dumps(doc) + "\n", encoding="utf-8")

    def test_disable_then_list_shows_enabled_false(self):
        self._write_sources([{"id": "reports:claude", "enabled": True}])
        r = self._run(["sources", "disable", "reports:claude"])
        self.assertEqual(r.returncode, 0, r.stderr)
        listed = json.loads(self._run(["sources", "list"]).stdout)
        row = next(s for s in listed["sources"] if s["id"] == "reports:claude")
        self.assertFalse(row["enabled"])

    def test_export_writes_collectors_reports_false(self):
        self._write_sources([{"id": "reports:claude", "enabled": True}])
        self._run(["sources", "disable", "reports:claude"])
        dest = self.dir / "out.resolved.json"
        r = self._run(["sources", "export"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = sources.export_resolved(path=dest)
        self.assertFalse(out["collectors"]["reports"])

    def test_unknown_id_exit_2(self):
        r = self._run(["sources", "enable", "not-a-real-id"])
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
