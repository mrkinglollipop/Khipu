"""Lexical tokens for ILIKE coverage ranking and hybrid semantic rerank.

Default search used to ILIKE the whole query as one substring, so
``openbot ingest PR 36`` missed rows that clearly named OpenBot + PR #36.
Token coverage ranks by how many query terms hit, not phrase-as-blob.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

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


def search_tokens(term: str, *, limit: int = MAX_TOKENS) -> list[str]:
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
        if len(out) >= limit:
            break
    return out


# Automatic push weighs a row against the whole prompt, not the first
# MAX_TOKENS words a search leg queries on: a repeated template (a job
# brief's opening lines) fills those first words, and what is new in the
# prompt comes after them.
PROMPT_TERMS_LIMIT = 48

# A path, URL or command line names something to open or run, not what the
# prompt is about: the 2026-10-30 replay of c78a418f pushed 413 rows on job
# briefs with no memory to recall, admitted by rare words from the brief's
# own file paths. Push weighs only the prompt's prose (``prose_text``).
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
_TICK_RE = re.compile(r"`([^`\n]+)`")
_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://\S+")
_EDGE_PUNCT = "\"'()[]{}<>,;:!?`"
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S")
_FLAG_RE = re.compile(r"^--?[A-Za-z]")
_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,4}$")


def _is_path(chunk: str) -> bool:
    for sep in ("/", "\\"):
        if sep not in chunk:
            continue
        if chunk.startswith(("/", "~", "./", "../", ".\\", "..\\")) or chunk.endswith(sep):
            return True
        if chunk.count(sep) >= 2 or _EXT_RE.search(chunk):
            return True
        if sep == "\\" and re.match(r"^[A-Za-z]:\\", chunk):
            return True
    return False


def _is_shell(chunk: str) -> bool:
    """A flag, an ``NAME=value`` assignment or a ``$`` expansion."""
    return bool(_FLAG_RE.match(chunk) or _ASSIGN_RE.match(chunk) or chunk.startswith("$")
                or "$(" in chunk or "${" in chunk)


def _chunk_kind(raw: str) -> str:
    chunk = raw.strip(_EDGE_PUNCT).rstrip(".")
    if not chunk:
        return "prose"
    if _URL_RE.search(chunk) or _is_path(chunk):
        return "path"
    if _is_shell(chunk) or chunk in {"|", "||", "&&", ">", ">>", "2>&1"}:
        return "shell"
    return "prose"


def prose_text(prompt: str) -> str:
    """``prompt`` without code fences, paths, URLs and command lines.

    A backtick span goes when it holds a path or a shell chunk (a bare
    identifier in backticks is prose: it is often the subject). A line goes
    whole when at least a third of its chunks, and at least two, are paths
    or shell chunks; otherwise only those chunks go."""
    text = _FENCE_RE.sub(" ", prompt or "")

    def _tick(m: re.Match[str]) -> str:
        inner = m.group(1)
        return " " if any(_chunk_kind(c) != "prose" for c in inner.split()) else f" {inner} "

    text = _TICK_RE.sub(_tick, text)
    lines = []
    for line in text.splitlines():
        chunks = line.split()
        kinds = [_chunk_kind(c) for c in chunks]
        odd = sum(1 for k in kinds if k != "prose")
        if odd >= 2 and 3 * odd >= len(chunks):
            continue
        lines.append(" ".join(c for c, k in zip(chunks, kinds) if k == "prose"))
    return "\n".join(lines)


def prompt_terms(prompt: str) -> list[str]:
    """The prompt's prose words automatic push weighs a row against."""
    return search_tokens(prose_text(prompt), limit=PROMPT_TERMS_LIMIT)


def note_prompt_term_hits(row: dict[str, Any], text: str, terms: Sequence[str]) -> None:
    """Record which of ``terms`` the row's full ranking text names, before
    that text is dropped, as ``prompt_term_hits`` (a list of strings)."""
    blob = (text or "").lower()
    row["prompt_term_hits"] = [
        t for t in terms if re.search(_WORD_BOUNDARY.format(tok=re.escape(t)), blob)
    ]


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


def fuse_ranked_lists(
    lists: Sequence[Sequence[Mapping[str, Any]]],
    *,
    limit: int,
    key: Callable[[Mapping[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion across N already-ordered (best-first) lists.

    Generalizes ``hybrid_rerank``'s two-list cosine+lexical fusion (W2.1) to
    any number of ranked lists — e.g. cosine order, token-overlap order, and
    literal-ILIKE order — merged by identity (``key``, default
    ``(kind, str(id))``). A row absent from a list simply contributes nothing
    from that list; a row present in more lists, or ranked higher within a
    list, scores higher. The per-list term is identical to ``hybrid_rerank``'s
    (``1 / (RRF_K + rank + 1)``), so a two-list call reproduces the same score
    ``hybrid_rerank`` would compute.

    The first occurrence supplies the canonical payload. Later occurrences
    fill missing metadata and preserve the maximum ``cosine`` and
    ``lexical_hits`` evidence; ``profile`` follows the maximum cosine. Each
    entity votes once per list and duplicate chunks do not consume ranks.
    ``score`` is replaced with the summed RRF value.
    """
    if key is None:

        def key(r: Mapping[str, Any]) -> Any:
            return (r.get("kind"), str(r.get("id")))

    want = max(1, int(limit))
    merged: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    scores: dict[Any, float] = {}
    for rows in lists:
        seen_in_list: set[Any] = set()
        unique_rank = 0
        for row in rows:
            k = key(row)
            if k not in merged:
                merged[k] = dict(row)
                order.append(k)
                scores[k] = 0.0
            target = merged[k]

            # Preserve evidence across chunks and retrieval legs without
            # replacing the canonical row based on incomparable raw scores.
            for field in ("cosine", "lexical_hits"):
                try:
                    incoming = float(row[field])
                except (TypeError, ValueError):
                    continue
                except KeyError:
                    continue
                try:
                    current = float(target[field])
                except (KeyError, TypeError, ValueError):
                    current = float("-inf")
                if incoming > current:
                    target[field] = row[field]
                    if field == "cosine":
                        if row.get("profile") not in (None, ""):
                            target["profile"] = row["profile"]
                        else:
                            target.pop("profile", None)
            for field, value in row.items():
                if field in {"score", "profile"}:
                    continue
                if field not in target or target[field] in (None, ""):
                    if value not in (None, ""):
                        target[field] = value

            if k in seen_in_list:
                continue
            seen_in_list.add(k)
            scores[k] += 1.0 / (RRF_K + unique_rank + 1)
            unique_rank += 1
    ranked = sorted(range(len(order)), key=lambda i: (-scores[order[i]], i))
    out: list[dict[str, Any]] = []
    for i in ranked[:want]:
        k = order[i]
        row = merged[k]
        row["score"] = round(scores[k], 6)
        out.append(row)
    return out


_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([dhm])\s*$", re.IGNORECASE)


def parse_time_filter(value: str) -> datetime:
    """``since``/``until`` filter value → aware UTC datetime (W2.3).

    Accepts an ISO date/datetime (``2026-08-01``, ``2026-08-01T12:00:00Z``) or
    a relative offset from now: ``7d``, ``24h``, ``30m``. Naive ISO values are
    treated as UTC. Raises ``ValueError`` on anything else so a typo'd filter
    fails loudly instead of silently matching nothing.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("empty time filter")
    m = _RELATIVE_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        return datetime.now(timezone.utc) - delta
    iso = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
