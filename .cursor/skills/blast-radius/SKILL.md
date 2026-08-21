---
name: blast-radius
description: >-
  Use for small-looking refactors, renames, API tweaks, or "what else could this
  break?" Pre-merge safety on a named change: path-census callers, name the
  invariant, Assess (default) or Prove with a real local oracle. Not a session
  audit — use /myauditandfix for that.
---

# Blast radius

Orchestrator-owned skill. Answer what else a named change could break, and prove (when authorized) the load-bearing safety fact with a real oracle — not “it compiles” alone.

## Modes

| Mode | When | What runs |
|------|------|-----------|
| **Assess** (default) | “what else could break?” / change not yet authorized to apply | Name change + intended invariant; path-census when reporting counts; residual risks. Read-only oracles that do not require applying the change. **No writes.** |
| **Prove** | Enter only when **(a)** Matt explicitly authorizes Prove/apply, or **(b)** Matt asks to verify an **already-applied** local diff he owns | Real local oracle (pytest / build / targeted script) against the changed artifact. Cap ≤3 runs of the same oracle per change. |

**Prove vs mutation:** Entering Prove authorizes **oracle runs only**, not code edits. Applying a fix still needs Matt asking to fix/apply. An uncommitted agent-authored diff does **not** auto-enter Prove.

## Flow

1. Name the change and intended invariant.
2. **Path-census (conduct 448):** whenever reporting path-audit **counts**, run five greps — literal path plus `.parent`, `parents[`, `$(dirname`, and `$VAR`-composed. Not only when paths move.
3. Optional caller discovery: Task `explore` or `generalPurpose`, `readonly: true`, model `cursor-grok-4.6-medium` — not the sole path.
4. Identify the single load-bearing safety fact.
5. **Assess:** report without applying. **Prove:** run the oracle (cap 3).
6. Report.

## Report

**Blast surface.** **Invariant** (named in Assess; proven in Prove). **Residual risks.**

## Not this

- Session/plan audit → `/myauditandfix` / `/verify-plan`
- Open-web research
- Auto-fixing the change under Assess
