# Summariser providers: let the user pick the model and the bill

Written 2026-10-13 after the capture summariser ran the owner's Google project into its monthly spending
cap. This page is the scope; a build session reads it first. The owner's rules: no loss of memory quality
or functionality, every hard limit liftable, and "we need to work with power users".

## Status

| Phase | What | State |
|---|---|---|
| 0 | Spend made visible and bounded without dropping anything (PR #157: machine messages are not turns, `khipu budget`, usage accounting) | done 2026-10-13 |
| 0b | Stop summarising re-emitted history after Claude Code compactions (dedupe by fingerprint) | done 2026-10-13 (PR #159) |
| 1 | Provider abstraction for the synth role: `gemini`, `anthropic`, `openai`, `local`, with a price table and per-provider daily ceilings | done 2026-10-13 (PR #160); [evidence](2026-10-13-synth-providers-p1-verification.md) |
| 2 | Subscription routes: `claude-cli` (`claude -p` on the user's own login) and `codex-cli` (`codex exec` on the user's own login) | done 2026-10-13 (PRs #161, #162, #163); quality gate passed for Haiku 5.5 (49 windows, Opus judge on 15: Haiku better 11, Gemini 2, equal 2, about half the hallucinated items); the maintainer's Mac runs `claude-cli`; product default stays `gemini` until phase 3 |
| 3 | Settings UI for provider, model and key source in the desktop app; `khipu models set --role synth --provider … --model …` | open; needs phase 1 |

One PR per phase, merged alone and soaked.

## Facts (researched 2026-10-13; prices are list prices per million tokens and must be re-checked at build time)

- Today: `models.synth` is `gemini-2.5-flash` via the Gemini REST API, thinking off, 4,096 max output;
  a capture call is about 3,000 prompt tokens and 400 output tokens (measured from `usageMetadata` on
  2026-10-13), roughly $0.002 a call; a heavy day is 1,000–2,000 calls.
- Gemini 2.5 Flash $0.30 in / $2.50 out (batch half; cached input $0.03). Gemini 2.5 Flash-Lite $0.10 /
  $0.40. Google's models page now steers new projects to 3.x Flash-Lite and says 2.5 access is limited to
  projects that already used it; no shutdown date.
- Claude Haiku 5.5 $0.10 in / $0.50 out for prompts up to 100k tokens (batch $0.05 / $0.25; cache read
  $0.01); 1M context. Three times cheaper than Flash on input and five times on output; near Flash-Lite.
- OpenAI's cheapest tiers (third-party listings only, unconfirmed): GPT-5 nano $0.05 / $0.40, GPT-5.4-nano
  $0.20 / $1.25.
- Anthropic, 2026-10-07 (support.claude.com): Max and Team plans include monthly API credits that cover the
  Agent SDK, `claude -p` and the API; `claude -p` and the SDK "still draw from your subscription limits".
  Legal page: advertised limits assume ordinary individual use; a product must not route requests through
  a user's Pro/Max credentials on the user's behalf, and developers building products should use API keys.
  Reading: a user running `claude -p` under their own login on their own machine is within the terms;
  Khipu must never ship, proxy or hold anyone's subscription credential, and must say so in the Settings
  text. Terms changed twice in 2026; re-check before release.
- OpenAI: `codex exec` reuses the saved CLI login or `CODEX_API_KEY`; the docs call API keys "the right
  default for automation" and ChatGPT-login headless use "an advanced option"; no published limits.
- Gemini Batch API: 50% off, target turnaround 24 h ("in the majority of cases much quicker"), jobs expire
  at 48 h. A capture delayed by hours is invisible to recall for those hours, so batch is only acceptable
  for a backlog the user has agreed to defer, never for the live path.
- Khipu's single network gate for cloud synth calls is `extract._generate_cloud` (captures, briefs,
  rerank, hygiene); `_generate` routes by `models.synth_settings()` (`provider` cloud|local). The daily
  ceiling and the usage file hang off that gate (PR #157).

## Decisions

1. **Provider is a user choice per role, never a silent switch.** `models.synth` gains `provider` values
   `gemini` (today's `cloud`, kept as an alias), `anthropic`, `openai`, `openrouter`, `local`, and in phase 2
   `claude-cli`, `codex-cli`. The default for a new install stays `gemini` until the Settings copy and the
   verify probe for each provider exist. Changing provider never changes what is captured, only who
   summarises it; the extraction prompt and JSON contract are shared, with one adapter per provider that
   maps the request and reads back text plus token counts.
2. **Every provider reports usage into the same file and the same ceiling.** `synth-usage.jsonl` rows
   carry `provider` and `model`; `PRICES_USD_PER_MTOKEN` is keyed by model with a `source`/`as_of` note;
   `khipu budget status` shows USD per provider; the synth call ceiling applies to all providers (a
   subscription route counts calls too, since the user's plan limit is finite).
3. **Keys follow the existing pattern.** API keys live in the Keychain under the `Khipu` service
   (`anthropic_api_key`, `openai_api_key`, beside `gemini_api_key`) or in the env; never in config.json.
   The subscription routes hold no credential at all: they run the user's installed CLI as the user
   (`claude -p --model <id> --output-format json`, `codex exec -m <id> --output-format json`), fail-open to
   the queue when the CLI is missing, logged out or rate-limited, and never touch another account's home.
4. **Subscription routes must not re-enter Khipu.** The CLI subprocess is a Claude Code session: Khipu's
   own hooks would fire inside it and capture the summariser summarising. The adapter runs it with hooks
   disabled (verify the exact flag or settings override at build time; `--settings` with an empty hooks
   block, or `CLAUDE_CONFIG_DIR` pointing to a Khipu-owned minimal config that still authenticates), marks
   the session id with a `khipu-synth:` prefix that `session_capture` ignores, and the verify probe proves
   no capture job was queued by the probe itself.
5. **Quality gate per provider.** Before a provider can be the default or recommended, the recall golden
   set plus a fixed sample of 50 real capture windows (redacted) are extracted with it and with Gemini
   Flash, and a Claude pass compares decisions, open loops and topics; the comparison is attached to the
   phase PR. A provider that drops decisions is offered as "cheaper, lower recall" in the UI, never as the
   default.
6. **Latency budget.** A capture extraction must finish inside the drain's per-job time (today about
   10 s for a 3k-token call); a CLI route that takes 20 s a call is acceptable for the drain but not for
   rerank, so rerank stays on API providers only.

## Phase 1: one build session

Read this page, then inspect `models.py` (`ROLES`, `synth_settings`, `cloud_model_id`, `PROVIDERS`),
`extract._generate`/`_generate_cloud`/`_generate_local`, `rerank._call_model`, `briefs.build_one`,
`embed._budget_take`, and `tests/test_models*.py`, `tests/test_extract*.py` before writing.

1. `extract`: an adapter per provider behind `_generate` (`_generate_gemini` is today's function renamed;
   `_generate_anthropic` uses the Messages API with `max_tokens` 4096 and no extended thinking;
   `_generate_openai` uses the Responses or Chat Completions API with the cheapest listed model); each
   returns `(text, usage)` and `_record_usage` takes both. Model ids come from `models.synth_settings()`;
   defaults per provider live in `models.py` (`gemini-2.5-flash`, `claude-haiku-5-5`, the OpenAI nano
   tier) and are validated by `khipu models set`.
2. Keys: `secrets` helpers for the two new Keychain accounts, env fallbacks `ANTHROPIC_API_KEY`,
   `OPENAI_API_KEY`, and `khipu secrets` support for setting them; the doctor `embed_provider`-style row
   for the synth provider ("synth provider reachable", one cheap call in `verify`).
3. Budget and usage: `provider` on usage rows; `budget status` per provider; price table entries.
4. Tests with fakes for each adapter (request shape, usage parsing, error mapping to the same exceptions
   the drain already handles), config round trips, and the doctor row.

Stop conditions: if an adapter needs more than ~120 lines, stop and say why; if the OpenAI model id or
price cannot be confirmed from a primary source at build time, ship the adapter with the model id as a
required setting and no default; never store a key in config.json.

## Phase 2: subscription routes (after phase 1 soaks)

`claude-cli` and `codex-cli` adapters as in decisions 3 and 4, with the verify probe that proves no
self-capture, a Settings note quoting the vendor's own terms with the date, and the quality gate of
decision 5 run before either appears in the UI.

## Phase 2 as built

OpenRouter uses one user key with any explicit vendor/model ID, reports its response cost per call, and
remains quality-gated per model.

The exact invocations (prompt on stdin; `<cwd>` is an empty Khipu-owned temporary directory under
`data_dir()/state/synth-cli/`, and `<file>` is a temporary last-message file outside that directory):

```text
claude -p --setting-sources "" --model <id> --output-format json --max-turns 1 --disallowedTools "Bash,Edit,Write,Read,Glob,Grep,WebSearch,WebFetch,Agent" --session-id <uuid>
codex exec --json --ephemeral -s read-only --skip-git-repo-check -m <id> -o <file> -C <cwd> -
```

The orchestrator's 2026-10-13 probe on Claude Code 2.1.296 established that `--bare` does not read
OAuth or the Keychain and returns "Not logged in" on a subscription login. `--setting-sources ""`
preserves authentication and loads no settings-file hooks. The adapter does not use `--bare`.
Codex 0.162.0-alpha.17.2 help confirms that `-` reads stdin and `--ephemeral` persists no session files.
Both adapters strip `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and every `KHIPU_*` environment variable,
default to a 60-second call timeout, and charge the liftable synth ceiling once per attempt.

Claude requires a valid UUID, so the exact capture marker is `state/synth-cli/sessions.json`, a
locked, atomically replaced list of the newest 2,000 IDs. IDs are persisted before launch; Codex's
emitted thread IDs are also registered. Hooks, transcript-path resolution and idle sweeps ignore
marked sessions; corrupt or unreadable lists are treated as empty. This checkout has no separate
Codex transcript crawler: native discovery consumes hook payload paths and sweep state files.
An explicit `khipu integrations verify` makes one configured CLI call and watches for matching queue
and hook state files for three seconds. Subscription rows count calls and tokens with estimated
USD zero and `billing: "subscription"`. Rerank refuses these providers.

Settings note, based on the orchestrator's Anthropic research dated 2026-10-07: `claude -p` and the
Agent SDK on a Max/Pro login "still draw from your subscription limits". The vendor's restriction
on routing products through a user's subscription credentials was recorded in that research as a
paraphrase, not a verbatim quote. Khipu runs the user's own installed CLI as the user and holds no
subscription credential. Re-check the vendor terms before release. No vendor research or live model
call was performed in this build session. Decision 5's 50-window comparison remains pending; neither
subscription provider becomes the default or receives a UI recommendation in this phase.

## Quality gate results (2026-10-13)

Method: 49 real queued capture windows (redacted, the same extraction prompt for every model), then an
Opus judge reading the transcript and every extraction for 15 of them. Two judged rounds.

| Summariser | Route measured | Median per capture | First places (of 15) | Wrong or invented items | Total failures |
|---|---|---|---|---|---|
| Claude Haiku 5.5 | user's Claude login (`claude-cli`) | 10.7 s | 10 | 3 | 2 wrong-task replies, since fixed (#162) |
| GPT-6 Luna, high effort | user's ChatGPT login (`codex-cli`) | 50 s with the user's Codex config | 3 | 2 | 0 |
| GPT-6 Luna, low effort | `codex-cli`, user config skipped | 8.0 s | 2 | 2 | 3 empty records, one for a 14,000-char window |
| Gemini 2.5 Flash | direct API | 2.9 s | 0 | about 10 | 1 empty record (a 561-char fragment) |

Verdicts: Haiku is the best memory and qualifies as the default. Luna is more accurate than Gemini Flash
where it answers (better in 12 of 15, about a fifth of the wrong items) but terse on open loops, and at low
effort it emptied a real window; at medium it returns that window in 10 s. Gemini Flash is last on quality
and first on speed. Consequences shipped: `claude-cli` runs with its own system prompt (#162); `codex-cli`
skips the user's Codex config and runs at medium effort (#167, #168); an empty record for a window of
2,000+ chars, or a reply that is not the capture JSON, from a non-Gemini provider gets one Gemini retry
when a Gemini key exists (#162, #168). Recommended order for the Settings page (phase 3): Haiku, then
Luna, then Gemini Flash; each on the user's own login where one exists, direct API or OpenRouter (#165)
otherwise. Luna at medium effort has not been through the full judged round; do that before it is shown
as "recommended" rather than "available".

### Round 3: Grok 4.7, Composer 2.5 fast, Luna at medium (2026-10-13, same 15 windows, same judge method)

| Summariser | Route | Median per capture | Cost per capture | First places | Wrong or invented | Mean rank (of 6) |
|---|---|---|---|---|---|---|
| Composer 2.5 fast, medium | Grok Build CLI | 21.9 s | $0.023 reported against the Grok subscription | 5 | 2 | 2.5 |
| Composer 2.5 fast, low | Grok Build CLI | 22.2 s | $0.024 reported | 3 | 5 (4 in one window) | 2.7 |
| Claude Haiku 5.5 (pre-#162 sample) | `claude-cli` | 10.8 s | subscription | 4 | 5 | 3.3 (2.8 without the two fixed wrong-task windows) |
| Grok 4.7 fast, low | Grok Build CLI | 18.1 s | $0.14 reported | 2 | 4, with inversions | 3.7 |
| Grok 4.7, low | OpenRouter API | 11.2 s | $0.012 | 1 | 1 | 4.3 |
| GPT-6 Luna, medium | OpenRouter API | 7.7 s | $0.0006 | 0 | 1 | 4.4 |

Findings: the Grok and Composer configs list about twice as many "decisions" as Haiku and the judge found
the extra items to be filler (restated findings, environment facts, proposals not yet decided); what
Composer adds over Haiku is open and closed loops. Luna is accurate but thin (9 open loops in 15 windows).
Grok Build adds about 30,000 tokens of its own harness to every call, which `--tools none`,
`--system-prompt-override` and a no-tools `--agents` agent do not remove; "Grok 4.7 fast" and Composer
have no public API model id (Grok Build and Cursor only), and Grok 4.7 on the API lists at $2/$6 per
million tokens. The top three are within the noise of a 15-window, single-judge sample (the judge notes
that one judgement call on two nested-task windows moves Luna up and the Composers down); a 49-window
round is needed before reordering the recommendation. Separate finding from the same afternoon: the Stop
hook drains the queue inline inside the harness's 20 s hook timeout (median hold 10 s, 72 of 262 drains
over 15 s on Gemini Flash), so summariser latency is felt at turn end; detaching the drain removes that
and makes per-call latency a freshness matter only.
