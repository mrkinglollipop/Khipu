#!/usr/bin/env python3
"""
Fortress Compounder Screener — finds stocks sharing the "CINF archetype":
trustworthy long-term grinders ideal for selling covered calls against.

HONEST CAVEAT: Deep drawdowns DO happen. CINF fell ~63% and took ~6.6 years to
recover. Covered-call premium does NOT protect against that magnitude of decline.
The edge is recovery-certainty plus getting paid to wait — not downside protection.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FMP_DIR = os.environ.get("FMP_DIR", "/Volumes/Cloud Storage/Databases/fmp")

TRAIT_FIELDS = [
    "div_streak_yrs",
    "vol_252",
    "beta_5y",
    "price_cagr",
    "pct_pos_years",
    "max_drawdown",
    "recovery_months",
    "fcf_yield",
    "payout_ratio",
    "jump_days_per_yr",
]

# Hook for future IV-HV spread / IV-rank when an options source is wired.
VRP_IV_HV_SPREAD: Optional[float] = None
VRP_IV_RANK: Optional[float] = None


# ---------------------------------------------------------------------------
# Reference math (verified against local FMP DBs)
# ---------------------------------------------------------------------------


def ann_vol(closes: Sequence[float]) -> Optional[float]:
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets) * math.sqrt(252)


def cagr(series: Sequence[Tuple[str, float]]) -> Optional[float]:
    if len(series) < 2:
        return None
    v0, v1 = series[0][1], series[-1][1]
    y = (date.fromisoformat(series[-1][0]) - date.fromisoformat(series[0][0])).days / 365.25
    if v0 <= 0 or y <= 0:
        return None
    return (v1 / v0) ** (1 / y) - 1


def max_dd(series: Sequence[Tuple[str, float]]) -> Tuple[float, str, str, Optional[str]]:
    peak = series[0][1]
    peak_d = series[0][0]
    worst = 0.0
    wpd = wtd = series[0][0]
    wpeak = peak
    for d, v in series:
        if v > peak:
            peak = v
            peak_d = d
        dd = v / peak - 1
        if dd < worst:
            worst = dd
            wtd = d
            wpd = peak_d
            wpeak = peak
    rec: Optional[str] = None
    seen = False
    for d, v in series:
        if d == wtd:
            seen = True
        if seen and v >= wpeak:
            rec = d
            break
    return worst, wpd, wtd, rec


def months_between(d1: str, d2: str) -> int:
    a = date.fromisoformat(d1)
    b = date.fromisoformat(d2)
    return (b.year - a.year) * 12 + (b.month - a.month)


def beta(
    ticker_by_date: Dict[str, float],
    spy_by_date: Dict[str, float],
    lookback: int = 1260,
) -> Optional[float]:
    common = sorted(set(ticker_by_date) & set(spy_by_date))
    if lookback:
        common = common[-lookback:]
    cr: List[float] = []
    sr: List[float] = []
    for i in range(1, len(common)):
        d0, d1 = common[i - 1], common[i]
        if (
            ticker_by_date[d0] > 0
            and ticker_by_date[d1] > 0
            and spy_by_date[d0] > 0
            and spy_by_date[d1] > 0
        ):
            cr.append(math.log(ticker_by_date[d1] / ticker_by_date[d0]))
            sr.append(math.log(spy_by_date[d1] / spy_by_date[d0]))
    if len(sr) < 100:
        return None
    var = statistics.pvariance(sr)
    if var == 0:
        return None
    mc = statistics.mean(cr)
    ms = statistics.mean(sr)
    cov = sum((cr[i] - mc) * (sr[i] - ms) for i in range(len(sr))) / len(sr)
    return cov / var


# ---------------------------------------------------------------------------
# Dividend run-rate streak
# ---------------------------------------------------------------------------


def annual_run_rates(div_rows: Sequence[Tuple[str, float]]) -> Dict[int, float]:
    """Annual run-rate from median regular payment; drop specials >2.5x year median."""
    by_year: Dict[int, List[Tuple[str, float]]] = defaultdict(list)
    for d, amt in div_rows:
        if amt and amt > 0:
            by_year[int(str(d)[:4])].append((str(d)[:10], float(amt)))
    rates: Dict[int, float] = {}
    for yr, items in by_year.items():
        pays = [amt for _, amt in items]
        med = statistics.median(pays)
        regular = [p for p in pays if p <= 2.5 * med]
        if not regular:
            continue
        n = len(regular)
        if n == 1:
            rates[yr] = regular[0]
        else:
            rates[yr] = statistics.median(regular) * min(n, 12)
    return rates


def div_streak_years(rates: Dict[int, float], as_of_year: Optional[int] = None) -> int:
    """Consecutive complete years (most recent backward) the dividend was maintained-or-raised.

    Uses a 2% tolerance rather than strict increase: split-adjusted run-rates carry rounding
    noise that can dip a real raise fractionally, and a held (not cut) dividend through a
    recession is still a trust signal. A genuine cut (typically 30-90%) still breaks the streak.
    """
    if not rates:
        return 0
    if as_of_year is None:
        as_of_year = date.today().year - 1
    # Ignore partial current-year run-rates (incomplete dividend calendar).
    filtered = {y: v for y, v in rates.items() if y <= as_of_year}
    while as_of_year not in filtered and as_of_year > 1900:
        as_of_year -= 1
    streak = 0
    yr = as_of_year
    while yr in filtered and (yr - 1) in filtered and filtered[yr] >= filtered[yr - 1] * 0.98:
        streak += 1
        yr -= 1
    if as_of_year not in filtered:
        return 0
    return streak + 1


def survived_recession(
    price_dates: Sequence[str],
    rates: Dict[int, float],
) -> bool:
    has_span = any("2007-06" <= d[:7] <= "2010-01" for d in price_dates)
    if not has_span:
        return False
    r08 = rates.get(2008)
    r10 = rates.get(2010)
    if r08 is None or r10 is None:
        return False
    return r10 >= r08


# ---------------------------------------------------------------------------
# Price-derived metrics
# ---------------------------------------------------------------------------


def pct_pos_periods(series: Sequence[Tuple[str, float]], period_len: int) -> Optional[float]:
    """period_len=4 for years, 7 for months (YYYY-MM)."""
    ends: Dict[str, Tuple[str, float]] = {}
    for d, v in series:
        key = d[:period_len]
        ends[key] = (d, v)
    ordered = sorted(ends.values(), key=lambda x: x[0])
    if len(ordered) < 2:
        return None
    pos = sum(1 for i in range(1, len(ordered)) if ordered[i][1] > ordered[i - 1][1])
    return pos / (len(ordered) - 1)


def above_200dma_frac(
    closes: Sequence[Tuple[str, float]], lookback_days: int = 1260
) -> Optional[float]:
    if len(closes) < 200:
        return None
    subset = closes[-lookback_days:] if len(closes) > lookback_days else closes
    if len(subset) < 200:
        return None
    above = 0
    total = 0
    prices = [v for _, v in subset]
    for i in range(199, len(prices)):
        ma = sum(prices[i - 199 : i + 1]) / 200
        if prices[i] > ma:
            above += 1
        total += 1
    return above / total if total else None


def jump_days_per_yr(series: Sequence[Tuple[str, float]]) -> Optional[float]:
    if len(series) < 2:
        return None
    jumps_by_year: Dict[int, int] = defaultdict(int)
    days_by_year: Dict[int, int] = defaultdict(int)
    for i in range(1, len(series)):
        d0, c0 = series[i - 1]
        d1, c1 = series[i]
        if c0 <= 0:
            continue
        yr = int(d1[:4])
        days_by_year[yr] += 1
        chg = abs(c1 / c0 - 1)
        if chg > 0.05:
            jumps_by_year[yr] += 1
    if not days_by_year:
        return None
    per_yr = [jumps_by_year[y] for y in days_by_year if days_by_year[y] > 0]
    return statistics.mean(per_yr) if per_yr else None


def median_dollar_volume(closes: Sequence[Tuple[str, float]], volumes: Sequence[int]) -> float:
    dv = [c * v for (_, c), v in zip(closes, volumes) if c > 0 and v > 0]
    return statistics.median(dv) if dv else 0.0


def compute_recovered(
    series: Sequence[Tuple[str, float]],
    worst_dd: float,
    recovery_date: Optional[str],
) -> bool:
    """True when history shows a material drawdown that later recovered."""
    if not series or recovery_date is None:
        return False
    if worst_dd > -0.10:
        return False
    return True


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def band_score(
    val: Optional[float],
    lo_sweet: float,
    hi_sweet: float,
    lo_zero: float,
    hi_zero: float,
    max_pts: float,
) -> float:
    if val is None:
        return 0.0
    if lo_sweet <= val <= hi_sweet:
        return max_pts
    if val < lo_sweet:
        if val <= lo_zero:
            return 0.0
        return max_pts * (val - lo_zero) / (lo_sweet - lo_zero)
    if val >= hi_zero:
        return 0.0
    return max_pts * (hi_zero - val) / (hi_zero - hi_sweet)


def streak_subscore(streak: int) -> float:
    if streak >= 50:
        return 25.0
    if streak >= 25:
        return 20.0 + (streak - 25) / 25.0 * 5.0
    if streak >= 15:
        return 14.0 + (streak - 15) / 10.0 * 6.0
    if streak >= 10:
        return 8.0 + (streak - 10) / 5.0 * 6.0
    return 0.0


def pillar_a(streak: int, survived: bool) -> float:
    s = streak_subscore(streak)
    if survived:
        s += 3.0
    return min(s, 25.0)


def pillar_b(
    recovered: bool,
    recovery_date: Optional[str],
    book_value: Optional[float],
    roe: Optional[float],
    debt_to_equity: Optional[float],
) -> float:
    s = 0.0
    if recovered:
        s += 10.0
    if recovery_date is not None:
        s += 5.0
    if book_value is not None and book_value > 0:
        s += 4.0
    if roe is not None and roe >= 0.10:
        s += 3.0
    elif roe is None:
        s += 1.0
    # debt_to_equity: don't penalize missing or insurer-skewed values
    if debt_to_equity is None or debt_to_equity <= 2.0:
        s += 3.0
    return min(s, 25.0)


def pillar_c(
    price_cagr: Optional[float],
    pct_pos_years: Optional[float],
    above_dma: Optional[float],
    jumps: Optional[float],
) -> float:
    s = band_score(price_cagr, 0.03, 0.15, -0.02, 0.30, 8.0)
    if pct_pos_years is not None and pct_pos_years >= 0.55:
        s += 6.0
    if above_dma is not None and above_dma >= 0.6:
        s += 3.0
    if jumps is not None and jumps <= 6.0:
        s += 3.0
    return min(s, 20.0)


def pillar_d(vol_252: Optional[float], beta_5y: Optional[float]) -> float:
    vol_pts = band_score(vol_252, 0.12, 0.28, 0.0, 0.35, 10.0)
    if beta_5y is None:
        beta_pts = 5.0
    else:
        beta_pts = band_score(beta_5y, 0.30, 0.85, 0.0, 1.10, 10.0)
    return min(vol_pts + beta_pts, 20.0)


def pillar_e(median_dvol: float) -> float:
    if VRP_IV_HV_SPREAD is not None and VRP_IV_RANK is not None:
        # Future hook: blend IV-HV spread + IV rank when wired.
        pass
    if median_dvol >= 20_000_000:
        return 8.0
    if median_dvol >= 5_000_000:
        return 5.0
    if median_dvol >= 1_000_000:
        return 3.0
    return 1.0


def fortress_total(a: float, b: float, c: float, d: float, e: float) -> float:
    return round(a + b + c + d + e, 1)


def verdict(score: float) -> str:
    if score >= 80:
        return "ELITE"
    if score >= 68:
        return "STRONG"
    if score >= 55:
        return "FAIR"
    return "WEAK"


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------


def check_gates(m: Dict[str, Any]) -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []

    streak = m.get("div_streak_yrs") or 0
    ok = streak >= 10
    results.append(("div_streak_yrs >= 10", ok, f"streak={streak}"))

    vol = m.get("vol_252")
    ok = vol is not None and vol <= 0.35
    results.append(("vol_252 <= 0.35", ok, f"vol_252={vol}"))

    beta_v = m.get("beta_5y")
    ok = beta_v is None or beta_v <= 1.10
    results.append(("beta_5y <= 1.10", ok, f"beta_5y={beta_v} (None soft-pass)"))

    rows = m.get("price_rows") or 0
    ok = rows >= 2000
    results.append(("price_rows >= 2000", ok, f"rows={rows}"))

    rec = m.get("recovered")
    ok = rec is True
    results.append(("recovered == True", ok, f"recovered={rec}"))

    mc = m.get("market_cap")
    lc = m.get("last_close")
    mc_ok = mc is not None and mc >= 1e9
    price_ok = lc is not None and 5 <= lc <= 1000
    ok = mc_ok and price_ok
    results.append(
        (
            "market_cap >= 1B & 5 <= close <= 1000",
            ok,
            f"market_cap={mc}, last_close={lc}",
        )
    )

    fcf = m.get("fcf_yield")
    ok = fcf is None or fcf > 0
    results.append(("fcf_yield None or > 0", ok, f"fcf_yield={fcf}"))

    return results


def passes_all_gates(m: Dict[str, Any]) -> bool:
    for _, ok, _ in check_gates(m):
        if not ok:
            return False
    return True


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


class FMPStore:
    def __init__(self, fmp_dir: str):
        self.fmp_dir = fmp_dir
        self._div_cache: Optional[Dict[str, List[Tuple[str, float]]]] = None
        self._fund_cache: Optional[Dict[str, Dict[str, Optional[float]]]] = None
        self._spy_by_date: Optional[Dict[str, float]] = None
        self._price_row_counts: Optional[Dict[str, int]] = None

    def _connect(self, name: str) -> sqlite3.Connection:
        return sqlite3.connect(os.path.join(self.fmp_dir, name))

    def dividend_rows(self) -> Dict[str, List[Tuple[str, float]]]:
        if self._div_cache is not None:
            return self._div_cache
        conn = self._connect("dividends.sqlite")
        # adj_dividend is split-adjusted; raw `dividend` makes a stock split look like a
        # dividend cut and snaps the streak (e.g. CINF 3:1 in 1998). COALESCE falls back
        # to raw for the rare names lacking an adjusted figure.
        cur = conn.execute(
            "SELECT ticker, date, COALESCE(adj_dividend, dividend) FROM dividends "
            "WHERE COALESCE(adj_dividend, dividend) IS NOT NULL"
        )
        out: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        for ticker, d, amt in cur:
            out[ticker].append((str(d)[:10], float(amt)))
        conn.close()
        for t in out:
            out[t].sort(key=lambda x: x[0])
        self._div_cache = dict(out)
        return self._div_cache

    def price_row_counts(self) -> Dict[str, int]:
        if self._price_row_counts is not None:
            return self._price_row_counts
        conn = self._connect("price_history.sqlite")
        cur = conn.execute(
            "SELECT ticker, COUNT(*) FROM daily_prices GROUP BY ticker HAVING COUNT(*) >= 2000"
        )
        self._price_row_counts = {t: n for t, n in cur}
        conn.close()
        return self._price_row_counts

    def load_prices(self, ticker: str) -> Tuple[List[Tuple[str, float]], List[int]]:
        conn = self._connect("price_history.sqlite")
        cur = conn.execute(
            "SELECT date, close, volume FROM daily_prices WHERE ticker=? ORDER BY date",
            (ticker,),
        )
        rows = cur.fetchall()
        conn.close()
        closes: List[Tuple[str, float]] = []
        volumes: List[int] = []
        for d, c, v in rows:
            if c is None:
                continue
            closes.append((str(d)[:10], float(c)))
            volumes.append(int(v) if v is not None else 0)
        return closes, volumes

    def spy_by_date(self) -> Dict[str, float]:
        if self._spy_by_date is not None:
            return self._spy_by_date
        conn = self._connect("price_history.sqlite")
        cur = conn.execute("SELECT date, close FROM daily_prices WHERE ticker='SPY' ORDER BY date")
        self._spy_by_date = {str(d)[:10]: float(c) for d, c in cur if c is not None}
        conn.close()
        return self._spy_by_date

    def fundamentals(self) -> Dict[str, Dict[str, Optional[float]]]:
        if self._fund_cache is not None:
            return self._fund_cache
        conn = self._connect("fundamentals.sqlite")
        km_cols = {
            "free_cash_flow_yield",
            "debt_to_equity",
            "roe",
            "roic",
            "dividend_yield",
            "payout_ratio",
            "book_value_per_share",
            "market_cap",
            "enterprise_value",
            "pb_ratio",
        }
        rt_map = {
            "return_on_equity": "roe",
            "dividend_payout_ratio": "payout_ratio",
            "debt_equity_ratio": "debt_to_equity",
            "dividend_yield": "dividend_yield",
            "price_to_book_ratio": "pb_ratio",
        }
        km_rows = conn.execute("SELECT * FROM key_metrics_ttm").fetchall()
        km_names = [d[1] for d in conn.execute("PRAGMA table_info(key_metrics_ttm)")]
        rt_rows = conn.execute("SELECT * FROM ratios_ttm").fetchall()
        rt_names = [d[1] for d in conn.execute("PRAGMA table_info(ratios_ttm)")]
        conn.close()

        out: Dict[str, Dict[str, Optional[float]]] = {}
        for row in km_rows:
            ticker = row[0]
            rec: Dict[str, Optional[float]] = {k: None for k in km_cols}
            for i, col in enumerate(km_names):
                if col in km_cols and row[i] is not None:
                    rec[col] = float(row[i])
            # market_cap is an unpopulated column in key_metrics_ttm; enterprise_value is
            # populated. For this low-leverage dividend universe EV approximates market cap,
            # so it is a sound size proxy for the >=$1B gate (ratios_ttm carries no mcap).
            if rec.get("market_cap") is None and rec.get("enterprise_value") is not None:
                rec["market_cap"] = rec["enterprise_value"]
            out[ticker] = rec

        for row in rt_rows:
            ticker = row[0]
            if ticker not in out:
                out[ticker] = {k: None for k in km_cols}
            rec = out[ticker]
            for i, col in enumerate(rt_names):
                if col in rt_map and row[i] is not None and rec[rt_map[col]] is None:
                    rec[rt_map[col]] = float(row[i])

        self._fund_cache = out
        return self._fund_cache


# ---------------------------------------------------------------------------
# Per-ticker evaluation
# ---------------------------------------------------------------------------


def evaluate_ticker(
    ticker: str,
    store: FMPStore,
    div_rows: List[Tuple[str, float]],
    spy: Dict[str, float],
    fund: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    closes, volumes = store.load_prices(ticker)
    price_rows = len(closes)
    close_vals = [c for _, c in closes]
    by_date = {d: c for d, c in closes}

    rates = annual_run_rates(div_rows)
    streak = div_streak_years(rates)
    surv = survived_recession([d for d, _ in closes], rates)

    vol_252 = ann_vol(close_vals[-253:]) if len(close_vals) >= 253 else ann_vol(close_vals)
    vol_full = ann_vol(close_vals)
    beta_5y = beta(by_date, spy, lookback=1260)

    worst, peak_d, trough_d, rec_d = max_dd(closes) if closes else (0.0, "", "", None)
    rec_months = months_between(trough_d, rec_d) if rec_d else None
    recovered = compute_recovered(closes, worst, rec_d)

    price_cagr_v = cagr(closes)
    pct_years = pct_pos_periods(closes, 4)
    pct_months = pct_pos_periods(closes, 7)
    above_dma = above_200dma_frac(closes)
    jumps = jump_days_per_yr(closes)
    med_dvol = median_dollar_volume(closes[-1260:], volumes[-1260:])

    last_close = closes[-1][1] if closes else None

    fcf_yield = fund.get("free_cash_flow_yield")
    roe = fund.get("roe")
    payout = fund.get("payout_ratio")
    dte = fund.get("debt_to_equity")
    bv = fund.get("book_value_per_share")
    mcap = fund.get("market_cap")
    div_yield = fund.get("dividend_yield")
    # book_value_per_share is unpopulated in the fundamentals tables; a positive
    # price-to-book (from ratios_ttm) implies positive book equity — the solvency
    # signal pillar B actually wants.
    pb = fund.get("pb_ratio")
    book_signal = bv if bv is not None else (1.0 if (pb is not None and pb > 0) else None)

    a = pillar_a(streak, surv)
    b = pillar_b(recovered, rec_d, book_signal, roe, dte)
    c = pillar_c(price_cagr_v, pct_years, above_dma, jumps)
    d = pillar_d(vol_252, beta_5y)
    e = pillar_e(med_dvol)
    total = fortress_total(a, b, c, d, e)

    return {
        "ticker": ticker,
        "sector": None,
        "price_rows": price_rows,
        "last_close": last_close,
        "price_cagr": price_cagr_v,
        "vol_252": vol_252,
        "vol_full": vol_full,
        "beta_5y": beta_5y,
        "max_drawdown": worst,
        "dd_peak_date": peak_d,
        "dd_trough_date": trough_d,
        "recovery_date": rec_d,
        "recovery_months": rec_months,
        "recovered": recovered,
        "pct_pos_years": pct_years,
        "pct_pos_months": pct_months,
        "above_200dma_frac": above_dma,
        "jump_days_per_yr": jumps,
        "div_streak_yrs": streak,
        "survived_recession": surv,
        "fcf_yield": fcf_yield,
        "roe": roe,
        "payout_ratio": payout,
        "debt_to_equity": dte,
        "book_value_per_share": bv,
        "market_cap": mcap,
        "div_yield": div_yield,
        "median_dollar_vol": med_dvol,
        "score_a": round(a, 1),
        "score_b": round(b, 1),
        "score_c": round(c, 1),
        "score_d": round(d, 1),
        "score_e": round(e, 1),
        "fortress_score": total,
        "verdict": verdict(total),
        "cinf_distance": None,
    }


# ---------------------------------------------------------------------------
# Similarity distance
# ---------------------------------------------------------------------------


def impute_medians(rows: Sequence[Dict[str, Any]], fields: Sequence[str]) -> Dict[str, float]:
    medians: Dict[str, float] = {}
    for f in fields:
        vals = [r[f] for r in rows if r.get(f) is not None]
        medians[f] = statistics.median(vals) if vals else 0.0
    return medians


def zscore_vector(
    rows: Sequence[Dict[str, Any]],
    fields: Sequence[str],
    medians: Dict[str, float],
) -> Tuple[List[List[float]], List[float], List[float]]:
    imputed: List[List[float]] = []
    for r in rows:
        imputed.append([r[f] if r.get(f) is not None else medians[f] for f in fields])
    means = []
    stds = []
    for j, f in enumerate(fields):
        col = [row[j] for row in imputed]
        mu = statistics.mean(col)
        sd = statistics.pstdev(col) if len(col) > 1 else 1.0
        if sd == 0:
            sd = 1.0
        means.append(mu)
        stds.append(sd)
    z_rows = [
        [(imputed[i][j] - means[j]) / stds[j] for j in range(len(fields))]
        for i in range(len(imputed))
    ]
    return z_rows, means, stds


def euclidean(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def assign_distances(
    rows: Sequence[Dict[str, Any]],
    anchor_ticker: str,
    fields: Sequence[str] = TRAIT_FIELDS,
) -> None:
    medians = impute_medians(rows, fields)
    z_rows, _, _ = zscore_vector(rows, fields, medians)
    ticker_to_z = {rows[i]["ticker"]: z_rows[i] for i in range(len(rows))}
    anchor_z = ticker_to_z.get(anchor_ticker)
    if anchor_z is None:
        return
    for i, r in enumerate(rows):
        r["cinf_distance"] = round(euclidean(z_rows[i], anchor_z), 4)


# ---------------------------------------------------------------------------
# Universe scan
# ---------------------------------------------------------------------------


def build_universe(store: FMPStore) -> List[str]:
    divs = store.dividend_rows()
    counts = store.price_row_counts()
    candidates = sorted(set(divs) & set(counts))
    # Cheap pre-filter: div streak >= 10 before heavy price math
    survivors: List[str] = []
    for t in candidates:
        rates = annual_run_rates(divs[t])
        if div_streak_years(rates) >= 10:
            survivors.append(t)
    return survivors


def run_screen(
    store: FMPStore,
    anchor: str = "CINF",
    max_tickers: Optional[int] = None,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    universe = build_universe(store)
    if max_tickers:
        universe = universe[:max_tickers]

    spy = store.spy_by_date()
    funds = store.fundamentals()
    divs = store.dividend_rows()

    evaluated: List[Dict[str, Any]] = []
    for ticker in universe:
        try:
            fund = funds.get(ticker, {})
            m = evaluate_ticker(ticker, store, divs.get(ticker, []), spy, fund)
        except (ValueError, TypeError, sqlite3.Error, ZeroDivisionError) as exc:
            print(f"  skip {ticker}: {exc}", file=sys.stderr)
            continue
        if not passes_all_gates(m):
            continue
        if m["fortress_score"] < min_score:
            continue
        evaluated.append(m)

    if evaluated:
        assign_distances(evaluated, anchor)
    return evaluated


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def fmt_pct(v: Optional[float], decimals: int = 1) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.{decimals}f}"


def print_scorecard(m: Dict[str, Any]) -> None:
    print(f"\n{'=' * 72}")
    print(f"  FORTRESS SCORECARD: {m['ticker']}")
    print(f"{'=' * 72}")
    print("\n--- Metrics ---")
    metrics = [
        ("price_cagr", fmt_pct(m.get("price_cagr")) + "%"),
        ("vol_252", fmt_pct(m.get("vol_252")) + "%"),
        ("vol_full", fmt_pct(m.get("vol_full")) + "%"),
        ("beta_5y", f"{m.get('beta_5y'):.2f}" if m.get("beta_5y") is not None else "-"),
        ("max_drawdown", fmt_pct(m.get("max_drawdown")) + "%"),
        ("recovery_months", str(m.get("recovery_months"))),
        ("recovered", str(m.get("recovered"))),
        ("pct_pos_years", fmt_pct(m.get("pct_pos_years")) + "%"),
        ("pct_pos_months", fmt_pct(m.get("pct_pos_months")) + "%"),
        ("above_200dma_frac", fmt_pct(m.get("above_200dma_frac")) + "%"),
        (
            "jump_days_per_yr",
            f"{m.get('jump_days_per_yr'):.1f}" if m.get("jump_days_per_yr") is not None else "-",
        ),
        ("div_streak_yrs", str(m.get("div_streak_yrs"))),
        ("survived_recession", str(m.get("survived_recession"))),
        ("fcf_yield", fmt_pct(m.get("fcf_yield")) + "%"),
        ("roe", fmt_pct(m.get("roe")) + "%"),
        ("payout_ratio", fmt_pct(m.get("payout_ratio")) + "%"),
        (
            "debt_to_equity",
            f"{m.get('debt_to_equity'):.2f}" if m.get("debt_to_equity") is not None else "-",
        ),
        ("market_cap", f"${m.get('market_cap') / 1e9:.2f}B" if m.get("market_cap") else "-"),
        ("div_yield", fmt_pct(m.get("div_yield")) + "%"),
        ("price_rows", str(m.get("price_rows"))),
        ("last_close", f"${m.get('last_close'):.2f}" if m.get("last_close") else "-"),
    ]
    for k, v in metrics:
        print(f"  {k:22s} {v}")

    print("\n--- Pillars ---")
    print(f"  A Dividend reliability : {m.get('score_a')}")
    print(f"  B Trust/recovery       : {m.get('score_b')}")
    print(f"  C Grind quality        : {m.get('score_c')}")
    print(f"  D Low-vol/beta         : {m.get('score_d')}")
    print(f"  E VRP/liquidity        : {m.get('score_e')}")
    print(f"  TOTAL Fortress Score   : {m.get('fortress_score')}  ({m.get('verdict')})")
    print(f"  cinf_distance          : {m.get('cinf_distance')}")

    print("\n--- Gates ---")
    for name, ok, reason in check_gates(m):
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name} — {reason}")
    print()


def print_table(rows: Sequence[Dict[str, Any]], title: str, sort_key: str, top: int) -> None:
    if sort_key == "cinf_distance":
        sorted_rows = sorted(
            rows,
            key=lambda r: r["cinf_distance"] if r.get("cinf_distance") is not None else 999.0,
        )
    else:
        sorted_rows = sorted(rows, key=lambda r: r[sort_key], reverse=True)
    display = sorted_rows[:top]

    print(f"\n{title}")
    hdr = (
        f"{'rank':>4} {'ticker':<6} {'sector':<8} {'score':>5} {'dist':>6} "
        f"{'A':>4} {'B':>4} {'C':>4} {'D':>4} {'E':>4} "
        f"{'streak':>6} {'vol%':>6} {'beta':>5} {'cagr%':>6} {'maxDD%':>7} {'verdict':<7}"
    )
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(display, 1):
        sector = r.get("sector") or "-"
        print(
            f"{i:4d} {r['ticker']:<6} {sector:<8} {r['fortress_score']:5.1f} "
            f"{(r['cinf_distance'] if r.get('cinf_distance') is not None else 0):6.3f} "
            f"{r['score_a']:4.1f} {r['score_b']:4.1f} {r['score_c']:4.1f} "
            f"{r['score_d']:4.1f} {r['score_e']:4.1f} "
            f"{r.get('div_streak_yrs') or 0:6d} "
            f"{(r.get('vol_252') or 0) * 100:6.1f} "
            f"{(r.get('beta_5y') if r.get('beta_5y') is not None else 0):5.2f} "
            f"{(r.get('price_cagr') or 0) * 100:6.1f} "
            f"{(r.get('max_drawdown') or 0) * 100:7.1f} "
            f"{r.get('verdict', ''):<7}"
        )


def write_csv(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["fortress_score"], reverse=True):
            w.writerow(r)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fortress Compounder Screener (CINF archetype)")
    p.add_argument("--ticker", help="Single-name diagnostic scorecard")
    p.add_argument("--anchor", default="CINF", help="Anchor ticker for similarity distance")
    p.add_argument("--top", type=int, default=40, help="Top N to display")
    p.add_argument("--min-score", type=float, default=0.0, dest="min_score")
    p.add_argument("--out", help="Write full ranked CSV")
    p.add_argument("--fmp-dir", default=None, help="Override FMP database directory")
    p.add_argument("--max-tickers", type=int, default=None, dest="max_tickers")
    p.add_argument("--fast", action="store_true", help="Use numpy/pandas if available")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    fmp_dir = args.fmp_dir or os.environ.get("FMP_DIR", FMP_DIR)

    if args.fast:
        try:
            import numpy as np  # noqa: F401
            import pandas as pd  # noqa: F401
        except ImportError:
            print(
                "Warning: --fast requested but numpy/pandas unavailable; using stdlib.",
                file=sys.stderr,
            )

    store = FMPStore(fmp_dir)
    try:
        divs = store.dividend_rows()
        spy = store.spy_by_date()
        funds = store.fundamentals()
    except (OSError, sqlite3.Error) as exc:
        print(f"ERROR: FMP data unavailable at {fmp_dir}: {exc}", file=sys.stderr)
        return 2
    if not spy:
        print("ERROR: SPY price history missing or empty.", file=sys.stderr)
        return 2

    if args.ticker:
        ticker = args.ticker.upper()
        try:
            fund = funds.get(ticker, {})
            div_rows = divs.get(ticker, [])
            m = evaluate_ticker(ticker, store, div_rows, spy, fund)
        except (ValueError, TypeError, sqlite3.Error, ZeroDivisionError) as exc:
            print(f"ERROR: {ticker}: {exc}", file=sys.stderr)
            return 2
        # Distance vs self-anchor for diagnostic
        assign_distances([m], args.anchor.upper())
        print_scorecard(m)
        return 0

    results = run_screen(
        store, anchor=args.anchor.upper(), max_tickers=args.max_tickers, min_score=args.min_score
    )
    print(f"\nFortress screen: {len(results)} names passed all gates (FMP dir: {fmp_dir})")
    print_table(results, f"Top {args.top} by Fortress Score", "fortress_score", args.top)
    print_table(results, "Top 25 by cinf_distance (nearest neighbors)", "cinf_distance", 25)

    if args.out:
        write_csv(args.out, results)
        print(f"\nWrote {len(results)} rows to {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
