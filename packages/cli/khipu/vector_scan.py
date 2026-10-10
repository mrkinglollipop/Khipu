# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.vector_scan — fast top-K dot product over packed float32 embedding
blobs (Phase 0, session C; finding B10, docs/research/
hindsight-plan-review-2026-09-28.md).

``hub_snapshot.cosine_candidates_snapshot`` scored the per-prompt recall
lane's local replica by unpacking every row's embedding blob and summing a
Python dot product one row at a time — 226ms measured live over ~18,400
rows, most of a 1.2s budget the lane missed on 85% of prompts in production.

Float32 BLAS shortlist scores cannot safely eliminate a row: a near tie can
round away the exact winner. This module therefore uses the original exact
Python arithmetic for every row. The retained BLAS helpers are intentionally
unused compatibility seams for callers that patch them in older tests.

No new dependency: ctypes (stdlib) loads a SYSTEM library that may or may
not be present. Every load/call failure — missing library, wrong ABI, a
shape mismatch, anything — degrades silently to scoring every row in pure
Python. This module must never raise for a scoring reason, only ever return
slower instead of wrong.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import operator
import struct
import sys
from typing import Any, Iterable, Sequence

# Retained for the private compatibility helper below; not used by ranking.
_SHORTLIST_FLOOR = 64
_SHORTLIST_MULTIPLIER = 8

# CBLAS enums (cblas.h) — fixed by the CBLAS ABI, never change; not worth a
# dependency to pull in two ints.
_CBLAS_ROW_MAJOR = 101
_CBLAS_NO_TRANS = 111


def _exact_score(query: Sequence[float], blob: bytes, dim: int, fmt: str) -> float:
    """The arithmetic ``hub_snapshot.cosine_candidates_snapshot`` has always
    used: unpack the blob's first ``dim`` float32s and take a plain dot
    product against ``query`` (the caller's job to normalize, if that's the
    intent — this is a bare dot product). Accumulates in Python float
    (64-bit) even though every unpacked value only carries float32
    precision — this is the reference arithmetic every other path in this
    module must reproduce exactly, byte for byte."""
    doc = struct.unpack(fmt, blob[: dim * 4])
    return sum(map(operator.mul, query, doc))


def _cblas_sgemv():
    """A configured ``cblas_sgemv`` ctypes function, or ``None`` when no
    usable BLAS library loads. Tries the macOS Accelerate framework first
    (present on every Darwin system), then a generic system BLAS/OpenBLAS by
    soname. Never raises — any failure (missing library, missing symbol,
    wrong ABI) just means "no BLAS today"; the caller falls back to pure
    Python."""
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates.append("/System/Library/Frameworks/Accelerate.framework/Accelerate")
    for name in ("blas", "openblas"):
        try:
            found = ctypes.util.find_library(name)
        except Exception:  # noqa: BLE001 — a hostile find_library must not break scoring
            found = None
        if found:
            candidates.append(found)

    for path in candidates:
        try:
            lib = ctypes.CDLL(path)
            fn = lib.cblas_sgemv
            fn.restype = None
            fn.argtypes = [
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_float, ctypes.POINTER(ctypes.c_float), ctypes.c_int,
                ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_float,
                ctypes.POINTER(ctypes.c_float), ctypes.c_int,
            ]
            return fn
        except Exception:  # noqa: BLE001 — try the next candidate library
            continue
    return None


def _blas_shortlist(
    sgemv: Any,
    query: Sequence[float],
    valid: list[tuple[int, Any, bytes]],
    dim: int,
    shortlist_n: int,
) -> list[tuple[int, Any, bytes]]:
    """One ``cblas_sgemv`` call scores every ``valid`` row in float32. Its
    shortlist is advisory only; exact ranking retains all rows. Raises on any
    shape/library problem so the caller can keep the exact path."""
    n = len(valid)
    packed = bytearray(n * dim * 4)
    for i, (_pos, _key, blob) in enumerate(valid):
        packed[i * dim * 4 : (i + 1) * dim * 4] = blob[: dim * 4]
    a_buf = (ctypes.c_float * (n * dim)).from_buffer(packed)
    x_buf = (ctypes.c_float * dim)(*[float(v) for v in query])
    y_buf = (ctypes.c_float * n)()
    sgemv(
        _CBLAS_ROW_MAJOR, _CBLAS_NO_TRANS, n, dim,
        1.0, a_buf, dim, x_buf, 1, 0.0, y_buf, 1,
    )
    approx = list(y_buf)
    order = sorted(range(n), key=lambda i: (-approx[i], valid[i][0]))[:shortlist_n]
    return [valid[i] for i in order]


def top_k_by_dot(
    query: Sequence[float],
    rows: Iterable[tuple[Any, bytes]],
    limit: int,
    *,
    use_blas: bool = True,
) -> list[tuple[Any, float]]:
    """Top ``limit`` ``(row_key, score)`` pairs from ``rows`` — an iterable
    of ``(row_key, blob)``, ``blob`` a packed float32 buffer — ranked by dot
    product against ``query`` (a bare dot product; the caller normalizes
    beforehand for cosine similarity, same as the implementation this
    replaces). Ties keep ``rows``'s original order. A row whose blob is
    missing or shorter than ``len(query) * 4`` bytes is skipped, same as the
    pure-Python implementation this replaces.

    Every valid row is scored with exact arithmetic (float64 accumulation)
    before the final cut. This function itself never raises for a scoring
    reason.
    """
    dim = len(query)
    fmt = f"{dim}f"
    q = tuple(query)
    limit = max(0, int(limit))
    valid: list[tuple[int, Any, bytes]] = [
        (pos, key, blob) for pos, (key, blob) in enumerate(rows) if blob and len(blob) >= dim * 4
    ]
    if not valid or limit == 0:
        return []

    scored = [(pos, key, _exact_score(q, blob, dim, fmt)) for pos, key, blob in valid]
    scored.sort(key=lambda item: (-item[2], item[0]))
    return [(key, score) for _pos, key, score in scored[:limit]]
