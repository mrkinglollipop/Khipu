"""Contract tests for Phase 5 "organisation without the user":

  - khipu.notes: slug collisions (G3) — different content under the same
    name across two project dirs -> two namespaced slugs + a redirect;
    identical content -> one.
  - khipu.topic_hits: hit-count aggregation from a fixture query log (G6).
  - khipu.organise: index rewrite (G1) — preamble, ranking, cap, overflow,
    the >30% drop refusal; split (G2/G4); stale report (G2); supersede
    (R6/G5).
  - khipu.embed: section-aware chunking (G4) — a prepend to one section
    re-embeds that section only.

Everything here runs against temp directories and fake cursors; nothing
touches the real ~/.claude, ~/.codex, or Postgres — same posture as
test_notes.py and test_embed.py.
"""
from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from khipu import embed as em
from khipu import notes, organise, topic_hits


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _note_text(name: str, note_type: str, body: str) -> str:
    # Built by plain concatenation, not textwrap.dedent — a multi-line
    # `body` (the split tests pass one) defeats dedent's common-leading-
    # whitespace detection, since body's own continuation lines carry no
    # indentation of their own and the whole block would end up NOT dedented
    # at all, leaving the frontmatter's leading spaces in place and
    # `_parse_note_frontmatter`'s ``text.startswith("---")`` check failing.
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{name} description"\n'
        "metadata:\n"
        "  node_type: memory\n"
        f"  type: {note_type}\n"
        "  modified: 2026-09-01T00:00:00.000Z\n"
        "---\n\n"
        f"{body}\n"
    )


# ---------------------------------------------------------------------------
# G3 — slug collisions
# ---------------------------------------------------------------------------


class CollisionTest(unittest.TestCase):
    def _item(self, path: str, project: str, body: str, claude_slug: str) -> dict:
        parsed = notes._note_topic_dict(Path(path), project=project)
        return {"harness": "claude_code", "parsed": parsed, "path": path, "claude_slug": claude_slug}

    def test_different_content_same_name_reslugs_both_and_redirects(self):
        with tempfile.TemporaryDirectory() as td:
            p1 = _write(Path(td) / "repo-a" / "memory" / "same-name.md",
                        _note_text("Same Name", "project", "Content from repo A"))
            p2 = _write(Path(td) / "repo-b" / "memory" / "same-name.md",
                        _note_text("Same Name", "project", "Content from repo B, different"))
            item1 = self._item(str(p1), "acme/repo-a", "A", "-repo-a")
            item2 = self._item(str(p2), "acme/repo-b", "B", "-repo-b")
            resolved, report = notes._resolve_collisions([item1, item2])
        self.assertEqual(report["collisions_reslugged"], 1)
        self.assertEqual(report["duplicate_copies"], 0)
        new_slugs = sorted(it["parsed"]["slug"] for it in resolved)
        self.assertEqual(len(new_slugs), 2)
        self.assertEqual(len(set(new_slugs)), 2)
        for s in new_slugs:
            self.assertTrue(s.startswith("note:"))
            self.assertIn("/same-name", s)
        self.assertEqual(len(report["redirects"]), 1)
        self.assertEqual(report["redirects"][0]["old_slug"], "note:same-name")
        self.assertEqual(sorted(report["redirects"][0]["new_slugs"]), new_slugs)

    def test_identical_content_across_dirs_keeps_one(self):
        with tempfile.TemporaryDirectory() as td:
            text = _note_text("Duplicated Dir", "project", "Byte-identical content")
            p1 = _write(Path(td) / "repo-a" / "memory" / "dup.md", text)
            p2 = _write(Path(td) / "repo-a-also" / "memory" / "dup.md", text)
            item1 = self._item(str(p1), "acme/repo-a", "A", "-repo-a")
            item2 = self._item(str(p2), "acme/repo-a", "A", "-repo-a-also")
            resolved, report = notes._resolve_collisions([item1, item2])
        self.assertEqual(report["duplicate_copies"], 1)
        self.assertEqual(report["collisions_reslugged"], 0)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["parsed"]["slug"], "note:duplicated-dir")

    def test_no_collision_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            p1 = _write(Path(td) / "repo-a" / "memory" / "one.md", _note_text("One", "project", "x"))
            item1 = self._item(str(p1), "acme/repo-a", "x", "-repo-a")
            resolved, report = notes._resolve_collisions([item1])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(report["collisions_reslugged"], 0)
        self.assertEqual(report["duplicate_copies"], 0)


# ---------------------------------------------------------------------------
# G6 — recall hit counts
# ---------------------------------------------------------------------------


class _FakeHitsCursor:
    def __init__(self, live_slugs: set[str]):
        self.live_slugs = live_slugs
        self.hits: dict[str, tuple[int, str | None]] = {}
        self._result: list = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("SELECT slug FROM topics WHERE slug = ANY"):
            (slugs,) = params
            self._result = [(s2,) for s2 in slugs if s2 in self.live_slugs]
            return
        if s.startswith("SELECT slug FROM topic_hits"):
            self._result = [(s2,) for s2 in self.hits]
            return
        if s.startswith("INSERT INTO topic_hits"):
            slug, n, last = params
            prev_n, prev_last = self.hits.get(slug, (0, None))
            if "hits_30d = topic_hits.hits_30d + EXCLUDED.hits_30d" in s:
                self.hits[slug] = (prev_n + n, last or prev_last)
            else:
                self.hits[slug] = (n, last)
            return
        if s.startswith("UPDATE topic_hits SET hits_30d = 0"):
            (slug,) = params
            n, last = self.hits.get(slug, (0, None))
            self.hits[slug] = (0, last)
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchall(self):
        return list(self._result)


class TopicHitsTest(unittest.TestCase):
    def test_incremental_counts_only_new_lines_and_only_live_slugs(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "query_log.jsonl"
            log_path.write_text(
                json.dumps({"ts": "2026-09-14T00:00:00Z", "top": [{"kind": "topic", "id": "note:a"}]}) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(topic_hits.query_log, "log_path", return_value=log_path), \
                    mock.patch.object(topic_hits, "_offset_state_path", return_value=Path(td) / "offset.json"):
                cur = _FakeHitsCursor(live_slugs={"note:a"})
                out1 = topic_hits.apply_incremental(cur)
                self.assertTrue(out1["ok"])
                self.assertEqual(cur.hits["note:a"][0], 1)

                # A second batch appended after the first offset: only the
                # NEW line is counted, and a hit for a slug that no longer
                # exists in topics is dropped rather than raising.
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "ts": "2026-09-14T01:00:00Z",
                        "top": [{"kind": "topic", "id": "note:a"}, {"kind": "topic", "id": "note:gone"}],
                    }) + "\n")
                out2 = topic_hits.apply_incremental(cur)
        self.assertTrue(out2["ok"])
        self.assertEqual(cur.hits["note:a"][0], 2)
        self.assertNotIn("note:gone", cur.hits)

    def test_recompute_zeroes_topics_outside_the_window(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "query_log.jsonl"
            log_path.write_text(
                json.dumps({"ts": "2026-09-14T00:00:00Z", "top": [{"kind": "topic", "id": "note:fresh"}]}) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(topic_hits.query_log, "log_path", return_value=log_path), \
                    mock.patch.object(topic_hits, "_offset_state_path", return_value=Path(td) / "offset.json"), \
                    mock.patch("khipu.topic_hits.datetime") as m_dt:
                import datetime as real_dt

                m_dt.now.return_value = real_dt.datetime(2026, 9, 14, 12, 0, 0, tzinfo=real_dt.timezone.utc)
                m_dt.fromisoformat = real_dt.datetime.fromisoformat
                m_dt.side_effect = lambda *a, **k: real_dt.datetime(*a, **k)
                cur = _FakeHitsCursor(live_slugs={"note:fresh", "note:stale"})
                cur.hits["note:stale"] = (5, "2026-01-01T00:00:00+00:00")
                out = topic_hits.recompute(cur)
        self.assertTrue(out["ok"])
        self.assertEqual(cur.hits["note:fresh"][0], 1)
        self.assertEqual(cur.hits["note:stale"][0], 0)


# ---------------------------------------------------------------------------
# G1 — index rewrite
# ---------------------------------------------------------------------------


class RewriteIndexTest(unittest.TestCase):
    """khipu.paths.ensure_data_dir is mocked class-wide: a real (non no-op)
    rewrite here calls organise._backup_index, and without this mock it
    would write this MACHINE's real ~/.config/khipu/index-backups/ full of
    these tests' temp-dir fixtures (found live, 2026-09-14, alongside the
    same evidence-file pollution in test_notes.py's ReconcileTest)."""

    def setUp(self):
        self._backup_root = tempfile.TemporaryDirectory()
        self.addCleanup(self._backup_root.cleanup)
        patcher = mock.patch("khipu.paths.ensure_data_dir", return_value=Path(self._backup_root.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_keeps_preamble_ranks_and_writes(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            _write(mem / "a-feedback.md", _note_text("A Feedback", "feedback", "lesson"))
            _write(mem / "b-reference.md", _note_text("B Reference", "reference", "ref"))
            _write(mem / "MEMORY.md", "hand-written preamble line\n\n- [old](old.md)\n")
            report = organise.rewrite_index(mem, cap_bytes=20_000, dry_run=False)
            self.assertTrue(report["ok"])
            self.assertFalse(report.get("skipped"))
            text = (mem / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("hand-written preamble line", text)
        # feedback ranks ahead of reference regardless of file order.
        self.assertLess(text.index("A Feedback"), text.index("B Reference"))

    def test_respects_cap_and_writes_overflow(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            for i in range(5):
                _write(mem / f"note-{i}.md", _note_text(f"Note {i}", "reference", "x" * 50))
            report = organise.rewrite_index(mem, cap_bytes=200, dry_run=False)
            self.assertTrue(report["ok"])
            self.assertGreater(report["lines_moved_to_overflow"], 0)
            self.assertLessEqual(report["bytes_after"], 200 + 200)  # one line's worth of slack, never unbounded
            self.assertTrue((mem / "_overflow_index.md").is_file())

    def test_refuses_when_drop_exceeds_30_percent_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            existing = "\n".join(f"- [Old {i}](old-{i}.md)" for i in range(10)) + "\n"
            _write(mem / "MEMORY.md", existing)
            # Only one real note on disk today -> a rewrite would drop 9/10
            # existing lines, well past the 30% guard.
            _write(mem / "one.md", _note_text("One", "reference", "x"))
            report = organise.rewrite_index(mem, cap_bytes=20_000, dry_run=False)
            self.assertTrue(report["skipped"])
            self.assertIn("drop", report["reason"])
            self.assertIn("90%", report["reason"])
            self.assertEqual((mem / "MEMORY.md").read_text(encoding="utf-8"), existing)

    def test_never_touches_a_non_khipu_index(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            maintainer_text = "# Just my own notes, no bullet list here\n"
            _write(mem / "MEMORY.md", maintainer_text)
            _write(mem / "one.md", _note_text("One", "reference", "x"))
            report = organise.rewrite_index(mem, dry_run=False)
            self.assertTrue(report["skipped"])
            self.assertEqual((mem / "MEMORY.md").read_text(encoding="utf-8"), maintainer_text)


class RewriteIndexPreservesTextTest(unittest.TestCase):
    """G1 incident (2026-09-14): the first rewrite_index replaced every
    existing bullet line's TEXT with a fresh render from frontmatter,
    destroying hand-edited index lines on 22 real memory dirs with no
    backup. These pin the fix: existing text survives verbatim, only
    membership/order may change, and a real change is always backed up
    first."""

    def test_existing_line_text_survives_even_when_frontmatter_changed(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            _write(mem / "a.md", _note_text("A", "project", "body"))
            hand_written = "- [A, the short hand-written hook the author wrote](a.md)\n"
            _write(mem / "MEMORY.md", hand_written)
            report = organise.rewrite_index(mem, dry_run=False)
            self.assertTrue(report["ok"])
            text = (mem / "MEMORY.md").read_text(encoding="utf-8")
        self.assertEqual(text, hand_written)
        self.assertNotIn("body", text)

    def test_new_note_gets_a_fresh_unescaped_line(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            note_path = mem / "b.md"
            _write(note_path, _note_text("B", "project", "body"))
            # Simulate a frontmatter description carrying a literal YAML
            # double-quote escape, same shape as the incident.
            # The escaped quotes sit INSIDE the value, not touching its own
            # outer quotes — matches the incident's actual shape and avoids
            # khipu.notes._parse_note_frontmatter's separate (pre-existing,
            # out of scope here) quirk of over-stripping quote characters
            # that are adjacent to the value's own boundary.
            text = note_path.read_text(encoding="utf-8").replace(
                'description: "B description"',
                'description: "a note about \\"the host runs it\\" behavior"',
            )
            note_path.write_text(text, encoding="utf-8")
            report = organise.rewrite_index(mem, dry_run=False)
            self.assertTrue(report["ok"])
            out = (mem / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn('a note about "the host runs it" behavior', out)
        self.assertNotIn('\\"', out)

    def test_reorder_only_is_a_true_no_op_no_write_no_backup(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            _write(mem / "a.md", _note_text("A", "reference", "x"))
            _write(mem / "b.md", _note_text("B", "feedback", "y"))
            # Pre-seed an index already in the NEW rank order (feedback
            # first) with hand-written text for both lines.
            existing = "- [B hand text](b.md)\n- [A hand text](a.md)\n"
            _write(mem / "MEMORY.md", existing)
            with mock.patch.object(organise, "_backup_index") as m_backup:
                report = organise.rewrite_index(mem, dry_run=False)
                m_backup.assert_not_called()
                self.assertFalse(report["changed"])
                self.assertIsNone(report["backup_path"])
                self.assertEqual((mem / "MEMORY.md").read_text(encoding="utf-8"), existing)

    def test_a_real_change_is_backed_up_first_and_pruned_to_20(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            _write(mem / "a.md", _note_text("A", "reference", "x"))
            original = "- [A original hand text](a.md)\n"
            _write(mem / "MEMORY.md", original)
            with mock.patch("khipu.paths.ensure_data_dir", return_value=mem / "_data"):
                # A brand-new note with no existing line forces a real
                # (membership) change, so a backup must be taken.
                _write(mem / "b.md", _note_text("B", "reference", "y"))
                report = organise.rewrite_index(mem, dry_run=False)
                self.assertTrue(report["changed"])
                backup_path = Path(report["backup_path"])
                self.assertTrue(backup_path.is_file())
                self.assertEqual(backup_path.read_text(encoding="utf-8"), original)
                # Under index-backups/<project-slug>/, never a sibling of
                # the note files themselves.
                self.assertIn("index-backups", str(backup_path))
                self.assertNotEqual(backup_path.parent, mem)

                # 25 more real changes -> only the newest 20 backups survive.
                for i in range(25):
                    _write(mem / f"n{i}.md", _note_text(f"N{i}", "reference", "z"))
                    organise.rewrite_index(mem, dry_run=False)
                backups = sorted((mem / "_data" / "index-backups").glob("*/MEMORY.md.*"))
        self.assertLessEqual(len(backups), organise.BACKUP_KEEP)


# ---------------------------------------------------------------------------
# G2/G4 — split
# ---------------------------------------------------------------------------


class SplitNoteTest(unittest.TestCase):
    def test_split_produces_children_with_parent_frontmatter_and_rewrites_parent(self):
        with tempfile.TemporaryDirectory() as td:
            body = "## First Entry\n\ncontent one\n\n## Second Entry\n\ncontent two\n"
            note_path = _write(Path(td) / "big-ledger.md", _note_text("Big Ledger", "project", body))
            report = organise.split_note(note_path, dry_run=False)
            self.assertTrue(report["ok"])
            self.assertEqual(len(report["children"]), 2)
            for child_path in report["children"]:
                child_text = Path(child_path).read_text(encoding="utf-8")
                self.assertIn("parent: big-ledger", child_text)
            parent_text = note_path.read_text(encoding="utf-8")
        self.assertIn("[[big-ledger--first-entry]]", parent_text)
        self.assertIn("[[big-ledger--second-entry]]", parent_text)
        self.assertNotIn("content one", parent_text)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            body = "## A\n\nx\n\n## B\n\ny\n"
            note_path = _write(Path(td) / "ledger.md", _note_text("Ledger", "project", body))
            original = note_path.read_text(encoding="utf-8")
            report = organise.split_note(note_path, dry_run=True)
            self.assertTrue(report["ok"])
            self.assertTrue(report["dry_run"])
            self.assertEqual(note_path.read_text(encoding="utf-8"), original)

    def test_fewer_than_two_sections_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            note_path = _write(Path(td) / "flat.md", _note_text("Flat", "project", "just prose, no sections"))
            report = organise.split_note(note_path, dry_run=False)
        self.assertTrue(report["ok"])
        self.assertTrue(report["skipped"])


# ---------------------------------------------------------------------------
# R6/G5 — supersede
# ---------------------------------------------------------------------------


class _FakeSupersedeCursor:
    def __init__(self, live_slugs: set[str]):
        self.live_slugs = live_slugs
        self.updates: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("SELECT slug FROM topics WHERE slug ="):
            self._found = params[0] in self.live_slugs
            return
        if s.startswith("UPDATE topics SET status = 'superseded'"):
            self.updates.append(params)
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchone(self):
        return ("x",) if getattr(self, "_found", False) else None


class SupersedeTest(unittest.TestCase):
    def test_supersede_sets_status_and_pointer(self):
        cur = _FakeSupersedeCursor(live_slugs={"note:old"})
        out = organise.supersede(cur, "note:old", "note:new")
        self.assertTrue(out["ok"])
        self.assertEqual(cur.updates, [("note:new", "note:old")])

    def test_supersede_refuses_missing_old_slug(self):
        cur = _FakeSupersedeCursor(live_slugs=set())
        out = organise.supersede(cur, "note:missing", "note:new")
        self.assertFalse(out["ok"])
        self.assertEqual(cur.updates, [])


# ---------------------------------------------------------------------------
# G2 — stale report (SQL-shape only; the fake cursor asserts what was asked)
# ---------------------------------------------------------------------------


class _FakeStaleCursor:
    def __init__(self, rows):
        self.rows = rows
        self.last_sql = None
        self.last_params = None

    def execute(self, sql, params=None):
        self.last_sql = " ".join(sql.split())
        self.last_params = params

    def fetchall(self):
        return self.rows


class StaleReportTest(unittest.TestCase):
    def test_report_shape_and_exclusions_in_sql(self):
        cur = _FakeStaleCursor([("note:x", "X", None, "active", "project", 0)])
        out = organise.stale_report(cur)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["notes"][0]["slug"], "note:x")
        self.assertIn("evergreen", cur.last_sql)
        self.assertIn("feedback", cur.last_sql)
        self.assertIn("user", cur.last_sql)

    def test_project_filter_adds_a_clause_and_param(self):
        cur = _FakeStaleCursor([])
        organise.stale_report(cur, project="acme/repo-a")
        self.assertIn("frontmatter->>'project'", cur.last_sql)
        self.assertIn("acme/repo-a", cur.last_params)


# ---------------------------------------------------------------------------
# G4 — section-aware chunking
# ---------------------------------------------------------------------------


class SectionAwareChunkingTest(unittest.TestCase):
    def _ledger(self, second_section_intro: str) -> str:
        return (
            "## Section One\n\n" + ("alpha " * 2000) + "\n\n"
            "## Section Two\n\n" + second_section_intro + ("beta " * 50) + "\n"
        )

    def test_chunk_text_matches_flat_chunk_text_content(self):
        text = self._ledger("")
        flat = em.chunk_text(text)
        indexed = em.chunk_text_indexed(text)
        self.assertEqual(flat, [c for _i, c in indexed])

    def test_no_heading_text_is_unaffected_by_sectioning(self):
        text = "x" * (em.CHUNK_CHARS + 100)
        self.assertEqual(len(em.chunk_text(text)), 2)

    def test_prepend_to_one_section_reembeds_at_most_two_chunks(self):
        before = dict(em.chunk_text_indexed(self._ledger("")))
        after_text = self._ledger("A NEW DATED ENTRY PREPENDED HERE. " * 5)
        after = dict(em.chunk_text_indexed(after_text))

        changed = 0
        for idx, chunk in after.items():
            if before.get(idx) != chunk:
                changed += 1
        # Anything in `before` that vanished from `after` (a chunk boundary
        # that shifted within the edited section) would also need
        # re-embedding — count those too, same as backfill's own diff would.
        vanished = sum(1 for idx in before if idx not in after)
        self.assertLessEqual(changed + vanished, 2)
        # Section One's own idx set is untouched — this is the whole point.
        section_one_before = {i for i in before if i // em.SECTION_LOCAL_SPACE == em._section_bucket("section-one")}
        section_one_after = {i for i in after if i // em.SECTION_LOCAL_SPACE == em._section_bucket("section-one")}
        self.assertEqual(section_one_before, section_one_after)
        for idx in section_one_before:
            self.assertEqual(before[idx], after[idx])


if __name__ == "__main__":
    unittest.main()
