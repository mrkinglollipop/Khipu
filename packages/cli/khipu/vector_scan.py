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

Float32 scores shortlist candidates only outside a conservative roundoff
bound. Near ties are retained and rescored with the original Python arithmetic.
Deadline checks bound both the accelerated and fallback scans.

No new dependency: ctypes (stdlib) loads a SYSTEM library that may or may
not be present. Every load/call failure — missing library, wrong ABI, a
shape mismatch, anything — degrades silently to scoring every row in pure
Python. This module must never raise for a scoring reason, only ever return
slower instead of wrong.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import heapq
import operator
import math
import time
import struct
import sys
from typing import Any, Iterable, Sequence

# Avoid native setup overhead for small candidate sets.
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
    *, entity_key=None, deadline: float | None = None, max_abs_component: float | None = None,
) -> list[tuple[int, Any, bytes]]:
    """One ``cblas_sgemv`` call scores every ``valid`` row in float32. Its
    shortlist retains every candidate whose error interval overlaps the cut.
    Raises on shape/library problems so the caller can use exact scoring."""
    n = len(valid)
    packed = bytearray(n * dim * 4)
    max_doc = max_abs_component or 0.0
    for i, (_pos, _key, blob) in enumerate(valid):
        _check_deadline(deadline)
        if max_abs_component is None:
            doc = struct.unpack(f"{dim}f", blob[:dim * 4])
            max_doc = max(max_doc, abs(min(doc)), abs(max(doc)))
        packed[i * dim * 4 : (i + 1) * dim * 4] = blob[: dim * 4]
    approx = _sgemv_scores(sgemv, packed, n, dim, query)
    _check_deadline(deadline)
    return _shortlist(approx, valid, query, dim, shortlist_n, max_doc,
                      entity_key=entity_key, deadline=deadline)


def _sgemv_scores(sgemv: Any, packed, n: int, dim: int, query: Sequence[float]) -> list[float]:
    """Float32 dot products of the ``n`` rows packed in ``packed`` (a
    writable buffer of ``n * dim`` float32s) with ``query``."""
    a_buf = (ctypes.c_float * (n * dim)).from_buffer(packed)
    x_buf = (ctypes.c_float * dim)(*[float(v) for v in query])
    y_buf = (ctypes.c_float * n)()
    sgemv(
        _CBLAS_ROW_MAJOR, _CBLAS_NO_TRANS, n, dim,
        1.0, a_buf, dim, x_buf, 1, 0.0, y_buf, 1,
    )
    return list(y_buf)


def _all_finite(values: list[float]) -> bool:
    # A finite sum of finite floats needs no per-value check; an infinite or
    # NaN sum (an overflow included) falls back to checking each value.
    return math.isfinite(sum(values)) or all(math.isfinite(v) for v in values)


def _shortlist(
    approx: list[float], valid: list[tuple[int, Any, Any]], query: Sequence[float], dim: int,
    shortlist_n: int, max_doc: float, *, entity_key=None, deadline: float | None = None,
) -> list[tuple[int, Any, Any]]:
    """The rows of ``valid`` whose float32 score ``approx`` (same order) is
    within the error bound of the ``shortlist_n``-th entity's."""
    n = len(valid)
    # Covers float32 query conversion, multiply/add rounding, float64
    # reference accumulation, and implementations that flush subnormals.
    nu = (2 * dim + 2) * 2.0 ** -24
    if nu >= 0.5 or not _all_finite(approx):
        return valid
    error = (2 * nu / (1 - nu) * max_doc * sum(abs(v) for v in query)
             + 4 * dim * 2.0 ** -126 * (max_doc + 1))
    cutoff = -math.inf
    # The walk below visits rows best first (score, then position). It reads
    # only a prefix of that order: every row scoring at least the k-th best
    # score, which is that order's first rows exactly. A prefix that holds
    # too few entities is widened until it is the whole list.
    k = min(n, max(shortlist_n * _SHORTLIST_MULTIPLIER, _SHORTLIST_FLOOR))
    while True:
        _check_deadline(deadline)
        floor = heapq.nlargest(k, approx)[-1] if k < n else -math.inf
        order = sorted((i for i in range(n) if approx[i] >= floor),
                       key=lambda i: (-approx[i], valid[i][0]))
        seen = set()
        for i in order:
            identity = entity_key(valid[i][1]) if entity_key else valid[i][0]
            seen.add(identity)
            if len(seen) >= shortlist_n:
                cutoff = approx[i] - 2 * error
                break
        if cutoff != -math.inf or k >= n:
            break
        k = min(n, k * 4)
    _check_deadline(deadline)
    return [row for i, row in enumerate(valid) if approx[i] >= cutoff]


class PackedRows:
    """One generation of rows, ``(key, blob)``, every blob exactly
    ``dim * 4`` bytes, packed once into one float32 matrix so a scan does not
    copy every vector again. ``rows`` hold views into that matrix, not
    copies. ``index`` maps a row's key to its row in the matrix (keys are
    unique). Built by ``pack``; None when the rows are not uniform."""

    __slots__ = ("buf", "dim", "n", "index", "rows")

    def __init__(self, buf: bytearray, dim: int, rows: list, index: dict):
        self.buf, self.dim, self.n, self.rows, self.index = buf, dim, len(rows), rows, index


def pack(rows: Sequence[tuple[Any, ...]], *, key_at: int = 0, blob_at: int = -1) -> "PackedRows | None":
    """Pack ``rows`` (tuples carrying a unique key at ``key_at`` and a
    float32 blob at ``blob_at``) into a ``PackedRows`` whose ``rows`` are the
    same tuples with each blob replaced by a view into the matrix, or None
    when any blob is missing, of another length, or a key repeats."""
    if not rows:
        return None
    try:
        size = len(rows[0][blob_at])
        if size == 0 or size % 4:
            return None
        blobs = [r[blob_at] for r in rows]
        if any(not isinstance(b, (bytes, bytearray, memoryview)) or len(b) != size for b in blobs):
            return None
        index = {r[key_at]: i for i, r in enumerate(rows)}
        if len(index) != len(rows):
            return None
        buf = bytearray().join(blobs)
        del blobs
        view = memoryview(buf)
        blob_pos = blob_at % len(rows[0])
        out = [(*r[:blob_pos], view[i * size:(i + 1) * size], *r[blob_pos + 1:]) for i, r in enumerate(rows)]
        return PackedRows(buf, size // 4, out, index)
    except (TypeError, IndexError, ValueError):
        return None


def top_k_packed(
    query: Sequence[float],
    rows: Sequence[tuple[Any, Any]],
    packed: PackedRows,
    limit: int,
    *,
    entity_key=None,
    deadline: float | None = None,
    max_abs_component: float,
) -> list[tuple[Any, float]]:
    """``top_k_by_dot(query, rows, limit, ...)`` (same answer) where every
    row of ``rows`` (``(key, blob)``) is a row of ``packed``, in the
    matrix's order: one ``cblas_sgemv`` over the packed matrix, with no
    per-prompt copy of the vectors. Falls back to ``top_k_by_dot`` whenever
    that would not take its accelerated path."""
    dim = len(query)
    limit = max(0, int(limit))
    sgemv = _cblas_sgemv() if dim == packed.dim else None
    if (sgemv is None or not rows or limit == 0 or dim == 0
            or len(rows) <= max(_SHORTLIST_FLOOR, _SHORTLIST_MULTIPLIER * limit)):
        return top_k_by_dot(query, rows, limit, entity_key=entity_key, deadline=deadline,
                            max_abs_component=max_abs_component)
    _check_deadline(deadline)
    valid = [(pos, key, blob) for pos, (key, blob) in enumerate(rows)]
    try:
        full = _sgemv_scores(sgemv, packed.buf, packed.n, dim, query)
        _check_deadline(deadline)
        index = packed.index
        approx = [full[index[key]] for _pos, key, _blob in valid]
        candidates = _shortlist(approx, valid, query, dim, limit, max_abs_component or 0.0,
                                entity_key=entity_key, deadline=deadline)
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 — native failures use the exact fallback
        candidates = valid
    return _exact_top(tuple(query), candidates, dim, f"{dim}f", limit, entity_key=entity_key, deadline=deadline)


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("vector scan deadline expired")


def top_k_by_dot(
    query: Sequence[float],
    rows: Iterable[tuple[Any, bytes]],
    limit: int,
    *,
    use_blas: bool = True,
    entity_key=None,
    deadline: float | None = None,
    max_abs_component: float | None = None,
) -> list[tuple[Any, float]]:
    """Top ``limit`` ``(row_key, score)`` pairs from ``rows`` — an iterable
    of ``(row_key, blob)``, ``blob`` a packed float32 buffer — ranked by dot
    product against ``query`` (a bare dot product; the caller normalizes
    beforehand for cosine similarity, same as the implementation this
    replaces). Ties keep ``rows``'s original order. A row whose blob is
    missing or shorter than ``len(query) * 4`` bytes is skipped, same as the
    pure-Python implementation this replaces.

    Retained candidates receive exact float64 scores. Optional entity keys
    collapse chunks in the same pass. Deadline expiry raises TimeoutError.
    ``max_abs_component`` must bound every document component; normalized
    callers can supply 1.0 to avoid inspecting the same floats again.
    """
    dim = len(query)
    fmt = f"{dim}f"
    q = tuple(query)
    limit = max(0, int(limit))
    valid = []
    for pos, (key, blob) in enumerate(rows):
        _check_deadline(deadline)
        if blob and len(blob) >= dim * 4:
            valid.append((pos, key, blob))
    if not valid or limit == 0 or dim == 0:
        return []
    candidates = valid
    if use_blas and len(valid) > max(_SHORTLIST_FLOOR, _SHORTLIST_MULTIPLIER * limit):
        try:
            sgemv = _cblas_sgemv()
            if sgemv is not None:
                candidates = _blas_shortlist(sgemv, q, valid, dim, limit,
                                            entity_key=entity_key, deadline=deadline,
                                            max_abs_component=max_abs_component)
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001 — native failures use the exact fallback
            candidates = valid
    return _exact_top(q, candidates, dim, fmt, limit, entity_key=entity_key, deadline=deadline)


def _exact_top(q: tuple, candidates: list, dim: int, fmt: str, limit: int, *,
               entity_key=None, deadline: float | None = None) -> list[tuple[Any, float]]:
    """The best ``limit`` of ``candidates`` (``(pos, key, blob)``) by exact
    score, one per entity, ties by position."""
    scored = []
    for pos, key, blob in candidates:
        _check_deadline(deadline)
        scored.append((pos, key, _exact_score(q, blob, dim, fmt)))
    scored.sort(key=lambda item: (-item[2], item[0]))
    out, seen = [], set()
    for pos, key, score in scored:
        _check_deadline(deadline)
        identity = entity_key(key) if entity_key else pos
        if identity not in seen:
            seen.add(identity)
            out.append((key, score))
            if len(out) >= limit:
                break
    return out
