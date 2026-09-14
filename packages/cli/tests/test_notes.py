"""Tests for khipu.notes — W4.3, indexing harness-native per-project notes
(~/.claude/projects/<slug>/memory/*.md, ~/.codex/memories/*.md) as topics.

Everything here runs against a temp directory tree and a fake cursor; nothing
touches the real ~/.claude, ~/.codex, or Postgres.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from khipu import notes


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


class ResolveClaudeProjectPathTest(unittest.TestCase):
    """The slug->path inverse is lossy (both '/' and a space collapse to
    '-'), so this walks a real directory tree rather than guessing offline."""

    def test_resolves_a_path_whose_segment_contains_a_space(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "Volumes" / "Cloud Storage" / "Code" / "Khipu"
            repo.mkdir(parents=True)
            slug = "-Volumes-Cloud-Storage-Code-Khipu"
            self.assertEqual(notes.resolve_claude_project_path(slug, root=root), repo)

    def test_resolves_a_plain_no_space_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo" / "widget"
            repo.mkdir(parents=True)
            self.assertEqual(
                notes.resolve_claude_project_path("-repo-widget", root=root), repo
            )

    def test_no_match_on_disk_is_none_not_a_raise(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(
                notes.resolve_claude_project_path("-nope-does-not-exist", root=Path(td))
            )

    def test_empty_slug_is_none(self):
        self.assertIsNone(notes.resolve_claude_project_path(""))
        self.assertIsNone(notes.resolve_claude_project_path("--"))


class ParseNoteFrontmatterTest(unittest.TestCase):
    def test_flat_and_one_level_nested_keys(self):
        text = _sample_note_text()
        flat, body = notes._parse_note_frontmatter(text)
        self.assertEqual(flat["name"], "khipu-state-of-play")
        self.assertEqual(flat["metadata.type"], "project")
        self.assertEqual(flat["metadata.modified"], "2026-08-17T17:13:10.321Z")
        self.assertNotIn("---", body.splitlines()[0] if body.splitlines() else "")
        self.assertIn("Khipu's", body)

    def test_no_frontmatter_block_is_the_whole_text_as_body(self):
        flat, body = notes._parse_note_frontmatter("just a plain note, no frontmatter\n")
        self.assertEqual(flat, {})
        self.assertEqual(body, "just a plain note, no frontmatter\n")


class ExtractNoteLinksTest(unittest.TestCase):
    def test_wikilinks_get_the_note_prefix_and_dedup(self):
        body = "See [[audit-lessons-2026-08-17]] and again [[audit-lessons-2026-08-17]], also [[Capture-Is-Hook-Driven]]."
        links = notes._extract_note_links(body)
        self.assertEqual(
            links, ["note:audit-lessons-2026-08-17", "note:capture-is-hook-driven"]
        )

    def test_no_links_is_empty_list(self):
        self.assertEqual(notes._extract_note_links("nothing here"), [])


def _sample_note_text() -> str:
    return textwrap.dedent(
        """\
        ---
        name: khipu-state-of-play
        description: "Where Khipu currently stands"
        metadata:
          node_type: memory
          type: project
          originSessionId: 0845cc23-5e13-4269-938f-08513a58b64f
          modified: 2026-08-17T17:13:10.321Z
        ---

        Khipu's "where are we" is recorded durably. See [[audit-lessons-2026-08-17]].
        """
    )


class NoteTopicDictTest(unittest.TestCase):
    def test_shape_matches_upsert_topic_expectations(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "khipu-state-of-play.md", _sample_note_text())
            parsed = notes._note_topic_dict(p, project="acme/khipu")
        self.assertEqual(parsed["slug"], "note:khipu-state-of-play")
        self.assertEqual(parsed["title"], "khipu-state-of-play")
        self.assertEqual(parsed["status"], "active")  # "project" matches no status keyword
        self.assertEqual(parsed["links"], ["note:audit-lessons-2026-08-17"])
        self.assertEqual(parsed["frontmatter"]["project"], "acme/khipu")
        self.assertEqual(parsed["frontmatter"]["status_raw"], "project")
        self.assertIsNotNone(parsed["updated_at"])
        self.assertTrue(parsed["digest"])
        # R7: event_at comes from the nested metadata.modified in this fixture.
        self.assertEqual(parsed["event_at"], "2026-08-17T17:13:10.321000+00:00")

    def test_status_type_shipped_maps_through_normalize_topic_status(self):
        text = _sample_note_text().replace("type: project", "type: shipped and wrapped")
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "note.md", text)
            parsed = notes._note_topic_dict(p, project=None)
        self.assertEqual(parsed["status"], "shipped")

    def test_missing_name_falls_back_to_filename_stem(self):
        text = _sample_note_text().replace("name: khipu-state-of-play\n", "")
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "fallback-name.md", text)
            parsed = notes._note_topic_dict(p, project=None)
        self.assertEqual(parsed["slug"], "note:fallback-name")

    def test_slugifies_free_text_names_with_spaces_and_punctuation(self):
        text = _sample_note_text().replace(
            "name: khipu-state-of-play", 'name: "Aggressive automatic memory capture!"'
        )
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "whatever.md", text)
            parsed = notes._note_topic_dict(p, project=None)
        self.assertEqual(parsed["slug"], "note:aggressive-automatic-memory-capture")
        self.assertEqual(parsed["title"], "Aggressive automatic memory capture!")

    def test_missing_file_is_none(self):
        self.assertIsNone(notes._note_topic_dict(Path("/no/such/file.md"), project=None))


class IterNoteFilesTest(unittest.TestCase):
    def test_memory_md_is_excluded_flat_and_non_recursive(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td) / "memory"
            _write(mem / "MEMORY.md", "index, not a note")
            _write(mem / "real-note.md", _sample_note_text())
            _write(mem / "sub" / "nested.md", "should not be picked up (non-recursive)")
            files = notes._iter_note_files(mem)
        self.assertEqual([f.name for f in files], ["real-note.md"])

    def test_missing_directory_is_empty_list(self):
        self.assertEqual(notes._iter_note_files(Path("/no/such/memory/dir")), [])


class _FakeTopicsCursor:
    """Enough of a Postgres cursor to exercise notes.reconcile's write path
    (mirror._upsert_topic's own SQL shapes) without a live database."""

    def __init__(self):
        self.topics: dict[str, dict] = {}
        self.revisions: list[tuple] = []
        self._result: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("SELECT content_hash FROM topics WHERE slug"):
            (slug,) = params
            row = self.topics.get(slug)
            self._result = [(row["content_hash"],)] if row else []
            return
        if s.startswith("INSERT INTO topics"):
            (slug, title, body, status, updated_at, frontmatter_json, links_json,
             source_path, content_hash, created_at, event_at) = params
            self.topics[slug] = {
                "title": title,
                "body": body,
                "status": status,
                "frontmatter": json.loads(frontmatter_json),
                "links": json.loads(links_json),
                "source_path": source_path,
                "content_hash": content_hash,
                "event_at": event_at,
                "deleted_at": None,
            }
            return
        if s.startswith("INSERT INTO topic_revisions"):
            self.revisions.append(params)
            return
        if s.startswith("SELECT slug, source_path FROM topics WHERE slug LIKE"):
            (like,) = params
            prefix = like[:-1] if like.endswith("%") else like
            self._result = [
                (slug, row.get("source_path"))
                for slug, row in self.topics.items()
                if slug.startswith(prefix) and not row.get("deleted_at")
            ]
            return
        if s.startswith("UPDATE topics SET deleted_at"):
            (slug,) = params
            if slug in self.topics:
                self.topics[slug]["deleted_at"] = "tombstoned"
            return
        raise AssertionError(f"unexpected SQL in fake cursor: {s[:120]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ReconcileTest(unittest.TestCase):
    """notes.reconcile is append-only and never touches the live hub in a
    test: khipu.db.connect and khipu.topic_graph.persist_topic_graph are
    always mocked here, same posture as WritePgOrchestrationTest in
    test_capture.py.

    khipu.organise.after_reconcile is also mocked class-wide: reconcile()
    calls it at the end of every real write (P5), and without this mock it
    would write this MACHINE's real ~/.config/khipu/state/notes-organise-
    last.json full of these tests' temp-dir fixtures — polluting the exact
    evidence file `khipu doctor`'s notes_organise row reads (found live,
    2026-09-14, while investigating the G1 incident).
    """

    def setUp(self):
        patcher = mock.patch("khipu.organise.after_reconcile", return_value={"ok": True, "mocked": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tree(self, td: str) -> tuple[Path, Path]:
        claude_root = Path(td) / "claude_projects"
        _write(claude_root / "-repo-a" / "memory" / "MEMORY.md", "index")
        _write(claude_root / "-repo-a" / "memory" / "note-one.md", _sample_note_text())
        _write(
            claude_root / "-repo-b" / "memory" / "note-two.md",
            _sample_note_text().replace("khipu-state-of-play", "second-note"),
        )
        codex_root = Path(td) / "codex_memories"
        _write(codex_root / "MEMORY.md", "index")
        _write(codex_root / "memory_summary.md", "a codex summary note, no frontmatter\n")
        _write(codex_root / "rollout_summaries" / "one.md", "not mirrored (subdirectory)")
        return claude_root, codex_root

    def test_dry_run_never_touches_the_db(self):
        with tempfile.TemporaryDirectory() as td:
            claude_root, codex_root = self._tree(td)
            with mock.patch.object(notes, "claude_projects_root", return_value=claude_root), \
                    mock.patch.object(notes, "codex_memories_root", return_value=codex_root), \
                    mock.patch.object(notes, "_project_for_slug", return_value="acme/repo-a"), \
                    mock.patch("khipu.db.connect") as m_connect:
                out = notes.reconcile(dry_run=True)
        m_connect.assert_not_called()
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["written"], 0)
        # note-one, note-two, memory_summary — MEMORY.md and the
        # rollout_summaries subdirectory are excluded.
        self.assertEqual(out["candidates"], 3)
        self.assertIn("note:khipu-state-of-play", out["slugs"])
        self.assertIn("note:second-note", out["slugs"])
        self.assertTrue(any("memory-summary" in s or "memory_summary" in s for s in out["slugs"]))

    def test_writes_land_with_note_prefix_and_project_in_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            claude_root, codex_root = self._tree(td)
            cur = _FakeTopicsCursor()
            with mock.patch.object(notes, "claude_projects_root", return_value=claude_root), \
                    mock.patch.object(notes, "codex_memories_root", return_value=codex_root), \
                    mock.patch.object(notes, "_project_for_slug", return_value="acme/repo-a"), \
                    mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                    mock.patch("khipu.mirror.persist_topic_graph", return_value={
                        "nodes_minted": 0, "edges_minted": 0,
                    }):
                out = notes.reconcile(dry_run=False)
        self.assertEqual(out["written"], 3)
        self.assertEqual(out["errors"], [])
        self.assertIn("note:khipu-state-of-play", cur.topics)
        row = cur.topics["note:khipu-state-of-play"]
        self.assertEqual(row["frontmatter"]["project"], "acme/repo-a")
        self.assertEqual(row["source_path"].endswith("note-one.md"), True)
        # codex memory has no repo project (mirrored the same way, not
        # attributed to a Claude Code project).
        codex_row = next(
            (v for k, v in cur.topics.items() if "memory-summary" in k or "memory_summary" in k),
            None,
        )
        self.assertIsNotNone(codex_row)
        self.assertIsNone(codex_row["frontmatter"]["project"])

    def test_one_bad_note_does_not_sink_the_batch(self):
        with tempfile.TemporaryDirectory() as td:
            claude_root, codex_root = self._tree(td)
            cur = _FakeTopicsCursor()
            calls = {"n": 0}

            def _flaky_upsert(cur_, parsed, path, **kw):
                calls["n"] += 1
                if parsed["slug"] == "note:khipu-state-of-play":
                    raise RuntimeError("simulated upsert failure")
                cur_.topics[parsed["slug"]] = {"frontmatter": parsed["frontmatter"]}
                return True

            with mock.patch.object(notes, "claude_projects_root", return_value=claude_root), \
                    mock.patch.object(notes, "codex_memories_root", return_value=codex_root), \
                    mock.patch.object(notes, "_project_for_slug", return_value="acme/repo-a"), \
                    mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                    mock.patch("khipu.mirror._upsert_topic", side_effect=_flaky_upsert):
                out = notes.reconcile(dry_run=False)
        self.assertEqual(out["written"], 2)
        self.assertEqual(len(out["errors"]), 1)
        self.assertTrue(out["errors"][0]["path"].endswith("note-one.md"))
        self.assertIn("simulated upsert failure", out["errors"][0]["error"])
        self.assertEqual(calls["n"], 3)

    def test_no_candidates_is_a_no_op_report_not_a_connect_call(self):
        with tempfile.TemporaryDirectory() as td:
            empty_claude = Path(td) / "no_claude_projects"
            empty_codex = Path(td) / "no_codex"
            with mock.patch.object(notes, "claude_projects_root", return_value=empty_claude), \
                    mock.patch.object(notes, "codex_memories_root", return_value=empty_codex), \
                    mock.patch("khipu.db.connect") as m_connect:
                out = notes.reconcile(dry_run=False)
        m_connect.assert_not_called()
        self.assertEqual(out["candidates"], 0)
        self.assertFalse(out["codex_root_found"])


class StateFileRoundTripTest(unittest.TestCase):
    """F1: the {path: {mtime, size}} state file _build_plan(changed_only=True)
    reads back to decide what to skip."""

    def test_read_missing_state_is_empty_not_a_raise(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("khipu.paths.ensure_data_dir", return_value=Path(td)):
                state = notes._read_state()
        self.assertEqual(state, {"last_reconcile_at": None, "files": {}})

    def test_write_then_read_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("khipu.paths.ensure_data_dir", return_value=Path(td)):
                notes._write_state({"last_reconcile_at": "2026-09-14T00:00:00Z",
                                     "files": {"/a/b.md": {"mtime": 1.0, "size": 10}}})
                state = notes._read_state()
        self.assertEqual(state["last_reconcile_at"], "2026-09-14T00:00:00Z")
        self.assertEqual(state["files"]["/a/b.md"], {"mtime": 1.0, "size": 10})

    def test_corrupt_state_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "state").mkdir()
            (Path(td) / "state" / "notes-reconcile-state.json").write_text("not json")
            with mock.patch("khipu.paths.ensure_data_dir", return_value=Path(td)):
                state = notes._read_state()
        self.assertEqual(state, {"last_reconcile_at": None, "files": {}})


class ChangedOnlyReconcileTest(unittest.TestCase):
    """F1: changed_only=True skips unchanged files entirely (no read, no
    parse) and writes only the edited file."""

    def _tree(self, td: str) -> Path:
        claude_root = Path(td) / "claude_projects"
        _write(claude_root / "-repo-a" / "memory" / "one.md", _sample_note_text())
        _write(
            claude_root / "-repo-a" / "memory" / "two.md",
            _sample_note_text().replace("khipu-state-of-play", "two"),
        )
        _write(
            claude_root / "-repo-a" / "memory" / "three.md",
            _sample_note_text().replace("khipu-state-of-play", "three"),
        )
        return claude_root

    def test_first_run_touches_every_file_second_run_touches_none(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as data_td:
            claude_root = self._tree(td)
            cur = _FakeTopicsCursor()
            with mock.patch.object(notes, "claude_projects_root", return_value=claude_root), \
                    mock.patch.object(notes, "codex_memories_root", return_value=Path(td) / "no-codex"), \
                    mock.patch.object(notes, "_project_for_slug", return_value="acme/repo-a"), \
                    mock.patch("khipu.paths.ensure_data_dir", return_value=Path(data_td)), \
                    mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                    mock.patch("khipu.mirror.persist_topic_graph", return_value={
                        "nodes_minted": 0, "edges_minted": 0}):
                first = notes.reconcile(dry_run=False, changed_only=True)
                second = notes.reconcile(dry_run=False, changed_only=True)
        self.assertEqual(first["candidates"], 3)
        self.assertEqual(first["written"], 3)
        self.assertEqual(second["candidates"], 0)
        self.assertEqual(second["written"], 0)

    def test_editing_one_file_reconciles_only_that_file(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as data_td:
            claude_root = self._tree(td)
            cur = _FakeTopicsCursor()
            patches = (
                mock.patch.object(notes, "claude_projects_root", return_value=claude_root),
                mock.patch.object(notes, "codex_memories_root", return_value=Path(td) / "no-codex"),
                mock.patch.object(notes, "_project_for_slug", return_value="acme/repo-a"),
                mock.patch("khipu.paths.ensure_data_dir", return_value=Path(data_td)),
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)),
                mock.patch("khipu.mirror.persist_topic_graph", return_value={
                    "nodes_minted": 0, "edges_minted": 0}),
            )
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                notes.reconcile(dry_run=False, changed_only=True)
                # Edit "two.md" only — bump its mtime forward so the stat
                # signature actually differs (some filesystems have 1s
                # mtime resolution).
                two = claude_root / "-repo-a" / "memory" / "two.md"
                text = two.read_text(encoding="utf-8") + "\nedited.\n"
                two.write_text(text, encoding="utf-8")
                new_mtime = (two.stat().st_mtime or 0) + 5
                os.utime(two, (new_mtime, new_mtime))
                second = notes.reconcile(dry_run=False, changed_only=True)
        self.assertEqual(second["candidates"], 1)
        self.assertEqual(second["written"], 1)
        self.assertTrue(second["slugs"][0].startswith("note:two"))

    def test_last_reconcile_at_is_recorded_even_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as data_td:
            claude_root = self._tree(td)
            cur = _FakeTopicsCursor()
            with mock.patch.object(notes, "claude_projects_root", return_value=claude_root), \
                    mock.patch.object(notes, "codex_memories_root", return_value=Path(td) / "no-codex"), \
                    mock.patch.object(notes, "_project_for_slug", return_value="acme/repo-a"), \
                    mock.patch("khipu.paths.ensure_data_dir", return_value=Path(data_td)), \
                    mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                    mock.patch("khipu.mirror.persist_topic_graph", return_value={
                        "nodes_minted": 0, "edges_minted": 0}):
                notes.reconcile(dry_run=False, changed_only=True)
                notes.reconcile(dry_run=False, changed_only=True)
                state = notes._read_state()
        self.assertIsNotNone(state["last_reconcile_at"])


class TombstoneMissingNotesTest(unittest.TestCase):
    """F5: a note: topic whose source file is gone gets deleted_at = now(),
    bounded so a mount blip cannot look like a bulk delete."""

    def test_a_deleted_note_is_tombstoned_on_full_reconcile(self):
        with tempfile.TemporaryDirectory() as td:
            claude_root = Path(td) / "claude_projects"
            _write(claude_root / "-repo-a" / "memory" / "one.md", _sample_note_text())
            gone = _write(
                claude_root / "-repo-a" / "memory" / "two.md",
                _sample_note_text().replace("khipu-state-of-play", "two"),
            )
            cur = _FakeTopicsCursor()
            with mock.patch.object(notes, "claude_projects_root", return_value=claude_root), \
                    mock.patch.object(notes, "codex_memories_root", return_value=Path(td) / "no-codex"), \
                    mock.patch.object(notes, "_project_for_slug", return_value="acme/repo-a"), \
                    mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                    mock.patch("khipu.mirror.persist_topic_graph", return_value={
                        "nodes_minted": 0, "edges_minted": 0}):
                notes.reconcile(dry_run=False)
                gone.unlink()
                out = notes.reconcile(dry_run=False)
        self.assertEqual(out["tombstone"]["tombstoned"], 1)
        self.assertFalse(out["tombstone"]["skipped"])
        self.assertEqual(cur.topics["note:two"]["deleted_at"], "tombstoned")
        self.assertIsNone(cur.topics["note:khipu-state-of-play"]["deleted_at"])

    def test_more_than_20_percent_missing_refuses(self):
        cur = _FakeTopicsCursor()
        for i in range(5):
            cur.topics[f"note:n{i}"] = {"source_path": f"/no/such/n{i}.md", "deleted_at": None}
        out = notes._tombstone_missing_notes(cur)
        self.assertTrue(out["skipped"])
        self.assertEqual(out["tombstoned"], 0)
        self.assertTrue(all(row["deleted_at"] is None for row in cur.topics.values()))

    def test_a_single_missing_note_out_of_many_is_not_blocked(self):
        cur = _FakeTopicsCursor()
        for i in range(10):
            cur.topics[f"note:n{i}"] = {
                "source_path": None if i == 0 else f"/no/such/n{i}.md", "deleted_at": None,
            }
        # 9 of 10 also point at nonexistent paths in this fixture (no real
        # files on disk), which would normally trip the breaker; isolate to
        # a single missing row by using a real path for the other 9.
        with tempfile.TemporaryDirectory() as td:
            for i in range(1, 10):
                p = Path(td) / f"n{i}.md"
                p.write_text("x", encoding="utf-8")
                cur.topics[f"note:n{i}"]["source_path"] = str(p)
            out = notes._tombstone_missing_notes(cur)
        self.assertEqual(out["tombstoned"], 1)
        self.assertFalse(out["skipped"])
        self.assertEqual(cur.topics["note:n0"]["deleted_at"], "tombstoned")


class EventAtNeverNowTest(unittest.TestCase):
    """R7: event_at is the note's own signal — frontmatter modified, else
    metadata.modified, else the file's mtime — never now()."""

    def test_falls_back_to_file_mtime_when_frontmatter_has_no_date(self):
        text = _sample_note_text().replace("  modified: 2026-08-17T17:13:10.321Z\n", "")
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "no-date.md", text)
            old = 1700000000.0
            os.utime(p, (old, old))
            parsed = notes._note_topic_dict(p, project=None)
        self.assertIsNotNone(parsed["event_at"])
        from datetime import datetime, timezone
        expected = datetime.fromtimestamp(old, tz=timezone.utc).isoformat()
        self.assertEqual(parsed["event_at"], expected)

    def test_frontmatter_modified_wins_over_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "dated.md", _sample_note_text())
            os.utime(p, (0, 0))  # 1970 — must NOT be used since frontmatter has a date
            parsed = notes._note_topic_dict(p, project=None)
        self.assertEqual(parsed["event_at"], "2026-08-17T17:13:10.321000+00:00")


class NotesFreshnessTest(unittest.TestCase):
    def test_reports_newest_mtime_and_last_reconcile(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as data_td:
            claude_root = Path(td) / "claude_projects"
            _write(claude_root / "-repo-a" / "memory" / "one.md", _sample_note_text())

            class _Cur:
                def execute(self, sql, params=None):
                    pass

                def fetchone(self):
                    return (None,)

            with mock.patch.object(notes, "claude_projects_root", return_value=claude_root), \
                    mock.patch.object(notes, "codex_memories_root", return_value=Path(td) / "no-codex"), \
                    mock.patch("khipu.paths.ensure_data_dir", return_value=Path(data_td)):
                out = notes.notes_freshness(_Cur())
        self.assertIsNotNone(out["newest_note_mtime"])
        self.assertIsNone(out["notes_last_reconcile_at"])


class CursorAegisRootsTest(unittest.TestCase):
    """F6: no such directories exist yet on any checked Mac — both return an
    empty list rather than raising, so the code path is ready when they do."""

    def test_no_cursor_memory_dirs_today(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(Path, "home", return_value=Path(td)):
                self.assertEqual(notes.cursor_memory_roots(), [])

    def test_aegis_is_always_empty_today(self):
        self.assertEqual(notes.aegis_memory_roots(), [])


if __name__ == "__main__":
    unittest.main()
