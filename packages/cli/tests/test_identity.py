"""Tests for khipu.identity — W1.2 stable repo_root/project resolution.

Pure-function tests only: real temporary git repos + worktrees on disk, no
database, no network (remote slug parsing is tested from a local ``origin``
URL set with ``git remote add``, never fetched).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from khipu import identity


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


class GitFixtureTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="khipu-identity-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "main_repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "a@b.com")
        _git(self.repo, "config", "user.name", "test")
        (self.repo / "f.txt").write_text("hi\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "commit", "-qm", "init")


class ResolveRepoRootTest(GitFixtureTestCase):
    def test_plain_checkout_resolves_itself(self):
        out = identity.resolve_repo_root(str(self.repo))
        self.assertEqual(out["repo_root"], str(self.repo.resolve()))
        self.assertFalse(out["is_worktree"])
        # no origin configured -> project falls back to basename
        self.assertEqual(out["project"], "main_repo")

    def test_remote_slug_used_as_project(self):
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
        out = identity.resolve_repo_root(str(self.repo))
        self.assertEqual(out["project"], "acme/widget")

    def test_https_remote_slug(self):
        _git(self.repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
        out = identity.resolve_repo_root(str(self.repo))
        self.assertEqual(out["project"], "acme/widget")

    def test_worktree_under_claude_resolves_to_main_checkout(self):
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
        wt_dir = self.repo / ".claude" / "worktrees" / "feature-x"
        wt_dir.parent.mkdir(parents=True)
        _git(self.repo, "worktree", "add", "-q", str(wt_dir), "-b", "feature-x")
        out = identity.resolve_repo_root(str(wt_dir))
        self.assertEqual(out["repo_root"], str(self.repo.resolve()))
        self.assertTrue(out["is_worktree"])
        self.assertEqual(out["project"], "acme/widget")

    def test_worktree_outside_named_markers_still_detected_via_common_dir(self):
        # A linked worktree anywhere (not just under a recognized marker dir)
        # still has a git-common-dir pointing outside its own toplevel.
        wt_dir = self.tmp / "elsewhere" / "wt"
        wt_dir.parent.mkdir(parents=True)
        _git(self.repo, "worktree", "add", "-q", str(wt_dir), "-b", "other")
        out = identity.resolve_repo_root(str(wt_dir))
        self.assertEqual(out["repo_root"], str(self.repo.resolve()))
        self.assertTrue(out["is_worktree"])

    def test_scratchpad_cwd_never_resolves(self):
        for cwd in ("/tmp/foo", "/private/tmp/foo/bar", "/tmp",
                    "/private/tmp/claude-501/abc/scratchpad"):
            out = identity.resolve_repo_root(cwd)
            self.assertIsNone(out["repo_root"], cwd)
            self.assertIsNone(out["project"], cwd)

    def test_scratchpad_inside_a_real_repo_still_short_circuits(self):
        scratch = self.repo / "tmp" / "notreal"
        # deliberately NOT under /tmp - this one should NOT be treated as
        # scratch, proving the check is about the cwd string, not existence.
        scratch.mkdir(parents=True)
        out = identity.resolve_repo_root(str(scratch))
        self.assertEqual(out["repo_root"], str(self.repo.resolve()))

    def test_non_git_cwd_resolves_to_nothing(self):
        other = self.tmp / "not_a_repo"
        other.mkdir()
        out = identity.resolve_repo_root(str(other))
        self.assertIsNone(out["repo_root"])
        self.assertIsNone(out["project"])

    def test_empty_cwd_is_safe(self):
        out = identity.resolve_repo_root("")
        self.assertIsNone(out["repo_root"])

    def test_no_cursor_never_touches_a_db(self):
        """The default (hook) path: no cursor, no alias lookup at all."""
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
        out = identity.resolve_repo_root(str(self.repo))
        self.assertEqual(out["project"], "acme/widget")

    def test_a_cursor_can_remap_the_resolved_project_via_alias(self):
        """K6: project_aliases lets a renamed/second-checkout project resolve
        to its canonical name, when the caller opts in with a cursor."""
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/widget.git")

        class _Cur:
            def execute(self, sql, params):
                self.params = params

            def fetchone(self):
                return ("acme/widget-canonical",)

        out = identity.resolve_repo_root(str(self.repo), cur=_Cur())
        self.assertEqual(out["project"], "acme/widget-canonical")

    def test_a_failing_alias_lookup_falls_back_to_the_resolved_project(self):
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/widget.git")

        class _BrokenCur:
            def execute(self, sql, params):
                raise RuntimeError("no such table")

        out = identity.resolve_repo_root(str(self.repo), cur=_BrokenCur())
        self.assertEqual(out["project"], "acme/widget")

    def test_no_alias_match_falls_back_to_the_resolved_project(self):
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/widget.git")

        class _EmptyCur:
            def execute(self, sql, params):
                pass

            def fetchone(self):
                return None

        out = identity.resolve_repo_root(str(self.repo), cur=_EmptyCur())
        self.assertEqual(out["project"], "acme/widget")


class NormalizeScopeTest(unittest.TestCase):
    """K6: `episodes.scope` is a free-text FALLBACK, not a project — kept
    only when it still reads like a short label."""

    def test_a_short_label_survives(self):
        self.assertEqual(identity.normalize_scope("build"), "build")
        self.assertEqual(identity.normalize_scope("the seal fix"), "the seal fix")

    def test_a_path_is_dropped(self):
        self.assertIsNone(identity.normalize_scope("/srv/checkouts/acme-widget"))
        self.assertIsNone(identity.normalize_scope("relative/path/here"))
        self.assertIsNone(identity.normalize_scope(r"C:\Users\matt\Khipu"))

    def test_more_than_six_words_is_dropped(self):
        self.assertIsNone(identity.normalize_scope("one two three four five six seven"))

    def test_exactly_six_words_survives(self):
        self.assertEqual(identity.normalize_scope("one two three four five six"), "one two three four five six")

    def test_empty_and_none_are_none(self):
        self.assertIsNone(identity.normalize_scope(""))
        self.assertIsNone(identity.normalize_scope("   "))
        self.assertIsNone(identity.normalize_scope(None))


class SlugFromRemoteUrlTest(unittest.TestCase):
    def test_ssh_form(self):
        self.assertEqual(identity._slug_from_remote_url("git@github.com:acme/widget.git"), "acme/widget")

    def test_https_form(self):
        self.assertEqual(identity._slug_from_remote_url("https://github.com/acme/widget.git"), "acme/widget")

    def test_https_no_dotgit(self):
        self.assertEqual(identity._slug_from_remote_url("https://github.com/acme/widget"), "acme/widget")

    def test_ssh_protocol_form(self):
        self.assertEqual(identity._slug_from_remote_url("ssh://git@host.example/acme/widget.git"), "acme/widget")

    def test_empty(self):
        self.assertIsNone(identity._slug_from_remote_url(""))
        self.assertIsNone(identity._slug_from_remote_url(None))


class GitTimeoutSafetyTest(unittest.TestCase):
    def test_git_call_against_missing_dir_does_not_raise(self):
        # cwd does not exist: git -C <missing> ... fails, must return None not raise
        out = identity.resolve_repo_root("/this/path/does/not/exist/at/all")
        self.assertIsNone(out["repo_root"])


if __name__ == "__main__":
    unittest.main()
