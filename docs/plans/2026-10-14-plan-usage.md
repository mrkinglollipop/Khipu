# Plan usage: agents can see how much of each subscription is left

Asked by Matt 2026-10-14 ("is it a good and possible feature for us to add live usage limits to Khipu so
agents can see what plans have what usage left? This would really help with orchestration with multiple
cli"; "Don't forget muse too"; "Proceed as you recommend"). This page is the scope; a build session reads
it first. Builds after the promotion gate (`2026-10-14-promotion-gate.md`) and goes through it.

## Status

| Phase | What | State |
|---|---|---|
| 1 | `khipu usage`: one probe per installed subscription CLI, cached, as CLI JSON and an MCP tool | built 2026-10-14 (branch `feat/plan-usage-p1`): `khipu/usage.py`, `khipu usage`, MCP `khipu_usage`, fixtures `tests/fixtures/usage/`; real run on this Mac answered all four CLIs in 0.6–1.6 s |
| 2 | Home card and Settings → Models rows; doctor warning when a plan is nearly spent | built 2026-10-14 (branch `feat/plan-usage-p2`, desktop 0.4.14): fixed-argv `khipu_usage_status` (`usage --json`) and `khipu_usage_refresh` (`usage --fresh --json`); `PlansCard.tsx` (Home card with Refresh, Settings → Capture & models card with Probe now and an add/remove list of the accounts read, through `config --set usage_accounts JSON` / `--unset`); health report "Plans" row and a Home attention item at 90%, read from `khipu_usage_status` (it can probe on demand; the item is re-judged every minute while it shows and at its earliest reset); `khipu doctor` carries a cache-only `plan_usage` block and a `warnings` line per window still in force at 90% or more, never gating `ok`, and `khipu_status` (complete schema) carries the same `warnings`; fix rounds 1 and 2 below |

Two PRs, phase 1 first.

## What was established (probed live on this Mac, 2026-10-14, all three in about one second each)

Every CLI reports its own plan headroom through a protocol it already speaks, using the login it already
holds. Khipu runs the CLI as the user and reads the answer; it never holds, reads or stores a credential,
and no model call is made (no tokens spent).

| CLI | Probe | Reports | Caveat |
|---|---|---|---|
| Claude Code 2.1.296 | `claude -p --input-format stream-json --output-format stream-json --verbose --setting-sources project --no-session-persistence`, send `initialize`, then the SDK control request `get_usage` with `skip_behaviors: true` | plan, five-hour window used % and reset, weekly all-models used % and reset, weekly per-model (Fable) used %, extra usage and spend | never `--bare` (skips the keychain, so no login). A `CLAUDE_CONFIG_DIR` login made with a setup token answers `rate_limits: null`; the second account needs a `/login` to be readable. The status line's `rate_limits.*.used_percentage` is a staler secondary source. |
| Codex 0.162 | `codex app-server` over stdio: `initialize`, `initialized`, `account/rateLimits/read` | plan, primary and secondary windows (used %, reset), credits balance | `secondary` can be null (the pro plan reported only a weekly window). Passive fallback: `rate_limits` inside `token_count` events in `~/.codex/sessions/**/rollout-*.jsonl`, current as of the last turn. Schema: `codex app-server generate-json-schema`, `v2/GetAccountRateLimitsResponse.json`. |
| Grok Build 1.0.52 | `grok agent stdio` with `GROK_HOME` of the login to read: `initialize`, then ACP method `_x.ai/billing` (with the underscore) | plan name, credit usage %, weekly period end, prepaid balance | undocumented; may change between versions. The Aegis home (`~/.grok`) and the dispatch home (`~/.grok-dispatch`) can be different logins; probe each configured home. `grok usage <id>` is per-session cost only. |
| Muse Code 1.4.6 | `muse serve --no-session-log` (line-delimited JSON-RPC, Muse Session Protocol v1): `initialize` with `clientInfo`, then `initialized`, then `usage/read` (stable surface, no model call; schema from `muse schema generate-json-schema`). `usage/changed` pushes the same shape. | tier, five-hour-class window (used %, reset, window length), weekly (used %, reset) | **Last-observed, not live:** the numbers come from a `subscription_usage` frame that streams in during model calls, and a fresh host answers `{}` until a turn has run. So the probe reports the figures as of the last Muse turn and marks them `observed_at`; it never spends a turn to refresh them. Signed in on this Mac since 2026-10-13 22:43 (OAuth device code, token in the keychain item `ai.meta.dev.credentials`); the probe must not set `META_API_KEY`, which overrides the login. Percentages are the provider's own and can exceed 100. Open until a real turn is read: whether `muse exec --json` carries the frame, the tier id strings, the real window length. |

Not allowed, by standing rule: calling the vendors' usage endpoints directly (`/api/oauth/usage`,
`/backend-api/codex/usage`, xAI billing). Each needs an OAuth token Khipu would have to read and keep.

## Design

**`khipu usage [--json] [--fresh] [--max-age S]`.** One entry per configured account: `{harness,
account, plan, windows: [{name, used_pct, resets_at}], credits, probed_at, ms, ok, error}`. Accounts come
from config `usage_accounts` (default: every installed CLI with its default home; Matt's Mac adds the
second Claude account and the two Grok homes), each with a label. Probes run in parallel with a 5 s
timeout each; a failed probe reports `ok: false` with a fixed error code and never hides the others.
Results are cached in `state/usage.json` with the probe time; the default max age is 120 s so an
orchestrator polling every prompt costs nothing, `--fresh` bypasses it. Every probe is a child process
with an allowlisted environment (`usage.probe_environment`), stdin closed after
the request, and a hard kill at the timeout.

**MCP tool `khipu_usage`** returns the same JSON plus a one-line `summary_text` per account ("Claude main:
5 h 13%, week 34%, resets Sun 15:00Z") so a routing table or an orchestrator can paste it. Nothing in
Khipu routes on it; routing stays the orchestrator's decision.

**Freshness and honesty:** every row carries `probed_at` and the CLI version that answered; a row older
than the max age is marked `stale: true` rather than dropped. A window the CLI does not report is absent,
not zero.

## Phase 1: one build session

Inspect `extract._generate_claude_cli`, `synth_cli.binary` / `environment`, and the three probe scripts
the research left (`cu.py`, `rl.py`, `gb.py` in the 2026-10-14 scratchpad; copy their exact request
shapes into fixtures, not the scripts) before writing anything. Build `khipu/usage.py` with one adapter
per CLI (`probe_claude`, `probe_codex`, `probe_grok`, `probe_muse`; Muse's row carries `observed_at` from the last turn and is never refreshed by spending one),
the cache, the CLI subcommand, the MCP tool, config `usage_accounts`. Tests: each adapter against a
recorded fixture reply, the parallel run with one adapter hanging (the others still answer inside the
timeout), the cache max-age, no environment variable or file read that names a credential. Verify with
the hermetic suite and one real `khipu usage --fresh` on this Mac whose numbers match the CLIs' own
`/usage`, `/status` and `/usage` screens read the same minute. Stop-condition: if any adapter needs a
token or key to be passed, stop and ask.

**As built (2026-10-14).** Codex and Grok exit on end of input before they answer, so their stdin stays
open until the answer arrives and the kill closes it; Claude and Muse get stdin closed right after the
requests. The Claude probe adds `--strict-mcp-config` (no MCP servers start; Khipu's own is one of them)
and the Grok probe adds `--no-leader` (never attach to a shared agent). Every probe gets an allowlisted
environment (fix round below) and sets only its own home; the whole process group is killed at the answer or the timeout.
Only response frames are read: Grok's `_x.ai/mcp/servers_updated` notification carries the user's MCP
server configuration, environment values included, and is dropped unread. On the HTTPS gateway
`khipu_usage` answers `available: false` (that host runs no subscription CLI). A failed probe keeps the
account's last good figures, marked `ok: false` and `stale: true`; `stale` is judged on the figures'
own time (`observed_at` when the CLI gives one), so Muse's last-turn figures read stale once older than
the max age. The second Claude home (`~/.claude-t3-second`) answers `rate_limits: null` as predicted.

**Fix round (2026-10-14, after a Codex cross-family review).** Errors are fixed short codes
(`timeout`, `not_signed_in`, `bad_reply`, `no_answer`, `exit_<n>`, `not_installed`, `cli_error`,
`no_plan_limits`, `pending`); a CLI's own text is only matched to pick a code and never reaches a row, the
cache, the CLI output or the MCP result. The probe environment is an allowlist (HOME, USER, LOGNAME, PATH,
SHELL, TMPDIR, LANG, TERM, LC_*, plus the account's own home variable); no other value is read.
Malformed nested replies are `bad_reply`; legitimate absence stays `ok: true` with no windows. The Codex
fallback reads the five newest rollout files by mtime, the last 256 KiB of each, inside the probe's
timeout. `probe_all` returns by an overall deadline (timeout + 2 s) on daemon threads and kills every
process group still alive. The MCP tool is cache-first: it answers from the cache at once and starts at
most one background refresh (flock `state/usage.refresh.lock`, shared across processes); an account never
probed reads `pending`. The cache (version 2) is keyed by (harness, home); a failed probe keeps the last
good figures with their own `probed_at`, `stale: true`, and records `last_attempt_at` / `last_error`.
Grok Build 1.0.52 has no flag or variable that starts it with no MCP servers; the probe sets
`GROK_CURSOR_MCPS_ENABLED=0` and `GROK_CLAUDE_MCPS_ENABLED=0`, which drop the imported Cursor and Claude
servers (20 of 28 on this Mac, firecrawl among them); the eight in `~/.grok/config.toml` are still
announced, and no MCP child was seen starting before the answer.

## Phase 2: visibility

Home card "Plans" with one row per account (plan, the tightest window as a bar, reset time, stale tag);
Settings → Models lists the accounts with their labels and homes and a Probe now button; doctor warns
when any window is at 90% or more ("Claude main five-hour at 94%, resets in 40 min") so the orchestrator sees
it in the health report it already reads.

**Fix round (2026-10-14, after a review of phase 2).** A window whose `resets_at` has passed has started
again: it never raises the 90% item, turns the Plans row red or colours a bar, and reads "reset since
<date>". Figures kept from an earlier read alert as "when last read" and carry their own age ("figures from 2
days ago", "as of the last turn, 3 h ago"); Settings shows that age and the last attempt separately. Reset
times are the viewer's local time everywhere in the app (the UTC "Z" form stays in the CLI and MCP text), with
the date when the reset has passed or is six or more days out, and "resets in 40 min" under an hour. The 75% and
90% lines are judged on the one-decimal figure shown. The Plans row is red, naming the accounts, when no
account gave a figure and any failed, and names failed accounts beside the figures that were read. Every fixed
error code has a plain phrase, and Settings shows the row's Khipu-written note (an invalid entry's reason, the
`/login` hint). A failed `khipu usage` run shows a fixed sentence; its text goes to the console only. Home and
Settings share one reader that runs one read at a time, so a cache read never replaces a fresh probe and the
CLIs are never started twice by the app. `khipu config --set usage_accounts` takes JSON checked by
`usage.parse_usage_accounts` (known harness, no home for Muse, a home that names a real user, each label and
login once, at most 32); `khipu config` shows the stored list, its source and the accounts it resolves to. A
home such as `~nouser/x` is an `invalid_account` row instead of a crash. Not done: the CLI's synchronous path
still probes beside a refresh another process holds (an MCP background refresh); the app no longer causes that.

**Fix round 2 (2026-10-14).** The Home 90% item is re-judged every minute while it shows and at the earliest
reset among its windows (`usePlanAttention`), so it never outlives a reset or freezes its "resets in 40 min"
while Home stays open. The Codex fallback's note is plain words; the failed live read's code is in the row's
`live_error`. A failed probe that keeps the last good figures carries its own note (the `/login` hint) and drops
the kept read's. An invalid entry's note reads "this account entry is not valid: …". An account added in
Settings with no Name is named after its CLI and login folder ("Codex (~/.codex-work)"); the form refuses a
name or a login already listed in plain words, pointing at the field to change, before anything is saved, and
empties as soon as the list is saved (the re-read that probes the new account is not waited on). `khipu doctor`
gains `plan_usage`, read from the usage cache only (no CLI is probed, nothing waits): per configured account the
tightest window still in force with its label, used %, reset time, figures time and stale flag (`never_read`
when `khipu usage` has not read it), and doctor's new `warnings` list gets one line per window still in force
at 90% or more on the one-decimal figure the app shows ("Plan nearly used up: Codex (pro) week at 94%, resets
Thu 02:00Z", "as of …" for kept or old figures). Neither gates `ok`. `khipu_status` (complete schema) carries
the same `warnings`; its light per-prompt payload does not.

## Decisions

- Probe the CLIs, never the vendor APIs: the only credential-free path, and it stays in step with whatever
  the vendor changes because the CLI is updated with it.
- Cache with a short max age rather than a daemon: a daemon would poll accounts nobody is using; a
  cached probe costs a second only when someone asks.
- Khipu reports; it does not route. The routing table and the orchestrator read `khipu_usage` and decide.
