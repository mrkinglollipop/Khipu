# Lens: product

## Question

What product improvements would materially help the **stated** primary workflow without
redefining scope or shipping net-new surface in this mission?

## In

- UX friction on the primary path
- Missing affordances that docs/thread imply are in-scope
- Workflow shortcuts, clarity, discoverability
- Competitive/obvious gaps vs stated intent (not invented roadmap)

## Out

- Auto-fix or implementation (parent logs only)
- Architecture rewrites, new subsystems, greenfield features
- Blocking Green — **product lens never blocks Green**

## Coverage floor (HARD)

See `references/finder-common.md` — product floor: friction map for primary journey;
proposals scored **or** explicit "none found" with evidence.

## Tool routing

| Class | Tools |
|-------|-------|
| iOS/Mac | xc-mcp a11y describe + primary flow tap-through |
| Electron/Web | browser MCP snapshot + primary path |
| Generic | README/CLAUDE.md + static UX read of entry points |

Record **Tools used** / **Tools missing** per finder-common contract.

## Steps

1. Read `references/finder-common.md` product floor.
2. Scan primary workflow with class tools (a11y/browser/build as appropriate).
3. Emit ≤8 **proposals** as findings; severity typically LOW or MEDIUM.
4. Every product finding: Status=`logged` in find-only; in fix mode parent sets
   `logged` or `deferred` — **never** counts as `open` for Green.

## Hard stop

- **Never auto-fix** product proposals in the fixer phase.
- **Never block Green** — proposals are logged for Matt; mission Green ignores open
  product `logged` items.
- No inventing product Matt did not imply. Unnamed intent → blocker finding, not
  feature list.
