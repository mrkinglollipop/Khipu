# Historical job-origin correction

Migration 0033 gave existing commitments conversation origin. Migration 0049
adds pending correction requests and conservative evidence of subsequent edits.
It does not correct or close any commitments.

`khipu owed correct-job-origin` reads a preview with counts by kind and project,
a sample, and the proposed items. `--project=example/widget` limits it to one
project. The preview uses a database read-only transaction and calls no model
or external provider.

`khipu owed correct-job-origin --request` stores that preview as a pending
request. The CLI with `--owner-direction`, and MCP `khipu_owner_act` action
`owed.correct_job_origin` with optional `project`, can only request it.
`--requests` reads pending previews.

The owner confirms with `khipu owed correct-job-origin --confirm=REQUEST`
and a yes typed on an interactive terminal. The desktop bridge exposes
`khipu_owed_origin_preview`, `khipu_owed_origin_requests`, and
`khipu_owed_origin_confirm`; confirmation uses the existing verified app-process
gate. No new desktop screen is included. `--owner-direction` cannot confirm,
including when called through MCP.

Confirmation rechecks the exact preview under locks, skips anything changed
since, and applies all eligible projects in one transaction. Opening lineage
and every surviving wording witness must agree. Missing, conflicting, owner,
or unproven witnesses exclude the item. Recorded edits, reassignment, snoozes,
closures, reopen evidence, restatements, and prior clean-up history exclude it
conservatively. Some untouched items may therefore remain for manual review.

Each correction retains its prior state in the existing per-project clean-up
batches. Undo is available through Owner actions or `owed tidy --undo=BATCH`
with the matching project and owner gate. Later edits are left alone.
Undo-of-Undo cannot replay a correction around its confirmation gate.

Work counts unmentioned commitments as quiet at 14 days, using last-seen time
(opening time when absent) and any later due date. Quiet commitments remain
open and visible; `counts.waiting` and `counts.blocking` count active work,
while `counts.quiet` counts quiet commitments. A linked active ledger need
still counts as active work.
