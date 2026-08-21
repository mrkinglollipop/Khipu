"""prepr_audit.py — adversarial code-change audit runner for /myauditandfix.

Primary consumer: ``/myauditandfix`` (``--worktree --prepare``). Prepare mode collects
session-scoped diff input and emits a machine-readable bundle for native Task critics —
no Cursor/Fleet dispatch at runtime. Full branch/worktree dispatch modes remain for
backward compatibility.

This workspace does not use GitHub Actions, so no CI consumes this script's output or its
``--post`` PR comment; the comment is informational only.

Modes:
- default (branch): audit committed ``base...HEAD``; stamp ``/tmp/.adversarial_audit_<HEAD>``
  on a clean result; optional informational ``--post`` PR comment. ``--path`` is an
  optional filter. Behavior is unchanged from the pre-worktree version, including the
  truncate-and-dispatch handling of oversized diffs.
- ``--worktree``: session-scoped audit. Requires at least one ``--path``. Collection and
  diffing are restricted to exactly those paths across committed ``base...HEAD``, tracked
  worktree changes vs HEAD (staged + unstaged), and untracked non-ignored files, so
  pre-session WIP elsewhere in the repo is never audited. Stamps
  ``/tmp/.adversarial_audit_worktree_<fingerprint>`` where the fingerprint is the SHA-256
  of the full normalized audit input. ``--post``, ``--pr``, and ``--waive`` are rejected.
- ``--worktree --prepare``: same collection/fingerprinting as worktree but **no** dispatch,
  stamp, or marker. With ``--json``, emits a bundle for dual critics; exit 0 on success
  including no-code. ``--prepare`` requires ``--worktree`` and rejects ``--post``, ``--pr``,
  and ``--waive``.

Repo resolution: the repository root comes from ``git rev-parse --show-toplevel``; every
git call and path operation uses that root, so invocation from any subdirectory produces
the same pathspec-consistent result. Relative ``--path`` values resolve against that root
rather than the invocation cwd. Git runs with ``-c core.quotepath=false`` so non-ASCII
paths stay usable.

argparse CLI: --base BASE, --worktree, --prepare, --path PATH (repeatable), --post, --pr N,
--max-files N (default 3), --json, --waive REASON.

Exit codes (dispatch modes):
- **0** — run completed with no candidate findings (or no in-scope code changed).
- **1** — run completed with candidate findings. These MUST enter the critics/verifier
  artifact pack. Exit 1 alone does not permanently forbid Green: if the latest confirm
  rejects every candidate, Green is still available.
- **2** — usage / repo / path / diff error, including a ``--worktree`` scope where *every*
  supplied ``--path`` is unauditable (directory, non-code extension, or a code-looking path
  git does not know and that is absent on disk). A partly-invalid scope warns and proceeds.
  Deleted-but-tracked paths stay valid; tracked unchanged paths stay valid and simply
  contribute nothing, so an all-unchanged scope is a legitimate no-code clean run.
  Prevents Green:Y.
- **3** — lane or infra failure, empty-input invariant breach, or oversized input.
  Never stamps. Prevents Green:Y.

Prepare mode (``--worktree --prepare``): exit **0** on successful preparation (including
no-code); exit **2** bad arguments/scope; exit **3** collection/oversize/invariant failure.
No exit 1 — adversarial review is owned by Task critics, not this script.

A missing prepare run also prevents Green:Y. Green requires a completed latest prepare
bundle for the current fingerprint plus zero confirmed open HIGH/MEDIUM from critics/
verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR: Path = Path(__file__).resolve().parent

CODE_EXTS: frozenset[str] = frozenset(
    {
        "py",
        "ts",
        "tsx",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "vue",
        "svelte",
        "swift",
        "dart",
        "go",
        "rs",
        "java",
        "kt",
        "kts",
        "rb",
        "scala",
        "c",
        "cc",
        "cpp",
        "cxx",
        "h",
        "hpp",
        "hh",
        "m",
        "mm",
        "sql",
        "sh",
        "bash",
        "zsh",
    }
)

DIFF_CHAR_CAP: int = 180000
TRUNCATION_MARKER: str = "\n...[diff truncated]..."


class PreprError(RuntimeError):
    """Runtime failure with an explicit exit code (2 = usage/repo, 3 = infra)."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def is_code_file(path: str) -> bool:
    """True when *path* has a recognized code extension."""
    if "." not in path:
        return False
    return path.rsplit(".", 1)[-1].lower() in CODE_EXTS


def filter_code_files(paths: list[str]) -> list[str]:
    """Keep unique code paths in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        p = p.strip()
        if not p or p in seen or not is_code_file(p):
            continue
        seen.add(p)
        out.append(p)
    return out


def truncate_diff(diff: str) -> str:
    """Apply the aggregate 180k cap with an explicit truncation marker."""
    if len(diff) <= DIFF_CHAR_CAP:
        return diff
    return diff[:DIFF_CHAR_CAP] + TRUNCATION_MARKER


def fingerprint_input(text: str) -> str:
    """SHA-256 hex digest of the audited diff/input."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def head_marker_path(head: str) -> str:
    return f"/tmp/.adversarial_audit_{head}"


def worktree_marker_path(fingerprint: str) -> str:
    return f"/tmp/.adversarial_audit_worktree_{fingerprint}"


def git_run(args: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
    """Run git with literal path output; surface failures instead of swallowing."""
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except OSError as e:
        raise PreprError(f"git {' '.join(args)} failed to start: {e}", 2) from e


def resolve_repo_root(cwd: str) -> str:
    """Repository top level for *cwd*; the anchor for every later git/path operation."""
    proc = git_run(["rev-parse", "--show-toplevel"], cwd=cwd)
    if proc.returncode != 0:
        raise PreprError(
            f"not inside a git work tree: {proc.stderr.strip() or proc.stdout.strip()}", 2
        )
    root = proc.stdout.strip()
    if not root:
        raise PreprError("git rev-parse --show-toplevel returned an empty path", 2)
    return root


def normalize_scope_paths(repo_root: str, raw_paths: list[str]) -> list[str]:
    """Repo-relative, deduped paths for ``--path`` values.

    Relative values are resolved against the **repository root**, not the invocation
    cwd, so the audited set is identical from any subdirectory (git reports session
    paths root-relative). Absolute paths are accepted. Existence is not required, so
    deleted files stay auditable. Symlinked parents are resolved before the containment
    check; anything landing outside the repository is rejected (exit 2).
    """
    root_real = os.path.realpath(repo_root)
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_paths:
        candidate = raw.strip()
        if not candidate:
            raise PreprError("--path received an empty value", 2)
        abs_path = candidate if os.path.isabs(candidate) else os.path.join(root_real, candidate)
        abs_path = os.path.normpath(abs_path)
        parent, leaf = os.path.split(abs_path)
        parent_real = os.path.realpath(parent) if parent else root_real
        resolved = os.path.join(parent_real, leaf) if leaf else parent_real
        try:
            rel = os.path.relpath(resolved, root_real)
        except ValueError as e:
            raise PreprError(f"--path {raw!r} is not inside the repository: {e}", 2) from e
        if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
            raise PreprError(f"--path {raw!r} resolves outside the repository root {repo_root}", 2)
        if rel == os.curdir:
            raise PreprError(
                f"--path {raw!r} resolves to the repository root; name files, not the root", 2
            )
        rel = rel.replace(os.sep, "/")
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _pathspec(path: str) -> str:
    """Literal pathspec so glob-ish characters in real filenames are not expanded."""
    return f":(literal){path}"


def _git_names(args: list[str], *, repo_root: str, exit_code: int = 2) -> list[str]:
    """Run a NUL-delimited name listing and split it safely."""
    proc = git_run(args, cwd=repo_root)
    if proc.returncode != 0:
        raise PreprError(
            f"git {' '.join(args[:3])} failed: {proc.stderr.strip() or proc.stdout.strip()}",
            exit_code,
        )
    return [name for name in proc.stdout.split("\0") if name]


def resolve_base(repo_root: str, base_arg: str | None) -> str:
    """Resolve merge-base ref from --base, origin/HEAD, or main/master."""
    if base_arg:
        return base_arg
    proc = git_run(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=repo_root)
    if proc.returncode == 0:
        origin_head = proc.stdout.strip()
        if origin_head.startswith("origin/"):
            return origin_head[7:]
        if origin_head:
            return origin_head
    elif proc.stderr.strip():
        print(f"warning: origin/HEAD resolve failed: {proc.stderr.strip()}", file=sys.stderr)

    for candidate in ["main", "master"]:
        proc = git_run(["rev-parse", "--verify", candidate], cwd=repo_root)
        if proc.returncode == 0:
            return candidate
        if proc.stderr.strip():
            print(
                f"warning: could not verify {candidate}: {proc.stderr.strip()}",
                file=sys.stderr,
            )
    raise PreprError("could not resolve base branch", 2)


def collect_branch_code_files(
    repo_root: str, base: str, head: str, scope: list[str] | None = None
) -> list[str]:
    """Code files changed on ``base...head`` (committed only), optionally scoped."""
    args = ["diff", "--name-only", "-z", f"{base}...{head}"]
    if scope:
        args += ["--", *(_pathspec(p) for p in scope)]
    return filter_code_files(_git_names(args, repo_root=repo_root))


def collect_tracked_worktree_code_files(
    repo_root: str, scope: list[str] | None = None
) -> list[str]:
    """Tracked code files differing from HEAD (staged + unstaged), optionally scoped."""
    args = ["diff", "--name-only", "-z", "HEAD"]
    if scope:
        args += ["--", *(_pathspec(p) for p in scope)]
    return filter_code_files(_git_names(args, repo_root=repo_root))


def collect_untracked_code_files(repo_root: str, scope: list[str] | None = None) -> list[str]:
    """Untracked, non-ignored code files, optionally scoped."""
    args = ["ls-files", "--others", "--exclude-standard", "-z"]
    if scope:
        args += ["--", *(_pathspec(p) for p in scope)]
    return filter_code_files(_git_names(args, repo_root=repo_root))


def classify_scope_paths(
    repo_root: str, base: str, head: str, scope: list[str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Split ``--path`` values into ones this oracle can audit and ones it cannot.

    A path is auditable when it has a code extension and either git knows it (index,
    committed branch change, staged/unstaged change, or untracked listing) or a file
    exists on disk. That keeps deleted-but-tracked paths valid, and keeps tracked
    unchanged paths valid — those simply contribute nothing, so a session made only of
    unchanged valid paths ends as a legitimate no-code clean run.

    Returns ``(auditable, [(path, reason), ...])`` preserving *scope* order.
    """
    if not scope:
        return [], []

    specs = [_pathspec(p) for p in scope]
    known: set[str] = set()
    known.update(_git_names(["ls-files", "-z", "--", *specs], repo_root=repo_root))
    known.update(
        _git_names(
            ["ls-files", "--others", "--exclude-standard", "-z", "--", *specs],
            repo_root=repo_root,
        )
    )
    known.update(
        _git_names(
            ["diff", "--name-only", "-z", f"{base}...{head}", "--", *specs], repo_root=repo_root
        )
    )
    known.update(
        _git_names(["diff", "--name-only", "-z", "HEAD", "--", *specs], repo_root=repo_root)
    )

    auditable: list[str] = []
    rejected: list[tuple[str, str]] = []
    for path in scope:
        full = Path(repo_root) / path
        if full.is_dir():
            rejected.append((path, "is a directory; name individual code files"))
            continue
        if not is_code_file(path):
            rejected.append((path, "not a code file extension"))
            continue
        if path not in known and not full.exists():
            rejected.append((path, "unknown to git and absent on disk"))
            continue
        auditable.append(path)
    return auditable, rejected


def collect_worktree_code_files(
    repo_root: str, base: str, head: str, scope: list[str]
) -> list[str]:
    """In-scope code paths with any committed, tracked, or untracked change.

    Order follows *scope* so the audit input — and therefore the fingerprint — is stable.
    """
    changed: set[str] = set()
    changed.update(collect_branch_code_files(repo_root, base, head, scope))
    changed.update(collect_tracked_worktree_code_files(repo_root, scope))
    changed.update(collect_untracked_code_files(repo_root, scope))
    return [p for p in scope if p in changed]


def build_branch_diff(repo_root: str, base: str, head: str, code_files: list[str]) -> str:
    """Committed three-dot diff for *code_files*."""
    if not code_files:
        return ""
    proc = git_run(
        ["diff", f"{base}...{head}", "--", *(_pathspec(p) for p in code_files)],
        cwd=repo_root,
    )
    if proc.returncode != 0:
        raise PreprError(
            f"git diff {base}...{head} failed: {proc.stderr.strip() or proc.stdout.strip()}", 2
        )
    return proc.stdout


def _synthetic_untracked_diff(repo_root: str, rel_path: str) -> str:
    """Synthetic new-file diff (with a real hunk header) for an untracked file.

    Content is never silently trimmed here: a per-file cap could pull an oversized
    session back under the aggregate cap and get it reviewed in part, then reported
    clean. Size is governed once, by the caller's aggregate check.
    """
    full = Path(repo_root) / rel_path
    try:
        raw = full.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise PreprError(f"failed to read untracked file {rel_path}: {e}", 2) from e

    lines = raw.splitlines()
    body_parts = [f"+{line}\n" for line in lines]
    if raw and not raw.endswith("\n"):
        body_parts.append("\\ No newline at end of file\n")

    added = len(lines)
    hunk = f"@@ -0,0 +1,{added} @@\n" if added else "@@ -0,0 +0,0 @@\n"
    return (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{rel_path}\n"
        f"{hunk}"
        f"{''.join(body_parts)}"
    )


def build_worktree_audit_input(repo_root: str, base: str, head: str, scope: list[str]) -> str:
    """Full (untruncated) audit input for in-scope committed + tracked + untracked change.

    The caller fingerprints and size-checks this text before any dispatch, so an oversized
    session fails closed instead of being reviewed in part and reported clean.
    """
    if not scope:
        return ""

    sections: list[str] = []

    committed = set(collect_branch_code_files(repo_root, base, head, scope))
    committed_in_scope = [p for p in scope if p in committed]
    if committed_in_scope:
        cdiff = build_branch_diff(repo_root, base, head, committed_in_scope)
        if cdiff.strip():
            sections.append(f"=== committed ({base}...{head}) ===\n{cdiff.rstrip()}\n")

    tracked = set(collect_tracked_worktree_code_files(repo_root, scope))
    tracked_in_scope = [p for p in scope if p in tracked]
    if tracked_in_scope:
        tw = git_run(
            ["diff", "HEAD", "--", *(_pathspec(p) for p in tracked_in_scope)],
            cwd=repo_root,
        )
        if tw.returncode != 0:
            raise PreprError(f"git diff HEAD failed: {tw.stderr.strip() or tw.stdout.strip()}", 2)
        if tw.stdout.strip():
            sections.append(
                f"=== worktree tracked (HEAD vs working tree) ===\n{tw.stdout.rstrip()}\n"
            )

    untracked = set(collect_untracked_code_files(repo_root, scope))
    untracked_in_scope = [p for p in scope if p in untracked]
    if untracked_in_scope:
        chunks = [_synthetic_untracked_diff(repo_root, p) for p in untracked_in_scope]
        sections.append("=== untracked (synthetic) ===\n" + "\n".join(chunks).rstrip() + "\n")

    return "\n".join(sections)


def _findings_array(text: str) -> list[dict[str, Any]] | None:
    """First balanced ``[...]`` span in *text* that parses to a findings list.

    Scans every ``[`` (a prose preamble may carry a stray bracket before the
    real array) and skips brackets inside JSON string literals, so a finding
    whose ``why``/``evidence`` quotes code containing ``[``/``]`` does not
    miscount depth. Returns the first span that loads to a list of dicts (or
    ``[]``); failing that, the first span that loads to any list.
    """
    fallback: list[dict[str, Any]] | None = None
    for m in re.finditer(r"\[", text):
        depth = 0
        in_str = False
        esc = False
        for j in range(m.start(), len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        arr: Any = json.loads(text[m.start() : j + 1])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        arr = None
                    if isinstance(arr, list):
                        if all(isinstance(x, dict) for x in arr):
                            return arr
                        if fallback is None:
                            fallback = arr
                    break
    return fallback


def parse_findings(text: str) -> list[dict[str, Any]] | None:
    """Tolerantly extract a findings JSON array from cursor-agent / fleet output.

    Handles three wrappings, in order: (1) the ``--output-format json`` envelope,
    a JSON object whose answer sits in a string field (the key name varies by CLI
    version, so known keys are tried first, then any string value holding a
    ``[``); (2) a ```json [ ... ] ``` fenced block; (3) a bare array anywhere in
    prose. Returns the list, or ``None`` when nothing parses (the caller treats
    ``None`` as a lane error and falls back — never a false-clean).
    """
    if not isinstance(text, str) or not text:
        return None
    text = text.strip()

    # (1) Unwrap the cursor-agent / fleet envelope. json.loads un-escapes the
    # inner string so its newlines/quotes are real for the steps below.
    try:
        env: Any = json.loads(text)
        if isinstance(env, list):
            return env
        if isinstance(env, dict):
            picked: str | None = None
            for k in ("result", "output", "content", "response", "text"):
                if isinstance(env.get(k), str):
                    picked = env[k]
                    break
            if picked is None:
                for v in env.values():
                    if isinstance(v, str) and "[" in v:
                        picked = v
                        break
            if picked is not None:
                text = picked.strip()
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # (2) A fenced ```json [ ... ] ``` block, scanned bracket-aware.
    fence = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.DOTALL)
    if fence:
        found = _findings_array(fence.group(1))
        if found is not None:
            return found

    # (3) Bare array anywhere in the text (or the text IS the array).
    return _findings_array(text)


def build_adversarial_prompt(files_list: str, diff: str) -> str:
    """Prompt text for adversarial code review (prepare bundle + full dispatch)."""
    return (
        "Adversarially review ONLY the following changed code for logic bugs, edge cases, "
        "race conditions, security issues, and broken error handling. Try to break it. "
        f"Changed files: {files_list}. Diff:\n{diff}\n"
        "Output ONLY a JSON array as your entire response — no preamble, no explanation, "
        'no markdown code fences: [{"file":..,"line":..,"severity":"high|medium|low","why":..,"evidence":..}]. '
        "Return [] (an empty array) if there is no real issue."
    )


def _prepare_json_bundle(
    *,
    repo_root: str,
    head: str,
    base: str,
    files: list[str],
    scope_paths: list[str],
    fingerprint: str,
    audit_input: str,
    prompt: str,
) -> dict[str, Any]:
    return {
        "repo_root": repo_root,
        "head": head,
        "base": base,
        "files": files,
        "scope_paths": scope_paths,
        "fingerprint": fingerprint,
        "audit_input": audit_input,
        "prompt": prompt,
        "worktree": True,
        "prepared": True,
    }


def _load_fleet_dispatch() -> Any:
    """Lazy fleet import so --prepare works in app repos without fleet.py."""
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from fleet import dispatch as fleet_dispatch
    except ImportError:
        return None
    return fleet_dispatch


def _json_summary(
    *,
    head: str,
    base: str,
    lane: str,
    files: list[str],
    findings: list[dict[str, Any]],
    blocking: list[dict[str, Any]],
    clean: bool,
    marker: str | None,
    posted: bool,
    worktree: bool,
    fingerprint: str | None,
    scope_paths: list[str],
    repo_root: str,
) -> dict[str, Any]:
    return {
        "head": head,
        "base": base,
        "lane": lane,
        "files": files,
        "findings": findings,
        "blocking": blocking,
        "clean": clean,
        "marker": marker,
        "posted": posted,
        "worktree": worktree,
        "fingerprint": fingerprint,
        "scope_paths": scope_paths,
        "repo_root": repo_root,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "prepr_audit.py — adversarial code-change audit for /myauditandfix. "
            "Default audits committed base...HEAD; --worktree audits the session paths "
            "given by --path across committed, staged, unstaged, and untracked change."
        ),
        epilog="See module docstring for full specification and exit-code semantics.",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="base ref; default auto-detect from origin/HEAD or main/master",
    )
    parser.add_argument(
        "--worktree",
        action="store_true",
        help=(
            "session-scoped audit of committed + staged + unstaged + untracked change; "
            "requires >=1 --path; stamps a fingerprint marker (unless --prepare); "
            "rejects --post/--pr/--waive"
        ),
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help=(
            "with --worktree: collect and fingerprint session diff input only; emit "
            "prepare bundle with --json; no dispatch/stamp/marker; rejects --post/--pr/--waive"
        ),
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "repeatable session path to audit; relative values resolve against the "
            "repository root (not the cwd), absolute paths allowed. "
            "Required in --worktree mode; optional filter in branch mode."
        ),
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="if clean, post 'adversarial-audit-ok:<sha>' comment to the open PR "
        "(branch mode only; rejected with --worktree)",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="explicit PR number to use with --post (branch mode only)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=3,
        help="threshold: <=N files uses cursor lane, else fleet lane (default 3)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON summary including worktree/fingerprint/scope fields",
    )
    parser.add_argument(
        "--waive",
        default=None,
        metavar="REASON",
        help=(
            "branch mode only: waive MEDIUM findings with a documented reason (>=12 chars); "
            "HIGH/CRITICAL still block. Rejected with --worktree — worktree candidates are "
            "adjudicated by critics/verifier, not waived here."
        ),
    )
    args = parser.parse_args()

    if args.prepare and not args.worktree:
        print("error: --prepare requires --worktree", file=sys.stderr)
        sys.exit(2)

    if args.worktree or args.prepare:
        rejected = [
            flag
            for flag, used in (
                ("--post", bool(args.post)),
                ("--pr", args.pr is not None),
                ("--waive", args.waive is not None),
            )
            if used
        ]
        if rejected:
            mode = "--prepare" if args.prepare else "--worktree"
            print(
                f"error: {', '.join(rejected)} cannot be combined with {mode} "
                "(worktree/prepare mode does not post PR comments and findings are "
                "adjudicated by the /myauditandfix critics and verifier)",
                file=sys.stderr,
            )
            sys.exit(2)

    invocation_cwd: str = os.getcwd()
    scope_paths: list[str] = []
    fingerprint: str | None = None
    repo_root = ""

    try:
        repo_root = resolve_repo_root(invocation_cwd)

        head_proc = git_run(["rev-parse", "HEAD"], cwd=repo_root)
        if head_proc.returncode != 0:
            raise PreprError(f"failed to get HEAD: {head_proc.stderr.strip()}", 2)
        HEAD: str = head_proc.stdout.strip()
        if not HEAD:
            raise PreprError("git rev-parse HEAD returned an empty sha", 2)

        base: str = resolve_base(repo_root, args.base)

        if args.path:
            scope_paths = normalize_scope_paths(repo_root, args.path)
        elif args.worktree:
            raise PreprError(
                "--worktree requires at least one --path (session file set); "
                "auditing the whole worktree would pull in pre-session WIP",
                2,
            )

        if args.worktree:
            code_scope, rejected_scope = classify_scope_paths(repo_root, base, HEAD, scope_paths)
            if rejected_scope and not code_scope:
                detail = "; ".join(f"{p} ({why})" for p, why in rejected_scope)
                raise PreprError(
                    f"no auditable code path in --path: {detail}. "
                    "Pass the session's individual code files",
                    2,
                )
            for p, why in rejected_scope:
                print(f"warning: skipping --path {p}: {why}", file=sys.stderr)
            code_files = collect_worktree_code_files(repo_root, base, HEAD, code_scope)
            diff = build_worktree_audit_input(repo_root, base, HEAD, code_files)
            fingerprint = fingerprint_input(diff)
        else:
            code_files = collect_branch_code_files(
                repo_root, base, HEAD, filter_code_files(scope_paths) or None
            )
            diff = truncate_diff(build_branch_diff(repo_root, base, HEAD, code_files))
    except PreprError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(e.exit_code)

    if args.prepare:
        if fingerprint is None:
            fingerprint = fingerprint_input(diff)

        def _emit_prepare_and_exit(files: list[str], audit_input: str) -> None:
            prompt = build_adversarial_prompt(", ".join(files), audit_input)
            if args.json:
                print(
                    json.dumps(
                        _prepare_json_bundle(
                            repo_root=repo_root,
                            head=HEAD,
                            base=base,
                            files=files,
                            scope_paths=scope_paths,
                            fingerprint=fingerprint or fingerprint_input(audit_input),
                            audit_input=audit_input,
                            prompt=prompt,
                        ),
                        indent=2,
                    )
                )
            elif not files:
                print("no code changed; prepare bundle ready (empty)")
            else:
                print(f"prepared {len(files)} file(s) for adversarial review")
            sys.exit(0)

        if not code_files:
            _emit_prepare_and_exit([], diff)
        if not diff.strip():
            print(
                "error: in-scope code files were detected but the audit input is empty — "
                f"refusing to prepare a hollow bundle (files: {', '.join(code_files)})",
                file=sys.stderr,
            )
            sys.exit(3)
        if len(diff) > DIFF_CHAR_CAP:
            print(
                f"error: audit input is {len(diff)} chars, over the {DIFF_CHAR_CAP} cap"
                f"{TRUNCATION_MARKER} — refusing to prepare a truncated bundle; "
                "narrow --path scope and re-run",
                file=sys.stderr,
            )
            sys.exit(3)
        _emit_prepare_and_exit(code_files, diff)

    # (4) nothing in scope changed
    if not code_files:
        print("no code changed; nothing to audit")
        marker: str | None = (
            worktree_marker_path(fingerprint)
            if args.worktree and fingerprint is not None
            else head_marker_path(HEAD)
        )
        if marker is None:
            print("error: could not determine marker path", file=sys.stderr)
            sys.exit(3)
        Path(marker).touch(exist_ok=True)
        if args.json:
            print(
                json.dumps(
                    _json_summary(
                        head=HEAD,
                        base=base,
                        lane="none",
                        files=[],
                        findings=[],
                        blocking=[],
                        clean=True,
                        marker=marker,
                        posted=False,
                        worktree=bool(args.worktree),
                        fingerprint=fingerprint,
                        scope_paths=scope_paths,
                        repo_root=repo_root,
                    ),
                    indent=2,
                )
            )
        sys.exit(0)

    # Fail closed: files changed but no readable diff means the pathspec/root resolution
    # produced nothing to review. Dispatching that would return a false clean.
    if not diff.strip():
        print(
            "error: in-scope code files were detected but the audit input is empty — "
            f"refusing to dispatch a hollow audit (files: {', '.join(code_files)})",
            file=sys.stderr,
        )
        sys.exit(3)

    # Worktree mode never reviews a partial diff: an oversized session fails closed.
    if args.worktree and len(diff) > DIFF_CHAR_CAP:
        print(
            f"error: audit input is {len(diff)} chars, over the {DIFF_CHAR_CAP} cap"
            f"{TRUNCATION_MARKER} — refusing to dispatch a truncated audit as clean; "
            "narrow --path scope and re-run",
            file=sys.stderr,
        )
        sys.exit(3)

    # (6) lane
    lane: str = "cursor" if len(code_files) <= args.max_files else "fleet"

    # (7) PROMPT
    files_list: str = ", ".join(code_files)
    PROMPT: str = build_adversarial_prompt(files_list, diff)

    # (8)(9) run lane
    findings: list[dict[str, Any]] = []
    error_occurred: bool = False
    if lane == "cursor":
        cursor_script: str = str(SCRIPT_DIR / "cursor_dispatch.py")
        try:
            input_json: str = json.dumps(
                {
                    "mode": "audit",
                    "workspace": repo_root,
                    "model": "auto",
                    "prompt": PROMPT,
                }
            )
            proc = subprocess.run(
                ["python3", cursor_script],
                input=input_json,
                capture_output=True,
                text=True,
            )
            rc: int = proc.returncode
            stdout: str = proc.stdout
            if rc == 3:
                lane = "fleet"
            elif rc == 2:
                print("audit lane blocked sensitive data", file=sys.stderr)
                if args.json:
                    print(
                        json.dumps(
                            _json_summary(
                                head=HEAD,
                                base=base,
                                lane="cursor",
                                files=code_files,
                                findings=[],
                                blocking=[],
                                clean=False,
                                marker=None,
                                posted=False,
                                worktree=bool(args.worktree),
                                fingerprint=fingerprint,
                                scope_paths=scope_paths,
                                repo_root=repo_root,
                            ),
                            indent=2,
                        )
                    )
                sys.exit(3)
            else:
                raw_findings = parse_findings(stdout)
                if raw_findings is None:
                    # Format hiccup, not unavailability: the dispatch succeeded
                    # but stdout carried no extractable array. Degrade to the
                    # fleet lane (structured-summary path) instead of dead-ending
                    # — the same recovery as rc==3, and never a false-clean.
                    print(
                        "audit lane parse failure — falling back to fleet lane",
                        file=sys.stderr,
                    )
                    lane = "fleet"
                else:
                    findings = raw_findings
        except Exception as e:
            print(f"Cursor lane error: {e}", file=sys.stderr)
            error_occurred = True

    if lane == "fleet":
        try:
            fleet_dispatch = _load_fleet_dispatch()
            if fleet_dispatch is None:
                raise ImportError("fleet module not importable (sibling fleet.py missing?)")
            work_items: list[dict[str, Any]] = [
                {
                    "work_id": "prepr-audit",
                    "work_class": "code_review",
                    "spec": PROMPT,
                    "sandbox": repo_root,
                }
            ]
            results: dict[str, Any] = fleet_dispatch(work_items)
            if isinstance(results, dict) and "results" in results and results["results"]:
                first_result: dict[str, Any] = results["results"][0]
                summary_str: str = first_result.get("summary", "")
                raw_findings = parse_findings(summary_str)
                if raw_findings is None:
                    print(
                        "error: fleet lane returned no parseable findings array",
                        file=sys.stderr,
                    )
                    error_occurred = True
                else:
                    findings = raw_findings
            else:
                print(
                    "error: fleet lane returned empty or malformed results",
                    file=sys.stderr,
                )
                error_occurred = True
        except Exception as e:
            print(f"Fleet lane error: {e}", file=sys.stderr)
            error_occurred = True

    if error_occurred:
        if args.json:
            print(
                json.dumps(
                    _json_summary(
                        head=HEAD,
                        base=base,
                        lane=lane,
                        files=code_files,
                        findings=[],
                        blocking=[],
                        clean=False,
                        marker=None,
                        posted=False,
                        worktree=bool(args.worktree),
                        fingerprint=fingerprint,
                        scope_paths=scope_paths,
                        repo_root=repo_root,
                    ),
                    indent=2,
                )
            )
        sys.exit(3)

    # (10) normalize findings — map severity to a known scale. An unrecognized
    # or missing label becomes "medium" (blocking), so an unexpected severity
    # string can never silently pass the gate. Only an explicit "low" is benign.
    valid_sev = ("critical", "high", "medium", "low")
    normalized: list[dict[str, Any]] = []
    for item in findings:
        if isinstance(item, dict):
            norm: dict[str, Any] = dict(item)
            sev = norm.get("severity")
            if isinstance(sev, str) and sev.lower() in valid_sev:
                norm["severity"] = sev.lower()
            else:
                norm["severity"] = "medium"
            normalized.append(norm)
    findings = normalized

    hard_block: list[dict[str, Any]] = [
        f for f in findings if f.get("severity") in ("critical", "high")
    ]
    medium_block: list[dict[str, Any]] = [f for f in findings if f.get("severity") == "medium"]
    waive_reason: str = (args.waive or "").strip()
    waiving: bool = bool(medium_block) and not hard_block and len(waive_reason) >= 12
    blocking: list[dict[str, Any]] = hard_block + ([] if waiving else medium_block)
    low_count: int = sum(1 for f in findings if f.get("severity") == "low")

    # (11) candidate findings — exit 1
    if blocking:
        for f in blocking:
            sev_s: str = f.get("severity", "low").upper()
            file_s: str = f.get("file", "unknown")
            line_s: str | int = f.get("line", "?")
            why_s: str = f.get("why", "")
            print(f"[{sev_s}] {file_s}:{line_s} - {why_s}")
        if args.worktree:
            print(
                f"AUDIT FOUND {len(blocking)} candidate finding(s) — put them in the "
                "/myauditandfix artifact pack for critic + verifier adjudication."
            )
        else:
            print(f"AUDIT FOUND {len(blocking)} issue(s) — fix before shipping.")
            if hard_block and waive_reason:
                print("--waive covers MEDIUM only; HIGH/CRITICAL must be fixed.")
        if args.json:
            print(
                json.dumps(
                    _json_summary(
                        head=HEAD,
                        base=base,
                        lane=lane,
                        files=code_files,
                        findings=findings,
                        blocking=blocking,
                        clean=False,
                        marker=None,
                        posted=False,
                        worktree=bool(args.worktree),
                        fingerprint=fingerprint,
                        scope_paths=scope_paths,
                        repo_root=repo_root,
                    ),
                    indent=2,
                )
            )
        sys.exit(1)

    if waiving:
        print(f"WAIVED {len(medium_block)} medium finding(s) — reason: {waive_reason}")
        for f in medium_block:
            file_w = f.get("file", "unknown")
            line_w = f.get("line", "?")
            why_w = f.get("why", "")
            print(f"  [waived MEDIUM] {file_w}:{line_w} - {why_w}")

    # (12) completed with no candidates — stamp (HEAD for branch mode; fingerprint otherwise)
    if args.worktree:
        if fingerprint is None:
            print("error: worktree fingerprint missing at stamp time", file=sys.stderr)
            sys.exit(3)
        marker = worktree_marker_path(fingerprint)
    else:
        marker = head_marker_path(HEAD)
    Path(marker).touch(exist_ok=True)
    posted: bool = False
    if args.post:
        pr_num: int | str | None = args.pr
        if pr_num is None:
            try:
                gh_result = subprocess.run(
                    ["gh", "pr", "view", "--json", "number", "-q", ".number"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                gh_out = gh_result.stdout.strip()
                if gh_out:
                    pr_num = gh_out
            except Exception as e:
                print(f"warning: failed to determine PR number: {e}", file=sys.stderr)
                pr_num = None
        if pr_num:
            if waiving:
                # Distinct prefix so a reader scanning for "adversarial-audit-ok:" does NOT
                # treat a WAIVED run as clean — waiving requires Matt's explicit authorization
                # and must never auto-clear on open MEDIUM findings.
                body = (
                    f"adversarial-audit-waived:{HEAD}\n\n"
                    f"Pre-PR adversarial audit: {len(medium_block)} MEDIUM finding(s) "
                    f"WAIVED ({lane}, {low_count} low-severity note(s)).\n"
                    f"Waive reason: {waive_reason}"
                )
            else:
                body = (
                    f"adversarial-audit-ok:{HEAD}\n\n"
                    f"Pre-PR adversarial audit clean ({lane}, {low_count} low-severity note(s))."
                )
            try:
                subprocess.run(
                    ["gh", "pr", "comment", str(pr_num), "--body", body],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                posted = True
            except Exception as e:
                print(f"warning: failed to post comment: {e}", file=sys.stderr)
    audit_status: str = (
        "AUDIT WAIVED — marker stamped" if waiving else "AUDIT CLEAN — marker stamped"
    )
    print(audit_status + (" + comment posted" if posted else ""))
    if args.json:
        print(
            json.dumps(
                _json_summary(
                    head=HEAD,
                    base=base,
                    lane=lane,
                    files=code_files,
                    findings=findings,
                    blocking=blocking,
                    clean=not waiving,
                    marker=marker,
                    posted=posted,
                    worktree=bool(args.worktree),
                    fingerprint=fingerprint,
                    scope_paths=scope_paths,
                    repo_root=repo_root,
                ),
                indent=2,
            )
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
