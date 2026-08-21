# Lens: bugs

## Question

What correctness, edge, silent-failure, a11y-functional, security, or performance
defects exist on inventoried surfaces?

## In

- Wrong results, crashes, race/state bugs
- Edge inputs, empty states that fail functionally
- Silent failures (swallowed errors, false success)
- Functional a11y blockers (unreachable controls, missing labels that break use)
- Security defects on inventoried surfaces (auth gaps, injection, secret exposure,
  unsafe defaults that break the security floor)
- Performance defects that break primary workflow or make it unusable (hangs,
  unbounded work, pathological latency on hot paths)

## Out

- Spec gaps vs docs without a defect (`gaps`)
- Pure polish / copy (`polish`)
- Roadmap features (`product`)

## Coverage floor (HARD)

See `references/finder-common.md` — bugs floor: ≥1 checked category per inventoried
surface (correctness | edge | silent-failure | a11y-functional | security |
performance); at least one automated oracle when the repo provides one.

## Tool routing

Class verify lane + cheapest automated check (test/build/lint). a11y describe for
functional a11y when the class has a UI surface — **harness/loom/aegis:** smokes +
doctor only (a11y N/A → Tools missing). Record **Tools used** / **Tools missing**.

## Steps

1. Inventory hot paths **under `$SCOPE_ROOT`** (primary workflow + recent session
   touchpoints that fall inside scope). Do not inventory outside-scope paths.
2. For each surface, check ≥1 category with evidence.
3. Cap 8 findings; prioritize HIGH/MEDIUM for fixer.

## Hard stop

No speculative refactors. No inventing defects without evidence.
