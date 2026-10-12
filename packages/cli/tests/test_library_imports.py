"""Independent library and memory request ceilings; all accounting is temporary."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from khipu import embed


class LibraryBudgetScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for patch in (
            mock.patch("khipu.paths.ensure_data_dir", return_value=self.root),
            mock.patch.object(embed, "EMBED_DAILY_CALL_BUDGET", 1),
            mock.patch.object(embed, "LIBRARY_DAILY_CALL_BUDGET", 1),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_library_take_increments_only_the_library_file_and_uses_its_lock(self):
        embed._budget_take(scope="library")
        path = self.root / "state" / "embed-budget-library.json"
        self.assertEqual(json.loads(path.read_text())["calls"], 1)
        self.assertFalse((path.parent / "embed-budget.json").exists())
        self.assertTrue(path.with_suffix(".json.lock").is_file())
        self.assertFalse((path.parent / "embed-budget.json.lock").exists())
        status = embed.budget_status(scope="library")
        self.assertEqual((status["calls"], status["cap"], status["remaining"]), (1, 1, 0))
        self.assertEqual(embed.budget_status()["calls"], 0)
        self.assertEqual(set(status), set(embed.budget_status()))

    def test_exhausted_library_ceiling_does_not_stop_memory_and_names_its_variable(self):
        embed._budget_take(scope="library")
        with self.assertRaisesRegex(RuntimeError, "embed budget exhausted.*KHIPU_LIBRARY_EMBED_DAILY_CALLS"):
            embed._budget_take(scope="library")
        embed._budget_take()
        self.assertEqual(embed.budget_status()["calls"], 1)
        self.assertEqual(embed.budget_status(scope="library")["calls"], 1)

    def test_exhausted_memory_ceiling_does_not_stop_library(self):
        embed._budget_take()
        with self.assertRaisesRegex(RuntimeError, "embed budget exhausted.*KHIPU_EMBED_DAILY_CALLS"):
            embed._budget_take()
        embed._budget_take(scope="library")
        self.assertEqual(embed.budget_status(scope="library")["calls"], 1)
        self.assertEqual(embed.budget_status()["calls"], 1)

    def test_zero_library_ceiling_is_unlimited_and_resets_by_day(self):
        with mock.patch.object(embed, "LIBRARY_DAILY_CALL_BUDGET", 0):
            for _ in range(3):
                embed._budget_take(scope="library")
            status = embed.budget_status(scope="library")
            self.assertTrue(status["unlimited"])
            self.assertFalse(status["exhausted"])
            self.assertEqual(status["calls"], 3)
            with mock.patch.object(embed, "_budget_today", return_value="1999-01-01"):
                self.assertEqual(embed.budget_status(scope="library")["calls"], 0)
        self.assertEqual(embed.budget_status()["calls"], 0)

    def test_embed_batch_threads_library_budget_to_every_provider_request(self):
        with mock.patch.object(embed, "_gemini_key", return_value="fake"), \
                mock.patch.object(embed, "_post_json", return_value={
                    "embeddings": [{"values": [1.] * embed.DIM}]
                }) as post:
            embed.embed_batch(["text"], budget="library")
        self.assertEqual(post.call_args.kwargs["budget"], "library")

    def test_post_json_charges_the_requested_scope_on_retries(self):
        transport = mock.Mock(side_effect=[urllib.error.URLError("retry"), b"{}"])
        with mock.patch.object(embed.time, "sleep"), \
                self.assertRaisesRegex(RuntimeError, "embed budget exhausted"):
            embed._post_json("unused", {}, {}, provider="local", retries=1,
                             timeout=1, delay=0, transport=transport, budget="library")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(embed.budget_status(scope="library")["calls"], 1)
        self.assertEqual(embed.budget_status()["calls"], 0)
