# Lens: working

## Question

Does the primary workflow actually work end-to-end for the stated product?

## In

- Launch/build/run of the target
- Primary journey (entry → success → exit)
- One empty/error/persistence step when applicable
- Class verify lane (xc-mcp, electron-oracle, browser, smokes)

## Out

- Feature invention (`product`)
- Spec gaps vs docs (`gaps`) unless they break the primary path
- Pure visual polish without functional impact (`polish`)

## Coverage floor (HARD)

See `references/finder-common.md` — working floor:

1. Decompose primary workflow into **3–7** ordered journey steps.
2. Mark each with `journey_step: true` in coverage.inventory.
3. Drive ≥80% of steps **or** BLOCKED + tool-missing per undriven step.
4. Launch/build evidence required — no diff-only "working".

## Tool routing

Use class row from finder-common tool matrix. Prefer a11y-first over screenshots.
Record **Tools used** / **Tools missing**.

## Steps

1. Name primary workflow in one sentence.
2. Build journey checklist (3–7 steps).
3. Exercise with class tools; record pass/fail/BLOCKED per step.
4. Emit findings for broken steps (HIGH) and below-floor coverage (MEDIUM+).

## Hard stop

Do not redefine the product. Missing launch tools → BLOCKED finding, not fake Green.
