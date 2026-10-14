# Promotion gate: merged is not live until a command says so

Approved in principle, Matt 2026-10-13 ("I think you should proceed as you recommend") and 2026-10-14
("Proceed as you recommend"). This page is the scope; a build session reads it first.

## Status

| Phase | What | State |
|---|---|---|
| 1 | A pinned live checkout; every launcher, launchd job and MCP server resolves into it; `khipu promote` flips it after a live gate; `--rollback` flips back | open |
| 2 | `khipu doctor` and the app's Home show "merged, not live" drift and the last promotion's gate result | open |

Two PRs, phase 1 first. Phase 1 is itself the first change promoted through the gate.

## The defect, in one paragraph

On this Mac the live Khipu is whatever commit the main checkout is on. Every launcher in
`~/.config/khipu/bin` is a symlink into `packages/cli/bin` of the main checkout and derives its root from its
own path (`khipu.paths.repo_root`, three levels up from `paths.py`, or `KHIPU_ROOT`); the launchd plists
carry a `PYTHONPATH` rendered from the same root (`launchd_gen.render_context`); the MCP server is
`~/.config/khipu/bin/khipu-mcp`. So `git pull --ff-only` in the main checkout is the deploy, and it happens
with no check of any kind. Every regression this month was that: the launchd drain's bare `PATH` could not
find `claude` the moment #161 was pulled (four minutes of failed captures until #163), the Stop hook's inline
drain held every turn end for a median of 10 s from the moment #160 was pulled (until #171, a day later),
and the 2026-10-12 nightly wiped the imported library on code that had passed the whole test suite. The
test suite is hermetic by design (temp `HOME`, no keychain, no network); the things that broke were the
seams it cannot see: the launchd environment, the installed CLIs, the live replica, the real hub.

## Design

**Two live slots and a symlink.** `~/.config/khipu/live` is a symlink to one of two git worktrees of the
Khipu repository, `~/.config/khipu/live-a` and `~/.config/khipu/live-b` (worktrees of the main checkout's
repository, each pinned to a commit, never a branch that moves). The five launchers link into
`~/.config/khipu/live/packages/cli/bin/`; the plists render their `PYTHONPATH` and `KHIPU_ROOT` from
`~/.config/khipu/live`; the MCP server entry stays `~/.config/khipu/bin/khipu-mcp` and follows the link.
The main checkout becomes a plain development checkout again: pulling it changes nothing that runs.
`KHIPU_ROOT` is honoured everywhere already, so a second machine or a release build is unaffected.

**`khipu promote [<ref>]`** (default `origin/main`): fetch; check the inactive slot out at the ref; run the
live gate below against that slot; on pass, flip the symlink atomically (`ln -sfn` to a temp name, `mv`),
kickstart `com.khipu.queue-drain` and `com.khipu.recall-daemon` (the long-running ones that hold the old
module), re-render and reload any plist whose `PYTHONPATH` changed, and record the promotion in
`state/promotions.jsonl` (ref, commit, gate result, timings, who). Running MCP servers keep the old code
until their session restarts; say so in the output. **`khipu promote --rollback`** flips to the other slot
with the same kickstarts and no gate. **`khipu promote --status`** prints live commit, main commit, drift,
and the last gate result.

**The live gate** (each step named in the output with its time; any failure stops the promotion and leaves
the symlink alone):

1. Import and doctor from the candidate slot: `python -m khipu doctor --quick` under the candidate's
   `PYTHONPATH`, with the launchd environment (`launchd_gen.render_context`, bare `PATH`), so a missing
   binary or module shows here and not at 06:05Z.
2. Per-prompt recall replay: the candidate's `khipu-prompt-recall` run as the hook would run it, on five
   real prompts sampled from the last day's `prompt-recall.log` (the hook's own log is the only truth for
   that lane), against the live replica, each inside the 1.2 s budget; compare the returned ids with the
   current live code's answer for the same prompts and refuse on an empty answer where live had one.
3. One real capture end to end: queue a fixture transcript (a redacted window from `tests/fixtures`,
   marked `gate: true`) through the candidate's `khipu-stop-hook`, drain it with the candidate code, see
   the episode land in the hub, then `khipu forget` it. This exercises the summariser provider, the
   fallback, the outbox and the hub write with the real keys and the real CLIs.
4. Version and schema: the candidate's `khipu.__version__` is not older than live; the replica schema the
   candidate expects is the one on disk, or the candidate can refresh it.

The gate is the four seams that broke, not a second test suite; it must finish in under two minutes or it
will be skipped in practice.

**What the gate does not do:** it does not run the nightly, build the app, or touch the gateway; those keep
their own deploys ([[merged-is-not-deployed]]). It does not promote on a schedule; a merge waits until
someone runs it. It never pulls the main checkout.

## Phase 1: one build session

Inspect `khipu.paths.repo_root`, `launchd_gen.render_context` / `install_job` / `plist_current`,
`cli.py` `jobs` and `doctor`, `packages/cli/bin/*` and `khipu integrations install` (which writes the
launcher symlinks) before writing anything. Build:

- `khipu/promote.py`: slots, `promote`, `rollback`, `status`, the four gate steps as separate functions
  that each return `{ok, ms, detail}`; `state/promotions.jsonl`.
- `khipu promote` subcommand; `khipu integrations install` and `khipu jobs install` learn the live root
  (the `live` symlink when it exists, else the checkout they run from, so a user who never promotes sees
  no change).
- Migration for this Mac, run once by hand and recorded in Khipu-ops: create the two slots, point the
  launchers and plists, promote the current main. Stop-condition: if any launcher is a plain file rather
  than a symlink, stop and ask.
- Tests: slot flip is atomic and reversible; a failing gate step leaves the symlink untouched; the gate's
  replay reads only the hook log and never a credential; `integrations install` on a machine with no
  `live` link behaves as today (the public install path must not change).
- Verify with the hermetic suite, then by promoting phase 1 itself through its own gate on this Mac and
  reading the next nightly.

## Phase 2: visibility

`khipu doctor` gains `promotion: {live_commit, main_commit, behind_by, last_gate}` and a warning when
main is ahead of live by more than a day; the app's Home shows "Live: <short sha>, merged: <short sha>"
with a Promote button that runs the gate and shows each step's result (it is the same command, through
the fixed-argv allowlist). Health report row. Settings → Advanced: the slot paths, read only.

## Decisions

- Two slots, not N: a rollback needs exactly one known-good predecessor; older ones are in git.
- Worktrees of the same repository rather than clones: one object store, one fetch, and `git worktree
  list` shows the live slots next to the session worktrees ([[khipu-live-launchers-follow-main-checkout]]).
- The gate replays real prompts from the hook log rather than a golden set: the golden set is for
  quality (`recall-golden.jsonl`), the gate is for "does the lane answer at all under the live
  environment".
- The capture step writes one real episode and forgets it rather than mocking the hub: the hub write and
  the provider call are exactly the seams that have broken.
