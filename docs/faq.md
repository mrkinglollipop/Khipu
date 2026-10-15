# FAQ

Short answers to the questions people ask most. Each links to the page with the detail.

## Privacy and data

**Does Khipu send my code or conversations anywhere?**
Only to your own PostgreSQL server and the model providers you configure.

- Summaries: transcript windows go to the summariser provider you pick, in parts of at most 14,000 characters. The default is Google Gemini. You can switch to Anthropic, OpenAI, OpenRouter, or a local OpenAI-compatible endpoint. You can also use the `claude` or `codex` CLI on your own login, in which case Khipu holds no credential.
- Embeddings: memory chunks and search queries go to the embedding provider of the active profile. That can be Google Gemini, Voyage, or an OpenAI-compatible endpoint, local or remote.

Nothing goes to the maintainer. There is no telemetry.

**Is my memory locked in?**
No. It is ordinary tables in your own PostgreSQL, such as `episodes`, `topics`, `nodes` and `edges`. `khipu regen-memory` can write topics back out as markdown (a `MEMORY.from-khipu.md` file) at any time. `khipu backup-local` zips the Mac-local data folder, and `khipu import-local` restores it.

**Can I delete something Khipu remembered?**
Yes. `khipu episode forget ID` removes an episode and its vectors. It also closes the commitments the episode opened, retracts its decisions, and updates the local offline copy. `khipu episode edit ID --summary "..."` corrects a summary and re-embeds it. `khipu topic purge SLUG --yes` hard-deletes a topic that is already tombstoned. In the desktop app, open a capture under Activity to correct or forget it. Agents can forget through the `forget` MCP tool. See [mcp-tools.md](mcp-tools.md).

## Database

**Do I have to run PostgreSQL 19? It is a beta.**
Yes, for now. SQL/PGQ property graphs arrived in 19 and the graph layer uses them. The Database step checks for version 19 or later and for the `vector` extension. For a new local database, the app uses a local image if you have one, then tries to pull the published image, and builds from the Dockerfile in `ops/docker/` if the pull fails (the published image is a private package, so the pull is often refused). See [setup.md](setup.md).

**Why is the database "private network only"? Can I just open a port?**
Please do not. Khipu assumes the database is reachable only from your own machines (Tailscale, WireGuard, an SSH tunnel). The gateway is the only part meant to face the internet, and it exists so the database never has to. See [operations.md](operations.md).

**Can I move my memory to another database?**
Yes. Use `khipu db move`, or Settings > Database > "Move this memory to another database...". It copies the tables, checks the counts, and only then switches over. The source is never written. See [setup.md](setup.md).

## Models and cost

**Can I use a model other than Gemini?**
Yes, for both jobs.

- Summaries: choose Gemini (the default, `gemini-2.5-flash`), Anthropic, OpenAI, OpenRouter, a local OpenAI-compatible endpoint, or the `claude` or `codex` CLI on your own login. Set it in Settings > Capture & models, in the Model step of first-run setup, or with `khipu models set --role synth`.
- Embeddings (search vectors): the first-run setup offers Gemini Embedding 2 at 768 dimensions. You can add Voyage or OpenAI-compatible profiles with `khipu embed profiles add`, or in Settings > Embeddings. You can also skip embeddings until you set one up. Switching later creates a new profile and a re-embed job. A profile becomes active only when its coverage is complete, unless you force it.
- Images: PNG and JPEG vectors are opt-in per source (`khipu sources set-embed-media`, then `khipu embed-media-backfill`). Captioning images is not built.

**How much does capture cost?**
A capture is due after five user turns or twenty minutes, and always at compaction or session end. Each capture makes one summariser call per part. Most captures are a single part. A window longer than 14,000 characters is split into several parts, and each part is summarised on its own. A window under 200 characters is never sent. Embeddings are keyed by content hash, so unchanged text is not re-embedded.

Daily call ceilings cap the spend. When a ceiling is hit, work waits and nothing is dropped. `khipu budget status` shows the ceilings and `khipu budget lift` raises them. See [how-it-works.md](how-it-works.md).

## Recall

**Does an agent have to ask for memory?**
Not always. Claude Code and Codex get memory pushed in two ways. At session start they receive a recall rule and a slice for the current project. On each prompt they receive up to three prior-work hits, capped at 600 characters, with no model call. Cursor gets only the session-start push and a project rule. Aegis gets no push, so it uses the `khipu_status` and `khipu_owed` tools. Agents can always search on their own. See [integrations.md](integrations.md).

**How do I search?**
`khipu search "why did we pick pgvector"` runs a hybrid search by default. It fuses meaning, token overlap and exact text. Use `--mode literal` for exact strings, ids and error messages. Use `--mode semantic` for meaning only. `--semantic` still works but is deprecated. You can filter with `--kind`, `--project`, `--since`, `--until` and `--source`. In the app, use Recall, which has a Search and Graph switch. See [cli.md](cli.md).

## Offline and failures

**What happens when I am offline, or the database is down?**
Two things protect you.

- Session capture jobs wait in a local queue until they are summarised. Captures whose database write fails go to a local outbox and replay when the database is back. `khipu doctor` stays red while the outbox has anything pending or dead, so a silent backlog cannot build up.
- Reads fall back to a local offline copy of memory, `hub_snapshot.sqlite`. The nightly job rebuilds it, and `khipu snapshot refresh` rebuilds it on demand. Captures keep it current in between. Results from a stale copy are tagged `stale: true`. Keyword search works offline. Semantic search works offline only if the embedding provider is local and matches the copy's active profile. The copy never holds writes. Writers still use PostgreSQL or the outbox. The copy does not include libraries or commitments.

**Something is red in `khipu doctor`. What now?**
Each check names what it wants. `khipu doctor` prints every `*_ok` key, so read the red one and the payload next to it (`drift`, `outbox`, `capture_liveness`, `git_sync`, `backup` and others). `not_configured` is not red: it lists checks that do not apply on this machine. Plan usage and routing warnings never turn doctor red. `khipu doctor --quick` runs only local checks, with no database or network, and `--probe` runs a fresh end-to-end recall test. See [operations.md](operations.md).

**Why does the backup check go red?**
It needs a successful `pg_dump` or WAL-G backup event within 36 hours, and a restore drill that passed. For a local Docker database, setup records one dump and one drill. Nothing in this repo re-runs them on a schedule, so the check can go red a day and a half later. Scheduling WAL-G is up to you. See [operations.md](operations.md).

## Agents and the gateway

**Which agents does Khipu support?**
Claude Code, Cursor, Codex and Aegis run on your Mac. Grok Bot runs in the cloud. Install with `khipu integrations install <harness>`, where the harness is `claude_code`, `cursor`, `aegis`, `codex`, `grok_bot` or `all`. Claude accounts run inside T3 Code are wired by the Claude Code install, one set of hooks per Claude home. A pack for stock Grok Build is planned, not built. See [integrations.md](integrations.md).

**Do I need the gateway?**
Only for some agents.

- Cloud agents (Grok Bot, Cursor cloud agents): yes. It is the only way in.
- Claude Code, Cursor and Codex on your Mac: no. They talk to the database directly over your private network.
- Aegis: its sandbox cannot reach PostgreSQL or the Keychain. Its capture hook only queues. A drain outside the sandbox writes to the database, and Aegis reads memory through the gateway. It needs a bearer token from `KHIPU_GATEWAY_TOKEN` or the token file. Once `gateway_url` is set, run `khipu gateway token set` once to stage it. `khipu integrations verify aegis` names the fix if the token is missing or refused.

If you use neither Aegis nor a cloud agent, skip the gateway. See [operations.md](operations.md).

**What do the MCP tools cover?**
There are 17: search, get, graph, status, capture, owed, owed update, forget, decisions, decisions update, brief, reflect, usage, route, route report, route rule and route priority. Brief and reflect are off by default and answer `available: false` until you turn them on with `khipu features`. Usage and routing tools work only on your Mac, not through the gateway. See [mcp-tools.md](mcp-tools.md).

**Does Khipu route my work between accounts?**
No. It reports each account's state and plan headroom, and the choice stays with you. See [routing.md](routing.md).

## Desktop app and platforms

**Does the app update itself?**
It checks GitHub Releases at launch and flags a new version in the menu-bar tooltip. It never downloads or installs on its own. To install, go to Settings > Updates > Check for updates. See [desktop-app.md](desktop-app.md).

**Can I run this on Linux or Windows?**
The desktop app is macOS only (macOS 12 or later, Apple silicon), and so is the Keychain integration. The CLI is plain Python, but it has not been tested on Linux or Windows. If you try, set `KHIPU_KEYCHAIN=0` and pass settings through the environment, for example `KHIPU_DATABASE_URL`, instead of the Keychain. Scheduled jobs use macOS LaunchAgents.

**How do I change capture mode?**
Capture mode is `legacy`, `dual` or `hub`, and `dual` is the default. Change it in Settings > Capture & models ("Files only", "Files and Khipu", "Khipu only"), or run `khipu config --set-capture-mode`. The app control is locked when `KHIPU_CAPTURE_MODE` is set in the environment.
