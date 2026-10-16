# CLI reference

Every `khipu` command, grouped by what it is for. Run `khipu <command> --help` for flags.

## Running the CLI

There is no installed `khipu` executable in the repo. The CLI runs as a Python module:

```
cd packages/cli
python3 -m khipu <command>
```

Most commands need the Python dependencies on `PYTHONPATH` first; [packages/cli/README.md](../packages/cli/README.md) shows the one-time setup. To type `khipu` instead, add a shell alias (for example `alias khipu='python3.11 -m khipu'`). The desktop app bundles its own copy of the CLI and Python.

There are 57 top-level commands. The sections below cover all of them.

## Health and status

| Command | What it does |
| --- | --- |
| `status` | Row counts in Postgres. `--sample N` (default 0, which skips the file walk), `--drift` adds drift samples. |
| `doctor` | The single health command. Exits 0 only when `ok` is true, otherwise 2. |
| `activity` | Recent capture episodes. `--limit`, `--show ID`. |
| `revisions` | Topic revisions, including edits that lost a last-write-wins conflict. `--slug`, `--show ID`. |
| `secrets` | Shows whether the database address and model keys are present (Keychain, environment or file). Never prints values. |
| `git-sync` | Reports whether the memory-tree git auto-sync is alive. Exits 2 if red. |
| `features` | Shows feature switches. `--set NAME VALUE` changes one. |

`doctor` flags:

- `--quick`: runs all the local health checks (imports, config, outbox, capture liveness, jobs, backups, launchers and the rest) and leaves out the hub, network and probe checks. It is the first step of the promotion gate.
- `--probe`: runs a fresh end-to-end recall probe. It writes a nonce episode, then soft-deletes it.
- `--sample N`: caps the topic drift pass.
- `--harness`: the harness label the probe records (default `$KHIPU_HARNESS`, or `doctor`).
- `--memory-root`: the file wiki to check for drift.

`doctor` prints every `*_ok` check and a `not_configured` list. Plan usage and routing appear as warnings and never gate `ok`.

`secrets` has one subcommand, `verify` (`--gemini`, `--openai` or `--all`, `--json`), which proves a model key works with one cheap call. `secrets --set ACCOUNT` stores a secret read from stdin in the Keychain (accounts such as `database_url`, `gemini_api_key`, `voyage_api_key`).

## Search and recall

| Command | What it does |
| --- | --- |
| `search QUERY` | Search memory. |
| `get ID` | Load one record by id. |
| `graph ID` | Neighbours of a graph node. `--hops`, `--limit`. |
| `recall` | Recall tooling (warm service, logs, evaluation). |
| `reflect QUESTION` | Answer from memory with cited evidence. `--project`. Needs the `reflect` switch. |
| `briefs` | Source-backed topic briefs: `plan` (topics whose brief is missing or stale), `build` (`--topic`, `--limit`, needs the `briefs` switch), `show SLUG`. |

### search

Default mode is hybrid: vector similarity, token overlap and literal match, fused by rank.

- `--mode hybrid` (default), `--mode literal` (exact words, ids, errors), `--mode semantic` (meaning).
- `--semantic` is a deprecated alias for `--mode semantic`.
- Filters: `--kind {episode,topic,node,media,library}`, `--source NAME` (a library, implies `--kind library`), `--project`, `--since`, `--until` (ISO date or relative, like `7d`), `--session-id`, `--harness`, `--via`, `--limit`.

### get

`get ID` takes an episode id (digits), a topic slug, or a library id (`library:<name>:<doc>` or `library:<name>:<doc>#<chunk>`).

Topic bodies come in pages by byte offset. `--offset` and `--max-bytes` (default 65536, maximum 524288) pick a page. `--all` reads every page. `--kind` forces the record kind.

### recall

- `recall serve`: runs the warm recall service in the foreground. The opt-in `recall_daemon` job runs it under launchd.
- `recall status`: shows whether the service is up.
- `recall log [--tail N]`: shows the search query log (default 20).
- `recall zero-results [--days]` (default 7): lists queries that found nothing.
- `recall eval`: scores a golden set. Flags: `--golden`, `--path explicit|prompt|status|all` (default `explicit`), `--budget-ms`, `--record`, `--compare`, `--allow-changes`, `--replay`, `--rerank on|off`, `--relevance-floor on|off`, `--sample`, `--seed`.

## Commitments and decisions

| Command | What it does |
| --- | --- |
| `owed` | List, close, reopen or snooze commitments. |
| `decisions` | The decisions registry. |

`owed` flags: `--project`, `--status open|closed|stale`, `--limit`, `--close ID`, `--reopen ID`, `--snooze ID` with `--until` (a date or a window like `7d`, `2w`).

`decisions` subcommands:

- `list`: `--project`, `--since`, `--limit`, and one of `--standing`, `--superseded`, `--retracted`, `--all`.
- `supersede OLD NEW`: `--reason`, `--force`.
- `restore`, `retract` (`--reason`).
- `links`: candidate links between decisions. `--project`, `--state`.
- `confirm LINK`, `reject LINK`: settle a candidate link.
- `backfill`: `--dry-run`, `--apply`, `--limit`.

## Writing and editing memory

| Command | What it does |
| --- | --- |
| `capture` | Capture an episode from a JSON payload on stdin. Routed by `capture_mode`. Flags: `--payload-file`, `--mode legacy|dual|hub`. `capture now` flags a session for capture at its next Stop, whatever the cadence (`--note`, `--harness`, `--session-id`; the last two default to the most recently active local session). |
| `episode` | `episode forget ID` soft-deletes an episode and removes its vectors. `episode edit ID --summary S` corrects it and re-embeds. |
| `topic` | `topic purge SLUG --yes` hard-deletes an already tombstoned topic from the hub database (with its revisions and vectors). This Mac's offline copy keeps it until its next full dump. |
| `notes` | Index per-project harness notes as topics: `reconcile` (`--dry-run`, `--changed-only`), `index` (`--project`, `--cap`, `--dry-run`, `--force`), `split PATH`, `stale` (`--project`), `supersede OLD NEW`. |
| `reconcile` | Full sync of memory files into Postgres. |
| `regen-memory` | Write the `MEMORY.md` sidecar from topics. `--index` rebuilds the live `MEMORY.md`. |
| `topic-graph` | `topic-graph backfill` updates topic links and graph edges from topic files. |
| `outbox` | The offline queue for captures whose database write failed: `drain`, `status`, `retry-dead`. |

## Embeddings and libraries

| Command | What it does |
| --- | --- |
| `embed` | Vectors and embedding profiles. |
| `library` | Folders of `.txt` and `.md` files indexed as a search space. |
| `embed-media-backfill` | Embed PNG and JPEG files under sources that have `embed_media` on. `--dry-run`, `--yes` (needed over 1000 images), `--limit`, `--source-id`, `--profile`. |
| `embed-backfill` | Job-mode alias for `embed backfill --job`. |
| `library-backfill NAME` | Job-mode alias for `library backfill NAME --job`. |
| `models` | Per-role model settings: `show`, `set`, `welcome`. |
| `sources` | Graph membership: `list`, `enable`, `disable`, `set-embed-media`, `add`, `remove`, `export`. |

### embed

- `backfill`: embed missing vectors. `--kind episode|topic|commitment`, `--limit`, `--dry-run`, `--profile`, `--stale`, `--job`, `--job-id`.
- `estimate`: size a backfill. Makes no model call.
- `jobs`: list backfill jobs. `--clear` removes finished ones.
- `test-key`: check an embedding provider. `--provider`, `--endpoint`, `--model`.
- `index PROFILE`: build the vector index for a profile. `--table memory|library` (default both).
- `status`: coverage per profile.
- `activate PROFILE`: make a profile the active one. Refuses incomplete coverage unless `--force`.
- `profiles list|add|delete`: `add` takes `id` as `model@dim`, plus `--provider gemini|voyage|openai-compatible`, `--model`, `--dim`, `--endpoint`, `--normalize l2|none`.

### library

- `add NAME --root DIR --profile P` (both required), `list`, `status NAME`, `remove NAME --yes`.
- `enable`, `disable`, `set-profile NAME PROFILE`.
- `scan NAME [--allow-rebuild]`: pick up file changes.
- `backfill NAME`: embed documents. `--limit`, `--stale`, `--profile`, `--job`, `--job-id`.
- `import NAME PATH`: load vectors computed elsewhere (a graphify SQLite file or `.jsonl`). `--strip-prefix`, `--profile`, `--batch`.

### models

`models set` takes `--role synth|embed|vision` and `--provider gemini|anthropic|openai|openrouter|claude-cli|codex-cli|cloud|local|off`, with `--endpoint` and `--model-id`, or a JSON blob.

## Routing and plan usage

Khipu reports account state. It never picks, ranks or refuses work.

| Command | What it does |
| --- | --- |
| `usage` | Plan headroom for each subscription CLI (Claude, Codex, Grok, Muse). |
| `route` | The routing record: which accounts are open, limited or down, plus holds and owner rules. |

### usage

Flags: `--json`, `--fresh`, `--max-age S` (default 120), `--account LABEL`, `--account-key KEY`, `--sign-in`. `--sign-in` (with `--account`) starts that CLI's own login and re-reads. It works for Claude and Codex only.

### route

Top-level flags: `--json`, `--since V`, `--summary` (the session-start text), `--brief` (text for a job-runner brief), `--account A`.

- `check ACCOUNT`: check one account now.
- `report`: record a state. `--state limited|open|down`, `--account` or `--runner` with `--lane`, `--until`, `--window five-hour|weekly`, `--model`, `--message`, `--seen`, `--name`, `--evidence`.
- `hold WORDS`: hold work (`hold end ID` ends one). `--reason machine-busy|disk-low|keep|outage|other`, `--until`, one of `--account`, `--machine`, `--all`, and `--owner`.
- `wait ACCOUNT`: waits for the account to open. Exits 0 when open, 1 on timeout. It launches nothing. `--timeout`, `--confirmed`.
- `sources`: lists each runner folder with its last good read.
- Owner commands, which need a typed confirmation on a terminal (or the app's Settings, Routing) and refuse without one: `pause`, `lift`, `clear`, `note {set,end}`, `priority {set,confirm,undo,clear}`, `rule {add,edit,retire,restore,confirm,undo,dismiss}`, and `hold --owner`. `priority show` needs none. With `--at-owner-request`, a conversation can record a `priority set` or a `rule add` or `rule edit` that stays unconfirmed until the owner confirms it. Every `priority set` and `priority clear`, the owner's or a conversation's, carries `--expect` (the priority's version as read) and is refused without it or once the priority moved.

## Database, backups and other Macs

| Command | What it does |
| --- | --- |
| `migrate` | Apply pending SQL migrations. `--dry-run` shows applied and pending. |
| `db` | Database setup: `preflight`, `connect`, `move`, `status`. |
| `join` | Join kits for a second Mac: `export`, `import`, `advertise`, `receive`. |
| `config` | Show or set hub config. |
| `budget` | Daily call ceilings: `status`, and `lift`. |
| `paths` | Show or set the Mac-local data folder. `--set DIR` (copies the connection file and root certificate if missing). |
| `backup-local` | Zip the Mac-local data folder. `--out PATH`. |
| `import-local` | Restore it. `--source ZIP|DIR`. Merges and skips existing files by default; `--replace` overwrites. |
| `snapshot` | The offline read replica: `refresh` (hub must be up), `status`. |
| `graph-backup` | Graph snapshot backup, on the Mac that builds the graph: `record-local`, `offsite`, `status`, `drill`. |
| `graph-sync` | Mirror the graph file into Postgres. `--dry-run`, `--check` (exits 2 on drift). |
| `components` | Postgres and Graphify install, status and upgrades: `status`, `install {postgres,graphify}`, `upgrade {postgres,graphify}`, `check-remote [--full]`, `bootstrap-local-backup`, the single-purpose forms `install-local-postgres`, `install-graphify`, `upgrade-postgres` and `upgrade-graphify`, plus helpers for the app (`status-json`, `select-compat-row`). |

- `db move`: `--to DSN` or `--new-local`, `--dry-run`, `--into-nonempty`. It copies and verifies before switching and never writes to the source.
- `db connect`: `--dsn`, `--use-stored`, `--root-crt`, `--no-jobs`, `--no-prove`.
- `join export --out FILE [--allow-unencrypted]`; `join import --file FILE [--skip-migrate-check]`; `join advertise [--allow-unencrypted] [--timeout] [--pin]`; `join receive --pin`. Each reads the passphrase from `KHIPU_JOIN_PASSPHRASE`, `--passphrase-stdin` (first line of stdin) or `--passphrase-fd N`; export and advertise ask for it in a terminal. A literal `--passphrase VALUE` is refused, because other processes can read command-line arguments. Export and advertise refuse without a passphrase unless `--allow-unencrypted` is passed.
- `config`: `--set KEY VALUE`, `--unset KEY`, `--set-gateway-url URL`, `--set-capture-mode legacy|dual|hub`. See `khipu config --help` for the full key list.
- `budget lift {synth,memory,library}` takes exactly one of `--today`, `--cap N`, `--unlimited`.

## Integrations and sessions

| Command | What it does |
| --- | --- |
| `integrations` | Per-harness packs: `install [HARNESS]`, `verify`, `uninstall`, `status`. |
| `sessions` | Session capture queue: `drain`, `sweep`, `status`, `liveness`. |
| `aegis` | Older name for `sessions`. Same queue, same drain. Its `status` defaults to `--harness aegis`, where `sessions status` defaults to `all`. |
| `gateway` | `gateway token set` and `gateway token status` for the gateway bearer token. |
| `grok-bot-config` | Prints the account-level cloud-agent config and the secret name to add. |

- Harness choices for `integrations`: `all`, `claude_code`, `cursor`, `aegis`, `codex`, `grok_bot`. Flags: `--dry-run`, `--home`, `--project`, `--no-verify`. There is no top-level `install` command.
- `sessions status` takes `--harness all|claude_code|cursor|codex|aegis`. `sessions liveness` exits 2 if any harness's capture is failing (its last hook run errored, its last capture failed with nothing landed since, a job has waited too long, or turns keep arriving without a capture). A harness that has never run the hook counts as healthy. `drain` and `sweep` take `--limit` and `--dry-run`.
- `gateway token set` reads the token from stdin, or from an environment variable with `--from-env [VAR]` (default `KHIPU_GATEWAY_TOKEN`). It never prints the token.

## Jobs, releases and maintenance

| Command | What it does |
| --- | --- |
| `jobs` | Scheduled LaunchAgents: `status`, `install`, `refresh`, `uninstall`. |
| `promote` | Gate a ref in the inactive live slot and make it live on a pass. |
| `nightly` | Run the nightly memory wiki job once. |
| `monthly` | Run the monthly wiki classification once. |
| `graph-build` | Run the graph build once. |
| `backfill` | `backfill identity` derives repo and project for episodes. `--dry-run`, `--apply`, `--limit`. |
| `project` | `project backfill`: same idea for project identity. |
| `hygiene` | `hygiene paths` finds and purges bad path graph nodes. `hygiene commitments` re-judges open commitments. |

### jobs

Job names: `nightly`, `monthly`, `graph_build`, `notes_watch`, `queue_drain`, `recall_daemon`. `install` with no name schedules the first five (the default set). `refresh` with no name re-renders every installed job whose plist is stale and never installs a new one. `uninstall` with no name removes all six, including `recall_daemon`. `recall_daemon` is opt-in: `khipu jobs install recall_daemon`.

### promote

`promote [ref]` defaults to `origin/main`. It checks the ref out in the inactive live slot, runs the gate (doctor, a replay of recent recall queries from the last day, one real capture, a version check), and flips the live link on a pass. The recall replay needs at least one query logged in the last day.

- `--rollback`: flip back to the previous slot.
- `--status`: show the live commit, main, and how far behind.
- `--gate-only STEP`: run one gate step (`doctor`, `recall`, `capture`, `version`). It never flips the link.
- `--json`: machine-readable output.

Merging to main deploys nothing on a Mac that uses the live link. `promote` does.

## Scripts in `packages/cli/bin`

These are launchers, not commands you normally run by hand. `khipu integrations install` wires them into each harness.

| Script | Role |
| --- | --- |
| `khipu-mcp` | The stdio MCP server (server id `khipu`). |
| `khipu-stop-hook` | Capture hook for Stop, PreCompact and SessionEnd in Claude Code, Cursor and Codex. Also drains the Aegis queue. |
| `khipu-aegis-capture` | Aegis capture. It only queues, because it runs in the Aegis sandbox. |
| `khipu-recall-hook` | SessionStart recall push. `--cursor` emits Cursor's format. Refuses inside Aegis. |
| `khipu-prompt-recall` | Per-prompt recall push on UserPromptSubmit. Returns `{}` on any error. |
| `khipu-recall-client.py` | Asks the warm recall service for recall. |
| `khipu-delegation-hook` | Routing push before work is handed to another agent. Never denies or alters a call. |

For the MCP tools these launchers serve, see [mcp-tools.md](mcp-tools.md). For the routing record, see [routing.md](routing.md). For jobs, backups and the gateway, see [operations.md](operations.md).
