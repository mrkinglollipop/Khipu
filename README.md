# Khipu

![Khipu icon](apps/desktop/src/assets/khipu-icon.png)

**Khipu gives AI coding agents a memory that outlives the session.**

A Mac app, a Python CLI, and a set of harness integrations that capture what
happened in an agent session, store it in PostgreSQL as searchable prose plus a
knowledge graph, and hand it back the next time it's relevant — on any machine.

The name comes from Andean knotted-cord records (*khipu*): memory encoded as
structure, not as a pile of chat transcripts.

---

## In short

Every coding agent forgets. Close the window and the decisions, the constraints,
the three approaches you already ruled out — all gone. The usual workaround is a
pile of markdown files and hand-rolled hooks, which works until you own a second
computer.

Khipu replaces that with one product and one database.

```mermaid
flowchart LR
  A["Agent session<br/>ends or compacts"] --> B["Hook fires"]
  B --> C["Summarise<br/>the window"]
  C --> D[("PostgreSQL")]
  D --> E["Next session<br/>searches it"]
  E --> A

  classDef s fill:#f4ebe3,stroke:#8b6914,color:#1a1a1a
  classDef d fill:#c45c3e,stroke:#8f3d28,color:#ffffff
  class A,B,C,E s
  class D d
```

Three things make it work:

**Capture is automatic.** A hook in each agent harness watches the session. When
enough has happened — five user turns, or twenty minutes — it summarises the
window into an *episode* and writes it. It always captures before a compaction
or a session end, so nothing is lost to a context window filling up.

**Storage is central and shared.** One PostgreSQL instance holds everything.
Every Mac reads the same database, so memory is not trapped on whichever laptop
happened to be open.

**Recall is on demand.** Agents search when it would change their answer —
by keyword, by meaning, or by walking the graph of how things connect.

---

## How it works, in depth

### 1. Capture — how a session becomes memory

Each harness gets a small hook script. It runs on every turn but does almost
nothing most of the time; its only job is to decide whether enough has
accumulated to be worth a model call.

```mermaid
sequenceDiagram
    participant A as Agent harness
    participant H as Khipu hook
    participant S as State file
    participant G as Gemini
    participant PG as PostgreSQL

    A->>H: turn ends
    H->>S: pending_turns += 1
    alt fewer than 5 turns and under 20 min
        H-->>A: exit 0, nothing written
    else threshold reached, or compact / session end
        H->>H: read transcript window (tail, max 14k chars)
        H->>G: summarise
        G-->>H: summary, topics, decisions, preferences
        H->>PG: INSERT episode + upsert topics
        H->>S: reset counters, stamp heartbeat
    end
```

The window is capped at 14,000 characters so a long session doesn't turn into an
expensive call, and a window under 200 characters is never worth summarising at
all. Summarisation uses `gemini-2.5-flash`.

**If the write fails, it is not dropped.** The episode goes to a local outbox
and replays later. `doctor` reports red for as long as anything is pending, so a
silent backlog is not possible.

**The hook stamps a heartbeat every time it runs.** That is what makes the
liveness check honest: Khipu can tell the difference between *the hook is
broken* and *you haven't used that harness lately*, because a claim about what a
component is doing has to be gated on evidence that it actually ran.

### 2. Storage — what actually gets written

```mermaid
erDiagram
    episodes {
        bigserial id
        timestamptz ts
        text session_id
        text summary
        jsonb topics
        jsonb decisions
        jsonb preferences
    }
    topics {
        text slug PK
        text title
        text body
        text status
        text content_hash
    }
    topic_revisions {
        text slug FK
        timestamptz revised_at
        text body
    }
    nodes {
        text id PK
        text type
        text name
        jsonb payload
    }
    edges {
        text src FK
        text dst FK
        text type
        float weight
    }
    memory_embeddings {
        text profile FK
        text kind
        text ref
        vector embedding
    }

    topics ||--o{ topic_revisions : "versioned by"
    nodes  ||--o{ edges : "source of"
    nodes  ||--o{ edges : "target of"
    episodes ||--o{ memory_embeddings : "embedded as"
    topics   ||--o{ memory_embeddings : "embedded as"
```

Two shapes of memory, deliberately:

| | What it is | Written by |
|---|---|---|
| **Episodes** | Append-only. What happened in one session window, with the decisions and preferences pulled out. | Capture hooks |
| **Topics** | A living page per subject, rewritten as understanding improves. Every version kept in `topic_revisions`. | Capture, plus edits |

Episodes are history and never change. Topics are the current best understanding
and change constantly — which is why they carry a `content_hash` and a full
revision trail.

The **graph** (`nodes` / `edges`) sits alongside as a PostgreSQL 19 SQL/PGQ
property graph named `alzy_graph`. One-hop queries use native `MATCH`; anything
deeper falls back to a recursive CTE in the CLI.

### 3. Recall — three ways to ask

```bash
khipu search "why did we pick pgvector"              # keyword
khipu search "why did we pick pgvector" --semantic   # meaning
khipu graph <node-id> --hops 2                       # connections
```

**Keyword** search is token-coverage `ILIKE` across topic bodies, episode
summaries plus capture extract fields (topics, decisions, preferences, people),
and node names — a multi-word query ranks by how many tokens hit, not whether
the whole phrase appears as one substring. It queries each kind separately
with its own ordering, so a flood of matching episodes cannot starve out the
one topic page you actually wanted.

**Semantic** search embeds the query with the **active** embedding profile
(`gemini-embedding-2@768`; `gemini-embedding-001` @ 768 retained as inactive
rollback), oversamples cosine neighbors, then fuses that order with query-term
overlap over the same embedded text (summary + extract), not the teaser
snippet. Reciprocal-rank fusion still prefers hits that name the question.
Embeddings are keyed by content hash, so re-embedding only touches text that
actually changed.

**Graph** traversal expands wiki/path/graphify nodes. Digit ids from search
are episodes: the neighborhood is that capture's topic slugs, not a node
named `9320`.

Agents reach these through the MCP tools `khipu_search`, `khipu_get`,
`khipu_graph`, `khipu_status`, and `khipu_capture`. Search returns
word-boundary teasers; `khipu_get` loads the full episode, topic, or media
(path/sha256/mime). Claude Code SessionStart injects the recall rule plus a
small cwd-scoped (else recents) slice so memory is pushed, not only pulled.
Cursor keeps the project `.cursor/rules/khipu.mdc` pull path and, when
`khipu install cursor` is applied, also appends a `sessionStart` hook that
emits Cursor's `additional_context` (not Claude's nested `additionalContext`)
with a longer timeout so a hub PG slice can finish — without replacing the
harness `session_start.sh` entry.

### 4. Where it runs

```mermaid
flowchart LR
  subgraph harnesses ["Agent harnesses"]
    direction TB
    CC["Claude Code"]
    CU["Cursor"]
    CX["Codex"]
    AE["Aegis"]
  end

  subgraph khipu ["Khipu — on your Mac"]
    direction TB
    HOOK["Capture hooks"]
    CLI["khipu CLI"]
    APP["Desktop app"]
    OUT["Outbox"]
  end

  subgraph server ["Private server"]
    direction TB
    GW["HTTPS gateway<br/>token · rate limit"]
    PG[("PostgreSQL 19<br/>pgvector · SQL/PGQ")]
    BK["WAL-G backups<br/>+ restore drills"]
  end

  GB["Grok Bot<br/>(cloud, no filesystem)"]

  CC --> HOOK
  CU --> HOOK
  CX --> HOOK
  AE --> HOOK
  APP --> CLI
  HOOK --> CLI
  CLI --> PG
  CLI -. "write failed" .-> OUT
  OUT -. replay .-> PG
  GB -- "MCP over HTTPS" --> GW
  GW --> PG
  PG --> BK

  classDef h fill:#f4ebe3,stroke:#8b6914,color:#1a1a1a
  classDef k fill:#e8f2f0,stroke:#2a6f6a,color:#1a1a1a
  classDef d fill:#c45c3e,stroke:#8f3d28,color:#ffffff
  classDef b fill:#f7f3ee,stroke:#6b5a4e,color:#1a1a1a
  class CC,CU,CX,AE,GB h
  class HOOK,CLI,APP,OUT k
  class PG,GW d
  class BK b
```

Five harnesses are supported. Four run locally and talk to PostgreSQL directly
over a private network. **Grok Bot is the exception** — it runs on an ephemeral
cloud VM with no filesystem to install hooks into, so it reaches Khipu through
an HTTPS MCP gateway guarded by a bearer token. The database is never exposed
publicly; the gateway is the only door, and it holds the token, the rate limit,
and a cap on JSON-RPC batch size.

`khipu integrations install <harness>` writes the config for a harness;
`khipu integrations verify <harness>` proves it works by actually exercising it,
rather than by checking that a file exists.

### 5. Nothing is cut over irreversibly

Khipu replaced a working system, so the migration was built to be reversible at
every step. `capture_mode` is a setting, not a pipeline:

```mermaid
stateDiagram-v2
  direction LR

  [*] --> dual: migration default

  state "legacy — old writers only" as legacy
  state "dual — legacy + mirror to PG" as dual
  state "hub — Khipu writes + outbox" as hub

  dual --> hub: Khipu writes proven
  hub --> dual: roll back default
  dual --> legacy: full roll back
  legacy --> dual: resume migration
```

| Mode | Behaviour |
|---|---|
| `legacy` | Existing capture scripts only — full rollback |
| `dual` | Legacy capture continues and mirrors into PostgreSQL — the migration default |
| `hub` | Khipu owns writes, with the outbox behind it |

Reads moved to PostgreSQL first. Writes stayed on the proven path until capture,
outbox, and parity checks were all demonstrably working. Flipping the default to
`hub` deletes nothing.

### 6. Keeping the copies honest

Memory exists as files *and* as database rows during the migration, which means
they can disagree. Three mechanisms stop that from going unnoticed:

| Command | What it checks |
|---|---|
| `khipu reconcile` | Sweeps files into PostgreSQL. Idempotent — upserts on `(ts, md5(summary))`, so re-running never duplicates |
| `khipu sources` | List, enable, or disable graph membership roots; writes `graph_sources.resolved.json` for graphify |
| `khipu doctor` | Drift between files and rows, directionally: every file episode must exist in PostgreSQL |
| `khipu revisions` | Conflicting topic edits across machines |

Deletions are tombstones (`topics.deleted_at`), never silent drops, and a purge
requires explicit confirmation.

### 7. Operations

`khipu doctor` is the single health command. Checks that AND into `ok` must
pass; `graph_offsite_ok` is visible but does not keep the Mac red when the
Matt-owned `r2` rclone remote is missing:

| Check | Red when |
|---|---|
| `backup_ok` | Postgres WAL-G / pg_dump stale or restore drill failed |
| `graph_backup_ok` | Latest local graph snapshot is missing, empty, or older than 36h (producer only) |
| `graph_offsite_ok` | Weekly R2 copy of the latest snapshot is missing or stale (producer only; blocked until `r2` remote exists) |
| `drift_ok` | A file episode has no row in PostgreSQL |
| `graph_drift_ok` | Graph nodes and edges disagree with their source |
| `outbox_ok` | A write is queued and hasn't replayed |
| `capture_liveness_ok` | A hook is running but never deciding a capture is due |
| `git_sync_ok` | The memory repo hasn't synced since the last nightly |
| `dsn_file_ok` | The on-disk connection string points at a certificate that doesn't exist |

Backups run nightly to object storage via WAL-G, with continuous WAL archiving
and **automated restore drills** — a backup that has never been restored is not
a backup. Local (Radio A) dump/restore uses `docker exec` inside the Postgres
container; set `KHIPU_PG_DUMP` / `KHIPU_PG_RESTORE` to use host binaries instead.

---

## Repo layout

| Path | What it is |
|---|---|
| [`packages/cli/`](packages/cli/) | Python CLI and library — capture, search, graph, doctor, migrate, mirror, gateway, MCP server |
| [`apps/desktop/`](apps/desktop/) | Tauri 2 + React desktop app |
| [`ops/migrations/`](ops/migrations/) | Database schema, applied by `khipu migrate` |
| [`ops/docker/`](ops/docker/) | PostgreSQL 19 + pgvector image recipe |

Operational runbooks, deployment scripts, and design notes for the maintainer's
own installation live in a private companion repository — they describe one
person's servers, not the product.

## Status

| Phase | State |
|---|---|
| **P1** PostgreSQL, ETL, mirror, CLI, Tauri shell | Shipped |
| **P2** Truthful signals, graph hop symmetry, idempotent reconcile, tombstones | Shipped |
| **P3** PG 19, dual-write capture, vectors and semantic search, five harness packs, recall rules | Shipped |
| **Soak** ≥ 7 days with two Macs on the same database | In progress |
| **P4** Model roles, embedding profiles, corpus picker (Settings + `khipu sources`), selective vision | Partial — Settings Models + corpus picker shipped; **native** PNG/JPEG ingest into `gemini-embedding-2@768` (per-source `embed_media`, `khipu embed-media-backfill`) shipped; embed-profiles UI, Firecrawl, caption/`models.vision` still Planned |

## Setting it up

Khipu is a personal system: one database, your machines, your agents. On a new
Mac, install the desktop app first — cloning the repo is only for developers.

**Requirements.** macOS on Apple silicon (the desktop app and Keychain
integration are Mac-only), **PostgreSQL 19 with pgvector** (local Docker or
your own server — SQL/PGQ property graphs need 19), and optionally Gemini or a
local model for summaries and search. The in-app Welcome flow covers Postgres,
models, Graphify, and agent wiring.

### Install the desktop app (recommended)

1. Download **`Khipu_0.3.14_aarch64.dmg`** from the
   [v0.3.14 release](https://github.com/mrkinglollipop/Khipu/releases/tag/v0.3.14)
   (GitHub `/releases/latest` now points here), or use
   [kinglollipop.com/khipu/download](https://kinglollipop.com/khipu/download).
2. Open the DMG and drag **Khipu.app** to **Applications**.
3. Launch **Khipu** from Applications and complete Welcome (database, model,
   graph engine, integrations).

The app **updates itself** from GitHub Releases using a signed
**`Khipu.app.tar.gz`** — not the DMG. Postgres / pgvector / Graphify version
compat is fetched from
[`mrkinglollipop/khipu-compat`](https://github.com/mrkinglollipop/khipu-compat).

**Gatekeeper:** the **v0.3.0** DMG is **notarized Developer ID** (stapled)
once that tag is published. Older releases (e.g. 0.2.9) were Developer ID
without notarization — if macOS blocks those, right-click **Khipu.app** →
**Open** once. See
[`apps/desktop/README.md`](apps/desktop/README.md).

### Developer setup

Clone the repo if you are hacking on Khipu itself or running the CLI outside the
bundled app:

```bash
git clone https://github.com/mrkinglollipop/Khipu.git && cd Khipu
python3.11 -m pip install --target .python_libs -r packages/cli/requirements.txt
```

`.python_libs/` is per-machine (compiled wheels) and git-ignored. Everything
below assumes `PYTHONPATH="packages/cli:.python_libs"` and `python3.11 -m khipu`;
put a `khipu` alias in your shell if you like.

**1. Point it at your database.** The connection string goes in the login
Keychain (the password never touches a config file or `ps` output):

```bash
printf '%s' 'postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=verify-full&sslrootcert=/path/root.crt' | khipu secrets --set database_url
```

Run Postgres somewhere only your machines can reach (a Tailscale network works
well); never on a public port. A Dockerfile for PG 19 + pgvector is in
[`ops/docker/`](ops/docker/).

**2. Apply the schema.**

```bash
khipu migrate            # --dry-run to see what would change
```

**3. Give it a model.** Session summaries and semantic search can use cloud
Gemini, a local OpenAI-compatible endpoint, or be deferred — capture queues
safely until credentials exist. Paste keys in the app (Settings → Secrets or
Welcome → Model), or:

```bash
printf '%s' 'YOUR-GEMINI-KEY' | khipu secrets --set gemini_api_key
```

Gemini is the convenient default, not a requirement.

**4. Wire in your agents.** One command per harness writes the hook and MCP
config; `verify` proves it works by exercising it, not by checking a file exists.

```bash
khipu integrations install claude_code    # or cursor, codex, aegis
khipu integrations verify  claude_code
```

Then `khipu doctor` should be green. It names anything it could not check under
`not_configured` — on a fresh install that is the legacy file-wiki checks, which
only apply if you are migrating from a pile of markdown (see `khipu config`).

**Build the desktop app locally:** `cd apps/desktop && npm install &&
npm run tauri dev` (needs Rust and Xcode command-line tools). Release builds:
[`apps/desktop/README.md`](apps/desktop/README.md).

**Cloud agents (Grok Bot / Cursor cloud) — optional.** These run on ephemeral
VMs with no filesystem to install hooks into, so they reach Khipu through the
HTTPS gateway instead. This is the one component built to face the internet, and
it exists so the database never has to. Set it up once, next to the database:

```bash
docker build -f ops/docker/Dockerfile.gateway -t khipu/gateway .
docker run -d --name khipu-gateway --network host \
  -e KHIPU_DATABASE_URL='postgresql://…' \
  -e GEMINI_API_KEY='…' \
  -e KHIPU_GATEWAY_TOKEN="$(openssl rand -hex 32)" \
  khipu/gateway
```

It listens on `127.0.0.1:8787` only; put a TLS-terminating reverse proxy in
front (Caddy or nginx with a real certificate) and give it a hostname. The token
must be at least 24 characters — the gateway refuses to start otherwise — and it
is the only credential a cloud agent ever holds; Postgres stays private. Then,
on your Mac:

```bash
khipu config --set-gateway-url https://khipu.example.com
khipu integrations install grok_bot --project /path/to/repo   # writes .cursor/mcp.json + a rule
khipu integrations verify grok_bot                            # probes the public URL, wrong-token negative included
```

Add the same token as a secret named `KHIPU_GATEWAY_TOKEN` in Cursor's cloud
agent settings; the repo's `.cursor/mcp.json` references it as
`${env:KHIPU_GATEWAY_TOKEN}` and never contains the value. Local harnesses do
not use the gateway at all.

**A second Mac** joins the same hub — it does not create a new empty database.

1. On the Mac that already works: Settings → **Set up another Mac** →
   **Save join kit…** and AirDrop the `.khipujoin` file. Passphrase is optional.
   Nearby PIN is optional (same Wi‑Fi).
2. On the new Mac: Welcome → **Join existing Khipu** → **Import join kit file…**.
   Only enter a passphrase if you set one when saving. PIN is not required.
3. Guest / client-isolated Wi‑Fi will fail nearby join — use the file.

That rewrites the TLS cert path for this machine, stores the DSN in Keychain,
and verifies live episode counts against the kit. Localhost Postgres cannot be
joined from another computer — the hub must be reachable (Tailscale, WireGuard,
SSH tunnel, or a shared server). After join, optionally add folders on this Mac
so Graphify feeds only those sources into the shared graph (scoped delete — it
will not purge the other Mac’s nodes).

See [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request.

## FAQ

**Does Khipu send my code or conversations anywhere?**
Only to two places you control: your own PostgreSQL server, and Google's Gemini
API for summarisation and embeddings (the transcript window, capped at 14,000
characters, is what gets summarised). Nothing goes to the maintainer. There is
no telemetry.

**Do I have to run PostgreSQL 19? It's a beta.**
Yes, for now — SQL/PGQ property graphs arrived in 19 and the graph layer uses
them natively. A Dockerfile that builds 19 + pgvector is in `ops/docker/`.

**Why is the database "private network only"? Can I just open a port?**
Please don't. Khipu's threat model is that the database is reachable only from
your machines (Tailscale, WireGuard, an SSH tunnel). The gateway is the only
component designed to face the internet, and it exists precisely so the
database never has to.

**Can I use a model other than Gemini?**
Yes. **Synth** (session summaries) supports cloud Gemini or a local
OpenAI-compatible endpoint (Settings → Models or Welcome → Model). **Embed**
(search vectors) defaults to Gemini Embedding 2 @768 when you choose cloud
embed at first-run; you can skip embed until a profile is configured. Switching
embed later creates a new profile and re-embed job. **Native** image vectors
(PNG/JPEG into the same `gemini-embedding-2@768` profile) are opt-in per source
via `embed_media` + `khipu embed-media-backfill`. Caption / `models.vision`
ingest is still not shipped (picker defaults to off).

**What happens when I'm offline, or the database is down?**
Captures queue in a local outbox and replay when the database is back;
`khipu doctor` stays red until the outbox is empty, so a silent backlog is not
possible. Reads fall back to a full local `hub_snapshot.sqlite` (episodes,
topics, revisions, nodes, edges, embeddings) refreshed whenever the hub was
last reachable — results are tagged `stale: true`. Keyword search always works
offline; semantic search needs a local embed provider configured (cloud Gemini
is not called while pretending to be online). The snapshot is never a write
SSOT — writers still use Postgres or the outbox. Capture mode (`legacy` /
`dual` / `hub`) is a CLI setting (`khipu config --set-capture-mode`); there is
no desktop toggle for it yet.

**How much does the capture cost in Gemini calls?**
One summarisation call per capture, and a capture happens after five user turns
or twenty minutes — plus one at every compaction or session end. A window under
200 characters is never sent. Embeddings are keyed by content hash, so text that
has not changed is never re-embedded.

**Do I need the gateway?**
Only for agents that run in the cloud (Grok Bot / Cursor cloud agents). Claude
Code, Cursor, Codex and Aegis on your Mac talk to the database directly over your
private network. If you never use a cloud agent, skip the gateway entirely.

**Something is red in `khipu doctor`. What now?**
Each check names what it wants. `not_configured` is not red — it lists checks
that do not apply on this machine. For anything else, the check's own payload
(`drift`, `outbox`, `capture_liveness`, `git_sync`, `backup`) says what it saw.

**Can I run this on Linux or Windows?**
The CLI is plain Python and should run anywhere; the Keychain integration and
the desktop app are macOS-only today. Use `KHIPU_DATABASE_URL` /
`GEMINI_API_KEY` in the environment instead of the Keychain.

**Is my memory locked in?**
No. It is ordinary tables in your own PostgreSQL — `episodes`, `topics`,
`nodes`, `edges` — and `khipu regen-memory` can write topics back out as
markdown at any time.

## Support

Email **[support@kinglollipop.com](mailto:support@kinglollipop.com)** for help,
questions, or a commercial licence conversation. If something is broken,
include the output of `khipu doctor` — it never contains secrets — and which
harness you were using. Bugs and feature requests are also welcome as GitHub
issues; security problems should go by email or through
[GitHub's private advisory form](https://github.com/mrkinglollipop/Khipu/security/advisories/new)
rather than a public issue.

---

## Licence

Khipu is licensed under the **[GNU Affero General Public License v3.0](LICENSE)**.

AGPL-3.0 is a strong copyleft licence. Anyone may use, modify and redistribute
Khipu, but a modified version must itself be released under AGPL-3.0 — and
Section 13 extends that to network use, so running a modified Khipu as a hosted
service obliges you to offer its source to the people using that service.
Hosting instead of shipping is not a way around it.

Commercial use is not forbidden, but it is only possible on those terms. If you
want to build on Khipu without releasing your own source, contact the maintainer
about a commercial licence.

Contributions are covered by the same licence plus a
[Contributor Licence Agreement](CLA.md) — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

The Khipu name and icon are not covered by the licence and remain the
maintainer's marks.
