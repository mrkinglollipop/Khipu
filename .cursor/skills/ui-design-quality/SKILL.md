---
name: ui-design-quality
description: >-
  Product UI mockup and design uplift playbook — constrain→generate→review for
  SwiftUI, web, Electron, and Tauri webviews. Triggers: mockup, uplift, AI-generic
  UI, design quality, settings screen mock, Figma assemble, Tauri webview UI,
  Mission Control chrome, pi-harness panel. Not art-vision, not Rust/src-tauri.
  Prefer Code Connect / search_design_system over freehand. Never electron-oracle
  for Tauri.
---

# UI design quality

Orchestrator-owned **skill playbook** (not a hook). Load when mockup/uplift intent matches this description, or Matt invokes `/ui-design-quality`.

## Loop

```
ground → generate (Figma/DS) → review → (optional) ship verify
```

Max **one** regenerate after review for uplift. Then stop.

## Lane routing

| Lane | Ground | Themes (better-design) | Review | Ship verify |
|------|--------|------------------------|--------|-------------|
| **SwiftUI** | `extract-design-system` + Figma library / Code Connect | **Never** | `get-review-rules` if better-design MCP live; else **offline degrade** checklist below | `Task(ios-oracle)` after `figma-to-swiftui` as needed |
| **Electron** | extract-DS; themes OK if shadcn-compatible | OK when kit in play | same review branch | **`electron-oracle` skill** and/or `Task(electron-oracle)` |
| **Web** | same as Electron | OK when kit in play | same | repo/web smoke as applicable |
| **Tauri webview** | same as web (React/HTML in webview — **not** Rust) | OK only if kit-compatible; hand-rolled (e.g. Grok Mission Control) → tokens + review **without** forcing themes | same | **Never `electron-oracle`.** Labeled manual/build verify (repo `npm`/`tauri` smoke, screenshot, Matt check) until a separate tauri-oracle exists |

### Figma MCP (do not conflate)

| Role | Stack | Use |
|------|-------|-----|
| Generate / write / DS | Official Cursor Figma plugin | `figma-generate-design`, `use_figma`, `search_design_system`, Code Connect |
| Layout / export → implement | Framelink (`user-figma` / figma-developer-mcp) | Fidelity into `figma-to-swiftui` — not the generate lane |

## Code Connect lean-in

Prefer `search_design_system` + Code Connect over freehand when generating or implementing Figma screens.

If the target app has **no** library/Code Connect mappings: **label freehand degrade** in-thread and continue, or stop if Matt rejected degrade. Do not silently freehand.

## Review branch

**Live better-design MCP is blocked on Matt** until he (1) sets `Authorization: Bearer …` in `~/.cursor/mcp.json` from `API Keys/Better Design API Key.txt` (template: `.cursor/mcp.json.example`; get key at https://better-design.com/settings), and (2) **restarts Cursor**. Do **not** invent or paste API keys. Until live, always use **offline degrade**.

`sync-harness.sh --local` copies the MCP template to `~/.cursor/mcp.json` **only when that file is missing**; it will **not** overwrite an existing config — after template changes, manually merge `better-design` / `shadcn-ui` entries.

1. **better-design MCP live** → call `get-review-rules` (accessibility + visual-design). Fix punch list; max one regenerate.
2. **MCP gated/off / not configured** → use **Offline degrade checklist** below and **label in-thread: `offline degrade`**.

### Offline degrade checklist (fold of anti-AI-aesthetic / polish ideas)

Label the review **`offline degrade`** when using this list instead of live `get-review-rules`.

- [ ] No default purple-on-white / purple-indigo gradient SaaS look unless brand demands it
- [ ] Clear hierarchy: one primary action per region; type scale consistent
- [ ] Spacing on a consistent scale; no random 13px gaps
- [ ] Contrast: body text and controls readable on backgrounds (aim WCAG AA)
- [ ] Focus/hover states present for interactive controls
- [ ] No decorative glow soup, emoji clusters, or multi-layer shadow stacks without brand reason
- [ ] Brand/product name not overpowered by a generic headline (when branded)
- [ ] Cards only when they contain interaction; hero not a card collage
- [ ] Tauri/Electron/web: component kit used if project has one; no inventing parallel buttons
- [ ] SwiftUI: Apple-idiom controls; no pretending shadcn CSS is the ground truth

## Out of scope

- Painterly / Sojourn / Circletown plates → **art-vision** / art skills
- Rust / `src-tauri` / Tokio / PTY / packaging → not this skill
- Auto-running on every agent turn without skill load → **not claimed**

## Dispatch reminders

```
Task(subagent_type: "ios-oracle", readonly: true)          # SwiftUI ship
# Electron: load electron-oracle skill and/or:
Task(subagent_type: "electron-oracle", readonly: true)
# Tauri: do NOT dispatch electron-oracle
```

Ground skill: `/Volumes/Cloud Storage/Claude/.cursor/skills/extract-design-system/SKILL.md` (after sync) or `.cursor-plugin/skills/extract-design-system/SKILL.md`.
