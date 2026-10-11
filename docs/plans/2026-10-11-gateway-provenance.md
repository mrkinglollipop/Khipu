# Gateway provenance: server-assigned ownership for mutations

Approved in principle, Matt 2026-10-11, second of four backlog decisions from the 2026-10-10 audit. Build
after `2026-10-11-capture-identity.md` phase 1 has landed, so the capture path changes one thing at a time.
This page is the scope; a build session reads it first. Audit IDs: GW-6 (P0); review RV2-04 (P1).

## Status

| Phase | What | State |
|---|---|---|
| 1 | `episodes.origin` column, backfill; gateway passes the token label; capture stamps it | open |
| 2 | One authorization check on every gateway mutation, dedup target and decision link | open |
| 3 | One token label per caller (Aegis gets its own); tool descriptions tell the truth | open |

One PR per phase, merged alone and soaked before the next.

## The defect, in one paragraph

Through the HTTPS gateway, every valid bearer token can call every tool, and no mutation checks who owns
the row it changes. `khipu_owed_update` and `khipu_decisions_update` have no check at all
(`mcp_server.py:1222`, `:1321`). Capture dedup picks a merge target by project, time window and similarity
only, so a cloud capture can fold into a local episode (`capture.py` `_dedup_candidates`). `khipu_forget`
refuses only when the episode's `session_id` starts with one of four local harness names
(`forget.LOCAL_HARNESSES`, `forget.py:38`); a missing, unprefixed or unknown prefix passes, and the tool
description promises the opposite ("only episodes captured by a cloud harness"). The first fix, an allow-list
of `grokbot:` prefixes on forget, was reverted (RV2-04) because `session_id` is optional on capture: the
gateway accepted summary-only captures that forget then refused. Ownership cannot come from a string the
caller typed; it has to be assigned by the server when the row is written.

## Source facts (verified against main `1cf486b`; paths under `packages/cli/`)

- Tokens: `KHIPU_GATEWAY_TOKEN` (label `default`) plus optional `KHIPU_GATEWAY_TOKENS="label:token,…"`
  (`gateway.py:160-175`, `parse_tokens`). `_authorized` (`:312-325`) returns the label. The label reaches
  only the rate bucket, the log line and the liveness file; it is not passed to `handle_message`.
  `mcp_server` knows one process-wide flag, `_via_https_gateway()` (`:138-154`).
- The Aegis harness on this Mac also talks to the gateway with a bearer token (`integrations.py:1720-1802`).
  "Came through the gateway" therefore does not mean "cloud".
- `episodes` has `session_id`, `harness`, `project`, `parent_session_id`, `transcript_range`, `raw` and no
  owner column. `harness` is written from the payload (`mirror.py:356-372`), so it is caller-supplied.
  `commitments` link to episodes by `opened_episode`/`closed_episode`; `decisions` by `episode_id` and
  `session_id`; `decision_links` by `old_id`/`new_id`.
- `_tool_capture` (`mcp_server.py:1113`) forwards every non-null argument into the payload. Nothing is
  stamped server-side; the only fallback is `CLAUDE_SESSION_ID` from the environment.
- The 12 tools: `khipu_search`, `khipu_get`, `khipu_graph`, `khipu_status`, `khipu_owed`, `khipu_decisions`,
  `khipu_brief`, `khipu_reflect` read; `khipu_capture`, `khipu_owed_update`, `khipu_forget`,
  `khipu_decisions_update` write. `khipu_brief` and `khipu_reflect` write caches; inspect them in phase 2.
- Liveness bookkeeping is a JSON file in the container's data dir keyed by token label
  (`gateway.py:116-157`), lost on every container recreate. It is evidence that labels already identify
  callers, nothing more.
- The gateway image never runs migrations; `has_columns` probes gate every column written since 0008. The
  deploy script lives in the private ops repo.
- Tests pinning today's behaviour: `tests/test_audit1010_mcp.py::MutationTest::
  test_gateway_can_forget_accepted_cloud_captures` (None, `cursor_cloud:`, `grokbot:` ids all forgettable)
  and `test_forget_rejects_local_gateway_provenance`. No test covers `owed_update` or `decisions_update`
  authorization, or a dedup merge across origins.

## Decisions

1. **Origin is a server-stamped column, `episodes.origin TEXT`.** Values: `gateway:<token label>` for
   anything written through the HTTPS gateway; `local:<host>` for the CLI and hooks on a machine (`<host>`
   is the stable machine identity Khipu already uses for logs and checkpoints; the builder confirms which
   one before choosing). A caller-supplied `origin` in a capture payload is dropped, not honoured.
2. **One rule for every gateway mutation.** A mutation through the gateway is allowed only when the target
   row's origin equals `gateway:<the calling token's label>`. `NULL` or any other origin refuses with a
   message that names the origin class, never the value. Commitments and decisions resolve ownership through
   their episode; a commitment or decision with no episode link is refused through the gateway. Decision
   links need both ends owned. Locally (not through the gateway) nothing changes: the machine owner owns
   everything.
3. **Dedup candidates must share origin**, both locally and through the gateway. A bot's capture never merges
   into a machine's episode and a machine's capture never merges into a bot's.
4. **Unknown is not cloud.** Pre-migration rows are backfilled by `session_id` prefix: `grokbot:` and
   `grok_bot:` become `gateway:default`; the four local harness prefixes become `local:<this host>`; anything
   else stays `NULL` and is mutable only locally. A second Mac joining later backfills nothing; its rows
   are stamped from its first capture after the migration.
5. **One label per caller.** The Grok bot keeps `default`; Aegis gets its own label and token. The deny-list
   on local harness prefixes stays only as the fallback for a hub that has not run the migration.
6. **No per-token scopes yet.** Read tools stay open to every valid token. Scopes are a later item if a
   second cloud caller ever appears.

## Phase 1: the column, the stamp, the label

Inspect `gateway.py` (`_authorized`, the handler that calls `handle_message`), `mcp_server.py`
(`_via_https_gateway`, `_tool_capture`), `capture.py::write_pg`, `mirror.py::_upsert_episode` and the
capture-identity migration before writing anything.

1. Migration (next free number): `ALTER TABLE episodes ADD COLUMN origin TEXT`, a partial index on `origin`
   for non-null values, and the backfill from decision 4 in the same transaction. The `<this host>` value
   is a migration parameter, not a hard-coded string; `khipu migrate` supplies it from the local identity.
2. The gateway passes the token label into the per-request context (a context variable set around
   `handle_message`, replacing the process-wide boolean, which becomes "label is set"). Concurrency: the
   gateway serves on threads, so the context must be thread-local, and a test must prove two concurrent
   requests with different labels never see each other's.
3. `write_pg` stamps `origin` from the context: `gateway:<label>` when set, `local:<host>` otherwise. The
   payload's own `origin`, if any, is removed before the row is written. `has_columns` gates the write.
4. Tests: a gateway capture lands with `gateway:default`; a local capture with `local:<host>`; a payload
   that claims `origin` is overwritten; a hub without the column still accepts both.

Stop-condition: if the machine identity is not stable across reinstalls (for example a random id regenerated
on setup), stop and say so; a backfill keyed to it would orphan rows on the next reinstall.

Deploy order on this Mac: `khipu migrate`, then deploy the gateway, then merge.

## Phase 2: the check

1. One function, `authorize_mutation(kind, id)`, used by `khipu_forget`, `khipu_owed_update`,
   `khipu_decisions_update` (every action, including confirm and reject links and supersede-with-new-text)
   and the dedup candidate query. It reads the target's origin and compares with the request context. Local
   callers short-circuit to allowed.
2. Dedup: `_dedup_candidates` filters on `origin = <the capture's origin>` when the column exists.
3. Replace the prefix deny-list in `_tool_forget` with the check, keeping the deny-list only when the column
   is absent.
4. Inspect `khipu_brief` and `khipu_reflect` for writes a gateway caller can trigger; if any mutate shared
   rows, they get the same check, otherwise record in the PR that they do not.
5. Tests, with local, cloud and unknown-origin rows for each mutating tool: owned rows mutate; the other two
   classes refuse with the stated message and leave the row untouched; the existing
   `test_gateway_can_forget_accepted_cloud_captures` still passes because its captures are now stamped.

Stop-condition: the live Grok bot captures and forgets continuously. Before merge, run one capture and one
forget through the deployed gateway from a token with the `default` label and confirm both succeed; a refusal
there means the backfill or the stamp is wrong, not the bot.

## Phase 3: labels and truth

1. Aegis gets its own token and label in the gateway's environment on the Linode (an ops change, no code),
   and `khipu gateway token set` on this Mac stores the new token. From then on Aegis episodes are
   `gateway:aegis` and Aegis can forget its own captures through the gateway, which the deny-list forbade.
2. Tool descriptions for the four mutating tools state the real rule from decision 2.
3. `khipu doctor` shows the count of `NULL`-origin episodes so a hub that skipped the backfill is visible.

## Oracle

Hermetic pytest from `packages/cli` (temp `HOME`, `KHIPU_KEYCHAIN=0`; baseline is the two named
`test_graph_backup.py::RunOffsiteTest` failures); the gateway socket tests in `tests/test_gateway.py` run
outside the sandbox; `ruff check --no-cache packages/cli`. At most three oracle runs per change. Live gate per
phase: `khipu verify` shows `grok_bot` and `aegis` green and one gateway capture carries the expected origin.

## Out of scope

Per-token scopes (decision 6). Validating the capture argument schema (GW-2, GW-5, RV2-02: separate
decision). Making the liveness file durable across deploys. Any change to local (non-gateway) authorization.

## Unverified

- Which machine identity is stable; phase 1's stop-condition covers it.
- The distribution of `session_id` prefixes on the live hub, which decides how many rows the backfill leaves
  `NULL`. Count it before writing the migration.
- Whether any current gateway caller besides the Grok bot and Aegis exists; the token environment on the
  Linode is the source of truth.
