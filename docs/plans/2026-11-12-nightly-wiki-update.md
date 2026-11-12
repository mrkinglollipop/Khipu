# The wiki learns every night (back-to-roots item 1a)

Status: scope for Matt, 2026-11-12. Nothing here is built. Read `docs/plans/2026-11-12-back-to-roots.md` first.

## Where things stand

- 626 wiki pages, 0 revised in 7 days, 6 in 30. Captures ran about 1,400 a day on 10 and 11 Nov.
- A page is a file, `Memory/conversations/topics/<slug>.md`. The file is the page of record: Obsidian, the monthly pass and the reconcile all read it. `reconcile_memory_root` (in `mirror.py`, called from the outside nightly) copies it into `topics`, and `_upsert_topic` adds a `topic_revisions` row holding the new body whenever the text changes.
- Pages used to be written at capture time by the old `capture_v2.py` path (`_synth_topic_page`). Capture now runs in the hub, which writes episodes but no pages. That is the likely reason the wiki stopped. I did not pin down the exact date.
- A nightly page writer already exists and has never run: `scripts/nightly_topic_amend.py` (since 7 Jul). It matches only exact slugs, rewrites with no fact checks and calls the old model gateway. Its approach is right, and the new step copies it. The code is not kept.
- Episode topics are free-form slugs, several per episode. Most do not match a page name exactly (for example `aegis-settings-immersive` versus a page called `aegis-settings`), so exact-slug matching alone would miss most of the day.

## 1. Which pages get touched

The nightly works off a queue: one row per (page, episode), in a new table `wiki_queue` (slug, episode_id, queued_at, done_at).

- **Assign.** For each episode since the last run, attach it to at most 2 pages, by a slug the episode names: a page with that slug, the page that replaced it if it was superseded, or the page the slug was filed under by the gate below. Vectors play no part (see "What the live hub showed"). Only episodes of top-level conversations are used; rows written by delegated jobs are left out, because the conversation that started them has its own episode about the same work. Pages named `note:` (mirrored Claude Code notes) and pages marked superseded or retired are skipped.
- **The gate for new slugs.** A slug that is not a page and turns up in at least 3 episodes from at least 2 sessions within 7 days is put to the writer model once: is it a real thing worth a page (a project, a product, a system, a person, an account or plan, a standing decision), a generic tag (reviewing, fixing, testing), or does it belong on a page that already exists? The answer is checked in code and kept in a table `wiki_slugs`, so a slug is asked about once. The 30 most-used undecided slugs are asked about per run, in one call.
- **Open a page** for a slug the gate called a real thing. At most 10 new pages a run. Pages are opened by the nightly and by a hand run, never by the hourly run.
- **Cap.** At most 60 page writes a night (the ceiling in section 4). Pages with the most waiting episodes go first, and a page that has waited 3 nights goes ahead of the rest. Within a page, episodes are written oldest first, so an earlier fact never overwrites a later one. A page with too many episodes for one call (for example `khipu`) takes more than one write, and each write counts against the cap.
- **Catch-up.** The first run fills the queue from the last 8 days (which takes in the 5 Nov cancellations of section 6) and drains it under the same cap. Not 30 days: a busy page such as `khipu` would take about 36 writes of old episodes before it reached today.

### What the live hub showed (2026-11-12, read-only, before anything wrote)

The first version of this plan matched an episode to the nearest page by vector when no slug matched. Measured on the live hub, that does not work, and it was removed before any page was written.

- For 400 recent episodes, the page an episode names by slug sits at a median rank of 127 out of 597 pages by distance. The nearest page is at 0.22 to 0.29 for almost every episode and the named page at 0.26 to 0.35, so no threshold separates right from wrong. Read by eye, about 3 in 14 nearest pages were right; an Olivewood PR episode's nearest page was `mrs-lollipop-merge-gate-mechanics`.
- The reason is plain in the data: the wiki has no page for the projects of the last weeks (`olivewood`, `church-harness`, `healthy-you`, `recap`), so the nearest page is always a page about an older project.
- The nearest-page query did not finish either: one day is 2,863 episodes (2,191 of them rows of delegated jobs) and it timed out at 60 s.
- Slugs are free tags. In three days top-level conversations used 8,065 distinct slugs; 17 are pages (2% of mentions). 1,197 new slugs pass the 3-episodes, 2-sessions rule, and the most used are half generic (`pr-review`, `code-review`, `bug-fix`) and half real things with no page. Hence the gate.
- To watch on the first runs, with no mechanism built for it: the length of busy pages (`khipu` gets about 110 episodes a day). The checks keep every dated or numbered line, so a busy page only grows until the monthly pass trims it.

## 2. What the writer does to a page

Input: the current page, plus one line per waiting episode (date, episode id, summary, decisions, preferences). Output: the whole page.

- **Must:** keep every fact that is still true; when a newer episode contradicts an old fact, replace it and leave a dated line ("Was X until 2026-11-12"); keep the frontmatter, set `last_updated`, and move `status: seed` to `active`; list the episode ids it used in a short Sources line. Plain short prose.
- **Must never:** invent; drop a fact that is still true; merge, retire, rename or split pages (those stay monthly); touch a `note:` page or a protected file.
- **Checks in code, before writing:** the slug and frontmatter are intact; the page did not shrink below 70% of its old length; every old line containing a date, number, path or link is still present or named in a "Was" line. A rewrite that fails a check is not written. The page stays queued, is retried the next night, and after that is listed in the morning report.
- **Write path:** if the file changed since the hub last saw it (an Obsidian edit), reconcile it first. Then write the file and call `_upsert_topic` with source `nightly-wiki`, so the revision is recorded at once. Before the first nightly write to any page that has no revision row matching its current text, write a baseline revision so there is always something to roll back to.

## 3. Which model writes

**Recommendation: Claude Haiku via the Claude login**, the same route and model the summaries use today, unless the sample in section 6 shows it dropping facts. Then Sonnet via the login.

- Why: page writing is merge-and-preserve work over short inputs, close to what Haiku already does well for summaries. Through the login it costs no cash, and at 02:05 the accounts are idle.
- **Cost per night** at a cap of 60 writes, each about 8,000 tokens in and 1,500 out: about 0.5 million tokens in and 0.1 million out. Through the login that is $0 cash, and about a tenth on top of what the summaries already draw from the plan each day (about 1,400 calls of up to 14,000 characters each). If billed through an Anthropic API key at the repo's own price table (`models.py`, Haiku $0.10 in and $0.50 out per million tokens, as of 2026-10-13), it is about $0.10 a night, or $3 a month. Not checked: Sonnet's current price. It is to be read from Anthropic's page before the sample run.
- **Setting:** a new `wiki` role in config `models` (next to `synth`, `embed`, `vision`), shown as a "Wiki" row under Models. It defaults to whatever the summaries use. Calls go through the existing `khipu.extract` providers, so every choice the summaries offer works here too.
- **What leaves the Mac:** no transcript. The writer sends the page's current text and each matched episode's summary, decisions and preferences. All of these were redacted at capture, and the page is redacted again on save by `_upsert_topic`. They go to the Wiki row's provider, which by default is Anthropic. Anthropic already receives the full transcript windows for the summaries, so this sends it less than it gets today. If Matt picks a different provider for Wiki, that provider sees page text and summaries, never a transcript. For comparison, the monthly pass sends whole pages to DeepSeek today.

## 4. Ceiling

- A new scope `wiki` in `BUDGET_SCOPES` (`embed.py`), config key `wiki_daily_pages`, default 60. It is separate from `synth_daily_calls`, so the wiki can never starve the summaries.
- Once the ceiling is hit, the rest of the queue waits for the next night. Nothing is dropped: an episode leaves the queue only when a written revision covers it. If the login is out of room, the night's writes fail the same way and wait. They never fall back to a paid key unless Matt says so (open question 2).
- Lifting it: `khipu budget lift` and `khipu config --set wiki_daily_pages N`, the same path as today's ceilings, so an agent can lift it on Matt's word on the spot.

## 5. Undo

- **One page:** `khipu wiki undo <slug>` writes the previous revision's body back to the file and records a revision with source `wiki-undo`. The episodes that bad write absorbed go back into the queue as waiting, and the page is held from rewriting for 7 days. That hold ends by itself.
- **A whole night:** `khipu wiki undo --night <date>` does the same for every `nightly-wiki` revision from that night. A page edited since by something else is skipped and listed. Pages opened that night are moved to `topics/_retired/`, never deleted, and the reconcile tombstones them.
- An agent runs either command on Matt's word. An "Undo last night" button in the app waits for the Settings mock (back-to-roots item 6).

## 6. Measure before it is on

- Ships behind one switch, `wiki_nightly`, off by default. While it is off, `khipu wiki plan` and `khipu wiki draft --sample N` run read-only and write their output to the jobs folder, never to `topics/`.
- **Sample:** 30 pages (the 20 most-searched by `topic_hits`, plus 10 at random) with their last 7 days of episodes, drafted by Haiku and by Sonnet.
  - A Codex Sol job (effort high, a different model family from the writer) reads each old page, the episodes and each draft, and lists every invented fact, every dropped fact that is still true, and every wrong "Was" line. It also checks 50 episode-to-page assignments.
  - Matt reads 3 diffs of pages he knows (`khipu` among them). This is the taste check.
- **A known miss the sample must include** (Matt, 2026-11-12: "a phenomenal example ... of something that you were supposed to remember"). On 2026-11-05 Matt cancelled Muse, Kimi Code and GLM; three episodes from an Olivewood conversation record it with the end dates (13, 27 and 20 Nov). No page was ever written from them. A week later, in a Khipu conversation, he mentioned Muse and Kimi; the prompt recall pushed one page, `note:muse-usage-limits` from 14 Oct (where to see Muse's limits), and nothing about the cancellation, and the agent paused two accounts that were still usable. The sample is not passed unless the draft for the page that covers these accounts says they are cancelled, usable until their end dates, and a prompt that names Muse, Kimi or GLM gets that page.
- **Bar to flip the switch:** 0 invented facts, at most 1 dropped true fact across the 30 pages, at least 90% of assignments right. The cheaper model that passes wins. Then 7 nights on for Matt only, then on by default.
- **Morning report (item 1c),** a `wiki_update` entry in `nightly-last.json` that shows in the doctor and the app's existing nightly row: pages revised, pages opened, episodes absorbed, queue left and its oldest age, rewrites refused, calls used and whether the ceiling was hit. It also shows the median and 90th-percentile age (days since last revision) of the pages searches returned in the last 24 hours, read from `query_log.jsonl` (each line already carries its top 3 hits).

## 7. Where the code lives

- In Khipu: `packages/cli/khipu/wiki_nightly.py`, run as a new nightly step `wiki_update` in `_run_nightly_locked` (`jobs.py`). It goes after `capture_export` and before `consolidate_nightly`, so the outside script's git sync carries tonight's pages offsite tonight, and the `embed_backfill` that follows re-embeds the revised pages. It runs only on the Mac that holds the hub and the memory folder (the M5). Other Macs skip it.
- The same step also runs outside the nightly (Matt, 2026-11-12: "I don't want to have to wait till tonight"; "not have to wait till the night for things to be put into their pages"): by hand with `khipu wiki run`, and every hour (`wiki_every_minutes`, default 60, 0 for nightly only) for pages that have waiting episodes. All three share the day's ceiling and the nightly lock. Writing at every capture was ruled out with Matt: too many writes.
- The outside light pass (`consolidate_nightly.py`) stays as it is for now: dedupe proposals, reflection, MEMORY.md rebuild, reconcile, git sync. Moving it into Khipu belongs to the 5 Nov line budget, not to this work. `nightly_topic_amend.py` is superseded; it is removed in the same clean-up once Matt okays deletions.
- The monthly pass keeps merge and retire. Its "update" action overlaps the nightly and is left alone until question 4 is answered.

## 8. Slices (smallest useful first)

Test command for every slice: `cd packages/cli && HOME=$(mktemp -d) KHIPU_KEYCHAIN=0 python -m pytest tests/test_wiki_nightly.py -q`, plus the named failures baseline for the full suite. Any slice that touches `apps/desktop` also runs `npx vite build` there.

1. **Queue and matching, read-only.** Migration for `wiki_queue`; `assign()`; `khipu wiki plan` prints tonight's pages and why. (As built it also matched by vector; that was measured and removed, see section 1.)
2. **Writer, drafts only.** The `wiki` model role, the `wiki` ceiling scope, the prompt, the checks in code and `khipu wiki draft --sample N` writing to the jobs folder. Stop if adding the role breaks the app's reading of `models`. That fix becomes its own slice.
3. **Sample run (no code).** 30 pages, Haiku and Sonnet, the Codex Sol check and Matt's 3 diffs; report in the jobs folder. Stop after two failed rounds and hand the diffs back.
4. **Live step behind the switch.** The `wiki_update` nightly step, file write plus `_upsert_topic`, baseline revisions, carry-over, the 8-day catch-up, and `khipu wiki undo` (page and night). Never run the real nightly to test it: tests use a temp home.
5. **Morning report (1c).** The step counts and the search page-age figures, plus a doctor line.
6. **Wiki row in Settings,** built with the approved regrouped Models page. Until then an agent sets it on Matt's word.
7. **Flip on by default** after 7 clean nights.

## 9. Questions, settled

Matt did not answer these one by one; on 2026-11-12 he asked for the step to run the same day, so each is taken as recommended and any of them changes on his word.

1. Writer: Haiku through the Claude login, stepping up to Sonnet only if the sample says so.
2. When the login is out of room, the wiki waits. No paid key.
3. Cap of 60 page writes a day, shared by the nightly, the hourly run and a hand run.
4. The monthly pass is left as it is.
