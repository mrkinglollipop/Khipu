"""Pull request references in archived commitment text."""
from __future__ import annotations
import re

_PR_WORD = r"(?:PRs?|pull\s+requests?)"
_PR_NUMBER_RE = re.compile(rf"\b{_PR_WORD}\s*#?\s*(\d{{1,7}})\b", re.I)
# A PR list runs to the end of its clause, however long: a list cut short
# would let an item close while a later PR it names is still open.
_PR_LIST_RE = re.compile(rf"\b{_PR_WORD}\b([^.;:\n]*)", re.I)
# A PR-number run immediately after the PR word: optional leading "(", bare or
# #N items, joined by ",", "&", "and", "/", or "plus". A later "issue #44" in
# the same clause is not part of the run.
_PR_LIST_HASH_RUN_RE = re.compile(
    r"\A\s*\(?\s*(#?\d{1,7}(?:\s*\(?\s*(?:,|&|and|/|plus)\s*\)?\s*#?\d{1,7})*)",
    re.I)
_PR_LIST_ITEM_RE = re.compile(r"#?(\d{1,7})")
_HASH_RE = re.compile(r"(?<![\w&])#(\d{1,7})\b")
_PULL_LINK_RE = re.compile(r"github\.com/([A-Za-z0-9][\w.-]*/[\w.-]+?)/pull/(\d{1,7})\b", re.I)
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")

def _pr_list_hash_hits(tail: str, base: int) -> list[tuple[int, int]]:
    """Bare or ``#N`` positions in a PR list run at the start of ``tail``."""
    run = _PR_LIST_HASH_RUN_RE.match(tail or "")
    if not run:
        return []
    chunk = run.group(1)
    return [(base + run.start(1) + m.start(1), int(m.group(1)))
            for m in _PR_LIST_ITEM_RE.finditer(chunk)]


def parse_prs(text: str) -> list[int]:
    """The pull request numbers ``text`` names, in order, each once: ``PR
    #N``, ``pull request N``, a ``#N`` in a list after "PR"/"PRs", and a
    GitHub pull link. A bare ``#N`` with no "PR" before it (an issue) is not
    one."""
    text = str(text or "")
    found: list[tuple[int, int]] = []
    for m in _PR_NUMBER_RE.finditer(text):
        found.append((m.start(1), int(m.group(1))))
    for m in _PR_LIST_RE.finditer(text):
        found.extend(_pr_list_hash_hits(m.group(1), m.start(1)))
    for m in _PULL_LINK_RE.finditer(text):
        found.append((m.start(2), int(m.group(2))))
    out: list[int] = []
    for _, n in sorted(found):
        if n > 0 and n not in out:
            out.append(n)
    return out


def _repo(value: str | None) -> str | None:
    return str(value).lower() if value else None


def pr_refs(text: str, project_repo: str | None) -> list[tuple[str | None, int]]:
    """The ``(repository, number)`` of each pull request ``text`` names, in
    the order they appear, each once. A GitHub pull link names its own
    repository; any other mention ("PR #41", "#41" in a PR list) is the
    project's repository, None when that is not known. So "PR #41 and
    github.com/other/repo/pull/41" is two refs, both required."""
    text = str(text or "")
    project = _repo(project_repo)
    found: list[tuple[int, str | None, int]] = []
    for m in _PR_NUMBER_RE.finditer(text):
        found.append((m.start(1), project, int(m.group(1))))
    for m in _PR_LIST_RE.finditer(text):
        for pos, n in _pr_list_hash_hits(m.group(1), m.start(1)):
            found.append((pos, project, n))
    for m in _PULL_LINK_RE.finditer(text):
        found.append((m.start(2), _repo(m.group(1)), int(m.group(2))))
    out: list[tuple[str | None, int]] = []
    for _, repo, n in sorted(found, key=lambda f: f[0]):
        if n > 0 and (repo, n) not in out:
            out.append((repo, n))
    return out


def pr_requirement_labels(text: str, project: str | None) -> list[str]:
    """Qualified requirements for every repository, including the project's.

    ``prs`` indexes numbers only; it cannot distinguish two repositories
    that both have PR #41 after the latest wording changes.
    """
    repo = _project_repo(project)
    return [f"{r}#{n}" for r, n in pr_refs(text, repo) if r]


def ref_label(ref: tuple[str | None, int], project_repo: str | None) -> str:
    """``"41"`` for the project's own repository, else ``"owner/repo#41"``."""
    repo, n = ref
    return str(n) if repo == _repo(project_repo) else f"{repo}#{n}"



def _project_repo(project: str | None) -> str | None:
    return project if project and _REPO_RE.match(project) else None
