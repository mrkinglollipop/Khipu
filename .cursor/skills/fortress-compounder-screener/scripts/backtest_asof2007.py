#!/usr/bin/env python3
"""
Survivorship-aware out-of-sample validation of the Fortress Compounder screener.

Scores a dividend-paying survivor cohort AS OF 2007-12-31 using ONLY price and
dividend history on/before that date (no fundamentals — those are present-day in
the local DB and would introduce look-ahead). Measures actual GFC-era outcomes
over 2008-01-01..2014-12-31.

What this CAN show: among names that still exist in today's FMP extracts, did a
high 2007 Fortress Score predict lower dividend-cut rates and faster recovery
through the GFC window?

What this CANNOT prove: true population base rates — delisted/bankrupt names are
absent from the database by construction (survivor-only universe).
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Sibling import — reuse verified metric math from the live screener.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from fortress_screener import (  # noqa: E402
    above_200dma_frac,
    ann_vol,
    annual_run_rates,
    beta,
    cagr,
    div_streak_years,
    jump_days_per_yr,
    max_dd,
    median_dollar_volume,
    months_between,
    pct_pos_periods,
    pillar_a,
    pillar_b,
    pillar_c,
    pillar_d,
    pillar_e,
)

FMP_DIR = os.environ.get("FMP_DIR", "/Volumes/Cloud Storage/Databases/fmp")
ASOF = "2007-12-31"
FWD_START = "2008-01-01"
FWD_END = "2014-12-31"

# Price+dividend pillars max without fundamentals / post-2007 recession flag (~90 raw).
_SCORE_DENOM = 100.0  # retained for docs; backtest uses raw sum + verdict_backtest()


def verdict_backtest(raw_score: float) -> str:
    """Verdict bands scaled to achievable raw score without fundamentals (~90 max)."""
    if raw_score >= 72:
        return "ELITE"
    if raw_score >= 61:
        return "STRONG"
    if raw_score >= 50:
        return "FAIR"
    return "WEAK"


NAMED_COHORT = [
    "CINF",
    "PG",
    "KO",
    "MMC",
    "GE",
    "BAC",
    "KEY",
    "CMA",
    "RF",
    "FITB",
    "USB",
    "STT",
    "PFE",
    "BBT",
]

ORACLE_TICKERS = {
    "CINF": {
        "div_cut": False,
        "recovered_by_2014": True,
        "fwd_max_dd_lo": -0.70,
        "fwd_max_dd_hi": -0.55,
    },
    "GE": {"div_cut": True, "recovered_by_2014": False, "fwd_max_dd_hi": -0.80},
    "BAC": {"div_cut": True, "recovered_by_2014": False, "fwd_max_dd_hi": -0.85},
    "KEY": {"div_cut": True, "recovered_by_2014": False},
    "FITB": {"div_cut": True, "recovered_by_2014": False},
}


def assert_windows_disjoint() -> None:
    assert ASOF < FWD_START, "as-of window must end before forward window starts"
    assert FWD_START <= FWD_END


def connect_db(name: str, fmp_dir: str) -> sqlite3.Connection:
    return sqlite3.connect(os.path.join(fmp_dir, name))


def load_spy(fmp_dir: str) -> Dict[str, float]:
    conn = connect_db("price_history.sqlite", fmp_dir)
    cur = conn.execute("SELECT date, close FROM daily_prices WHERE ticker='SPY' ORDER BY date")
    spy = {str(d)[:10]: float(c) for d, c in cur if c is not None}
    conn.close()
    return spy


def build_universe(fmp_dir: str) -> List[str]:
    """Survivor-only dividend cohort with usable pre-2007 history.

      Requires >=500 total price rows (DB depth), >=250 rows on/before ASOF
    (local FMP history starts ~2006-06-29 → ~379 pre-2007 rows for most names),
      and >=1 dividend on/before ASOF.
    """
    ph = connect_db("price_history.sqlite", fmp_dir)
    dv = connect_db("dividends.sqlite", fmp_dir)
    total_ok = {
        t
        for (t,) in ph.execute(
            "SELECT ticker FROM daily_prices GROUP BY ticker HAVING COUNT(*) >= 500"
        )
    }
    pre_ok = {
        t
        for (t,) in ph.execute(
            "SELECT ticker FROM daily_prices "
            "WHERE date <= ? GROUP BY ticker HAVING COUNT(*) >= 250",
            (ASOF,),
        )
    }
    div_ok = {
        t
        for (t,) in dv.execute(
            "SELECT DISTINCT ticker FROM dividends "
            "WHERE date <= ? AND COALESCE(adj_dividend, dividend) IS NOT NULL "
            "AND COALESCE(adj_dividend, dividend) > 0",
            (ASOF,),
        )
    }
    ph.close()
    dv.close()
    return sorted(total_ok & pre_ok & div_ok)


class FMPBacktestStore:
    """Lightweight loader — one price connection per run."""

    def __init__(self, fmp_dir: str):
        self.fmp_dir = fmp_dir
        self._ph = connect_db("price_history.sqlite", fmp_dir)
        self._dv = connect_db("dividends.sqlite", fmp_dir)

    def close(self) -> None:
        self._ph.close()
        self._dv.close()

    def load_prices(self, ticker: str) -> Tuple[List[Tuple[str, float]], List[int]]:
        rows = self._ph.execute(
            "SELECT date, close, volume FROM daily_prices WHERE ticker=? ORDER BY date",
            (ticker,),
        ).fetchall()
        closes: List[Tuple[str, float]] = []
        volumes: List[int] = []
        for d, c, v in rows:
            if c is None:
                continue
            closes.append((str(d)[:10], float(c)))
            volumes.append(int(v) if v is not None else 0)
        return closes, volumes

    def load_dividends(self, ticker: str) -> List[Tuple[str, float]]:
        rows = self._dv.execute(
            "SELECT date, COALESCE(adj_dividend, dividend) FROM dividends "
            "WHERE ticker=? AND COALESCE(adj_dividend, dividend) IS NOT NULL ORDER BY date",
            (ticker,),
        ).fetchall()
        return [(str(d)[:10], float(v)) for d, v in rows if v and float(v) > 0]


def load_ticker_series(
    ticker: str, store: FMPBacktestStore
) -> Tuple[List[Tuple[str, float]], List[int]]:
    return store.load_prices(ticker)


def load_dividends(ticker: str, store: FMPBacktestStore) -> List[Tuple[str, float]]:
    return store.load_dividends(ticker)


def slice_pre(closes: Sequence[Tuple[str, float]]) -> List[Tuple[str, float]]:
    return [(d, v) for d, v in closes if d <= ASOF]


def slice_fwd(closes: Sequence[Tuple[str, float]]) -> List[Tuple[str, float]]:
    return [(d, v) for d, v in closes if FWD_START <= d <= FWD_END]


def score_asof_2007(
    pre_closes: List[Tuple[str, float]],
    pre_volumes: List[int],
    pre_divs: List[Tuple[str, float]],
    spy: Dict[str, float],
) -> Dict[str, Any]:
    """Price+dividend pillars only; renormalized to 100."""
    close_vals = [c for _, c in pre_closes]
    by_date = {d: c for d, c in pre_closes}

    rates = annual_run_rates(pre_divs)
    streak = div_streak_years(rates, as_of_year=2007)
    if pre_closes:
        price_years = max(1, 2007 - int(pre_closes[0][0][:4]) + 1)
        streak = min(streak, price_years)

    vol252 = ann_vol(close_vals[-253:]) if len(close_vals) >= 253 else ann_vol(close_vals)
    beta_v = beta(by_date, {d: v for d, v in spy.items() if d <= ASOF}, lookback=1260)

    cagr_v = cagr(pre_closes)
    pct_years = pct_pos_periods(pre_closes, 4)
    above_dma = above_200dma_frac(pre_closes)
    jumps = jump_days_per_yr(pre_closes)

    worst, _pk_d, trough_d, rec_d = max_dd(pre_closes) if pre_closes else (0.0, "", "", None)
    # ~2006+ price window is too short for meaningful recovery scoring at ASOF.
    rec_to_2007 = False

    vol_tail = pre_volumes[-1260:]
    price_tail = pre_closes[-1260:]
    med_dvol = median_dollar_volume(price_tail, vol_tail)

    # Pillar A: streak only (survived_recession needs 2008–2010 data — excluded).
    a = pillar_a(streak, survived=False)
    # Pillar B: price recovery only — no free points for missing fundamentals.
    b = pillar_b(rec_to_2007, rec_d if rec_to_2007 else None, -1.0, 0.0, 999.0)
    c = pillar_c(cagr_v, pct_years, above_dma, jumps)
    d = pillar_d(vol252, beta_v)
    e = pillar_e(med_dvol)

    raw = a + b + c + d + e
    fortress_score = round(raw, 1)
    last_close = pre_closes[-1][1] if pre_closes else None

    return {
        "div_streak_2007": streak,
        "vol252_2007": vol252,
        "beta_2007": beta_v,
        "cagr_2007": cagr_v,
        "maxdd_to_2007": worst,
        "recovered_to_2007": rec_to_2007,
        "fortress_score_2007": fortress_score,
        "verdict_2007": verdict_backtest(fortress_score),
        "score_a": round(a, 1),
        "score_b": round(b, 1),
        "score_c": round(c, 1),
        "score_d": round(d, 1),
        "score_e": round(e, 1),
        "last_close_2007": last_close,
        "price_rows_2007": len(pre_closes),
    }


def gates_passed_2007(m: Dict[str, Any]) -> bool:
    """Hard gates adapted for as-of-2007 price+dividend data (no fundamentals)."""
    if (m.get("div_streak_2007") or 0) < 10:
        return False
    vol = m.get("vol252_2007")
    if vol is None or vol > 0.35:
        return False
    beta_v = m.get("beta_2007")
    if beta_v is not None and beta_v > 1.10:
        return False
    if (m.get("price_rows_2007") or 0) < 250:
        return False
    # NOTE: recovered_to_2007 is intentionally NOT gated here. The local price history
    # starts ~2006, so a stock's true pre-2006 peak is invisible and this flag is False
    # for essentially everyone as-of-2007 — a data artifact, not a real signal. It is
    # retained as an informational column only. (The live screener keeps the gate, where
    # full history is available.)
    lc = m.get("last_close_2007")
    if lc is None or not (5 <= lc <= 1000):
        return False
    return True


def forward_outcomes(
    closes: Sequence[Tuple[str, float]],
    div_rows: Sequence[Tuple[str, float]],
) -> Dict[str, Any]:
    pre = slice_pre(closes)
    fwd = slice_fwd(closes)
    if not pre or not fwd:
        return {}

    precrisis_peak = max(v for _, v in pre)
    # Local DB history starts ~2006; peak may understate true pre-GFC highs (see SKILL caveats).
    # Measure GFC stress from the pre-crisis peak (do not ratchet peak on later new highs).
    worst = 0.0
    trough_d = fwd[0][0]
    for d, v in fwd:
        dd = v / precrisis_peak - 1.0
        if dd < worst:
            worst = dd
            trough_d = d

    rec_d: Optional[str] = None
    for d, v in fwd:
        if v >= precrisis_peak:
            rec_d = d
            break

    recovered = rec_d is not None and rec_d >= trough_d and fwd[-1][1] >= precrisis_peak
    months_rec: Optional[int] = None
    if recovered and rec_d and trough_d <= rec_d:
        months_rec = months_between(trough_d, rec_d)

    rates = annual_run_rates([(d, v) for d, v in div_rows if d <= FWD_END])
    baseline = rates.get(2007)
    if not baseline or baseline <= 0:
        for y in range(2006, 2002, -1):
            if rates.get(y, 0) > 0:
                baseline = rates[y]
                break
    div_cut = False
    for yr in range(2008, 2015):
        year_divs = [v for d, v in div_rows if int(str(d)[:4]) == yr and v > 0]
        if not year_divs:
            div_cut = True
            break
        if not baseline or baseline <= 0:
            continue
        r = rates.get(yr)
        if r is None and year_divs:
            r = float(sum(year_divs))
        if r is not None and r < 0.9 * baseline:
            div_cut = True
            break

    return {
        "precrisis_peak": precrisis_peak,
        "fwd_max_dd": worst,
        "fwd_trough_date": trough_d,
        "recovered_by_2014": recovered,
        "recovery_date_fwd": rec_d,
        "months_to_recover": months_rec,
        "div_cut": div_cut,
    }


def evaluate_ticker(
    ticker: str,
    store: FMPBacktestStore,
    spy: Dict[str, float],
) -> Optional[Dict[str, Any]]:
    closes, volumes = store.load_prices(ticker)
    div_rows = store.load_dividends(ticker)
    pre_divs = [(d, v) for d, v in div_rows if d <= ASOF]
    if not pre_divs:
        return None

    pre_closes = slice_pre(closes)
    if len(pre_closes) < 250:
        return None

    pre_volumes = [v for (d, _), v in zip(closes, volumes) if str(d)[:10] <= ASOF]

    asof_m = score_asof_2007(pre_closes, pre_volumes, pre_divs, spy)
    fwd_m = forward_outcomes(closes, div_rows)
    if not fwd_m:
        return None

    row = {"ticker": ticker, **asof_m, **fwd_m}
    row["gates_passed_2007"] = gates_passed_2007(row)
    return row


def median_or_none(vals: Sequence[float]) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return statistics.median(clean) if clean else None


def bucket_stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "n": 0,
            "div_cut_pct": None,
            "median_fwd_max_dd": None,
            "recovered_pct": None,
            "median_months_to_recover": None,
        }
    cuts = sum(1 for r in rows if r.get("div_cut"))
    recs = sum(1 for r in rows if r.get("recovered_by_2014"))
    dds = [r["fwd_max_dd"] for r in rows if r.get("fwd_max_dd") is not None]
    mths = [r["months_to_recover"] for r in rows if r.get("months_to_recover") is not None]
    return {
        "n": n,
        "div_cut_pct": 100.0 * cuts / n,
        "median_fwd_max_dd": statistics.median(dds) if dds else None,
        "recovered_pct": 100.0 * recs / n,
        "median_months_to_recover": statistics.median(mths) if mths else None,
    }


def print_bucket_table(title: str, buckets: Dict[str, Dict[str, Any]]) -> None:
    print(f"\n{title}")
    hdr = f"{'bucket':<12} {'N':>6} {'div_cut%':>9} {'med_fwdDD':>10} {'recov%':>8} {'med_mo_rec':>11}"
    print(hdr)
    print("-" * len(hdr))
    for name in sorted(buckets.keys()):
        s = buckets[name]
        dd = s["median_fwd_max_dd"]
        mo = s["median_months_to_recover"]
        print(
            f"{name:<12} {s['n']:6d} "
            f"{(s['div_cut_pct'] if s['div_cut_pct'] is not None else 0):9.1f} "
            f"{(dd * 100 if dd is not None else 0):10.1f}% "
            f"{(s['recovered_pct'] if s['recovered_pct'] is not None else 0):8.1f} "
            f"{(mo if mo is not None else 0):11.1f}"
        )


def print_named_cohort(rows_by_ticker: Dict[str, Dict[str, Any]]) -> None:
    print("\nNamed cohort (controls + known casualties)")
    hdr = (
        f"{'ticker':<6} {'score':>6} {'verdict':<7} {'gates':>6} "
        f"{'fwd_max_dd':>10} {'recov2014':>9} {'div_cut':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for t in NAMED_COHORT:
        r = rows_by_ticker.get(t)
        if not r:
            print(f"{t:<6} {'—':>6} {'—':<7} {'—':>6} {'—':>10} {'—':>9} {'—':>8}")
            continue
        print(
            f"{t:<6} {r['fortress_score_2007']:6.1f} {r['verdict_2007']:<7} "
            f"{('Y' if r['gates_passed_2007'] else 'N'):>6} "
            f"{r['fwd_max_dd'] * 100:9.1f}% "
            f"{('Y' if r['recovered_by_2014'] else 'N'):>9} "
            f"{('Y' if r['div_cut'] else 'N'):>8}"
        )


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v:.1f}" if v is not None else "n/a"


def headline_compare(all_rows: Sequence[Dict[str, Any]], passed: Sequence[Dict[str, Any]]) -> None:
    base = bucket_stats(all_rows)
    gate = bucket_stats(passed)
    cut_delta = (gate["div_cut_pct"] or 0) - (base["div_cut_pct"] or 0)
    rec_delta = (gate["recovered_pct"] or 0) - (base["recovered_pct"] or 0)
    print("\nHEADLINE: gates_passed_2007 vs full universe baseline")
    print(f"  Universe N={base['n']}")
    print(
        f"  Baseline div-cut rate: {_fmt_pct(base['div_cut_pct'])}%  |  "
        f"Passed-screen div-cut rate: {_fmt_pct(gate['div_cut_pct'])}%  |  "
        f"Δ cut-rate: {cut_delta:+.1f} pp"
    )
    print(
        f"  Baseline recovered-by-2014: {_fmt_pct(base['recovered_pct'])}%  |  "
        f"Passed-screen recovered: {_fmt_pct(gate['recovered_pct'])}%  |  "
        f"Δ recovery: {rec_delta:+.1f} pp"
    )
    b_dd = base["median_fwd_max_dd"]
    g_dd = gate["median_fwd_max_dd"]
    print(
        f"  Baseline median fwd max DD: {(b_dd * 100 if b_dd is not None else 0):.1f}%  |  "
        f"Passed-screen median fwd max DD: {(g_dd * 100 if g_dd is not None else 0):.1f}%"
    )


def verdict_block(all_rows: Sequence[Dict[str, Any]], passed: Sequence[Dict[str, Any]]) -> str:
    base = bucket_stats(all_rows)
    gate = bucket_stats(passed)
    elite = [r for r in all_rows if r["verdict_2007"] == "ELITE"]
    elite_s = bucket_stats(elite)

    cut_delta = (gate["div_cut_pct"] or 0) - (base["div_cut_pct"] or 0)
    rec_delta = (gate["recovered_pct"] or 0) - (base["recovered_pct"] or 0)

    fair_strong = [r for r in all_rows if r["verdict_2007"] in ("FAIR", "STRONG")]
    fs_s = bucket_stats(fair_strong)
    weak_s = bucket_stats([r for r in all_rows if r["verdict_2007"] == "WEAK"])
    verdict_cut_gap = (weak_s["div_cut_pct"] or 0) - (fs_s["div_cut_pct"] or 0)
    verdict_rec_gap = (fs_s["recovered_pct"] or 0) - (weak_s["recovered_pct"] or 0)

    if gate["n"] > 0 and cut_delta < -3 and rec_delta > 3:
        label = "yes — meaningful out-of-sample separation"
    elif gate["n"] > 0 and cut_delta < 0 and rec_delta > 0:
        label = "partial — directionally right but modest"
    elif verdict_cut_gap > 5 and verdict_rec_gap > 5:
        label = "partial — FAIR/STRONG vs WEAK separate, but 2007 gates pass no one"
    else:
        label = "no / weak — gates do not clearly separate GFC outcomes"

    lines = [
        f"VERDICT: {label}.",
        (
            f"Among {base['n']} survivor-universe names, gates-passed ({gate['n']} names) "
            f"cut-rate {_fmt_pct(gate['div_cut_pct'])}% vs baseline {_fmt_pct(base['div_cut_pct'])}% "
            f"({cut_delta:+.1f} pp); recovered-by-2014 {_fmt_pct(gate['recovered_pct'])}% vs "
            f"{_fmt_pct(base['recovered_pct'])}% ({rec_delta:+.1f} pp)."
        ),
        (
            f"ELITE bucket (N={elite_s['n']}): cut {_fmt_pct(elite_s['div_cut_pct'])}%, "
            f"recovered {_fmt_pct(elite_s['recovered_pct'])}%, median fwd DD "
            f"{(elite_s['median_fwd_max_dd'] or 0) * 100:.1f}%."
        ),
        (
            f"FAIR+STRONG (N={fs_s['n']}) vs WEAK (N={weak_s['n']}): cut "
            f"{_fmt_pct(fs_s['div_cut_pct'])}% vs {_fmt_pct(weak_s['div_cut_pct'])}% "
            f"({verdict_cut_gap:+.1f} pp); recovered {_fmt_pct(fs_s['recovered_pct'])}% vs "
            f"{_fmt_pct(weak_s['recovered_pct'])}% ({verdict_rec_gap:+.1f} pp)."
        ),
        "Scoring used price+dividend pillars only (no TTM fundamentals). Survivor-only universe.",
        (
            "External benchmark: ~28% of 2007 S&P Dividend Aristocrats cut within 3y of GFC; "
            "this DB cannot observe delisted failures."
        ),
    ]
    return "\n".join(lines)


def run_oracle(rows_by_ticker: Dict[str, Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    for t, spec in ORACLE_TICKERS.items():
        r = rows_by_ticker.get(t)
        if not r:
            errors.append(f"{t}: missing from results")
            continue
        if r.get("div_cut") != spec["div_cut"]:
            errors.append(f"{t}: div_cut expected {spec['div_cut']}, got {r.get('div_cut')}")
        if r.get("recovered_by_2014") != spec["recovered_by_2014"]:
            errors.append(
                f"{t}: recovered_by_2014 expected {spec['recovered_by_2014']}, "
                f"got {r.get('recovered_by_2014')}"
            )
        dd = r.get("fwd_max_dd")
        if "fwd_max_dd_lo" in spec and dd is not None and dd < spec["fwd_max_dd_lo"]:
            errors.append(f"{t}: fwd_max_dd {dd:.3f} < lo {spec['fwd_max_dd_lo']}")
        if "fwd_max_dd_hi" in spec and dd is not None and dd > spec["fwd_max_dd_hi"]:
            errors.append(f"{t}: fwd_max_dd {dd:.3f} > hi {spec['fwd_max_dd_hi']}")
    return errors


def write_csv(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["fortress_score_2007"], reverse=True):
            w.writerow(r)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    default_out = str(_SCRIPT_DIR.parent / "scratchpad" / "backtest_asof2007.csv")
    p = argparse.ArgumentParser(
        description="Fortress screener GFC out-of-sample backtest (as-of 2007)"
    )
    p.add_argument("--fmp-dir", default=None)
    p.add_argument("--out", default=default_out)
    p.add_argument("--max-tickers", type=int, default=None, help="Debug: cap universe size")
    p.add_argument("--oracle-only", action="store_true", help="Run oracle on named tickers only")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    fmp_dir = args.fmp_dir or FMP_DIR
    assert_windows_disjoint()

    try:
        spy = load_spy(fmp_dir)
        universe = build_universe(fmp_dir)
    except (OSError, sqlite3.Error) as exc:
        print(f"ERROR: FMP data unavailable at {fmp_dir}: {exc}", file=sys.stderr)
        return 2
    if not spy:
        print("ERROR: SPY price history missing or empty — cannot compute beta.", file=sys.stderr)
        return 2
    if args.max_tickers:
        universe = universe[: args.max_tickers]
    if args.oracle_only:
        universe = list(NAMED_COHORT)

    print(f"Fortress as-of-2007 backtest | universe={len(universe)} tickers | FMP={fmp_dir}")
    print("SCORING NOTE: price+dividend pillars only — TTM fundamentals excluded (look-ahead).")
    print(
        "UNIVERSE NOTE: >=500 total price rows, >=250 rows on/before 2007-12-31, "
        ">=1 pre-2007 dividend (local DB starts ~2006-06)."
    )
    print(
        "SURVIVORSHIP NOTE: universe requires history in today's DB; "
        "delisted/bankrupt names are absent (~28% Aristocrat cut rate external benchmark)."
    )

    store = FMPBacktestStore(fmp_dir)
    rows: List[Dict[str, Any]] = []
    try:
        for i, ticker in enumerate(universe, 1):
            try:
                row = evaluate_ticker(ticker, store, spy)
            except (ValueError, TypeError, sqlite3.Error, ZeroDivisionError, KeyError) as exc:
                print(f"  skip {ticker}: {exc}", file=sys.stderr)
                continue
            if row:
                rows.append(row)
            if i % 500 == 0:
                print(f"  ... processed {i}/{len(universe)}", flush=True)
    finally:
        store.close()

    print(f"Evaluated {len(rows)} names with complete as-of + forward windows.")

    rows_by_ticker = {r["ticker"]: r for r in rows}

    # Bucket by verdict
    verdict_buckets: Dict[str, Dict[str, Any]] = {}
    for v in ("ELITE", "STRONG", "FAIR", "WEAK"):
        verdict_buckets[v] = bucket_stats([r for r in rows if r["verdict_2007"] == v])
    print_bucket_table("Buckets by verdict_2007", verdict_buckets)

    gate_buckets = {
        "passed": bucket_stats([r for r in rows if r["gates_passed_2007"]]),
        "failed": bucket_stats([r for r in rows if not r["gates_passed_2007"]]),
    }
    print_bucket_table("Buckets by gates_passed_2007", gate_buckets)

    passed_rows = [r for r in rows if r["gates_passed_2007"]]
    headline_compare(rows, passed_rows)
    if not passed_rows:
        print(
            "  (No names passed all 2007 gates — common blockers with truncated "
            "2006+ price history: recovered_to_2007=False, div_streak<10.)"
        )
    print_named_cohort(rows_by_ticker)
    print("\n" + verdict_block(rows, passed_rows))

    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    write_csv(out_path, rows)
    print(f"\nWrote {len(rows)} rows → {out_path}")

    oracle_errors = run_oracle(rows_by_ticker)
    if oracle_errors:
        print("\nORACLE FAILURES:")
        for e in oracle_errors:
            print(f"  - {e}")
        return 1
    print("\nORACLE: all named-ticker checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
