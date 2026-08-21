---
name: web-search
description: >-
  Web search via Firecrawl (general) and Tavily (news/recency) through
  scripts/_clients. Prefer this over tavily-search plugin skill. Use for
  lookups, articles, recent news. Multi-step or report-grade → research skill.
---

# Web search

**Provider-first.** Use Python clients in `scripts/_clients/` — no local gateway.

**Do not load** the Tavily plugin skill `tavily-search` (or `tavily-research` for multi-step work) when this harness is active — use this skill or `research` instead. Disable the Tavily plugin in Settings → Plugins if the picker shows duplicates. See `cursor-web-providers.md` § Plugin dedup.

## Quick path

```bash
cd "/Volumes/Cloud Storage/Claude"
PYTHONPATH=scripts/_clients python3 -c "
from firecrawl_call import search
print(search('your query', limit=5))
"
```

News / recency (Tavily):

```bash
PYTHONPATH=scripts/_clients python3 -c "
from tavily_call import tavily_search_cached
print(tavily_search_cached('latest AI news', topic='news', days=7, max_results=5))
"
```

For news queries, **always** pass `topic='news'` and a recency flag (`days`, `time_range`, or date range).

## Provider split

| Intent | Client |
|--------|--------|
| General web search | `firecrawl_call.search()` |
| News / recency | `tavily_call.tavily_search_cached(..., topic='news', days=N)` |
| Video discovery | `youtube_call.search_videos()` (research skill owns multi-step video runs) |

Keys: `API Keys/Firecrawl API Key.txt`, `API Keys/Tavily API Key.txt` (gitignored).

## Research intent → research skill

When Matt says **"research X"**, wants a **comprehensive report**, **literature review**, or **multi-source investigation** — load **[research/SKILL.md](../research/SKILL.md)** and dispatch the `research` agent (`gather` or `deep` mode). Do **not** stack deep pipeline on a one-line lookup.

Single-shot "what is / look up / find" (no research intent) → this skill inline.

## Fallback order

1. **Firecrawl** — general search
2. **Tavily** — when news/recency matters, or Firecrawl errors
3. **Tavily MCP** (`plugin-tavily-tavily`) — last resort only when Python clients fail
4. Do **not** load Tavily **plugin skills** (`tavily-search`, etc.) — they bypass harness clients
5. Do **not** use browser MCP for routine search

## Oracle

```bash
bash scripts/smoke_web_providers.sh
bash scripts/smoke_deep_research.sh
```
