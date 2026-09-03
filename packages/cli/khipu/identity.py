"""Stable session identity: repo root + project, resolved from a cwd (W1.2).

``scope`` today is whatever the model wrote in the extraction prompt — a free
label like ``"build"`` or an absolute path — so two episodes from the same
conversation can carry different, unfilterable scope strings, and a worktree
checkout or a dispatched child session (cwd under a scratchpad) looks like an
unrelated project. This module resolves the one thing that IS structured and
available from the hook's own cwd, with no model and no network: which git
checkout this is, and what project it belongs to.

Called from the in-session hook (``session_capture.hook_main``), so it must
stay cheap, dependency-free, and safe to run inside Aegis's sandbox: only
``git`` as a subprocess, a bounded timeout, and no exception ever escapes.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

GIT_TIMEOUT_S = 3.0

WORKTREE_MARKERS = ("/.claude/worktrees/", "/.cursor/worktrees/", "/.codex/worktrees/")

_SCRATCHPAD_RE = re.compile(r"/claude-[^/]*/.*scratchpad")


def _is_scratch_cwd(cwd: str) -> bool:
    """cwd that is never a stable project: /tmp, /private/tmp, or a Claude
    scratchpad dir (the harness's own per-session temp area, which can live
    outside /tmp on some installs — matched by name, not just prefix)."""
    p = (cwd or "").rstrip("/")
    if not p:
        return True
    if p == "/tmp" or p == "/private/tmp":
        return True
    if p.startswith("/tmp/") or p.startswith("/private/tmp/"):
        return True
    if _SCRATCHPAD_RE.search(p):
        return True
    return False


def _git(cwd: str, args: list[str], timeout: float = GIT_TIMEOUT_S) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def _slug_from_remote_url(url: str) -> str | None:
    """``owner/repo`` from an https/ssh/scp-style git remote URL, or None."""
    url = (url or "").strip()
    if not url:
        return None
    url = re.sub(r"\.git$", "", url)
    url = url.rstrip("/")
    m = re.search(r"[:/]([^/:]+/[^/:]+)$", url)
    if not m:
        return None
    slug = m.group(1).strip("/")
    return slug or None


def _remote_slug(repo_root: Path) -> str | None:
    url = _git(str(repo_root), ["remote", "get-url", "origin"])
    if not url:
        return None
    return _slug_from_remote_url(url)


def resolve_repo_root(cwd: str) -> dict[str, Any]:
    """{repo_root, project, is_worktree} for a hook's cwd.

    - A path under a Claude/Cursor/Codex worktree tree, or whose git common
      dir lives outside its own toplevel, resolves to the MAIN checkout
      (repo_root is the common dir's parent, not the worktree's own path).
    - project is the git remote's ``owner/repo`` slug when origin is set,
      else the repo root's basename.
    - A scratchpad / /tmp cwd (dispatched-child pattern) never resolves —
      repo_root and project are both None; the caller decides fallback.
    Never raises: any git failure or unexpected shape yields the empty result.
    """
    out: dict[str, Any] = {"repo_root": None, "project": None, "is_worktree": False}
    cwd = (cwd or "").strip()
    if not cwd or _is_scratch_cwd(cwd):
        return out
    try:
        cwd_path = Path(cwd).resolve()
    except OSError:
        cwd_path = Path(cwd)
    toplevel = _git(cwd, ["rev-parse", "--show-toplevel"])
    if not toplevel:
        return out
    try:
        toplevel_path = Path(toplevel).resolve()
    except OSError:
        toplevel_path = Path(toplevel)

    # git's --git-common-dir, when relative, is relative to the invocation
    # CWD (not the toplevel) — resolving it against toplevel_path instead
    # silently walks to the wrong directory whenever cwd is a subdirectory.
    common = _git(cwd, ["rev-parse", "--git-common-dir"])
    main_root = toplevel_path
    is_worktree = False
    if common:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = cwd_path / common_path
        try:
            common_path = common_path.resolve()
        except OSError:
            pass
        if common_path.name == ".git" and common_path.parent != toplevel_path:
            main_root = common_path.parent
            is_worktree = True
    if any(marker in str(toplevel_path) for marker in WORKTREE_MARKERS):
        is_worktree = True

    out["repo_root"] = str(main_root)
    out["is_worktree"] = is_worktree
    slug = _remote_slug(main_root)
    out["project"] = slug or main_root.name
    return out
