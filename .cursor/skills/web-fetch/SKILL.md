---
name: web-fetch
description: >-
  Fetch and extract web page content via Firecrawl scrape (primary) and Tavily
  extract (fallback). Use when Matt wants to fetch a page, extract a URL, scrape
  content, or pull text from a link.
---

# Web fetch / extract

**Firecrawl scrape first.** Use Python clients in `scripts/_clients/` — no local gateway.

## Quick path

```bash
cd "/Volumes/Cloud Storage/Claude"
PYTHONPATH=scripts/_clients python3 -c "
from firecrawl_call import scrape
print(scrape('https://example.com'))
"
```

Tavily extract fallback:

```bash
PYTHONPATH=scripts/_clients python3 -c "
from tavily_extract_call import tavily_extract_cached
print(tavily_extract_cached(['https://example.com']))
"
```

Key: `API Keys/Firecrawl API Key.txt` (gitignored).

## Fallback order

1. **Firecrawl** — `firecrawl_call.scrape(url)`
2. **Tavily extract** — when Firecrawl errors or returns empty
3. **Tavily MCP extract** — convenience when clients unavailable
4. Do **not** use browser MCP for routine page extraction

## Research intent

Multi-URL evidence gathering for reports → **[research/SKILL.md](../research/SKILL.md)** (`gather` or `deep` mode), not repeated inline fetches.

## Oracle

```bash
bash scripts/smoke_web_providers.sh
```
