# Repos that get renamed (scope, 2026-10-19)

Status: approved by Matt 2026-10-19 (all three recommendations, plus the conversation rule below).

## The incident

On 2026-10-19 Matt renamed `mrkinglollipop/church-harness` to `mrkinglollipop/olivewood` on GitHub.
Khipu keys every project on the `owner/repo` slug of the checkout's `origin` remote
(`khipu.identity.resolve_repo_root`). A checkout that keeps the old remote URL goes on writing under
`church-harness`. A checkout that switches to the new URL, or a fresh clone, starts writing under
`olivewood`. From then on one project reads as two: its open commitments, decisions, search scope
and the pushed session-start slice are split between the two names. Nothing tells anyone.

The only remedy today is manual: the `project_aliases` table (migration 0021) plus the alias
readers and the `khipu project alias` command being built on `feat/project-alias-readers`. That
build is the foundation for this scope. It does not detect anything by itself.

## Findings

- The rename can be detected for certain. An authenticated `gh api repos/mrkinglollipop/church-harness`
  answers `full_name: mrkinglollipop/olivewood`, because GitHub follows its own redirect. The
  unauthenticated API answers 404 for a private repo, so the lookup needs the owner's `gh` login.
- Every checkout of a repo has the same root commit (`git rev-list --max-parents=0 HEAD`), whatever
  the remote URL or folder name. Forks share it too, so a shared root commit alone is a strong hint
  but not proof.
- The hook path has no database connection and must not gain one. It already computes the slug on
  every capture.
- The local replica (`hub_snapshot.sqlite`) has no `project_aliases` table. Readers that run from
  the replica, including the session-start slice the hook pushes, cannot expand an alias until it is
  replicated.
- Repos with no remote are keyed on the folder name, so renaming or moving the folder splits them the
  same way.

## Design

Three ways a rename shows up, each handled where the evidence already is.

1. **The same checkout reports a new name.** A checkout's identity is the (device, inode) of its git
   common dir, not its path: a moved folder keeps it, a deleted-and-recloned folder gets a new one, so
   a reused path never inherits the old repo's evidence. The hook only observes: each capture job
   carries `{checkout_key, root_commit, project, project_kind}` (`remote`, `folder` when git says
   there is no origin, or `unknown` on any git error) and the hook keeps no "last project" state; its
   only cache is the root commit, one small file per checkout key. The hub decides, in the capture
   write, from its `project_checkouts` table (the observation is recorded for every job, including
   ones whose extraction is empty; an observation older than the row is ignored; `unknown` only
   refreshes `last_seen_at`). A transition is the same checkout and root commit with a changed project:
   - folder to folder (a remoteless folder renamed or moved) and folder to remote (a local project
     pushed to a remote) are certain: applied, remote name canonical;
   - remote to folder (origin removed) is ignored;
   - remote to remote may be a fork switch: applied only if `gh api repos/<old>` confirms it,
     otherwise a `project_pairs` "asked" row is kept for the owner and nothing is merged;
   - a different root commit is never a rename. Empty repos have no root commit and never produce one.
2. **GitHub says the old name is now another name.** A daily check (in the existing nightly, off
   the hook path) asks `gh api repos/<slug>` for each GitHub project seen in the last 30 days. When
   the answer's `full_name` differs, Khipu records the alias before anyone re-points a remote. This
   is the case that would have caught olivewood the same day. Without a `gh` login the check is
   skipped and doctor says so; it never guesses.
3. **Two names share a root commit but nothing confirms a rename.** That is a fork or a rename on a
   Mac without `gh`. Khipu never merges these by itself. It raises "These look like the same
   project" with Merge and Keep separate, both in the app and in conversation (below).

**Everything works from a conversation (Matt, 2026-10-19).** The owner must never have to open the
app to check a fix or make a choice. So:

- A read tool (`khipu_project`, MCP and `khipu project show`) answers "is this one project now?":
  the canonical name, every alias with its evidence and date, and what each name holds (open
  commitments, standing decisions, episodes), grouped the way readers see them. Any agent can run
  it and paste the answer as proof.
- Pending "same project?" questions are pushed in the session-start slice for that project, so the
  agent sees them and asks the owner in chat.
- Merge, Keep separate, add, remove and Undo are owner actions through `khipu_owner_act`
  (`owner_direction`, scope 2026-10-16-owner-direction.md): the owner says it in chat, the agent
  applies it, and the action log keeps the undo.

Rules for every alias Khipu records:

- The newest name is canonical, so labels show the name the repo has now. Older aliases are
  re-pointed at it, so there are no chains (A to B to C becomes A to C and B to C).
- Stored rows keep the project name they were written with. Readers group by alias; nothing is
  rewritten, so removing an alias undoes the grouping exactly.
- The two names of a kept-separate pair are never in one group: every write to `project_aliases`
  (`record_rename`, alias add, the owner actions and their Undo) checks the two complete groups it
  would join under the one group lock and refuses when that pair would straddle them (automatic
  sources skip with a reason, owner actions say which pair and to undo it in Kept separate first).
- Every automatic alias is logged with its evidence (same checkout, GitHub, or owner), shown by
  `khipu_project` and in Settings, and undoable from either. Doctor reports the count and the
  newest one.
- `project_aliases` is replicated to the local snapshot, so replica readers and the session-start
  slice group the same way the hub does.
- Folder-keyed projects (no remote) get case 1 too, since a moved or renamed folder keeps its
  git common dir identity and root commit.
- Grouping never splits. Observation times are compared only within one checkout (one Mac's clock,
  `project_checkouts`), never across checkouts. A rename whose two names are already one group only
  relabels it (every member re-pointed at the new name); otherwise the two groups merge under the new
  name. So a late, stale event from another Mac can at worst relabel a group, never drop a member.
- Each checkout's observations are evaluated under a per-checkout lock, and each group change under a
  lock on the group's canonical name; there is no global lock.
- An observation the hub cannot record (unreachable, failed write) waits in a small local spool per
  checkout key in the capture state dir (at most 20, consecutive repeats kept once) and lands, oldest
  first, before the next observation for that checkout. A rolled-back capture is retried from the
  outbox with its observation.

Out of scope: rewriting old rows to the new name; per-project files outside Khipu (Claude's own
`~/.claude/projects/<path>` folders, Cursor rule files) beyond what Khipu itself writes; GitLab and
other hosts for case 2 (cases 1 and 3 still work there).

## Slices (one session each)

- **A. Foundation (in flight):** `feat/project-alias-readers`: alias-aware readers and
  `khipu project alias add|list|remove`. Gate it, then add the olivewood alias by hand.
- **B. Replica and same-checkout detection:** replicate `project_aliases` to the snapshot (whole table on every pull); the
  checkout observation in the hook and the hub's `project_checkouts` decision; the capture write records the alias; re-pointing so there are no chains;
  the evidence log. Hermetic tests: rename on one checkout, rename twice, folder rename.
- **C. GitHub check and doctor (built):** `khipu/project_renames.py`: the nightly `project_renames` step (before
  the replica refresh) and `khipu project renames check [--json]` ask `gh api repos/<slug>` for each GitHub project
  active in 30 days (at most 50 lookups, 3 s each) and for 'asked' pairs; a confirmed pair gets state
  `merged_github` (slice D adds the owner states `merged` / `kept_separate`). No `gh` or no login is `skipped`,
  never a failure. Doctor's `project_renames` block: aliases by source, newest alias, pairs waiting, last sweep;
  yellow only after 3 days of failed or skipped sweeps.
- **D. Conversation and app:** the `khipu_project` read tool; the owner actions (merge, keep
  separate, add, remove, undo) through `khipu_owner_act`; the "same project?" question in the
  session-start slice; then the Settings list with evidence and Undo and the in-app prompt. Mock of
  the app part first, approved before that part is built. The conversation part needs no mock.

## Decisions (Matt, 2026-10-19)

1. The newest name is canonical (labels follow the rename).
2. Same-checkout and GitHub-confirmed renames are applied automatically and can be undone. Only the
   unconfirmed shared-root case asks.
3. Old rows are never rewritten.
4. Every check and every choice in this scope works from a conversation; the app is never required.
