---
name: research
description: >-
  Unified research router — AskQuestion for gather vs deep when mode is ambiguous;
  gather returns sources + short synthesis + conflicts (orchestrator relays).
  Triage quick/low→AskQuestion; single lookup→web-search. Deep: effort+profile
  AskQuestion then run.py. Tavily + Firecrawl + YouTube via scripts/_clients;
  X deferred. Prefer this over tavily-research / tavily-search plugin skills.
---

# Research

Orchestrator-owned **triage + dispatch**. LLMs do not browse independently in gather or deep — evidence comes from `scripts/_clients` (gather) or `web_context.py` inside `run.py` (deep).

**Not research:** single lookups → `web-search` skill. URL fetch → `web-fetch` skill.

**Do not load** official Tavily plugin skills (`tavily-research`, `tavily-search`, `tavily-extract`, …) when this harness is active — they use the `tvly` CLI and bypass gather/deep evidence, YouTube, and deep pipeline wiring. Disable the Tavily plugin in Settings → Plugins if the picker shows duplicates. See `/Volumes/Cloud Storage/Memory/conversations/topics/cursor-web-providers.md` § Plugin dedup.

## Triage table

| Matt says | Mode | Effort / preset | Dispatch |
|-----------|------|-----------------|----------|
| "what is / look up / find" (no research intent) | **inline** | — | `web-search` skill |
| "research X" (bare) | **AskQuestion mode** | — | mode popup → then gather or deep |
| "research X, quick / low effort" | **AskQuestion mode** | `quick` (if Deep chosen) | effort-only does **not** skip mode popup |
| "comprehensive / report / literature review / scholarly" | **deep** | **Ask if unspecified** | mode=deep, then Deep preflight below |
| "high / deep dive / aggressive" | **deep** | `aggressive` (effort explicit) | Deep preflight still for **profile** if missing |
| "latest news on X" | **gather** | — | Tavily news + recency in gather (no mode popup) |
| "video / tutorial / talk / YouTube" | **gather** | — | YouTube prioritized + web providers (no mode popup) |
| explicit `gather` / `deep` | named mode | as applicable | gather: dispatch; deep: Deep preflight if effort/profile missing |

**Effort mapping:** low → `quick`, medium → `balanced`, high → `aggressive`.

**HARD — never silent-default deep:** Do **not** invent `--preset balanced` or `--profile default` when Matt did not name them. Missing either → Deep preflight (below). Only exception: Matt said `your call` / `use defaults` / `don't ask` this turn.

## § Mode preflight — AskQuestion (ambiguous only)

**Fire when:** intent is research-agent dispatch and mode is not implied (bare `"research X"`, or effort-only like quick/low with no report/deep cues).

**Skip when any of:** explicit gather/deep/comprehensive/report/literature review/scholarly/deep dive; single lookup → `web-search`; strong gather cues (`latest news`, video/tutorial/YouTube); Matt already named mode in-thread this turn.

**Skipping mode AskQuestion does not skip Deep preflight.** `"comprehensive report"` → deep mode without mode popup, but still ask effort + profile unless both are explicit.

**Popup options (Recommended = Gather):**

| Option | Meaning |
|--------|---------|
| **Gather (Recommended)** | multi-source evidence + short synthesis + conflicts; foreground; max 5 paid calls |
| **Deep** | full cited report with verifier pipeline; background; then effort + profile AskQuestions below |

After Gather is chosen, agent returns sources + short synth + conflicts; **orchestrator relays** to Matt (Flavor-ON).

## Deep preflight — AskQuestion (required)

Before `Task(research, mode=deep, run_in_background=true)`, run Deep preflight when **either** effort **or** model profile is missing from the prompt. **STOP** until answered — do not dispatch deep.

**Skip** Deep preflight only when **both** are explicit, e.g. `"comprehensive report, high effort, default models"` or `"deep research X, medium, grok-only"`.

### Operator note — tool availability (Cursor product)

Parent-tool inventory is a Cursor product feature on the *orchestrator* turn — not something the research skill can conjure.

1. Prefer **`AskQuestion`** (modal) when the tool exists in this turn's tool list.
2. If `AskQuestion` is **missing** / errors ("Tool not found", "couldn't pop up") → ask the **same two questions in chat prose** (numbered options) and **STOP**. Do not proceed with invented defaults. Prose picker is success, not degradation theater.
3. Known product gap (**verified** in memory): some parents (notably **Grok 4.5 / 4.6**, sometimes others) do not expose `AskQuestion`. Do not claim “popup failed due to research skill” when the tool is absent.

### Question 1 — Effort

| Option | `--preset` |
|--------|------------|
| Low | `quick` |
| **Medium (Recommended)** | `balanced` |
| High | `aggressive` |

### Question 2 — Model roster

Web evidence is always Tavily + Firecrawl + YouTube (not selectable). LLM roles only:

| Profile ID | Label | Researchers | Synthesizer | Adjudicator |
|------------|-------|-------------|-------------|-------------|
| `default` | **Default (Recommended)** — mixed pool, Cursor-billed Grok synth | cursor-grok-4.6-high, deepseek-v4-pro, MiniMax-M3 | cursor-grok-4.6-high | cursor-grok-4.6-high |
| `grok-only` | All Grok via Cursor subscription (not xAI API) | cursor-grok-4.6-high ×3 | cursor-grok-4.6-high | cursor-grok-4.6-high |
| `mixed-economy` | Cursor Grok + DeepSeek — no MiniMax | cursor-grok-4.6-high, deepseek-v4-pro | cursor-grok-4.6-high | cursor-grok-4.6-high |

Grok roles bill through **Cursor** (`cursor-agent` + plan pool). Metered `api.x.ai` only if `provider: xai` or `DEEP_RESEARCH_GROK_VIA=xai`.

Profiles: `scripts/deep_research/profiles/{id}.yaml`. Pass `--profile <id>` to `run.py`.

### Optional local persistence (v1)

Gitignored `scripts/deep_research/deep_research_config.local.yaml` merges over profile + `config.yaml` when present. Use to persist last AskQuestion choices locally; pre-select as Recommended on next AskQuestion (orchestrator may read file if it exists — do not commit). Persistence does **not** skip asking unless Matt opted into defaults.
## Format knobs (deep)

Pass through to `run.py`: `--format`, `--citation-style`, `--sections`, `--visual-level`, `--search-topic`, `--context`.

Supported formats: `briefing_memo`, `literature_review`, `pros_cons`, `how_to`, `scholarly`.

## Deep query hygiene (HARD)

- **`--query`:** research question (may be longer).
- **`--search-topic`:** short keywords / model names for Tavily/relevance (≪350 chars). If omitted, `run.py` extracts from `--query` (first paragraph / sentence, ≤200).
- **`--context`:** synth/adjudicate-only (already-tried, stack, VRAM, licenses). **Never** prefix into Tavily search strings.
- **One** `run.py` per deep dispatch — no salvage re-runs that rewrite truncated slugs unless orchestrator re-dispatches after a harness fix.
- **Fail-closed:** empty sources or provider error → `ok: false` + `## Blocked` stub. Agent handoff **Status: BLOCKED**. Orchestrator must **not** relay the stub as a literature review.
- Always `required_permissions: ["full_network"]` on the first provider/LLM Shell (no sandbox retry cosplay).

## Gather guardrails (docs-only; acceptance = SSOT + post-sync spot-check; no hook)

- When the brief is “what’s new for stack X,” search **competing families / adjacent releases**, not only the named stack.
- Before “try next” ROI pitches: check Memory topics / prompt exclusions for **already tried**; exclude those.
- Prefer listed primary URLs (Firecrawl scrape) over broad search when provided; keep any Tavily query ≤~100–200 chars.
- Cap remains 5 paid calls; free HF raw / GitHub HTTP OK for licenses when paid budget is spent.

## Dispatch

```
Task(subagent_type: "research", readonly: true, run_in_background: <true only for deep>)
```

Prompt must include: `mode`, `query`, and for deep: `preset`, `profile` (post-AskQuestion). Prefer also `search-topic` + `context` for deep production briefs.

**Gather:** foreground, max 5 paid provider calls; agent returns sources + short synthesis + conflicts; **orchestrator relays** Lane A to Matt.

**Deep:** background; relay `outputs/research/<slug>.md` excerpt Lane B **only when `ok: true`**. On BLOCKED stub, report blocker — do not pitch as a literature review.

## YouTube cues

- YouTube URL in query → metadata via `videos.list` (1 quota unit), skip search
- Video/tutorial intent → `search_videos` once per gather (100 units); transcripts for top 5 (free)
- Deep pipeline always calls YouTube via `web_context.py` (supplemental; soft when Tavily salvage exists)

## X / Twitter (deferred)

`x_call.py` returns `{"error": "x_not_configured"}` with no network. Note in gather evidence if queried; do not retry or block on X.

## Related skills

| Skill | When |
|-------|------|
| `web-search` | Single lookup, no multi-source research |
| `web-fetch` | One URL extract/scrape |

## Oracle

```bash
bash scripts/smoke_web_providers.sh
bash scripts/smoke_deep_research.sh
bash .cursor/scripts/smoke_harness_agents.sh
bash .cursor/scripts/smoke_harness_skills.sh
```
