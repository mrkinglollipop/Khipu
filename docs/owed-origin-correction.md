# Historical job-origin correction

Migration 0033 gave existing commitments conversation origin. Migration 0049
adds pending correction requests and conservative evidence of subsequent edits.
It does not correct or close any commitments.

`khipu owed correct-job-origin` reads a preview with counts by kind and project,
a sample, and the proposed items. `--project=example/widget` limits it to one
project. The preview uses a database read-only transaction and calls no model
or external provider. Preview and pending-request reads do not change local
reachability state or migrate legacy secrets, even when connection setup fails.

`khipu owed correct-job-origin --request` stores that preview as a pending
request. The CLI with `--owner-direction`, and MCP `khipu_owner_act` action
`owed.correct_job_origin` with optional `project`, request it without applying it.
`--requests` reads pending previews.

The owner confirms from a recent, recorded owner prompt with
`khipu owed correct-job-origin --confirm=REQUEST --owner-direction`, or MCP
`khipu_owner_act` action `owed.correct_job_origin.confirm` with `request`.
The existing owner-direction gate binds the action log to that exact request,
recorded prompt and calling harness. Missing, stale, dispatched-job or foreign
prompt evidence is refused. A terminal `yes` cannot authorize correction. The desktop bridge exposes
`khipu_owed_origin_preview`, `khipu_owed_origin_requests`, and
`khipu_owed_origin_confirm`; confirmation uses the existing verified app-process
gate. No new desktop screen is included.

Confirmation rechecks the exact preview under locks, skips anything changed
since, and applies all eligible projects in one transaction. Opening lineage
and every surviving wording witness must agree. Every witness session,
including the opener, is excluded when another capture of that session gives
contradictory parent lineage. Missing, conflicting, owner,
or unproven witnesses exclude the item. Recorded edits, reassignment, snoozes,
closures, reopen evidence, restatements, and prior clean-up history exclude it
conservatively. Eligible historical rows must have opened during the protected
interval from migration 0035 (ownership metadata coverage) through migration
0049 (the correction boundary). All pre-0035 rows are quarantined because an
unrecorded owner assignment cannot be ruled out; null later metadata is not
proof of untouched ownership. Preview reports this count under
`quarantined.ownership_unestablished`. Some untouched items therefore remain
for manual review. Previously stored previews are revalidated against this
policy and cannot apply an older, unproven candidate.

Each correction retains its prior state in the existing per-project clean-up
batches. Undo is available through Owner actions or `owed tidy --undo=BATCH`
with the matching project and owner gate. Later edits are left alone.
Undo-of-Undo cannot replay a correction around its confirmation gate.

Work counts unmentioned commitments as quiet at 14 days, using last-seen time
(opening time when absent) and any later due date. Quiet commitments remain
open and visible; `counts.waiting` and `counts.blocking` count active work,
while `counts.quiet` counts quiet commitments. A linked active ledger need
still counts as active work.
