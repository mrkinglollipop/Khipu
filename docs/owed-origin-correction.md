# Historical job-origin correction

Migration 0033 gave existing commitments conversation origin. Migration 0049
adds pending correction requests and conservative evidence of subsequent edits.
It does not correct or close any commitments.

`khipu owed correct-job-origin` reads a preview with counts by kind and project,
a sample, and the proposed items. `--project=example/widget` limits it to one
project. The preview uses a database read-only transaction and calls no model
or external provider. Preview and pending-request reads do not change local
reachability state or migrate legacy secrets, even when connection setup fails.
Parser defaults and cold ownership-alias reads also avoid local account
recovery while retaining the configured values.

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
conservatively. Historical eligibility requires positive per-row proof that ownership was
untouched. Migration dates and null reassignment fields cannot provide this:
a long-lived writer can retain pre-migration schema metadata and omit evidence.
No current historical row has a supported certificate, so all historical
candidates are quarantined for manual review. No migration backfills trust.
Preview reports this under `quarantined.ownership_unestablished`; old stored
previews are revalidated and cannot bypass this floor. Synthetic lifecycle
tests inject explicit test-only per-row evidence to exercise confirmation and
Undo; that evidence is never available to the production command.

Each correction retains its prior state in the existing per-project clean-up
batches. Undo is available through Owner actions or `owed tidy --undo=BATCH`
with the matching project and verified app or prompt-bound owner gate.
A terminal confirmation cannot authorize correction Undo. Ordinary clean-up
Undo retains its existing terminal compatibility. Add `--ids=ID,ID` to restore selected
commitments; unselected items remain corrected and can be undone later. The
owner-directed `owed.tidy` action accepts the same selection as an `ids` list
alongside `project` and `batch`. Later edits are left alone.
Undo-of-Undo cannot replay a correction around its confirmation gate.

Work counts unmentioned commitments as quiet at 14 days, using last-seen time
(opening time when absent) and any later due date. Quiet commitments remain
open and visible; `counts.waiting` and `counts.blocking` count active work,
while `counts.quiet` counts quiet commitments. A linked active ledger need
still counts as active work. The desktop badge totals active and quiet work,
with a label explaining both counts. The Work view separates those counts,
marks quiet rows, and excludes them from its active Blocking filter.

See the [fix verification record](research/2026-10-26-owed-origin-fix-verification.md)
for regression evidence and the remaining rendered-verification limitation.

Machine entrypoints never count as owner turns, even when older configuration
lists them as trusted. Authorization-setting changes (including their Undo)
require the verified app or an already eligible, recent owner prompt; terminal
confirmation cannot widen the window or grant trust.

If the correction COMMIT reply is lost, the action reconciles against the
durable request and retains its exact batch IDs. A confirmation retry returns
those IDs without changing rows again. An unavailable outcome stays
unconfirmed, and Undo waits for durable resolution. A confirmed rollback is
reported as failed rather than an applied correction.
