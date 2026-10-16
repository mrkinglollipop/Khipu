"""The prompt lane's cosine leg in a cold one-shot hook (2026-10-16): about a
third of replayed prompts logged ``degraded=embedding error`` and answered on
words alone. The error was the replica scan's own deadline: the vector matrix
loaded only after the query embedding returned. These tests hold the fix: the
matrix loads while the embedding is in flight, a scan deadline reads as late
rather than an error, and a transient provider error is tried once more when
the deadline leaves room.

Everything is faked: no replica, no provider, short sleeps for slow legs.
"""
from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from khipu import hub_snapshot
from khipu import recall_prompt as rp
from tests.test_prompt_lane import _fresh_cosine_row
from tests.test_prompt_lane_provisional import PROMPT, _lane

LOAD_S = 0.18
EMBED_S = 0.18


def _http_error(code: int) -> RuntimeError:
    err = urllib.error.HTTPError("https://provider.invalid", code, "x", {}, io.BytesIO(b""))
    try:
        raise RuntimeError(f"embed HTTP {code}") from err
    except RuntimeError as exc:
        return exc


class _SlowMatrix:
    """A stand-in for the replica's generation cache: the first load takes
    LOAD_S under one lock, later callers get the cached rows."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.loaded = False
        self.loads = 0

    def rows(self, *_a, **_k):
        with self.lock:
            if not self.loaded:
                self.loads += 1
                time.sleep(LOAD_S)
                self.loaded = True
        return [], {}


class ColdMatrixTest(unittest.TestCase):
    def _cosine(self, matrix: _SlowMatrix):
        def cosine(_vec, profile, *, limit, kind=None, deadline=None):
            matrix.rows(None, profile, kind)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("replica scan deadline expired")
            return [_fresh_cosine_row()]
        return cosine

    def test_the_matrix_loads_while_the_embedding_is_in_flight(self) -> None:
        # Embedding then load in series (0.36 s) runs past the 0.25 s lane
        # deadline; side by side (0.18 s) the cosine leg lands.
        matrix = _SlowMatrix()

        def embed(*_a, **_k):
            time.sleep(EMBED_S)
            return [1.0]

        with _lane(embed=embed, cosine=self._cosine(matrix)), \
                mock.patch.object(hub_snapshot, "_matrix_rows", side_effect=matrix.rows):
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertIsNone(out["degraded"])
        self.assertIn("cosine", out["legs"])
        self.assertEqual(matrix.loads, 1)

    def test_a_scan_past_its_deadline_is_late_not_an_error(self) -> None:
        def cosine(*_a, **_k):
            raise TimeoutError("replica scan deadline expired")

        with _lane(cosine=cosine), mock.patch.object(hub_snapshot, "warm_matrix"):
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertEqual(out["degraded"], "embedding late")
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])

    def test_warm_matrix_never_raises(self) -> None:
        with mock.patch.object(hub_snapshot, "_matrix_rows", side_effect=OSError("gone")):
            self.assertIsNone(hub_snapshot.warm_matrix("p1"))


class QueryEmbedRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="khipu-qembed-"))
        patch = mock.patch.object(rp, "_query_embed_cache_path", return_value=self.tmp / "c.json")
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_transient_provider_error_is_tried_once_more_within_the_deadline(self) -> None:
        with mock.patch("khipu.embed.embed_one", side_effect=[_http_error(503), [0.5]]) as m:
            vec = rp._cached_query_embed("routing push", "p1", time.monotonic() + 0.9)
        self.assertEqual(vec, [0.5])
        self.assertEqual(m.call_count, 2)
        # The second try ends by the deadline.
        self.assertLessEqual(m.call_args.kwargs["timeout"], 0.9)

    def test_a_dropped_connection_is_tried_once_more(self) -> None:
        dropped = RuntimeError("embed network error")
        dropped.__cause__ = ConnectionResetError("reset")
        with mock.patch("khipu.embed.embed_one", side_effect=[dropped, [0.5]]) as m:
            self.assertEqual(rp._cached_query_embed("q", "p1", time.monotonic() + 0.9), [0.5])
        self.assertEqual(m.call_count, 2)

    def test_no_second_try_without_room_before_the_deadline(self) -> None:
        with mock.patch("khipu.embed.embed_one", side_effect=[_http_error(503), [0.5]]) as m:
            with self.assertRaises(RuntimeError):
                rp._cached_query_embed("q", "p1", time.monotonic() + rp.QUERY_EMBED_RETRY_MIN_S / 2)
        self.assertEqual(m.call_count, 1)

    def test_a_refused_key_is_not_tried_again(self) -> None:
        with mock.patch("khipu.embed.embed_one", side_effect=[_http_error(403), [0.5]]) as m:
            with self.assertRaises(RuntimeError):
                rp._cached_query_embed("q", "p1", time.monotonic() + 0.9)
        self.assertEqual(m.call_count, 1)

    def test_at_most_one_retry(self) -> None:
        with mock.patch("khipu.embed.embed_one", side_effect=[_http_error(503), _http_error(503), [0.5]]) as m:
            with self.assertRaises(RuntimeError):
                rp._cached_query_embed("q", "p1", time.monotonic() + 5)
        self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
