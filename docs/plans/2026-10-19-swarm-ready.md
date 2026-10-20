# Swarm-ready search (scope, 2026-10-19)

Status: scope for review; no code changed. Kept: no lost captures, no lost quality or function, every limit a liftable app setting. Must work on local Docker Postgres and on a small shared remote hub.

## The incident

At 22:56Z about 35 Grok Build jobs started at once on the Mac. Each agent ran its own `python -m khipu.mcp_server` (45 processes), and each fired about 3 `khipu_search` calls at startup. One search took 39 minutes. The hub (Postgres 19 beta 3, 2 CPUs, 4 GB, `max_connections` 40, `shared_buffers` 128 MB) refused new clients, which blocked saves and searches from every Mac and from the cloud gateway. After the clients were killed, 31 orphaned SELECTs ran for another 20 to 46 minutes until they were terminated by hand.

It was building again while this doc was written. At 23:55Z the Mac had 47 `khipu.mcp_server` processes (church-harness Grok and Claude jobs), and the hub had 8 copies of the memory-cosine query from this Mac, each running 340 to 620 s.

Evidence: code at `67732f0` (live slot `eefa03d`); read-only catalog, `pg_stat_*` and EXPLAIN on the hub; the query log; a throwaway Postgres 16 + pgvector with synthetic data of the hub's shape (23,600 episodes, 7,080 in one project, 38,750 vectors).

## Findings

### 1. Root cause: a project-filtered search scans `episodes` once per matching episode

The memory cosine leg (`embed._cosine_candidates`, `packages/cli/khipu/embed.py:2098`) checks that each candidate is still live with `EXISTS (SELECT 1 FROM episodes e WHERE e.id::text = m.ref …)` (`embed.py:2141-2147`). The project filter is another `EXISTS` on the same join (`embed.py:2153`). The cast `e.id::text` rules out the primary key, and no index exists on that expression. The liveness check also sits inside an `OR` across kinds, so the planner cannot hash it.

The hub's plan for `project = mrkinglollipop/church-harness` estimates **2** matching episodes. There are **7,092** (counted). The planner picks a nested loop, and every one of those episodes runs `SubPlan exists_3 -> Seq Scan on episodes … Filter: ((deleted_at IS NULL) AND ((id)::text = m.ref))`. One probe measured on the hub read 363 to 501 buffers in 6 to 9 ms. The table has 6,868 pages, so a typical probe reads thousands. That is **minutes of CPU per search, unloaded**; 45 on 2 CPUs take tens of minutes.

The hub's statistics have never been reset (postmaster up since 2026-08-17). They show 5,377,637 sequential scans of `episodes`, reading 70.1 billion tuples. No other table comes close.

On the hermetic copy (1/17 of the hub's page count) the filtered query took **8,035 ms** and 1.53 M buffer hits. After `CREATE INDEX ON episodes ((id::text))` it took **51 ms**, and both the liveness probe and the label lookup used the index.

This is the common path. 131 of the 134 searches logged between 22:40 and 23:59Z carried `project`, because agents pass their repo.

### 2. Every search pays for the same cast

The episode label is `(SELECT e.summary FROM episodes e WHERE e.id::text = m.ref)` (`embed.py:2203`). That is one sequential scan per returned row, up to 50 per search; the unfiltered hub plan shows it as `SubPlan expr_3 -> Seq Scan on episodes`. On the hermetic copy the index brought the unfiltered query from 41.5 ms to 6.0 ms.

### 3. Unfiltered semantic search returns fewer candidates than it asks for (a quality defect)

The unfiltered leg does use `idx_memory_embeddings_hnsw_gemini2_768`. But the memory leg never sets `hnsw.ef_search`, so pgvector's default of 40 sits below the query's `LIMIT 50`. The commitment filter (13,591 of the 40,841 active-profile vectors) and the liveness filter run after the index scan. On the hermetic copy the query returned **27 of 50** rows. With `hnsw.iterative_scan = relaxed_order` and `ef_search = 100` (pgvector 0.8; the hub has 0.8.6) it returned 50 in 88 ms. The library leg already sets `ef_search` (`library_search.py:319-320`).

There is no `idx_embeddings_hnsw` index. The real ones and their scan counts:

| Index | Scans | Note |
|---|---|---|
| memory `gemini2_768` | 1,476 | active profile |
| memory `gemini768` | 0 | inactive profile, 21 MB |
| memory `voyage_3_1024` | 0 | empty |
| library `voyage_3_1024` | 669 | |

Statistics were not reset (`stats_reset` is null), so the zero that prompted the question belongs to an inactive profile's index.

### 4. The memory legs have no time limit

- `_cosine_candidates` connects with no statement timeout (`embed.py:2131`). `hybrid_search` uses `try_hub_connect()` (`embed.py:2861`), which sets no timeout and no keepalives (`hub_snapshot.py:386-405`). Owed and decision reads do get `hub_statement_timeout` (30 s, `offline.py:14`); search does not.
- The library legs are bounded per statement (`library_search.py:393-410`). When the 6 s budget runs out, the code cancels only the statement in flight (`embed.py:2684`). The worker thread treats that as one failed leg and carries on with its next statements.
- The query is embedded while the cosine connection is open (`embed.py:2169`). The connection then sits idle on the hub for up to the embed timeout plus retries. A cache hit also writes and commits an `UPDATE` (`embed.py:626-684`).

### 5. Connections are opened per call, but nothing limits them across processes

`mcp_server` reads stdio one frame at a time (`mcp_server.py:2687-2716`), so each process has at most one search in flight. It does not hold a connection for its lifetime; each leg opens and closes its own. A default unfiltered search holds two connections at once (the memory leg plus the library thread). Forty-five processes therefore need 45 to 90 connections against a limit of 40, and nothing on the Mac caps how many processes reach the hub.

### 6. Libraries were not the cause

Any filter turns the library legs off (`embed.py:2820`), so 131 of 134 swarm searches never touched the 5 GB library tables. The memory literal leg is a two-worker parallel sequential scan of `episodes` across five ILIKE columns.

### 7. Orphaned queries and saves

- `client_connection_check_interval` is 0 and `tcp_keepalives_idle` is 7200, so a backend whose client has died keeps running until it tries to send. That is why the orphans outlived their clients. `idle_in_transaction_session_timeout` is 0.
- Every client connects as one superuser role, `alzy`. `superuser_reserved_connections = 3` therefore reserves nothing for captures, and `reserved_connections` is 0.
- Captures on Macs were delayed, not lost: the outbox keeps them until the hub accepts them (`capture.py:113`). Whether the gateway's callers lost anything was not checked.

### 8. The local replica is fresh enough to carry search under load

`replica_heal` pulls incremental changes at most once a minute (`replica_heal.py:43`, `:330`). The last measurement found episodes 233 s behind and vectors 0 s behind. Per-prompt recall already answers from the replica, using both cosine and keyword matching (`recall_prompt.py:2052-2134`).

The MCP tool's fallback is weaker. `search_stale_payload` (`hub_snapshot.py:6032`) runs keyword-only for hybrid mode, and cosine only when the mode is semantic (`:6061`). In the window, 71 of 134 searches were answered this way.

## Design

**A. Make the query cheap.** A migration adds `idx_episodes_id_text ON episodes ((id::text))`. It is tiny and fixes the liveness check, the label lookup and the backfill path at `embed.py:1805` without changing any result. The cosine SQL is then split into an episode branch and a topic branch, so liveness and filters are top-level conditions and never an `EXISTS` inside an `OR`. With a project filter, an exact sort of about 7,000 vectors is cheap and more accurate than an approximate scan. The unfiltered leg sets `ef_search` to at least the limit, and turns on `iterative_scan` where pgvector is 0.8 or later. Local Docker installs get the same migration.

**B. Bound every search statement and degrade one leg at a time.**

- A new owner setting, `hub_search_timeout` (default 8 s; 0 means no limit), puts the memory legs under `SET LOCAL statement_timeout`. A leg that times out is dropped and named in `degraded_legs`, as library legs already are.
- The query is embedded before a hub connection opens.
- The cache-hit write moves off the search path.
- The library thread stops at its next leg once cancelled.
- Search connections get keepalives.

**C. Admission control on each Mac.**

- A machine-wide gate of `hub_search_slots` (default 4) is built from `flock` on slot files, shared by MCP servers, the CLI and the recall fallback. No new process is involved, and a crashed holder frees its slot automatically.
- A search waits up to `hub_search_wait` (1.5 s) for a slot. It then answers from the replica using the recall lane's engine (cosine plus keyword), labelled `answered_from: "replica"` with `replica_lag_seconds`.
- Library searches, which have no replica, wait up to the timeout and then say plainly that the hub is busy.
- The same replica engine replaces the keyword-only offline fallback.
- Captures never take a search slot.

**D. Server limits that protect saves.**

- Searches and reads connect as a non-superuser role, `khipu_read`, with a connection limit (default 20 of 40), `statement_timeout = 30s` and `idle_in_transaction_session_timeout = 60s`.
- Captures, the outbox drain, migrations and the nightly keep the owner role, which gets `pg_use_reserved_connections` with `reserved_connections = 6`.
- Setup creates both roles for local Docker; joining a hub stores the second DSN; the gateway's search tools use the read role.
- `client_connection_check_interval`, server keepalives and the idle-in-transaction timeout are set on every install, and a doctor check reports any that are missing.

**E. Acceptance.** A hermetic Postgres at the hub's real size and project skew, loaded by 40 processes × 3 searches (half with a project filter) while a writer saves an episode every second. Pass requires all of:

- hub search p95 ≤ 2 s;
- every search answers, from the hub or as a labelled replica answer, within `hub_search_timeout` + 2 s;
- search connections never exceed slots × Macs;
- no capture fails or waits more than 2 s;
- no backend outlives its killed client by more than 15 s;
- `khipu recall eval` shows no loss.

## Rejected

- **One search service per Mac (the proposal's item 1).** The statement itself took minutes, so a pool of 4 would only have queued every agent behind four of them. Once A and B make searches cheap, what's left is a concurrency cap, and slot files provide it without a daemon. A broker would add a process to every search path. It restarts on every code change (`recall_daemon.py:28-32`), and the direct path would still be needed for when it is down.
- **PgBouncer.** It caps connections but cannot stop a 6-minute statement. It is one more service for every self-hoster, and it is awkward with the `options=-c statement_timeout` that `db._with_statement_timeout` sends. The role limit gives saves the same guarantee.
- **Running all search from the replica.** The replica has no libraries, runs minutes behind, and does not exist for the gateway or for cloud agents. It is the right tool for overflow (C), not for everything.
- **A bigger Linode.** Cost grows with project size × table size; more CPUs only divide it.
- **A short cache for identical queries.** 138 of the 140 queries in the window were distinct.
- **Library search only on request.** Libraries were already off for 98% of these searches. Removing them from the default would lose function, and B fixes the cancel.
- **A global `statement_timeout`.** The nightly dump (29 to 70 s), backfills and imports legitimately run long. The bound belongs on the read role and on search statements.

## Slices

Each slice is one session and one PR, merged on its own. The oracle is hermetic pytest from `packages/cli` with a temp HOME and `KHIPU_KEYCHAIN=0`, plus the check named for the slice. Allow at most 3 oracle runs per change; if it is still red, stop and report the last log. Any change to the live hub waits for Matt's go.

1. **Cheap cosine query.** Migration (`CREATE INDEX CONCURRENTLY IF NOT EXISTS`, so it adopts the index if it was created early), the per-kind SQL split, and `ef_search` with iterative scan behind a feature check. Oracle: a hermetic Postgres test asserting that the filtered plan has no `Seq Scan on episodes` under a loop and that unfiltered searches return 50 rows, plus `khipu recall eval`. Stop if the loop remains or golden recall drops at all.
2. **Bounded legs.** The `hub_search_timeout` setting in the CLI and the app, `SET LOCAL` with per-leg degrade, embedding before connecting, the cache write moved off the search path, the library stop flag, and keepalives. Oracle: pytest with `pg_sleep` legs proving that a slow leg is dropped and named while the search still answers, and that the library thread is silent after cancel. Stop if any timeout fails the whole search.
3. **Slots and replica fallback.** Slot files, `hub_search_slots` and `hub_search_wait` in the app, a replica hybrid engine shared with recall, the labelled payload, and the offline path switched to that engine. Oracle: pytest with 20 processes and 2 slots, proving at most 2 hub connections, that every caller answers, and that overflow answers are labelled; the two new setting rows render in the app. Stop if a replica answer is unlabelled.
4. **Server guard rails.** The read role and second DSN, reserved connections, the server settings, the doctor check, Docker setup, and gateway search on the read role. Oracle: on a hermetic Postgres, saturate the read role and show a capture still lands within 2 s; kill a client and show its backend ends within 15 s. Stop before the Linode (Decision 2).
5. **Acceptance load test.** Design item E, kept as a script the promotion gate can run. Never tune thresholds to pass.

## Immediate server changes (recommended now, not applied)

Each one applies with `SELECT pg_reload_conf()`, needs no restart, and is undone with `ALTER SYSTEM RESET <name>` (`DROP INDEX CONCURRENTLY` for the index).

- `ALTER SYSTEM SET client_connection_check_interval = '10s';` ends orphans: a backend stops within 10 s of losing its client.
- `ALTER SYSTEM SET tcp_keepalives_idle = 60;`, `tcp_keepalives_interval = 10;` and `tcp_keepalives_count = 6;` give up on a silent client after about 2 minutes instead of 2 hours.
- `ALTER SYSTEM SET idle_in_transaction_session_timeout = '10min';` is far above any Khipu transaction.
- `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_episodes_id_text ON episodes ((id::text));` is a schema write. It takes seconds and does not block writers. It removes the minutes-long search (8,035 ms to 51 ms on the hermetic copy), and slice 1 adopts it by name. It is the most valuable of the four, and the one that most needs an explicit go.

## Decisions for Matt

1. **Apply the four immediate changes before slice 1?** Recommendation: yes. Each one is undone with a single command, and the index alone stops a swarm from taking the hub down while the slices are built.
2. **Split the database role (a second, read-only DSN for search) or keep one role?** Recommendation: split. It is the only way to guarantee that searches never block saves, including from old clients, other Macs and the gateway.
3. **Defaults: 4 search slots and a 1.5 s wait per Mac, then a labelled replica answer?** Recommendation: yes. Both are app settings (0 slots means no limit), and the replica answer is the recall lane's engine, at most minutes behind.
