# Setup that cannot strand you — scope (2026-09-05)

Status: **approved direction (Matt, 2026-09-05): "Everything that can be automated, should be. I cannot
have the user at any point thinking 'now what do I do?' or 'is it working?'"** Also in scope by the same
message: moving the database between hosts (local ↔ cloud, local ↔ local, cloud ↔ cloud).

Source of truth for what is wrong today: the setup-flow audit of 2026-09-05 (maintainer note in the ops
repo, state-of-play 2026-09-04 → "Setup audit"). Its twelve ranked gaps are the acceptance list below.

## The rule every screen follows

Every step ends in exactly one of three states, and the state is on screen:

1. **Working — proved by X.** A real round trip, not "saved": the server answered, the schema is
   current, a capture was written and found by search, the nightly is scheduled.
2. **Needs you — one action.** Plain words, one verb, one button or one thing to paste. Never a raw
   code (`vector_extension_missing`) and never a term without its gloss (DSN → "connection string";
   `sslmode=verify-full` → "encrypted, certificate checked").
3. **Fixing it…** with a progress line; the app does the work.

No step may end without a next action. "Skip for now" stays on every optional step (model key, graph,
agents); the database step has no skip because nothing works without it.

## One pipeline, shared by every entry point

`khipu db connect --dsn … --json` (CLI) and the desktop's Database step call the same engine
function, `khipu.setup.connect_database(dsn)`, which runs in order and reports each stage:

1. **Reach** — TCP/TLS/auth/database-exists, each mapped to plain words and a fix.
2. **Version** — Postgres 19 or newer; older says "your server runs 16; Khipu needs 19".
3. **Privileges** — can this role create the `vector` extension and tables? If not: "ask your host to
   run `CREATE EXTENSION vector;` on <db>" (managed providers: enable it in their console).
4. **Schema** — apply migrations (already existed; now reported as a stage).
5. **Graph** — the graph-query probe (already existed).
6. **Store** — Keychain entry, `root.crt` picked up from a file picker when the host gave one.
7. **Upkeep** — install the three LaunchAgents (`khipu jobs install`). This never happened from any
   app flow before today (audit gap #1).
8. **Prove** — a real capture → search round trip (`khipu.probe.run_probe("app")`), shown as
   "Memory round trip: 1.8 s".
9. **Summary** — "Working. <host> · N sessions remembered · nightly upkeep at 02:05".

Join-kit import and "new local database" end in the same stages 4–9.

## Move the database

`khipu db move --to DSN` and Settings › Database › "Move this memory to another database…":
run stages 1–5 against the target WITHOUT switching; copy every table in dependency order with
psycopg COPY (no external tools — the app ships none); verify row counts table by table; switch the
stored connection; run stages 7–9; leave the old database untouched; then say what remains: "Other
Macs: hand them a new join kit" with the button right there. A local-only source (Docker on this
Mac) is copied the same way; "new local database" is offered as a target.

## Second Mac

Before the join kit is offered: if the stored connection points at this Mac (localhost, 127.0.0.1,
a Docker-local address) say so and offer Move. The PIN screen states the same-network requirement
before the user tries. Import runs the shared pipeline.

## Finish, keys, harnesses, Docker

- Finish lists each red check with its one action (the same mapping Home uses); "Continue anyway"
  is always available and lists what is not yet proven. If no harness has verified, Finish runs the
  app probe itself so the recall gate is earned, not skipped.
- A model key is proven on save with one real call ("Key works · gemini-embedding-2").
- After a harness install the card says "Restart <harness>" and flips to "Verified" on its own the
  first time that harness's hook fires (liveness beat / probe file), no Verify click required.
- The Docker step polls every 5 s after the download link opens and advances by itself.
- Settings' "Connect to a server you run or another Mac…" opens the Database step directly.

## Phasing (one agent brief each; oracle after every phase)

- **Phase 1 — engine.** `khipu.setup` (connect pipeline, plain-words error map, jobs install,
  probe), `khipu.dbmove` (copy + verify + switch), CLI verbs `db connect|move|preflight`, tests
  with fake connections plus one live dry run against the maintainer hub (read-only stages).
- **Phase 2 — desktop Database step + Move + Another Mac** on the pipeline; Tauri commands
  `db_connect`, `db_move`, `db_preflight` (fixed argv); DOM check in the render harness; pixel pass.
- **Phase 3 — Finish gate, key validation, harness auto-verify, Docker polling, Settings deep link,
  README setup section** rewritten to mirror the app in under a page.

Acceptance: the twelve audit gaps closed one by one, each named in the PR; a stranger's-Mac dry run
of the remote flow against a fresh empty database (Docker on this Mac standing in for "a server")
recorded with screenshots.

## The gaps become oracles (Matt, 2026-09-05: "these should be part of the oracles")

A green suite must fail if any of the twelve gaps returns. Three layers, all run by `pytest` in the
normal suite (the Docker one skips itself when Docker is absent and says so):

1. **Contract tests (`tests/test_setup_contract.py`).** Every failure the connect pipeline can
   produce carries `title`, `detail` and a one-action `fix`, none of them a bare code; every stage
   id is reported; `mask_dsn` never leaks a password; the pipeline installs the LaunchAgents and
   runs the probe on success (asserted with fakes); `khipu db` verbs exit 0/1/2 with JSON.
2. **First-run end to end (`tests/test_setup_live.py`, Docker).** Start a scratch Postgres from the
   same image the local install builds (reuse `components_backup`'s drill-cluster helpers), run
   `connect_database` against the EMPTY database with store off, assert every stage ok and the
   schema at the newest migration; then `move_database` from the scratch database into a second
   scratch database (dry run + real), assert identical counts. This is the "stranger's first run"
   that no reviewer has to remember to do by hand.
3. **Desktop walkthrough (`apps/desktop` render harness, Phase 2).** The built UI driven by a
   scripted fake backend through the first-run flow (remote, local, join) asserting at each step
   that the DOM shows exactly one of the three states and never a raw error code; run as
   `npm run check:setup` and wired into the same oracle list as `npm run build`.
