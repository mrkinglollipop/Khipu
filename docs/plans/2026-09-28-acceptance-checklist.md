# Memory-reasoning acceptance checklist — every harness, every added capability

Revision under test: `<branch>`, HEAD `<sha>` (fill in at verification time — this
document ships with the capability rows and matrix pre-built and every cell
**PENDING**; it carries no result yet). Gateway build under test:
`<KHIPU_BUILD / healthz build stamp>`. Desktop app under test: `<version>`.

This checklist is never edited in place once a verification pass records a result in
it: a pass appends a dated addendum section at the end (same convention as
docs/plans/2026-09-14-acceptance.md's "Cursor live row" / "Aegis per-turn recall"
addenda) with its own harness-matrix rows, evidence, and pytest-baseline line. The
matrix and sections above stay as the PENDING template so a stale addendum can never
be mistaken for a fresh one.

## Method

For each harness: one scripted or live session that (1) asks a topical question naming
a stored decision or topic, (2) asks an explicit-reversal question ("didn't we decide
X, then change it?"), (3) asks a historical/as-of question, (4) triggers a capture with
a deferred commitment, (5) triggers `khipu_decisions`/`khipu decisions supersede` where
that harness can reach it, and (6) forces the local replica stale and confirms
reconnection. Evidence for each row is the tool/hook output itself (a log line, a
payload, a rendered block) — never a claim without a pasted artifact, same discipline
as docs/plans/2026-09-14-acceptance.md. A harness with no path to a capability is
recorded **N/A** with the exact reason, not left blank.

Per docs/plans/2026-09-27-memory-reasoning-scope.md, "Cursor and cloud clients cannot
be declared equivalent to automatic prompt hooks" — every PASS below needs a real
session that actually calls the relevant MCP tool; configuration presence, tool
discovery, and `/healthz` are necessary but not sufficient.

## 1. Harness matrix

Legend: **PASS** — proven live, evidence in this pass's addendum. **FAIL** — proven
live and broken, with the fix noted. **N/A** — not exercised, exact reason given.
**PENDING** — not yet run (every cell below, until the first addendum).

| Capability | Claude Code | Codex | Cursor | Aegis | Gateway |
|---|---|---|---|---|---|
| Hermetic suite (pytest, no live hub/API by default) | PENDING | PENDING | PENDING | PENDING | PENDING |
| Launchers untouched by a read-only `status`/`verify`/`doctor` call | PENDING | PENDING | PENDING | PENDING | PENDING |
| Per-prompt recall lane answers inside its budget | PENDING | PENDING | PENDING | PENDING | PENDING |
| Capability signal (`contract.version`/`capabilities` on `initialize` and status) | PENDING | PENDING | PENDING | PENDING | PENDING |
| Forgetting is complete (no reader returns a forgotten episode; cascades to decisions/deliverables) | PENDING | PENDING | PENDING | PENDING | PENDING |
| Decisions tools (`khipu_decisions` read, `khipu_decisions_update` supersede/restore/retract) | PENDING | PENDING | PENDING | PENDING | PENDING |
| Validity honored in search results | PENDING | PENDING | PENDING | PENDING | PENDING |
| Validity honored in the rendered prompt block | PENDING | PENDING | PENDING | PENDING | PENDING |
| Validity honored in `khipu_status` | PENDING | PENDING | PENDING | PENDING | PENDING |
| Validity honored in the session-start slice | PENDING | PENDING | PENDING | PENDING | PENDING |
| Replica carries a schema version; an old replica degrades to validity-unknown | PENDING | PENDING | PENDING | PENDING | PENDING |
| Bounded graph candidates contribute to recall (switch-gated) | PENDING | PENDING | PENDING | PENDING | PENDING |
| Time interpretation on a natural-language query (switch-gated) | PENDING | PENDING | PENDING | PENDING | PENDING |
| Aegis remains an unchanged consumer of the unmodified fields it already reads | N/A (not this harness) | N/A (not this harness) | N/A (not this harness) | PENDING | N/A (not this harness) |
| Aegis adapter packet (`prior_work_meta.outcome`, capability signal, validity/revision in its dedup filter) | N/A (not this harness) | N/A (not this harness) | N/A (not this harness) | PENDING | N/A (not this harness) |

## 2. Evidence

### Claude Code

_(empty — first verification pass appends its evidence here, in a dated addendum, not
in this section.)_

### Codex

_(empty)_

### Cursor

_(empty)_

### Aegis

_(empty)_

### Gateway

_(empty)_

## 3. Pytest baseline

Oracle command (from `packages/cli`):

```
S=$(mktemp -d) && PYTHONPYCACHEPREFIX="$S/pyc" PYTHONPATH="$PWD:$KHIPU_ROOT/.python_libs" python3.11 -m pytest -q -p no:cacheprovider
```

(`$KHIPU_ROOT` — the repo checkout root; see `packages/cli/README.md`'s own `PYTHONPATH` line for the same convention.)

Baseline as of this document's own phase (Phase 1, session B): **10 named failures**,
unchanged before and after this session's additions —

- `tests/test_embed_media.py::EmbedMediaFlagTest` (2)
- `tests/test_graph_backup.py` (3)
- `tests/test_session_capture.py::ConversationImageLandTest` (4)
- `tests/test_topic_graph.py::ConversationMemoryToggleTest` (1)

`tests/test_scenarios.py` and `tests/test_contracts.py` (this session's additions) run
in well under 20 seconds combined and contribute 0 failures: 21 passed, 6 strict-xfailed
(scenarios written against a Phase 2A/2B/2C contract that has not landed yet — see the
scenario table in that session's own report). A strict xfail that starts passing without
its marker being removed is itself a failure the next pass must account for.

PENDING: the full-suite count and duration for the revision named at the top of this
document — filled in by the first verification pass, as an addendum, never by editing
this section.

## 4. Scratch data

Every live probe, fixture episode, fixture commitment, or fixture decision any
verification pass writes to a real (non-hermetic) hub during this checklist's
execution must be listed here by id and forgotten/closed/deleted before the pass is
recorded done — same discipline as docs/plans/2026-09-14-acceptance.md's "Forgotten
scratch episodes" section. PENDING: no live pass has run yet, so nothing has been
written.

---

## Addenda

_(A verification pass appends its dated section here — its own harness-matrix rows,
evidence, pytest baseline, and scratch-data cleanup list. Nothing above this line is
ever edited by that pass.)_
