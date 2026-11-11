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

The owner's own lines are ``episodes.verbatim.quotes`` (up to three user turns
of the window) and, for conversations captured since ``owner_lines`` existed,
``episodes.verbatim.owner_lines`` (every typed user turn of the window). Only
"only my words" reads ``owner_lines``, and on a row that has the list it reads
nothing else (``owner_lines_of``); a job's row has no owner words
(``job_row_sql``). The quote shown without it, the title and ``activity``
read ``quotes`` alone, as before. Errors, commands, paths and the capture
note are not the owner's words.
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


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [q for q in value if isinstance(q, str) and q.strip()]


def _verbatim_dict(verbatim: Any) -> Any:
    if isinstance(verbatim, str):
        try:
            import json

            return json.loads(verbatim)
        except ValueError:
            return None
    return verbatim


def quotes_of(verbatim: Any) -> list[str]:
    """The owner's lines out of a stored ``verbatim`` value (dict or JSON text)."""
    verbatim = _verbatim_dict(verbatim)
    if isinstance(verbatim, dict):
        verbatim = verbatim.get("quotes")
    return _strings(verbatim)


def owner_lines_of(verbatim: Any) -> list[str]:
    """Every stored owner line, for "only my words". A row with an
    ``owner_lines`` list (a conversation captured since the list was always
    stored; empty when nothing was typed) has exactly those: its ``quotes`` can
    hold a start brief or a relay the capture knew was not typed. A row
    without the key (captured before) has its ``quotes``."""
    verbatim = _verbatim_dict(verbatim)
    if not isinstance(verbatim, dict):
        return []
    if isinstance(verbatim.get("owner_lines"), list):
        return list(dict.fromkeys(_strings(verbatim["owner_lines"])))
    return list(dict.fromkeys(_strings(verbatim.get("quotes"))))


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
            for c in ("verbatim", "origin", "harness", "project", "scope", "raw", "parent_session_id")}


def job_row_sql(cur) -> str:
    """SQL true for an episode a job captured, as commitments.capture_origin
    decides it: lineage (``parent_session_id``) or ``session_origin: job`` in
    the stored payload. A job's user turns are briefs and relays, never the
    owner's words."""
    have = _columns(cur)
    tests = []
    if have["parent_session_id"]:
        tests.append("COALESCE(btrim(parent_session_id), '') <> ''")
    if have["raw"]:
        tests.append("lower(btrim(COALESCE(raw->>'session_origin', ''))) = 'job'")
    return "(" + " OR ".join(tests) + ")" if tests else "FALSE"


def episode_facts(cur, ids: list[Any], *, owner_lines: bool = False) -> dict[str, dict[str, Any]]:
    """One query for the rows' summary, owner lines, origin, tool and project.
    ``owner_lines`` (only my words): also read ``verbatim.owner_lines``."""
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
        + (", verbatim->'owner_lines', " + job_row_sql(cur) if owner_lines and have["verbatim"] else "")
        + " FROM episodes WHERE id = ANY(%s)",
        (numbers,),
    )
    out = {}
    for r in cur.fetchall():
        out[str(r[0])] = {"summary": r[1], "quotes": quotes_of(r[2]), "origin": r[3], "harness": r[4],
                          "session_id": r[5], "project": r[6], "summary_kind": r[7]}
        if owner_lines:
            # A job's row has no owner lines; a row with the key has exactly
            # those (owner_lines_of), a row without it its quotes.
            vb = {"quotes": r[2]}
            if len(r) > 8 and isinstance(r[8], list):
                vb["owner_lines"] = r[8]
            out[str(r[0])]["owner_lines"] = [] if len(r) > 9 and r[9] else owner_lines_of(vb)
    return out


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
    facts = episode_facts(cur, [r.get("id") for r in rows if r.get("kind") == "episode"], owner_lines=True)
    kept = []
    for r in rows:
        if r.get("kind") != "episode":
            continue
        line = owner_line((facts.get(str(r.get("id"))) or {}).get("owner_lines") or [], phrase, tokens)
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


def _owner_array(verbatim: str, key: str) -> str:
    """``verbatim -> key`` when it is a JSON array, else an empty one."""
    return (f"(CASE WHEN jsonb_typeof({verbatim}->'{key}') = 'array' "
            f"THEN {verbatim}->'{key}' ELSE '[]'::jsonb END)")


def owner_candidates(cur, term: str, limit: int, filters=None) -> list[dict]:
    # forget-fence source: a search leg; every search output fences the fused rows (khipu_search, khipu search, the prompt push, offline search)
    """Episodes with an owner line holding the phrase or every query word, newest
    first, in the shape ``cli._literal_candidates`` returns. The index from 0051
    narrows by the words in any verbatim string (``owner_lines`` included);
    the check is exact, on ``owner_lines`` when the row has the list and on
    ``quotes`` when it does not (``owner_lines_of``). A job's row is never
    a candidate."""
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
        + "EXISTS (SELECT 1 FROM jsonb_array_elements_text("
        + f"(CASE WHEN jsonb_typeof({verbatim}->'owner_lines') = 'array' THEN {verbatim}->'owner_lines' "
        + "ELSE " + _owner_array(verbatim, "quotes") + " END)"
        + ") AS q WHERE " + " AND ".join(line_tests) + ") "
        f"AND NOT {job_row_sql(cur)} AND ({ep_filter}) ORDER BY ts DESC NULLS LAST, id DESC LIMIT %(lim)s OFFSET %(off)s"
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
            line = owner_line(owner_lines_of(vb), phrase, tokens)
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
