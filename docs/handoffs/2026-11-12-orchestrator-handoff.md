# Orchestrator hand-off, 2026-11-12 ~19:20Z

Written by the Khipu orchestrator session on the M5 (Claude Code desktop app, session `b131b405`) just before Matt switched the M5's Claude login. Read this first, then the memory index (`~/.claude/projects/-Volumes-Cloud-Storage-Code-Khipu/memory/MEMORY.md`), which loads on its own.

## 1. The account switch, and what it changes

**What happened.** Matt is logging the M5's default Claude Code login out of the main account and into the secondary account, which becomes the primary. Same Mac, same macOS user.

**What does not change.** Everything on disk stays where it is:

- `~/.claude/`: `CLAUDE.md` (Matt's rules), the memory folder, hooks, settings, plugins, MCP servers (Khipu's included).
- This session's transcript: `~/.claude/projects/-Volumes-Cloud-Storage-Code-Khipu/b131b405-c71b-4bd2-a46f-f801ea71e8c9.jsonl`.
- The jobs folder: `~/.claude/projects/-Volumes-Cloud-Storage-Code-Khipu/jobs/2026-11-12/`.
- Live Khipu (`~/.config/khipu/`), the hub, the replica, the wiki files, the Keychain.

A new session in this repo gets the same rules, memory and Khipu tools.

**What does change, and needs a decision or a check.** None of these were done before the switch.

1. **"Main" and "second" become the same account.** Jobs meant for the second account run with `CLAUDE_CONFIG_DIR=~/.claude-t3-second`. That folder is signed into the secondary account, which is now also the default login, so both lanes bill one account. To keep two lanes, sign the old main account into one of the two homes. To keep one lane, change the rules. Matt's call.
2. **The CLAUDE.md rule "Never Fable on the second Claude account"** now names the account that is the default login. Matt's call how it reads now.
3. **Khipu's routing record** still lists "Claude" (main, resets Mon 15:00Z, must last until Monday) and "Claude second" (resets Sat 18:59Z). Usage reads each home's login, so after the switch both rows may read the same account. Khipu warns when two tracked accounts answer as the same account. The record changes by Matt's direction through `khipu_owner_act`; read `khipu_route` first.
4. **Summaries and the wiki writer use the default login.** The `claude-cli` provider drops `CLAUDE_CONFIG_DIR`, so after the switch Khipu's summaries (Haiku) and the wiki writer (Sonnet, once #607 is live) bill the new primary.
5. **The second home's sign-in** warned at every call today that it "can end at any time from now until 2026-11-14".
6. **The M1** is signed into the secondary account only. Unchanged by this switch.

## 2. Work in flight: the wiki learns every night (PR #607)

Matt's request today: get the wiki fix running today rather than tonight, with pages updated during the day ("hourly batches, not per capture"). The acceptance case: the 2026-11-05 Muse, Kimi and GLM cancellations (episodes 117390, 117396, 117608, 117626) must land on a page that a search for those names finds.

**State.** PR https://github.com/mrkinglollipop/Khipu/pull/607, branch `feat/wiki-nightly` (pushed, head `4b81496e`, main `dabdf9e4` merged in), worktree `/Volumes/Cloud Storage/Code/Khipu-wt/orch-wiki`. Not merged, not promoted, switch `wiki_nightly` off. Scope and every finding: `docs/plans/2026-11-12-nightly-wiki-update.md` (on the branch).

**Checked:**

- Wiki, provider, migration, jobs, nightly, config and models tests: 883 passed on the merged branch.
- Five draft samples on the live writer, each read against its sources by Codex Sol. On the fourth (Sonnet): no old text lost, and the cancellations are stated correctly on `model-routing`, with the aliases below applied in memory. Two slips in about 130 new bullets: one "is being rebuilt" written as "was rebuilt", and an August policy block on model-routing left in place above the newer rules that replace it. The orchestrator judged that acceptable to ship (every bullet carries its episode id; `khipu wiki undo` takes back a page or a run) and told Matt.
- Three cross-family reviews (Sonnet, second account) of the Codex-built parts. The last: nothing to fix first.

**Not finished:**

- A full test suite on the merged branch was running at the switch (69% at 19:20Z, a few failures so far). Log: `jobs/2026-11-12/full-suite-wiki.log`. It runs in a temp home with no Claude login, so the switch does not touch it. Known failures that are not from this branch:
  - 4 in `test_agent_view_routing_false_withholds.py`; they also fail on main `b25b7d95`. A model name that looks like a private value withholds the routing page. Needs its own fix.
  - `test_provenance_copy_bench` (timing; passes alone).
  - `test_settings_sync_pg::test_an_older_mac_on_main_reads_the_new_lines_without_harm` (known since 2026-11-09).

  Any other failure must be read before merging.

**Remaining steps, in order:**

1. Read the full suite. If only the known failures remain, merge #607 (`gh pr merge 607 --merge`).
2. **Promote** main on the M5: copy `jobs/2026-11-12/promote-091dac50.sh` with the new sha. It runs `khipu promote origin/main`, then `khipu integrations refresh`. This applies migrations 0053 and 0054. Coordinate a short quiet window with other active sessions (memory: `coordinate-promotes-dont-wait`).
3. **Aliases**, so the subscription episodes reach `model-routing`. For each of `subscriptions codex-plan claude-plans claude-plan model-lanes glm-lane kimi-lane muse kimi glm`, run `khipu wiki slug <slug> --set alias --target model-routing`.
4. **Budget.** The wiki has 60 calls a day (`wiki_daily_pages`). Today's drafts used most of them; check with `python -c "from khipu.embed import budget_status; print(budget_status('wiki'))"`. For a hand run today, `KHIPU_WIKI_DAILY_PAGES=<n>` in the environment lifts it for that process only.
5. **First real run:** `khipu wiki run --json`, using the live CLI after the promote. Read the report: written, skipped, held, failures.
6. **Verify:** `khipu_search` for "Muse Kimi GLM canceled" must return the `model-routing` page with the end dates (11-13, 11-20, 11-27). Read the changed pages' diffs, especially `model-routing`.
7. **Switch on** `wiki_nightly` (config key; enum `false|true`). The hourly run follows `wiki_every_minutes` (default 60) inside the 5-minute snapshot timer.
8. Rebuild the desktop app on the M1 and install it on both Macs. The installed app is `b25b7d95` on both; scripts follow `jobs/2026-11-12/m5-app-install-b25b7d95.sh` and `~/khipu-tools/build-<sha>.sh` on the M1.

**Useful files in `jobs/2026-11-12/`:** `wiki-draft-1` to `wiki-draft-5` (samples: `old.md`, `episodes.jsonl`, `draft.md`, `checks.json`, `used.json` per page), `check-drafts-*-codex.md` (the content checks), `review-wiki*.out` (the reviews), `wiki-scripts/` (`alias_preview_all.py` drafts pages with the aliases applied in memory; `raw_call.py` makes one writer call and prints turns, stop reason and sizes).

## 3. Other open items

- **Routing-page withhold bug** (the 4 failing tests above): a real fix, small.
- **Routing-reminder removal** (back-to-roots item 8): brief at `jobs/2026-11-12/brief-remove-routing-reminders.md`, not started.
- **Stop building vectors for agent rows** (`embed.py` around lines 1215 and 3712): not started.
- **Model-routing page rewrite:** its August "on-subscription only" block is stale and contradicts newer rules. That is a monthly-pass or hand job, not the wiki writer's.
- **Two scope questions for Matt on rules and hooks** (unanswered): make `CLAUDE.md` Khipu's rules (advice: yes); three new guards (advice: no).
- The orchestrator's resume points live in memory `khipu-orchestrator-on-m5.md`.

## 4. Rules that bite in this work

- Only a yes Matt typed himself is approval. Nothing public (release, site, X card) without his go. A gateway deploy needs a fresh yes each time.
- Reports and job outputs go in the jobs folder, never in temp folders. Worktrees go on `/Volumes/Cloud Storage`.
- Hermetic tests: from `packages/cli`, `HOME=$(mktemp -d) KHIPU_KEYCHAIN=0`, with the database URL and key variables unset (memory: `khipu-test-oracle-is-hermetic-pytest`).
- Never run the nightly in the daytime. A wiki-only hand run (`khipu wiki run`) is what Matt asked for and is fine.
- Grok is paused for all jobs until Matt lifts it. Codex is the first lane for builds.
