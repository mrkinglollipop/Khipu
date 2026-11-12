# CLI reference

Every `khipu` command, grouped by what it is for. Run `khipu <command> --help` for flags.

## Running the CLI

There is no installed `khipu` executable in the repo. The CLI runs as a Python module:

```
cd packages/cli
python3 -m khipu <command>
```

Most commands need the Python dependencies on `PYTHONPATH` first; [packages/cli/README.md](../packages/cli/README.md) shows the one-time setup. To type `khipu` instead, add a shell alias (for example `alias khipu='python3.11 -m khipu'`). The desktop app bundles its own copy of the CLI and Python.

There are 61 top-level commands; the reference below includes storage, ledger, work, owner-direction, and accounts.

## Health and status

| Command | What it does |
| --- | --- |
| `storage` | Free disk space and the locations and sizes of Khipu's local stores, as JSON. |
| `status` | Row counts in Postgres. `--sample N` (default 0, which skips the file walk), `--drift` adds drift samples. |
| `doctor` | The single health command. Exits 0 only when `ok` is true, otherwise 2. |
| `rules show` | For each app Khipu knows: the rule files it reads (path, size, whether a Khipu block is in it), the hooks it has wired and whether each is Khipu's, and its size cap where it has one. Read-only; `--json` for the same as data. |
| `activity` | Recent capture episodes. `--limit`, `--show ID`. |
| `health` | The five checks the app shows (saving, finding, routing, database, jobs) as plain rows, with a line each for what Khipu is doing and what you do. A view over `doctor`; `--json` is the only form, and it exits 0 whatever the checks say. Also shares this Mac's status row through the hub. |
| `macs` | One status row per Mac, shared through the hub: this Mac live, every other Mac as last reported (marked stale after 15 minutes). A Mac on an older build, a hub that is down or a missing table gives this Mac only, with a note. `--json` is the only form. |
| `running` | Which jobs are running, on which Mac, and which have gone quiet. This Mac live, the other as last reported; it also refreshes this Mac's remembered running part. `--json` is the only form. |
| `revisions` | Topic revisions, including edits that lost a last-write-wins conflict. `--slug`, `--show ID`. |
| `secrets` | Shows whether the database address and model keys are present (Keychain, environment or file). Never prints values. |
| `git-sync` | Reports whether the memory-tree git auto-sync is alive. Exits 2 if red. |
| `features` | Shows the one feature switch left (`routing_push`). `--set NAME VALUE` changes it. |

`doctor` flags:

- `--quick`: runs all the local health checks (imports, config, outbox, capture liveness, jobs, backups, launchers and the rest) and omits hub reachability, network and probe checks; reads the hub only for the pending-migration check. It is the first step of the promotion gate.
- `--probe`: runs a fresh end-to-end recall probe. It writes a nonce episode, then soft-deletes it.
- `--sample N`: caps the topic drift pass.
- `--harness`: the harness label the probe records (default `$KHIPU_HARNESS`, or `doctor`).
- `--memory-root`: the file wiki to check for drift.

`doctor` prints every `*_ok` check and a `not_configured` list. Plan usage and routing appear as warnings and never gate `ok`.

`secrets verify` (`--gemini`, `--openai` or `--all`, `--json`) makes small provider checks: `--gemini` tests embeddings and generation, `--openai` tests the configured local OpenAI-compatible endpoint, and `--all` checks every configured provider, including model-list checks. A run may make several requests. `secrets --set ACCOUNT` stores a secret read from stdin in the Keychain (accounts such as `database_url`, `gemini_api_key`, `voyage_api_key`).

## Search and recall

| Command | What it does |
| --- | --- |
| `search QUERY` | Search memory. |
| `get ID` | Load one record by id. |
| `graph ID` | Neighbours of a graph node. `--hops`, `--limit`. |
| `recall` | Recall tooling (warm service, logs, evaluation). |

### search

Default mode is hybrid: vector similarity, token overlap and literal match, fused by rank.

- `--mode hybrid` (default), `--mode literal` (exact words, ids, errors), `--mode semantic` (meaning).
- `--semantic` is a deprecated alias for `--mode semantic`.
- Filters: `--kind` depends on mode — semantic: `episode`, `topic`, `media`; literal and hybrid: `episode`, `topic`, `node`; plus `library` with `--source NAME` (implies `--kind library`). Also `--project`, `--since`, `--until` (ISO date or relative, like `7d`), `--session-id`, `--harness`, `--via`, `--limit`.

### get

`get ID` takes an episode id (digits), a topic slug, or a library id (`library:<name>:<doc>` or `library:<name>:<doc>#<chunk>`).

Topic bodies come in pages by byte offset. `--offset` and `--max-bytes` (default 65536, maximum 524288) pick a page. `--all` reads every page. `--kind` forces the record kind.

### recall

- `recall serve`: runs the warm recall service in the foreground. The opt-in `recall_daemon` job runs it under launchd.
- `recall status`: shows whether the service is up.
- `recall log [--tail N]`: shows the search query log (default 20).
- `recall zero-results [--days]` (default 7): lists queries that found nothing.
- `recall eval`: scores a golden set. Flags: `--golden`, `--path explicit|prompt|status|all` (default `explicit`), `--budget-ms`, `--record`, `--compare`, `--allow-changes`, `--replay`, `--sample`, `--seed`.

## Explicit asks and archived commitments

| Command | What it does |
| --- | --- |
| `needs-you` | Show what waits on the owner. `--json` prints structured output; `--item KEY_OR_NUMBER` shows one whole item. `--yes KEY_OR_NUMBER` and `--no KEY_OR_NUMBER` display it and ask once for a typed yes in a real owner terminal. Conversation shells answer in the conversation or app. |
| `owed` | List and update owner asks and archived owner items; dismiss routing directions. |
| `work` | Project progress, running jobs, and Waiting on you; see below for summary checks and owner answers. |

`owed` listing flags: `--project`, `--status open|closed|stale`, `--limit` (default 50), `--owner user`, `--kind followup|blocker|question|promise|routing_direction`, `--origin conversation|job`, `--query WORDS`, `--pr NUMBER`, and `--looks-done`. Follow `next_cursor` with `--cursor`; `--envelope` always includes stale, snapshot freshness, and waiting-to-sync metadata. `owed_page_max` caps pages at 200 by default; 0 removes the cap.

`--close ID[,ID...]`, `--reopen ID[,ID...]`, and `--reassign ID[,ID...]` accept comma-separated ids. Reassignment accepts `--to user`; agent and assistant to-do lists were removed. Multiple ids require `--reason` and default to a 500-item cap (`owed_bulk_max`; 0 unlimited). A multi-item change including the owner's items requires owner authorization. `--dismiss ID[,ID...]` dismisses `rd-...` routing directions. `--snooze ID --until WINDOW` takes a date or a window like `7d` or `2w`.

`work [--json]` shows the Work view. `work check FILE` validates a runner summary; `hidden`, `hide ID`, and `unhide ID` manage hidden commitments. `same ROW COMMITMENT` and `not-same ROW COMMITMENT` answer a proposed match, with ROW written as `<project>#<row id>`. Hide/unhide and match answers support `--owner-direction`.

## Writing and editing memory

| Command | What it does |
| --- | --- |
| `ledger` | Project work rows: add, set, get, list, loose-ends, needs-owner (alias needs-matt), render, check, remove, removed, restore, purge, projects, project, import, and export. |
| `owner-direction` | Direction settings, owner-actions log, and Undo; see below. |
| `capture` | Capture an episode from a JSON payload on stdin. Routed by `capture_mode`. Flags: `--payload-file`, `--mode legacy|dual|hub`. `capture now` flags a session for capture at its next Stop, whatever the cadence (`--note`, `--harness`, `--session-id`; the last two default to the most recently active local session). `capture export` adds a line to the session files for every live hub episode that has none, append-only; the nightly runs it (`--dry-run` counts without writing, `--json`). |
| `episode` | `episode forget ID` soft-deletes an episode and removes its vectors. `episode edit ID --summary S` corrects it and re-embeds. |
| `topic` | `topic purge SLUG --yes` hard-deletes an already tombstoned topic from the hub database (with its revisions and vectors). It immediately purges this Mac's replica too; a replica failure is reported as partial, and that copy then needs a successful full refresh. |
| `notes` | Index per-project harness notes as topics: `reconcile` (`--dry-run`, `--changed-only`), `index` (`--project`, `--cap`, `--dry-run`, `--force`), `split PATH`, `stale` (`--project`), `supersede OLD NEW`. |
| `reconcile` | Full sync of the memory folder's topic pages (`topics/*.md`) into Postgres. Episodes go the other way, through `capture export`. |
| `regen-memory` | Write the `MEMORY.from-khipu.md` sidecar from topics. `--index` rebuilds the live `MEMORY.md`. |
| `provenance` | Fact provenance maintenance: `backfill` proves legacy restaters' claims; `index` indexes episode source identities and sweeps refused carriers. |
| `outbox` | The offline queue for captures whose database write failed: `drain`, `status` and `retry-dead`. `retry-dead` leaves jobs refused for not announcing their source identities held. |

`ledger` uses `--project` to select a project and `--json` for JSON output. Writes accept `--op-id` for safe retries. `ledger export --tracker-json` exports the tracker shape. `ledger project` can rename, hide, or show a project.

`owner-direction` supports `show`, `actions [--json]`, `on`, `off`, `set KEY VALUE`, and `undo ID`. Direction defaults on, with session binding on and a six-hour eligible-owner-turn window. Supported owner commands accept `--owner-direction` from a conversation with a recent hook-recorded eligible owner turn and verified prompt-hook provenance. The check does not inspect the owner's words; submit only changes the owner requested. Background and dispatched jobs are refused.

## Embeddings and libraries

| Command | What it does |
| --- | --- |
| `embed` | Vectors and embedding profiles. |
| `library` | Folders of `.txt` and `.md` files indexed as a search space. |
| `embed-backfill` | Job-mode alias for `embed backfill --job`. |
| `library-backfill NAME` | Job-mode alias for `library backfill NAME --job`. |
| `wiki` | Read-only `plan --json --since DAYS` (default 30) and `draft --sample N [--model ID] [--out DIR]`; live `run [--max N] [--json]`; exact undo `undo <slug>` or `undo --night YYYY-MM-DD [--run ID]` (also `undo --run ID`). |
| `models` | Per-role model settings: `show`, `set`, `welcome`. |
| `sources` | Memory sources: `list`, `enable`, `disable`, `remove`. |

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
- `import NAME PATH`: load vectors computed elsewhere (an embeddings SQLite file or `.jsonl`). `--strip-prefix`, `--profile`, `--batch`.

### models

`models set` takes `--role synth|embed|vision` and `--provider gemini|anthropic|openai|openrouter|claude-cli|codex-cli|cloud|local|off`, with `--endpoint` and `--model-id`, or a JSON blob.

## Routing and plan usage

Khipu reports account state and ranks advisory launch choices. The orchestrator decides and launches work; Khipu does not enforce its advice.

| Command | What it does |
| --- | --- |
| `usage` | Plan headroom for Claude, Codex, Grok, Muse, GLM Coding Plan, and configured HTTP-quota accounts. Supports one DeepSeek account, reporting measured balance-derived spend against rolling caps, with known or partial coverage; stale, failed, missing, or untrusted history stays unknown. |
| `accounts` | Tracked login lifecycle: status, track, rename, stop, move, decline, offer-again, auto-track, merge, and request confirm/undo. |
| `route` | The routing record: which accounts are open, limited or down, plus holds and owner rules. |

### usage

Flags: `--json`, `--fresh`, `--max-age S` (default 120), `--account LABEL`, and `--account-key KEY`. `--refresh-due S` reads only accounts whose figures are older than S seconds. `--sign-in` with `--account` starts that CLI's own login, waits, then re-reads; it works for Claude and Codex only.

`--discover` finds known login folders; `--now` checks every due folder without the daily wait. `--check FOLDER` checks one found login. `--try --harness H [--folder F]` reads an unsaved account once. `--forget-login-paths` clears login paths recorded for saved conversations.

Discovery can automatically track eligible logins a conversation used. `usage_auto_track` defaults on for a new install with no config file; an existing config without the setting migrates to off. `accounts auto-track on|off` controls it.

For the app's separate-login workflow, `--sign-in-offers` lists available choices; `--sign-in-folder --harness H --name N` makes a login folder. `--sign-in-new --harness H --folder F` starts login and returns an attempt id immediately; `--again` first ends a running attempt. Use `--sign-in-status ID` and `--sign-in-cancel [ID]`, optionally `--remove-folder` with the harness/folder after login ends to remove an empty folder Khipu made. `--sign-in-reap` ends a leftover attempt.

Usage normally reads the CLI's usage protocol. Claude may use a tiny Haiku turn if its reply omits figures, and Muse may use a tiny turn to refresh stale observations, Claude's at most every 2 minutes and Muse's every 30 minutes per login. Every account is read each minute, so a reset shows within about 2 minutes. Claude reset checks can use the same fallback.

`accounts status` supplies the list version for `--expect` on `track`, `rename`, `stop`, `move`, and `request confirm|undo`. `track` takes `--entries JSON`; `stop` offers choices for retained priority, pause, lane mappings, and stopping the last account. Run the subcommand's help for those choices.

### route

Top-level flags: `--json`, `--since V`, `--page` (the routing page; `--summary` and `--brief` print the same page), `--account A`.

- `check ACCOUNT`: check one account now; Claude may use the bounded tiny-turn fallback if its usage reply omits figures.
- `report`: record a state. `--state limited|open|down`, `--account` or `--runner` with `--lane`, `--until`, `--window five-hour|weekly`, `--model`, `--message`, `--seen`, `--name`, `--evidence`. A runner lane must resolve through its provider/account declaration and local identity binding, or be named in `routing_lane_map`; unresolved or ambiguous declarations stay unmatched.
- `hold WORDS`: hold work (`hold end ID` ends one). `--reason machine-busy|disk-low|keep|outage|other`, `--until`, one of `--account`, `--machine`, `--all`, and `--owner`.
- `wait ACCOUNT`: exits 0 for open or probably open, and 1 at timeout. `--confirmed` requires an open state confirmed by Khipu's own usage probe or reset check. It launches nothing. `--timeout` bounds the wait.
- `sources`: lists each runner folder with its last good read.
- `advise [ACCOUNT]`: rank advisory choices. Filters: `--lane`, `--tier opus|sonnet|haiku`, `--kind build|fix|check`, `--privacy`, `--sensitive`, and `--purpose job|check|orchestration` (default job).
- `cap set|remove ACCOUNT --kind share|per_day|pace_target|spend|concurrency`: owner-managed caps for `--purpose job|check`. Check allowances can include `--tier` and `--sensitive-only` qualifiers; the subcommand help lists values for each cap kind.
- `definition set provider|account|lane_map --value JSON`: owner-managed definitions.
- `tier-table show|set|confirm|clear`: owner-managed tier table; a conversation can propose `set` with `--at-owner-request`.
- `replication status|sync|restore`: report, synchronize, or restore routing replication. Cross-Mac replication defaults off and must be enabled by the owner. `copy publish` publishes the permitted closed routing copy to the hub.
- Other owner commands: `pause`, `lift`, `clear`, `note set|end`, `priority set|confirm|undo|clear`, `rule add|edit|retire|restore|confirm|undo|dismiss`, and `hold --owner`. Supported commands accept app authorization, a typed terminal confirmation, or `--owner-direction` from an eligible conversation in which the owner typed recently; unqualified and background calls are refused. `priority show` needs no authorization.

With `--at-owner-request`, a conversation can record `priority set`, `rule add`, or `rule edit` as unconfirmed changes. Priority requests and existing-rule edits carry the prepared owner revision in `--base`. Owner priority set, confirm, undo, and clear require the observed priority version in `--expect`; stale or crossed requests do not overwrite newer owner changes.

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
| `snapshot` | Offline read replica: `refresh` (hub must be up), `status`, and `heal` (background recovery stages and refresh of an old copy). |
| `components` | Postgres install, status and upgrades: `status`, `install postgres`, `upgrade postgres`, `check-remote [--full]`, `bootstrap-local-backup`, the single-purpose forms `install-local-postgres` and `upgrade-postgres`, plus helpers for the app (`status-json`, `select-compat-row`). |

- `db move`: `--to DSN` or `--new-local`, `--dry-run`, `--into-nonempty`. It copies and verifies before switching and never writes to the source.
- `db connect`: `--dsn`, `--use-stored`, `--root-crt`, `--no-jobs`, `--no-prove`.
- `join export --out FILE [--allow-unencrypted]`; `join import --file FILE [--skip-migrate-check]`; `join advertise [--allow-unencrypted] [--timeout] [--pin]`; `join receive --pin`. Each reads the passphrase from `KHIPU_JOIN_PASSPHRASE`, `--passphrase-stdin` (first line of stdin) or `--passphrase-fd N`; export and advertise ask for it in a terminal. A literal `--passphrase VALUE` is refused, because other processes can read command-line arguments. Export and advertise refuse without a passphrase unless `--allow-unencrypted` is passed.
- `config`: `--set KEY VALUE`, `--unset KEY`, `--set-gateway-url URL`, `--set-capture-mode legacy|dual|hub`. Run `khipu config` to see current setting names, values, defaults, and sources. `config --help` lists common settings; `--set`/`--unset` also support CLI-path, replica, storage, ledger, work, offline, and routing groups.
- `budget lift {synth,memory,library}` takes exactly one of `--today`, `--cap N`, `--unlimited`.

`snapshot heal` checks every five minutes by default, refreshes copies at least 15 minutes old, and memoizes a failed hub connection for 60 seconds.

Configuration changes to `usage_accounts` use `--expect` with the account-list version. Owner-controlled account and routing settings can be requested with `--at-owner-request` or changed through eligible `--owner-direction` authorization. Answer account-list requests with `accounts request confirm|undo --expect V`; answer routing-setting requests with `config --answer-request KEY confirm|undo`.

Configuration includes these shipped defaults. Duration values below are shown in seconds (`s`) or days (`d`); use `config --set KEY VALUE` to change them.

| Group | Setting = default |
| --- | --- |
| Replica | `replica_refresh_after=900s`, `replica_check_every=300s` (minimum 60s), `hub_down_memo_for=60s` |
| Offline | `hub_statement_timeout=30s`, `hub_dead_connection_after=60s`, `app_cli_timeout=60s` |
| Storage | `disk_warn_free_gb=10`, `disk_warn_free_percent=5`, `disk_fail_free_gb=3`, `disk_fail_free_percent=2`, `commitments_backup_keep_days=30`, `synth_usage_keep_days=90`, `log_max_mb=5` |
| Ledger | `ledger_id_chars=80`, `ledger_title_chars=2000`, `ledger_evidence_chars=1000`, `ledger_stale_after=7d`, `ledger_purge_after=30d`, `ledger_owner_word="NEEDS OWNER"` |
| Work | `work_projects=[]`, `work_file_max_bytes=262144`, `work_max_jobs=50`, `work_max_prs=200`, `work_max_trains=20`, `work_max_runs=20`, `work_line_chars=200`, `work_stale_after="auto"`, `work_job_quiet_after=900s`, `work_gone_after=86400s`, `work_finished_for=1800s`, `work_merged_for=7d`, `work_clock_slack=300s`, `work_refresh=5s` |
| Owed | `owed_page_max=200`, `owed_bulk_max=500` |
| CLI paths | `cli_path.claude`, `cli_path.codex`, `cli_path.grok`, `cli_path.muse`, `node_folder`; no configured path by default |

Zero is setting-specific: `replica_refresh_after=0` refreshes at every check; `hub_down_memo_for=0` never skips a connection attempt. Timeouts, retention, and display limits each define their own zero meaning in `khipu config`; there is no blanket unlimited rule.

## Integrations and sessions

| Command | What it does |
| --- | --- |
| `integrations` | Per-harness packs: `install [HARNESS]`, `verify`, `uninstall`, `status`. |
| `sessions` | Session capture queue: `drain`, `sweep`, `status`, `liveness`. |
| `aegis` | Older name for `sessions`. Same queue, same drain. Its `status` defaults to `--harness aegis`, where `sessions status` defaults to `all`. |
| `gateway` | `gateway token set` and `gateway token status` for the gateway bearer token. |
| `t3` | Khipu for providers inside T3 Code. `t3 antigravity add\|remove --project DIR [--dry-run]` writes or removes Khipu's `.agents/` files in one project (the same files `integrations install antigravity --project` writes), `status --project DIR` reports them, and `list` shows every project added and the runtime version. |

- Harness choices for `integrations`: `all`, `claude_code`, `cursor`, `aegis`, `codex`, `grok_build`, `grok_bot`, `antigravity`. Flags: `--dry-run`, `--home`, `--project`, `--no-verify`. There is no top-level `install` command.
- `sessions status` takes `--harness all|claude_code|cursor|codex|aegis`. `sessions liveness` exits 2 if any harness's capture is failing (its last hook run errored, its last capture failed with nothing landed since, a job has waited too long, or turns keep arriving without a capture). A harness that has never run the hook counts as healthy. `drain` and `sweep` take `--limit` and `--dry-run`.
- `gateway token set` reads the token from stdin, or from an environment variable with `--from-env [VAR]` (default `KHIPU_GATEWAY_TOKEN`). It never prints the token.

## Jobs, releases and maintenance

| Command | What it does |
| --- | --- |
| `jobs` | Scheduled LaunchAgents: `status`, `install`, `refresh`, `uninstall`. |
| `promote` | Gate a ref in the inactive live slot and make it live on a pass. |
| `nightly` | Run the nightly memory wiki job once. |
| `monthly` | Run the monthly wiki classification once. |
| `backfill` | `backfill identity` derives repo and project for episodes. `--dry-run`, `--apply`, `--limit`. |
| `project` | Project identity: backfill, alias add/list/remove, show NAME, list, merge A B --show-as NAME, keep-separate A B, separate NAME, and renames check. Grouped names read as one project; owner merge/separation decisions control grouping. |
| `hygiene` | `hygiene paths` finds and purges bad path graph nodes. Archived commitments are not re-judged. |

### jobs

Job names: `nightly`, `monthly`, `notes_watch`, `queue_drain`, `recall_daemon`, `replica_refresh`. `install` with no name schedules the five jobs other than `recall_daemon` (the default set). `refresh` with no name re-renders every installed job whose plist is stale and never installs a new one. `uninstall` with no name removes all six, including `recall_daemon`. `recall_daemon` is opt-in: `khipu jobs install recall_daemon`.

### promote

`promote [ref]` defaults to `origin/main`. It checks the ref out in the inactive live slot, runs the gate (doctor, a replay of recent recall queries from the last day, one real capture, a version check), and flips the live link on a pass. The recall replay needs at least one query logged in the last day.

- `--rollback`: flip back to the previous slot.
- `--status`: show the live commit, main, how far behind, and queued promotion.
- `--retry-queued`: retry the queued attempt once the hub answers. A promotion blocked only by an unreachable hub stays queued and retries when the hub returns.
- `--apply-migrations`: before the gate runs, list the pending database migrations with the new code, apply them with that code, and print each one applied. Without it nothing is applied, and a gate that is red only for pending migrations names them and says to use this flag. If a migration fails, the promotion stops, live is unchanged, and the output names the failed migration and says the database may be partly ahead.
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
| `khipu-owner-turn.py` | Records a prompt-submit owner turn for owner-direction checks. Invoked by `khipu-prompt-recall`; recorder failures do not fail the prompt. |
| `khipu-recall-client.py` | Asks the warm recall service for recall. |
| `khipu-delegation-hook` | Routing push before work is handed to another agent. Never denies or alters a call. |

For the MCP tools these launchers serve, see [mcp-tools.md](mcp-tools.md). For the routing record, see [routing.md](routing.md). For jobs, backups and the gateway, see [operations.md](operations.md).

Owner actions that scan or change batches return “Started; it will finish on its own.” after authorization. They run in a detached process, so closing or timing out the conversation does not stop them. Owner actions records the final outcome and counts.

### wiki

`wiki plan` reads episodes, existing active-profile vectors, topics and the queue
(if migrated). It never creates tables, embeds, queues work, reconciles files or
writes wiki pages. `--since DAYS` defaults to a 30-day catch-up proposal. Exact
slug and supersession redirects precede nearest vector matching (at most two
pages per episode). The provisional maximum cosine distance is 0.15; sample
calibration is required before live use. More than 20% missing episode vectors on any day
blocks drafting and returns a blocked plan with the count. Episodes without a
vector wait two UTC nights, then use exact matches only.

`wiki draft --sample N` uses the first planned batch for up to N distinct pages (1–60); additional batches remain undrafted. It it writes
old.md, episodes.jsonl (summary/decisions/preferences only), draft.md,
checks.json and summary.json below the jobs folder or an empty `--out` directory
outside memory_root. These are unreviewed artifacts. Mechanical checks preserve
frontmatter (except today's last_updated and seed → active), at least 70% of old
length, old lines carrying numbers/dates/paths/links verbatim or named verbatim
in a dated Was line, and the supplied Sources ids. They cannot establish factual
truth, completeness of other facts, or correctness of a Was claim.

The `wiki` models role inherits the configured `synth` route until explicitly
set with `models set --role wiki`; `--model ID` overrides the draft model only.
Login exhaustion never selects a paid fallback. `wiki_daily_pages` defaults to
60, independently of summaries; lift with `budget lift wiki --cap N` or
`config --set wiki_daily_pages N`. Draft calls consume this separate budget.
`wiki run [--max N] [--json]` runs only the wiki update, even with
`wiki_nightly` off. It uses the same lock as the complete nightly: a hand run
refuses with exit 75 while that lock is held. A page is reconciled from its
file before the provider sees it; a missing matching revision gets a baseline.
The accepted file and a `nightly-wiki` revision are saved before its queue
entries are marked done. Failed checks or calls leave those entries waiting.
At most 60 writes run per invocation and 10 new pages open per UTC day; all invocations share
the separate daily wiki call budget, including failed calls and draft calls.
`--max N` further limits one invocation. Reports count unique episode IDs
absorbed in that run, even when an episode updates two pages.

`config --set wiki_nightly true` enables the scheduled step (default false).
It runs after `capture_export`, before the outside consolidation, only on the
configured nightly sync host with its memory folder mounted. Its failure does
not stop later nightly steps. `config --set wiki_every_minutes N` sets the
periodic interval (default 60, 0 means nightly only). The existing
`replica_refresh` timer runs it when due; it also requires `wiki_nightly` and
quietly skips a held nightly lock. No extra launchd job or daytime git sync
is installed. A wiki run failure does not change the replica timer's outcome.

`wiki undo <slug>` restores the exact bytes before its latest wiki write.
`wiki undo --night YYYY-MM-DD` restores all writes on that UTC date, and
`--run ID` selects one recorded run. These commands use the same nightly lock.
They record `wiki-undo`, requeue the absorbed episodes and hold each affected
page for seven days; the hold expires automatically. An opened page moves to
`topics/_retired/` and is tombstoned, never deleted. Pages changed since their
selected write are skipped and listed. Undo refuses when exact source bytes
are unavailable; the older body-only revision history cannot restore them.

The `wiki_update` entry in `nightly-last.json` and `wiki-last.json` retain the
run ID, pages revised/opened, unique episodes absorbed, waiting queue count
and oldest age in days, refusals, calls and cap status, plus the median and
90th percentile age of topic pages returned by searches in the past 24 hours.
Hand runs and periodic runs update the same step receipt, keeping other
nightly receipts. Doctor adds one wiki line only when the switch is on and
the latest run wrote no pages while episodes remain queued.

Migrations `0053_wiki_queue` and `0054_wiki_writes` are prerequisites for live
runs. The latter retains exact before bytes separately from redacted revision
bodies. Neither planning nor running applies migrations automatically. The
assignment distance remains provisional and the switch stays off until the
scope's sample and owner review pass.
