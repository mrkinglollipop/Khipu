# Lens: gaps

## Question

Which stated requirements (docs/plan/thread) are missing or only partially shipped?

## In

- README / CLAUDE.md / plan / Deliverable Contract / thread intent
- P0 primary-workflow must-ships
- P1 explicitly listed secondary requirements

## Out

- Unnamed wishlist / invented scope (`product` if friction-only; else blocker)
- Pure polish without a stated requirement
- Auto-implementing new surface without Matt approval
- Writes or inventory expansion outside `$SCOPE_ROOT`

## Coverage floor (HARD)

See `references/finder-common.md` — gaps floor: list every discrete requirement from
named intent sources; **100% of P0+P1** checked or N/A with cite. If intent has no
priority labels, treat primary-workflow requirements as P0 and other cited bullets as P1.

**Scope reconciliation (HARD):** Intent sources outside `$SCOPE_ROOT` may be read as
**read-only evidence/cites** only — they are **not** inventory surfaces and **not**
fix targets. Enumerate/check requirements against surfaces **inside** `$SCOPE_ROOT`.
Never write outside scope.

## Tool routing

Read intent sources first (in-scope preferred; outside-scope cite OK as read-only);
exercise missing surface with class tools when claiming a gap — exercise stays under
`$SCOPE_ROOT`. Record **Tools used** / **Tools missing**.

## Steps

1. Cite intent sources (note which cites are outside `$SCOPE_ROOT`).
2. Enumerate P0/P1 requirements.
3. Mark each present / partial / missing / N/A with evidence **inside** `$SCOPE_ROOT`.
4. Cap 8 findings.

## Hard stop

Unnamed intent → blocker finding, not a feature list. Do not invent requirements.
No writes outside `$SCOPE_ROOT`.
