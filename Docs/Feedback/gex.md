Now I have the full picture. Here is the definitive, code-verified GEX issue list.

***

# GEX (Gamma Exposure) — Final Issue List

After reading your actual [`gex.py`](https://github.com/mtsl001/opt_v5/blob/main/optdash/analytics/gex.py) and [`config.py`](https://github.com/mtsl001/opt_v5/blob/main/optdash/config.py) line by line and cross-checking both the external feedback and my earlier analysis, here is the complete, de-duplicated, code-grounded list of issues.

***

## ✅ What Is Already Correct (No Action Needed)

Before the issues, it is important to confirm what your code already handles correctly so you do not second-guess it:

- **GEX formula** `γ × OI × lot_size × spot² × 0.01 × direction` is mathematically correct for ₹-denominated gamma exposure per 1% spot move 
- **`pct_of_peak` denominator zero-guard** is correctly fixed using `!= 0` guard — the detailed comment in `_get_gex_peak()` and `get_net_gex()` confirms this was a known bug (GEX-2) and it's now properly resolved 
- **Scaling `/ 1e9` to display in ₹ Billions** is correct and consistent throughout both `get_net_gex` and `get_gex_series` 
- **`gex_near_B` / `gex_far_B` split** is already computed — TIER1+TIER2 vs TIER3 separation exists in the SQL queries 
- **Regime classification logic** in `_classify_regime()` is correct — three states, clean logic, no off-by-one errors 

***

## 🔴 Issue GEX-1 — Static Direction Assumption (CE=+1, PE=−1) Is Wrong for Indian Markets

**Severity: HIGH | Type: Logic Flaw**

### What your code does
The `direction` multiplier is baked into the Parquet during processor enrichment as a static sign: CE always gets `+1`, PE always gets `−1`. Every GEX sum in `gex.py` blindly trusts this .

### Why it is wrong
This is the SqueezeMetrics convention built for US markets where **market makers are the primary option writers**. In NSE, a large fraction of OTM CE and PE volume is **retail buying** — meaning dealers are frequently **short gamma on both sides**, not long. When retail floods into OTM calls (common on strong gap-up mornings), dealers are actually short those calls, making CE GEX negative — the exact opposite of your `+1` assumption. Your regime classification then labels a `NEGATIVE_TREND` environment as `POSITIVE_CHOP`. [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md)

### The Fix
This is a processor-level change, not just an analytics change. You need to incorporate the **taker-side signal** from the OBI already computed in `coc.py`:

```python
# In processor.py, replace static direction:
# OLD: direction = +1 if option_type == 'CE' else -1

# NEW: use bid/ask imbalance as a proxy for dealer positioning
# If ask_qty dominates → buyers hitting offer → dealers SHORT → direction = -1 for CE
# If bid_qty dominates → sellers hitting bid → dealers LONG → direction = +1 for CE
net_flow = row['bid_qty'] - row['ask_qty']
if option_type == 'CE':
    direction = +1 if net_flow > 0 else -1   # net buy flow → dealers short
else:  # PE
    direction = -1 if net_flow > 0 else +1
```

**Important caveat:** `total_buy_qty` and `total_sell_qty` from Upstox are **cumulative day totals**, not tick-level taker flow. This makes them a coarse proxy. The implementation should therefore use a **blended weight** rather than a binary flip:

```python
# Blended approach — safer for cumulative qty data
imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty + 1e-9)  # range: -1 to +1
if option_type == 'CE':
    direction = imbalance          # positive = more buying = dealers short = negative GEX
else:
    direction = -imbalance
```

This is a **Parquet-breaking change** — the `gex` column definition changes. You will need to re-run backfill from `BACKFILL_START_DATE = 2026-02-17` after updating `processor.py` .

***

## 🔴 Issue GEX-2 — Zero Gamma Level (ZGL) Is Not Computed

**Severity: HIGH | Type: Missing Feature**

### What your code does
`gex.py` computes total net GEX and tracks `pct_of_peak`, but there is no function that finds the **specific spot price level at which cumulative GEX crosses zero** .

### Why it matters
The Zero Gamma Level is the single most actionable output of GEX analysis. When spot is **above ZGL**, dealers are net long gamma (they buy dips, sell rips → market dampens). When spot **crosses below ZGL**, dealers flip to net short gamma (they sell into weakness → volatility expands, moves accelerate). It is a dynamic support/resistance level that **directly feeds the Environment Gate** — a spot below ZGL should increase the gate score for trending setups. [strike-watch](https://www.strike-watch.com/lab/gamma-exposure-gex-dealer-hedging-shapes-price-action.php)

Currently your gate has no awareness of this level. You have `GEX regime = NEGATIVE_TREND` as a proxy, but that only fires when *total* GEX is negative, not when spot is near the flip point while total GEX is still positive.

### The Fix
Add a `get_zero_gamma_level()` function to `gex.py`:

```python
def get_zero_gamma_level(
    conn: duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time: str,
    underlying: str,
) -> dict:
    """
    Zero Gamma Level: the spot price where cumulative GEX flips sign.
    Computed by scanning per-strike GEX and finding the interpolated
    crossing point from the strike-level cumulative sum.
    """
    rows = conn.execute("""
        SELECT strike_price, SUM(gex) AS strike_gex
        FROM options_data
        WHERE trade_date = ? AND snap_time = ? AND underlying = ?
          AND expiry_tier IN ('TIER1', 'TIER2')
        GROUP BY strike_price
        ORDER BY strike_price
    """, [trade_date, snap_time, underlying]).fetchall()

    if not rows:
        return {"zgl": None, "spot_vs_zgl": None}

    strikes = np.array([r[0] for r in rows], dtype=float)
    gex_arr = np.array([r [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md) or 0 for r in rows], dtype=float)
    cum_gex = np.cumsum(gex_arr)  # cumulative from lowest to highest strike

    # Find where cumulative GEX crosses zero
    sign_changes = np.where(np.diff(np.sign(cum_gex)))[0]
    if len(sign_changes) == 0:
        return {"zgl": None, "spot_vs_zgl": None}  # no flip in current chain

    # Linear interpolation between the two strikes bracketing the zero crossing
    idx = sign_changes[0]
    zgl = float(np.interp(0, [cum_gex[idx], cum_gex[idx + 1]],
                              [strikes[idx], strikes[idx + 1]]))

    # Get spot for distance calculation
    spot_row = conn.execute(
        "SELECT MAX(spot) FROM options_data WHERE trade_date=? AND snap_time=? AND underlying=?",
        [trade_date, snap_time, underlying]
    ).fetchone()
    spot = spot_row[0] if spot_row else None
    dist_pct = round((spot - zgl) / zgl * 100, 2) if (spot and zgl) else None

    return {
        "zgl": round(zgl, 1),
        "spot_vs_zgl": dist_pct,   # positive = spot above ZGL (stable), negative = below (volatile)
    }
```

Then expose `zgl` and `spot_vs_zgl` from the `/api/gex` endpoint, and add a **new Gate condition** using ZGL proximity .

***

## 🟡 Issue GEX-3 — `gex_near_B` Used as Primary Signal but Not Exposed to Gate

**Severity: MEDIUM | Type: Underutilisation**

### What your code does
`get_net_gex()` computes `gex_near_B` (TIER1+TIER2 only) but the `regime` and `pct_of_peak` are calculated from `gex_all` which includes TIER3 (far expiry) . The Environment Gate (`environment.py`) therefore reads regime from the all-inclusive total.

### Why it matters
TIER3 (monthly/quarterly expiries) gamma is far-dated, structurally weaker, and changes slowly. It dilutes the intraday regime signal. On days when TIER3 has large open interest (near monthly expiry), `gex_all` can stay positive even when TIER1 GEX has gone negative — the gate reads `POSITIVE_CHOP` but the intraday reality is trending.

### The Fix
Add a `regime_near` field to `get_net_gex()` output computed from `gex_near_B` and its own peak (tracked separately). Use `regime_near` as the **primary input to Gate C1** and keep `regime` (all-inclusive) as context only. This requires storing `peak_near` in the cache alongside `peak` .

***

## 🟡 Issue GEX-4 — `_get_gex_peak()` Is a Full-Day Table Scan Every Call Without Cache

**Severity: MEDIUM | Type: Performance**

### What your code does
`_get_gex_peak()` runs a `MAX(ABS(gex_sum))` subquery across **all snaps of the day** every time it's called. The `_peak_cache` dict is passed by the caller (scheduler) but only covers **a single scheduler tick** — it is not persisted across ticks .

### Why it matters
At 5-min intervals with 5 underlyings, this is 5 full table scans per tick just for peak GEX. As the day accumulates snaps (up to ~75 snaps by 3:30 PM), each scan grows. By afternoon the peak from 9:30 AM is repeatedly recomputed. The correct peak is monotonically non-decreasing intraday — once found, it should be cached until day end.

### The Fix
Persist the peak cache in the scheduler's tick state (or in a module-level dict keyed by `(trade_date, underlying)`) that survives across ticks. Invalidate only on `trade_date` rollover .

***

## 🟡 Issue GEX-5 — `get_gex_series()` Recomputes Peak from the Series Itself (In-Memory), Not `_get_gex_peak()`

**Severity: MEDIUM | Type: Inconsistency**

### What your code does
`get_gex_series()` uses `peak = max(abs(r [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md) or 0) for r in rows)` — the maximum from the current series result set. `get_net_gex()` calls `_get_gex_peak()` which queries DuckDB separately .

### Why it matters
If `get_gex_series()` is called mid-day (e.g., at 11:00 AM), its `peak` is the max-so-far from the 9:15–11:00 series. But `get_net_gex()` at the same moment uses a DuckDB query peak also from the same window. These two should be identical — but they diverge if: the series endpoint filters by `trade_date` only (all snaps) while the live endpoint filters by `snap_time` as well. **Verify the frontend is not showing inconsistent `pct_of_peak` between the chart and the live tile.**

### The Fix
Both paths should use the same peak source. Either route `get_gex_series()` to call `_get_gex_peak()` for consistency, or document explicitly that the series peak is a "rolling visual peak" and the live tile peak is the "session peak" so the difference is intentional and labelled on the UI .

***

## Summary Table

| # | Issue | Severity | Fix Location |
|---|-------|----------|-------------|
| GEX-1 | Static CE=+1/PE=−1 direction is wrong for Indian market structure | 🔴 HIGH | `processor.py` (Parquet rebuild required) |
| GEX-2 | Zero Gamma Level (ZGL) not computed or exposed | 🔴 HIGH | New function in `gex.py`, new Gate condition |
| GEX-3 | Regime computed from `gex_all` (includes TIER3) instead of `gex_near` | 🟡 MEDIUM | `gex.py` + `environment.py` |
| GEX-4 | `_get_gex_peak()` re-scans entire day on every tick, no cross-tick cache | 🟡 MEDIUM | Scheduler tick state or module-level cache |
| GEX-5 | `gex_series` and `get_net_gex` compute `pct_of_peak` from different peak sources | 🟡 MEDIUM | `gex.py` — unify peak source |

**Recommended fix order:** GEX-2 (ZGL, standalone new function, no breakage) → GEX-3 (regime_near, small change) → GEX-4 (cache, pure performance) → GEX-5 (consistency, low risk) → GEX-1 last (requires processor change + full backfill rebuild).



--------------------------------


***

# OptDash Analytics — Deep Research & Validation


***

## GEX (Gamma Exposure) — §3

### Formula Correctness

Your formula is **correct and matches industry standard**  [perfiliev](https://perfiliev.com/blog/how-to-calculate-gamma-exposure-and-zero-gamma-level/):

$$\text{GEX} = \gamma \times OI \times \text{lot\_size} \times \text{spot}^2 \times 0.01 \times \text{direction}$$

The `spot² × 0.01` scaling converts raw gamma into "dollar delta change per 1% move in the underlying" — identical to what Perfiliev and SpotGamma use  [perfiliev](https://perfiliev.com/blog/how-to-calculate-gamma-exposure-and-zero-gamma-level/). CE = +1, PE = −1 is consistent with the **dealer-perspective convention** where sold calls = positive GEX (stabilizing hedging) and sold puts = negative GEX (destabilizing hedging)  [strike-watch](https://www.strike-watch.com/lab/gamma-exposure-gex-dealer-hedging-shapes-price-action.php).

### ⚠️ Issues Found

- **pct_of_peak denominator risk:** The formula is `|current_gex| / day_peak_gex × 100`. If the day starts with negative GEX and peaks there, `day_peak_gex` is negative. The code must use `|day_peak_gex|` in the denominator — verify this in `gex.py`.
- **Gamma units from Upstox:** The `γ` from the BQ feed (BSM gamma) must be in **per-rupee** units (not per-percent). If Upstox provides gamma per 1% move, you must multiply by `spot/100` instead of `spot²×0.01`. This is a silent data contract issue worth explicitly documenting.
- **Multi-expiry aggregation:** Current GEX aggregates all tiers. TIER2/TIER3 gamma is far-dated and structurally weaker — blending it with TIER1 dilutes the intraday signal.

### 🔧 Improvements

- Track the **zero-GEX level** (strike where net GEX flips from positive to negative) — this is the strongest support/resistance level derived from GEX, widely used by SpotGamma and similar tools  [strike-watch](https://www.strike-watch.com/lab/gamma-exposure-gex-dealer-hedging-shapes-price-action.php).
- Add **TIER1-only GEX** as a separate signal to avoid far-expiry dilution.
- Store `pct_of_peak` as a raw column in Parquet so it can be trended over time.

***
