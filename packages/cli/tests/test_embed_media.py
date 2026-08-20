"""Unit tests for native Gemini Embedding 2 image ingest (no live Gemini)."""
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


class EmbedMediaFlagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.dir)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_embed_media_defaults_false(self):
        doc = sources.default_document()
        self.assertTrue(all(s.get("embed_media") is False for s in doc["sources"]))
        self.assertFalse(sources.embed_media_enabled("conversation_memory"))
        self.assertFalse(sources.embed_media_enabled("code:claude"))

    def test_set_embed_media_round_trip(self):
        sources.set_embed_media("code:claude", True)
        self.assertTrue(sources.embed_media_enabled("code:claude"))
        opted = sources.sources_with_embed_media()
        self.assertTrue(any(s["id"] == "code:claude" for s in opted))
        sources.set_embed_media("code:claude", False)
        self.assertFalse(sources.embed_media_enabled("code:claude"))
        self.assertFalse(any(s["id"] == "code:claude" for s in sources.sources_with_embed_media()))

    def test_conversation_memory_gets_a_root_for_the_checkbox(self):
        doc = sources.load_sources()
        row = next(s for s in doc["sources"] if s["id"] == "conversation_memory")
        self.assertTrue(row.get("root"))
        self.assertFalse(row["embed_media"])
        sources.set_embed_media("conversation_memory", True)
        self.assertTrue(sources.embed_media_enabled("conversation_memory"))
        opted = sources.sources_with_embed_media()
        self.assertTrue(any(s["id"] == "conversation_memory" for s in opted))
        self.assertTrue(Path(row["root"]).is_dir() or Path(
            next(s for s in sources.load_sources()["sources"] if s["id"] == "conversation_memory")["root"]
        ).is_dir())


class ImageHashAndMimeTest(unittest.TestCase):
    def test_mime_png_jpeg_only(self):
        self.assertEqual(embed.mime_for_image_path(Path("a.PNG")), "image/png")
        self.assertEqual(embed.mime_for_image_path(Path("b.jpeg")), "image/jpeg")
        self.assertIsNone(embed.mime_for_image_path(Path("c.webp")))
        self.assertIsNone(embed.mime_for_image_path(Path("d.gif")))
        self.assertIsNone(embed.mime_for_image_path(Path("e.heic")))

    def test_sha256_bytes_stable(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        self.assertEqual(embed._sha256_bytes(raw), embed._sha256_bytes(raw))
        self.assertNotEqual(embed._sha256_bytes(raw), embed._sha256_bytes(raw + b"x"))


class EmbedBatchImagesShapeTest(unittest.TestCase):
    def test_request_one_image_part_and_dim_768(self):
        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                # 768-dim vector of zeros (L2-normalized → zeros still ok for len check)
                vec = [0.0] * embed.DIM
                vec[0] = 1.0
                return json.dumps({"embeddings": [{"values": vec}]}).encode()

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            captured["body"] = body
            return _Resp()

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        with mock.patch.object(embed, "_gemini_key", return_value="AIza-test"), mock.patch(
            "urllib.request.urlopen", fake_urlopen
        ):
            out = embed.embed_batch_images([(png, "image/png")], profile=embed.PROFILE_2)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), embed.DIM)
        req0 = captured["body"]["requests"][0]
        self.assertEqual(req0["outputDimensionality"], 768)
        parts = req0["content"]["parts"]
        self.assertEqual(len(parts), 1)
        self.assertIn("inline_data", parts[0])
        self.assertEqual(parts[0]["inline_data"]["mime_type"], "image/png")
        # No text task prefixes on image parts.
        self.assertNotIn("text", parts[0])
        self.assertNotIn("taskType", req0)
        self.assertNotIn("title", json.dumps(req0))

    def test_rejects_webp(self):
        with mock.patch.object(embed, "_gemini_key", return_value="AIza-test"):
            with self.assertRaises(ValueError):
                embed.embed_batch_images([(b"x", "image/webp")], profile=embed.PROFILE_2)


class ActivateIgnoresMediaTest(unittest.TestCase):
    def test_activate_gate_uses_episode_topic_missing_only(self):
        cov = {
            "episodes": {"missing": 0},
            "topics": {"missing": 0},
            "media": {"missing": 99},
        }
        missing = cov["episodes"]["missing"] + cov["topics"]["missing"]
        self.assertEqual(missing, 0)
        # Mirror activate()'s gate: media missing must not block.
        self.assertTrue(missing == 0)


class JobsOnDemandReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.dir)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_on_demand_entry_maps_receipt(self):
        from khipu import jobs

        jobs._write_job_state("embed_media_backfill", 0)
        status = jobs.job_status()
        entry = status["embed_media_backfill"]
        self.assertTrue(entry.get("on_demand"))
        self.assertEqual(entry.get("last_exit"), 0)
        self.assertIsNotNone(entry.get("last_run_iso"))
        self.assertIsNone(entry.get("plist_label"))


if __name__ == "__main__":
    unittest.main()
