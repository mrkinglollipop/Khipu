# Library sources and bring-your-own embeddings

Approved by Matt 2026-10-07: "Do it. Make sure this is integrated into Khipu. Integrating multiple types of
embedding is a very nice feature for those who don't want to re-embed. We should add compatibility for other
embedding models ... so people can bring theirs into Khipu." Origin: an Aegis session asked Khipu for a
transcript in the biblical library and got nothing, because Khipu indexes the library's author folders and
not its text (`docs/plans/2026-09-30-settings-parity.md` has the Settings background; this page is the scope).

This supersedes the P4 plan lock "Voyage vectors in graph.sqlite are NOT mirrored; Khipu re-embeds with its
own profile" (`khipu/graph_sync.py` docstring). Re-embedding is still available; importing is now equal.

## What ships

1. **Embedding providers.** A profile (`embedding_profiles`: provider, model, dim, normalize) can be
   `gemini` (exists), `voyage`, or `openai-compatible` (OpenAI itself, Ollama, LM Studio, any `/v1/embeddings`
   endpoint; endpoint + model + optional key). Query embedding and backfill dispatch on the profile's provider.
   Keys live in the Keychain like the others: new account `voyage_api_key`; `openai_compat_api_key` already
   exists. `khipu embed profiles list|add` manages profiles; the Settings Index screen lists them.
2. **Library sources.** A new source kind, `library`: a folder of `.txt` and `.md` files (PDF is a later cut).
   Each library source names its own embedding profile, so the memory search space keeps its single active
   pointer and a library can sit on a different model. Tables: `library_documents` (per file: path, title,
   author, tags, size, mtime, hash), `library_chunks` (chunk text, the same 6000/300 windows as memory),
   `library_embeddings` (profile, doc, chunk, `embedding vector` with no fixed dimension; one partial
   expression HNSW index per profile, created when the profile first gets rows). Title, author and tags come
   from front matter when a file has it, else from the path (`Author/Title.txt`).
   CLI: `khipu library add NAME --root PATH --profile PROFILE`, `list`, `status NAME`, `remove NAME`,
   `scan NAME` (walk, chunk, record; no model), `backfill NAME` (embed missing chunks with the source's
   profile; joins the nightly embed backfill).
3. **Import an existing index.** `khipu library import NAME PATH [--strip-prefix P] [--profile PROFILE]`
   reads vectors that were already computed elsewhere, so nobody re-embeds what they already paid for.
   Two input shapes: graphify's SQLite `embeddings` table (node_id, chunk_idx, source_file, chunk_text,
   embedding float32 blob, model, dims) and JSONL rows with the same fields (embedding as a float list).
   Each row's file path, after `--strip-prefix`, must resolve under the source root; rows whose chunk text is
   binary garbage are skipped and counted (the library's own index builder has the test for this). The
   profile is created from `model@dims` with the provider given by `--profile`'s record or `--provider`.
   Import is idempotent on (doc, chunk_idx, profile).
4. **Search.** `khipu_search` and `khipu search` gain a `library` kind: cosine over each enabled library's
   embeddings with that library's profile (the query is embedded once per distinct profile), literal and
   lexical over chunk text and document titles, fused by the existing reciprocal-rank fusion and reranked
   like everything else. A hit is `kind: "library"`, id `library:<name>:<doc_id>#<chunk_idx>`, label
   "Author, *Title*", snippet, and the file path; a title-only hit has no chunk. `khipu_get` returns the
   chunk with its neighbours. Filters: `kind: "library"` and `source: NAME`. The gateway exposes the same
   (it needs the provider key for any library profile it serves). The per-prompt recall lane does **not**
   search libraries: that lane has a latency budget and must never wait on a second embedding provider.
5. **Settings.** Data screen, "Libraries" card: add a folder, pick or create a profile, import an index
   file, see documents / chunks / embedded percent / profile, remove. Fixed-argv Tauri commands per the
   rule in `2026-09-30-settings-parity.md`. Ships as desktop 0.4.8.

## This Mac, after the feature ships

Library `biblical`, root this Mac's biblical corpus folder (5,581 txt/md files, 1.72 GB).
Import from the graphify SQLite file: 329,224 voyage-3 rows (dim 1024) over 5,340 files,
`--strip-prefix "Biblical System/corpus/"`. No corpus file has changed since that index was built
(2026-07-28), so coverage after import should be complete except files the old index skipped; `backfill`
then embeds those with voyage-3 at $0.06 per million tokens. Hub growth about 3 GB (text plus vectors); the
Linode has 59 GB free. The gateway gets the Voyage key through Khipu-ops. Acceptance: the exact Aegis query
("What Does God Want Heiser transcript preface") returns the three drmsh/miqlat posts as library hits, and
a passage query ("Deuteronomy 32 worldview disinherited nations") returns Unseen Realm chunks.

## The user's view: changing embeddings (added 2026-10-07 after Matt: "compatibility without loss of
functionality, quality or significant speed; surfaced clearly in the UI; the user may want to change
embeddings and have Khipu redo some; all aspects thought out")

Today the Settings "Capture & models" embed picker is persist-only: changing it changes nothing at runtime,
because search reads the active profile from the hub (`embed.py` never reads `models.embed`). That picker
goes away. The "Search index" screen becomes **Embeddings** and is the one place this lives.

**What the screen shows.** One row per embedding model (profile): provider, model, dimensions, where it is
used (memory, or which libraries), coverage for each space it serves (embedded / total chunks, missing,
stale), the key's presence (yes / no, never the value), and the median query-embedding time over the last
day (from the model call log), so a slow provider is visible before it is chosen. The memory row that
search uses is marked "Active for memory".

**Actions, each a job with a receipt** (estimate first, then progress, retry and cancel; partial shown as
partial; never a silent background change):
- *Add a model*: provider (Gemini, Voyage, OpenAI-compatible), model id, dimensions, endpoint for
  OpenAI-compatible, key (write-only field, stored in the Keychain). The dialog tests the key with one tiny
  embedding call before saving and shows the measured time.
- *Re-embed memory with ...*: estimate (chunks, approximate tokens, and the provider's list price when
  Khipu knows it, else "price unknown"), then the job embeds every memory chunk into the new profile while
  search keeps using the old one. When coverage reaches 100 percent the row offers *Make active for
  memory*; before that the button is disabled with the missing count beside it. Switching is a pointer
  flip and takes effect on the next search; the old vectors stay until the user chooses *Delete vectors*
  for that profile, with a confirm that names the row count. Rollback is the same pointer flip back.
- *Re-embed library X with ...* and *Import an index into library X* (same receipt shape; import shows
  rows read, rows skipped as corrupt, rows mapped to no file).
- *Redo some*: a profile's coverage row lists what is missing or stale (a chunk whose text hash changed
  since it was embedded); *Embed missing* runs only those. This is how "have Khipu redo some" works for
  both memory and libraries.
- *Delete vectors* for a profile that nothing uses; the row says what uses it when it is in use and the
  button is disabled.

**Quality and speed guarantees that make this safe to offer:**
- Nothing about the Gemini path changes. A user who never opens this screen sees no difference.
- A search space is never served by a half-built profile: *Make active* is gated on full coverage, so
  results never silently lose rows.
- Every provider's query embedding runs under the same interactive budget (10 s, two retries) and
  degrades to literal plus lexical the same way Gemini does, so a slow or down provider costs the same as
  today's failure mode, never a hang.
- The per-prompt recall lane uses only the memory profile. Libraries are explicit-search only.
- Dimensions are per profile; vectors never mix across profiles in one index (one HNSW index per profile).
- Doctor reports every profile's coverage and the active pointer, so the health report and this screen
  cannot disagree.

**Before Session D builds the screen, a mock of it goes to Matt for approval** (planning rule: approved
mocks for UI). Session D also removes the persist-only picker and migrates nothing: a user's stored
`models.embed` choice is shown once as "this setting no longer does anything; the Embeddings screen
replaces it" via the post-update notice.

## Cost

Ingest $0 for an imported index. Each library search costs one query embedding on the library's provider
(about 100 tokens; Voyage lists $0.06 per million after a 200M free allowance). A new 100k-word book is
about one cent to embed on voyage-3.

## Rules that stay

Embedding profile rules in `khipu-models` still hold: profiles are never overwritten, the memory space has
one active pointer, rollback is a pointer flip, embeddings are a derived cache and the files are the source.
What changes: a library source carries its own pointer. Secrets never travel in argv; the UI shows presence
only. `ALLOWED_SUBCOMMANDS` does not grow.

## Sessions

- **A. Providers and profiles**: provider adapters (voyage, openai-compatible) for query and batch
  embedding, `voyage_api_key` secret, `khipu embed profiles`, migration for untyped library vectors and the
  three library tables, index-per-profile helper. Oracle: hermetic pytest baseline plus scratch PG for the
  migration (pgvector-free; vector columns become `real[]` there, so similarity is not testable on scratch).
- **B. Library sources**: add/list/status/remove/scan/backfill/import, path mapping, corrupt-row skip,
  nightly job hook.
- **C. Search and get**: the library kind end to end, MCP tool docs, gateway.
- **D. Settings and release**: Libraries card, profiles list, 0.4.8 through the usual release path.
- **E. This Mac**: import, gateway key, acceptance queries above, supersede the plan-lock decision.

Each session reads this page first and inspects the code it extends before writing.
