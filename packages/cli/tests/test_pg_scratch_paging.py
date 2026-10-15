"""Topic paging against real Postgres: convert_to/substring on bytea must hand
back exactly the bytes the page function expects, or a page fails to decode.

Same guard as test_pg_scratch.py: KHIPU_LIVE_TESTS=1 and a nonce-scoped,
loopback KHIPU_SCRATCH_DSN whose ownership sentinel matches.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import pytest

from khipu import activity
from tests import connect_scratch, validate_scratch_dsn

SCRATCH_DSN = os.environ.get("KHIPU_SCRATCH_DSN")

if SCRATCH_DSN and os.environ.get("KHIPU_LIVE_TESTS") == "1":
    validate_scratch_dsn(SCRATCH_DSN)

pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN or os.environ.get("KHIPU_LIVE_TESTS") != "1",
    reason="requires KHIPU_LIVE_TESTS=1 and a provisioned nonce-scoped KHIPU_SCRATCH_DSN",
)

BODIES = {
    "scratch-paging-ascii": "x" * (400_000 - len("THE_END")) + "THE_END",
    "scratch-paging-cjk": "漢" * 44_998 + "末尾",
    "scratch-paging-mixed": ("aé漢😀" * 3000) + "end",
    "scratch-paging-empty": "",
}


class TopicPagingScratchTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect_scratch(SCRATCH_DSN)
        with self.conn.cursor() as cur:
            # The core schema's topics table, in case the scratch database
            # was provisioned without it; a no-op when it exists.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    slug TEXT PRIMARY KEY, title TEXT, body TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    links JSONB NOT NULL DEFAULT '[]'::jsonb,
                    frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
                    source_path TEXT, content_hash TEXT
                )
                """
            )
            cur.execute("ALTER TABLE topics ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
            for slug, body in BODIES.items():
                cur.execute(
                    "INSERT INTO topics (slug, title, body, content_hash) VALUES (%s, %s, %s, %s)",
                    (slug, slug, body, "h-" + slug),
                )
        self.conn.commit()

    def tearDown(self):
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM topics WHERE slug = ANY(%s)", (list(BODIES),))
        self.conn.commit()
        self.conn.close()

    def _connect(self):
        return connect_scratch(SCRATCH_DSN)

    def test_pages_follow_to_the_whole_body_at_every_size(self):
        with mock.patch.object(activity, "connect", side_effect=self._connect):
            for slug, body in BODIES.items():
                for max_bytes in (4, 5, 6, 7, 1000, 65536, 524288):
                    if max_bytes < 1000 and len(body.encode("utf-8")) > 5000:
                        continue
                    with self.subTest(slug=slug, max_bytes=max_bytes):
                        offset, parts = 0, []
                        while True:
                            page = activity.topic_detail(slug, offset=offset, max_bytes=max_bytes)
                            self.assertNotIn("�", page["body"])
                            self.assertLessEqual(len(page["body"].encode("utf-8")), max_bytes)
                            self.assertEqual(page["body_total_bytes"], len(body.encode("utf-8")))
                            self.assertEqual(page["content_hash"], "h-" + slug)
                            parts.append(page["body"])
                            if page["complete"]:
                                break
                            self.assertGreater(page["body_next_offset"], offset)
                            offset = page["body_next_offset"]
                        self.assertEqual("".join(parts), body)

    def test_small_bodies_at_every_offset_and_size(self):
        body = "aé漢😀b😀漢éa"
        raw = body.encode("utf-8")
        with mock.patch.object(activity, "connect", side_effect=self._connect):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE topics SET body = %s WHERE slug = 'scratch-paging-empty'", (body,))
            self.conn.commit()
            for max_bytes in range(4, 9):
                for offset in range(len(raw) + 2):
                    with self.subTest(max_bytes=max_bytes, offset=offset):
                        page = activity.topic_detail("scratch-paging-empty", offset=offset,
                                                     max_bytes=max_bytes)
                        start = page["body_offset"]
                        end = page["body_next_offset"] or len(raw)
                        if offset >= len(raw):
                            self.assertEqual((page["body"], page["complete"]), ("", True))
                        else:
                            self.assertEqual(page["body"], raw[start:end].decode("utf-8"))

    def test_an_offset_past_int4_is_an_empty_last_page(self):
        with mock.patch.object(activity, "connect", side_effect=self._connect):
            for offset in (2**31, 2**40, 2**64):
                with self.subTest(offset=offset):
                    page = activity.topic_detail("scratch-paging-mixed", offset=offset)
                    self.assertEqual((page["body"], page["complete"]), ("", True))

    def test_a_tombstoned_topic_is_not_found(self):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE topics SET deleted_at = now() WHERE slug = 'scratch-paging-cjk'")
        self.conn.commit()
        with mock.patch.object(activity, "connect", side_effect=self._connect):
            self.assertIsNone(activity.topic_detail("scratch-paging-cjk"))
