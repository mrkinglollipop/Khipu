# Lens: polish

## Question

What small UX/consistency/copy improvements raise quality without redefining the product?

## In

- Empty states, spacing, labels, consistency with existing patterns
- Copy clarity, error messages, loading states
- Design dimensions from `ui-design-quality` / `swiftui-pro` when class matches
- Cross-surface consistency (nav, type, color, spacing language)

## Out

- New features, architecture changes, AI-slop rewrites of working code
- Stated missing requirements (`gaps`)
- Product roadmap (`product`)

## Coverage floor (HARD)

See `references/finder-common.md` — polish floor: design checklist pass/fail/N/A per
applicable surface; ≥1 cross-surface consistency check; route skills below.

## Tool routing

| Class | Tools / skills |
|-------|----------------|
| iOS/Mac | a11y describe + snapshot; load `swiftui-pro` when SwiftUI; xc-mcp |
| Electron | `electron-oracle` / CDP + a11y/snapshot |
| Web | browser MCP snapshot; `ui-design-quality` for mockup/uplift surfaces |
| Aegis / Loom / harness | smokes, `harness_doctor.py`, doc/consistency read (UI skills N/A) |
| Story / art plates | `art-vision` when applicable |
| Generic | read UI-facing files (SwiftUI, TSX, templates) |

Record **Tools used** / **Tools missing**. Missing skill/MCP → degrade + tell Matt.

## Steps

1. Enumerate user-facing surfaces on the primary path.
2. Run design checklist (hierarchy, spacing, type, color, empty/error/loading).
3. Prefer existing design patterns in-repo; invoke `ui-design-quality` /
   `swiftui-pro` / `electron-oracle` / `art-vision` when class matches.
4. Cap 8 findings; parent runs cheap `working` re-smoke after polish fixes when
   `working` was requested or primary path touched.

## Hard stop

No product redefine. No silent always-on harness hook/rule rewrites. Polish fixes stay
existing-surface only.
