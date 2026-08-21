---
name: fortress-compounder-screener
description: >-
  Find stocks sharing the "CINF archetype" — low-volatility dividend compounders that
  grind upward over time, reliably recover from drawdowns, and are well suited to
  selling covered calls (and cash-secured puts / the wheel) against. Screens a stock
  universe on quantified "trustworthiness" + covered-call-fit traits, scores each name
  0–100 across five pillars, gates out the unfit, and ranks candidates by similarity to
  a reference anchor (default CINF). The logic is validated out-of-sample on the 2008
  GFC. Use when asked to find CINF-like / "trustworthy" / "only goes up over time" /
  covered-call / wheel / income-compounder candidate stocks, or to score one ticker.
---

# Fortress Compounder Screener

Cincinnati Financial (CINF) is unusually good to sell covered calls against and behaves
as if it "only goes up over time." This skill distills *why* into measurable traits and
screens any dividend-paying universe to find other stocks like it. It is a quantitative,
deterministic screen — no LLM calls, no API calls in the core path.

## What makes CINF special (the archetype)

CINF is a property-&-casualty insurer that invests its insurance float in an unusually
large **common-equity portfolio** (Berkshire-like) rather than the all-bond book its
peers hold. That single structural choice makes book value compound at equity-like rates,
which is the engine behind the slow, durable upward grind. Layered on top is a **66-year
consecutive dividend-increase streak** (a Dividend King) that was **never cut even through
a 63% drawdown in 2008–2009** — which creates a price-insensitive income-buyer base and a
yield floor that defends the stock on dips.

The honest reframe that drives this skill: **"trustworthy" does not mean "low drawdown."**
CINF fell **−63%** in the GFC and took **6.6 years** to make a new high. Trustworthy means
it **always recovers to new highs** and the **dividend keeps rising the entire time you
wait** — you get *paid to hold through the dip, and the dip always ends in a new high.*
That profile is covered-call gold: a slow grind (shares rarely called away on a moonshot),
moderate-but-persistent implied volatility (harvestable premium), and dividend-supported
mean reversion. The edge a call-seller monetizes is **recovery-certainty, not downside
protection.**

CINF's measured profile (from local data): beta 0.71 (recent) / 1.04 (full-history),
realized vol ~19.9% trailing / 29.7% full, price CAGR ~6.9%/yr (~9–10% with dividends),
60% of years positive, dividend yield ~2.2%, payout ~19–43%, ROE ~18%, near-zero leverage.
It scores **98/100 (ELITE)** on this screen.

## The traits — five quantified pillars (0–100)

| Pillar | Measures | Metric & threshold | Why it matters |
|---|---|---|---|
| **A. Dividend reliability** (0–25) | Commitment / buyer-base floor | Consecutive years dividend maintained-or-raised (split-adjusted run-rate). ≥10 candidate, ≥25 Aristocrat, ≥50 King | A multi-decade streak signals management commitment and attracts price-insensitive income buyers who defend dips |
| **B. Trust / recovery** (0–25) | Does it come back, and can it not? | 100% historical recovery to new highs after drawdowns; positive book value; ROE ≥10%; sane leverage | Covered calls cannot protect a *permanent* impairment — only quality avoids that |
| **C. Grind quality** (0–20) | Slow up, not explosive | Price CAGR in 3–15% sweet spot; ≥55% positive years; ≤~6 days/yr with \|move\|>5%; persistent above 200-day MA | "Grinds up" keeps strikes out-of-the-money; "rips up" gets your shares called away and caps the move that justified the risk |
| **D. Low-vol / beta** (0–20) | Calm enough to sell against | Trailing-1yr realized vol 12–28%; beta 0.30–0.85 | High vol = overnight gap-through and assignment whipsaw; low vol = predictable premium harvesting |
| **E. VRP / liquidity** (0–10) | Is the premium real & tradeable? | (Tier-2) implied vol persistently > realized + adequate option open interest. Core path uses a stock-liquidity proxy | The variance-risk-premium (IV>realized) is the structural edge in *selling* options |

**Hard gates (a candidate must pass all):** dividend streak ≥10 · trailing vol ≤35% ·
beta ≤1.10 · ≥8 years of price history · recovered to new highs (track record) · market
cap ≥$1B and price $5–$1000 · free-cash-flow not negative. A name failing any gate is
reported with the reason, not silently dropped.

**Verdicts:** ELITE ≥80 · STRONG ≥68 · FAIR ≥55 · WEAK <55. Note the score *saturates* near
the cap for top-tier names — many genuine fortresses tie at the high end (the pillars are capped
and a pre-filtered quality universe maxes them out), so use the CINF-distance below to rank among
the ELITE cluster rather than the raw score alone.

**Similarity-to-CINF:** beyond the absolute score, every candidate gets a z-scored
Euclidean **distance to the anchor** (default CINF) across the trait vector, so you can ask
directly for "the stocks most *like* CINF," not just "the highest-scoring."

What a top screen surfaces (face-valid): CINF, Ecolab, Casey's, Costco, Linde, Waste
Management, PepsiCo, and fellow insurers Travelers / Marsh / Assurant — the boring-fortress
archetype. High-vol momentum (NVDA, TSLA) and post-cut dividend traps (AT&T, 3M, whose
streak resets to ~0 on the cut) are correctly rejected.

## Out-of-sample validation — READ THIS BEFORE TRUSTING IT

The framework was tested out-of-sample: score the survivor universe using **only data on
or before 2007-12-31** (no look-ahead), then measure each name's **actual 2008–2014 GFC
outcome**. Result across 3,088 names:

| 2007 verdict | dividend cut | recovered by 2014 | median drawdown |
|---|---|---|---|
| ELITE (n=11) | **0%** | **100%** | −42% |
| STRONG (n=48) | 2% | 98% | −53% |
| FAIR (n=175) | 36% | 86% | −55% |
| WEAK (n=2,854) | **65%** | 71% | −62% |

**Passing the 2007 gates cut the dividend-cut rate from 62% to 21%.** A high score, computed
with no knowledge of the future, genuinely tilted toward GFC survival and recovery. This is
a real, earned edge.

**It is a tilt, not a guarantee. Three hard bounds — do not ignore them:**

1. **No score is a safety.** General Electric scored STRONG as of 2007, then fell **−89%**,
   never recovered, and cut its dividend. Roughly 1 in 50 of the top bucket still detonated.
2. **Deep drawdowns are normal even for winners** (ELITE median −42%; CINF −63%). Covered-call
   premium (~2–3%/month) does **not** offset that. The edge is recovery-certainty and getting
   paid to wait — *never* downside protection.
3. **Survivorship caps confidence.** The screen only sees stocks that still exist today.
   **~28% of the 60 S&P 500 Dividend Aristocrats of 2007 cut their dividend within three
   years** of the GFC, and several (GE, BAC) took 15+ years to recover or never did — those
   failures are partly invisible to a present-day database. Treat the output as a
   *resemblance-to-survivors* score, not a *survival-probability*.

## How to use it (covered-call workflow)

1. **Screen** for ELITE/STRONG candidates (or nearest-neighbors to CINF).
2. **Diversify.** The ~28% historical crisis-cut base rate means single-name risk is real —
   spread across many fortress names; never concentrate the thesis in one.
3. **Size for a 50–60% drawdown** on any single position. The recovery is reliable; the path
   is not gentle.
4. **Time the call sale by implied volatility.** The variance-risk-premium is *episodic* —
   sell calls when IV rank is elevated (≥30–50), not when IV is asleep. (At research time
   CINF's IV rank was only ~27, i.e. thin premium — a great *stock* but a poor *entry* for
   selling.)
5. **Let the gates do their job.** Names that have cut (AT&T, 3M) or are in permanent decline
   auto-fail on the streak/recovery gates — that is the screen catching the walking wounded.

## CLI / usage

```bash
# Score one ticker (full diagnostic — metrics, five pillar sub-scores, gates, verdict):
python3 scripts/fortress_screener.py --ticker CINF

# Full screen (top by Fortress Score AND nearest-neighbors to the anchor), write CSV:
python3 scripts/fortress_screener.py --top 40 --out matches.csv

# Change the similarity anchor, set a score floor, point at a different DB dir:
python3 scripts/fortress_screener.py --anchor PG --min-score 70 --fmp-dir /path/to/fmp

# Re-run the out-of-sample validation:
python3 scripts/backtest_asof2007.py --out backtest.csv
```

## Dependencies

- **Core (Tier-1): Python 3.9+ standard library only** — `sqlite3, statistics, math,
  datetime, csv, argparse, json`. **Nothing to pip install** when using the local data.
- **Data:** the local FMP SQLite databases — `price_history.sqlite` (daily_prices),
  `dividends.sqlite` (dividends; uses split-adjusted `adj_dividend`), `fundamentals.sqlite`
  (key_metrics_ttm with ratios_ttm fallback). Directory from `--fmp-dir` or `$FMP_DIR`,
  default `/Volumes/Cloud Storage/Databases/fmp`.
- **Optional:** `numpy`/`pandas` for faster full-universe screens (`--fast`); an options/IV
  source (Schwab, ORATS, or yfinance option chains) to activate the Tier-2 VRP/IV pillar.
- **Portability note:** the only data backend is FMP-schema SQLite (the `FMPStore` class). There
  is **no yfinance/online fallback** — running outside this workspace needs databases of the same
  schema (or a new adapter added to `FMPStore`).
- **Cursor:** on claude-os-workspace, registered via `.cursor/rules/fortress-compounder-screener.mdc`
  (workspace-tier rule — **not** mirrored to app repos). The skill folder itself ships in the plugin
  and app-repo mirrors. Scripts are pure stdlib with no workspace imports; Cursor needs the FMP
  SQLite directory (`--fmp-dir`/`$FMP_DIR`). To enable the rule trigger in another project, also copy
  that `.mdc` into the project's `.cursor/rules/`.

## Limitations & honest caveats

- **Survivorship** is the core limit (see Validation). A fully survivorship-free backtest
  needs delisted-ticker price history, which the local DB does not carry; the FMP legacy
  delisted endpoint is retired. The honest external benchmark is the ~28% Aristocrat
  crisis-cut rate.
- **The E pillar currently measures *stock* dollar-liquidity, not *option* liquidity.** Until
  an options source is wired, do not treat it as evidence the *options* are tradeable —
  confirm the chain yourself.
- **Dividend-streak depth** is capped by the local dividend history (~1984), so the computed
  streak undercounts very long Kings (CINF computes 38 vs the true 66). Cross-reference a
  curated Aristocrat/King list for exact streak length.
- **The score does not penalize drawdown depth** — a name with an −80% history can still
  score ELITE. The max-drawdown column is shown for exactly this reason; read it.
- **`market_cap`, `book_value`, and `roe` come from fallbacks** — `key_metrics_ttm` leaves
  those three columns unpopulated, so the screener reads market cap from `enterprise_value`
  (≈ market cap for this low-leverage dividend universe), reads ROE from `ratios_ttm`, and
  infers positive book value from the `ratios_ttm` price-to-book. The ≥$1B size gate works;
  for a highly-leveraged name EV overstates market cap, so treat the size figure as approximate.

## Files

- `scripts/fortress_screener.py` — the live screener (scoring, gates, similarity, CLI).
- `scripts/backtest_asof2007.py` — the out-of-sample validation harness.
- `requirements.txt` — dependency detail. `MANIFEST.yaml` — skill metadata.
