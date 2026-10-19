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

1. **The same checkout reports a new name.** Khipu keeps a small local record, per checkout (real
   path plus root commit), of the last project name it reported. When the name changes on the same
   checkout, that is a rename: certain, local, no network. The capture that sees it records the
   alias with the new name as canonical.
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
- Every automatic alias is logged with its evidence (same checkout, GitHub, or owner), shown by
  `khipu_project` and in Settings, and undoable from either. Doctor reports the count and the
  newest one.
- `project_aliases` is replicated to the local snapshot, so replica readers and the session-start
  slice group the same way the hub does.
- Folder-keyed projects (no remote) get case 1 too, since a folder rename on the same checkout
  keeps the root commit.

Out of scope: rewriting old rows to the new name; per-project files outside Khipu (Claude's own
`~/.claude/projects/<path>` folders, Cursor rule files) beyond what Khipu itself writes; GitLab and
other hosts for case 2 (cases 1 and 3 still work there).

## Slices (one session each)

- **A. Foundation (in flight):** `feat/project-alias-readers`: alias-aware readers and
  `khipu project alias add|list|remove`. Gate it, then add the olivewood alias by hand.
- **B. Replica and same-checkout detection:** replicate `project_aliases` to the snapshot; the
  per-checkout name record; the capture write records the alias; re-pointing so there are no chains;
  the evidence log. Hermetic tests: rename on one checkout, rename twice, folder rename.
- **C. GitHub check and doctor:** the nightly `gh` lookup; doctor lines for aliases found, check
  skipped (no `gh`), and lookup failures.
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
