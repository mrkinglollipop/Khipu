# Routing: what each account can take right now (mock, 2026-10-14, revised after review; approved 2026-10-14)

Static mocks for showing routing state in the desktop app, for `docs/plans/2026-10-14-routing-truth.md`. Open
any `.html` in a browser; it follows the system appearance. `App.css` is a copy of the app stylesheet on main;
`mock.css` holds the mock-only layout, plus the Plans card styles copied from `feat/plan-usage-p2` until that
branch merges. Times are Wednesday 2026-10-14 at about 11:40 AM, New York time (the states mock is at 3:05 PM).

- `home-routing.html`: Home with a Routing card under Plans. One row per account: Claude main at its limit
  until 1:52 PM, read from Claude, with Check now and "Khipu checks again at 1:54 PM"; Claude second at its
  limit until 2:52 PM, from Claude's own message seen by the Project A job runner, which can't be checked
  without using a turn; Codex open, read from Codex; Grok Build paused by you until Friday; Muse at its weekly
  limit, reported by a conversation. Below them: the holds with their end and who set them, your note to every
  conversation in your words with its date, the seven standing rules folded (six still to confirm), and
  "Khipu reports this; your agents decide where work goes."
- `home-routing-conflict.html`: two sources disagree about Claude second. The job runner saw Claude's own
  limit message; a conversation in project-b later reported the account open. Claude's own message
  outranks a conversation's report, so the limit shows, both are named, and conversations are told both. A
  health-check warning says a standing rule may be broken (the job runner's settings name a model ending in
  -fast) and that nothing was stopped.
- `home-routing-states.html`: the other states a row can show (a model-scoped limit, a reset that can't be
  checked, probably open, a guess, down with its reason, unknown, a busy Mac, a record that stopped
  refreshing, no record on this Mac), the attention item for a job runner that stopped reporting, and the
  Routing row in the health report.
- `settings-routing.html`: Settings → Routing with every option the scope lists. Accounts (Check now, Mark
  paused until… with one form open, Lift, Clear, and Dismiss one side of a disagreement); your note; the
  standing rules with date, source, "Seeded, confirm", Watched or Not watched, Confirm, Edit and Remove, one
  removal asking to confirm and an Add asking to confirm; holds (list, add, end, and how long they last);
  checking for resets with its waits and retries; telling conversations (one master switch, one switch per
  moment, what each harness hears, and every size and time with Change…, one shown open with "No limit");
  job runner folders, one stopped; which account each job lane uses, one lane not matched; the routing table
  file; the copy for cloud agents; and recent changes, one list per day.

Decisions in the mock:

- Khipu reports; it never routes. It records, reconciles and tells conversations. No control on these
  pages sends, stops, refuses or reorders work. Mark paused, Clear and Dismiss change what Khipu reports,
  nothing else, and the pause form says so.
- Every limit, pause and report can be ended on the spot by you. Clear treats the account as open until
  something newer reports on it; Dismiss drops one report. If Claude or a job runner reports the limit again,
  it comes back.
- When two sources disagree, the stronger one shows (Claude's own message or a plan check outranks a
  conversation's report) and both are named, on the card and to conversations. Nothing wins silently. The
  routing table is never a source of an account's state; it shows only as "NOW block changed".
- An account is never shown open without saying who saw it and when. With no report, or only old ones, it
  reads Unknown.
- Standing rules come first and whole to every conversation, then your note, word for word. Khipu does not
  enforce rules; a rule that looks broken is a warning on Home and in the health report that blocks nothing,
  and a rule Khipu has no check for says "Not watched by Khipu".
- The words of a hold or report written by a conversation show in the app and when a conversation asks;
  other conversations are told only who, what kind and until when. Your own holds are sent word for word.
- How soon a conversation hears a change is stated per harness: Claude Code and Codex at the start and
  before each prompt; Cursor at the start only; Grok Build and Aegis when they ask; cloud agents from the
  copy. The switches are per moment, not per harness.
- The account rows use the same names and order as the Plans card, and the name column lines up with it.

Changed decision (owner approval, 2026-10-14): Khipu checks an account again just after each known reset
time. This replaces, for this one case, plan usage's decision "Cache with a short max age rather than a
daemon" (`docs/plans/2026-10-14-plan-usage.md`, Decisions). A check is one plan read and no model call: 2
minutes after the reset time, then 10 and 30 minutes after if the limit is still there, so at most three per
reset. It covers accounts that are at their limit; open accounts are not checked, because a reset changes
nothing for them. Claude second and Muse can't be checked without using a turn, and their rows say so.

Approved by the owner on 2026-10-14 ("Looks good. Proceed."). The approval confirms what this page asked to
confirm: that the reset check covers only accounts at their limit; the seven seeded rules and their wording;
and that your note is where your direction on where work should go lives.

Tagged "Comes later" in the mock: the reset check and Check now (phase 5), the switch before handing work to
another agent (phase 6), and the copy for cloud agents (phase 7). The first app release (phase 4) leaves those
controls out rather than showing controls that do nothing.

Keyboard and screen reader notes: Mark paused until… keeps its button in the row with `aria-expanded` and
`aria-controls`; opening it moves focus to the first field, and Cancel or Escape returns focus to the button.
Rule numbers are hidden from screen readers and the list keeps `role="list"`. Each switch's tag is part of its
description. Recent changes and the lane lists have a heading above each list instead of inside it.

Left in the job runner's own project, not Khipu: the runner's bugs (no Codex limit detection, a killed Grok
check filed as unclear, settings read once at start, Muse reset time misread) go back to that project as
tasks.
