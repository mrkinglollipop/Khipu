# Desktop overhaul — scope (approved direction A, 2026-09-04)

Status: **scope approved 2026-09-04; phases 1–2 in progress the same evening.** The audit and the
mocks are the design source of truth; this document only fixes the decisions and the phasing so a
build session can start from a brief. Release decision (Matt, 2026-09-04): **one published release,
0.4.0**, carrying the audit fixes (PR #61) and the overhaul; no 0.3.17.

- Audit (findings, gap table, harness matrix, per-screen critique, copy glossary):
  maintainer artifact "Khipu Audit, September 2026" (private Claude artifact, 2026-09-04).
- Mocks: maintainer artifact "Khipu Overhaul Mocks" — direction A (six hi-fi screens), the
  component kit, and two discarded low-fi alternates (B command-first, C dense board).
- Fixes that landed before the overhaul: PR from branch `claude/khipu-memory-system-53e7a0`
  dated 2026-09-04 ("audit fixes").

## Decisions (made)

1. **Palette, radii, sidebar motif stay.** Tokens in `apps/desktop/src/App.css:6-72` are the
   source; both themes. Nothing else in the current UI is protected.
2. **Six screens, one per user job.** Home (Status + Doctor merged; the scheduled-jobs table
   rendered once), Recall (Search + Graph; graph walk is a drill-in from a result), Owed (new),
   Activity, Harnesses (Integrations renamed; "Another Mac" promoted), Settings (sub-sections:
   Database, Capture & models, Search index, Data & backups, Another Mac, Components, Updates,
   Advanced). Welcome becomes a first-run flow reachable from Settings. Revisions becomes a
   drill-in from a Home health item.
3. **Every red state names its cause in plain words and carries exactly one fix action.**
   Raw JSON lives behind an Advanced disclosure on every screen, collapsed by default.
4. **One component set:** Tag (four tones + kind variant), Callout (severity stripe), Dialog
   wrapper, ListRow, Tile, segmented control, chip filter, empty state. Type scale
   11/12/13/15/18/22; spacing scale 4/8/12/16/20/24/32. `Note` in Welcome.tsx is deleted in
   favour of Callout.
5. **Copy glossary** from the audit's UI section is binding: no DSN, mirror lag, File↔pg drift,
   LWW, topic_revisions, Graphify, SQL/PGQ or pgvector on a primary surface.
6. **Static mocks, not a prototype.** Interaction details not shown in the mocks (sorting,
   keyboard, drag) follow the existing app until a mock says otherwise.

## Phasing (one brief per session)

- **Phase 1, tokens and kit.** Token layer (spacing/type scales, `--backdrop`), the component
  set above, `RawJson` behind Advanced. No navigation change yet. Oracle: `npm run build`, a
  screenshot of each existing tab to prove nothing regressed visually.
- **Phase 2, navigation and Home.** Six-item rail, Home built from the mock (attention callout,
  four tiles, recent captures, owed preview, disclosure). Status and Doctor tabs removed; their
  data paths reused. Oracle: build + screenshot of Home in both themes, red and green states.
- **Phase 3, Recall and Activity.** Merge Search + Graph; result list + detail pane; Activity
  list with filters, day grouping, detail pane, Forget and Edit summary. Oracle: build + live
  search against the hub + screenshots.
- **Phase 4, Owed.** New screen over `khipu owed` (open/closed/stale, project chip, Done /
  Snooze / Reopen; Done and Reopen map to `khipu owed --close ID` / `--reopen ID`, which exist;
  Snooze needs a `due_after` update, add it to the CLI first). Oracle: build + live rows.
- **Phase 5, Harnesses and Settings.** Card grid with verified-round-trip line — the "verified"
  state must come from the CLI's probe and heartbeat evidence (`integrations status`/`verify`,
  `recall_probe`), never from app-local install state, which today shows "Installed, not yet
  verified" forever on packs the CLI has already probed green; Settings
  sub-navigation with the capture cadence, retention and redaction controls (retention and
  redaction need engine work first: see the audit's gap table; ship the controls disabled with a
  "coming" note only if the engine lands in the same release, otherwise omit them).
- **Phase 6, Welcome and cleanup.** Welcome re-flowed onto the new kit and glossary; delete
  dead CSS and the duplicated dialog code; update README screenshots.

Each brief opens: "Read docs/plans/2026-09-05-desktop-overhaul.md, then the mock for this
screen, then inspect the current tab's data calls in App.tsx before writing the new screen."
Stop conditions inside every brief: if a mock is missing for a state you need, stop and ask;
if a CLI verb is missing, add it to the CLI with a test before touching the UI; never delete
a data path until its replacement renders the same fields.
