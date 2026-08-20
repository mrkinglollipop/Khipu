"""Single-text Gemini embed client (kept for callers that need one vector).

The corpus path lives in khipu.embed (batched, profile-aware). This module is
the thin single-call form used by tests and by anything that just wants one
768-d vector for a string.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request

MODEL = "gemini-embedding-001"
MODEL_2 = "gemini-embedding-2"
DIM = 768


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed_text(text: str, *, model: str = MODEL) -> list[float]:
    """Embed one text with the given Gemini embedding model @768 + L2.

    Default remains gemini-embedding-001. Callers that need Embedding 2
    prefixes must apply them before calling; this helper does not rewrite text.
    """
    if not text.strip():
        return [0.0] * DIM
    from khipu.keychain import resolve_gemini_key

    key = resolve_gemini_key()
    # Header auth, not ?key= — a live credential must not sit in a URL
    # (audit 2026-08-17).
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
    body = {
        "model": f"models/{model}",
        "content": {"parts": [{"text": text[:8000]}]},
        "outputDimensionality": DIM,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"embed HTTP {e.code}: {err[:500]}") from e
    values = payload["embedding"]["values"]
    if len(values) != DIM:
        raise RuntimeError(f"expected dim {DIM}, got {len(values)}")
    return _l2(values)
