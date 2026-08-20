"""Lexical tokens for ILIKE coverage ranking and hybrid semantic rerank.

Default search used to ILIKE the whole query as one substring, so
``openbot ingest PR 36`` missed rows that clearly named OpenBot + PR #36.
Token coverage ranks by how many query terms hit, not phrase-as-blob.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# Function words that would otherwise match almost every episode.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "why",
        "with",
    }
)
MAX_TOKENS = 8
RRF_K = 20
_WORD_BOUNDARY = r"(?<![a-z0-9]){tok}(?![a-z0-9])"


def search_tokens(term: str) -> list[str]:
    """Lowercased content tokens. Length >= 3, or digits of length >= 2."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in TOKEN_RE.findall(term or ""):
        key = raw.lower()
        if key in seen or key in STOPWORDS:
            continue
        if len(key) < 3 and not (key.isdigit() and len(key) >= 2):
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= MAX_TOKENS:
            break
    return out


def token_hit_count(text: str, tokens: Sequence[str]) -> int:
    blob = (text or "").lower()
    n = 0
    for tok in tokens:
        if re.search(_WORD_BOUNDARY.format(tok=re.escape(tok)), blob):
            n += 1
    return n


def _row_text(row: Mapping[str, Any]) -> str:
    """Text the RRF token-overlap side may see.

    Search teasers stay summary-only. Ranking uses ``rank_text`` when present
    so extract fields that were already embedded (topics / decisions /
    preferences / people) can lift a hit whose snippet never names them.
    """
    parts: list[str] = []
    for key in ("label", "snippet", "id", "rank_text"):
        val = row.get(key)
        if val:
            parts.append(str(val))
    extra = row.get("topics")
    if isinstance(extra, (list, tuple)):
        parts.extend(str(item) for item in extra if item)
    elif extra:
        parts.append(str(extra))
    return " ".join(parts)


def hybrid_rerank(
    rows: Sequence[Mapping[str, Any]], query: str, *, limit: int
) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion of current order (cosine) with token-hit order.

    Cosine-alone left relevant and irrelevant episodes in a 0.02-wide band.
    Fusing token overlap lifts hits that actually name the query terms
    (including extract fields in ``rank_text``) without a second embedding call.
    """
    want = max(1, int(limit))
    material = [dict(r) for r in rows]
    tokens = search_tokens(query)
    if not material or not tokens:
        return material[:want]
    lex_order = sorted(
        range(len(material)),
        key=lambda i: (-token_hit_count(_row_text(material[i]), tokens), i),
    )
    lex_rank = {i: rank + 1 for rank, i in enumerate(lex_order)}
    scored: list[tuple[float, int, int, dict[str, Any]]] = []
    for i, row in enumerate(material):
        hits = token_hit_count(_row_text(row), tokens)
        rrf = 1.0 / (RRF_K + i + 1) + 1.0 / (RRF_K + lex_rank[i])
        scored.append((rrf, hits, i, row))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [row for _, _, _, row in scored[:want]]
