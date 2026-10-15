# Record paging: every record is retrievable in full, in bounded pages

Approved in principle, Matt 2026-10-11, fourth of four backlog decisions from the 2026-10-10 audit. This page
is the scope; a build session reads it first. Audit IDs: GW-20 (P2); review RV2-03 (P1). Additive API, no
migration.

## Status

| Phase | What | State |
|---|---|---|
| 1 | `khipu_get` and `khipu get` page topic bodies by UTF-8 byte offset; identity and metadata on every page | built, in review (branch feat/record-paging-p1) |
| 2 | Output is UTF-8 on the wire (`ensure_ascii=False`), with a byte budget that measures real content | built, in review (branch feat/record-paging-p2) |

Two PRs, phase 1 first.

## The defect, in one paragraph

Topic pages are the only large records (seven over 48 KiB on this hub, the largest about 1 MB; everything
else is capped at write time or by `limit`). The hub path clips a topic body at 64 KiB with no way to ask
for the rest (`activity.py:11`, `TOPIC_DETAIL_BODY_BYTES`; `:126-147`, `LEFT(body, n+1)` then
`_clip_utf8`, `body_truncated: True`), while the offline replica path returns the whole body
(`hub_snapshot.topic_detail_snapshot:2142`), so the same id answers differently depending on whether the
hub is reachable. The CLI `khipu get <topic>` (`cli.py:2413`) goes through the same clip. A previous fix
added a second cut in the MCP server (48 KiB per detail field, 256 KiB per tool text, replaced by a generic
notice) and was reverted (RV2-03): `khipu_get` takes only `id` and `kind`, so a caller could never retrieve
the remainder, and JSON escaping of non-ASCII (`\uXXXX`, then the backslashes doubled by the second
`json.dumps`) made a 135 KB CJK body measure 315 KB on the wire and get replaced wholesale. The regression
test that guards the revert (`test_audit1010_mcp.py:373`) mocks the detail functions and never exercises the
surviving clip in `activity.py`.

## Source facts (verified against main `1cf486b`; paths under `packages/cli/khipu/`)

- The MCP server is hand-rolled JSON-RPC (`mcp_server.py`), no SDK; protocol versions 2024-11-05,
  2025-03-26, 2025-06-18. Every tool returns one text block through `_tool_text` (`:1461-1467`):
  `json.dumps(payload, indent=2, default=str)`, default `ensure_ascii=True`, then the frame is dumped again
  at `:1662`. Input frames are capped at `MAX_FRAME_BYTES = 1 MiB` (`:63`, `:1642`); output is unbounded.
- The gateway (`gateway.py`) caps request bodies at 1 MiB (`:70`), serialises responses unbounded (`:469`),
  and sits behind Caddy whose config is not in the repo.
- `tools/call` results are free-form content blocks; `cursor`/`nextCursor` exist only for list endpoints.
  Paging must be a tool-level contract carried in arguments and result fields. `khipu_get` kind `library`
  already does this: a chunk plus `previous`/`next` neighbours (`library_search.py:591-653`).
- Sizes: episode `summary` max 4.1 KB on the replica; `verbatim` capped at 2,000 chars per class at write
  (`extract.py:264`); library chunks 6,000 chars (`embed.py:62`); search snippets 500 chars; `limit` on
  search 50, graph 200, owed 200, decisions 100. Topics: 1,440 live, 1,106 with non-ASCII; over 64 KiB: 4;
  over 256 KiB: 3 (`note:raw-memories` 1,050,959 B, `note:phase2-workspace-diff` 699,506 B,
  `note:aegis-2026-09-01-state` 394,381 B).
- Consumers of topic detail: MCP `khipu_get` (local stdio and gateway), CLI `khipu get <topic>`. The
  desktop app reads episodes through `khipu activity --show` (`App.tsx:1854`) and never calls topic detail.
  Nothing reads `body_truncated` except the test `test_audit1010_capa.py:455`, which pins the current clip.
- Write paths already use `ensure_ascii=False` (`capture.py:383`, `briefs.py:463`); only the output path
  escapes.

## Decisions

1. **Offsets are UTF-8 byte offsets into the stored body; a page never splits a code point.** The server
   fetches at most `max_bytes + 4` bytes from the database (`substring(convert_to(body, 'UTF8') from … for
   …)` on the hub, the byte-sliced `CAST(body AS BLOB)` on sqlite), trims the tail to a code-point boundary,
   and reports the next offset. Database reads are bounded before allocation, which the audit's "input
   framing limits do not resolve upstream allocation" clause asks for.
2. **Every page carries the record's identity and metadata**: id, kind, slug, title, `updated_at`,
   `content_hash`, `body_total_bytes`, `body_offset`, `body_next_offset` (`null` on the last page), and
   `complete: bool`. The body field is the only thing that differs between pages. `body_truncated` is
   replaced by `complete`.
3. **Default page is 64 KiB; maximum 512 KiB; `offset` defaults to 0.** Arguments are optional, so every
   existing call keeps working and receives the first page, which for 99.5% of topics is the whole record.
4. **Hub and replica answer identically** for the same id and arguments, except for staleness that the
   freshness scope already reports.
5. **The wire is UTF-8** (`ensure_ascii=False` in `_tool_text` and the gateway response). A byte budget, if
   one is ever added to tool text, is then measured on the content, not on escapes. No budget on tool text
   in this scope: paging bounds the one unbounded field.
6. **Episodes do not page.** Their fields are capped at write time; adding offsets there is speculative.

## Phase 1: paging on topic detail

Inspect `activity.topic_detail`, `hub_snapshot.topic_detail_snapshot`, `_tool_get` and `_detail_response`
in `mcp_server.py`, `cli.py:2413-2455`, `library_search.get` (the existing neighbour pattern) and the two
tests named above before writing anything.

1. One function computes the page from `(body bytes, offset, max_bytes)` and is shared by the hub and
   replica paths: returns the decoded text, `next_offset`, `total_bytes`, `complete`. Pin it with tests for
   ASCII, two-byte, three-byte and four-byte (astral) characters at every boundary position, and for
   `offset` beyond the end (empty body, `complete: true`, not an error).
2. `activity.topic_detail(slug, *, offset=0, max_bytes=65536)` fetches bounded bytes from the hub; the
   `LEFT(body, n+1)` clip goes. `topic_detail_snapshot` takes the same arguments and slices the sqlite blob.
3. `khipu_get` gains optional `offset` and `max_bytes` on its input schema, documented in the tool
   description with the result fields from decision 2; `khipu get <topic>` gains `--offset`, `--max-bytes`
   and `--all` (loops pages to stdout).
4. Tests: a 400,000-byte ASCII body ending `THE_END` and a 135,000-byte CJK body ending `末尾` are retrieved
   completely by following `body_next_offset` from both the hub (fake cursor) and a temp replica, and the
   final page carries the terminal text; the mocked full-record test becomes this one. `test_topic_body_
   query_and_response_are_utf8_bounded` is rewritten to the new contract.

Stop-condition: Postgres `convert_to`/`substring` on `bytea` must be verified against the scratch database
(`tests/test_pg_scratch.py`), not only the fake; an off-by-one here returns a page that fails to decode.

## Phase 2: UTF-8 on the wire

1. `ensure_ascii=False` in `_tool_text` and in the gateway's response encoding; the stdio writer must write
   UTF-8 bytes regardless of locale (`sys.stdout.reconfigure(encoding="utf-8")` or writing to the buffer).
2. Tests: a CJK and an emoji payload round-trip through a stdio frame and through a gateway request; the
   measured wire size is within 1.1× the UTF-8 size.
3. Live check: one `khipu_get` of a non-ASCII topic from a Claude Code session and one through the gateway
   from the Grok bot path, both rendering correctly. Hosts that cannot take UTF-8 JSON are not known; if one
   appears, the change is reverted for that transport only.

## Oracle

Hermetic pytest from `packages/cli` (temp `HOME`, `KHIPU_KEYCHAIN=0`; baseline is the two named
`test_graph_backup.py::RunOffsiteTest` failures); gateway socket tests outside the sandbox; `ruff check
--no-cache packages/cli`. At most three oracle runs per change. Live gate: `khipu get note:raw-memories
--all` returns the full body byte-for-byte equal to the hub's `length(convert_to(body,'UTF8'))`.

## Out of scope

Budgets on search, graph or reflect output (already bounded by `limit`). Episode paging (decision 6).
Response limits in Caddy. Strict capture argument validation (RV2-02, separate decision).

## Unverified

- The hub's `verbatim` column sizes were not sampled (the replica has no such column); the 2,000-char write
  cap is from code.
- Caddy's response handling for bodies over 1 MB; the live gate above exercises it through the local
  stdio path, and a gateway `--all` fetch of the same topic should be run once after deploy.
