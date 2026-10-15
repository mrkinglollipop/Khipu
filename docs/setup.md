# Setting it up

Khipu is a personal system: one database, your machines, your agents. Most people only need the desktop app, which walks you through the rest. Cloning the repo is for developers and self-hosters (see the last sections).

**Requirements.** macOS 12 or later on Apple silicon. The desktop app and its Keychain integration are Mac-only. Nothing else needs deciding in advance: where the database lives and which model to use are choices the app walks you through.

Related pages: [How it works](how-it-works.md), [Desktop app](desktop-app.md), [Integrations](integrations.md), [CLI reference](cli.md), [Operations](operations.md), [FAQ](faq.md).

## Install the desktop app

1. Download the latest `Khipu_*.dmg` from the [GitHub releases page](https://github.com/mrkinglollipop/Khipu/releases/latest).
2. Open the DMG and drag **Khipu.app** to **Applications**.
3. Launch **Khipu**. The first launch opens **Welcome**, a six-step walkthrough: Welcome, Database, Model, Graph, Agents, Finish.

Each step checks itself, so you can stop and resume later. A step ends in one of these states, shown on screen: working (a real check passed, not just "saved"), needs you (one plain-words action), or checking (the app is doing the work, so wait it out).

After setup, the left rail has six screens, one per job:

- **Home**: is memory working right now.
- **Recall**: ask what it remembers. It has a Search and Graph switch.
- **Owed**: what you still owe on each project.
- **Activity**: every session it recorded.
- **Harnesses**: which agents are wired in, and whether each is recording.
- **Settings**: ten sections (Database, Capture & models, Routing, Optional features, Embeddings, Data & backups, Another Mac, Components, Updates, Advanced).

You can reopen Welcome later from Settings, Advanced, **Run setup again**.

### Updates

The app checks GitHub Releases when it launches. If a newer version exists, the menu-bar tooltip says so. Nothing downloads or installs on its own. To install, go to Settings, Updates, **Check for updates**.

If macOS blocks a release that is not notarized, right-click **Khipu.app**, choose **Open**, and confirm once.

More detail: [Desktop app](desktop-app.md) and [`apps/desktop/README.md`](../apps/desktop/README.md).

## The Database step

The Database step offers three choices. Each one makes the schema current and schedules the background upkeep jobs: nightly (02:05), graph (02:17), monthly (the 1st, 09:00), a notes watcher, and a capture-queue drain every five minutes. The warm recall service is a sixth job and is opt-in. Details are in [Operations](operations.md).

The last stage is a memory round trip: write a test memory, find it by search, and show the time it took. The round trip is skipped if capture mode is "Files only" or no embedding key is set.

### Join a Khipu I already have

Pick this when a Mac you own already runs Khipu and you want the same memory, not a second one.

1. On the working Mac, open Settings, **Another Mac**, and choose **Save join kit...**. This writes a `.khipujoin` file. You can protect it with a passphrase.
2. Move the file to the new Mac (AirDrop works). Or use **Advertise nearby (PIN)** on the working Mac and **Find nearby Mac** on the new one, over the same Wi-Fi.
3. On the new Mac, go to Welcome, Database, **Join a Khipu I already have**, then **Import join kit file...**.

The import checks that the live episode, topic and node counts are not zero when the kit says they should be. A kit whose database address points at `localhost` is refused. A database set up on one Mac's own disk cannot be joined from another Mac, so the Save button is off for that case. Move the database to a server first (see below).

### Set up a new database on this Mac (needs Docker)

Pick this when nobody has Khipu yet. It needs [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) (or OrbStack or Colima). The app notices when Docker is installed and turns the button on.

The app uses a local Postgres image if one exists, then tries to pull the published image, then builds it from the bundled Dockerfile. The published image is a private package, so on most Macs the pull is refused and the build runs. The proof is that a backup was taken and test-restored.

This database lives on this Mac's disk only. Another Mac cannot reach it directly.

### Connect to a database I already run

Pick this when you already host a Postgres server (your own machine, a cloud VM, or a managed provider). Paste its connection string. It looks like `postgresql://user:pass@host:5432/dbname`, and your host shows it under connection details.

You need Postgres 19 or later with the `vector` extension, reachable from every Mac that will use Khipu. The proof is that the server answers, the account has enough privilege, and the schema is current.

### What the checks show

The Database step runs a pipeline of stages: reach, version, privileges, schema, graph, store, upkeep, prove, summary. Each shows as Working, Needs you, Skipped, Waiting or Checking.

## Model, Graph, Agents, Finish

**Model** chooses who writes your session summaries. Skip for now is always there. The choices are:

- Cloud Gemini (the default).
- Claude Haiku, with an API key.
- Claude on your own login (runs `claude -p`).
- ChatGPT on your own login (runs `codex`).
- A local OpenAI-compatible endpoint.

For key-based choices, the key is proven with one cheap real call when you save it. You see "Key works" with the model name, not just "saved". Keys go in the macOS Keychain. The login choices use your own CLI sign-in, and Khipu holds no credential for them. Transcript text is sent to whichever provider you pick. See [How it works](how-it-works.md) and [FAQ](faq.md).

**Graph** installs the graph builder next to the app. It can be skipped or retried, and added later from Settings, Components.

**Agents** lists the harnesses it detects (found, not installed, or installed). Claude Code, Cursor, Aegis and Codex install on this Mac. Grok Bot is a cloud harness that goes through the gateway (see the self-host section). The install buttons live in **Harnesses**, and Welcome links there with **Open Harnesses to install and verify**. Restart the harness and start a session. Its card turns green by itself once it records. Claude accounts run inside T3 Code are covered by the Claude Code install. See [Integrations](integrations.md).

**Finish** runs a health check and lists anything still unproven, each with one fix. **Continue anyway** is always available, so nothing here can leave you stuck.

## Moving your memory to another database

Go to Settings, **Database**, then **Move this memory to another database...**. Point it at the target, which can be a new local database or a server. It checks the target first and copies nothing until you confirm. After you confirm, it copies every table, verifies that the row counts match, and then switches over. The old database is left untouched.

Afterward, any other Mac needs a fresh join kit from the new database.

The CLI equivalent is `khipu db move` with `--to DSN` or `--new-local`. Add `--dry-run` to check only. See [CLI reference](cli.md).

## Developer and self-host setup

Clone the repo to work on Khipu itself, run the CLI outside the bundled app, or run the pieces the desktop app does not cover (the cloud-agent gateway).

```bash
git clone https://github.com/mrkinglollipop/Khipu.git && cd Khipu
python3.11 -m pip install --target .python_libs -r packages/cli/requirements.txt
```

`.python_libs/` is per-machine and git-ignored. The commands below assume `PYTHONPATH="packages/cli:.python_libs"`. There is no installed `khipu` executable in the repo. The CLI runs as `python3.11 -m khipu`, so alias that to `khipu` if you like.

A Dockerfile for Postgres 19 plus pgvector is in [`ops/docker/`](../ops/docker/). It is the same image the app's "set up a new database" button uses, as a last step after a local image and a pull.

```bash
# Point at your database. The password goes in the Keychain, never a file or ps output.
printf '%s' 'postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=verify-full&sslrootcert=/path/root.crt' \
  | khipu secrets --set database_url
khipu migrate                              # apply the schema; --dry-run to preview
khipu integrations install claude_code     # or cursor, codex, aegis, grok_bot, all
khipu integrations verify  claude_code     # proves it by exercising it
```

Then `khipu doctor` should be green. Anything it could not check is named under `not_configured`, which is normal on a fresh install. `khipu doctor --quick` runs all the local health checks and leaves out the hub, network and probe checks.

To run the database setup pipeline from the CLI, use `khipu db connect` (flags: `--dsn`, `--use-stored`, `--root-crt`, `--no-jobs`, `--no-prove`). `khipu db preflight --dsn DSN` runs only the checks: it does not store the connection, install jobs or run the round trip, but it may still update the server's schema and privileges.

### Build the desktop app locally

```bash
cd apps/desktop && npm install && npm run tauri dev
```

This needs Rust and the Xcode command-line tools. The app bundles the CLI from a generated, git-ignored `khipu-resources/` folder made by `apps/desktop/scripts/bundle_cli.sh`. Run that script first if the build asks for it. Release builds are covered in [`apps/desktop/README.md`](../apps/desktop/README.md).

### Cloud agents (Grok Bot, Cursor cloud): optional

Cloud agents run on short-lived VMs with no filesystem to install hooks into. They reach Khipu through an HTTPS gateway, the one component meant to face the internet, so the database never has to.

```bash
docker build -f ops/docker/Dockerfile.gateway -t khipu/gateway .
docker run -d --name khipu-gateway --network host \
  -e KHIPU_DATABASE_URL='postgresql://...' \
  -e GEMINI_API_KEY='...' \
  -e KHIPU_GATEWAY_TOKEN="$(openssl rand -hex 32)" \
  khipu/gateway
```

The gateway listens on `127.0.0.1:8787` by default (`KHIPU_GATEWAY_BIND` changes it). Put a TLS-terminating reverse proxy (Caddy or nginx, with a real certificate) in front. It refuses to start if the token is missing or shorter than 24 characters. The token is the only credential a cloud agent holds. Khipu does not check where your database lives; keep PostgreSQL on a private network and do not open its port to the internet. If you use library search through the gateway, the gateway host also needs the embedding key for each enabled library profile.

Then, on your Mac:

```bash
khipu config --set-gateway-url https://khipu.example.com   # must be https
khipu integrations install grok_bot
khipu integrations verify grok_bot
```

With no `--project`, `install grok_bot` prints the account-level config to paste once into Cursor's cloud-agent MCP settings, plus the secret name `KHIPU_GATEWAY_TOKEN`. Add that token as a secret with the same name. To wire a single repo instead, add `--project /path/to/repo`. That writes `.cursor/mcp.json` and a rule file, and the config refers to the token as `${env:KHIPU_GATEWAY_TOKEN}` without containing its value. `verify` probes the public URL, including a wrong-token check.

`khipu gateway token set` stores the bearer token for harnesses that read it from a file (Aegis uses it for search). Local harnesses (Claude Code, Cursor, Codex) talk to the database directly and do not need the gateway.

## Next

Before opening a pull request, read [`CONTRIBUTING.md`](../CONTRIBUTING.md).
