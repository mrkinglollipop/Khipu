# Routing exceptions that expire on their own

Status: approved by Matt 2026-10-26 ("This needs to happen automatically. Otherwise, it's gonna keep happening over and over and over again, ballooning.").

## Problem

Time-bound owner directions ("Codex checks may run 15 points over pace until tomorrow 20:30Z") are written as prose inside an account's priority role. Nothing removes them when they end; agents append "that exception ended ..." instead. By 2026-10-25 the Codex role was 4,215 characters with five ended exceptions, and every session paid for it in the routing block on every prompt ("page 14/30"). A hand tidy (version 8467) cut it to 2,842, but nothing stops it growing back.

## Design

1. **Structured exceptions.** A priority entry gains an `exceptions` list. Each item is `{text, until, at, by, source}`: the owner's direction in their words, when it ends, when and by whom it was recorded, and the conversation it came from. `until` is required.
2. **Expiry is automatic.** Every reader of the priority (the routing block pushed into prompts, `khipu route` and `--summary`, `khipu_route`, routing advice, lanes and caps logic) sees only exceptions whose `until` is in the future. Expired items leave the live view with no write; they remain readable in the priority's history (`khipu route priority history` or the existing revision record). The routing block shows each live exception's end time.
3. **No open-ended exceptions.** Recording an exception without an end time uses the account's next weekly reset as `until` (or 7 days when no reset is known), and says so in the result. An exception tied to an event ("until the branch merges") still carries a date; renewing it is a new exception.
4. **Write paths.** `route.priority.amend` gains `--exception TEXT --until T` (owner_act, CLI, MCP) to add one exception to one account; `--end-exception ID` ends one early. Owner-direction and conversation-request semantics, `--expect`, undo and history are the same as for amend.
5. **Role size budget.** A role (the standing text) has a budget of 2,000 characters. A `set` or `amend` whose resulting role exceeds it, or grows a role that is already over it, is refused with a message naming the budget and pointing at `--exception ... --until ...`. Roles already over budget keep working and show a warning in `khipu route`, `khipu doctor` and the routing block until trimmed.
6. **Old data.** Priorities stored before this change read unchanged (no exceptions list = empty). No prose is parsed or moved automatically; moving today's live prose exceptions into structured items is a one-time owner-direction edit after deploy.

## Out of scope

Parsing existing prose for dates; changing rules (standing rules hold only what never changes); lanes-runner configs in other repositories, which read the routing record.
