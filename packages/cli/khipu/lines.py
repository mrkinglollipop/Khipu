"""Split JSON Lines and line-based logs into records.

str.splitlines() also breaks at U+2028, U+2029, U+0085, \\v, \\f and
\\x1c-\\x1e. JSON writers such as JavaScript's JSON.stringify (and Python's
json.dumps with ensure_ascii=False) leave U+2028 and U+2029 raw inside
strings, so splitlines() cuts one record in two and both halves fail to
parse. A record ends at LF and nowhere else.
"""

from __future__ import annotations

from pathlib import Path


def split_lines(data: bytes | str, *, keepends: bool = False) -> list[str]:
    """``data`` cut at LF only. Bytes are decoded as UTF-8 with replacement
    (UTF-8 never uses the LF byte inside a character). Without ``keepends``
    one trailing CR is dropped from each line; a final LF adds no empty line."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    if not text:
        return []
    parts = text.split("\n")
    last = parts.pop()
    if keepends:
        return [p + "\n" for p in parts] + ([last] if last else [])
    if last:
        parts.append(last)
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def read_lines(path: str | Path) -> list[str]:
    """The lines of a file another program wrote: bytes, UTF-8 with
    replacement, LF only."""
    return split_lines(Path(path).read_bytes())
