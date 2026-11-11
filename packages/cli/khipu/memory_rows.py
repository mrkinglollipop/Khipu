"""What the Memory screen shows for a search hit or a recent conversation.

Used by ``khipu search``, the MCP ``khipu_search`` tool when it is given
``owner_words``, and ``khipu activity``. Never by per-prompt recall: that path
calls ``hybrid_search`` without ``memory_fields``, so none of this runs there.

Fields added to an episode row (appended after the existing keys):

* ``quote``   the owner's matching line (``verbatim.quotes``), else the matching
  summary line; for ``activity`` the owner's first line. ``None`` when there is none.
* ``title``   first sentence of the summary (or of the owner's first line when
  the episode has no summary). Episodes have no title column.
* ``tool``    the agent tool that captured it ("Claude Code", "Codex", ...).
* ``mac``     the name of the Mac that wrote it (``macs.mac_label`` / ``macs.name_of``), else ``None``
  for a row from the gateway.
* ``project`` ``COALESCE(project, scope)``, else ``None``.

The owner's own lines are ``episodes.verbatim.quotes`` (the user turns of the
window). Errors, commands, paths and the capture note are not the owner's words.
"""
from __future__ import annotations

import re
from typing import Any

QUOTE_LIMIT = 300
PAGE_CAP = 6   # most SQL pages owner_candidates reads to find ``limit`` typed matches
TITLE_LIMIT = 90

TOOL_NAMES = {
    "claude_code": "Claude Code", "cursor": "Cursor", "codex": "Codex", "aegis": "Aegis",
    "grok": "Grok Build", "grokbot": "Grok Bot", "grok_bot": "Grok Bot",
    "antigravity": "Antigravity",
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def quotes_of(verbatim: Any) -> list[str]:
    """The owner's lines out of a stored ``verbatim`` value (dict or JSON text)."""
    if isinstance(verbatim, str):
        try:
            import json

            verbatim = json.loads(verbatim)
        except ValueError:
            return []
    if isinstance(verbatim, dict):
        verbatim = verbatim.get("quotes")
    if not isinstance(verbatim, list):
        return []
    return [q for q in verbatim if isinstance(q, str) and q.strip()]


def typed_quotes(quotes: list[str]) -> list[str]:
    """The owner lines the owner typed: ``verbatim.quotes`` holds every user-role
    message of the window, scheduled prompts, notices and job briefs included,
    and ``turn_text.is_automated`` is the one test that drops those."""
    from khipu.turn_text import is_automated

    return [q for q in quotes if not is_automated(q)]


def query_terms(query: str) -> tuple[str, list[str]]:
    """(phrase, tokens) a line must contain, case-insensitively."""
    from khipu.search_text import explicit_search_tokens, search_phrase

    phrase = search_phrase(query or "").casefold()
    return phrase, [t.casefold() for t in explicit_search_tokens(phrase)]


def _line_matches(line: str, phrase: str, tokens: list[str]) -> bool:
    low = line.casefold()
    if phrase and phrase in low:
        return True
    return bool(tokens) and all(t in low for t in tokens)


def owner_line(quotes: list[str], phrase: str, tokens: list[str]) -> str | None:
    """The first typed owner line holding the whole phrase, or every query word."""
    for q in typed_quotes(quotes):
        if _line_matches(q, phrase, tokens):
            return q
    return None


def _highlight_line(text: str, phrase: str, tokens: list[str]) -> str | None:
    """The first line of ``text`` naming the phrase or any query word."""
    for line in str(text or "").splitlines():
        low = line.casefold()
        if line.strip() and ((phrase and phrase in low) or any(t in low for t in tokens)):
            return line
    return None


def _clip_around(line: str, phrase: str, tokens: list[str]) -> str:
    """Clip a long line around its match so the match stays inside the quote."""
    line = " ".join(line.split())
    if len(line) <= QUOTE_LIMIT:
        return line
    low = line.casefold()
    at = low.find(phrase) if phrase else -1
    if at < 0:
        hits = [low.find(t) for t in tokens if t in low]
        at = min(hits) if hits else 0
    start = max(0, at - QUOTE_LIMIT // 3)
    end = min(len(line), start + QUOTE_LIMIT)
    return ("…" if start else "") + line[start:end].strip() + ("…" if end < len(line) else "")


def title_of(summary: Any, quotes: list[str], verbatim_only: bool = False) -> str | None:
    from khipu.summary_kind import summary_text_is_empty

    text = "" if verbatim_only or summary_text_is_empty(summary) else str(summary)
    if not text.strip() and quotes:
        # The first line the owner typed; the first stored quote only when none was typed.
        text = (typed_quotes(quotes) or [quotes[0]])[0]
    for line in text.splitlines():
        line = line.strip().lstrip("#*- ").strip()
        if line:
            cut = re.search(r"[.!?](\s|$)", line)
            if cut and cut.start() >= 6:
                line = line[: cut.start() + 1]
            return _clip(line, TITLE_LIMIT)
    return None


def tool_of(harness: Any, session_id: Any) -> str | None:
    key = str(harness or "").strip() or str(session_id or "").split(":", 1)[0].strip()
    if not key:
        return None
    return TOOL_NAMES.get(key, key)


def mac_resolver():
    """origin -> the name of the Mac that wrote the row, from ``macs`` (the one
    place a Mac gets a name). ``local:<hash>`` equal to this Mac's origin is
    this Mac; any other ``local:`` origin is another Mac (a hardware origin is
    not a Mac id, so it gets the shared fallback). A gateway origin or none is
    not a Mac: ``None``."""
    from khipu import macs

    try:
        from khipu.machine_identity import local_origin

        mine = local_origin()
    except Exception:  # noqa: BLE001
        mine = None
    this_name, other_name = macs.mac_label(), macs.name_of(None)

    def resolve(origin: Any) -> str | None:
        origin = str(origin or "")
        if not origin.startswith("local:"):
            return None
        return this_name if mine and origin == mine else other_name

    return resolve


def _columns(cur) -> dict[str, bool]:
    from khipu.db import has_columns

    return {c: has_columns(cur, "episodes", c)
            for c in ("verbatim", "origin", "harness", "project", "scope", "raw")}


def episode_facts(cur, ids: list[Any]) -> dict[str, dict[str, Any]]:
    """One query for the rows' summary, owner lines, origin, tool and project."""
    numbers = sorted({int(i) for i in ids if str(i).isdigit()})
    if not numbers:
        return {}
    have = _columns(cur)
    project = ("COALESCE(project, scope)" if have["project"] else "scope") if have["scope"] or have["project"] else "NULL"
    cur.execute(
        "SELECT id, summary, "
        + ("verbatim->'quotes'" if have["verbatim"] else "NULL::jsonb")
        + ", " + ("origin" if have["origin"] else "NULL")
        + ", " + ("harness" if have["harness"] else "NULL")
        + ", session_id, " + project + ", "
        + ("raw->>'summary_kind'" if have["raw"] else "NULL")
        + " FROM episodes WHERE id = ANY(%s)",
        (numbers,),
    )
    return {str(r[0]): {"summary": r[1], "quotes": quotes_of(r[2]), "origin": r[3], "harness": r[4],
                        "session_id": r[5], "project": r[6], "summary_kind": r[7]}
            for r in cur.fetchall()}


def _fill(row: dict, facts: dict, resolve, quote: str | None) -> None:
    row["quote"] = quote
    row["title"] = title_of(facts.get("summary"), facts.get("quotes") or [],
                            facts.get("summary_kind") == "verbatim_only")
    row["tool"] = tool_of(facts.get("harness"), facts.get("session_id"))
    row["mac"] = resolve(facts.get("origin"))
    row["project"] = (str(facts.get("project") or row.get("project") or "").strip() or None)


def keep_owner_matches(cur, rows: list[dict], query: str) -> list[dict]:
    """With "only my words" on: keep only episode rows with an owner line that
    holds the phrase or every query word, and set that line as ``quote``."""
    phrase, tokens = query_terms(query)
    facts = episode_facts(cur, [r.get("id") for r in rows if r.get("kind") == "episode"])
    kept = []
    for r in rows:
        if r.get("kind") != "episode":
            continue
        line = owner_line((facts.get(str(r.get("id"))) or {}).get("quotes") or [], phrase, tokens)
        if line is not None:
            r["quote"] = _clip_around(line, phrase, tokens)
            kept.append(r)
    return kept


def annotate_search_rows(cur, rows: list[dict], query: str) -> list[dict]:
    """Add quote/title/tool/mac/project to every search row (episodes from the
    row's episode, topics and nodes with only their own label)."""
    phrase, tokens = query_terms(query)
    facts = episode_facts(cur, [r.get("id") for r in rows if r.get("kind") == "episode"])
    resolve = mac_resolver()
    for r in rows:
        if r.get("kind") == "episode":
            f = facts.get(str(r.get("id"))) or {}
            quote = r.get("quote")
            if quote is None:
                line = owner_line(f.get("quotes") or [], phrase, tokens) or _highlight_line(
                    f.get("summary") if f.get("summary_kind") != "verbatim_only" else "", phrase, tokens)
                quote = _clip_around(line, phrase, tokens) if line else None
            _fill(r, f, resolve, quote)
        else:
            r.update(quote=None, title=_clip(r.get("label") or "", TITLE_LIMIT) or None, tool=None, mac=None,
                     project=(str(r.get("project") or "").strip() or None))
    return rows


def annotate_activity_rows(cur, rows: list[dict]) -> list[dict]:
    """Add quote/title/tool/mac/project to ``activity``'s recent episodes. The
    quote is the owner's opening line."""
    facts = episode_facts(cur, [r.get("id") for r in rows])
    resolve = mac_resolver()
    for r in rows:
        f = facts.get(str(r.get("id"))) or {}
        first = (typed_quotes(f.get("quotes") or []) or [None])[0]
        _fill(r, f, resolve, _clip(first, QUOTE_LIMIT) if first else None)
        r["project"] = r["project"] or (str(r.get("scope") or "").strip() or None)
    return rows


def owner_candidates(cur, term: str, limit: int, filters=None) -> list[dict]:
    # forget-fence source: a search leg; every search output fences the fused rows (khipu_search, khipu search, the prompt push, offline search)
    """Episodes with an owner line holding the phrase or every query word, newest
    first, in the shape ``cli._literal_candidates`` returns. The index from 0051
    narrows by the words in any verbatim string; the quotes check is exact."""
    from khipu import cli
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    verbatim = cli._episode_verbatim_select(cur)
    if verbatim == "NULL::jsonb":
        return []
    phrase, tokens = query_terms(term)
    needles = tokens or ([phrase] if phrase else [])
    if not needles:
        return []
    params: dict[str, Any] = {}
    ep_filter = "TRUE"
    if filters is not None and filters.active:
        filters.expand_project(cur)
        params.update(filters.params)
        ep_filter = filters.episode_sql(cur)
    line_tests, index_tests = [], []
    for i, needle in enumerate(needles):
        params[f"ow{i}"] = f"%{cli._escape_like(needle)}%"
        line_tests.append(f"lower(q) LIKE %(ow{i})s ESCAPE '\\'")
        index_tests.append(f"public.khipu_owner_words({verbatim}) LIKE %(ow{i})s ESCAPE '\\'")
    cur.execute("SELECT EXISTS (SELECT 1 FROM pg_index WHERE indexrelid = "
                "to_regclass('public.idx_episodes_owner_words_trgm') AND indisvalid AND indisready)")
    indexed = bool(cur.fetchone()[0])
    want = max(1, int(limit))
    sql = (
        f"SELECT id::text, summary, topics, decisions, preferences, people, ts, {verbatim} AS verbatim "
        f"FROM episodes WHERE {cli._episode_live_clause(cur)}"
        + (" AND ".join(index_tests) + " AND " if indexed else "")
        + "CASE WHEN jsonb_typeof(" + verbatim + "->'quotes') = 'array' THEN EXISTS ("
        "SELECT 1 FROM jsonb_array_elements_text(" + verbatim + "->'quotes') AS q WHERE "
        + " AND ".join(line_tests) + ") ELSE false END "
        f"AND ({ep_filter}) ORDER BY ts DESC NULLS LAST, id DESC LIMIT %(lim)s OFFSET %(off)s"
    )
    # A scheduled prompt that quotes the owner matches in SQL but is not his
    # line, so the exact check can drop rows: read on until ``want`` typed
    # matches, the hits run out, or PAGE_CAP pages have been read.
    out: list[dict] = []
    offset, page = 0, want
    for _ in range(PAGE_CAP):
        params["lim"], params["off"] = page, offset
        cur.execute(sql, params)
        rows = cur.fetchall()
        for eid, summary, topics, decisions, preferences, people, ts, vb in rows:
            line = owner_line(quotes_of(vb), phrase, tokens)
            if line is None:
                continue
            out.append({
                "kind": "episode", "id": eid,
                "label": clip_snippet(summary or "", LABEL_LIMIT),
                "snippet": clip_snippet(summary or "", SNIPPET_LIMIT),
                "rank_text": line, "exact_phrase": bool(phrase and phrase in line.casefold()),
            })
        if len(out) >= want or len(rows) < page:
            break
        offset += page
        page = max(want, 50)
    return out[:want]
