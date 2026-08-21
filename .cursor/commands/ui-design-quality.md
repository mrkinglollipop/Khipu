---
name: ui-design-quality
description: Product UI mockup / design uplift playbook (constrain→generate→review). Authoritative contract in ui-design-quality skill.
---

# UI design quality

**Authoritative contract:** `.cursor/skills/ui-design-quality/SKILL.md` only (synced from `.cursor-plugin/skills/ui-design-quality/`).

## Where this command lives

Cursor loads slash commands from:

1. **`~/.cursor/commands/`** (global — any opened folder)
2. **`<workspace-root>/.cursor/commands/`** (project root only)

After harness updates:

```bash
bash "/Volumes/Cloud Storage/Claude/.cursor/scripts/sync-harness.sh" --local --repos
```

## Scope

- Mockups, design uplift, anti–AI-generic UI for SwiftUI, web, Electron, and **Tauri webviews**.
- Ground with `extract-design-system` when a reference brand/site is named.
- Review via better-design `get-review-rules` when MCP live; else labeled **offline degrade** checklist in the skill.
- **Not:** painterly art (`art-vision`); Rust/`src-tauri`; Electron verify for Tauri apps.

## Bootstrap

1. Read `.cursor/skills/ui-design-quality/SKILL.md`.
2. Run constrain → generate → review (max one regenerate after review).
3. Ship verify by lane: `ios-oracle` / `electron-oracle` / Tauri manual — never electron-oracle for Tauri.
