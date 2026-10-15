# Settings clarity (mock, 2026-10-15, approved by Matt)

Static mocks for `docs/plans/2026-10-15-settings-clarity.md`. Open any `.html` in a browser; it follows the
system appearance. `App.css` is a copy of the app stylesheet on main; each screen's mock-only layout is in its
own stylesheet (`capture-models.css`, `plans.css`, `routing.css`, `onboarding.css`). `folds.css` and `folds.js`
(mock-only) make every fold a button with `aria-expanded` that works by mouse and keyboard. All accounts, projects,
rules and times are fictional. Times are Thursday 2026-10-15 at about 3:40 PM. "Before" files show today's app
(v0.4.15); the others show the design (v0.5.0). Every file is laid out for 1280 and 960 wide (960 is the app's
smallest window); files with several states stack them as labelled panels.

Matt's points, and where each is answered:

1. Capture & models, cleaner, especially models: `capture-models-before.html`, `capture-models.html`.
2. The Plans section "not attached to anything", Grok's, with errors: `plans-before.html` (today's Home card
   and today's Plans card inside Capture & models, with Grok's rows that never name Grok), `plans.html`.
3. Settings → Accounts and routing too long: `routing-before.html` (today's page, long on purpose), `routing.html` (the
   folded overview), `routing-open.html` (sorting, filtering, one account open, the rules open),
   `routing-states.html`.
4. "Watched" and "Not watched by Khipu": the rules in `routing-open.html`.
5. New and updating users: `setup-before.html`, `setup-accounts.html`, `whats-new-before.html`,
   `whats-new.html`.

Polish items (a) switches off while loading, (b) "Live" beside "no turn observed yet", (c) negative ages:
`polish.html`, with (b) also in `plans.html`.

Files:

- `capture-models.html`: three cards (When Khipu saves a conversation, Commitments, Session summaries with
  the providers as rows) and Keys on this Mac; Plans is gone from this page. States: a refused key, a local
  model, a key set by the environment, the capture setting locked by the environment, the daily limit reached.
- `plans.html`: Home's Plans card with one status phrase per account (Grok Build paused by you and not
  readable, Muse with no figures yet), then Settings → Accounts and routing → Accounts with each account's plan block
  inside it. States: the last read failed, never read, not signed in, earlier figures kept, no accounts.
- `routing.html`: Needs you at the top, Open all and Close all, Accounts open with sort and filter chips, every
  other group closed with its summary and its problem tags.
- `routing-open.html`: one account open with its disagreement and its plan; the standing rules open with the
  check result under each rule Khipu checks and a grey line under each it can't; Telling conversations with
  its inner folds; Recent changes filtered to today.
- `routing-states.html`: loading, the record can't be read, no record on this Mac, nothing needs you, an
  account that can't be read, a paused account, no holds.
- `setup-accounts.html`: the new setup step "Your accounts" (six of seven). States: nothing found, still
  reading, the Welcome page's updated list.
- `whats-new.html`: the dialog after an update, listing everything since the last version shown, each with
  Show me; the same news as a card on Home that stays until closed; the same with "Set up accounts" when routing was never set up; Settings → Updates with the What's
  new history.
- `polish.html`: the three fixes, before and after.

Phase 9 (addendum, finding and adding accounts; mock-only layout in `add-account.css`):

- `add-account.html`: where Add an account starts, then the dialog's three steps and every state: choose the
  service, service not installed, the logins Khipu found, nothing found besides what is tracked, sign in to
  another account, sign-in in progress, sign-in failed, Grok Build (sign-in can't start from Khipu), and the
  read before saving (Live, signed in but unreadable, not signed in, duplicate login, name already used).
- `add-account-found.html`: Home's "Found a Claude account you use" notice, Accounts with two Found rows, Not
  offered again, the Home notice when Track new accounts automatically is on, and the setup step listing every
  login found with Add another account.
- `add-account-manage.html`: rename in place, Stop tracking with its choices (priority, pause, lanes, holds) and
  what it does not change, and two accounts that read the same login.
- `add-account-states.html`: states added after the cradle-to-grave review: a moved folder, a folder that
  couldn't be checked, a missing login folder, signed out with earlier figures, a plan that changed after
  signing in again, newly tracked and not ranked, a CLI Khipu can't run, nothing installed, the Grok sign-in
  code and no code, rename with what is and isn't updated, stopping the last account, no accounts tracked,
  Looking…, and setup with a signed-out default login and with nothing installed.

Decisions in the mock:

- Folding never hides a problem: a closed group shows the count and colour of each problem in it, and Needs
  you lists them all with a button that opens the group at the item.
- Nothing is removed. Plans, the commitment settings and the keys move; groups and long lists fold; every
  control from the before files is still on some screen.
- Sorting and filtering change only what the page shows. Your priority changes only with the Priority
  controls.
- One failure, one message, in words, inside the account it belongs to; the short code only under Details.
- "Watched" becomes what Khipu actually does: "Checked by Khipu" with the last result, or a grey line saying
  the rule is passed on as written and Khipu has no way to check it.
- What's new appears after an update as a dialog and as a card on Home, so closing one by accident leaves the
  other; Settings → Updates keeps every item.
- Every readable account line shows its spend pace under the week figure, and the sort includes spend pace.
- A paused account's plan is still read.

Not shown: the Work tab and its own setup line, which the Work scope adds (`2026-10-15-work-view.md`).
