# Routing changes reach every conversation (2026-10-20)

Status: APPROVED by Matt 2026-10-20 ("Verify. If green, go.") after a Codex verification of the diagnosis below. Extends `2026-10-14-routing-truth.md`; it does not replace it.

## What went wrong

On 2026-10-14 a conversation recorded a hold on Grok ("launch no new Grok jobs"). On 2026-10-19 another conversation set an unconfirmed priority that made Grok a regular builder, but it never ended the hold. For a day, every conversation kept seeing the hold and treating Grok as paused. Codex reproduced each cause on a temporary routing state (probes: `/tmp/khipu-routing-*-probe.py`).

1. **A priority change never touches a hold.** Nothing flags a priority or rule that contradicts an active hold on the same account.
2. **The pushed block hides the change and keeps the stale line.** Pending conversation rules and priority get at most half the budget (750 characters per summary, 200 per prompt update). Anything over that collapses to "too long to show here: read it with `khipu_route`", with no account name. A hold is a short line, so it stays visible (`routing.py` ~152, ~4600, ~4696, ~6269).
3. **Changes left out are marked as delivered.** When a prompt update shows four of eight changes plus "4 more changes", acknowledging the returned version means the next prompt shows nothing. The other four are never pushed (`routing.py` ~6337, `recall_prompt.py` ~1697).
4. **Collisions dead-end.** A second conversation cannot replace another conversation's pending priority or rule edit, and the error doesn't name the ways that work: the owner in the app, the terminal, or the owner-direction path (`routing.py` ~4233, ~4434).
5. **Rules and priority roles carry account levels that go stale.** "Codex is low" sits beside a fresh reading of 96% left, and nothing says they disagree.
6. **Ending a hold from a conversation is hard to find.** It exists: `khipu_route_report` with `action: "end_hold"` and `hold_id`. Nothing points to it.

## What changes

- **A. Every change names what it touches.** A change too long to show whole still shows its kind, its account names and a short opening, within its budget. It never collapses to a bare generic pointer.
- **B. Contradictions are flagged.** When a priority entry or a rule names an account that has an active hold, the hold line and the priority line say so beside each other. The flag names `khipu_route_report action=end_hold hold_id=<id>` as the way to end a hold, and an owner hold names its owner-only route. Khipu never infers a release from prose.
- **C. Nothing undelivered is marked delivered.** Changes left out of a prompt update stay owed and are shown first in the next update, until each has been shown once. The "N more changes" line still appears.
- **D. Collision errors say what works.** They name the pending change's author and age, the current version, and the paths that can replace it.
- **E. Stale level claims are flagged.** When a rule or priority role states an account's level ("low", "nearly used up", "N% left", "limited") and the latest usage reading for that account disagrees, the line carries a short marker with the live reading. The owner's words are never edited.
- **F. The end-hold action is easy to find.** `khipu_route` output and the hold lines in the pushed blocks name the end-hold action.

Out of scope: copying routing between Macs (phase 7 of `2026-10-14-routing-truth.md`); checking dispatches against rule 12's model tiers (a separate slice); GLM account and lane mapping (configuration after the GLM-capable deploy).

## Acceptance

- Replaying the real 2026-10-14..19 events (`~/.config/khipu/state/routing/events.jsonl`, copied into a temporary state) produces a session-start block and a prompt update that both show Grok named as a builder in the pending priority, and a contradiction flag on the Grok hold that names the end-hold action.
- The eight-change prompt-update reproduction shows all eight across two consecutive prompts.
- The "2% left" against a 96% reading reproduction carries the stale marker.
- Budgets stay within today's caps (1,500 per summary, 400 per prompt update). Owner text is never altered.
- Hermetic pytest from `packages/cli` stays at the known baseline failures.
