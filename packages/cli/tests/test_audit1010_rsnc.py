"""Hermetic regressions for the rsnc audit packet."""
from __future__ import annotations

import contextlib
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import hygiene, notes, organise, topic_graph


def _note(name, body):
    return f"---\nname: {name}\ndescription: {name} summary\ntype: project\n---\n\n{body}"


class FilesystemAuditTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.mem = self.root / "memory"
        self.mem.mkdir()
        patcher = mock.patch("khipu.paths.ensure_data_dir", return_value=self.root / "data")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, text):
        path = self.mem / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_rsn1_preserves_interleaved_and_trailing_handwriting(self):
        self.write("a.md", _note("A", "body"))
        self.write("b.md", _note("B", "body"))
        original = "# Instructions\n- [A](a.md)\n\nKeep this instruction.\n- [B](b.md)\n## Rules\nNever delete this.\n"
        index = self.write("MEMORY.md", original)
        report = organise.rewrite_index(self.mem, force=True)
        self.assertEqual(index.read_text(), original)
        self.assertTrue(report["skipped"])
        self.assertIn("region", report["reason"])

    def test_rsn1_delimited_region_preserves_surroundings_and_is_idempotent(self):
        self.write("a.md", _note("A", "body"))
        self.write("b.md", _note("B", "body"))
        before = "# Handwritten\n- [Manual](manual.md)\nInstructions before.\n"
        after = "\n## More instructions\nTrailing prose.\n"
        index = self.write("MEMORY.md", before + "<!-- khipu:index:start -->\n- [A custom](a.md)\n<!-- khipu:index:end -->\n" + after)
        first = organise.rewrite_index(self.mem)
        result = index.read_text()
        self.assertTrue(first["ok"])
        self.assertTrue(result.startswith(before))
        self.assertTrue(result.endswith(after))
        self.assertIn("- [A custom](a.md)", result)
        self.assertIn("(b.md)", result)
        self.assertEqual(Path(first["backup_path"]).read_text(), before + "<!-- khipu:index:start -->\n- [A custom](a.md)\n<!-- khipu:index:end -->\n" + after)
        with mock.patch.object(organise, "_backup_index") as backup:
            second = organise.rewrite_index(self.mem)
        self.assertFalse(second["changed"])
        backup.assert_not_called()

    def test_rsn1_malformed_region_refuses_without_writes(self):
        self.write("a.md", _note("A", "body"))
        original = "<!-- khipu:index:start -->\n- [A](a.md)\nUnparsed prose\n"
        index = self.write("MEMORY.md", original)
        with mock.patch.object(organise, "_backup_index") as backup:
            report = organise.rewrite_index(self.mem, force=True)
        self.assertTrue(report["skipped"])
        self.assertEqual(index.read_text(), original)
        backup.assert_not_called()

    def test_rsn2_unreadable_index_or_overflow_refuses_all_mutations(self):
        self.write("a.md", _note("A", "body"))
        for filename in ("MEMORY.md", "_overflow_index.md"):
            with self.subTest(filename=filename):
                index = self.write("MEMORY.md", "- [A](a.md)\n")
                overflow = self.write("_overflow_index.md", "- [Old](old.md)\n")
                target = self.mem / filename
                real_read = Path.read_text

                def read(path, *args, **kwargs):
                    if path == target:
                        raise PermissionError("fixture read denied")
                    return real_read(path, *args, **kwargs)

                with mock.patch.object(Path, "read_text", read), \
                        mock.patch.object(Path, "write_text") as write, \
                        mock.patch.object(Path, "replace") as replace, \
                        mock.patch.object(Path, "unlink") as unlink:
                    report = organise.rewrite_index(self.mem, force=True)
                self.assertFalse(report["ok"])
                self.assertIn("read", report["error"])
                write.assert_not_called()
                replace.assert_not_called()
                unlink.assert_not_called()
                self.assertEqual(index.read_text(), "- [A](a.md)\n")
                self.assertEqual(overflow.read_text(), "- [Old](old.md)\n")

    def test_rsn2_overflow_is_backed_up_before_replacement(self):
        self.write("a.md", _note("A", "body"))
        self.write("MEMORY.md", "- [A](a.md)\n")
        original = "- [Old custom](old.md)\n"
        self.write("_overflow_index.md", original)
        report = organise.rewrite_index(self.mem, force=True)
        self.assertEqual(Path(report["overflow_backup_path"]).read_text(), original)

    def test_rsn3_split_preserves_overview_and_backs_up_original(self):
        original = _note("Ledger", "Overview and handwritten instructions.\n\n## First\nOne.\n\n## Second\nTwo.\n")
        parent = self.write("ledger.md", original)
        report = organise.split_note(parent)
        self.assertTrue(report["ok"])
        self.assertIn("Overview and handwritten instructions.\n\n", parent.read_text())
        self.assertEqual(Path(report["backup_path"]).read_text(), original)
        self.assertIn("## First\nOne.\n\n", Path(report["children"][0]).read_text())
        self.assertIn("## Second\nTwo.\n", Path(report["children"][1]).read_text())
        self.assertTrue(organise.split_note(parent)["skipped"])

    def test_rsn4_normalized_headings_have_unique_children_and_topics(self):
        parent = self.write("ledger.md", _note("Ledger", "## API?\nFirst.\n## API!\nSecond.\n"))
        report = organise.split_note(parent)
        self.assertTrue(report["ok"])
        self.assertEqual(len(set(report["children"])), 2)
        children = [notes._note_topic_dict(Path(p), project=None) for p in report["children"]]
        self.assertEqual(len({c["slug"] for c in children}), 2)
        self.assertIn("First.", children[0]["body"])
        self.assertIn("Second.", children[1]["body"])

    def test_rsn4_existing_child_refuses_without_any_writes(self):
        original = _note("Ledger", "## First\nOne.\n## Second\nTwo.\n")
        parent = self.write("ledger.md", original)
        child = self.write("ledger--second.md", "Handwritten child.\n")
        report = organise.split_note(parent)
        self.assertFalse(report["ok"])
        self.assertIn("exist", report["error"])
        self.assertEqual(parent.read_text(), original)
        self.assertEqual(child.read_text(), "Handwritten child.\n")
        self.assertFalse((self.mem / "ledger--first.md").exists())
        self.assertFalse((self.root / "data").exists())

    def test_split_backup_failure_leaves_all_notes_untouched(self):
        original = _note("Ledger", "## First\nOne.\n## Second\nTwo.\n")
        parent = self.write("ledger.md", original)
        with mock.patch.object(organise, "_backup_index", return_value=None):
            report = organise.split_note(parent)
        self.assertFalse(report["ok"])
        self.assertEqual(parent.read_text(), original)
        self.assertEqual([p.name for p in self.mem.iterdir()], ["ledger.md"])

    def test_split_staging_failure_preserves_parent_and_creates_no_children(self):
        original = _note("Ledger", "## First\nOne.\n## Second\nTwo.\n")
        parent = self.write("ledger.md", original)
        with mock.patch("khipu.organise.tempfile.mkstemp", side_effect=OSError("fixture staging failure")):
            report = organise.split_note(parent)
        self.assertFalse(report["ok"])
        self.assertEqual(parent.read_text(), original)
        self.assertEqual(Path(report["backup_path"]).read_text(), original)
        self.assertEqual([p.name for p in self.mem.iterdir()], ["ledger.md"])

    def test_split_late_child_collision_never_overwrites_and_rolls_back_created_children(self):
        import os

        original = _note("Ledger", "## First\nOne.\n## Second\nTwo.\n")
        parent = self.write("ledger.md", original)
        real_link = os.link

        def link(source, destination):
            if destination.name == "ledger--second.md":
                destination.write_text("Concurrent handwritten child.\n")
            real_link(source, destination)

        with mock.patch("khipu.organise.os.link", side_effect=link):
            report = organise.split_note(parent)
        self.assertFalse(report["ok"])
        self.assertEqual(parent.read_text(), original)
        self.assertEqual((self.mem / "ledger--second.md").read_text(), "Concurrent handwritten child.\n")
        self.assertFalse((self.mem / "ledger--first.md").exists())
        self.assertFalse(list(self.mem.glob(".khipu-split-*")))

    def test_split_malformed_frontmatter_refuses_without_writes(self):
        original = "---\nname: Ledger\n## First\nOne.\n## Second\nTwo.\n"
        parent = self.write("ledger.md", original)
        report = organise.split_note(parent)
        self.assertFalse(report["ok"])
        self.assertEqual(parent.read_text(), original)
        self.assertEqual([p.name for p in self.mem.iterdir()], ["ledger.md"])

    def test_split_refuses_a_child_slug_that_would_overwrite_its_parent_topic(self):
        original = _note("Ledger", "## ???\nOne.\n## Second\nTwo.\n")
        parent = self.write("ledger.md", original)
        report = organise.split_note(parent)
        self.assertFalse(report["ok"])
        self.assertEqual(parent.read_text(), original)
        self.assertEqual([p.name for p in self.mem.iterdir()], ["ledger.md"])

    def test_rsn24_parent_links_equal_child_canonical_slugs(self):
        parent = self.write("big-ledger.md", _note("Big Ledger", "## First Entry\nOne.\n## Second Entry\nTwo.\n"))
        report = organise.split_note(parent)
        parsed = notes._note_topic_dict(parent, project=None)
        child_slugs = {notes._note_topic_dict(Path(p), project=None)["slug"] for p in report["children"]}
        self.assertEqual(set(parsed["links"]), child_slugs)
        graph = GraphCursor()
        with mock.patch("khipu.sources.conversation_memory_enabled", return_value=True):
            topic_graph.persist_topic_graph(graph, parsed)
        self.assertEqual(graph.nodes, {"topic:" + s for s in child_slugs | {parsed["slug"]}})

    def test_rsn25_overflow_is_never_a_note_or_its_own_index_candidate(self):
        for i in range(6):
            self.write(f"n{i}.md", _note(f"N{i}", "body"))
        organise.rewrite_index(self.mem, cap_bytes=120, force=True)
        self.assertNotIn("_overflow_index.md", [p.name for p in notes._iter_note_files(self.mem)])
        first_overflow = (self.mem / "_overflow_index.md").read_text()
        organise.rewrite_index(self.mem, cap_bytes=120, force=True)
        self.assertNotIn("(_overflow_index.md)", (self.mem / "_overflow_index.md").read_text())
        self.assertEqual(first_overflow, (self.mem / "_overflow_index.md").read_text())


class GraphCursor:
    def __init__(self):
        self.nodes = set()
        self.edges = set()
        self.result = []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        if sql.startswith("SELECT id FROM nodes"):
            self.result = [(n,) for n in self.nodes if params is None or n in params[0]]
        elif sql.startswith("SELECT src, dst, type FROM edges"):
            self.result = [e for e in self.edges if e[0] in params[0]]
        elif sql.startswith("SELECT slug") or sql.startswith("SELECT alias"):
            self.result = []
        elif sql.startswith("INSERT INTO nodes"):
            self.nodes.add(params[0])
        elif sql.startswith("INSERT INTO edges"):
            self.edges.add(tuple(params[:3]))
        elif sql.startswith("DELETE FROM edges"):
            self.edges = {e for e in self.edges if e[0] not in params[0] and e[1] not in params[1]}
        elif sql.startswith("DELETE FROM nodes"):
            self.nodes -= set(params[0])
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.result


class HygieneAuditTest(unittest.TestCase):
    def test_rsn5_capture_accepted_extensionless_path_survives_purge(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "docs" / "README"
            path.parent.mkdir()
            path.write_text("real file")
            cur = GraphCursor()
            with mock.patch("khipu.sources.conversation_memory_enabled", return_value=True):
                topic_graph.persist_capture_graph(cur, {"topics": ["manual"], "summary": "Read `docs/README`.", "repo_root": td})
            self.assertIn("path:docs/README", cur.nodes)
            original_edges = set(cur.edges)
            report = hygiene.apply_purge_junk_paths(cur)
        self.assertEqual(report["deleted_nodes"], 0)
        self.assertIn("path:docs/README", cur.nodes)
        self.assertEqual(cur.edges, original_edges)
        self.assertTrue(report["report_only"])

    def test_rsn10_backfill_preserves_project_and_uses_alias_cursor(self):
        class Cur:
            project = "canonical/repo"
            root = None
            rowcount = 1

            def execute(self, sql, params=None):
                if sql.startswith("UPDATE episodes"):
                    self.root = params[0] if self.root is None or "COALESCE" not in sql else self.root
                    self.project = params[1] if self.project is None or "COALESCE" not in sql else self.project

            def fetchall(self):
                return [(1, "/checkout/fork")]

        cur = Cur()
        with mock.patch("khipu.identity.resolve_repo_root", return_value={"repo_root": "/checkout/fork", "project": "fork/repo"}) as resolve:
            report = hygiene.apply_backfill_identity(cur)
        self.assertEqual(cur.root, "/checkout/fork")
        self.assertEqual(cur.project, "canonical/repo")
        resolve.assert_called_once_with("/checkout/fork", cur=cur)
        self.assertEqual(report["updated"], 1)


class NotesCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []

    def execute(self, sql, params=None):
        c = self.connection
        if c.aborted:
            raise RuntimeError("transaction is aborted")
        sql = " ".join(sql.split())
        if sql.startswith("SELECT content_hash"):
            row = c.topics.get(params[0])
            self.result = [(row["content_hash"],)] if row else []
        elif sql.startswith("INSERT INTO topics"):
            if params[0] == c.fail_slug:
                c.aborted = True
                raise RuntimeError("fixture SQL constraint failure")
            slug, title, body, status, _, fm, links, source, digest, _, _ = params
            c.topics[slug] = {"title": title, "body": body, "status": status, "frontmatter": json.loads(fm), "links": json.loads(links), "source_path": source, "content_hash": digest}
        elif sql.startswith("INSERT INTO topic_revisions"):
            c.revisions.append(params)
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.result[0] if self.result else None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NotesConnection:
    def __init__(self):
        self.topics = {}
        self.revisions = []
        self.committed = {}
        self.aborted = False
        self.fail_slug = None

    def cursor(self):
        return NotesCursor(self)

    @contextlib.contextmanager
    def transaction(self):
        snapshot = copy.deepcopy((self.topics, self.revisions))
        try:
            yield
        except Exception:
            self.topics, self.revisions = snapshot
            self.aborted = False
            raise

    def commit(self):
        if self.aborted:
            self.topics = copy.deepcopy(self.committed)
            self.revisions = []
            self.aborted = False
        else:
            self.committed = copy.deepcopy(self.topics)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NotesAuditTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.conn = NotesConnection()
        for patch in (
            mock.patch("khipu.db.connect", return_value=self.conn),
            mock.patch("khipu.mirror.persist_topic_graph", return_value={}),
            mock.patch("khipu.organise.after_reconcile", return_value={"ok": True}),
            mock.patch("khipu.paths.ensure_data_dir", return_value=self.root / "data"),
            mock.patch.object(notes, "claude_projects_root", return_value=self.root / "absent"),
            mock.patch.object(notes, "codex_memories_root", return_value=self.root / "absent"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, project, name, body):
        path = self.root / project / "memory" / "note.md"
        path.parent.mkdir(parents=True)
        path.write_text(_note(name, body))
        return path

    def candidates(self, paths, projects):
        return contextlib.ExitStack(), (
            mock.patch.object(notes, "_iter_note_file_candidates", return_value=[(p, "claude_code", str(i)) for i, p in enumerate(paths)]),
            mock.patch.object(notes, "_project_for_slug", side_effect=lambda slug: projects[int(slug)]),
        )

    def test_rsn7_incremental_collision_keeps_namespaced_rows_and_redirect(self):
        a = self.write("a", "Release Process", "A body")
        b = self.write("b", "Release Process", "B body")
        stack, patches = self.candidates([a, b], ["owner/a", "owner/b"])
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            notes.reconcile(changed_only=True)
            original_slugs = set(self.conn.topics)
            a_slug = next(s for s, row in self.conn.topics.items() if row["body"] == "A body")
            a.write_text(_note("Release Process", "A edited body, longer"))
            report = notes.reconcile(changed_only=True)
        self.assertTrue(report["ok"])
        self.assertEqual(set(self.conn.topics), original_slugs)
        self.assertEqual(self.conn.topics["note:release-process"]["status"], "superseded")
        self.assertEqual(self.conn.topics[a_slug]["body"], "A edited body, longer")

    def test_rsn8_same_project_basename_does_not_merge_topics(self):
        a = self.write("a", "Release Process", "A body")
        b = self.write("b", "Release Process", "B body")
        stack, patches = self.candidates([a, b], ["owner-a/widget", "owner-b/widget"])
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            report = notes.reconcile(changed_only=True)
        self.assertTrue(report["ok"])
        bodies = {row["body"] for row in self.conn.committed.values() if row["status"] != "superseded"}
        self.assertEqual(bodies, {"A body", "B body"})
        self.assertEqual(len(self.conn.committed), 3)

    def test_rsn7_new_collision_moves_the_unchanged_incumbent_too(self):
        a = self.write("a", "Release Process", "A body")
        b = self.write("b", "Release Process", "B body")
        stack, patches = self.candidates([a], ["owner/a", "owner/b"])
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            notes.reconcile(changed_only=True)
            with mock.patch.object(notes, "_iter_note_file_candidates", return_value=[(a, "claude_code", "0"), (b, "claude_code", "1")]):
                report = notes.reconcile(changed_only=True)
        self.assertEqual(report["written"], 2)
        self.assertEqual({r["body"] for r in self.conn.committed.values() if r["status"] != "superseded"}, {"A body", "B body"})

    def test_rsn7_namespace_survives_when_collision_partner_is_removed(self):
        a = self.write("a", "Release Process", "A body")
        b = self.write("b", "Release Process", "B body")
        stack, patches = self.candidates([a, b], ["owner/a", "owner/b"])
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            notes.reconcile(changed_only=True)
            a_slug = next(s for s, r in self.conn.committed.items() if r["body"] == "A body")
            a.write_text(_note("Release Process", "A edited body"))
            with mock.patch.object(notes, "_iter_note_file_candidates", return_value=[(a, "claude_code", "0")]):
                notes.reconcile(changed_only=True)
        self.assertEqual(self.conn.committed[a_slug]["body"], "A edited body")
        self.assertEqual(self.conn.committed["note:release-process"]["status"], "superseded")

    def test_rsn7_previously_distinct_notes_keep_identity_when_content_converges(self):
        a = self.write("a", "Release Process", "A body")
        b = self.write("b", "Release Process", "B body")
        stack, patches = self.candidates([a, b], ["owner/a", "owner/b"])
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            notes.reconcile(changed_only=True)
            b_slug = next(s for s, r in self.conn.committed.items() if r["body"] == "B body")
            b.write_text(a.read_text())
            notes.reconcile(changed_only=True)
        self.assertEqual(self.conn.committed[b_slug]["body"], "A body")
        self.assertEqual(self.conn.committed["note:release-process"]["status"], "superseded")

    def test_rsn9_sql_failure_keeps_success_committed_and_failed_note_retryable(self):
        a = self.write("a", "A", "A body")
        b = self.write("b", "B", "B body")
        self.conn.fail_slug = "note:b"
        stack, patches = self.candidates([a, b], ["owner/a", "owner/b"])
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            failed = notes.reconcile(changed_only=True)
            state = notes._read_state()
            self.assertIn("note:a", self.conn.committed)
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["written"], 1)
            self.assertIn(str(a), state["files"])
            self.assertNotIn(str(b), state["files"])
            self.conn.fail_slug = None
            retried = notes.reconcile(changed_only=True)
        self.assertTrue(retried["ok"])
        self.assertEqual(retried["written"], 1)
        self.assertIn("note:b", self.conn.committed)

    def test_rsn9_redirect_failure_is_reported_and_retried(self):
        a = self.write("a", "Release Process", "A body")
        b = self.write("b", "Release Process", "B body")
        self.conn.fail_slug = "note:release-process"
        stack, patches = self.candidates([a, b], ["owner/a", "owner/b"])
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            failed = notes.reconcile(changed_only=True)
            self.assertFalse(failed["ok"])
            self.assertEqual(len(self.conn.committed), 2)
            self.conn.fail_slug = None
            retried = notes.reconcile(changed_only=True)
        self.assertTrue(retried["ok"])
        self.assertEqual(self.conn.committed["note:release-process"]["status"], "superseded")

    def test_cap3_note_body_metadata_and_revision_are_redacted(self):
        key = "sk-ant-" + "fixturevalue" * 4
        path = self.write("a", "Credentials " + key, "Body " + key)
        stack, patches = self.candidates([path], ["owner/a"])
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            notes.reconcile(changed_only=True)
        serialized = json.dumps([self.conn.committed, self.conn.revisions])
        self.assertNotIn(key, serialized)
        self.assertIn("[REDACTED]", serialized)
        parsed = notes._note_topic_dict(path, project="owner/a")
        from khipu.mirror import topic_content_hash
        self.assertEqual(parsed["digest"], topic_content_hash(path.read_text().replace(key, "[REDACTED]")))
        self.assertIn(key, path.read_text())
