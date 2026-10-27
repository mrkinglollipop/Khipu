"""Regression probes for library scan/import leads (grokq-triage-3)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from khipu import library
from tests.test_library import TINY, _Base


class OversizeTest(_Base):
    def test_a_file_that_grows_past_the_cap_keeps_its_document_and_vectors(self):
        self.add()
        self.write("a.txt", "alpha text")
        self.write("b.txt", "beta text")
        library.scan(self.hub, "lib1")
        a_doc = next(i for i, d in self.hub.docs.items() if d["rel_path"] == "a.txt")
        self.hub.embs[(TINY.id, a_doc, 0)] = ("[1,0,0,0]", None)
        with mock.patch.object(library, "MAX_FILE_BYTES", 50):
            self.write("a.txt", "alpha text " * 20)
            out = library.scan(self.hub, "lib1")
        self.assertIn("a.txt", {d["rel_path"] for d in self.hub.docs.values()},
                      f"oversize file's document was deleted with its vectors: {out}")

    def test_a_permanently_oversize_file_does_not_block_sibling_deletion(self):
        self.add()
        self.write("big.txt", "small")
        gone = self.write("gone.txt", "bye")
        library.scan(self.hub, "lib1")
        gone.unlink()
        with mock.patch.object(library, "MAX_FILE_BYTES", 40):
            self.write("big.txt", "x" * 50)
            out = library.scan(self.hub, "lib1")
        self.assertEqual(out.get("removed"), 1, f"oversize blocked deletions: {out}")
        self.assertEqual(set(self.docs_by_rel()), {"big.txt"})
        self.assertTrue(out.get("ok"), f"too_large alone must not mark the scan incomplete: {out}")


class PartialImportTest(_Base):
    def _jsonl(self, name, idxs):
        p = Path(self.tmp.name) / name
        p.write_text("\n".join(json.dumps({
            "node_id": "a", "chunk_idx": i, "source_file": "corpus/book.txt",
            "chunk_text": f"chunk {i}", "embedding": [1.0, 0.0, 0.0, 0.0], "model": TINY.model, "dims": TINY.dim,
        }) for i in idxs) + "\n")
        return p

    def test_a_resumed_import_can_add_the_chunks_the_first_run_never_wrote(self):
        self.add()
        self.write("book.txt", "the file bytes stay the same")
        library.import_index(self.hub, "lib1", str(self._jsonl("one.jsonl", [0])), strip_prefix="corpus/")
        out = library.import_index(self.hub, "lib1", str(self._jsonl("two.jsonl", [0, 1])), strip_prefix="corpus/")
        doc = next(i for i, d in self.hub.docs.items() if d["rel_path"] == "book.txt")
        self.assertIn((doc, 1), self.hub.chunks, f"chunk 1 refused as a conflict: {out}")


class StripPrefixTest(_Base):
    def test_prefix_without_trailing_slash_still_maps_the_rows_or_fails_loudly(self):
        self.add()
        self.write("Author/Book.txt", "hello world")
        p = Path(self.tmp.name) / "i.jsonl"
        p.write_text(json.dumps({
            "node_id": "a", "chunk_idx": 0, "source_file": "corpus/Author/Book.txt",
            "chunk_text": "hello world", "embedding": [1.0, 0.0, 0.0, 0.0], "model": TINY.model, "dims": TINY.dim,
        }) + "\n")
        out = library.import_index(self.hub, "lib1", str(p), strip_prefix="corpus")
        self.assertTrue(out["imported"] > 0 or out["ok"] is False,
                        f"nothing imported yet the receipt says ok: {out}")
