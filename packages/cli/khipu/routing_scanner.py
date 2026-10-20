"""Flag routing-state claims in text that belongs in Khipu's record.

``scan_text`` is pure: it never opens ``path``, never walks a filesystem,
never reads API Keys or My Portfolio, and never sends text to a model or
the network. ``path`` is copied onto each claim as evidence.
"""
from __future__ import annotations

import re
from typing import Any

SUPERSEDED_MARK = "routing state: superseded by Khipu's record"


def has_routing_claim(path: str, text: str) -> bool:
    """Write-time advisory metadata; scanner failure must not block import."""
    try:
        return bool(scan_text(path, text))
    except Exception:  # noqa: BLE001 — never log source text or scanner errors
        return False

# Providers, accounts, lanes and model names Khipu's record names.
# "main account" / "primary account" are live phrasings for the primary
# Claude login; bare "main"/"primary" are ordering words.
_ACCOUNT_RE = re.compile(
    r"\b(?:main\s+account|primary\s+account|anthropic|deepseek|openai|"
    r"claude|codex|grok|muse|glm|jev|xai|haiku|sonnet|opus)\b",
    re.I,
)
_STATE_RE = re.compile(r"\b(?:open|limited|paused|held|tight|low|down)\b", re.I)
_ORDERING_RE = re.compile(
    r"\b(?:first|second|before|after|order|priority|prefers|prefer|"
    r"main|primary|only|not used)\b",
    re.I,
)
_STRONG_ORDER_RE = re.compile(
    r"\b(?:prefers|prefer|priority|not used|first choice)\b|\border\b",
    re.I,
)
# Numbered ranking whose first token is an account; a trailing description
# ("1. Codex — builds") is still a ranking.
_RANK_RE = re.compile(
    r"^\s*\d{1,3}\.\s+\*{0,2}("
    r"main\s+account|primary\s+account|anthropic|deepseek|openai|"
    r"claude|codex|grok|muse|glm|jev|xai|haiku|sonnet|opus"
    r")\b\*{0,2}",
    re.I,
)
_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_WEEKDAY_RE = re.compile(
    r"\b(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|"
    r"Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b",
    re.I,
)
# Bound the percent so a long numeric line cannot retry an unbounded \d+
# at every offset (O(n^2) on a 4 KB digit run).
_LEVEL_RE = re.compile(
    r"(?<!\d)\d{1,3}(?:\.\d{1,2})?\s*%\s*(?:left|used|remaining)",
    re.I,
)
_HOWTO_RE = re.compile(
    r"\bexec\b|(?<!\w)--[A-Za-z]|\bto reach\b|\bstarting the\b",
    re.I,
)
_HISTORY_PREFIX_RE = re.compile(r"^\s*history\s*:", re.I)
_PAST_RE = re.compile(r"\b(?:was|were|had been|preferred|chose)\b", re.I)
_PRESENT_RE = re.compile(r"\b(?:is|are|stays|stay)\b", re.I)
_ACTOR_PAUSED_RE = re.compile(
    r"\b(?:matt|the operator|we|the owner)\s+paused\b",
    re.I,
)
_CAPABILITY_RE = re.compile(
    r"\bgood for\b|\bweak at\b|\bfirst drafts\b|\bin this example\b",
    re.I,
)
_INCIDENTAL_ORDER_RE = re.compile(r"\bfirst drafts\b|\bsupports only\b", re.I)
_CURRENT_FRAME_RE = re.compile(r"\bcurrent(?:ly)?(?:\s+routing)?\b", re.I)


def scan_text(path: str, text: str) -> list[dict[str, Any]]:
    """Claims in ``text``. ``path`` is stored as ``file`` and is never read."""
    file = str(path)
    claims: list[dict[str, Any]] = []
    in_frontmatter = False
    fence: tuple[str, int] | None = None
    in_capability_table = False
    for index, line in enumerate(str(text).splitlines(), 1):
        stripped = line.strip()
        if index == 1 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        marker = _fence_marker(stripped)
        if marker is not None:
            ch, n = marker
            if fence is None:
                fence = (ch, n)
            elif ch == fence[0] and n >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        if stripped.startswith("|"):
            if _CAPABILITY_RE.search(line):
                in_capability_table = True
                continue
            if in_capability_table:
                continue
            kind, account = _claim_on(_table_cells(line))
        else:
            in_capability_table = False
            if stripped.startswith(">"):
                inner = stripped.lstrip(">").strip()
                # Quoted or past-tense history is not a live claim; a current
                # claim in a blockquote still is (scope §10). Bare quotations
                # without a current frame stay quoted, matching the pinned
                # quote-block fixtures.
                if _is_history_text(inner) or not _CURRENT_FRAME_RE.search(inner):
                    continue
                kind, account = _claim_on(inner)
            else:
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


def _table_cells(line: str) -> str:
    return " ".join(cell.strip() for cell in line.split("|") if cell.strip())


def _claim_on(line: str) -> tuple[str | None, str | None]:
    stripped = line.strip()
    if not stripped:
        return None, None
    ranked = _ranked_account(line)
    if ranked is not None:
        return "ordering", ranked
    for clause in _clauses(stripped):
        kind, account = _claim_on_clause(clause)
        if kind is not None:
            return kind, account
    return None, None


def _clauses(line: str) -> list[str]:
    """Split on ';' or '. ' in one left-to-right pass (linear in ``line``)."""
    parts: list[str] = []
    start = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == ";":
            part = line[start:i].strip()
            if part:
                parts.append(part)
            start = i + 1
            i += 1
            continue
        if ch == "." and i + 1 < n and line[i + 1].isspace():
            part = line[start:i].strip()
            if part:
                parts.append(part)
            start = i + 1
            i += 1
            continue
        i += 1
    tail = line[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _fence_marker(stripped: str) -> tuple[str, int] | None:
    """Opening/closing fence: the same character, three or more, at the start."""
    if len(stripped) < 3:
        return None
    ch = stripped[0]
    if ch not in "`~":
        return None
    n = 0
    for c in stripped:
        if c != ch:
            break
        n += 1
    if n < 3:
        return None
    return ch, n


def _claim_on_clause(clause: str) -> tuple[str | None, str | None]:
    if _is_history_text(clause):
        return None, None
    account = _account(clause)
    if account is None:
        return None, None
    state_claim = bool(_STATE_RE.search(clause) and _has_qualifier(clause))
    if state_claim:
        return "state", account
    masked = _INCIDENTAL_ORDER_RE.sub(" ", clause)
    ordering = bool(_ORDERING_RE.search(masked))
    # Capability/how-to exclusions apply to this clause only, and never to a
    # clause that already states a current ordering claim.
    if _CAPABILITY_RE.search(clause) and not ordering:
        return None, None
    howto = bool(_HOWTO_RE.search(clause))
    if howto and not _STRONG_ORDER_RE.search(clause):
        return None, None
    if ordering:
        return "ordering", account
    return None, None


def _is_history_text(text: str) -> bool:
    if _HISTORY_PREFIX_RE.match(text.strip()) or _ACTOR_PAUSED_RE.search(text):
        return True
    return bool(_PAST_RE.search(text) and not _PRESENT_RE.search(text))


def _account(line: str) -> str | None:
    match = _ACCOUNT_RE.search(line)
    return match.group(0) if match else None


def _ranked_account(line: str) -> str | None:
    match = _RANK_RE.match(line)
    if match is None:
        return None
    # Numbered capability prose ("1. Codex is good for first drafts.") is a
    # capability row, not a ranking of accounts.
    if _CAPABILITY_RE.search(line):
        return None
    return match.group(1)


def _has_qualifier(line: str) -> bool:
    if _LEVEL_RE.search(line):
        return True
    if _DATE_RE.search(line):
        return True
    return bool(_WEEKDAY_RE.search(line))
