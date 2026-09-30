# Settings parity: every user setting reachable in the desktop app

Matt, 2026-09-30: "No UI missing options." The 0.4.6 notes said the new behaviours were optional and off by
default, and the app had no way to turn them on. This page lists every persistent setting the `khipu` CLI
exposes, whether the desktop app exposes it, and what gets built. Inventory taken against main `e4f5ec4`.

## Gaps (CLI only today)

| Setting | CLI show / set | Build |
|---|---|---|
| 9 feature switches: validity_ranking, time_interpretation, relevance_floor, rerank, reflect, briefs, graph_candidates, decision_details, auto_supersede | `khipu features` (JSON, per switch `enabled` + `source` env/file/default) / `khipu features --set NAME on\|off` | New Settings section "Optional features": one toggle per switch with what it does, its cost, and the gate result from `docs/research/switch-gates-2026-09-29.md`. A switch whose `source` is `env` is shown locked, naming the variable. |
| `relevance.cosine_floor` (default 0.65) | Not shown; **no CLI setter** (`config --set` refuses it as an unknown path key) | Add the CLI setter first (`khipu config --set relevance.cosine_floor 0.65`, number in (0, 1], shown in `khipu config`), then a number field under the relevance_floor toggle. |
| Local recall service (`recall_daemon` launchd job, opt-in) | `khipu jobs status` / `khipu jobs install\|uninstall recall_daemon`; `khipu recall status` | Toggle in "Optional features": install/uninstall the job, show running or not. |
| Background jobs: nightly, monthly, graph_build, notes_watch, queue_drain | `khipu jobs status` / `jobs install\|uninstall NAME` | Per-job installed state with install/remove, in the Advanced section. |
| `capture_mode` (legacy / dual / hub) | `khipu config` / `--set-capture-mode` | Select in "Capture & models". |
| `dedup_similarity`, `commitment_close_similarity` (0 to 1) | `khipu config` / `--set KEY N` | Number fields in "Capture & models". |
| `user_aliases` (comma list) | `khipu config` / `--set user_aliases "a,b"` | Text field in "Capture & models". |
| `gateway_url` (https) | `khipu config` / `--set-gateway-url URL` (empty clears) | Field in "Another Mac". |
| Path overrides: memory_root, memory_repo, capture_v2, graph_sqlite, gemini_key_file | `khipu config` (`paths`, each with value, source, exists) / `--set KEY PATH`, `--unset KEY` | Fields with Reset in "Data". |
| Gateway bearer token (Aegis reads it) | `khipu gateway token status` / `token set` from stdin | Secret field (write-only, value on stdin, never argv, never echoed) next to the Aegis integration. |
| Active embedding profile | `khipu embed status` / `embed activate PROFILE` | "Make active" per profile in "Index"; disabled while that profile is missing vectors. No force from the UI. |

## Already exposed

Models (provider, endpoint, model id per role), sources (enable, embed media, add, remove), data directory,
integrations (install, verify, status), secrets (Gemini key, database URL, local endpoint key), capture now.

## Not settings (no UI planned)

Capture cadence (environment only), `grok-bot-config` (read-only account config), and one-shot maintenance verbs
(notes, project, briefs, reflect, decisions, hygiene, backfill). Briefs and reflect behaviour is governed by
their switches above.

## Security rule for the new commands

The webview never supplies argv. Each new Tauri command builds a fixed argv in Rust; a name or key it
accepts is checked against a constant list, a value is validated per key and passed as `--flag=value` or
after `--`, and the token travels on stdin. Each command gets an argv test like the existing
`jobs_refresh_is_a_fixed_argv_command_not_an_allowlist_entry`. `ALLOWED_SUBCOMMANDS` does not grow.
