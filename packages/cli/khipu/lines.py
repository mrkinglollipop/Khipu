"""Split JSON Lines and line-based logs into records.

str.splitlines() also breaks at U+2028, U+2029, U+0085, \\v, \\f and
\\x1c-\\x1e. JSON writers such as JavaScript's JSON.stringify (and Python's
json.dumps with ensure_ascii=False) leave U+2028 and U+2029 raw inside
strings, so splitlines() cuts one record in two and both halves fail to
parse. A record ends at LF and nowhere else.
"""

from __future__ import annotations

import subprocess
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


def run_text(argv, *, input: str, **kw) -> subprocess.CompletedProcess:
    """``subprocess.run`` with ``input`` sent as strict UTF-8 and the captured
    output decoded as UTF-8 with replacement. One ``errors=`` in text mode
    covers both directions, so replacement there would also send a lone
    surrogate to the child as "?" and let a corrupted request succeed."""
    r = subprocess.run(argv, input=input.encode("utf-8"), capture_output=True, **kw)
    return subprocess.CompletedProcess(r.args, r.returncode, r.stdout.decode("utf-8", "replace"),
                                       r.stderr.decode("utf-8", "replace"))
