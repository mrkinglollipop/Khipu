# S7 gateway prerequisite: partial implementation

S7 is incomplete. This change implements per-label gateway tool scope and
adds a regression test for the existing `khipu_needs_you` gateway refusal.
Commitment ownership and recall exclusion hit the approved stop conditions
below and remain unchanged. No schema migration, deployment, real credential,
provider call or live database access was performed.

## Implemented

`KHIPU_GATEWAY_TOOLS='label:tool1|tool2;label2:*'` restricts both `tools/list`
and `tools/call`. The call check runs before looking up the tool handler.
Configured labels deny every tool not explicitly allowed; local-only and
unknown tools remain unavailable even when named. An empty list denies every
tool. Wildcards cover existing gateway tools only. Labels omitted from a valid
setting, and all labels when the setting is unset or empty, retain their prior
behavior. Local stdio is unchanged.

Malformed policies deny all gateway tools, rather than silently dropping a
restriction. A configured policy also refuses requests lacking an authenticated
label. Refusals contain fixed error text; each refusal logs the label and tool
name as JSON strings, with no arguments, results or credentials. The setting is
read on every list and call. Syntax and operational behavior are documented in
[operations](../operations.md#the-gateway).

Hermetic regression cases cover an allowed call, listing, every forbidden tool
handler, explicitly named local-only and unknown tools, wildcard scope, empty
and malformed policies, label isolation, policy changes between list and call,
unconfigured compatibility, local stdio, and the existing `khipu_needs_you`
internal refusal. The HTTP test checks primary and labelled tokens and multiple
calls in a batch using fake tokens and handlers.

## Blocked: commitment ownership and ask provenance

[Migration 0033](../../ops/migrations/0033_commitment_owner.sql) defines
`commitments.origin` as `conversation` or `job` and enforces those values with
`commitments_origin_check`. This is session lineage, not authenticated writer
provenance. No later migration supplies a commitment gateway-writer origin.

[explicit_asks.py](../../packages/cli/khipu/explicit_asks.py) writes
`origin='conversation'`, a fixed `owner_reassigned_by='explicit_ask'`, and no
authenticated caller label. Its `requester` is caller-supplied text. Source
`episode_id` is optional and, when supplied, is not checked for gateway ownership;
its presence cannot establish who filed the ask. The `needs_you` rendering also
identifies these asks as originating in a conversation.

Consequently there is no stored `gateway:<label>` commitment origin against
which to authorize close or snooze. The approved brief explicitly requires
stopping this item rather than inventing a column or migrating the hub. No
owed-update or ask behavior was changed. Gateway close-on-owner and
close-on-own-origin tests cannot be implemented truthfully against the current
contract; local stdio close was not changed.

A subsequent approved scope must decide the commitment's authenticated-writer
representation, including asks without a source episode, retain the existing
conversation/job lineage, and define the hub migration and writer/read behavior.
That scope must include server-stamped ask attribution visible to the owner and
an ownership check in the same transaction as close/snooze, covering single and
bulk changes and replay. Those changes are not included here.

## Blocked: recall and replica provenance

[hub_snapshot.py](../../packages/cli/khipu/hub_snapshot.py) omits episode
`origin` from `_EPISODE_COLS` and from the SQLite `episodes` table created by
`_create_schema`. The full dump (`_insert_episodes`) and incremental pull select
from `_EPISODE_COLS`, so the local replica drops the hub's episode origin.
[recall_prompt.py](../../packages/cli/khipu/recall_prompt.py) uses this replica
for recall search and standing decisions.

The brief explicitly says to stop this item if recall's replica has lost the
origin field. No recall filtering, replica schema changes or setting were added.
Explicit `khipu_search` is unchanged. The gateway/local same-text recall test is
blocked by the missing origin representation; it was not replaced by an
assumption based on a session prefix or caller-supplied content.

A subsequent approved scope must carry server-stamped episode origin through
full and incremental replica writes, decide how older replicas are upgraded or
handled, then filter gateway-origin recall by default behind the requested
setting across the recall paths. This requires explicit additional scope before
implementation under this brief.

## Verification

Verification results are recorded after the finite local oracle completes.
S7's cross-family review remains required before merge. Nothing was pushed,
merged or deployed.
