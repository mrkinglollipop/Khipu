"""Tests for khipu.notes — W4.3, indexing harness-native per-project notes
(~/.claude/projects/<slug>/memory/*.md, ~/.codex/memories/*.md) as topics.

Everything here runs against a temp directory tree and a fake cursor; nothing
touches the real ~/.claude, ~/.codex, or Postgres.
"""
from __future__ import annotations

import json
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
             source_path, content_hash, created_at) = params
            self.topics[slug] = {
                "title": title,
                "body": body,
                "status": status,
                "frontmatter": json.loads(frontmatter_json),
                "links": json.loads(links_json),
                "source_path": source_path,
                "content_hash": content_hash,
            }
            return
        if s.startswith("INSERT INTO topic_revisions"):
            self.revisions.append(params)
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
    test_capture.py."""

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


if __name__ == "__main__":
    unittest.main()
