"""Strict JSON for every frame Khipu writes: MCP stdio, the gateway, hook
stdout, and the CLI JSON the desktop parses.

Strict means any client parses it: Apple's Foundation decoder and Rust's
serde_json reject a lone surrogate escape (``\\udc80``), and a reader that
splits on Unicode line breaks (Python's ``str.splitlines``) cuts a frame at
a literal U+2028 or U+2029. So a lone surrogate (text decoded with
surrogateescape) is written as the visible text ``\\udc80``, never as a
JSON escape or a raw code unit, and U+2028 and U+2029 are always escaped.
Readers decode bytes as UTF-8 explicitly and split only on LF.
"""
from __future__ import annotations

import json
import math
import re
import sys

_PAIR = re.compile("[\ud800-\udbff][\udc00-\udfff]")
_LONE = re.compile("[\ud800-\udfff]")


def _join_pair(m: re.Match) -> str:
    hi, lo = (ord(c) for c in m.group())
    return chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))


def clean_text(s: str) -> str:
    """``s`` with no surrogate code units: a high and low pair becomes the
    character it encodes, a lone one the six characters ``\\udc80``."""
    if not _LONE.search(s):
        return s
    s = _PAIR.sub(_join_pair, s)
    return _LONE.sub(lambda m: "\\u%04x" % ord(m.group()), s)


def clean(value):
    """A copy of a JSON-shaped value with every string (keys included)
    passed through ``clean_text`` and NaN or infinity as null; anything
    else is returned as is."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None  # NaN and Infinity are not JSON; serde_json rejects them
    if isinstance(value, dict):
        return {clean(k) if isinstance(k, str) else k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def dumps(value, *, ensure_ascii: bool = False, default=None, **kw) -> str:
    """``json.dumps`` that only ever produces strict JSON (see the module
    docstring). ``default`` results are cleaned too, so ``default=str`` on an
    object whose text holds a surrogate stays strict."""
    hook = None
    if default is not None:
        def hook(o):
            return clean(default(o))
    text = json.dumps(clean(value), ensure_ascii=ensure_ascii, default=hook, **kw)
    # Only possible inside a string literal, where the escape means the same.
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def encode(value, **kw) -> bytes:
    """``dumps`` as UTF-8 bytes; strict UTF-8, since no surrogate survives."""
    return dumps(value, **kw).encode("utf-8")


def split_frames(data: bytes) -> list[str]:
    """Newline-delimited frames from raw bytes: UTF-8 whatever the locale,
    split on LF only, blank lines dropped."""
    return [ln for ln in data.decode("utf-8").split("\n") if ln.strip()]


def read_stdin() -> str:
    """All of stdin as UTF-8 whatever the locale (a C locale made it ASCII),
    undecodable bytes replaced, as the recall client reads it."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    return buffer.read().decode("utf-8", "replace")
