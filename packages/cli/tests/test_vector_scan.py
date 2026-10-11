# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.vector_scan — top-K dot product (Phase 0 session C, finding B10).

The core claim under test: scoring through BLAS (a float32 shortlist) and
then re-scoring only that shortlist with the exact old arithmetic must
return IDENTICAL rows, in the identical order, to scoring every row in pure
Python the way ``hub_snapshot.cosine_candidates_snapshot`` always did. The
differential test keeps a verbatim copy of the pre-change implementation as
the reference and checks the new (BLAS-backed) implementation against it on
a generated fixture, not against hand-picked expectations.
"""
from __future__ import annotations

import math
import random
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import hub_snapshot as hs
from khipu import vector_scan as vs

_DIM = 768
_N_ROWS = 3000


def _pack_f32(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unit_vector(rng: random.Random, dim: int = _DIM) -> list[float]:
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _test_sgemv(order, trans, rows, dim, alpha, matrix, stride, query, incx, beta, scores, incy):
    """An observable float32 scoring callable avoids host BLAS availability gates."""
    for i in range(rows):
        scores[i] = sum(matrix[i * stride + j] * query[j] for j in range(dim))


class TopKByDotTest(unittest.TestCase):
    """Direct unit coverage of ``top_k_by_dot`` itself, independent of
    ``hub_snapshot`` — the fixture-driven differential test below covers the
    full integration."""

    def test_rv3_expired_scan_stops_before_scoring(self):
        with mock.patch.object(vs, "_exact_score") as score:
            with self.assertRaises(TimeoutError):
                vs.top_k_by_dot([1., 0.], [("a", _pack_f32([1., 0.]))], 1, deadline=0.)
        score.assert_not_called()

    def test_rv3_scan_stops_when_deadline_expires_during_scoring(self):
        clock = [0.]
        def score(*args):
            clock[0] += 1.
            return 1.
        with mock.patch.object(vs.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(vs, "_exact_score", side_effect=score) as scoring:
            with self.assertRaises(TimeoutError):
                vs.top_k_by_dot([1., 0.], [(i, _pack_f32([1., 0.])) for i in range(10)],
                               1, use_blas=False, deadline=2.)
        self.assertEqual(scoring.call_count, 2)

    def test_rv3_twenty_thousand_vectors_use_blas_and_match_exact_ranking(self):
        import time
        native = vs._cblas_sgemv()
        if native is None:
            self.skipTest("system BLAS unavailable")
        rng = random.Random(83)
        corpus = [_pack_f32(_unit_vector(rng, 768)) for _ in range(128)]
        rows = [(i, corpus[i % len(corpus)]) for i in range(20000)]
        query = _unit_vector(rng, 768)
        start = time.monotonic()
        expected = vs.top_k_by_dot(query, rows, 8, use_blas=False)
        exact_ms = (time.monotonic() - start) * 1000
        with mock.patch.object(vs, "_cblas_sgemv", return_value=native), \
             mock.patch.object(vs, "_exact_score", wraps=vs._exact_score) as score:
            start = time.monotonic()
            actual = vs.top_k_by_dot(query, rows, 8, max_abs_component=1.0)
            accelerated_ms = (time.monotonic() - start) * 1000
        self.assertEqual(actual, expected)
        self.assertLess(score.call_count, len(rows))
        print(f"20,000 x 768: exact={exact_ms:.1f}ms accelerated={accelerated_ms:.1f}ms rescored={score.call_count}")

    def test_empty_rows_yields_empty(self) -> None:
        self.assertEqual(vs.top_k_by_dot([1.0, 0.0], [], 5), [])

    def test_limit_zero_yields_empty(self) -> None:
        rows = [("a", _pack_f32([1.0, 0.0]))]
        self.assertEqual(vs.top_k_by_dot([1.0, 0.0], rows, 0), [])

    def test_short_blob_is_skipped_not_raised(self) -> None:
        rows = [
            ("short", struct.pack("1f", 1.0)),  # dim=2 query, 1-float blob: too short
            ("full", _pack_f32([1.0, 0.0])),
        ]
        out = vs.top_k_by_dot([1.0, 0.0], rows, 5)
        self.assertEqual([k for k, _s in out], ["full"])

    def test_missing_blob_is_skipped(self) -> None:
        rows = [("empty", b""), ("full", _pack_f32([1.0, 0.0]))]
        out = vs.top_k_by_dot([1.0, 0.0], rows, 5)
        self.assertEqual([k for k, _s in out], ["full"])

    def test_ties_keep_original_row_position(self) -> None:
        # Three rows with the IDENTICAL vector: ranked purely by insertion
        # order among the tie (`(-score, original row position)`).
        v = _pack_f32([1.0, 0.0])
        rows = [("c", v), ("a", v), ("b", v)]
        out = vs.top_k_by_dot([1.0, 0.0], rows, 5, use_blas=False)
        self.assertEqual([k for k, _s in out], ["c", "a", "b"])

    def test_ranks_the_closer_vector_first(self) -> None:
        rows = [
            ("far", _pack_f32([0.0, 1.0, 0.0])),
            ("near", _pack_f32([1.0, 0.0, 0.0])),
        ]
        out = vs.top_k_by_dot([1.0, 0.0, 0.0], rows, 5, use_blas=False)
        self.assertEqual(out[0][0], "near")
        self.assertAlmostEqual(out[0][1], 1.0, places=6)

    def test_use_blas_false_and_true_agree_on_a_small_case(self) -> None:
        rng = random.Random(7)
        dim = 16
        rows = [(f"r{i}", _pack_f32(_unit_vector(rng, dim))) for i in range(200)]
        query = _unit_vector(rng, dim)
        off = vs.top_k_by_dot(query, rows, 8, use_blas=False)
        sgemv = mock.Mock(side_effect=_test_sgemv)
        with mock.patch.object(vs, "_cblas_sgemv", return_value=sgemv):
            on = vs.top_k_by_dot(query, rows, 8, use_blas=True)
        sgemv.assert_called()
        self.assertEqual(off, on)

    def test_blas_loader_returning_none_falls_back_silently(self) -> None:
        rng = random.Random(11)
        dim = 16
        rows = [(f"r{i}", _pack_f32(_unit_vector(rng, dim))) for i in range(200)]
        query = _unit_vector(rng, dim)
        expected = vs.top_k_by_dot(query, rows, 5, use_blas=False)
        with mock.patch.object(vs, "_cblas_sgemv", return_value=None):
            got = vs.top_k_by_dot(query, rows, 5, use_blas=True)
        self.assertEqual(got, expected)

    def test_blas_raising_mid_call_falls_back_to_identical_rows(self) -> None:
        """"otherwise, or on ANY exception, use the existing pure-Python
        arithmetic" — a BLAS library that LOADS but then raises when
        actually invoked (a shape mismatch, a corrupted library, anything)
        must still produce the exact same top-K as the pure-Python path,
        not a partial or wrong result."""
        rng = random.Random(13)
        dim = 24
        rows = [(f"r{i}", _pack_f32(_unit_vector(rng, dim))) for i in range(200)]
        query = _unit_vector(rng, dim)
        expected = vs.top_k_by_dot(query, rows, 5, use_blas=False)

        def _raising_sgemv(*_a, **_k):
            raise OSError("simulated BLAS failure mid-call")

        with mock.patch.object(vs, "_cblas_sgemv", return_value=_raising_sgemv):
            got = vs.top_k_by_dot(query, rows, 5, use_blas=True)
        self.assertEqual(got, expected)


def _reference_cosine_candidates_snapshot(vec, profile, *, limit, kind=None):
    """Verbatim copy of ``khipu.hub_snapshot.cosine_candidates_snapshot`` as
    it existed before Phase 0 session C (finding B10) — full per-row Python
    scoring, no BLAS, ``chunk_text`` selected for every row. This is the
    reference the BLAS-backed replacement must match exactly; it is not
    itself exercised in production, only here."""
    import operator

    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    con = hs.open_snapshot()
    params: list = [profile]
    kind_clause = ""
    if kind:
        kind_clause = " AND kind = ?"
        params.append(kind)
    rows = con.execute(
        f"SELECT kind, ref, chunk_idx, chunk_text, embedding FROM memory_embeddings "
        f"WHERE profile = ? AND kind != 'commitment'{kind_clause} AND embedding IS NOT NULL",
        params,
    ).fetchall()
    qn = math.sqrt(sum(x * x for x in vec)) or 1.0
    qvec = tuple(x / qn for x in vec)
    dim = len(qvec)
    fmt = f"{dim}f"
    mul = operator.mul
    scored: list = []
    for knd, ref, chunk_idx, chunk_text, blob in rows:
        if not blob or len(blob) < dim * 4:
            continue
        doc = struct.unpack(fmt, blob[: dim * 4])
        score = sum(map(mul, qvec, doc))
        scored.append((
            score,
            {
                "kind": knd, "id": ref, "chunk_idx": chunk_idx,
                "score": round(float(score), 4),
                "label": clip_snippet(chunk_text or "", LABEL_LIMIT),
                "snippet": clip_snippet(chunk_text or "", SNIPPET_LIMIT),
                "rank_text": chunk_text or "",
            },
        ))
    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored[: max(1, int(limit))]]


def _build_fixture(path: Path) -> None:
    """3,000 seeded-random unit vectors of dim 768, plus: three exact
    duplicates (rows 10/11/12 share one vector), a too-short blob, and a
    commitment row (excluded by the WHERE clause in both implementations)."""
    con = sqlite3.connect(str(path))
    hs._create_schema(con)
    con.execute(
        "INSERT INTO embedding_profiles (id, provider, model, dim, is_active) "
        "VALUES ('p1', 'gemini', 'x', ?, 1)",
        (_DIM,),
    )
    rng = random.Random(20260928)
    vecs = [_unit_vector(rng) for _ in range(_N_ROWS)]
    vecs[11] = vecs[10]
    vecs[12] = vecs[10]
    for i, vec in enumerate(vecs):
        con.execute(
            "INSERT INTO memory_embeddings "
            "(profile, kind, ref, chunk_idx, chunk_text, embedding) "
            "VALUES ('p1', 'episode', ?, 0, ?, ?)",
            (f"row-{i}", f"chunk text for row {i}", _pack_f32(vec)),
        )
    con.execute(
        "INSERT INTO memory_embeddings "
        "(profile, kind, ref, chunk_idx, chunk_text, embedding) "
        "VALUES ('p1', 'episode', 'short-blob', 0, 'short', ?)",
        (struct.pack("3f", 1.0, 0.0, 0.0),),
    )
    con.execute(
        "INSERT INTO memory_embeddings "
        "(profile, kind, ref, chunk_idx, chunk_text, embedding) "
        "VALUES ('p1', 'commitment', 'commit-1', 0, 'a commitment', ?)",
        (_pack_f32(_unit_vector(rng)),),
    )
    con.commit()
    con.close()


class CosineCandidatesDifferentialTest(unittest.TestCase):
    """The fixture-driven oracle for finding B10's fix: on a realistic-size
    replica, the new BLAS-backed scan must match the old full-scan
    implementation exactly, whether or not BLAS actually ran."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.snap = Path(cls._tmp.name) / "hub_snapshot.sqlite"
        _build_fixture(cls.snap)
        cls.queries = [_unit_vector(random.Random(999 + i)) for i in range(20)]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_matches_reference_with_blas(self) -> None:
        sgemv = mock.Mock(side_effect=_test_sgemv)
        with mock.patch.object(hs, "snapshot_path", return_value=self.snap), \
             mock.patch.object(vs, "_cblas_sgemv", return_value=sgemv):
            for i, q in enumerate(self.queries):
                with self.subTest(query=i):
                    new_out = hs.cosine_candidates_snapshot(q, "p1", limit=8)
                    old_out = _reference_cosine_candidates_snapshot(q, "p1", limit=8)
                    self.assertEqual(new_out, old_out)
        sgemv.assert_called()

    def test_matches_reference_with_blas_forced_off(self) -> None:
        with mock.patch.object(hs, "snapshot_path", return_value=self.snap), \
                mock.patch.object(vs, "_cblas_sgemv", return_value=None):
            for i, q in enumerate(self.queries):
                with self.subTest(query=i):
                    new_out = hs.cosine_candidates_snapshot(q, "p1", limit=8)
                    old_out = _reference_cosine_candidates_snapshot(q, "p1", limit=8)
                    self.assertEqual(new_out, old_out)

    def test_commitment_row_excluded_in_the_fixture(self) -> None:
        with mock.patch.object(hs, "snapshot_path", return_value=self.snap):
            out = hs.cosine_candidates_snapshot(self.queries[0], "p1", limit=_N_ROWS)
        self.assertNotIn("commit-1", {r["id"] for r in out})

    def test_short_blob_row_excluded_in_the_fixture(self) -> None:
        with mock.patch.object(hs, "snapshot_path", return_value=self.snap):
            out = hs.cosine_candidates_snapshot(self.queries[0], "p1", limit=_N_ROWS)
        self.assertNotIn("short-blob", {r["id"] for r in out})


if __name__ == "__main__":
    unittest.main()
