# Summariser providers: let the user pick the model and the bill

Written 2026-10-13 after the capture summariser ran the owner's Google project into its monthly spending
cap. This page is the scope; a build session reads it first. The owner's rules: no loss of memory quality
or functionality, every hard limit liftable, and "we need to work with power users".

## Status

| Phase | What | State |
|---|---|---|
| 0 | Spend made visible and bounded without dropping anything (PR #157: machine messages are not turns, `khipu budget`, usage accounting) | done 2026-10-13 |
| 0b | Stop summarising re-emitted history after Claude Code compactions (dedupe by fingerprint) | in flight 2026-10-13, branch `fix/capture-dedupe-reemitted` |
| 1 | Provider abstraction for the synth role: `gemini`, `anthropic`, `openai`, `local`, with a price table and per-provider daily ceilings | open |
| 2 | Subscription routes: `claude-cli` (`claude -p` on the user's own login) and `codex-cli` (`codex exec` on the user's own login) | open; needs phase 1 |
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
   `gemini` (today's `cloud`, kept as an alias), `anthropic`, `openai`, `local`, and in phase 2
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
