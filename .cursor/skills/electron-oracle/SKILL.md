---
name: electron-oracle
description: >-
  Dispatch electron-oracle agent for Electron UI/shell verify — Phase 1 npm run smoke
  build gate, optional Phase 2 CDP screenshot + DOM marker. Not art-vision, not ship,
  not ios-oracle. Hard rule: smoke alone = VISUAL SKIP, never VISUAL PASS.
---

# Electron Oracle

Orchestrator-owned **dispatch** for Electron build + optional visual verify. The agent runs smoke/CDP oracles and returns a Flavor-OFF BUILD/VISUAL report. Route via this skill + agent `description` — not art-vision or ios-oracle.

## When to dispatch

| Matt / context | Action |
|----------------|--------|
| Electron UI, renderer, preload, or main process change | `Task(electron-oracle, readonly: true)` |
| pi-harness-test shell / chat UI verify | same + repo pi-harness-test |
| Claim **BUILD PASS** or **VISUAL PASS** for Electron | **electron-oracle mandatory** |
| iOS / SwiftUI / sim screenshot | **ios-oracle** — not electron-oracle |
| Story plate / painterly QA | **art-vision** — not electron-oracle |
| Notarize / dmg / TestFlight | ship runbook — not electron-oracle |

## App registry (v1)

| App | Repo | Launch | Smoke (Phase 1) | Debug port | Root marker |
|-----|------|--------|-----------------|------------|-------------|
| pi-harness-test | `/Volumes/Cloud Storage/Code/pi-harness-test` | `npm start` / `Launch Pi Harness Test.command` | `npm run smoke` | `9222` via `ELECTRON_ORACLE_DEBUG_PORT` + `scripts/start.js` | `[data-oracle-root]` |
| FT Terminal Electron | later | later | later | later | later |
| Loom ADE | later | later | later | later | later |

**Generic fallback:** dispatch prompt must supply `repo`, `launch_cmd`, `smoke_cmd`, `debug_port`, `root_selector` when the app is not in the table.

## Launch contract

Before any Electron launch:

```bash
unset ELECTRON_RUN_AS_NODE
cd "$REPO"
```

**pi-harness-test (Phase 2):**

```bash
ELECTRON_ORACLE_DEBUG_PORT=9222 npm start
```

`scripts/start.js` appends `--remote-debugging-port=<port>` when the env var is set. CDP URL: `http://127.0.0.1:9222`.

## Smoke contract (Phase 1 only)

```bash
cd "$REPO"
unset ELECTRON_RUN_AS_NODE
npm run smoke
```

- **pi-harness-test:** `node scripts/smoke.js` via `npm run smoke` — headless; Phase 1 build gate only.
- Exit **0** → **BUILD PASS** evidence. Non-zero → **BUILD FAIL**.

## CDP / visual contract (Phase 2)

Load **control-ui** for CDP attach patterns (cursor-team-kit plugin — Generic CDP Harness section).

1. Launch app with debug port (see Launch contract).
2. `chromium.connectOverCDP("http://127.0.0.1:<port>")` — select page by `root_selector`.
3. Confirm `[data-oracle-root]` (or app-specific marker) exists.
4. Screenshot: `/tmp/electron-oracle-<timestamp>.png` (absolute path in report).

### Hard rule

**`npm run smoke` alone ⇒ VISUAL SKIP — never VISUAL PASS.** Headless smoke does not prove renderer paint, layout, or window chrome. Phase 2 (CDP + screenshot + marker) is required for VISUAL PASS.

## D-hybrid fallback

1. Agent: `readonly: true` — no source writes.
2. Agent shell: `required_permissions: ["all"]` for launch, CDP, screenshots.
3. **If Task readonly blocks shell/CDP:** orchestrator runs Phase 1/2 inline using control-ui / Playwright CDP, then (i) pastes evidence into a follow-up synthesizer dispatch, or (ii) reports evidence itself. Documented in `app-development.mdc` (same pattern as ios-oracle enum-unavailable).
4. **Must not** claim VISUAL PASS without a runnable Phase 2 path or orchestrator-inline evidence.

## Dispatch

```
Task(subagent_type: "electron-oracle", readonly: true)
```

When the product Task enum lacks `electron-oracle`, use orchestrator-inline Phase 1/2 per `orchestration.mdc` or `generalPurpose` + this skill — do not skip verify.

Prompt must include: absolute **repo path**, whether **visual** is requested, and registry overrides if not pi-harness-test.

Omit Task `model` — agent frontmatter owns `inherit`.

## Orchestrator synthesis

Lane A: relay agent Flavor-OFF report (Phase 1 + Phase 2 + handoff tail). Orchestrator owns authoritative gate.

If agent reports **BLOCKED** on permissions, run inline smoke/CDP and attach evidence before claiming pass.

## Smoke (harness)

```bash
bash .cursor/scripts/smoke_harness_agents.sh
bash .cursor/scripts/smoke_harness_skills.sh
```

Do not substitute harness smokes for app Phase 1/2 — they only verify plugin files exist.
