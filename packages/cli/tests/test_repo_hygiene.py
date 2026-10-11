"""Public-repo hygiene gate — no maintainer-specific paths/names in tracked files.

Khipu is a public repo (mrkinglollipop/Khipu). This test walks every
git-tracked file and asserts none of them carry the maintainer's private
filesystem paths or private-project names, so a future edit can't
accidentally reintroduce one. The two guard tests below are exempt because
their whole job is to assert these strings are ABSENT elsewhere — they
legitimately hold the literal strings as negative-match fixtures.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]

FORBIDDEN_STRINGS = (
    "/Volumes/Cloud Storage",
    "/Users/matthewschwartz",
    "king-lollipop-studio",
    "frozen-threshold",
    "ft-terminal",
    "Mrs Lollipop",
    "mrs-lollipop",
    "UNIFICATION",
)

ALLOWLISTED_FILES = {
    "packages/cli/tests/test_jobs_paths.py",
    "packages/cli/tests/test_portable_doctor.py",
    "packages/cli/tests/test_repo_hygiene.py",
}

MAX_FILE_BYTES = 2 * 1024 * 1024


def _git_available() -> bool:
    if shutil.which("git") is None:
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


GIT_AVAILABLE = _git_available()


@unittest.skipUnless(GIT_AVAILABLE, "git unavailable or not a git work tree")
class RepoHygieneTest(unittest.TestCase):
    def test_no_maintainer_specific_strings_in_tracked_files(self) -> None:
        result = subprocess.run(
            ["git", "ls-files", "--", ".", ":(exclude)**/API Keys/**", ":(exclude)**/My Portfolio/**"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        tracked = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(tracked, "git ls-files returned no tracked files")

        offenses: list[str] = []
        for rel_path in tracked:
            if rel_path in ALLOWLISTED_FILES:
                continue
            path = REPO_ROOT / rel_path
            try:
                seen = set()
                overlap = max(len(s.encode()) for s in FORBIDDEN_STRINGS) - 1
                tail = b""
                with path.open("rb") as fh:
                    while chunk := fh.read(MAX_FILE_BYTES):
                        data = tail + chunk
                        for needle in FORBIDDEN_STRINGS:
                            if needle.encode() in data:
                                seen.add(needle)
                        tail = data[-overlap:]
                offenses.extend(f"{rel_path}: contains {needle!r}" for needle in sorted(seen))
            except OSError as exc:
                offenses.append(f"{rel_path}: incomplete scan ({type(exc).__name__})")

        self.assertEqual(
            offenses,
            [],
            msg="maintainer-specific strings found in tracked files:\n" + "\n".join(offenses),
        )


if __name__ == "__main__":
    unittest.main()


class HygieneCoverageTest(unittest.TestCase):
    def _scan_fixture(self, root):
        with mock.patch(__name__ + ".REPO_ROOT", root), mock.patch(
            "subprocess.run", return_value=mock.Mock(stdout="fixture.bin\n")
        ):
            RepoHygieneTest("test_no_maintainer_specific_strings_in_tracked_files").test_no_maintainer_specific_strings_in_tracked_files()

    def test_large_and_binary_files_are_checked(self):
        for prefix in (b"x" * (MAX_FILE_BYTES - 3), b"\xff\xfe"):
            with self.subTest(size=len(prefix)), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "fixture.bin").write_bytes(prefix + FORBIDDEN_STRINGS[0].encode())
                with self.assertRaisesRegex(AssertionError, "contains"):
                    self._scan_fixture(root)

    def test_unreadable_files_fail_the_gate_explicitly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fixture.bin").write_bytes(b"safe")
            with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(AssertionError, "incomplete"):
                    self._scan_fixture(root)
