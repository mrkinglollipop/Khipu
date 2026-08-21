---
name: figma-to-swiftui
description: |
  Convert a Figma frame into native, production-quality SwiftUI that matches an app's existing design tokens and modern iOS APIs. Use this skill whenever the user provides a Figma link, frame, or node and wants it built in SwiftUI — phrasings like 'build this Figma design', 'figma to swiftui', 'turn this mockup into a screen', 'implement this Figma frame', or pastes a figma.com/design/... URL with implementation intent. The skill pulls exact layout metadata (positions, sizes, spacing, colors, typography, auto-layout) from the Figma API via the Framelink MCP, maps it onto the project's design system rather than hardcoding values, generates idiomatic SwiftUI (not transpiled web patterns), and verifies visual fidelity against the source frame. Do NOT use for web/React targets or for freehand 'make it look nice' requests with no Figma source.
allowed-tools: mcp__figma__get_figma_data, mcp__figma__download_figma_images, Bash, Read, Edit, Write, Glob, Grep
---
# figma-to-swiftui
Turn a Figma frame into native SwiftUI that fits the target app, using real design data instead of guesses. The point is to replace the lossy screenshot-eyeball-prose feedback loop with exact metadata and a visual fidelity check.

## Prerequisites (verify once per session)
- The `figma` MCP server must be connected. If `mcp__figma__*` tools are not available, the server was added but the session predates it — tell the user to restart the session, or fall back to the Figma REST API directly using the same `figd_` personal access token configured for the `figma` MCP server: `curl -H "X-Figma-Token: $KEY" "https://api.figma.com/v1/files/{fileKey}/nodes?ids={nodeId}"`.
- The token is the `figd_` personal access token already configured on the `figma` MCP server (File-content read scope); read it from wherever your project keeps credentials rather than hardcoding it.

## Step 1 — Parse the Figma URL
A Figma URL looks like `https://www.figma.com/design/{fileKey}/{name}?node-id={node-id}`. Extract fileKey (the segment after `/design/` or `/file/`) and nodeId (the `node-id` query param). Figma URLs use a dash (`123-456`); the API expects a colon (`123:456`) — convert it. If the user gives a file but no specific node, ask which frame; do not dump the whole file tree.

## Step 2 — Pull the layout metadata
Call `mcp__figma__get_figma_data` with the fileKey and nodeId. It returns the node tree with absolute positions, sizes, auto-layout direction/spacing/padding, fills, strokes, corner radii, text content, font family/size/weight/line-height, and constraints. This metadata is the source of truth. Do not invent any value not present; if a needed value is genuinely absent, say so explicitly rather than guessing.

## Step 3 — Anchor to the project's design system FIRST
Before writing any SwiftUI, find and read the target repo's design tokens and conventions so output uses them instead of raw literals. Look for a theme/token file (e.g. `FTTheme.swift`, `Theme.swift`, a `DesignSystem/` or `Tokens/` group, a Color asset catalog). Map Figma colors to the nearest semantic token (`theme.accent`, `theme.bg2`); only fall back to a literal `Color(hex:)` when no token matches, and flag that gap. Map typography to the project's text styles or native Dynamic Type styles (`.title2`, `.body`) rather than fixed point sizes unless deliberately fixed. Reuse existing components instead of re-deriving from pixels. This is what makes output feel native, not transplanted.

## Step 4 — Generate idiomatic SwiftUI
Translate structure, do not transcribe geometry. Auto-layout to stacks: horizontal to `HStack`, vertical to `VStack`, with `spacing:` from Figma item spacing and `.padding()` from frame padding; use `Spacer()` for space-between. Avoid absolute frames: do not reproduce with hardcoded `.position()`/`.offset()` and fixed `.frame(width:height:)` everywhere — that is the web absolute-positioning anti-pattern and does not adapt; use intrinsic sizing, `.frame(maxWidth: .infinity)`, and layout priorities. Native idioms: SF Symbols for icons (match weight to surrounding text), `.background(.regularMaterial)` / `.glassEffect()` (iOS 26) for translucent surfaces, native `Button`/`List`/`NavigationStack`. iOS 26 / Liquid Glass: when the design implies translucent chrome, use `GlassEffectContainer` and `.glassEffect()` rather than approximating with blurs, when the deployment target allows. Extract repeated elements into small reusable subviews; do not emit one giant `body`.

## Step 5 — Verify visual fidelity (do not skip)
1. Export a PNG of the Figma frame via `mcp__figma__download_figma_images` (or the REST `images` endpoint) as the reference. 2. Render the SwiftUI: use Inject hot-reload if the repo has it, else build and run in the simulator. 3. Screenshot the running view (xc-mcp `screenshot`, or a `#Preview` snapshot). 4. Compare reference vs render — spacing, alignment, type scale, color, corner radii. Fix mismatches and repeat. Cap at ~3 iterations; if still off, report the remaining diffs rather than looping.

## Refusals and honesty
- Never fabricate a measurement, color, or font not in the Figma data.
- If the frame uses a font, color, or component the project has no token/asset for, surface it as a list of 'design gaps to resolve' instead of silently hardcoding.
- Report fidelity honestly: state what matched and what did not, with the actual remaining differences.