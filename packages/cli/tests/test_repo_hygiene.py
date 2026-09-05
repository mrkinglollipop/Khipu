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
from pathlib import Path

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
            ["git", "ls-files"],
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
                if not path.is_file():
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            for line_no, line in enumerate(text.splitlines(), start=1):
                for needle in FORBIDDEN_STRINGS:
                    if needle in line:
                        offenses.append(f"{rel_path}:{line_no}: contains {needle!r}")

        self.assertEqual(
            offenses,
            [],
            msg="maintainer-specific strings found in tracked files:\n" + "\n".join(offenses),
        )


if __name__ == "__main__":
    unittest.main()
