# Operations

How to keep a Khipu install healthy: the background jobs, `khipu doctor`, the gateway, backups, snapshots, deploying new code with `khipu promote`, adding a second Mac, moving the database, and Docker.

Khipu runs as `python3 -m khipu` (there is no installed `khipu` executable in the repo). The examples below write `khipu` for short. Alias it if you like: `alias khipu='python3.11 -m khipu'`. Every command is listed in [cli.md](cli.md). First-time setup is in [setup.md](setup.md). Most of this page can also be done in the desktop app, see [desktop-app.md](desktop-app.md).

## Background jobs

Khipu runs its upkeep as macOS LaunchAgents. A fresh install (the Database step, or `khipu db connect`) schedules five of them:

| Job | When | What it does |
|---|---|---|
| `nightly` | Daily at 02:05 | Memory upkeep: consolidation, embedding catch-up, commitment hygiene, replica refresh |
| `graph_build` | Daily at 02:17 | Builds the graph |
| `monthly` | 1st of the month, 09:00 | Monthly organisation pass |
| `notes_watch` | When watched note files change (30 s throttle) | Syncs edited notes |
| `queue_drain` | Every 5 minutes | Drains the capture queue |

A sixth job, `recall_daemon`, is opt-in: `khipu jobs install recall_daemon`. It keeps a warm recall service running so the per-prompt hook avoids a cold Python start. See [how-it-works.md](how-it-works.md).

```bash
khipu jobs status                      # what is installed and loaded
khipu jobs install [NAME ...]          # render and load (default: all five)
khipu jobs refresh [NAME ...]          # re-render jobs whose plist is stale
khipu jobs uninstall [NAME ...]
```

The desktop app lists the jobs under Settings, Advanced.

Do not start the nightly by hand in the middle of the day. It can race with live captures.

## Doctor

`khipu doctor` is the single health command. It prints one JSON object and exits 0 only when `ok` is true, otherwise 2.

```bash
khipu doctor            # full check
khipu doctor --quick    # imports, config, secrets presence, jobs, summariser only; no hub, no network
khipu doctor --probe    # also runs a fresh end-to-end recall probe (writes, then soft-deletes, a test episode)
khipu doctor --sample N # cap the topic drift pass
```

`ok` is the AND of many checks, not just the nine most people look at. Every one is printed as a `*_ok` key. Checks that do not apply on this machine are listed under `not_configured`. That is not red.

The main ones:

| Check | Red when |
|---|---|
| `hub_ok` | The database is unreachable |
| `drift_ok` | A file episode has no row in PostgreSQL |
| `graph_drift_ok` | Graph nodes and edges disagree with their source |
| `outbox_ok` | Any write is pending, any write is dead-lettered, or the oldest pending one has waited over an hour |
| `capture_liveness_ok` | A harness's hook is erroring, its drain is failing or stuck, a queued job has waited too long, or the hook sees turns but never decides a capture is due |
| `git_sync_ok` | The memory repo's auto-sync failed, was blocked, was left mid-flow (off `main`, or on a leftover autosync branch), or has not run since the last nightly. Judged only on the Mac that runs the sync |
| `dsn_file_ok` | The on-disk connection string names a certificate file that does not exist |
| `backup_ok` | See [Backups](#backups-and-restore) |
| `graph_backup_ok`, `graph_offsite_ok` | See [Graph backup](#graph-backup) |
| `hub_snapshot_ok` | The offline copy is stale. See [Snapshots](#snapshots-the-offline-copy) |
| `jobs_ok` | An expected background job is missing or not loaded |
| `embed_coverage_ok`, `synth_budget_ok`, `synth_provider_ok` | Embeddings, the daily call ceiling or the summariser setup need attention |
| `recall_probe_ok`, `recall_daemon_ok`, `prompt_recall_*_ok` | Recall is slow, failing or degraded |
| `bundle_seal_ok`, `launchers_ok` | The app bundle or a launcher does not match what is installed |

Plan usage and routing appear in the output as warnings. They never turn `ok` red. See [routing.md](routing.md).

If something is red, read the check's own payload: `drift`, `outbox`, `capture_liveness`, `git_sync`, `backup`. It says what it saw. More in [faq.md](faq.md).

Related commands:

- `khipu status` prints row counts. `--sample 0` skips the file walk. `--drift` adds the drift sample.
- `khipu reconcile` syncs files into PostgreSQL. It is safe to run again.
- `khipu outbox status|drain|retry-dead` handles the offline write queue.
- `khipu sessions status|liveness|drain|sweep` handles the capture queue.
- `khipu revisions` shows conflicting topic edits (the older version is kept).

## The two queues

There are two, and they are different.

- The capture queue holds session windows that have not been summarised yet. The `queue_drain` job and the hooks drain it.
- The outbox holds captures that were summarised but could not be written because the database was unreachable. They replay when it is back.

`outbox_ok` stays red until the outbox is empty, so a silent backlog is not possible.

## The gateway

The gateway is an HTTPS MCP server for agents that cannot reach your database or install hooks: Grok Bot, Cursor cloud agents, and Aegis (which cannot reach Postgres or the Keychain from its sandbox). Claude Code, Cursor and Codex on your Mac talk to the database directly and do not use it. If you use none of the gateway agents, skip it.

What it is:

- A small Python server (standard library `ThreadingHTTPServer`). MCP Streamable HTTP is `POST /mcp`. `GET /healthz` is unauthenticated and answers only `{"ok": true}`.
- It binds `127.0.0.1:8787` by default (`KHIPU_GATEWAY_BIND` or `--bind`). Put a TLS-terminating reverse proxy (Caddy or nginx, with a real certificate) in front of it.
- It refuses to start unless `KHIPU_GATEWAY_TOKEN` is set and at least 24 characters. Extra labelled tokens: `KHIPU_GATEWAY_TOKENS=label:token,...`.
- Limits: 1 MiB request body, 120 requests a minute per IP, 240 a minute per token, 10 failed auths a minute, 32 calls per JSON-RPC batch (`KHIPU_GATEWAY_MAX_BATCH`). Browser requests with an `Origin` are refused unless listed in `KHIPU_GATEWAY_ALLOWED_ORIGINS`.
- It serves the same tools as the local MCP server, with exceptions: `khipu_usage` and `khipu_route` answer `available: false` through it, and `khipu_forget` only forgets episodes captured by cloud harnesses. Capture is accepted only in `hub` mode. See [mcp-tools.md](mcp-tools.md).
- For library search the gateway host also needs the embedding key of every enabled library profile. A missing key drops that library's meaning leg and reports `degraded_legs`. It does not fail the search.

The database is never exposed. The gateway is the only door.

### Run it

```bash
docker build -f ops/docker/Dockerfile.gateway -t khipu/gateway .
docker run -d --name khipu-gateway --network host \
  -e KHIPU_DATABASE_URL='postgresql://...' \
  -e GEMINI_API_KEY='...' \
  -e KHIPU_GATEWAY_TOKEN="$(openssl rand -hex 32)" \
  khipu/gateway
```

The image is `python:3.11-slim`, runs as a non-root user, and sets `KHIPU_CAPTURE_MODE=hub`.

### Connect agents to it

On your Mac:

```bash
khipu config --set-gateway-url https://khipu.example.com   # must be https://
khipu integrations install grok_bot                        # prints the config to paste
khipu integrations verify grok_bot                         # handshake plus a wrong-token check
```

Without `--project`, `install grok_bot` prints the account-level MCP config to paste once into Cursor's cloud-agent settings, so every repo gets Khipu. Add the same token as a secret named `KHIPU_GATEWAY_TOKEN` there. With `--project DIR` it instead writes `DIR/.cursor/mcp.json` and a rule file. The token is never written to a file.

For Aegis, run `khipu gateway token set` once to stage the token (it reads stdin, or `--from-env`, and never prints it). `khipu gateway token status` shows only whether the file exists, its mode and its size. Aegis search goes through the gateway. Aegis capture still reaches Postgres through the drain that runs outside its sandbox. Details in [integrations.md](integrations.md).

## Backups and restore

`backup_ok` in doctor is red unless both of these hold:

1. A successful `walg_basebackup` or `pg_dump` event was recorded in the last 36 hours.
2. A `restore_drill` event with status ok exists.

It is also red if the events table is missing or cannot be read. On a local Docker Postgres only `pg_dump` counts.

What the repo does and does not do:

- **Local Docker database.** The setup step (Database step in the app, or `khipu components bootstrap-local-backup`) takes a dump with `docker exec ... pg_dump` plus `docker cp`, restores it into a scratch container (port 54338), and records both events. Dumps go to the `backups/pg` folder under Khipu's Application Support folder. Set `KHIPU_PG_DUMP` or `KHIPU_PG_RESTORE` to use host binaries instead of the container.
- **Nothing in this repo re-runs that on a schedule.** The 36-hour rule means `backup_ok` goes red about a day and a half after setup unless something re-runs the backup, or you run `khipu components bootstrap-local-backup` again.
- **WAL-G.** Doctor reads `walg_basebackup` events, but the repo does not configure or schedule WAL-G, WAL archiving or recurring restore drills. If you want object-storage backups with continuous archiving, set that up on your Postgres server yourself and record the events. A backup that has never been restored is not a backup, which is why the drill is part of the check.

### The Mac-local data folder

Khipu keeps some state on the Mac (queues, state files, the offline copy). This is separate from the database.

```bash
khipu paths                          # show the data folder
khipu backup-local --out PATH        # zip it (PATH is a .zip or a directory)
khipu import-local --source ZIP|DIR  # restore; merges and skips existing by default
khipu import-local --source ZIP --replace
```

These are also in the app under Settings, Data & backups.

### Getting your memory out

Memory is ordinary tables in your own PostgreSQL. `khipu regen-memory` writes topics back out as markdown at any time.

## Graph backup

For the Mac that builds the graph (the producer). Other Macs report these checks as skipped.

```bash
khipu graph-backup status
khipu graph-backup record-local   # record a local snapshot
khipu graph-backup offsite        # rclone copy of the latest snapshot to an r2: remote
khipu graph-backup drill          # restore into scratch; never touches the live graph
```

- `graph_backup_ok` is red when the latest local snapshot is missing, empty or older than 36 hours.
- `graph_offsite_ok` is red when no `r2:` rclone remote is configured, no successful offsite copy is recorded, or the last copy is more than 8 days old. The graph job schedules the offsite copy when it is due (every 7 days) and a drill every 8 days.

You need an rclone remote named `r2` for the offsite part.

## Snapshots, the offline copy

Khipu keeps a read-only copy of memory in a local SQLite file, `hub_snapshot.sqlite`. Reads fall back to it when the database is unreachable, and the per-prompt recall hook reads it first. It holds episodes, topics, revisions, nodes, edges, embedding profiles, embeddings, decisions and deliverables. It does not hold libraries or commitments. Keyword search works offline. Meaning search works offline only if the embedding model is a local one that matches the stored profile.

```bash
khipu snapshot refresh   # full dump (the hub must be up)
khipu snapshot status    # age, size, row counts
```

- A full dump runs in the nightly job and on `khipu snapshot refresh`. Doctor and status do not dump it.
- Between dumps, capture, edits, decisions and forgets update the copy as they happen.
- Doctor reports freshness per table: fresh, behind, stale or unknown. Only stale turns `hub_snapshot_ok` red. Search results from a stale copy carry `stale: true`.

Planned, not built: a bounded incremental `snapshot sync` and a scheduled sync.

## Promote: merged is not live

On a Mac that runs Khipu from a pinned live checkout, pulling or merging `main` deploys nothing. `khipu promote` is how new code goes live.

How it works:

- Two git worktree slots, `live-a` and `live-b`, sit under `~/.config/khipu`. A symlink named `live` points at the active one. Launchers, launchd jobs and MCP servers all resolve through that link.
- `promote` checks the inactive slot out at a ref, runs a four-step gate against it, and only on a pass flips the link (atomically).
- The gate steps, in order: `doctor --quick` under launchd's own environment, a replay of up to five recent per-prompt recall queries within a 1.2 s budget, one real capture end to end (then forgotten), and a version and schema check.
- Each run is one line in `state/promotions.jsonl`. Live code is precompiled before the flip.
- A Mac with no `live` link is unaffected. `integrations install` and `jobs install` keep pointing at the checkout they run from.

```bash
khipu promote                    # gate origin/main and go live on pass
khipu promote REF                # a different ref
khipu promote --status           # live commit, main commit, how far behind, last gate result
khipu promote --gate-only STEP   # run one step (doctor, recall, capture, version); never flips
khipu promote --rollback         # flip back to the other slot, no gate
khipu promote --json
```

Running MCP servers keep the old code until their session restarts. Never delete a slot.

Planned, not built: a `promotion` block in doctor and a "merged, not live" display with a Promote button on Home. For now only `khipu promote --status` shows the gap.

## Adding another Mac

A second Mac uses the same database. You give it a join kit.

On the working Mac:

- App: Settings, Another Mac, "Set up another Mac", then "Save join kit...". It is disabled if the database lives only on this Mac (this-mac or local Docker), because another Mac could not reach it.
- CLI: `khipu join export --out FILE.khipujoin [--passphrase ...]`.

On the new Mac:

- App: Welcome, Database, "Join a Khipu I already have", then "Import join kit file...". "Find nearby Mac" uses the PIN path.
- CLI: `khipu join import --file FILE.khipujoin [--passphrase ...]`.

Nearby pairing without a file: on the working Mac `khipu join advertise [--pin ...]` (Bonjour on the local network, TLS, 10 minutes by default); on the new Mac `khipu join receive --pin PIN`.

Notes:

- The kit format is `khipu-join-v1`, encrypted with AES-GCM (PBKDF2 key, 390,000 iterations). The passphrase is optional but a good idea. Treat the kit as a credential.
- A kit whose database address is `localhost` is refused.
- Import checks the live row counts. It fails hard only when the kit expects data and the live database shows none.
- After you move the database (below), every other Mac needs a fresh kit.

## Moving the database

App: Settings, Database, "Move this memory to another database...". CLI:

```bash
khipu db move --dry-run --to 'postgresql://...'   # check the target, change nothing
khipu db move --to 'postgresql://...'             # or --new-local for a fresh local Docker database
```

It copies every table in foreign-key order, checks that the counts match, then switches this Mac over (and re-checks upkeep jobs and a memory round trip). The old database is only read, never changed. `--into-nonempty` allows a target that already has data.

Related: `khipu db status` (masked address, host kind, row counts), `khipu db preflight`, `khipu db connect` (flags `--dsn`, `--use-stored`, `--root-crt`, `--no-jobs`, `--no-prove`), and `khipu migrate [--dry-run]` to apply schema migrations.

The connection string lives in the Keychain first. Order of lookup: `KHIPU_DATABASE_URL`, then the Keychain, then the file named by `KHIPU_DSN_FILE`. Store it with `printf '%s' 'postgresql://...' | khipu secrets --set database_url`. Khipu needs Postgres 19 or later with the `vector` extension.

## Docker

Docker is needed in two places, both optional:

- **A local database.** "Set up a new database on this Mac (needs Docker)" runs Postgres 19 with pgvector in a container named for Khipu. The app uses a local image if it has one, otherwise tries to pull the published image, and builds from `ops/docker/Dockerfile.pgvector` (Postgres 19 beta with pgvector 0.8.6) as a last resort.
- **The gateway.** See [The gateway](#the-gateway). Its image is built from `ops/docker/Dockerfile.gateway`.

If you connect to a Postgres you already run, you do not need Docker on the Mac at all.
