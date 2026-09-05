"""Secret redaction before anything reaches a model or the hub (2026-09-05).

Two places see raw text: the transcript window the native extractor sends to
the summariser (``session_capture.render``), and the capture payload every
writer lands through (``capture.write_pg`` — the hook, the MCP tool, the
gateway). Both call in here first, so a pasted API key, a token in a curl
line, or a password inside a connection string never becomes a summary, a
topic page, a commitment or a vector.

Deterministic patterns only, and only shapes that are unmistakably secrets:
key-vendor prefixes, private-key blocks, JWTs, bearer/basic headers,
credentials inside URLs, and ``name = value`` assignments whose name says
secret. Commit SHAs, ids and ordinary hex are deliberately NOT matched — they
are exactly the kind of thing a memory is for. ``KHIPU_REDACT_SECRETS=0``
turns it off for a session that genuinely needs raw values in memory.
"""
from __future__ import annotations

import os
import re
from typing import Any

MASK = "[REDACTED]"

# Order matters only where patterns overlap: blocks first, then vendor
# prefixes, then structural shapes, then name/value assignments.
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.S,
        ),
        "[REDACTED private key]",
    ),
    # Vendor-prefixed keys. Lengths are the vendors' own minimums, minus a
    # little so a key pasted with a trailing character cut off still matches.
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), MASK),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"), MASK),
    ("stripe", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), MASK),
    ("aws-access", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), MASK),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
    ("gemini-like", re.compile(r"\bya29\.[0-9A-Za-z_-]{30,}"), MASK),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    # Header values. The header name survives so the summary can still say
    # "used a bearer token"; the token does not.
    (
        "auth-header",
        re.compile(r"(?i)\b(bearer|basic|x-goog-api-key\s*:|x-api-key\s*:|api-key\s*:)\s+([A-Za-z0-9._~+/=-]{16,})"),
        r"\1 " + MASK,
    ),
    # scheme://user:password@host — keep the user, drop the password.
    ("url-credential", re.compile(r"(://[^/\s:@]+:)([^@\s/]{1,})@"), r"\1" + MASK + "@"),
    # name = value / name: value / name="value" where the name says secret.
    # The value must be at least 8 characters without spaces so prose such as
    # "token budget" or "the password is wrong" is left alone.
    (
        "assignment",
        re.compile(
            r"(?i)\b([A-Za-z0-9_.-]*?(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|"
            r"refresh[_-]?token|token|password|passwd|pwd|client[_-]?secret|private[_-]?key|"
            r"access[_-]?key(?:[_-]?id)?|dsn|connection[_-]?string)\s*[:=]\s*[\"']?)"
            r"([^\s\"']{8,})"
        ),
        r"\1" + MASK,
    ),
]


def enabled() -> bool:
    return os.environ.get("KHIPU_REDACT_SECRETS", "1").strip().lower() not in ("0", "false", "no", "off")


def redact_secrets(text: str) -> tuple[str, int]:
    """Return (redacted text, number of replacements). Idempotent."""
    if not text or not enabled():
        return text or "", 0
    total = 0
    out = text
    for _name, pat, repl in _PATTERNS:
        out, n = pat.subn(repl, out)
        total += n
    return out, total


def _redact_str_list(items: Any) -> tuple[Any, int]:
    if not isinstance(items, list):
        return items, 0
    total = 0
    out = []
    for it in items:
        if isinstance(it, str):
            s, n = redact_secrets(it)
            total += n
            out.append(s)
        elif isinstance(it, dict):
            d = dict(it)
            for k in ("text", "summary", "why"):
                if isinstance(d.get(k), str):
                    d[k], n = redact_secrets(d[k])
                    total += n
            out.append(d)
        else:
            out.append(it)
    return out, total


def redact_payload(payload: dict[str, Any]) -> int:
    """Redact every free-text field of a capture payload in place. Returns
    the number of replacements. Topic pages carry a ``body``; open/closed
    loops carry ``text``; ``raw`` (the harness's own dump) is covered too."""
    if not isinstance(payload, dict) or not enabled():
        return 0
    total = 0
    for k in ("summary", "scope", "raw"):
        if isinstance(payload.get(k), str):
            payload[k], n = redact_secrets(payload[k])
            total += n
    for k in ("decisions", "preferences", "open_loops", "closed_loops", "people"):
        if k in payload:
            payload[k], n = _redact_str_list(payload[k])
            total += n
    pages = payload.get("topic_pages")
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get("body"), str):
                page["body"], n = redact_secrets(page["body"])
                total += n
    return total
