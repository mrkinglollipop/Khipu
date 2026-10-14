# Promotion gate: merged is not live until a command says so

Approved in principle, Matt 2026-10-13 ("I think you should proceed as you recommend") and 2026-10-14
("Proceed as you recommend"). This page is the scope; a build session reads it first.

## Status

| Phase | What | State |
|---|---|---|
| 1 | A pinned live checkout; every launcher, launchd job and MCP server resolves into it; `khipu promote` flips it after a live gate; `--rollback` flips back | built, awaiting migration (branch `feat/promotion-gate-p1`) |
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
   real queries sampled from the last day of Khipu's own `query_log.jsonl` (any harness, non-empty
   `query`, de-duplicated, secrets masked with the capture path's masker), each wrapped as the harness's
   UserPromptSubmit stdin JSON, against the live replica, each inside the 1.2 s budget; compare the
   returned ids with the current live code's answer for the same queries and refuse on an empty answer
   where live had one. No usable line from the last day fails the step ("no queries in the last day; use
   Khipu once and re-run"); it never passes on missing evidence.
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
  replay reads only `query_log.jsonl` and never a credential; `integrations install` on a machine with no
  `live` link behaves as today (the public install path must not change).
- Verify with the hermetic suite, then by promoting phase 1 itself through its own gate on this Mac and
  reading the next nightly.

### As built

- `packages/cli/khipu/promote.py`; `khipu promote [ref] [--rollback] [--status] [--gate-only STEP] [--json]`;
  `khipu doctor --quick` (see below).
- The replay samples from `query_log.jsonl` (the hook's `prompt-recall.log` holds no prompt text, and
  prompt text is not logged there). Each run's hit ids, reason and time are read from that run's own
  hook log line.
- Isolation is one variable, `KHIPU_ISOLATED_DIR` (`khipu.paths`): when set, `data_dir()` is that directory
  (config, outbox, hub replica, decision sync, embedding cache and budget state), `khipu_home()` is its
  `capture/` (queue, hook state, heartbeats, hook logs), the stop hook logs into its `logs/`, the file-wiki
  path settings (`memory_root`, `memory_repo`, `capture_v2`, `graph_sqlite`) read as unset and the file-wiki
  leg is skipped. Credentials are still found the live way: Keychain, key files named in `config.json`, and the
  DSN and root certificate read from the configured data dir (`paths.credential_dir`). The gate seeds each
  isolated dir with the live `config.json` minus the file-wiki paths and any secret-named value.
- The capture step's isolated dir is `~/.config/khipu/state/gate-runs/<session>`. Its session id is written
  there (`gate.json`) and to `state/promotions.jsonl` as `cleanup_pending` before the stop hook runs; the
  hook runs in gate mode (`KHIPU_GATE=1`: `hook_background.run` drains the isolated queue and skips the mirror,
  outbox replay, notes reconcile and embed catch-up); the forget runs in the same dir. The only write outside
  it is the one gate episode in the hub. Cleanup is done only when the hub answers that no live episode is
  left for the session; until then every `khipu promote`, `--gate-only` and `--status` retries it. A candidate
  whose code predates the isolation is refused rather than run.
- The replay samples from `query_log.jsonl` (the hook's `prompt-recall.log` holds no prompt text), masked
  with the capture masker even when `KHIPU_REDACT_SECRETS=0`. Each hook run (live and candidate) gets a fresh
  isolated dir that links the live replica read-only, with `KHIPU_RECALL_DAEMON=0` (the warm service would
  answer with the old code); a live baseline whose code predates `KHIPU_ISOLATED_DIR` gets `KHIPU_DATA_DIR`
  pointed into the same dir. Each run's hit ids, reason and time are read from its own hook log line.
- The gate builds every subprocess environment from a fixed list of names and never reads a variable whose
  name holds KEY or TOKEN; hooks resolve their keys as a live hook does.
- A slot is a detached worktree with a `.python_libs` link to the main checkout's (the launchers put
  `<root>/.python_libs` on `PYTHONPATH` and it is untracked); the plists rendered for the `live` link or a slot
  carry it too. Every flip writes each slot's last-live commit to `state/live-slots.json`; `--rollback` puts the
  other slot back on that commit if a failed or gate-only run has moved it since, and refuses a slot with no
  last-live commit, so it never activates a commit that was not live before.
- `khipu doctor --quick` gates on every local check plain doctor gates on (`cli.LOCAL_GATES`, computed by the
  same function: outbox, capture liveness, launchers, bundle seal, jobs, summariser provider and the rest)
  plus imports, config and DSN presence; it skips only the hub, network and probe checks.

## Migrating this Mac by hand

Run once by the orchestrator after review, in this order; record it in Khipu-ops. `MAIN` is the main
checkout, read from the launcher that runs it today; `PY` is the interpreter the launchers use.

```sh
L="$HOME/.config/khipu"
MAIN="$(cd "$(dirname "$(readlink "$L/bin/khipu-mcp")")/../../.." && pwd)"
echo "$MAIN"                           # must be the main checkout; stop if it is not
PY=/opt/homebrew/bin/python3.11
unset KHIPU_ROOT

# 0. Use Khipu search once (any harness, or `"$PY" -m khipu search ...`) so query_log.jsonl holds a
#    query from the last day; the recall step refuses without one. MAIN is not pulled: live-a below
#    is what runs now, and the merged phase 1 reaches live only through the gate in step 5.

# 1. Stop-condition: every launcher must be a symlink. Any output here: stop and ask.
for f in khipu-mcp khipu-stop-hook khipu-recall-hook khipu-prompt-recall khipu-aegis-capture; do
  [ -L "$L/bin/$f" ] || echo "NOT A LINK: $L/bin/$f"
done

# 2. Slot live-a on the commit that is live now, and the live link to it (nothing that runs changes yet).
git -C "$MAIN" fetch origin
git -C "$MAIN" worktree add --detach "$L/live-a" "$(git -C "$MAIN" rev-parse HEAD)"
ln -s "$MAIN/.python_libs" "$L/live-a/.python_libs"
ln -s "$L/live-a" "$L/live"

# 3. Point the five launchers through the link (what `khipu integrations install` now writes).
for f in khipu-mcp khipu-stop-hook khipu-recall-hook khipu-prompt-recall khipu-aegis-capture; do
  ln -sfn "$L/live/packages/cli/bin/$f" "$L/bin/$f"
done

# 4. Re-render the plists from the link and restart the two resident jobs.
export PYTHONPATH="$L/live/packages/cli:$L/live/.python_libs"
"$PY" -m khipu jobs refresh            # read "refreshed" / "external": an external plist is left as is
plutil -extract EnvironmentVariables.KHIPU_ROOT raw "$HOME/Library/LaunchAgents/com.khipu.queue-drain.plist"
#   must print $HOME/.config/khipu/live; any job listed under "external" needs its plist re-pointed by hand
launchctl kickstart -k "gui/$(id -u)/com.khipu.recall-daemon"
launchctl kickstart -k "gui/$(id -u)/com.khipu.queue-drain"

# 5. Check, then promote through the gate (one real capture is written to the hub and forgotten).
"$PY" -m khipu doctor | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["launchers"])'
"$PY" -m khipu promote --status
"$PY" -m khipu promote origin/main      # creates live-b, gates it, flips on a pass
"$PY" -m khipu promote --status
```

Then read the next nightly. Rollback is `"$PY" -m khipu promote --rollback`. To undo the migration
entirely: re-point the five links at `$MAIN/packages/cli/bin/`, `rm "$L/live"`, run `jobs refresh` with
`PYTHONPATH="$MAIN/packages/cli:$MAIN/.python_libs"`, kick the two jobs, then
`git -C "$MAIN" worktree remove --force "$L/live-a"` (and `live-b`).

## Phase 2: visibility

`khipu doctor` gains `promotion: {live_commit, main_commit, behind_by, last_gate}` and a warning when
main is ahead of live by more than a day; the app's Home shows "Live: <short sha>, merged: <short sha>"
with a Promote button that runs the gate and shows each step's result (it is the same command, through
the fixed-argv allowlist). Health report row. Settings → Advanced: the slot paths, read only.

## Decisions

- Two slots, not N: a rollback needs exactly one known-good predecessor; older ones are in git.
- Worktrees of the same repository rather than clones: one object store, one fetch, and `git worktree
  list` shows the live slots next to the session worktrees ([[khipu-live-launchers-follow-main-checkout]]).
- The gate replays real queries from `query_log.jsonl` rather than a golden set: the golden set is for
  quality (`recall-golden.jsonl`), the gate is for "does the lane answer at all under the live
  environment".
- The capture step writes one real episode and forgets it rather than mocking the hub: the hub write and
  the provider call are exactly the seams that have broken.
