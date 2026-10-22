# The desktop app

The Khipu desktop app is a window and a menu-bar icon over the Khipu CLI. It shows whether memory is working, lets you search and correct what was captured, and holds every owner setting. You can do each owner action in the app, without a terminal. (The same actions exist in the CLI: see [cli.md](cli.md).)

The app is built with Tauri 2 and React. It needs macOS 12 or later. The published builds are for Apple silicon. The app carries its own copy of the CLI and Python, so it does not depend on a system install.

This page describes version 0.4.15.

## Navigation

The left rail has six screens:

1. Home
2. Work
3. Recall
4. Activity
5. Harnesses
6. Settings

Two more views are not in the rail:

- **Welcome** (the first-run setup). Reach it again from Settings > Advanced > Run setup again.
- **Conflicting edits.** Reach it from Home or from Settings > Advanced.

Most screens have a Refresh button in the header.

## Home

Question it answers: "Is memory working right now?"

- **Health callouts** at the top. Each one names a problem and, where Khipu knows a fix, offers a button for it.
- **Right now.** Per harness: turns waiting for capture, queued sessions, and sessions captured today. It has a capture-now action that flags a session for capture at its next stop.
- **Plans.** Plan usage for Claude, Codex, Grok, Muse and the GLM Coding Plan. Supports one DeepSeek account, reporting measured spend from balance changes against a spend cap, with partial history labelled. See [routing.md](routing.md).
- **Routing.** One row per account with its state, where Khipu learned it, and how old that is. It also shows holds, your note, standing rules, and anything that needs attention. It has a Check now button. See [routing.md](routing.md).
- **Four tiles.**
  - Database: connected or not reachable, and the age of the offline copy.
  - Recording: whether each harness is capturing.
  - Search index: how much is indexed, and how much is catching up.
  - Backups: fresh or stale.
- **Recent captures.**

The health report behind these is the same data as `khipu doctor`. It includes recent capture summaries, paths and error text and is not redacted, so read it and remove anything private before you attach it to a support email.

## Recall

Question it answers: "What does the agent remember about this?"

A switch at the top chooses **Search** or **Graph**.

**Search** finds episodes, topics and nodes. It has three modes:

- **Best match.** The default. It mixes meaning, shared words and exact text.
- **By meaning.** Semantic only.
- **Exact words.** Literal text only. Use it for ids, error messages and exact phrases.

Filter chips narrow the results, for example by project. Open a result to read it in full.

**Graph** walks what a topic or node connects to. You choose how many hops to follow, from 1 to 4.

How search works is in [how-it-works.md](how-it-works.md).

## Work

Work brings together Owed, project ledger boards, Waiting on you, Running now and Accounts. Open a project board to read or change its ledger. Settings > Work manages tracker imports and the summary files your runners report.

### Owed

Question it answers: "What do I still owe on each project?"

Khipu keeps the follow-ups, blockers, questions and promises it finds in your sessions. Within Work, Owed groups open items by owner, with project and kind filters alongside **Open**, **Closed** and **Stale**. Select items for bulk close or reassignment, or use **Tidy this list…** to review a cleanup before applying it. For each item you can:

- mark it done,
- reopen it,
- snooze it (a Snooze until dialog offers a week, two weeks or a month).

A snoozed item does not go stale while the snooze lasts. Your own items remain open until you resolve them; **Looks done** suggests items to review, and quiet items stay visible under **Quiet for N weeks**.

## Activity

Every capture, newest first, grouped by day. Open one to read it. Two owner actions are on each episode:

- **Edit summary.** Corrects the text. Khipu re-embeds it so search follows the fix.
- **Forget.** Tombstones the episode: search and agent tools stop returning it, and Activity and recent-capture readers exclude it on a migrated hub. Its row stays in the database, and earlier backups still hold it. See [how-it-works.md](how-it-works.md#forget). A confirmation dialog comes first. (An episode opened in Recall has the same Forget button.)

## Harnesses

Question it answers: "Which agents are wired in, and is each one actually recording?"

There is one card per harness:

- Claude Code
- Cursor
- Aegis
- Codex
- Grok Bot (cloud)

Each card shows whether the integration is installed and whether captures are arriving. The local cards offer install (or reinstall), verify and remove. Remove asks for confirmation. The Claude Code card lists each Claude config home Khipu found, and Install or Remove can act on one home.

Grok Bot runs in the cloud, so its card has nothing to install here. Its button is **Probe gateway**, which checks the connection. The setup is one per repo, or once in the Cursor cloud-agent settings, and the token lives in Cursor cloud secrets, never in a repo.

If T3 Code settings exist on this Mac, a read-only T3 card also appears. It shows whether thread lookup works and how many captures were linked to a T3 thread today, and has a Search T3 captures button that opens Recall filtered to that source. T3 Code is not a separate pack. Claude accounts run inside it are covered by the Claude Code install. Khipu only reads T3's settings and thread records and never writes them.

What each pack installs is in [integrations.md](integrations.md).

## Settings

Settings has thirteen sections. Database is open first.

| Section | What it holds |
| --- | --- |
| Database | The connection state and where it is stored. Buttons: Recheck connection, connect to a server you run or another Mac (opens the setup step), and Move this memory to another database. |
| Capture & models | Choose how captures are stored, the summary models and the daily summariser budget. Keys includes Gemini, Claude API, OpenAI, OpenRouter, an optional local-endpoint key and an optional GLM Coding Plan key. |
| Accounts and routing | Manage tracked accounts, inline plan usage and sign-in, priorities, your note, standing rules, holds, reset checks, what conversations are told, job runner folders and the lane map. Review recent changes. See [routing.md](routing.md). |
| Owner actions | Review owner changes made in conversations, the app or terminal, with eligible Undo controls. Acknowledge tamper alarms and manage owner-direction settings. |
| Work | Manage ledger projects, tracker import, runner summary-file sources, Owed items hidden from Waiting on you, and Work/ledger size, timing and retention limits. |
| Projects | Review canonical and older project names, same-project questions and kept-separate pairs. Owner actions can merge names, keep them separate or separate an older name again. |
| Optional features | Switches for Search and memory, the Local recall service, Routing, and a group of options marked "Measured, not recommended". |
| Embeddings | Embedding models (profiles) with an Add a model action, libraries (folders of .txt and .md files that become a search space), and the progress of indexing jobs. |
| Data & backups | The data folder, older file locations (path overrides), the graph backup destination, Backup and Restore. |
| Another Mac | The gateway address, and Set up another Mac (Save join kit). |
| Components | App and CLI version, the database and graph builder components, and What feeds the graph. |
| Updates | Installed version and Check for updates. |
| Advanced | Run setup again, Conflicting edits, Send feedback, background jobs, Health checks and storage controls, and the raw configuration. |

### Capture & models

**How captures are stored** offers three choices:

- Files only
- Files and Khipu
- Khipu only

The choice is locked, with a note, when the `KHIPU_CAPTURE_MODE` environment variable is set. It matches `khipu config --set-capture-mode`.

The session-summary provider can be Gemini Flash (the default), Claude Haiku, OpenAI or OpenRouter with an API key, Claude on your own login (`claude -p`), ChatGPT on your own login (`codex`), or a local OpenAI-compatible endpoint. API keys go to the login Keychain. For Anthropic, OpenAI, OpenRouter and the Claude/Codex CLI providers, a failed window retries on Gemini when `synth_fallback` is `gemini` and a Gemini key is configured, or waits to retry later when fallback is off. Local providers never use the Gemini fallback. Local-provider requests are not charged against the daily synth call ceiling, even when the endpoint is remote. Key verification makes small provider checks and can send several requests: Gemini checks embeddings and generation, while other configured providers use compatible-endpoint, model-list or key-metadata checks. A passing check shows "Key works" with the model or provider name.

Session summaries send transcript text to the provider you pick. A local endpoint accepts plain HTTP only on this Mac but also accepts any HTTPS address, so it keeps text on your Mac only if you point it at one. The search index sends text to the embedding model you choose under Embeddings. By default, text that matches known secret shapes (vendor API key prefixes, private key blocks, JWTs, bearer and basic headers, credentials in URLs, and `name = value` lines whose name says secret) is masked before a transcript window is sent to the summariser and before a capture is stored. It is pattern matching: a secret in an unrecognised shape is not masked. Setting `KHIPU_REDACT_SECRETS=0` turns masking off.

### Plans and signing in again

Plan usage, tracked accounts and account sign-in live under **Settings > Accounts and routing > Accounts**, with one usage block inside each account. Use **Refresh** on Home's Plans card or **Check all now** in Accounts to read usage again. Usage includes the GLM Coding Plan. Supports one DeepSeek account, reporting measured spend against a spend cap, with partial history labelled; it does not report subscription headroom. You can add accounts (up to 32) and remove them. A Sign in again button starts the login for Claude and Codex accounts. Grok and Muse accounts say to sign in from their own CLI.

### Another Mac

Do this on the Mac that already works:

1. Type a passphrase. Save and Advertise stay disabled until you do, or until you tick **I understand: make it unencrypted**, which shows a warning that anyone with the file can read the database password and API keys.
2. Click **Save join kit** and AirDrop the `.khipujoin` file to the new Mac.
3. On the new Mac, open Welcome, choose **Join a Khipu I already have**, and import the file.

**Advertise nearby (PIN)** is an alternative on the same Wi-Fi.

Save is disabled when the database lives only on this Mac, because another Mac could not reach it. The section then offers a button to move the database to a server. The full flow is in [setup.md](setup.md).

### Data & backups

Backup zips the Mac-local data folder. Restore reads a zip or folder back into it: files with the same name are overwritten, and other files already there are kept. These are the app forms of `khipu backup-local` and `khipu import-local --replace`. (On the command line, `khipu import-local` without `--replace` skips files that already exist.) Database backups and health checks are covered in [operations.md](operations.md).

**Graph backup destination** sets where the weekly off-site copy of the graph snapshot goes (the `graph_backup_remote` setting, an rclone `remote:path`, default `r2:khipu-backups/khipu-graph`). It shows the current value and whether it comes from the default, this Mac's settings or the `KHIPU_GRAPH_BACKUP_REMOTE` environment variable, which locks the field. Save checks the value first; **Use the default** removes a saved one.

### Components

The database and the graph builder upgrade on their own, apart from the app. **What feeds the graph** lists the local folders the graph builder reads on its next run. Turning one off skips it next time and deletes nothing. Each folder with a path can opt in to **Embed images** (PNG and JPEG). You can add or remove a code folder. On a Mac that does not build the graph, the list is read-only.

### Advanced

Background jobs shows the scheduled jobs and lets you install or remove each one. The replica timer also runs daily local-storage maintenance. **Health checks** exposes disk thresholds and retention limits; **Open storage report** shows local usage. Doctor reports the offline ledger copy separately, and a failed ledger part gets ledger-only recovery with a fifteen-minute backoff while healthy memory parts stay available. Send feedback opens a form. The raw configuration is shown for support.

## First-run setup (Welcome)

Welcome has seven steps: Welcome, Database, Model, Graph, Agents, Accounts and Finish. Each step checks itself, so setup can be resumed.

- **Database.** Choose one:
  - Join a Khipu I already have (import a join kit with its passphrase, or use a nearby PIN; a kit saved unencrypted needs none)
  - Set up a new database on this Mac (needs Docker)
  - Connect to a database I already run

  A progress list shows each stage (reach, version, privileges, schema, graph, store, upkeep, and a memory round trip) as Working, Needs you, Skipped, Waiting or Checking.
- **Model.** Choose the summary provider, or Skip for now.
- **Graph.** Installs the graph builder. You can skip it and do it later in Settings > Components.
- **Agents.** Lists the harnesses found on this Mac and a button, Open Harnesses to install and verify.
- **Accounts.** Optionally selects tracked accounts, sets priority and enables telling conversations about routing. Next applies the choices; Skip for now writes nothing.
- **Finish.** Runs a health check and lists any unresolved item with one fix each. Continue anyway is always available.

Details are in [setup.md](setup.md).

## Conflicting edits

When the same topic is edited in two places, the newest edit wins and the older version is kept. This view compares every note file with the database so you can see what lost. It is a drill-in, not a rail item.

## Menu-bar icon

The app puts a full-color icon in the menu bar. Left-click shows and focuses the window. The menu has three items:

- **Show Khipu**
- **Run doctor…** (shows the window and refreshes the tooltip from `khipu doctor`)
- **Quit**

The tooltip starts as "Khipu · starting…". It then shows the doctor result (OK, WARN, NOT RECORDING, or doctor unavailable). If doctor fails it falls back to `khipu status`. When an update is waiting, the update tooltip replaces the health one.

## Updates

The app checks GitHub Releases once at launch. The check only looks. It never downloads, installs or restarts. If a newer version exists, the tray tooltip reads "Khipu · update vX available".

To install, open **Settings > Updates** and click **Check for updates**. The app downloads the build, checks its signature, installs it and relaunches. Nothing installs unless you click.

After upgrades, **What's new** presents changes since the last seen version in a Home card and a dialog, with permanent history under **Settings > Updates**.

Captures carry server-stamped local or gateway provenance when the schema supports it. Gateway forget still uses the session-prefix boundary described in [MCP tools](mcp-tools.md).
