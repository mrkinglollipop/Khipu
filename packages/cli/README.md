# Khipu CLI

The Khipu command line, the MCP server and the hook scripts. The CLI runs as a Python module: there is no installed `khipu` executable in the repo. Everything below uses `python3 -m khipu`. If you want a short name, alias it in your shell (for example `alias khipu='python3.11 -m khipu'`).

The full command reference is in [docs/cli.md](../../docs/cli.md). For a quick list of commands, run `python3 -m khipu --help`. For one command's flags, run `python3 -m khipu <command> --help`.

## Setup

```bash
export KHIPU_ROOT="/path/to/Khipu"   # the repo root (Khipu itself falls back to this checkout when unset)
export PYTHONPATH="$KHIPU_ROOT/packages/cli:$KHIPU_ROOT/.python_libs"

# One-time deps. .python_libs/ is gitignored (platform-specific wheels), so a fresh
# clone has no dependencies until this runs. Rebuild it per machine, never copy it:
python3.11 -m pip install \
  --target "$KHIPU_ROOT/.python_libs" -r "$KHIPU_ROOT/packages/cli/requirements.txt"

# One-time DSN, read from stdin and stored in the Keychain (never pass it as an argument):
printf '%s' 'postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=verify-full' | python3 -m khipu secrets --set database_url

python3 -m khipu migrate --dry-run   # show applied and pending schema migrations
python3 -m khipu migrate             # apply them
```

Most people set the database up from the desktop app instead. See [docs/setup.md](../../docs/setup.md).

## Everyday commands

```bash
python3 -m khipu doctor              # the single health command
python3 -m khipu doctor --quick      # local checks only, no hub or network
python3 -m khipu status              # row counts
python3 -m khipu activity            # recent captures
python3 -m khipu activity --show 42
python3 -m khipu revisions           # conflicting edits (last write wins)
python3 -m khipu search "khipu"                  # hybrid (default)
python3 -m khipu search "khipu" --mode literal   # exact words
python3 -m khipu search "khipu" --mode semantic  # meaning
python3 -m khipu get 42              # load an episode, a topic slug, or a library chunk
python3 -m khipu graph '<node id>' --hops 1   # graph neighbours of one node
python3 -m khipu regen-memory
python3 -m khipu reconcile           # full file-to-database sync
```

To point Khipu at a legacy file wiki, use `python3 -m khipu config --set memory_root /path/to/memory/conversations`. Run `python3 -m khipu config --help` for every key you can set.

## Command groups

| Group | Commands |
|---|---|
| Health and data | `doctor`, `status`, `activity`, `revisions`, `reconcile`, `migrate`, `snapshot`, `outbox` |
| Search and records | `search`, `get`, `graph`, `episode`, `topic`, `notes`, `owed`, `decisions`, `briefs`, `reflect`, `recall` |
| Maintenance | `regen-memory`, `backfill`, `project`, `hygiene`, `topic-graph`, `graph-sync`, `git-sync` |
| Embeddings and libraries | `embed`, `embed-backfill`, `embed-media-backfill`, `library`, `library-backfill`, `sources` |
| Models and limits | `models`, `budget`, `features`, `secrets`, `config` |
| Setup and moving | `db`, `join`, `components`, `paths`, `backup-local`, `import-local`, `graph-backup` |
| Harnesses and capture | `integrations`, `sessions` (older name `aegis`), `capture`, `gateway`, `grok-bot-config` |
| Jobs and deploy | `jobs`, `nightly`, `monthly`, `graph-build`, `promote` |
| Routing and plans | `route`, `usage` |

## Environment

| Var | Meaning |
|---|---|
| `KHIPU_DATABASE_URL` | Prefer over Keychain/file |
| `KHIPU_DSN_FILE` | The connection-string file (default: `dsn` in the data folder, `~/.config/khipu` unless moved with `khipu paths --set`) |
| `KHIPU_KEYCHAIN` | `0` disables Keychain lookup |
| `KHIPU_MIRROR` | `0` disables the file-to-database mirror |
| `KHIPU_MIRROR_EMBED` | `1` enables embed-on-mirror (off by default) |
| `KHIPU_ROOT` | Repo root for imports |
| `KHIPU_CAPTURE_MODE` | Overrides the stored capture mode |

DSN resolve order: **env, then Keychain (`Khipu`/`database_url`), then file**.

## Capture mode

Capture is driven by the Stop, PreCompact and SessionEnd hooks (`bin/khipu-stop-hook`). The capture mode decides where a capture is stored:

- `legacy`: files only.
- `dual` (default): files and the database.
- `hub`: the database is the record. By default Khipu still mirrors each capture to the legacy files; set `KHIPU_HUB_FILE_MIRROR=0` to turn the mirror off.

Change it with `python3 -m khipu config --set-capture-mode dual` or in the desktop app under Settings, Capture & models. The app control is locked when `KHIPU_CAPTURE_MODE` is set.

## What is in this folder

| Path | Contents |
|---|---|
| `khipu/` | The Python package (`python3 -m khipu` calls `khipu.cli.main`) |
| `bin/khipu-mcp` | stdio MCP launcher (server id `khipu`) |
| `bin/khipu-stop-hook` | Capture hook for Claude Code, Cursor and Codex |
| `bin/khipu-aegis-capture` | Aegis capture hook; only queues a session |
| `bin/khipu-recall-hook` | SessionStart recall push |
| `bin/khipu-prompt-recall` | Per-prompt recall push |
| `bin/khipu-recall-client.py` | Asks the warm recall service for recall |
| `bin/khipu-delegation-hook` | Routing push when work is handed to another agent |
| `launchd/` | LaunchAgent plist templates |
| `scripts/` | Helper shell scripts |
| `tests/` | The pytest suite (see [CONTRIBUTING.md](../../CONTRIBUTING.md)) |

Install the hooks and MCP entry for each agent with `python3 -m khipu integrations install <harness>`. See [docs/integrations.md](../../docs/integrations.md).
