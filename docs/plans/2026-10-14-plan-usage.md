# Plan usage: agents can see how much of each subscription is left

Asked by Matt 2026-10-14 ("is it a good and possible feature for us to add live usage limits to Khipu so
agents can see what plans have what usage left? This would really help with orchestration with multiple
cli"; "Don't forget muse too"; "Proceed as you recommend"). This page is the scope; a build session reads
it first. Builds after the promotion gate (`2026-10-14-promotion-gate.md`) and goes through it.

## Status

| Phase | What | State |
|---|---|---|
| 1 | `khipu usage`: one probe per installed subscription CLI, cached, as CLI JSON and an MCP tool | open |
| 2 | Home card and Settings → Models rows; doctor warning when a plan is nearly spent | open |

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
timeout each; a failed probe reports `ok: false` with the CLI's own words and never hides the others.
Results are cached in `state/usage.json` with the probe time; the default max age is 120 s so an
orchestrator polling every prompt costs nothing, `--fresh` bypasses it. Every probe is a child process
with the same environment hygiene as the summariser CLIs (`synth_cli.environment`), stdin closed after
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

## Phase 2: visibility

Home card "Plans" with one row per account (plan, the tightest window as a bar, reset time, stale tag);
Settings → Models lists the accounts with their labels and homes and a Probe now button; doctor warns
when any window is above 90% ("Claude main five-hour at 94%, resets in 40 min") so the orchestrator sees
it in the health report it already reads.

## Decisions

- Probe the CLIs, never the vendor APIs: the only credential-free path, and it stays in step with whatever
  the vendor changes because the CLI is updated with it.
- Cache with a short max age rather than a daemon: a daemon would poll accounts nobody is using; a
  cached probe costs a second only when someone asks.
- Khipu reports; it does not route. The routing table and the orchestrator read `khipu_usage` and decide.
