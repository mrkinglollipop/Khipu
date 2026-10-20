"""Flag routing-state claims in text that belongs in Khipu's record.

``scan_text`` is pure: it never opens ``path``, never walks a filesystem,
never reads API Keys or My Portfolio, and never sends text to a model or
the network. ``path`` is copied onto each claim as evidence.
"""
from __future__ import annotations

import re
from typing import Any

SUPERSEDED_MARK = "routing state: superseded by Khipu's record"

# Providers, accounts and lanes Khipu's record names. "main account" is the
# live phrasing for the primary Claude login; bare "main" is an ordering word.
_ACCOUNT_RE = re.compile(
    r"\b(?:main\s+account|anthropic|deepseek|openai|claude|codex|grok|muse|glm|jev|xai)\b",
    re.I,
)
_STATE_RE = re.compile(r"\b(?:open|limited|paused|held|tight|low|down)\b", re.I)
_ORDERING_RE = re.compile(
    r"\b(?:first|second|before|after|order|priority|prefers|prefer|main|only|not used)\b",
    re.I,
)
_RANK_RE = re.compile(r"^\s*\d+\.\s+(\S.*?)\s*$")
_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_DATE_QUALIFIER_RE = re.compile(r"\b(?:as of|until)\b", re.I)
_LEVEL_RE = re.compile(r"\d+(?:\.\d+)?%\s+left", re.I)
_HOWTO_RE = re.compile(
    r"\bexec\b|(?<!\w)--[A-Za-z]|\bto reach\b|\bstarting the\b",
    re.I,
)
_HISTORY_RE = re.compile(r"\b(?:was|were|had been|preferred)\b", re.I)


def scan_text(path: str, text: str) -> list[dict[str, Any]]:
    """Claims in ``text``. ``path`` is stored as ``file`` and is never read."""
    file = str(path)
    claims: list[dict[str, Any]] = []
    in_frontmatter = False
    for index, line in enumerate(str(text).splitlines(), 1):
        stripped = line.strip()
        if index == 1 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        kind, account = _claim_on(line)
        if kind is None or account is None:
            continue
        claims.append({
            "file": file,
            "line": index,
            "account": account,
            "kind": kind,
            "words": line,
        })
    return claims


def _claim_on(line: str) -> tuple[str | None, str | None]:
    stripped = line.lstrip()
    if not stripped or stripped.startswith((">", "|")):
        return None, None
    if _HOWTO_RE.search(line) or _HISTORY_RE.search(line):
        return None, None
    ranked = _ranked_account(line)
    if ranked is not None:
        return "ordering", ranked
    account = _account(line)
    if account is None:
        return None, None
    if _STATE_RE.search(line) and _has_qualifier(line):
        return "state", account
    if _ORDERING_RE.search(line):
        return "ordering", account
    return None, None


def _account(line: str) -> str | None:
    match = _ACCOUNT_RE.search(line)
    return match.group(0) if match else None


def _ranked_account(line: str) -> str | None:
    match = _RANK_RE.match(line)
    if not match:
        return None
    rest = match.group(1)
    found = _ACCOUNT_RE.match(rest)
    if found is None or found.end() != len(rest):
        return None
    return found.group(0)


def _has_qualifier(line: str) -> bool:
    if _LEVEL_RE.search(line):
        return True
    return bool(_DATE_RE.search(line) and _DATE_QUALIFIER_RE.search(line))
