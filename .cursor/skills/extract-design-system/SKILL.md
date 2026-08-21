---
name: extract-design-system
description: >-
  Extract design tokens (colors, type, spacing, radius, shadows) from a public
  website into design-system/tokens.json and design-system/tokens.css for Cursor
  agents. Use when grounding UI mockups on a real brand (Linear, Stripe, etc.)
  before generate. Node + Playwright required. Not for SwiftUI shadcn install;
  tokens are design constraints.
---

# Extract design system

Orchestrator-owned **token grounding** before UI generate. Wraps the upstream CLI from [arvindrk/extract-design-system](https://github.com/arvindrk/extract-design-system). Output feeds `ui-design-quality` / mockup turns.

## Prerequisites

- **Node 20+** on PATH
- **Playwright Chromium** for the CLI (`npx playwright install chromium` if first run fails)
- Public URL only — do not point at auth-walled or private apps

## When to use

| Matt / context | Action |
|----------------|--------|
| "Make it feel like Linear / Stripe / \<site\>" | Run extract → put tokens in session |
| SwiftUI mockup ground step | Tokens as **design constraints** (colors, type, spacing) — **not** a shadcn install |
| Web / Electron / Tauri webview | Same tokens; may also feed shadcn theme work when kit is in play |
| Artwork / painterly plates | **Not this skill** — art-vision / art gen |

## CLI (agent runs)

**`cd` to the desired output project or scratch directory first.** The CLI writes **cwd-relative** files under `design-system/` (and optional `.extract-design-system/`). There is **no `--out` flag** — confirmed via `npx -y extract-design-system --help`.

```bash
cd "/path/to/target-project"   # or a Matt-named scratch dir
npx -y extract-design-system "https://example.com"
```

Optional flags from `--help`: `--dark-mode`, `--mobile`, `--slow`, `--browser <browser>`, `--extract-only`. Commands: `init`, `audit [dir]`.

If the package exposes only the skills.sh path, fall back:

```bash
npx -y skills add arvindrk/extract-design-system
# then invoke per upstream skill; or:
npx -y extract-design-system "https://example.com"
```

Expected artifacts (cwd-relative; names may vary by CLI version):

- `design-system/tokens.json` (W3C-ish)
- `design-system/tokens.css` (CSS custom properties)
- optional `.extract-design-system/raw.json` / `normalized.json`

## Session contract

1. Run extract against the reference URL Matt named (default brand refs: Linear + one product-specific when Matt set them).
2. Read `design-system/tokens.json` and `design-system/tokens.css` into context (summarize palette, type, spacing — do not dump megabytes).
3. Hand off to **ui-design-quality** generate step (Figma DS / Code Connect). Do **not** install better-design **themes** for SwiftUI.
4. Label failures (Playwright missing, blocked site) and stop — do not invent tokens.

## Hard rules

- Never send `API Keys/` contents off-sub.
- Never claim extract ran without shell evidence.
- SwiftUI: constraints only. Webview lanes may use tokens with shadcn when kit-compatible.
