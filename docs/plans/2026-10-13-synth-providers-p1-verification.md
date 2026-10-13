# Summariser providers, phase 1: local verification

Branch: `feat/synth-providers-p1`. Base: `844b6f0eaad8e59706e9d4afb1ef8383228d6262`.
Worktree: `/Volumes/Cloud Storage/Code/Khipu/.claude/worktrees/synth-providers`.

Implemented Gemini/Anthropic/OpenAI synth adapters, provider defaults and alias normalization,
credential resolution, cached-token usage and cost accounting, budget breakdowns, and doctor presence checks.
OpenAI requires an explicit model. Gemini remains the default; embed/vision settings and local transport are unchanged.

Two brief path/caller discrepancies were resolved: secrets live in `keychain.py`, and rerank previously called
Gemini directly. Rerank now dispatches the configured API provider through `_generate`; no CLI route was added.
`modelcheck.py` accepts canonical Gemini for its existing Gemini probe. Integrations verification is unchanged.

## Verification

- Focused worker run: `241 passed, 16 subtests`; log `/tmp/.codex_synth_providers_focused.log`.
- Root full gate, attempt 1: five doctor fixtures lacked the new synth-key prerequisite. Added fake presence
  to the fixtures, preserving all green assertions. Also removed one unused test import found by ruff.
- Root full gate, attempt 2, exit 1:
  `31 failed, 3551 passed, 83 skipped, 2 xpassed, 18 errors, 636 subtests passed in 76.38s (0:01:16)`.
- Final failures: exactly two documented `test_graph_backup.py::RunOffsiteTest` baseline failures;
  29 recall socket failures (including subtests) and 18 gateway setup errors from
  `PermissionError: [Errno 1] Operation not permitted`. No new assertion failures remain.
- Full logs: `/tmp/.codex_synth_providers_full_1.log`, `/tmp/.codex_synth_providers_full_2.log`.
- Ruff: 9 errors, output byte-identical to the initial baseline; log `/tmp/.codex_synth_providers_ruff_final.log`.
- Root AST comparison against the base: `PROMPT`, `_build_prompt`, `extract_memory`, `_generate_local` unchanged.
  The actual extraction fixture also sends byte-identical prompt text through all three fake HTTP adapters.
- Adapter/helper sizes: Gemini 41 lines, shared Anthropic/OpenAI helper 34 lines, individual adapters 4 lines each.
- All verification used temporary homes, disabled Keychain, cleared credentials/DSNs, and fake providers.
  Root gates ran the requested pytest command directly with an explicit 1,200-second subprocess timeout.
  One focused run plus two full runs exhausts the repeated selectors' three-run budget. No skips or xfails were added.

## Remaining limits

No live provider calls or online price checks were performed. Prices use the supplied 2026-10-13 snapshot.
Provider quality/default promotion requires the separate scope quality gate; this phase makes no recommendation.

The requested commit could not be created: the initial `git add` failed before staging with:
`fatal: Unable to create '/Volumes/Cloud Storage/Code/Khipu/.git/worktrees/synth-providers/index.lock': Operation not permitted`.
No merge, push, or pull request was performed. Changes remain in the requested worktree.
Intended commit message:

```text
synth providers: add Anthropic and OpenAI adapters behind the synth gate

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

## Changed files

Implementation: `packages/cli/khipu/{cli,config,extract,keychain,modelcheck,models,rerank}.py`.
Tests: `packages/cli/tests/test_{bundle_seal,capture_spend_bounds,cli_models,cli_recall_quality,integrations,models,rerank,synth_providers}.py`.
Tracking: `docs/plans/2026-10-13-synth-providers.md` and this verification file.
