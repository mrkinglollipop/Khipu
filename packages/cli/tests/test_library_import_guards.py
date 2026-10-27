"""Library scan and import guards: oversize-only walks, resumed imports of edited files, unmapped rows of imported documents."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from khipu import library
from tests.test_library import TINY, _Base


class OversizeOnlyWalkTest(_Base):
    def test_one_oversize_file_does_not_let_a_scan_delete_every_imported_document(self):
        self.add()
        self.write("a.txt", "alpha text")
        self.write("b.txt", "beta text")
        library.scan(self.hub, "lib1")
        for i, d in self.hub.docs.items():
            self.hub.embs[(TINY.id, i, 0)] = ("[1,0,0,0]", None)
        # the two indexed files vanish from the walk; only an oversize new file remains
        (self.root / "a.txt").unlink()
        (self.root / "b.txt").unlink()
        with mock.patch.object(library, "MAX_FILE_BYTES", 50):
            self.write("huge.txt", "x " * 100)
            try:
                out = library.scan(self.hub, "lib1")
            except library.LibraryError:
                return  # refusing is the fix
        self.assertTrue(self.hub.docs, f"every imported document deleted by a walk that found only an oversize file: {out}")


class ResumeAfterEditTest(_Base):
    def _jsonl(self, name, idxs):
        p = Path(self.tmp.name) / name
        p.write_text("\n".join(json.dumps({
            "node_id": "a", "chunk_idx": i, "source_file": "corpus/book.txt",
            "chunk_text": f"chunk {i}", "embedding": [1.0, 0.0, 0.0, 0.0], "model": "voyage-3.5", "dims": 4,
        }) for i in idxs) + "\n")
        return p

    def test_resume_refuses_chunks_when_the_file_bytes_changed_since_the_first_run(self):
        self.add()
        self.write("book.txt", "the file bytes stay the same")
        library.import_index(self.hub, "lib1", str(self._jsonl("one.jsonl", [0])), strip_prefix="corpus/")
        self.write("book.txt", "the file was edited after the first run")
        out = library.import_index(self.hub, "lib1", str(self._jsonl("two.jsonl", [0, 1])), strip_prefix="corpus/")
        doc = next(i for i, d in self.hub.docs.items() if d["rel_path"] == "book.txt")
        self.assertNotIn((doc, 1), self.hub.chunks, f"chunk 1 stored against bytes it was not made from: {out}")


class GrewPastCapDuringResumeTest(_Base):
    def test_mixed_run_with_unmapped_rows_of_an_imported_document_is_not_ok(self):
        self.add()
        self.write("book.txt", "small")
        self.write("other.txt", "other")
        p = Path(self.tmp.name) / "i.jsonl"

        def row(f, i):
            return json.dumps({"node_id": "a", "chunk_idx": i, "source_file": f"corpus/{f}",
                               "chunk_text": "small" if f == "book.txt" else "other",
                               "embedding": [1.0, 0.0, 0.0, 0.0], "model": "voyage-3.5", "dims": 4})
        p.write_text(row("book.txt", 0) + "\n")
        library.import_index(self.hub, "lib1", str(p), strip_prefix="corpus/")
        p.write_text(row("book.txt", 0) + "\n" + row("other.txt", 0) + "\n")
        with mock.patch.object(library, "MAX_FILE_BYTES", 6):
            self.write("book.txt", "small but now over the cap")
            out = library.import_index(self.hub, "lib1", str(p), strip_prefix="corpus/")
        self.assertFalse(out["ok"] and out.get("unmapped"), f"unmapped rows of an imported doc but ok: {out}")


class EditedResumeHashesOnceTest(ResumeAfterEditTest):
    def test_an_edited_document_is_hashed_once_not_once_per_row(self):
        self.add()
        self.write("book.txt", "the file bytes stay the same")
        library.import_index(self.hub, "lib1", str(self._jsonl("one.jsonl", [0])), strip_prefix="corpus/")
        self.write("book.txt", "the file was edited after the first run")
        real = Path.read_bytes
        calls = []

        def counting(p):
            if p.name == "book.txt":
                calls.append(p)
            return real(p)
        with mock.patch.object(Path, "read_bytes", counting):
            out = library.import_index(self.hub, "lib1", str(self._jsonl("two.jsonl", range(50))), strip_prefix="corpus/")
        self.assertEqual(out["skipped_conflict"], 50, out)
        self.assertLessEqual(len(calls), 1, f"edited file read {len(calls)} times for 50 rows")


class UndecodableOnlyWalkTest(_Base):
    def test_a_non_utf8_file_does_not_let_a_scan_delete_every_document(self):
        self.add()
        self.write("a.txt", "alpha text")
        self.write("b.txt", "beta text")
        library.scan(self.hub, "lib1")
        (self.root / "a.txt").unlink()
        (self.root / "b.txt").unlink()
        (self.root / "junk.txt").write_bytes(b"\xff\xfe")
        try:
            out = library.scan(self.hub, "lib1")
        except library.LibraryError:
            return  # refusing is the fix
        self.assertTrue(self.hub.docs, f"every document deleted by a walk that found only an undecodable file: {out}")
