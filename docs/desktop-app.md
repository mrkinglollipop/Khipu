# The desktop app

The Khipu desktop app is a window and a menu-bar icon over the Khipu CLI. It shows whether memory is working, lets you search and correct what was captured, and holds every owner setting. You can do each owner action in the app, without a terminal. (The same actions exist in the CLI: see [cli.md](cli.md).)

The app is built with Tauri 2 and React. It needs macOS 12 or later. The published builds are for Apple silicon. The app carries its own copy of the CLI and Python, so it does not depend on a system install.

This page describes version 0.4.15.

## Navigation

The left rail has six screens:

1. Home
2. Recall
3. Owed
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
- **Plans.** Headroom left on each subscription CLI account (Claude, Codex, Grok, Muse). See [routing.md](routing.md).
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

## Owed

Question it answers: "What do I still owe on each project?"

Khipu keeps the follow-ups, blockers, questions and promises it finds in your sessions. Owed lists them under three filters: **Open**, **Closed** and **Stale**. For each one you can:

- mark it done,
- reopen it,
- snooze it (a Snooze until dialog offers a week, two weeks or a month).

A snoozed item does not go stale while the snooze lasts.

## Activity

Every capture, newest first, grouped by day. Open one to read it. Two owner actions are on each episode:

- **Edit summary.** Corrects the text. Khipu re-embeds it so search follows the fix.
- **Forget.** Removes it from memory. A confirmation dialog comes first. (An episode opened in Recall has the same Forget button.)

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

Settings has ten sections. Database is open first.

| Section | What it holds |
| --- | --- |
| Database | The connection state and where it is stored. Buttons: Recheck connection, connect to a server you run or another Mac (opens the setup step), and Move this memory to another database. |
| Capture & models | The capture rule (after 5 turns or 20 minutes, shown read-only). How captures are stored. Models, including the session-summary provider. The summariser daily budget. The Plans card. Keys (Gemini, and an optional key for a local endpoint). |
| Routing | Accounts, your note, standing rules, holds, reset checks, what conversations are told, job runner folders, the lane map, and recent changes. Described in [routing.md](routing.md). |
| Optional features | Switches for Search and memory, the Local recall service, Routing, and a group of options marked "Measured, not recommended". |
| Embeddings | Embedding models (profiles) with an Add a model action, libraries (folders of .txt and .md files that become a search space), and the progress of indexing jobs. |
| Data & backups | The data folder, older file locations (path overrides), Backup and Restore. |
| Another Mac | The gateway address, and Set up another Mac (Save join kit). |
| Components | App and CLI version, the database and graph builder components, and What feeds the graph. |
| Updates | Installed version and Check for updates. |
| Advanced | Run setup again, Conflicting edits, Send feedback, background jobs, and the raw configuration. |

### Capture & models

**How captures are stored** offers three choices:

- Files only
- Files and Khipu
- Khipu only

The choice is locked, with a note, when the `KHIPU_CAPTURE_MODE` environment variable is set. It matches `khipu config --set-capture-mode`.

The session-summary provider can be Gemini Flash (the default), Claude Haiku, OpenAI or OpenRouter with an API key, Claude on your own login (`claude -p`), ChatGPT on your own login (`codex`), or a local OpenAI-compatible endpoint. API keys go to the login Keychain. For a provider other than Gemini, a setting chooses what happens if it fails: retry that window on Gemini Flash, or wait and retry later. A key check makes one cheap call and shows "Key works" with the model name when it passes.

Session summaries send transcript text to the provider you pick (a local endpoint keeps it on your Mac). The search index sends text to the embedding model you choose under Embeddings. By default, text that matches known secret shapes (vendor API key prefixes, private key blocks, JWTs, bearer and basic headers, credentials in URLs, and `name = value` lines whose name says secret) is masked before a transcript window is sent to the summariser and before a capture is stored. It is pattern matching: a secret in an unrecognised shape is not masked. Setting `KHIPU_REDACT_SECRETS=0` turns masking off.

### Plans and signing in again

The Plans card lists subscription accounts. **Probe now** reads their headroom again. You can add accounts (up to 32) and remove them. A Sign in again button starts the login for Claude and Codex accounts. Grok and Muse accounts say to sign in from their own CLI.

### Another Mac

Do this on the Mac that already works:

1. Optionally type a passphrase.
2. Click **Save join kit** and AirDrop the `.khipujoin` file to the new Mac.
3. On the new Mac, open Welcome, choose **Join a Khipu I already have**, and import the file.

**Advertise nearby (PIN)** is an alternative on the same Wi-Fi.

Save is disabled when the database lives only on this Mac, because another Mac could not reach it. The section then offers a button to move the database to a server. The full flow is in [setup.md](setup.md).

### Data & backups

Backup zips the Mac-local data folder. Restore reads a zip or folder back into it: files with the same name are overwritten, and other files already there are kept. These are the app forms of `khipu backup-local` and `khipu import-local --replace`. (On the command line, `khipu import-local` without `--replace` skips files that already exist.) Database backups and health checks are covered in [operations.md](operations.md).

### Components

The database and the graph builder upgrade on their own, apart from the app. **What feeds the graph** lists the local folders the graph builder reads on its next run. Turning one off skips it next time and deletes nothing. Each folder with a path can opt in to **Embed images** (PNG and JPEG). You can add or remove a code folder. On a Mac that does not build the graph, the list is read-only.

### Advanced

Background jobs shows the scheduled jobs and lets you install or remove each one. Send feedback opens a form. The raw configuration is shown for support.

## First-run setup (Welcome)

Welcome has six steps: Welcome, Database, Model, Graph, Agents and Finish. Each step checks itself, so setup can be resumed.

- **Database.** Choose one:
  - Join a Khipu I already have (import a join kit, with an optional passphrase or nearby PIN)
  - Set up a new database on this Mac (needs Docker)
  - Connect to a database I already run

  A progress list shows each stage (reach, version, privileges, schema, graph, store, upkeep, and a memory round trip) as Working, Needs you, Skipped, Waiting or Checking.
- **Model.** Choose the summary provider, or Skip for now.
- **Graph.** Installs the graph builder. You can skip it and do it later in Settings > Components.
- **Agents.** Lists the harnesses found on this Mac and a button, Open Harnesses to install and verify.
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

After an upgrade, the app may show a one-time notice for that version.

## Planned

A Work view, a settings clarity pass and gateway provenance are planned. They are not in this version.
