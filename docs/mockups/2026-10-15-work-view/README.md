# Work: the ledger and the live view (mock, approved by Matt 2026-10-15)

Static mocks for `docs/plans/2026-10-15-work-view.md`. Open any `.html` in a browser; it follows the system
appearance. `App.css` is a copy of the app stylesheet on main; `mock.css` holds the mock-only layout. All
projects, pull requests, rows and people are fictional. Times are a Thursday at about 3:40 PM, a few weeks
after two projects switched their trackers to Khipu.

- `work.html`: the Work tab at 1280 wide. Waiting on you with 23 items (8 shown, 3 blocking) from the three
  sources: ledger rows that need you, Owed items that need you (one with a "Looks like" suggestion), and a
  routing rule to confirm; one item that is in both the ledger and Owed shows once. Running now with five jobs:
  one short status line each (opening a job shows up to its last 3), one quiet for 18 minutes, one waiting to start on an account at its limit, one
  from a project whose summary is out of date. Accounts, as on Home. Boards: Atlas (goals, loose ends, a
  flagged pull request with its verdict, a merge the ledger has not caught up with, a release train), Harbor
  (a flagged pull request), Quarry (summary out of date), Lumen (a ledger and no job runner) and Pebble
  (folded: nothing running, nothing waiting).
- `ledger.html`: one project's ledger. Rows grouped by kind, then parent, in the order they were added; filters;
  a row whose parent is missing; one Needs you row open with its fields, the change form (answering it closes
  the linked Owed item too) and its whole history, imported entries included. A notice that two changes are
  waiting for the database.
- `work-states.html`: first run with no projects; nothing waiting and nothing running; a summary file in each
  state (out of date, stopped reporting, missing, too big, unreadable, newer format, wrong project, inside a
  private folder, items left out); the database away; an old tracker changed after import; the health row.
- `settings-work.html`: Settings, Work. Ledger projects (rename, hide; no delete); Import a tracker with its
  preview before anything is written (in phase 6 every active ledger is imported together); the last import's check (counts and a field-by-field read back); old
  tracker files watched for changes; summary files (change, turn off, remove, add with a first read, a
  suggested one, the switch for what jobs say they are doing); limits with Change…, one open showing No limit;
  hidden items.
- `narrow.html`: the Work tab, a ledger and Settings at 960 wide, the app's smallest window. Job rows stack
  their account under the title, boards drop to one column, and a ledger row's detail opens below the list.

Decisions in the mock:

- Khipu keeps the ledger and shows what job runners report. No control here starts, stops, retries, merges or
  moves work. The only writes are to Khipu's own ledger, Owed and settings, each recorded with who made it.
- A ledger row is never moved. Changing it adds history with the old value. Remove (decided after these mocks
  were drawn, so not shown) takes a row out of every list at once; Matt can restore it for 30 days in a Removed
  list, then it is purged. See the scope's "Removing a row".
- A need is a ledger row, so it can be closed here. Items in both the ledger and Owed show once; items that
  only look alike are offered as "These are the same", never merged silently.
- Where the runner and the ledger disagree (a merge the ledger has not recorded), both are shown; Khipu changes
  neither.
- Every limit has Change… and, where it makes sense, No limit.
