# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# Sonnet build agent (brief: do not delegate); no further agent to route to.
"""khipu.library and `khipu library ...` (library sources + BYO embeddings,
Session B). An in-memory fake hub stands in for Postgres (it answers exactly
the statements library.py issues), a fake transport stands in for the
embedding provider, files live in a temp folder. No database, no network."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from khipu import cli, db, embed, jobs, library, profiles
from khipu.profiles import ProfileSpec

TINY = ProfileSpec("voyage-t@4", "voyage", "voyage-t", 4)


def _md5(t: str) -> str:
    return hashlib.md5(t.encode()).hexdigest()


class FakeHub:
    """Connection + cursor over dicts. Dispatches on the statement text."""

    def __init__(self, *, profile_rows=None):
        self.profile_rows = dict(profile_rows or {
            TINY.id: ("voyage", "voyage-t", 4, "l2", None),
        })
        self.sources: dict[str, dict] = {}
        self.docs: dict[int, dict] = {}
        self.chunks: dict[tuple[int, int], tuple[str, str]] = {}
        self.embs: dict[tuple[str, int, int], tuple[str, str | None]] = {}
        self.indexes: set[str] = set()
        self.next_doc = 1
        self.commits = 0
        self.executed: list[tuple[str, tuple]] = []
        self.library_tables = True

    # connection surface
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    # model helpers
    def source_docs(self, source):
        return [i for i, d in self.docs.items() if d["source"] == source]

    def state(self, source, profile):
        """[(doc, idx, text, hash, emb or None)] for the source's chunks."""
        out = []
        for doc in sorted(self.source_docs(source)):
            for (d, idx), (text, h) in sorted(self.chunks.items()):
                if d == doc:
                    out.append((doc, idx, text, h, self.embs.get((profile, doc, idx))))
        return out

    def drop_chunks(self, doc, keep=None):
        for k in [k for k in self.chunks if k[0] == doc and (keep is None or k[1] not in keep)]:
            del self.chunks[k]
            for ek in [e for e in self.embs if e[1:] == k]:
                del self.embs[ek]


class FakeCursor:
    def __init__(self, hub: FakeHub):
        self.h = hub
        self._r: list = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return list(self._r)

    def executemany(self, sql, rows):
        for r in rows:
            self.execute(sql, r)

    def execute(self, sql, params=()):  # noqa: C901 - a dispatch table
        h = self.h
        s = " ".join(sql.split())
        p = tuple(params)
        h.executed.append((s, p))
        self._r = []
        if "information_schema.columns" in s:
            cols = ["id", "provider", "model", "dim", "normalize", "is_active", "note", "endpoint"]
            self._r = [(c,) for c in cols]
        elif "FROM embedding_profiles WHERE id" in s:
            r = h.profile_rows.get(p[0])
            self._r = [(p[0], *r)] if r else []
        elif s.startswith("INSERT INTO embedding_profiles"):
            pid, provider, model, dim, norm = p[:5]
            h.profile_rows[pid] = (provider, model, dim, norm, p[6] if len(p) > 6 else None)
        elif s.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO")):
            pass
        elif s.startswith("SELECT 1 FROM pg_indexes"):
            self._r = [(1,)] if p[0] in h.indexes else []
        elif s.startswith("SELECT indexname FROM pg_indexes"):
            self._r = [(n,) for n in sorted(h.indexes)]
        elif s.startswith("CREATE INDEX IF NOT EXISTS"):
            h.indexes.add(s.split()[5])
        elif s.startswith("DROP INDEX IF EXISTS"):
            h.indexes.discard(s.split()[-1].split(".")[-1])
        elif s.startswith("SELECT to_regclass"):
            self._r = [(h.library_tables,)]
        elif s.startswith("SELECT 1 FROM library_sources"):
            self._r = [(1,)] if p[0] in h.sources else []
        elif s.startswith("INSERT INTO library_sources"):
            h.sources[p[0]] = {"root": p[1], "profile": p[2], "enabled": True}
        elif s.startswith("UPDATE library_sources SET enabled"):
            h.sources[p[1]]["enabled"] = p[0]
        elif s.startswith("UPDATE library_sources SET profile"):
            h.sources[p[1]]["profile"] = p[0]
        elif s.startswith("DELETE FROM library_sources"):
            h.sources.pop(p[0], None)
            for doc in h.source_docs(p[0]):
                h.drop_chunks(doc)
                del h.docs[doc]
        elif s.startswith("SELECT name, root, profile, enabled FROM library_sources"):
            rows = sorted(h.sources.items())
            if "WHERE name" in s:
                rows = [(n, v) for n, v in rows if n == p[0]]
            elif "WHERE enabled" in s:
                rows = [(n, v) for n, v in rows if v["enabled"]]
            self._r = [(n, v["root"], v["profile"], v["enabled"]) for n, v in rows]
        elif s.startswith("SELECT COUNT(DISTINCT d.id)"):
            profile, source = p
            st = h.state(source, profile)
            emb = [x for x in st if x[4] is not None]
            stale = [x for x in emb if x[4][1] is not None and x[4][1] != x[3]]
            self._r = [(len(h.source_docs(source)), len(st), len(emb), len(stale))]
        elif s.startswith("SELECT id, rel_path, content_hash FROM library_documents"):
            self._r = [(i, d["rel_path"], d["content_hash"]) for i, d in h.docs.items()
                       if d["source"] == p[0]]
        elif s.startswith("INSERT INTO library_documents"):
            source, rel, title, author, tags, size, _mtime, digest = p
            doc = next((i for i, d in h.docs.items()
                        if d["source"] == source and d["rel_path"] == rel), None)
            if doc is None:
                doc = h.next_doc
                h.next_doc += 1
            h.docs[doc] = {"source": source, "rel_path": rel, "title": title,
                           "author": author, "tags": list(tags), "bytes": size,
                           "content_hash": digest}
            self._r = [(doc,)]
        elif s.startswith("DELETE FROM library_chunks WHERE document = %s AND NOT"):
            h.drop_chunks(p[0], keep=set(p[1]))
        elif s.startswith("DELETE FROM library_chunks WHERE document"):
            h.drop_chunks(p[0])
        elif s.startswith("INSERT INTO library_chunks"):
            h.chunks[(p[0], p[1])] = (p[2], p[3])
        elif s.startswith("INSERT INTO library_embeddings"):
            h.embs[(p[0], p[1], p[2])] = (p[3], p[4])
        elif s.startswith("DELETE FROM library_documents WHERE id"):
            for doc in p[0]:
                h.drop_chunks(doc)
                h.docs.pop(doc, None)
        elif s.startswith("UPDATE library_documents SET scanned_at"):
            pass
        elif s.startswith(("SELECT MAX(scanned_at)", "SELECT MAX(e.built_at)")):
            self._r = [(None,)]
        elif s.startswith(("SELECT DISTINCT d.rel_path", "SELECT c.document, c.chunk_idx",
                           "SELECT COUNT(*) FROM library_chunks")):
            self._chunk_query(s, p)
        else:
            raise AssertionError(f"FakeHub got an unexpected statement: {s}")

    def _chunk_query(self, s, p):
        h = self.h
        profile, source = p[0], p[1]
        rows = []
        for doc, idx, text, hsh, emb in h.state(source, profile):
            is_stale = emb is not None and emb[1] is not None and emb[1] != hsh
            if "e.document IS NULL OR" in s:
                keep = emb is None or is_stale
            elif "e.document IS NULL" in s:
                keep = emb is None
            else:
                keep = is_stale
            if keep:
                rows.append((doc, idx, text, hsh, h.docs[doc]["title"]))
        if s.startswith("SELECT COUNT(*)"):
            self._r = [(len(rows),)]
        elif s.startswith("SELECT DISTINCT"):
            self._r = [(r,) for r in sorted({h.docs[x[0]]["rel_path"] for x in rows})[: p[2]]]
        else:
            last = (p[2], p[3])
            rows = [r for r in rows if (r[0], r[1]) > last][: p[4]]
            self._r = rows


def _voyage_transport(calls=None, *, fail_on=None, dim=4):
    """Replays Voyage-shaped answers sized to each request."""
    n = {"i": 0}

    def transport(url, data, headers, timeout):
        n["i"] += 1
        body = json.loads(data.decode())
        if calls is not None:
            calls.append(body)
        if fail_on and n["i"] in fail_on:
            raise urllib.error.HTTPError("https://x", 400, "bad", {}, io.BytesIO(b"nope"))
        k = len(body["input"])
        vecs = [[float(j == i % dim) for j in range(dim)] for i in range(k)]
        items = [{"object": "embedding", "embedding": v, "index": i} for i, v in enumerate(vecs)]
        return json.dumps({"data": items}).encode()

    return transport


class _Base(unittest.TestCase):
    def setUp(self):
        db._TABLE_COLUMNS_CACHE.clear()
        profiles.clear_learned()
        self.addCleanup(profiles.clear_learned)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "lib"
        self.root.mkdir()
        self.hub = FakeHub()
        for patch in (
            mock.patch.object(library.time, "sleep"),
            mock.patch.object(embed, "_budget_take"),
            mock.patch.object(embed, "_voyage_key", return_value="vk-secret"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, rel, text, *, raw=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw if raw is not None else text.encode())
        return p

    def add(self, name="lib1", profile=TINY.id):
        return library.add_source(self.hub, name, str(self.root), profile)

    def docs_by_rel(self):
        return {d["rel_path"]: d for d in self.hub.docs.values()}


class RegistryTest(_Base):
    def test_add_validates_name_root_and_profile(self):
        for bad in ("Bad Name", "", "x" * 41, "UP", "a/b"):
            with self.subTest(name=bad), self.assertRaises(library.LibraryError):
                library.add_source(self.hub, bad, str(self.root), TINY.id)
        with self.assertRaises(library.LibraryError):
            library.add_source(self.hub, "ok", str(self.root / "nope"), TINY.id)
        f = self.write("a.txt", "x")
        with self.assertRaises(library.LibraryError):
            library.add_source(self.hub, "ok", str(f), TINY.id)
        with self.assertRaises(library.LibraryError) as ctx:
            library.add_source(self.hub, "ok", str(self.root), "ghost@9")
        self.assertIn("unknown embedding profile", str(ctx.exception))
        self.assertEqual(self.hub.sources, {})

    def test_add_then_duplicate_refused(self):
        out = self.add()
        self.assertEqual((out["ok"], out["name"], out["profile"]), (True, "lib1", TINY.id))
        with self.assertRaises(library.LibraryError):
            self.add()

    def test_list_status_enable_disable_remove(self):
        self.add()
        self.write("Heiser/Unseen.txt", "hello world")
        library.scan(self.hub, "lib1")
        rows = library.list_sources(self.hub)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            set(rows[0]),
            {"name", "root", "profile", "enabled", "documents", "chunks", "embedded",
             "missing", "stale", "pct"},
        )
        self.assertEqual((rows[0]["documents"], rows[0]["chunks"], rows[0]["missing"]), (1, 1, 1))
        st = library.source_status(self.hub, "lib1")
        self.assertEqual(st["sample_missing"], ["Heiser/Unseen.txt"])
        self.assertEqual(st["sample_stale"], [])
        self.assertIn("last_scan", st)
        self.assertIn("last_backfill", st)
        self.assertFalse(library.set_enabled(self.hub, "lib1", False)["enabled"])
        self.assertFalse(self.hub.sources["lib1"]["enabled"])
        library.set_enabled(self.hub, "lib1", True)
        with self.assertRaises(library.LibraryError) as ctx:
            library.remove_source(self.hub, "lib1", yes=False)
        self.assertIn("--yes", str(ctx.exception))
        self.assertIn("lib1", self.hub.sources)
        out = library.remove_source(self.hub, "lib1", yes=True)
        self.assertEqual((out["documents"], out["chunks"]), (1, 1))
        self.assertEqual((self.hub.sources, self.hub.docs, self.hub.chunks), ({}, {}, {}))

    def test_set_profile_refuses_until_full_coverage_then_moves_the_pointer(self):
        other = ProfileSpec("voyage-u@4", "voyage", "voyage-u", 4)
        self.hub.profile_rows[other.id] = ("voyage", "voyage-u", 4, "l2", None)
        self.add()
        self.write("A/x.txt", "some text")
        self.write("A/y.txt", "other text")
        library.scan(self.hub, "lib1")
        with self.assertRaises(library.LibraryError) as ctx:
            library.set_profile(self.hub, "lib1", other.id)
        self.assertIn("2 of 2 chunks still have no vector", str(ctx.exception))
        self.assertEqual(self.hub.sources["lib1"]["profile"], TINY.id)
        # partly embedded is still refused, with the remaining count
        doc = sorted(self.hub.source_docs("lib1"))[0]
        self.hub.embs[(other.id, doc, 0)] = ("[0,0,0,1]", self.hub.chunks[(doc, 0)][1])
        with self.assertRaises(library.LibraryError) as ctx:
            library.set_profile(self.hub, "lib1", other.id)
        self.assertIn("1 of 2 chunks", str(ctx.exception))
        self.assertEqual(self.hub.sources["lib1"]["profile"], TINY.id)
        # a stale vector does not count as covered
        doc2 = sorted(self.hub.source_docs("lib1"))[1]
        self.hub.embs[(other.id, doc2, 0)] = ("[0,0,0,1]", "old-hash")
        with self.assertRaises(library.LibraryError) as ctx:
            library.set_profile(self.hub, "lib1", other.id)
        self.assertIn("out of date", str(ctx.exception))
        # complete: the pointer moves and the index is ensured first
        self.hub.embs[(other.id, doc2, 0)] = ("[0,0,0,1]", self.hub.chunks[(doc2, 0)][1])
        out = library.set_profile(self.hub, "lib1", other.id)
        self.assertEqual((out["ok"], out["profile"], out["previous_profile"]), (True, other.id, TINY.id))
        self.assertEqual(self.hub.sources["lib1"]["profile"], other.id)
        self.assertTrue(any(i.startswith("idx_library_hnsw_voyage_u") for i in self.hub.indexes))

    def test_set_profile_refuses_unknown_profile_and_library(self):
        self.add()
        with self.assertRaises(library.LibraryError) as ctx:
            library.set_profile(self.hub, "lib1", "ghost@9")
        self.assertIn("unknown embedding profile", str(ctx.exception))
        with self.assertRaises(library.LibraryError):
            library.set_profile(self.hub, "ghost", TINY.id)

    def test_unknown_library_is_refused(self):
        for fn in (library.source_status, library.scan, library.backfill):
            with self.subTest(fn=fn.__name__), self.assertRaises(library.LibraryError):
                fn(self.hub, "ghost")


class FrontMatterTest(unittest.TestCase):
    def test_keys_lists_and_quotes(self):
        text = ('---\ntitle: "The Unseen Realm"\nauthor: Michael Heiser\n'
                'tags: [divine council, Psalm 82]\n---\nBody here')
        fm, body = library.parse_front_matter(text)
        self.assertEqual(fm, {"title": "The Unseen Realm", "author": "Michael Heiser",
                              "tags": ["divine council", "Psalm 82"]})
        self.assertEqual(body, "Body here")

    def test_topic_tags_dash_list_and_comma_forms(self):
        fm, _ = library.parse_front_matter("---\ntopic_tags:\n  - a\n  - b\n---\nx")
        self.assertEqual(fm["tags"], ["a", "b"])
        fm, _ = library.parse_front_matter("---\ntags: a, b\n---\nx")
        self.assertEqual(fm["tags"], ["a", "b"])

    def test_no_front_matter_unclosed_and_empty(self):
        for text in ("plain", "---\ntitle: x\nno close", ""):
            self.assertEqual(library.parse_front_matter(text), ({}, text))
        self.assertEqual(library.parse_front_matter("---\n---\nbody"), ({}, "body"))

    def test_path_fallback(self):
        self.assertEqual(library.document_metadata("x", "Heiser/Unseen Realm.txt")[:3],
                         ("Unseen Realm", "Heiser", []))
        self.assertEqual(library.document_metadata("x", "loose.md")[:3], ("loose", None, []))
        # front matter wins, partially
        t, a, tags, body = library.document_metadata("---\ntitle: T\n---\nb", "Auth/f.txt")
        self.assertEqual((t, a, tags, body), ("T", "Auth", [], "b"))


class ScanTest(_Base):
    def setUp(self):
        super().setUp()
        self.add()

    def test_walk_front_matter_fallback_and_filters(self):
        self.write("Heiser/Unseen.txt", "---\ntitle: Unseen Realm\nauthor: Michael Heiser\n"
                   "tags: [council]\n---\nThe divine council text.")
        self.write("Lewis/Mere Christianity.md", "Plain prose, no front matter.")
        self.write("top.txt", "loose file")
        self.write("notes.pdf", "ignored")
        self.write(".hidden.txt", "ignored")
        self.write(".git/x.txt", "ignored")
        self.write("Heiser/.draft.md", "ignored")
        out = library.scan(self.hub, "lib1")
        self.assertEqual((out["added"], out["updated"], out["removed"], out["unchanged"]), (3, 0, 0, 0))
        self.assertEqual(out["documents"], 3)
        self.assertEqual(out["skipped"], 0)
        docs = self.docs_by_rel()
        self.assertEqual(set(docs), {"Heiser/Unseen.txt", "Lewis/Mere Christianity.md", "top.txt"})
        u = docs["Heiser/Unseen.txt"]
        self.assertEqual((u["title"], u["author"], u["tags"]), ("Unseen Realm", "Michael Heiser", ["council"]))
        m = docs["Lewis/Mere Christianity.md"]
        self.assertEqual((m["title"], m["author"], m["tags"]), ("Mere Christianity", "Lewis", []))
        self.assertIsNone(docs["top.txt"]["author"])
        # front matter is stripped from the chunk text; hashes are md5 of the chunk
        texts = {text for text, _ in self.hub.chunks.values()}
        self.assertIn("The divine council text.", texts)
        self.assertFalse(any("title:" in t for t in texts))
        for text, hsh in self.hub.chunks.values():
            self.assertEqual(hsh, _md5(text))
        self.assertEqual(out["chunks"], len(self.hub.chunks))

    def test_unchanged_hash_is_skipped_without_writes(self):
        self.write("A/one.txt", "one")
        self.write("A/two.txt", "two")
        library.scan(self.hub, "lib1")
        before = len(self.hub.executed)
        out = library.scan(self.hub, "lib1")
        self.assertEqual((out["added"], out["updated"], out["unchanged"]), (0, 0, 2))
        new = [s for s, _ in self.hub.executed[before:]]
        self.assertFalse(any(s.startswith(("INSERT INTO library_documents", "INSERT INTO library_chunks",
                                           "DELETE FROM library_chunks")) for s in new))

    def test_changed_file_is_rechunked_and_its_vectors_dropped(self):
        self.write("A/one.txt", "first version")
        self.write("A/two.txt", "stays")
        library.scan(self.hub, "lib1")
        (doc_one,) = [i for i, d in self.hub.docs.items() if d["rel_path"] == "A/one.txt"]
        (doc_two,) = [i for i, d in self.hub.docs.items() if d["rel_path"] == "A/two.txt"]
        for doc in (doc_one, doc_two):
            self.hub.embs[(TINY.id, doc, 0)] = ("[1,0,0,0]", self.hub.chunks[(doc, 0)][1])
        self.write("A/one.txt", "second version, longer")
        out = library.scan(self.hub, "lib1")
        self.assertEqual((out["updated"], out["unchanged"], out["added"]), (1, 1, 0))
        self.assertEqual(self.hub.chunks[(doc_one, 0)][0], "second version, longer")
        self.assertNotIn((TINY.id, doc_one, 0), self.hub.embs)
        self.assertIn((TINY.id, doc_two, 0), self.hub.embs)

    def test_vanished_file_is_deleted_with_its_chunks(self):
        gone = self.write("A/gone.txt", "bye")
        self.write("A/kept.txt", "stay")
        library.scan(self.hub, "lib1")
        gone.unlink()
        out = library.scan(self.hub, "lib1")
        self.assertEqual((out["removed"], out["documents"]), (1, 1))
        self.assertEqual(set(self.docs_by_rel()), {"A/kept.txt"})
        self.assertEqual({k[0] for k in self.hub.chunks}, set(self.hub.docs))

    def test_a_root_with_no_files_does_not_wipe_the_index(self):
        f = self.write("A/only.txt", "x")
        library.scan(self.hub, "lib1")
        f.unlink()
        with self.assertRaises(library.LibraryError) as ctx:
            library.scan(self.hub, "lib1")
        self.assertIn("refusing", str(ctx.exception))
        self.assertEqual(len(self.hub.docs), 1)

    def test_missing_root_refuses_and_changes_nothing(self):
        self.write("A/x.txt", "x")
        library.scan(self.hub, "lib1")
        self.hub.sources["lib1"]["root"] = str(self.root / "unmounted")
        with self.assertRaises(library.LibraryError):
            library.scan(self.hub, "lib1")
        self.assertEqual(len(self.hub.docs), 1)

    def test_symlink_leaving_the_root_and_oversized_files_are_skipped(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret")
        self.write("A/ok.txt", "fine")
        os.symlink(outside, self.root / "A" / "link.txt")
        inside = self.write("A/target.txt", "inside target")
        os.symlink(inside, self.root / "A" / "ok-link.txt")
        self.write("A/big.txt", "x" * 50)
        with mock.patch.object(library, "MAX_FILE_BYTES", 40):
            out = library.scan(self.hub, "lib1")
        self.assertEqual(out["skipped_detail"], {"symlink_outside_root": 1, "too_large": 1})
        self.assertEqual(out["skipped"], 2)
        self.assertEqual(set(self.docs_by_rel()), {"A/ok.txt", "A/target.txt", "A/ok-link.txt"})

    def test_undecodable_bytes_are_replaced_and_nul_is_stripped(self):
        self.write("A/bad.txt", "", raw=b"caf\xe9 and\x00 nul")
        library.scan(self.hub, "lib1")
        (text, _), = self.hub.chunks.values()
        self.assertIn("caf�", text)
        self.assertNotIn("\x00", text)

    def test_long_file_gets_several_windows(self):
        self.write("A/long.txt", "word " * 3000)
        out = library.scan(self.hub, "lib1")
        self.assertGreater(out["chunks"], 1)


class BackfillTest(_Base):
    def setUp(self):
        super().setUp()
        self.add()
        for i in range(7):
            self.write(f"A/doc{i}.txt", f"document number {i}")
        library.scan(self.hub, "lib1")
        for patch in (mock.patch.object(embed, "BATCH", 3),):
            patch.start()
            self.addCleanup(patch.stop)

    def run_backfill(self, transport, **kw):
        embed.set_transport(transport)
        self.addCleanup(embed.set_transport, None)
        return library.backfill(self.hub, "lib1", **kw)

    def test_a_small_backfill_leaves_an_existing_index_alone(self):
        name = profiles.profile_index_name(TINY.id, "library_embeddings")
        self.hub.indexes.add(name)
        self.run_backfill(_voyage_transport())
        self.assertFalse([s for s, _ in self.hub.executed if s.startswith("DROP INDEX")])
        self.assertIn(name, self.hub.indexes)

    def test_a_big_backfill_drops_the_index_first_and_rebuilds_it_at_the_end(self):
        name = profiles.profile_index_name(TINY.id, "library_embeddings")
        self.hub.indexes.add(name)
        with mock.patch.object(profiles, "BULK_DROP_ROWS", 3), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = self.run_backfill(_voyage_transport())
        self.assertEqual(out["embedded"], 7)
        kinds = []
        for stmt, _ in self.hub.executed:
            if stmt.startswith("DROP INDEX"):
                kinds.append("drop")
            elif stmt.startswith("INSERT INTO library_embeddings") and "insert" not in kinds:
                kinds.append("insert")
            elif stmt.startswith("CREATE INDEX"):
                kinds.append("create")
        self.assertEqual(kinds, ["drop", "insert", "create"])
        self.assertIn(name, self.hub.indexes)
        self.assertIn("before loading 7 rows", err.getvalue())

    def test_batches_of_the_batch_size_document_input_type_and_receipt(self):
        calls: list = []
        out = self.run_backfill(_voyage_transport(calls))
        self.assertEqual([len(c["input"]) for c in calls], [3, 3, 1])
        self.assertTrue(all(c["input_type"] == "document" for c in calls))
        self.assertEqual((out["embedded"], out["failed"], out["remaining"], out["profile"]),
                         (7, 0, 0, TINY.id))
        self.assertEqual(len(self.hub.embs), 7)
        for (profile, doc, idx), (_vec, hsh) in self.hub.embs.items():
            self.assertEqual(profile, TINY.id)
            self.assertEqual(hsh, self.hub.chunks[(doc, idx)][1])
        idx_ddl = [s for s, _ in self.hub.executed if s.startswith("CREATE INDEX")]
        self.assertEqual(len(idx_ddl), 1)
        self.assertIn("ON library_embeddings", idx_ddl[0])
        # idempotent: nothing left to embed, no model call
        calls.clear()
        again = self.run_backfill(_voyage_transport(calls))
        self.assertEqual((again["embedded"], again["remaining"], calls), (0, 0, []))

    def test_a_failed_batch_is_isolated_and_counted(self):
        out = self.run_backfill(_voyage_transport(fail_on={2}))
        self.assertEqual((out["embedded"], out["failed"], out["remaining"]), (4, 3, 3))
        self.assertTrue(out["ok"])

    def test_budget_exhaustion_stops_the_run_but_keeps_committed_batches(self):
        n = {"i": 0}

        def take():
            n["i"] += 1
            if n["i"] == 2:
                raise RuntimeError("embed budget exhausted: 10000 calls today")

        with mock.patch.object(embed, "_budget_take", side_effect=take):
            out = self.run_backfill(_voyage_transport())
        self.assertTrue(out["budget_exhausted"])
        self.assertEqual((out["embedded"], out["failed"], out["remaining"]), (3, 0, 4))

    def test_wrong_width_vectors_are_never_stored(self):
        out = self.run_backfill(_voyage_transport(dim=5))
        self.assertEqual((out["embedded"], out["failed"]), (0, 7))
        self.assertEqual(self.hub.embs, {})
        self.assertEqual([s for s, _ in self.hub.executed if s.startswith("CREATE INDEX")], [])

    def test_limit_caps_the_run(self):
        out = self.run_backfill(_voyage_transport(), limit=4)
        self.assertEqual((out["embedded"], out["remaining"]), (4, 3))

    def test_stale_chunks_are_reembedded_only_with_the_flag(self):
        self.run_backfill(_voyage_transport())
        key = next(iter(self.hub.embs))
        vec, _h = self.hub.embs[key]
        self.hub.embs[key] = (vec, "hash-of-an-older-text")
        st = library.list_sources(self.hub)[0]
        self.assertEqual((st["embedded"], st["stale"], st["missing"]), (7, 1, 0))
        plain = self.run_backfill(_voyage_transport())
        self.assertEqual((plain["embedded"], plain["remaining"]), (0, 0))
        with_stale = self.run_backfill(_voyage_transport(), stale=True)
        self.assertEqual(with_stale["embedded"], 1)
        self.assertEqual(self.hub.embs[key][1], self.hub.chunks[(key[1], key[2])][1])
        self.assertEqual(library.list_sources(self.hub)[0]["stale"], 0)

    def test_a_null_embedded_hash_is_never_stale(self):
        self.run_backfill(_voyage_transport())
        key = next(iter(self.hub.embs))
        self.hub.embs[key] = (self.hub.embs[key][0], None)
        self.assertEqual(library.list_sources(self.hub)[0]["stale"], 0)

    def test_gemini_v2_profile_uses_the_task_prefix(self):
        self.hub.profile_rows[embed.PROFILE_2] = ("gemini", embed.MODEL_2, 768, "l2", None)
        self.hub.sources["lib1"]["profile"] = embed.PROFILE_2
        seen: list = []
        with mock.patch.object(embed, "embed_batch", side_effect=lambda texts, **kw: (
                seen.append((texts, kw)) or [[0.0] * 768 for _ in texts])):
            library.backfill(self.hub, "lib1", limit=1)
        texts, kw = seen[0]
        self.assertTrue(texts[0].startswith("title: doc0 | text: "))
        self.assertEqual(kw["input_type"], "document")


def _make_index(path: Path, rows, *, table=True):
    con = sqlite3.connect(path)
    if table:
        con.execute("CREATE TABLE embeddings (node_id TEXT, chunk_idx INTEGER, source_file TEXT,"
                    " chunk_text TEXT, embedding BLOB, model TEXT, dims INTEGER)")
        con.executemany("INSERT INTO embeddings VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def _blob(*vals):
    return struct.pack(f"<{len(vals)}f", *vals)


class ImportTest(_Base):
    def setUp(self):
        super().setUp()
        self.add()
        self.write("Heiser/Unseen.txt", "alpha beta")
        self.write("Lewis/Mere.md", "gamma delta")
        self.idx = Path(self.tmp.name) / "graph.sqlite"
        self.rows = [
            ("author:Heiser", 0, "corpus/Heiser/Unseen.txt", "first chunk", _blob(1, 0, 0, 0), "voyage-t", 4),
            ("author:Heiser", 1, "corpus/Heiser/Unseen.txt", "second chunk", _blob(0, 1, 0, 0), "voyage-t", 4),
            ("author:Lewis", 0, "corpus/Lewis/Mere.md", "lewis chunk", _blob(0, 0, 1, 0), "voyage-t", 4),
            ("author:Lewis", 1, "corpus/Lewis/Mere.md", "\x01\x02\x03\x04garbage", _blob(0, 0, 0, 1), "voyage-t", 4),
            ("author:X", 0, "corpus/Ghost/Gone.txt", "no such file", _blob(1, 1, 0, 0), "voyage-t", 4),
            ("author:Lewis", 2, "corpus/Lewis/Mere.md", "short vector", _blob(1, 2, 3), "voyage-t", 4),
        ]
        _make_index(self.idx, self.rows)

    def run_import(self, **kw):
        kw.setdefault("strip_prefix", "corpus/")
        return library.import_index(self.hub, "lib1", str(self.idx), **kw)

    def test_receipt_counts_and_what_was_written(self):
        out = self.run_import()
        self.assertEqual(
            {k: out[k] for k in ("read", "imported", "skipped_corrupt", "unmapped",
                                 "skipped_bad_vector", "documents", "profile")},
            {"read": 6, "imported": 3, "skipped_corrupt": 1, "unmapped": 1,
             "skipped_bad_vector": 1, "documents": 2, "profile": TINY.id})
        self.assertEqual(set(self.docs_by_rel()), {"Heiser/Unseen.txt", "Lewis/Mere.md"})
        texts = sorted(t for t, _ in self.hub.chunks.values())
        self.assertEqual(texts, ["first chunk", "lewis chunk", "second chunk"])
        self.assertEqual(len(self.hub.embs), 3)
        for (profile, doc, idx), (vec, hsh) in self.hub.embs.items():
            self.assertEqual(profile, TINY.id)
            self.assertEqual(hsh, _md5(self.hub.chunks[(doc, idx)][0]))
        # float32 little-endian decoded exactly
        heiser = next(i for i, d in self.hub.docs.items() if d["rel_path"] == "Heiser/Unseen.txt")
        self.assertEqual(self.hub.embs[(TINY.id, heiser, 0)][0], "[1.0000000,0.0000000,0.0000000,0.0000000]")
        self.assertEqual(self.docs_by_rel()["Heiser/Unseen.txt"]["author"], "Heiser")
        self.assertEqual(any(s.startswith("CREATE INDEX") and "library_embeddings" in s
                             for s, _ in self.hub.executed), True)
        row = library.list_sources(self.hub)[0]
        self.assertEqual((row["documents"], row["chunks"], row["embedded"], row["missing"]), (2, 3, 3, 0))

    def _ddl_order(self):
        """Statement kinds in the order they ran: drop, first insert, create."""
        kinds = []
        for stmt, _ in self.hub.executed:
            if stmt.startswith("DROP INDEX"):
                kinds.append("drop")
            elif stmt.startswith("INSERT INTO library_embeddings") and "insert" not in kinds:
                kinds.append("insert")
            elif stmt.startswith("CREATE INDEX"):
                kinds.append("create")
        return kinds

    def test_an_existing_index_is_dropped_before_the_insert_and_rebuilt_after(self):
        name = profiles.profile_index_name(TINY.id, "library_embeddings")
        other = profiles.profile_index_name("other@4", "library_embeddings")
        self.hub.indexes |= {name, other}
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.run_import()
        self.assertEqual(self._ddl_order(), ["drop", "insert", "create"])
        self.assertIn(name, self.hub.indexes)  # rebuilt
        self.assertIn(other, self.hub.indexes)  # a profile the job never wrote to is untouched
        self.assertIn("scan sequentially", err.getvalue())
        self.assertIn(f"dropped index {name}", err.getvalue())

    def test_no_index_means_no_drop_and_one_build_at_the_end(self):
        self.run_import()
        self.assertEqual(self._ddl_order(), ["insert", "create"])

    def test_the_index_is_rebuilt_even_when_the_load_dies(self):
        name = profiles.profile_index_name(TINY.id, "library_embeddings")
        self.hub.indexes.add(name)
        real = library._decode_vector
        calls = {"n": 0}

        def boom(raw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk gone")
            return real(raw)

        with mock.patch.object(library, "_decode_vector", boom), \
                mock.patch("sys.stderr", new_callable=io.StringIO), \
                self.assertRaises(RuntimeError):
            self.run_import()
        self.assertEqual(self._ddl_order()[0], "drop")
        self.assertIn(name, self.hub.indexes)

    def test_batch_sets_the_rows_per_commit_and_must_be_positive(self):
        self.run_import()
        default_commits = self.hub.commits
        self.hub.commits = 0
        self.run_import(batch=1)
        self.assertGreater(self.hub.commits, default_commits)
        with self.assertRaises(library.LibraryError):
            self.run_import(batch=0)

    def test_second_run_is_idempotent(self):
        first = self.run_import()
        snapshot = (dict(self.hub.chunks), dict(self.hub.embs), set(self.hub.docs))
        second = self.run_import()
        self.assertEqual(first, second)
        self.assertEqual((dict(self.hub.chunks), dict(self.hub.embs), set(self.hub.docs)), snapshot)

    def test_profile_is_created_with_the_provider_inferred_from_the_model(self):
        self.hub.profile_rows.clear()
        profiles.clear_learned()  # add() had cached the profile in this process
        self.run_import()
        self.assertEqual(self.hub.profile_rows[TINY.id][:3], ("voyage", "voyage-t", 4))
        # a gemini-named model infers gemini
        self.assertEqual(library._infer_provider("gemini-embedding-001"), "gemini")
        self.assertEqual(library._infer_provider("Voyage-3"), "voyage")
        self.assertEqual(library._infer_provider("nomic-embed-text"), "openai-compatible")

    def test_an_uninferable_new_profile_is_refused_not_created_broken(self):
        idx = Path(self.tmp.name) / "other.sqlite"
        _make_index(idx, [("n", 0, "Heiser/Unseen.txt", "t", _blob(1, 0, 0, 0), "nomic-embed-text", 4)])
        with self.assertRaises(library.LibraryError) as ctx:
            library.import_index(self.hub, "lib1", str(idx))
        self.assertIn("--endpoint", str(ctx.exception))
        self.assertNotIn("nomic-embed-text@4", self.hub.profile_rows)

    def test_explicit_profile_wins_and_must_exist(self):
        self.hub.profile_rows["other@4"] = ("voyage", "other", 4, "l2", None)
        out = self.run_import(profile="other@4")
        self.assertEqual(out["profile"], "other@4")
        self.assertIn("note", out)  # library searches with voyage-t@4, not other@4
        self.assertEqual({k[0] for k in self.hub.embs}, {"other@4"})
        with self.assertRaises(library.LibraryError):
            self.run_import(profile="ghost@4")

    def test_dims_that_disagree_with_the_blob_are_counted_bad(self):
        idx = Path(self.tmp.name) / "d.sqlite"
        _make_index(idx, [("n", 0, "Heiser/Unseen.txt", "t", _blob(1, 0, 0, 0), "voyage-t", 8)])
        out = library.import_index(self.hub, "lib1", str(idx), profile=TINY.id)
        self.assertEqual((out["imported"], out["skipped_bad_vector"]), (0, 1))
        self.assertEqual(self.hub.embs, {})

    def test_jsonl_with_float_lists(self):
        j = Path(self.tmp.name) / "idx.jsonl"
        lines = [
            {"node_id": "a", "chunk_idx": 0, "source_file": "corpus/Heiser/Unseen.txt",
             "chunk_text": "from jsonl", "embedding": [0.5, 0.5, 0.5, 0.5], "model": "voyage-t", "dims": 4},
            {"node_id": "a", "chunk_idx": 1, "source_file": "corpus/Heiser/Unseen.txt",
             "chunk_text": "bad vec", "embedding": [0.5, "x"], "model": "voyage-t", "dims": 4},
        ]
        j.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n[1,2]\n")
        out = library.import_index(self.hub, "lib1", str(j), strip_prefix="corpus/")
        self.assertEqual((out["read"], out["imported"], out["skipped_bad_vector"],
                          out["skipped_bad_row"]), (4, 1, 1, 2))

    def test_imported_chunks_replace_a_scans_chunks_for_the_same_file(self):
        library.scan(self.hub, "lib1")
        self.assertEqual(len(self.hub.chunks), 2)  # one scan chunk per file
        self.run_import()
        heiser = next(i for i, d in self.hub.docs.items() if d["rel_path"] == "Heiser/Unseen.txt")
        self.assertEqual(sorted(i for (d, i) in self.hub.chunks if d == heiser), [0, 1])
        self.assertEqual(self.hub.chunks[(heiser, 0)][0], "first chunk")
        # a later scan finds the files unchanged and leaves the imported chunks alone
        out = library.scan(self.hub, "lib1")
        self.assertEqual((out["updated"], out["unchanged"]), (0, 2))

    def test_a_path_outside_the_root_or_not_txt_md_is_unmapped(self):
        self.write("Lewis/data.csv", "a,b")
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("x")
        idx = Path(self.tmp.name) / "p.sqlite"
        _make_index(idx, [
            ("n", 0, "Lewis/data.csv", "t", _blob(1, 0, 0, 0), "voyage-t", 4),
            ("n", 0, "../outside.txt", "t", _blob(1, 0, 0, 0), "voyage-t", 4),
            ("n", 0, str(outside), "t", _blob(1, 0, 0, 0), "voyage-t", 4),
            ("n", 0, "", "t", _blob(1, 0, 0, 0), "voyage-t", 4),
            ("n", 0, f"{self.root}/Lewis/Mere.md", "abs path under root", _blob(1, 0, 0, 0), "voyage-t", 4),
        ])
        out = library.import_index(self.hub, "lib1", str(idx))
        self.assertEqual((out["unmapped"], out["imported"]), (4, 1))
        self.assertEqual(set(self.docs_by_rel()), {"Lewis/Mere.md"})

    def test_bad_inputs_refuse(self):
        with self.assertRaises(library.LibraryError):
            library.import_index(self.hub, "lib1", str(Path(self.tmp.name) / "nope.sqlite"))
        empty = Path(self.tmp.name) / "empty.sqlite"
        _make_index(empty, [], table=False)
        with self.assertRaises(library.LibraryError) as ctx:
            library.import_index(self.hub, "lib1", str(empty))
        self.assertIn("embeddings", str(ctx.exception))
        junk = Path(self.tmp.name) / "junk.sqlite"
        junk.write_bytes(b"this is not sqlite" * 50)
        with self.assertRaises(library.LibraryError):
            library.import_index(self.hub, "lib1", str(junk))

    def test_streaming_commits_in_batches(self):
        many = [("n", i, "corpus/Heiser/Unseen.txt", f"chunk {i}", _blob(1, 0, 0, 0), "voyage-t", 4)
                for i in range(25)]
        idx = Path(self.tmp.name) / "many.sqlite"
        _make_index(idx, many)
        with mock.patch.object(library, "IMPORT_BATCH", 10):
            before = self.hub.commits
            out = library.import_index(self.hub, "lib1", str(idx), strip_prefix="corpus/")
        self.assertEqual(out["imported"], 25)
        self.assertGreaterEqual(self.hub.commits - before, 3)

    def test_corrupt_text_heuristic(self):
        self.assertFalse(library.is_corrupt_chunk_text("Plain prose.\n\tTabbed."))
        self.assertFalse(library.is_corrupt_chunk_text(""))
        self.assertFalse(library.is_corrupt_chunk_text(None))
        self.assertTrue(library.is_corrupt_chunk_text("\x01" * 5 + "a" * 95 + "\x02"))
        self.assertTrue(library.is_corrupt_chunk_text("�" * 3 + "a" * 50))


class CliTest(_Base):
    def run_cli(self, **kw):
        args = argparse.Namespace(**kw)
        with mock.patch("khipu.db.connect", return_value=self.hub), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_library(args)
        return rc, json.loads(out.getvalue())

    def test_refusals_are_exit_2_with_the_reason(self):
        self.add()
        for kw in (
            dict(library_cmd="add", name="Bad Name", root=str(self.root), profile=TINY.id),
            dict(library_cmd="add", name="x", root=str(self.root / "nope"), profile=TINY.id),
            dict(library_cmd="add", name="x", root=str(self.root), profile="ghost@4"),
            dict(library_cmd="add", name="lib1", root=str(self.root), profile=TINY.id),
            dict(library_cmd="status", name="ghost"),
            dict(library_cmd="scan", name="ghost"),
            dict(library_cmd="remove", name="lib1", yes=False),
            dict(library_cmd="import", name="lib1", path="/no/such/file", strip_prefix="", profile=None),
            dict(library_cmd="enable", name="UPPER"),
        ):
            with self.subTest(kw=kw):
                rc, out = self.run_cli(**kw)
                self.assertEqual(rc, 2)
                self.assertFalse(out["ok"])
                self.assertTrue(out["error"])
        self.assertIn("lib1", self.hub.sources)

    def test_set_profile_cli_is_exit_2_with_the_missing_count_then_exit_0(self):
        other = ProfileSpec("voyage-u@4", "voyage", "voyage-u", 4)
        self.hub.profile_rows[other.id] = ("voyage", "voyage-u", 4, "l2", None)
        self.add()
        self.write("A/x.txt", "some text")
        library.scan(self.hub, "lib1")
        rc, out = self.run_cli(library_cmd="set-profile", name="lib1", profile=other.id)
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertIn("1 of 1 chunks", out["error"])
        doc = sorted(self.hub.source_docs("lib1"))[0]
        self.hub.embs[(other.id, doc, 0)] = ("[0,0,0,1]", self.hub.chunks[(doc, 0)][1])
        rc, out = self.run_cli(library_cmd="set-profile", name="lib1", profile=other.id)
        self.assertEqual((rc, out["ok"], out["profile"]), (0, True, other.id))

    def test_name_and_root_refusals_never_connect(self):
        with mock.patch("khipu.db.connect", side_effect=AssertionError("must not connect")), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            for kw in (dict(library_cmd="add", name="Bad", root=str(self.root), profile=TINY.id),
                       dict(library_cmd="add", name="ok", root="/no/such/dir", profile=TINY.id),
                       dict(library_cmd="scan", name="BAD")):
                self.assertEqual(cli.cmd_library(argparse.Namespace(**kw)), 2)

    def test_json_shapes_for_every_verb(self):
        rc, out = self.run_cli(library_cmd="add", name="lib1", root=str(self.root), profile=TINY.id)
        self.assertEqual((rc, out["ok"], out["enabled"]), (0, True, True))
        self.write("A/x.txt", "some text")
        rc, out = self.run_cli(library_cmd="scan", name="lib1")
        self.assertEqual(rc, 0)
        self.assertEqual(
            set(out), {"ok", "source", "added", "updated", "removed", "unchanged",
                       "skipped", "skipped_detail", "documents", "chunks"})
        rc, out = self.run_cli(library_cmd="list")
        self.assertEqual((rc, list(out)), (0, ["libraries"]))
        self.assertEqual(out["libraries"][0]["missing"], 1)
        rc, out = self.run_cli(library_cmd="status", name="lib1")
        self.assertEqual(rc, 0)
        self.assertTrue({"last_scan", "last_backfill", "sample_missing", "sample_stale"} <= set(out))
        embed.set_transport(_voyage_transport())
        self.addCleanup(embed.set_transport, None)
        rc, out = self.run_cli(library_cmd="backfill", name="lib1", limit=None, stale=False)
        self.assertEqual(rc, 0)
        self.assertEqual(
            {k: out[k] for k in ("embedded", "failed", "remaining", "profile")},
            {"embedded": 1, "failed": 0, "remaining": 0, "profile": TINY.id})
        idx = Path(self.tmp.name) / "i.sqlite"
        _make_index(idx, [("n", 0, "A/x.txt", "chunk", _blob(1, 0, 0, 0), "voyage-t", 4)])
        rc, out = self.run_cli(library_cmd="import", name="lib1", path=str(idx), strip_prefix="", profile=None)
        self.assertEqual(rc, 0)
        self.assertTrue({"read", "imported", "skipped_corrupt", "unmapped", "documents",
                         "profile"} <= set(out))
        self.assertEqual(self.run_cli(library_cmd="disable", name="lib1")[1], {"ok": True, "name": "lib1", "enabled": False})
        self.assertEqual(self.run_cli(library_cmd="enable", name="lib1")[1], {"ok": True, "name": "lib1", "enabled": True})
        rc, out = self.run_cli(library_cmd="remove", name="lib1", yes=True)
        self.assertEqual((rc, out["removed"]), (0, "lib1"))

    def test_parser_syntax(self):
        p = cli.build_parser()
        a = p.parse_args(["library", "add", "bib", "--root", "/r", "--profile", "voyage-3@1024"])
        self.assertEqual((a.library_cmd, a.name, a.root, a.profile), ("add", "bib", "/r", "voyage-3@1024"))
        a = p.parse_args(["library", "backfill", "bib", "--limit", "5", "--stale"])
        self.assertEqual((a.limit, a.stale), (5, True))
        a = p.parse_args(["library", "import", "bib", "/x.sqlite", "--strip-prefix", "c/", "--profile", "p@4"])
        self.assertEqual((a.path, a.strip_prefix, a.profile), ("/x.sqlite", "c/", "p@4"))
        a = p.parse_args(["library", "remove", "bib", "--yes"])
        self.assertTrue(a.yes)
        for verb in ("list", "status", "enable", "disable", "scan"):
            argv = ["library", verb] + ([] if verb == "list" else ["bib"])
            self.assertEqual(p.parse_args(argv).library_cmd, verb)
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                p.parse_args(["library", "add", "bib", "--root", "/r"])  # --profile required


class NightlyAndDoctorTest(_Base):
    def setUp(self):
        super().setUp()
        self.add()
        self.write("A/x.txt", "some text")

    def test_backfill_all_scans_then_embeds_every_enabled_library_and_isolates_failures(self):
        other = Path(self.tmp.name) / "lib2"
        other.mkdir()
        library.add_source(self.hub, "lib2", str(other), TINY.id)  # empty root: scan refuses nothing (no docs)
        library.set_enabled(self.hub, "lib2", False)
        embed.set_transport(_voyage_transport())
        self.addCleanup(embed.set_transport, None)
        out = library.backfill_all(self.hub)
        self.assertTrue(out["ok"])
        self.assertEqual(out["libraries"], 1)  # disabled ones are skipped
        self.assertEqual({k: out["results"][0][k] for k in ("name", "added", "embedded", "failed", "remaining")},
                         {"name": "lib1", "added": 1, "embedded": 1, "failed": 0, "remaining": 0})
        # a library that errors does not stop the next one
        library.set_enabled(self.hub, "lib2", True)
        library.add_source(self.hub, "zlib", str(self.root), TINY.id)
        self.hub.sources["lib2"]["root"] = str(Path(self.tmp.name) / "gone")
        out = library.backfill_all(self.hub)
        self.assertFalse(out["ok"])
        by = {r["name"]: r for r in out["results"]}
        self.assertIn("error", by["lib2"])
        self.assertNotIn("error", by["zlib"])

    def test_budget_exhaustion_skips_the_remaining_libraries(self):
        library.add_source(self.hub, "zlib", str(self.root), TINY.id)
        n = {"i": 0}

        def take():
            n["i"] += 1
            raise RuntimeError("embed budget exhausted")

        embed.set_transport(_voyage_transport())
        self.addCleanup(embed.set_transport, None)
        with mock.patch.object(embed, "_budget_take", side_effect=take):
            out = library.backfill_all(self.hub)
        self.assertTrue(out["results"][0]["budget_exhausted"])
        self.assertEqual(out["results"][1]["skipped"], "embed budget exhausted")
        self.assertEqual(n["i"], 1)

    def test_a_hub_without_library_tables_is_a_skip_not_a_failure(self):
        self.hub.library_tables = False
        out = library.backfill_all(self.hub)
        self.assertEqual((out["ok"], out["libraries"]), (True, 0))
        self.assertEqual(library.summary(self.hub), [])

    def test_the_nightly_embed_step_runs_library_backfill_and_keeps_the_receipt(self):
        receipt = {"ok": True, "libraries": 1, "results": [
            {"name": "lib1", "added": 1, "updated": 0, "removed": 0, "embedded": 3,
             "failed": 0, "remaining": 0}]}
        logged: list[str] = []
        with mock.patch("khipu.embed.backfill", return_value={"embedded": 2}), \
                mock.patch("khipu.db.dsn_configured", return_value=True), \
                mock.patch.object(library, "nightly", return_value=receipt), \
                mock.patch.object(jobs, "_nightly_log", side_effect=logged.append):
            out = jobs._embed_backfill()
        self.assertEqual(out["library"], receipt)
        self.assertTrue(out["ok"])  # the memory sweep's own verdict is untouched
        line = next(x for x in logged if x.startswith("[khipu-library] backfill ok"))
        self.assertIn('"embedded": 3', line)
        self.assertIn("lib1", line)

    def test_library_step_runs_even_when_the_memory_sweep_raises(self):
        with mock.patch("khipu.embed.backfill", side_effect=RuntimeError("no key")), \
                mock.patch.object(jobs, "_library_backfill", return_value={"ok": True}), \
                mock.patch.object(jobs, "_nightly_log"):
            out = jobs._embed_backfill()
        self.assertFalse(out["ok"])
        self.assertEqual(out["library"], {"ok": True})

    def test_no_hub_means_no_library_step_and_no_new_key(self):
        with mock.patch("khipu.db.dsn_configured", return_value=False), \
                mock.patch("khipu.embed.backfill", return_value={"embedded": 0}), \
                mock.patch.object(jobs, "_nightly_log"):
            self.assertIsNone(jobs._library_backfill())
            self.assertNotIn("library", jobs._embed_backfill())

    def test_a_failing_library_sweep_is_logged_and_never_raises(self):
        logged: list[str] = []
        with mock.patch("khipu.db.dsn_configured", return_value=True), \
                mock.patch.object(library, "nightly", side_effect=RuntimeError("hub down")), \
                mock.patch.object(jobs, "_nightly_log", side_effect=logged.append):
            out = jobs._library_backfill()
        self.assertFalse(out["ok"])
        self.assertTrue(any("[khipu-library] backfill skipped" in x for x in logged))

    def test_the_nightly_still_records_the_same_steps(self):
        src = open(jobs.__file__, encoding="utf-8").read()
        run = src[src.index("def run_nightly"):src.index("BRIEFS_NIGHTLY_LIMIT")]
        self.assertIn("_embed_backfill()", run)
        self.assertNotIn("library_backfill", run)  # rides inside embed_backfill; step set unchanged

    def test_doctor_block_lists_each_library_with_coverage_and_profile(self):
        library.scan(self.hub, "lib1")
        block = library.summary(self.hub)
        self.assertEqual(len(block), 1)
        self.assertEqual(
            set(block[0]),
            {"name", "profile", "enabled", "documents", "chunks", "embedded", "missing", "stale", "pct"})
        self.assertEqual((block[0]["name"], block[0]["profile"], block[0]["pct"]), ("lib1", TINY.id, 0.0))

    def test_doctor_reports_libraries_beside_embed_coverage_and_survives_a_hub_error(self):
        src = open(cli.__file__, encoding="utf-8").read()
        doctor = src[src.index('"embed_coverage": embed_coverage,'):]
        self.assertTrue(doctor.lstrip().startswith('"embed_coverage": embed_coverage,\n        "libraries": libraries,'))
        with mock.patch("khipu.db.connect", side_effect=RuntimeError("no hub")):
            with self.assertRaises(RuntimeError):
                library.doctor_block()  # cli.doctor wraps this and reports {"error": ...}

    def test_pct_never_rounds_a_gap_to_100(self):
        self.assertEqual(library._pct(4335, 4336), 99.9)
        self.assertEqual(library._pct(4336, 4336), 100.0)
        self.assertEqual(library._pct(0, 0), 0.0)


if __name__ == "__main__":
    unittest.main()
