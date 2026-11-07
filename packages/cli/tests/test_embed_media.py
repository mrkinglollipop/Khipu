"""What is left of the image-embedding tests: the migration, and that image coverage never gates activation."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import embed
from khipu import sources


class MigrationMediaKindTest(unittest.TestCase):
    def test_0006_widens_kind_check_to_media(self):
        sql = (
            Path(__file__).resolve().parents[3]
            / "ops"
            / "migrations"
            / "0006_memory_embeddings_media.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("CHECK (kind IN ('episode', 'topic', 'media'))", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS media_assets", sql)
        self.assertNotIn("'garbage'", sql)


class CleanInstallSourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.dir)})
        self._env.start()
        sources.save_sources({"sources": [
            {"id": "code:claude", "root": str(self.dir), "enabled": True},
            {"id": "conversation_memory", "enabled": True},
        ]})

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_clean_install_has_no_sources(self):
        sources.sources_file().unlink()
        self.assertEqual(sources.load_sources()["sources"], [])


class ActivateIgnoresMediaTest(unittest.TestCase):
    def test_activate_gate_uses_episode_topic_missing_only(self):
        cov = {
            "episodes": {"missing": 0},
            "topics": {"missing": 0},
            "media": {"missing": 99},
            "missing": 0, "stale": 0,
        }
        with mock.patch("khipu.db.connect") as connect, \
             mock.patch.object(embed, "coverage", return_value=cov), \
             mock.patch.object(embed._profiles, "ensure_profile_index") as index:
            conn = connect.return_value.__enter__.return_value
            cur = conn.cursor.return_value.__enter__.return_value
            cur.fetchone.return_value = (embed.PROFILE_2,)
            out = embed.activate(embed.PROFILE_2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["active_profile"], embed.PROFILE_2)
        index.assert_called_once()
        self.assertIn(mock.call("UPDATE embedding_profiles SET is_active = false WHERE is_active"), cur.execute.call_args_list)
        self.assertIn(mock.call("UPDATE embedding_profiles SET is_active = true WHERE id = %s", (embed.PROFILE_2,)), cur.execute.call_args_list)
        conn.commit.assert_called_once()

    def test_missing_text_vectors_prevent_pointer_updates(self):
        for kind in ("episodes", "topics"):
            cov = {"episodes": {"missing": 0}, "topics": {"missing": 0}, "media": {"missing": 99}}
            cov[kind]["missing"] = 1
            cov.update(missing=1, stale=0)
            with self.subTest(kind=kind), mock.patch("khipu.db.connect") as connect, \
                 mock.patch.object(embed, "coverage", return_value=cov), \
                 mock.patch.object(embed._profiles, "ensure_profile_index") as index:
                conn = connect.return_value.__enter__.return_value
                cur = conn.cursor.return_value.__enter__.return_value
                cur.fetchone.return_value = (embed.PROFILE_2,)
                with self.assertRaisesRegex(RuntimeError, "1 chunks missing and 0 stale"):
                    embed.activate(embed.PROFILE_2)
                self.assertFalse(any("UPDATE" in c.args[0] for c in cur.execute.call_args_list))
                index.assert_not_called()
                conn.commit.assert_not_called()
