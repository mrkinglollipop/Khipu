# Settings sync and answering from either Mac

Branch `feat/settings-sync`. Plan: `docs/plans/2026-11-03-settings-reorg.md` (Settings sync, slices F and G).
This replaces the earlier checkpoint, whose claims were not backed by a full run (one existing test was red).

## What it does

- **Settings** the owner changes on one Mac (the closed `settings_syncable` list: no secrets, addresses, paths, login
  folders, launchd jobs, machine limits or switches that decide what leaves a Mac) travel as `setting_set` lines through
  the routing log, `_sync` and `routing_events`. The receiving Mac applies each by event id once, never over a later
  change of its own, never from a first-switch-on snapshot, and logs it to `owner-actions.jsonl` (`auth: other_mac`) so
  Undo works there. An Undo is itself the owner's change and follows to the other Mac.
- **First switch-on** lists differences on Your Macs; nothing is overwritten until chosen. "Keep X" / "Use Y" per row and
  "Use the other Mac's for all" are ordinary owner sets (`khipu config --set`), so they are logged and undoable.
- **Waiting on the owner** (held requests, a conversation's setting request) shows on every Mac: the origin publishes
  `request_state`; the other Mac keeps a mirror row in its held-requests file, so Needs you, the app and the CLI list it.
  His yes or no is proved on the Mac he typed on (conversation turn, app, or a typed yes on a terminal) and sent as
  `request_answer`. The Mac holding the request applies it once with its own owner handling. A repeat does nothing; an
  answer to a request that already ended says so on the answering Mac. Words travel only under the existing
  `routing_proposal_text_between_macs` switch and are withheld for reviewed files or secret-shaped text.
- **Copy switch from either Mac.** An owner set of `routing_replication_live` emits a tiny `settings_sync_switch` line. A
  Mac with the copy off reads only that record, at most once per timer tick (270 s), and sends only its own switch lines.
  Limits: the hub must be reachable and the timer must run (a Mac that cannot reach the hub cannot hear it); it takes up
  to one tick.
- **Retention.** Compaction never drops owner lines and `setting_set` is not a droppable kind. A replay of the whole hub
  history after day 120 does not re-apply an old change (applied marker per key).
- **Migration 0051 removed.** It only added a speed-up index for the off-state poll; nothing depends on it. An older Mac
  on `origin/main` reads the new lines without harm (tested against real code from `origin/main`).
- **Account warnings.** A Mac with no account list raises no untracked warning; an account the owner stopped still warns;
  one id per account (`codex:main`, never also `codex|`).

## Answering

- Conversation: `khipu_needs_you`, then `khipu_owner_act direction.confirm|decline` on the mirror id.
- CLI / app: `khipu owner-direction answer REQUEST --yes|--no [--from-app]` (Tauri command `khipu_owner_answer`; Yes/No
  buttons on Your Macs).

## Tests

`tests/test_settings_sync_pg.py` (two Macs, real throwaway PostgreSQL), `tests/test_settings_sync_retention.py`,
`tests/test_routing_s11b_replication.py`. Results are in the job's final report.
