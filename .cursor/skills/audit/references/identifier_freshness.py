#!/usr/bin/env python3
"""Identifier freshness for /verify-plan — grep load-bearing plan tokens against target repos."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

IDENTIFIER_CAP = 40

CODE_SUFFIXES = (
    ".tsx",
    ".ts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".py",
    ".go",
    ".rs",
    ".swift",
    ".java",
    ".kt",
    ".rb",
    ".sh",
    ".bash",
    ".zsh",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".md",
    ".sql",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".php",
    ".lua",
    ".vue",
    ".svelte",
)

SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "out",
    ".next",
    ".nuxt",
    "target",
    "DerivedData",
    ".turbo",
    ".venv",
    "venv",
    ".tox",
}

DENYLIST = frozenset(
    {
        "track",
        "role",
        "high",
        "medium",
        "low",
        "green",
        "matt",
        "none",
        "true",
        "false",
        "plan",
        "session",
        "audit",
        "verify",
        "loop",
        "skill",
        "skills",
        "cursor",
        "orchestrator",
        "verifier",
        "critic",
        "critics",
        "delta",
        "phase",
        "round",
        "todo",
        "todos",
        "done",
        "wip",
        "draft",
        "shipped",
        "merged",
        "blocked",
        "verified",
        "unverified",
        "inferred",
        "hard",
        "soft",
        "yes",
        "no",
        "n/a",
        "na",
        "fix",
        "bug",
        "hunt",
        "bust",
        "claim",
        "claims",
        "ledger",
        "freshness",
        "oracle",
        "oracles",
        "prepr",
        "confirm",
        "skip",
        "full",
        "quick",
        "loop.md",
        "skill.md",
        "verify-plan",
        "myauditandfix",
        "session-auditor",
        "audit-verifier",
        "identifier_freshness.py",
        "prepr_audit.py",
        "sync-harness.sh",
        "smoke_harness_skills.sh",
        "smoke_harness_agents.sh",
        "harNESS.md",
        "harness.md",
        "agents.md",
        "conduct.mdc",
        "master.mdc",
        "orchestration.mdc",
        "audit.mdc",
        "deliverable",
        "contract",
        "scope",
        "target",
        "files",
        "path",
        "paths",
        "root",
        "roots",
        "repo",
        "repos",
        "code",
        "claude",
        "workspace",
        "plugin",
        "dispatch",
        "composer",
        "grok",
        "omit",
        "pin",
        "model",
        "task",
        "readonly",
        "write",
        "read",
        "edit",
        "edits",
        "markdown",
        "json",
        "yaml",
        "toml",
        "python",
        "bash",
        "shell",
        "git",
        "pytest",
        "ruff",
        "xcodebuild",
        "npm",
        "node",
        "docker",
        "linux",
        "macos",
        "windows",
        "string",
        "strings",
        "int",
        "bool",
        "null",
        "undefined",
        "void",
        "type",
        "types",
        "class",
        "function",
        "const",
        "let",
        "var",
        "import",
        "export",
        "from",
        "return",
        "async",
        "await",
        "new",
        "this",
        "self",
        "def",
        "elif",
        "else",
        "for",
        "while",
        "with",
        "not",
        "and",
        "or",
        "in",
        "is",
        "as",
        "if",
        "the",
        "and",
        "but",
        "you",
        "your",
        "must",
        "should",
        "will",
        "can",
        "may",
        "when",
        "where",
        "what",
        "which",
        "that",
        "this",
        "these",
        "those",
        "then",
        "than",
        "into",
        "from",
        "over",
        "under",
        "after",
        "before",
        "each",
        "every",
        "all",
        "any",
        "some",
        "only",
        "also",
        "just",
        "still",
        "already",
        "never",
        "always",
        "same",
        "other",
        "such",
        "very",
        "more",
        "most",
        "less",
        "least",
        "here",
        "there",
        "how",
        "why",
        "who",
        "whom",
        "whose",
    }
)

RECANT_MARKERS = (
    "EMPIRICAL CORRECTION",
    "do not hook",
    "does NOT fire",
    "does not fire",
    "recant",
)

LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
BACKTICK_RE = re.compile(r"`([^`]{2,})`")
# "/Volumes/Cloud Storage/..." has a space; a [^\s]+ class splits it into
# "/Volumes/Cloud" plus CODE_PATH_RE fragment "Storage/Code/...". First alt
# continues through later spaced segments (same idea as alt 2). Second alt
# allows other spaced abs paths only when a "/" segment follows the space.
# A spaced directory component may be multiple words ("some big repo/App.tsx")
# and may include parentheses ("some big repo (legacy)/App.tsx").
# ` +` (not a single space) keeps "some  repo/App.tsx" (double space, one extra
# word) as one path; do not tighten that to exactly one space.
_ABS_SEG = r"[^\s`\"'<>|]+"
_ABS_WORD = r"[A-Za-z0-9_.()-]+"
_ABS_SPACED_TAIL = rf"(?: +{_ABS_WORD}(?: +{_ABS_WORD})*(?:/{_ABS_SEG})+)*"
ABS_PATH_RE = re.compile(
    rf"("
    rf"/Volumes/Cloud Storage(?:/{_ABS_SEG})*{_ABS_SPACED_TAIL}|"
    rf"/{_ABS_SEG}{_ABS_SPACED_TAIL}"
    rf")"
)
_SUFFIX_ALT = "|".join(
    re.escape(s.lstrip(".")) for s in sorted(CODE_SUFFIXES, key=len, reverse=True)
)
CODE_PATH_RE = re.compile(
    rf"(?:^|[\s(\[\"'`])([A-Za-z0-9_./-]+\.(?:{_SUFFIX_ALT}))(?:[:\d]+)?",
    re.MULTILINE,
)
CODE_ROOT_RE = re.compile(r"/Volumes/Cloud Storage/Code/([A-Za-z0-9_.-]+)")
SLUG_RE = re.compile(r"[^a-z0-9]+")


def strip_line_suffix(token: str) -> str:
    return LINE_SUFFIX_RE.sub("", token.strip())


def is_denied(token: str) -> bool:
    bare = token.strip().strip("`\"'")
    if not bare or len(bare) < 2:
        return True
    lower = bare.lower()
    if lower in DENYLIST:
        return True
    base = Path(bare).name.lower()
    if base in DENYLIST:
        return True
    if base.endswith(".md") and base.replace(".md", "") in DENYLIST:
        return True
    return False


def token_priority(token: str) -> tuple[int, int]:
    """Lower sort key = higher priority."""
    if "/" in token or token.startswith("."):
        return (0, -len(token))
    if "_" in token or any(c.isupper() for c in token[1:]):
        return (1, -len(token))
    return (2, -len(token))


def make_claim_id(token: str) -> str:
    slug = SLUG_RE.sub("-", token.lower().strip())[:80].strip("-")
    if not slug:
        slug = "token"
    return f"id-{slug}"


def extract_identifiers(plan_text: str) -> tuple[list[str], list[str]]:
    seen: set[str] = set()
    candidates: list[str] = []

    def add(raw: str) -> None:
        token = strip_line_suffix(raw.strip().strip("`\"'"))
        if not token or len(token) < 2:
            return
        key = token.lower()
        if key in seen:
            return
        if is_denied(token):
            return
        seen.add(key)
        candidates.append(token)

    backtick_spans: list[tuple[int, int]] = [
        (m.start(1), m.end(1)) for m in BACKTICK_RE.finditer(plan_text)
    ]
    abs_matches = list(ABS_PATH_RE.finditer(plan_text))
    abs_spans = [(m.start(1), m.end(1)) for m in abs_matches]
    protected_spans = [*backtick_spans, *abs_spans]

    for match in BACKTICK_RE.finditer(plan_text):
        add(match.group(1))

    for match in CODE_PATH_RE.finditer(plan_text):
        start, end = match.span(1)
        if any(start < pe and ps < end for ps, pe in protected_spans):
            continue
        add(match.group(1))

    for match in abs_matches:
        start, end = match.span(1)
        if any(start < pe and ps < end for ps, pe in backtick_spans):
            continue
        if any(start >= ps and end <= pe and (end - start) < (pe - ps) for ps, pe in abs_spans):
            continue
        path = match.group(1).rstrip(".,;:)")
        if any(path.endswith(suffix) for suffix in CODE_SUFFIXES):
            add(path)

    candidates.sort(key=token_priority)

    kept: list[str] = []
    dropped: list[str] = []
    for token in candidates:
        if len(kept) < IDENTIFIER_CAP:
            kept.append(token)
        else:
            dropped.append(token)
    return kept, dropped


def discover_auto_roots(plan_text: str, cwd: Path) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def add_root(path: Path) -> None:
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            return
        if resolved.is_dir():
            seen.add(key)
            roots.append(resolved)

    for match in ABS_PATH_RE.finditer(plan_text):
        raw = match.group(1).rstrip(".,;:)")
        candidate = Path(raw)
        if candidate.is_dir():
            add_root(candidate)
        elif candidate.is_file():
            add_root(candidate.parent)

    for match in CODE_ROOT_RE.finditer(plan_text):
        repo_name = match.group(1)
        candidate = Path("/Volumes/Cloud Storage/Code") / repo_name
        add_root(candidate)

    if not roots:
        add_root(cwd.resolve())
    return roots


def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES


class SearchError(Exception):
    """rg spawn or IO failure — not the same as a successful ZERO_HITS search."""


def rg_available() -> bool:
    return shutil.which("rg") is not None


def _rg_skip_globs() -> list[str]:
    globs: list[str] = []
    for name in sorted(SKIP_DIR_NAMES):
        globs.extend(["-g", f"!{name}/**"])
    return globs


def search_rg(query: str, roots: list[Path]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    skip = _rg_skip_globs()
    for root in roots:
        cmd = [
            "rg",
            "-n",
            "-F",
            "--no-heading",
            "--color=never",
            "--no-ignore",
            *skip,
            query,
            str(root),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SearchError(f"rg spawn failed under {root}: {exc}") from exc
        if proc.returncode not in (0, 1):
            err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            raise SearchError(f"rg failed under {root}: {err}")
        for line in proc.stdout.splitlines():
            if ":" not in line:
                continue
            path_part, line_no, content = line.split(":", 2)
            try:
                line_num = int(line_no)
            except ValueError:
                continue
            hits.append((Path(path_part), line_num, content))
    return hits


def looks_like_path(query: str) -> bool:
    if "/" in query or query.startswith("."):
        return True
    return any(query.endswith(suffix) for suffix in CODE_SUFFIXES)


def search_by_filename(query: str, roots: list[Path]) -> list[tuple[Path, int, str]]:
    if not looks_like_path(query):
        return []
    basename = Path(query).name
    if (
        not basename
        or basename == query
        and "/" not in query
        and not any(query.endswith(s) for s in CODE_SUFFIXES)
    ):
        return []
    hits: list[tuple[Path, int, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob(basename):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if not lines:
                hits.append((path, 1, ""))
                continue
            for idx, line in enumerate(lines, start=1):
                if line.strip():
                    hits.append((path, idx, line))
                    break
            else:
                hits.append((path, 1, lines[0]))
    return hits


def search_python(query: str, roots: list[Path]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    hits.append((path, idx, line))
    return hits


def file_has_recant(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lower = text.lower()
    return any(marker.lower() in lower for marker in RECANT_MARKERS)


def extract_recant_slice(path: Path, near_line: int, context: int = 3) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    lower_lines = [ln.lower() for ln in lines]
    recant_indices = [
        i
        for i, ln in enumerate(lower_lines)
        if any(marker.lower() in ln for marker in RECANT_MARKERS)
    ]
    if not recant_indices:
        start = max(0, near_line - 1 - context)
        end = min(len(lines), near_line + context)
        return "\n".join(lines[start:end])

    nearest = min(recant_indices, key=lambda i: abs(i - (near_line - 1)))
    start = max(0, nearest - context)
    end = min(len(lines), nearest + context + 1)
    return "\n".join(lines[start:end])


def pick_best_hit(hits: list[tuple[Path, int, str]], query: str) -> tuple[Path, int, str] | None:
    if not hits:
        return None

    recant_hits = [h for h in hits if file_has_recant(h[0])]
    pool = recant_hits or hits

    def score(hit: tuple[Path, int, str]) -> tuple[int, int, int]:
        path, line_no, content = hit
        lower_content = content.lower()
        recant_bonus = 0
        if file_has_recant(path):
            recant_bonus = -1000
        near_recant = 0
        if any(marker.lower() in lower_content for marker in RECANT_MARKERS):
            near_recant = -500
        path_bonus = 0
        if query in path.name:
            path_bonus = -100
        return (recant_bonus + near_recant + path_bonus, abs(line_no), len(str(path)))

    return min(pool, key=score)


def format_hit_section(
    claim_id: str,
    query: str,
    hit: tuple[Path, int, str] | None,
    *,
    error: str | None = None,
) -> str:
    lines = [f"### {claim_id}", f"- **query:** `{query}`"]
    if error:
        lines.append("- **result:** SEARCH_ERROR")
        lines.append(f"- **error:** {error}")
        return "\n".join(lines) + "\n"
    if hit is None:
        lines.append("- **result:** ZERO_HITS")
        return "\n".join(lines) + "\n"

    path, line_no, content = hit
    slice_text = extract_recant_slice(path, line_no)
    lines.append(f"- **hit:** `{path}:{line_no}`")
    lines.append(f"- **line:** {content.strip()}")
    if slice_text:
        lines.append("- **slice:**")
        lines.append("```")
        lines.append(slice_text)
        lines.append("```")
    return "\n".join(lines) + "\n"


def write_notes(
    out_path: Path,
    identifiers: list[str],
    dropped: list[str],
    sections: list[str],
    roots: list[Path],
) -> None:
    body: list[str] = [
        "# Identifier freshness notes",
        "",
        f"- **roots:** {', '.join(str(r) for r in roots) if roots else '(none)'}",
        "",
    ]
    if not identifiers:
        body.append("## NO_IDENTIFIERS")
        body.append("")
        body.append("No load-bearing identifiers extracted from plan after denylist.")
    else:
        body.extend(sections)
    if dropped:
        body.append("## DROPPED_FOR_CAP")
        body.append("")
        for token in dropped:
            body.append(f"- `{token}`")
        body.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(body), encoding="utf-8")


def resolve_roots(cli_roots: list[str], plan_text: str, cwd: Path) -> list[Path]:
    if cli_roots:
        return [Path(r).resolve() for r in cli_roots]
    return discover_auto_roots(plan_text, cwd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan identifier freshness grep for /verify-plan")
    parser.add_argument("--plan", required=True, help="Path to plan markdown")
    parser.add_argument("--root", action="append", default=[], help="Search root (repeatable)")
    parser.add_argument("--out", required=True, help="Output notes markdown path")
    parser.add_argument("--cwd", default=".", help="Fallback cwd when no roots in plan")
    args = parser.parse_args(argv)

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"ERROR: unreadable plan: {plan_path}", file=sys.stderr)
        return 1

    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: unreadable plan: {plan_path}: {exc}", file=sys.stderr)
        return 1

    cwd = Path(args.cwd).resolve()
    roots = resolve_roots(args.root, plan_text, cwd)
    readable_roots = [r for r in roots if r.is_dir()]
    if args.root and not readable_roots:
        print("ERROR: every --root unreadable", file=sys.stderr)
        return 1
    if not readable_roots:
        readable_roots = [cwd]

    identifiers, dropped = extract_identifiers(plan_text)
    search_fn = search_rg if rg_available() else search_python

    sections: list[str] = []
    search_failed = False
    for token in identifiers:
        claim_id = make_claim_id(token)
        try:
            hits = search_fn(token, readable_roots)
            if looks_like_path(token):
                seen = {(str(p), ln) for p, ln, _ in hits}
                for hit in search_by_filename(token, readable_roots):
                    key = (str(hit[0]), hit[1])
                    if key not in seen:
                        hits.append(hit)
                        seen.add(key)
            best = pick_best_hit(hits, token)
            sections.append(format_hit_section(claim_id, token, best))
        except SearchError as exc:
            search_failed = True
            print(f"ERROR: {exc}", file=sys.stderr)
            sections.append(format_hit_section(claim_id, token, None, error=str(exc)))

    out_path = Path(args.out)
    write_notes(out_path, identifiers, dropped, sections, readable_roots)
    return 2 if search_failed else 0


if __name__ == "__main__":
    sys.exit(main())
