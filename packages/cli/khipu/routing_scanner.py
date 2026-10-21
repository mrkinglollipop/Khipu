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
            elif ch == fence[0] and n >= fence[1] and stripped[n:] == "":
                # Closer: same marker, at least the opener's length, and
                # nothing after it but whitespace (already stripped).
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
    """Fence run: the same character, three or more, at the start of the line.

    An opener may carry an info string after the marker. A closer is that
    same marker, at least as long as the opener, with only whitespace after.
    """
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
    # "good for", "weak at", and other capability phrases are task wording
    # even when an ordinal follows ("good for second opinions"). A strong
    # current ordering claim in the same clause still counts.
    if _CAPABILITY_RE.search(clause) and not _STRONG_ORDER_RE.search(clause):
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


# ---- runner configs against Khipu's cap facts (slice 19b) ------------------
#
# A registered job runner's own ``budget`` declarations are compared with the
# owner-confirmed cap facts the folded view holds. This is a report: it takes
# parsed declarations and accounts, opens nothing, writes nothing, and never
# turns a runner number into a fact or a permission.

_WINDOW_KINDS = frozenset(("share", "per_day", "pace_target"))
_CAP_VALUES = {"share": ("pct",), "per_day": ("pct",), "pace_target": ("pct", "slack"),
               "spend": ("usd", "days"), "concurrency": ("n",)}
_TEXT_MAX = 200


_UNNAMED_ACCOUNT = "an unnamed account"
_WITHHELD_LABEL = "(name withheld)"


def _account_label(account: dict) -> str:
    """The label a flag may show for a known account. A label that is, or
    contains, the account's login key (or any path/key shape) is withheld:
    a login key must never be echoed into an account label or any output."""
    label = account.get("label")
    if not isinstance(label, str) or not label.strip():
        return _UNNAMED_ACCOUNT
    label = label.strip()
    if label == account.get("key") or any(mark in label for mark in ("/", "\\", "|")):
        return _WITHHELD_LABEL
    return label


def compare_runner_caps(declarations: list[dict], accounts: list[dict], account_key: str | None = None) -> list[dict]:
    """Where each runner declaration has no Khipu cap fact, names no Khipu
    account, or disagrees with the fact it has. Declarations that agree are
    not reported. Only ``declared`` and ``fact`` carry numbers; ``text`` is
    Khipu's own words and never a runner's free text. A matched account is
    named by its label only, never by its key (a key holds its login
    folder); an unmatched account is named by a fixed phrase, never by the
    name the runner wrote (a letters-only key cannot be told from a name).
    When ``account_key`` is given, only that account's flags are kept."""
    by_key: dict[str, dict] = {}
    by_label: dict[str, list[dict]] = {}
    for entry in accounts or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            continue
        by_key[entry["key"]] = entry
        label = entry.get("label")
        if isinstance(label, str) and label.strip():
            by_label.setdefault(label.strip().casefold(), []).append(entry)
    flags: list[dict] = []
    for declaration in declarations or []:
        if not isinstance(declaration, dict):
            continue
        kind = declaration.get("kind")
        if kind not in _CAP_VALUES:
            continue
        runner = str(declaration.get("runner") or "a job runner")
        raw_account = declaration.get("account")
        matched = _runner_account(raw_account, by_key, by_label)
        if account_key is not None and (matched is None or matched.get("key") != account_key):
            continue
        declared = _declared_values(declaration)
        window = declaration.get("window") if kind in _WINDOW_KINDS else None
        if matched is None:
            flags.append({
                "runner": runner, "account": None, "account_words": _UNNAMED_ACCOUNT,
                "kind": kind, "window": window, "declared": declared, "fact": None,
                "status": "no_account",
                "text": _clip(f"{runner}'s budget names {_UNNAMED_ACCOUNT}, which isn't a "
                              f"Khipu account; its {_cap_words(kind, window)} of "
                              f"{_value_words(kind, declaration)} has no fact to match.")})
            continue
        label = _account_label(matched)
        facts = _facts_for(matched, kind, window)
        if not facts:
            flags.append({
                "runner": runner, "account": label, "account_words": label,
                "kind": kind, "window": window, "declared": declared, "fact": None,
                "status": "no_fact",
                "text": _clip(f"{runner}'s budget sets {label} a "
                              f"{_cap_words(kind, window)} of {_value_words(kind, declaration)}; "
                              "Khipu holds no such cap.")})
            continue
        if any(_same_cap(kind, declaration, fact) for fact in facts):
            continue
        fact = facts[0]
        flags.append({
            "runner": runner, "account": label, "account_words": label,
            "kind": kind, "window": window, "declared": declared, "fact": _fact_values(fact, kind),
            "status": "disagrees",
            "text": _clip(f"{runner}'s budget sets {label} a "
                          f"{_cap_words(kind, window)} of {_value_words(kind, declaration)}; "
                          f"Khipu's cap is {_value_words(kind, fact)}.")})
    flags.sort(key=lambda f: (f["runner"], f["account"] or "", f["kind"], f["window"] or ""))
    return flags


def _runner_account(account, by_key: dict[str, dict], by_label: dict[str, list[dict]]) -> dict | None:
    if not isinstance(account, str) or not account:
        return None
    if account in by_key:
        return by_key[account]
    matches = by_label.get(account.strip().casefold()) or []
    return matches[0] if len(matches) == 1 else None


def _facts_for(account: dict, kind: str, window) -> list[dict]:
    out = []
    for fact in account.get("caps") or []:
        if not isinstance(fact, dict) or fact.get("kind") != kind:
            continue
        if kind in _WINDOW_KINDS and fact.get("window") != window:
            continue
        out.append(fact)
    return out


def _declared_values(declaration: dict) -> dict:
    out = {k: declaration[k] for k in ("kind", "window") if declaration.get(k) is not None}
    for field in _CAP_VALUES[declaration["kind"]]:
        if declaration.get(field) is not None:
            out[field] = declaration[field]
    return out


def _fact_values(fact: dict, kind: str) -> dict:
    out = {"kind": kind}
    if fact.get("window") is not None:
        out["window"] = fact["window"]
    for field in _CAP_VALUES[kind]:
        if fact.get(field) is not None:
            out[field] = fact[field]
    if fact.get("revision") is not None:
        out["revision"] = fact["revision"]
    return out


def _same_cap(kind: str, declaration: dict, fact: dict) -> bool:
    for field in _CAP_VALUES[kind]:
        if declaration.get(field) != fact.get(field):
            return False
    return True


def _window_word(window) -> str:
    return str(window or "").replace("_", "-") or "account"


def _cap_words(kind: str, window) -> str:
    if kind == "share":
        return f"{_window_word(window)} share"
    if kind == "per_day":
        return "rolling daily cap"
    if kind == "pace_target":
        return f"{_window_word(window)} pace target"
    if kind == "spend":
        return "spend cap"
    return "concurrency cap"


def _value_words(kind: str, declaration: dict) -> str:
    if kind in ("share", "per_day"):
        return f"{_number_words(declaration.get('pct'))}%"
    if kind == "pace_target":
        return f"{_number_words(declaration.get('pct'))}% with {_number_words(declaration.get('slack'))} slack"
    if kind == "spend":
        return f"${_number_words(declaration.get('usd'))} in {_number_words(declaration.get('days'))} days"
    n = declaration.get("n")
    jobs = "job" if n == 1 else "jobs"
    return f"{_number_words(n)} {jobs}"


def _number_words(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def _clip(text: str) -> str:
    return text if len(text) <= _TEXT_MAX else text[: _TEXT_MAX - 1].rstrip() + "…"
