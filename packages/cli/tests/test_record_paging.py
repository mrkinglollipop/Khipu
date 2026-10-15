"""Record paging, phase 1: topic bodies page by UTF-8 byte offset on the hub
and the replica, and every page carries the topic's identity."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from khipu import activity, cli, hub_snapshot
from khipu import mcp_server as srv
from khipu.text_pages import (
    PAGE_BYTES_DEFAULT,
    PAGE_BYTES_MAX,
    PAGE_BYTES_MIN,
    fetch_bytes,
    page_args,
    page_utf8,
)

ASCII_BIG = "x" * (400_000 - len("THE_END")) + "THE_END"
CJK_BIG = "漢" * 44_998 + "末尾"
UPDATED = dt.datetime(2026, 10, 11, 9, 30, tzinfo=dt.timezone.utc)


def _page(raw: bytes, offset: int, max_bytes: int) -> dict:
    """What a database read hands page_utf8: the bounded slice from offset."""
    return page_utf8(raw[offset:offset + fetch_bytes(max_bytes)], offset, max_bytes, len(raw))


def _follow(fetch) -> tuple[str, list[dict]]:
    pages = [fetch(offset=0)]
    while not pages[-1]["complete"]:
        nxt = pages[-1]["body_next_offset"]
        assert nxt > pages[-1]["body_offset"], "paging made no progress"
        pages.append(fetch(offset=nxt))
    return "".join(p["body"] for p in pages), pages


class PageUtf8Test(unittest.TestCase):
    CHARS = {"ascii": "a", "two": "é", "three": "漢", "four": "😀"}

    def test_every_size_reassembles_without_splitting_a_character(self):
        for name, ch in self.CHARS.items():
            # A one-byte lead-in and a one-byte middle shift the multi-byte
            # characters so page ends land at every position inside them.
            body = "a" + ch * 7 + "b" + ch * 3
            raw = body.encode("utf-8")
            for max_bytes in range(PAGE_BYTES_MIN, 14):
                with self.subTest(char=name, max_bytes=max_bytes):
                    pages = [_page(raw, 0, max_bytes)]
                    while not pages[-1]["complete"]:
                        pages.append(_page(raw, pages[-1]["body_next_offset"], max_bytes))
                    self.assertEqual("".join(p["body"] for p in pages), body)
                    for p in pages:
                        self.assertTrue(p["body"])
                        self.assertNotIn("�", p["body"])
                        self.assertLessEqual(len(p["body"].encode("utf-8")), max_bytes)
                        self.assertEqual(p["body_total_bytes"], len(raw))
                    self.assertIsNone(pages[-1]["body_next_offset"])
                    for prev, nxt in zip(pages, pages[1:]):
                        self.assertEqual(prev["body_next_offset"], nxt["body_offset"])
                        self.assertFalse(prev["complete"])

    def test_an_offset_inside_a_character_starts_at_the_next_one(self):
        for name, ch in self.CHARS.items():
            raw = (ch * 5).encode("utf-8")
            width = len(ch.encode("utf-8"))
            for offset in range(len(raw) + 1):
                with self.subTest(char=name, offset=offset):
                    page = _page(raw, offset, 64)
                    boundary = -(-offset // width) * width
                    self.assertEqual(page["body"], raw[boundary:].decode("utf-8"))
                    self.assertEqual(page["body_offset"], boundary if boundary < len(raw) else offset)
                    self.assertTrue(page["complete"])

    def test_offset_past_the_end_is_an_empty_complete_page(self):
        raw = b"hello"
        for offset in (5, 6, 10_000):
            page = _page(raw, offset, 64)
            self.assertEqual(page["body"], "")
            self.assertTrue(page["complete"])
            self.assertIsNone(page["body_next_offset"])
            self.assertEqual(page["body_total_bytes"], 5)

    def test_an_empty_or_null_body_is_one_empty_complete_page(self):
        for chunk, total in ((b"", 0), (None, None)):
            page = page_utf8(chunk, 0, 64, total)
            self.assertEqual(page, {"body": "", "body_total_bytes": 0, "body_offset": 0,
                                    "body_next_offset": None, "complete": True})

    def test_a_body_of_exactly_one_page_is_complete(self):
        page = _page(("é" * 8).encode("utf-8"), 0, 16)
        self.assertEqual((page["body"], page["complete"], page["body_next_offset"]),
                         ("é" * 8, True, None))

    def test_page_args_defaults_clamps_and_refusals(self):
        self.assertEqual(page_args(), (0, PAGE_BYTES_DEFAULT))
        self.assertEqual(page_args("7", "100"), (7, 100))
        self.assertEqual(page_args(0, 1), (0, PAGE_BYTES_MIN))
        self.assertEqual(page_args(0, 10**9), (0, PAGE_BYTES_MAX))
        for bad in ((-1, None), ("x", None), (None, "big"), (True, None), (None, [1]),
                    (float("inf"), None), (float("nan"), None)):
            with self.subTest(args=bad), self.assertRaises(ValueError):
                page_args(*bad)


class _PgCursor:
    """Answers the topic query the way Postgres does: substring(bytea FROM
    start FOR count) with a 1-based start, octet_length of the whole body."""

    def __init__(self, topics: dict[str, str]):
        self.topics, self.statements, self.params = topics, [], []
        self.hashes: dict[str, str] = {}
        self._row = None

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params):
        self.statements.append(sql)
        self.params.append(params)
        start, count, slug = params
        body = self.topics.get(slug)
        if body is None:
            self._row = None
            return
        raw = body.encode("utf-8")
        self._row = (slug, slug.title(), raw[start - 1:start - 1 + count], len(raw),
                     "active", UPDATED, UPDATED, [], f"/{slug}.md",
                     self.hashes.get(slug, "hash-" + slug))

    def fetchone(self): return self._row


class _PgConn:
    def __init__(self, cur): self.cur = cur
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self.cur


def _replica(td: str, topics: dict[str, str]) -> Path:
    path = Path(td) / "replica.sqlite"
    with sqlite3.connect(path) as con:
        hub_snapshot._create_schema(con)
        for slug, body in topics.items():
            con.execute(
                "INSERT INTO topics(slug, title, body, status, created_at, updated_at, links,"
                " source_path, content_hash) VALUES (?, ?, ?, 'active', ?, ?, '[]', ?, ?)",
                (slug, slug.title(), body, UPDATED.isoformat(), UPDATED.isoformat(),
                 f"/{slug}.md", "hash-" + slug),
            )
    return path


class HubAndReplicaPagingTest(unittest.TestCase):
    TOPICS = {"big-ascii": ASCII_BIG, "big-cjk": CJK_BIG}

    def test_sizes_are_the_ones_the_scope_names(self):
        self.assertEqual(len(ASCII_BIG.encode("utf-8")), 400_000)
        self.assertEqual(len(CJK_BIG.encode("utf-8")), 135_000)

    def test_hub_pages_reassemble_the_whole_body_and_reads_are_bounded(self):
        cur = _PgCursor(self.TOPICS)
        with mock.patch.object(activity, "connect", return_value=_PgConn(cur)):
            for slug, body in self.TOPICS.items():
                with self.subTest(slug=slug):
                    text, pages = _follow(lambda offset: activity.topic_detail(slug, offset=offset))
                    self.assertEqual(text, body)
                    self.assertGreater(len(pages), 1)
                    self.assertTrue(pages[-1]["body"].endswith(body[-2:]))
                    for p in pages:
                        self.assertEqual((p["id"], p["slug"], p["title"]), (slug, slug, slug.title()))
                        self.assertEqual(p["content_hash"], "hash-" + slug)
                        self.assertEqual(p["updated_at"], UPDATED.isoformat())
                        self.assertEqual(p["body_total_bytes"], len(body.encode("utf-8")))
                        self.assertLessEqual(len(p["body"].encode("utf-8")), PAGE_BYTES_DEFAULT)
                        self.assertNotIn("body_truncated", p)
        self.assertIn("substring(", cur.statements[0])
        self.assertIn("convert_to(t.body, 'UTF8')", cur.statements[0])
        self.assertNotIn("LEFT(body", cur.statements[0])
        self.assertTrue(all(count == fetch_bytes(PAGE_BYTES_DEFAULT) for _, count, _ in cur.params))

    def test_replica_answers_exactly_as_the_hub_does(self):
        cur = _PgCursor(self.TOPICS)
        with tempfile.TemporaryDirectory() as td:
            path = _replica(td, self.TOPICS)
            with mock.patch.object(activity, "connect", return_value=_PgConn(cur)), \
                    mock.patch.object(hub_snapshot, "snapshot_path", return_value=path):
                for slug, body in self.TOPICS.items():
                    for max_bytes in (PAGE_BYTES_DEFAULT, 1000, 4097):
                        with self.subTest(slug=slug, max_bytes=max_bytes):
                            text, pages = _follow(lambda offset: hub_snapshot.topic_detail_snapshot(
                                slug, offset=offset, max_bytes=max_bytes))
                            self.assertEqual(text, body)
                            self.assertTrue(pages[-1]["body"].endswith(body[-2:]))
                            for p in pages:
                                hub = activity.topic_detail(slug, offset=p["body_offset"],
                                                            max_bytes=max_bytes)
                                # links is JSON text on the replica, a list on the hub;
                                # both decode to [] here, so the whole page must match.
                                self.assertEqual(p, hub)

    def test_replica_without_a_content_hash_column_still_pages(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.sqlite"
            with sqlite3.connect(path) as con:
                hub_snapshot._create_schema(con)
                con.execute("ALTER TABLE topics DROP COLUMN content_hash")
                con.execute("INSERT INTO topics(slug, body) VALUES ('old', 'é漢😀')")
            with mock.patch.object(hub_snapshot, "snapshot_path", return_value=path):
                page = hub_snapshot.topic_detail_snapshot("old", max_bytes=5)
        self.assertIsNone(page["content_hash"])
        self.assertEqual((page["body"], page["body_next_offset"]), ("é漢", 5))

    def test_a_huge_offset_is_an_empty_last_page_on_both_paths(self):
        # Postgres substring takes an int4 start and SQLite binds int64: an
        # offset past either must still be an empty, complete page.
        cur = _PgCursor({"t": "abc"})
        with tempfile.TemporaryDirectory() as td:
            path = _replica(td, {"t": "abc"})
            with mock.patch.object(activity, "connect", return_value=_PgConn(cur)), \
                    mock.patch.object(hub_snapshot, "snapshot_path", return_value=path):
                for offset in (2**31, 2**40, 2**64):
                    with self.subTest(offset=offset):
                        hub = activity.topic_detail("t", offset=offset)
                        rep = hub_snapshot.topic_detail_snapshot("t", offset=offset)
                        self.assertEqual(rep, hub)
                        self.assertEqual((hub["body"], hub["complete"], hub["body_total_bytes"]),
                                         ("", True, 3))
        self.assertTrue(all(start <= 2**30 + 1 for start, _, _ in cur.params))

    def test_a_null_replica_body_is_an_empty_complete_page(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "null.sqlite"
            with sqlite3.connect(path) as con:
                hub_snapshot._create_schema(con)
                con.execute("INSERT INTO topics(slug, body) VALUES ('nil', NULL)")
            with mock.patch.object(hub_snapshot, "snapshot_path", return_value=path):
                page = hub_snapshot.topic_detail_snapshot("nil")
        self.assertEqual((page["body"], page["body_total_bytes"], page["complete"]), ("", 0, True))


class KhipuGetToolTest(unittest.TestCase):
    TOPICS = HubAndReplicaPagingTest.TOPICS

    def _raw_call(self, args):
        return srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "khipu_get", "arguments": args}})

    def _call(self, args):
        out = self._raw_call(args)
        self.assertFalse(out["result"].get("isError"), out)
        return json.loads(out["result"]["content"][0]["text"])

    def test_khipu_get_pages_from_the_hub_and_from_the_replica(self):
        cur = _PgCursor(self.TOPICS)
        with tempfile.TemporaryDirectory() as td:
            path = _replica(td, self.TOPICS)
            for source in ("hub", "replica"):
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(hub_snapshot, "snapshot_path", return_value=path))
                    if source == "hub":
                        stack.enter_context(mock.patch.object(activity, "connect", return_value=_PgConn(cur)))
                    else:
                        stack.enter_context(mock.patch.object(
                            activity, "connect", side_effect=ConnectionError("connection refused")))
                        stack.enter_context(mock.patch.object(
                            hub_snapshot, "stale_fields", return_value={"stale": True}))
                    for slug, body in self.TOPICS.items():
                        with self.subTest(source=source, slug=slug):
                            text, pages = _follow(lambda offset: self._call(
                                {"id": slug, "kind": "topic", "offset": offset}))
                            self.assertEqual(text, body)
                            self.assertTrue(pages[-1]["body"].endswith(body[-2:]))
                            for p in pages:
                                self.assertEqual((p["kind"], p["id"], p["slug"]), ("topic", slug, slug))
                                self.assertEqual(p["content_hash"], "hash-" + slug)
                                for field in ("title", "updated_at", "body_total_bytes",
                                              "body_offset", "body_next_offset", "complete"):
                                    self.assertIn(field, p)
                                self.assertEqual(p.get("stale"), True if source == "replica" else None)

    def test_khipu_get_without_paging_arguments_returns_the_first_page(self):
        cur = _PgCursor({"small": "short body"})
        with mock.patch.object(activity, "connect", return_value=_PgConn(cur)):
            out = self._call({"id": "small"})
        self.assertEqual((out["body"], out["complete"]), ("short body", True))
        self.assertEqual(cur.params[0], (1, fetch_bytes(PAGE_BYTES_DEFAULT), "small"))

    def test_khipu_get_passes_max_bytes_and_refuses_a_negative_offset(self):
        cur = _PgCursor({"t": "abcdef"})
        with mock.patch.object(activity, "connect", return_value=_PgConn(cur)):
            out = self._call({"id": "t", "offset": 2, "max_bytes": 4})
            self.assertEqual((out["body"], out["body_next_offset"]), ("cdef", None))
            bad = self._raw_call({"id": "t", "offset": -1})
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("offset", bad["result"]["content"][0]["text"])

    def test_schema_documents_the_paging_arguments(self):
        tool = next(t for t in srv.TOOLS if t["name"] == "khipu_get")
        props = tool["inputSchema"]["properties"]
        self.assertEqual(props["offset"]["type"], "integer")
        self.assertEqual(props["max_bytes"]["maximum"], PAGE_BYTES_MAX)
        self.assertEqual(tool["inputSchema"]["required"], ["id"])
        for field in ("body_next_offset", "complete", "content_hash", "body_total_bytes"):
            self.assertIn(field, tool["description"])


class CliGetTest(unittest.TestCase):
    def _run(self, cur, **argv):
        args = SimpleNamespace(**{"kind": None, "offset": None, "max_bytes": None, "all": False, **argv})
        buf = io.BytesIO()
        # An ASCII-only stdout: --all must still write the body's UTF-8 bytes.
        out = io.TextIOWrapper(buf, encoding="ascii", errors="strict", write_through=True)
        err = io.StringIO()
        with mock.patch.object(activity, "connect", return_value=_PgConn(cur)), \
                mock.patch.object(cli.sys, "stdout", out), mock.patch.object(cli.sys, "stderr", err):
            code = cli.cmd_get(args)
        return code, buf.getvalue(), err.getvalue()

    def test_all_writes_the_whole_body_as_utf8_whatever_the_locale(self):
        for slug, body in HubAndReplicaPagingTest.TOPICS.items():
            with self.subTest(slug=slug):
                code, raw, _ = self._run(_PgCursor({slug: body}), id=slug, all=True, max_bytes=100_000)
                self.assertEqual((code, raw), (0, body.encode("utf-8")))

    def test_all_from_an_offset_writes_the_rest(self):
        code, raw, _ = self._run(_PgCursor({"t": "é漢😀tail"}), id="t", all=True, offset=2, max_bytes=4)
        self.assertEqual((code, raw), (0, "漢😀tail".encode("utf-8")))

    def test_one_page_prints_json_with_paging_fields(self):
        code, raw, _ = self._run(_PgCursor({"t": "abcdef"}), id="t", offset=1, max_bytes=4)
        out = json.loads(raw)
        self.assertEqual(code, 0)
        self.assertEqual((out["kind"], out["id"], out["body"], out["body_next_offset"], out["complete"]),
                         ("topic", "t", "bcde", 5, False))

    def test_all_stops_when_the_topic_changes_between_pages(self):
        cur = _PgCursor({"t": "abcdefgh"})
        real = cur.execute

        def edited_after_the_first_page(sql, params):
            if cur.params:
                cur.hashes["t"] = "hash-edited"
            real(sql, params)

        cur.execute = edited_after_the_first_page
        code, raw, err = self._run(cur, id="t", all=True, max_bytes=4)
        self.assertEqual(code, 1)
        self.assertEqual(raw, b"abcd")
        self.assertIn("changed while paging", err)

    def test_parser_takes_the_paging_flags(self):
        args = cli.build_parser().parse_args(
            ["get", "note:x", "--offset", "10", "--max-bytes", "500", "--all"])
        self.assertEqual((args.offset, args.max_bytes, args.all), (10, 500, True))


if __name__ == "__main__":
    unittest.main()
