# How — critique rubric

Score only architectural issues grounded in the explanation + code paths provided.

| Lens | Look for |
|------|----------|
| Boundaries | Leaky layers, wrong ownership, circular deps |
| State | Shared mutable state without a clear owner |
| Coupling | One-caller wrappers, god modules, shotgun changes |
| Failure modes | Missing idempotency, unclear error boundaries |
| Extensibility | Illegal states representable; hard-coded variants |
| Simplicity | Accidental complexity vs problem complexity |

Do **not** niggle style, Deadpool voice, or unrelated refactors. Prefer fewer high-signal findings.
