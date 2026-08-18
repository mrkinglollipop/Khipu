"""khipu.embed_mirror is the single-vector embed client. Untested until the
2026-08-17 audit; its sibling khipu.embed had the live API key sitting in the
request URL, and the same construction lived here.

Nothing here reaches Gemini.
"""

from __future__ import annotations

import io
import json
import math
import unittest
import urllib.error
from unittest import mock

from khipu import embed_mirror as em


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(dim=em.DIM, value=3.0):
    return {"embedding": {"values": [value] * dim}}


class L2Test(unittest.TestCase):
    def test_a_normalized_vector_has_unit_length(self):
        out = em._l2([3.0, 4.0])
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in out)), 1.0)
        self.assertAlmostEqual(out[0], 0.6)

    def test_an_all_zero_vector_does_not_divide_by_zero(self):
        self.assertEqual(em._l2([0.0, 0.0]), [0.0, 0.0])


class EmbedTextTest(unittest.TestCase):
    def _call(self, text="hello", payload=None, **kw):
        seen = {}

        def fake_urlopen(req, **_):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = json.loads(req.data.decode())
            return _Resp(payload if payload is not None else _ok())

        with mock.patch("khipu.keychain.resolve_gemini_key", return_value="SECRET-KEY"), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            out = em.embed_text(text, **kw)
        return out, seen

    def test_empty_text_short_circuits_without_a_request(self):
        with mock.patch("khipu.keychain.resolve_gemini_key") as key:
            out = em.embed_text("   ")
        self.assertEqual(out, [0.0] * em.DIM)
        key.assert_not_called()

    def test_the_returned_vector_is_normalized_to_the_expected_dimension(self):
        out, _ = self._call()
        self.assertEqual(len(out), em.DIM)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in out)), 1.0)

    # --- regression: audit 2026-08-17 -----------------------------------------

    def test_the_api_key_travels_as_a_header_never_in_the_url(self):
        """A key in a query string lands in proxy logs, crash reports and
        anything that records a URL."""
        _, seen = self._call()
        self.assertNotIn("SECRET-KEY", seen["url"])
        self.assertNotIn("key=", seen["url"])
        self.assertEqual(seen["headers"].get("x-goog-api-key"), "SECRET-KEY")

    def test_a_wrong_dimension_is_refused_rather_than_stored(self):
        """A short vector would be inserted and then never match anything."""
        with self.assertRaises(RuntimeError) as e:
            self._call(payload=_ok(dim=8))
        self.assertIn("expected dim", str(e.exception))

    def test_long_text_is_truncated_before_it_is_sent(self):
        _, seen = self._call("x" * 20000)
        self.assertEqual(len(seen["body"]["content"]["parts"][0]["text"]), 8000)

    def test_the_requested_dimensionality_is_pinned_in_the_body(self):
        _, seen = self._call()
        self.assertEqual(seen["body"]["outputDimensionality"], em.DIM)

    def test_an_http_error_is_reraised_with_the_server_message_bounded(self):
        def boom(req, **_):
            raise urllib.error.HTTPError(
                req.full_url, 429, "rate", {}, io.BytesIO(b"slow down" * 200)
            )

        with mock.patch("khipu.keychain.resolve_gemini_key", return_value="K"), \
             mock.patch("urllib.request.urlopen", boom):
            with self.assertRaises(RuntimeError) as e:
                em.embed_text("hi")
        msg = str(e.exception)
        self.assertIn("embed HTTP 429", msg)
        self.assertLess(len(msg), 600)


if __name__ == "__main__":
    unittest.main()
