# OptDash — Part 3: Analytics Engine

All analytics modules live in `optdash/analytics/`. They are **pure functions** — no state, no side effects — taking a `DuckDB connection` (LockedConn) plus time/underlying coordinates and returning dicts or lists. Every analytics function has a top-level `try/except` that calls `record_error(fn_name)` and returns a safe empty result on failure.

---

## 1. GEX — Gamma Exposure (`analytics/gex.py`)

### 1.1 What is GEX?

Gamma Exposure measures the aggregate dollar gamma that market makers hold. Dealers short options must hedge by buying into rallies and selling into dips — this creates the gamma pin effect.

- **Positive GEX**: Dealers net long gamma → suppress volatility (range-bound, chop)
- **Negative GEX**: Dealers net short gamma → amplify moves (trending)

### 1.2 `get_net_gex(conn, trade_date, snap_time, underlying, _peak_cache=None) → dict`

```sql
SELECT SUM(gex)                                                              AS gex_all_raw,
       SUM(CASE WHEN expiry_tier IN ('TIER1','TIER2') THEN gex ELSE 0 END)  AS gex_near_raw,
       SUM(CASE WHEN expiry_tier = 'TIER3'            THEN gex ELSE 0 END)  AS gex_far_raw,
       MAX(spot) AS spot
FROM options_data
WHERE trade_date=? AND snap_time=? AND underlying=?
GROUP BY snap_time
```

GEX values are divided by `settings.GEX_SCALING` (default 1B) to produce readable `_B` (billion) figures.

`_peak_cache` is an optional dict shared within a tick to avoid redundant full-day peak scans across multiple callers.

**Returns:**
```json
{
  "snap_time": "10:30",
  "gex_all_B": 4.52,
  "gex_near_B": 3.21,
  "gex_far_B": 1.31,
  "pct_of_peak": 78.3,
  "regime": "POSITIVE_CHOP",
  "spot": 22150.5
}
```

### 1.3 GEX Regime Classification (`_classify_regime`)

| Condition | Regime | Meaning |
|---|---|---|
| `gex < 0` | `NEGATIVE_TREND` | Dealers net short gamma → trending |
| `gex >= 0` and `pct_of_peak <= GEX_DECLINE_THRESHOLD×100` | `POSITIVE_DECLINING` | Gamma wall weakening |
| `gex > 0` and above threshold | `POSITIVE_CHOP` | Strong gamma pin → mean-reversion |

`pct_of_peak = abs(gex_all) / day_peak_gex * 100`. Guard: `if peak != 0` prevents division by zero (not `if peak` which would treat a genuine zero-GEX day incorrectly).

### 1.4 `get_max_pain(conn, trade_date, snap_time, underlying, expiry_date) → dict`

Max pain is computed via **vectorised NumPy outer-subtraction** — no Python loop:

```python
diff        = strikes[:, None] - strikes[None, :]   # NxN matrix
ce_pain_mat = np.maximum(0.0,  diff) * ce_oi       # CE payout at each settlement
pe_pain_mat = np.maximum(0.0, -diff) * pe_oi       # PE payout at each settlement
pain_arr    = ce_pain_mat.sum(axis=1) + pe_pain_mat.sum(axis=1)
max_pain    = strikes[np.argmin(pain_arr)]
```

Returns `{"max_pain": float, "distance_pct": float, "spot": float}`. `max_pain` may be `None` if no options data.

### 1.5 `get_spot_summary(conn, trade_date, underlying) → dict`

```sql
SELECT MAX(snap_time), arg_max(spot, snap_time) AS spot,
       arg_min(spot, snap_time) AS day_open,
       MAX(spot) AS day_high, MIN(spot) AS day_low
FROM options_data WHERE trade_date=? AND underlying=?
```

`arg_min(spot, snap_time)` returns the spot at the earliest snap — correct day open.

---

## 2. Cost-of-Carry (`analytics/coc.py`)

### 2.1 `get_coc_latest(conn, trade_date, snap_time, underlying) → dict`

```sql
SELECT AVG(fut_price) AS fut_price, AVG(spot) AS spot,
       AVG(fut_price) - AVG(spot) AS coc
FROM options_data
WHERE trade_date=? AND snap_time=? AND underlying=? AND instrument_type='FUT'
```

### 2.2 V_CoC — 15-Minute Velocity (`_compute_vcoc`)

Computes CoC diff over a **true 15-minute wall-clock window** (not a fixed row count):

```python
h, m = map(int, snap_time.split(":"))
cutoff = f"{(h*60+m-15)//60:02d}:{(h*60+m-15)%60:02d}"
# Query: snap_time >= cutoff AND snap_time <= snap_time, ORDER BY snap_time DESC
v_coc = coc_rows[0][1] - coc_rows[-1][1]  # latest minus earliest in window
```

This handles feed gaps (e.g. missed snap) correctly — the window is always 15 real minutes, not 3 rows.

For the full-day series, `_compute_vcoc_from_series(rows, i)` uses a row-index offset derived from `settings.SCHEDULER_INTERVAL_SECONDS`:

```python
interval = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)  # minutes per snap
lookback = max(1, 15 // interval)                             # rows for 15-min window
```

| V_CoC | Signal |
|---|---|
| `> VCOC_BULL_THRESHOLD` (default 10.0) | `VELOCITY_BULL` |
| `< VCOC_BEAR_THRESHOLD` (default –10.0) | `VELOCITY_BEAR` |
| Between | `STABLE` |

### 2.3 ATM OBI (`get_atm_obi`) — Two-CTE Approach

Uses two CTEs to find the exactly-ATM strike on a per-snap basis, avoiding the `LIMIT 4` skew:

```sql
WITH spot_cte AS (SELECT AVG(spot) FROM options_data WHERE ...),
     min_dist AS (SELECT MIN(ABS(strike_price - spot)) FROM options_data WHERE ... AND expiry_tier='TIER1')
SELECT
    SUM(CASE WHEN option_type='CE' THEN (bid_qty - ask_qty) ELSE 0 END) AS ce_flow,
    SUM(CASE WHEN option_type='PE' THEN (bid_qty - ask_qty) ELSE 0 END) AS pe_flow,
    SUM(bid_qty + ask_qty) AS total_qty
FROM options_data WHERE ... AND ABS(strike_price - spot) = min_dist
```

`OBI = (ce_flow - pe_flow) / total_qty` — range `[–1, +1]`.

### 2.4 Futures OBI (`get_futures_obi`)

```sql
SELECT SUM(bid_qty - ask_qty) AS net_flow, SUM(bid_qty + ask_qty) AS total_qty
FROM options_data WHERE ... AND instrument_type='FUT'
```

`FUT_OBI = net_flow / total_qty`.

---

## 3. PCR Analytics (`analytics/pcr.py`)

### 3.1 `get_pcr(conn, trade_date, snap_time, underlying) → dict`

```sql
SELECT
    SUM(CASE WHEN option_type='PE' THEN volume ELSE 0 END) /
    NULLIF(SUM(CASE WHEN option_type='CE' THEN volume ELSE 0 END), 0) AS pcr_vol,
    SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) /
    NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END), 0)     AS pcr_oi
FROM options_data WHERE ... AND expiry_tier='TIER1'
```

`pcr_divergence = pcr_vol - pcr_oi`

| Divergence | Signal |
|---|---|
| `> PCR_DIV_BULL_THRESHOLD` | `RETAIL_PANIC_PUTS` |
| `< PCR_DIV_BEAR_THRESHOLD` | `RETAIL_PANIC_CALLS` |
| `abs > 0.10` | `DIVERGENCE_BUILDING` |
| Within | `BALANCED` |

### 3.2 Smoothed OBI (full-day series)

The series uses a SQL window function — single query, no N+1:

```sql
AVG(obi) OVER (ORDER BY snap_time ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS smoothed_obi
```

For the single-snap path, `_smoothed_obi()` fetches the last 3 snaps at or before `snap_time` and averages their OBI.

---

## 4. IV Analytics (`analytics/iv.py`)

### 4.1 `get_ivr_ivp(conn, trade_date, snap_time, underlying) → dict`

**ATM IV** — uses a subquery to find the exact ATM strike (min distance from spot):

```sql
WITH spot_cte AS (SELECT AVG(spot) FROM options_data WHERE ...)
SELECT AVG(o.iv) FROM options_data o, spot_cte s
WHERE ... AND ABS(o.strike_price - s.spot) = (
    SELECT MIN(ABS(strike_price - s.spot)) FROM options_data WHERE ...
)
```

**IVR** (IV Rank):
```
IVR = (atm_iv - iv_low) / (iv_high - iv_low) × 100
```
Where `iv_low`/`iv_high` are from the last `IV_LOOKBACK_DAYS` (default 252) trading days.

**IVP** (IV Percentile):
```
IVP = count(daily_avg_iv < atm_iv) / total_days × 100
```
**Guard**: Returns `None` when fewer than 20 days of history exist. Gate C5 then defaults to `ivp_val = 100.0` (conservative "not cheap" posture).

**HV20** (20-day Historical Volatility):
```sql
-- Triple-nested to ensure LAG() runs over full history BEFORE the LIMIT 22 cut
SELECT STDDEV(daily_ret) * SQRT(252) * 100 AS hv20 FROM (
    SELECT daily_ret FROM (
        SELECT LN(MAX(spot) / LAG(MAX(spot)) OVER (ORDER BY trade_date)) AS daily_ret
        FROM options_data WHERE underlying=? AND trade_date<=?
        GROUP BY trade_date ORDER BY trade_date DESC
    ) all_rets LIMIT 22
)
```

### 4.2 Term Structure (`get_term_structure`) and Shape (`_classify_shape`)

```python
def _classify_shape(near_iv, far_iv) -> str:
    if near_iv is None or far_iv is None or near_iv == 0:
        return "FLAT"          # explicit None/zero guard — never `not near_iv`
    ratio = far_iv / near_iv
    if ratio > 1.05:   return "CONTANGO"
    if ratio < 0.95:   return "BACKWARDATION"
    return "FLAT"
```

> `near_iv == 0` guard prevents `ZeroDivisionError`. `near_iv is None or near_iv == 0` — never `not near_iv` which would misclassify a genuine zero IV.

### 4.3 Volatility Risk Premium (VRP) & Skew

**Volatility Risk Premium (VRP):**
```
VRP = atm_iv - hv20
```
Categorized into rigorous regimes: `OVERPRICED` (>2.0), `UNDERPRICED` (<0.0), or `FAIR`.

**25-Delta Skew:**
`get_iv_skew()` queries the nearest options to `0.25` absolute delta.
```
Skew = Put_25d_IV - Call_25d_IV
```
It computes tracking metrics `skew_direction` (`STEEPENING` / `FLATTENING`) based on trailing snaps, and signals an `ELEVATED` regime if it exceeds `settings.SKEW_ELEVATED_THRESHOLD`.

---

## 5. VEX/CEX Analytics (`analytics/vex_cex.py`)

### 5.1 `get_vex_cex_current(conn, trade_date, snap_time, underlying) → dict`

```sql
SELECT SUM(vex)/1e6 AS vex_total_M,
       SUM(cex)/1e6 AS cex_total_M,
       AVG(spot), MIN(dte)
FROM options_data WHERE ... AND expiry_tier='TIER1'
```

### 5.2 VEX Classification (`_classify_vex`) — Per-Underlying Thresholds

```python
threshold = settings.VEX_THRESHOLDS.get(underlying, settings.VEX_BULL_THRESHOLD)
if vex_total > threshold:   return "VEX_BULLISH"
if vex_total < -threshold:  return "VEX_BEARISH"
return "NEUTRAL"
```

Per-underlying thresholds are required because MIDCPNIFTY and NIFTYNXT50 have ~10× lower VEX magnitudes than BANKNIFTY.

### 5.3 CEX Classification (`_classify_cex`) — Two-Level Thresholds

```python
strong_thr = settings.CEX_CHARM_THRESHOLD.get(underlying, settings.CEX_STRONG_BID)
bid_thr    = settings.CEX_VANNA_THRESHOLD.get(underlying, settings.CEX_BID)
```

The CEX interpretation is contextually bounded by the **Net GEX** regime. If dealers are *net short* gamma (Negative GEX), their charm delta hedging reverses direction:

| CEX (Normal/Positive GEX) | Signal | Meaning |
|---|---|---|
| `>= strong_thr` | `STRONG_CHARM_BID` | Dealers must rapidly buy delta as options decay |
| `<= -strong_thr` | `CHARM_PRESSURE` | Dealers must rapidly sell delta |

*(Note: If Net GEX < 0, a mathematically positive CEX induces `CHARM_PRESSURE`, explicitly inverting the hedge direction).*

### 5.4 Dealer O'Clock (`_is_dealer_oclock`)

```python
def _is_dealer_oclock(snap_time, dte, underlying, trade_date) -> bool:
    if dte > settings.DEALER_OCLOCK_DTE:   return False
    if snap_time < settings.DEALER_OCLOCK_START:  return False
    expected_weekday = settings.EXPIRY_WEEKDAY.get(underlying, 3)  # 3=Thursday
    return date.fromisoformat(trade_date).weekday() == expected_weekday
```

Uses `trade_date` (not wall-clock) so historical replay and backtests work correctly.

---

## 6. Strike Screener (`analytics/screener.py`)

### 6.1 S_Score — 7-Factor Composite (maximum 150)

```sql
(
    W_DELTA    × ABS(o.delta)
  + W_THETA    × (1 - LEAST(1, ABS(o.theta)/NULLIF(o.ltp,0)/0.05))   -- theta efficiency cap 5%
  + W_LIQUIDITY × LEAST(1, o.oi*o.ltp/1e7/5.0)                       -- cap 5 Cr
  + W_IV       × (1 - LEAST(1, o.iv/100.0))                          -- lower IV preferred
  + W_GAMMA    × LEAST(1, ABS(o.gamma)*100)                           -- cap 0.01
  + W_VEGA     × LEAST(1, ABS(o.vega)/50.0)                          -- cap 50
  + W_EFF_RATIO× (1 - LEAST(1, ABS(o.theta)/NULLIF(o.ltp,0)/0.10))   -- eff ratio cap 10%
) × 10 AS s_score
```

Default weights (`config.py`): W_DELTA=4, W_THETA=2, W_LIQUIDITY=3, W_IV=2, W_GAMMA=1, W_VEGA=1, W_EFF_RATIO=4.

Theoretical max = (4+2+3+2+1+1+4)×10 = 170, but delta capped at 0.50 by the filter → typical max ~150.

### 6.2 Filters Applied Before Ranking

```sql
AND ABS((strike_price - spot) / spot * 100) <= SCREENER_MAX_MONEYNESS_PCT   -- default 5%
AND ABS(o.delta) BETWEEN SCREENER_MIN_DELTA AND SCREENER_MAX_DELTA           -- default 0.10–0.50
AND o.oi * o.ltp / 1e7 >= SCREENER_MIN_LIQUIDITY_CR                         -- default 0.5 Cr
AND o.ltp > 0
```

### 6.3 Star Ratings

| Stars | S_score Threshold |
|---|---|
| ⭐⭐⭐⭐ | ≥ STAR_4_THRESHOLD (default 100) |
| ⭐⭐⭐ | ≥ STAR_3_THRESHOLD (default 80) |
| ⭐⭐ | ≥ STAR_2_THRESHOLD (default 60) |
| ⭐ | < 60 |

---

## 7. Environment Gate (`analytics/environment.py`)

Documented fully in **Part 7: Environment Gate**.

---

## 8. PnL Analytics (`analytics/pnl.py`)

### 8.1 Theta-SL Series (`compute_theta_sl`)

Computes the minimum acceptable LTP at each snap to compensate for theta decay:

```python
def compute_theta_sl(entry_premium, theta, snaps_since_entry):
    cumulative_theta_loss = abs(theta) * (snaps_since_entry * interval_hours)
    return max(0.0, entry_premium - cumulative_theta_loss) * (1 - AI_SL_PCT)
```

### 8.2 Greek PnL Attribution (`compute_pnl_attribution`)

Decomposes total PnL into Greeks and an unexplained residual:

```python
delta_pnl   = delta * (spot_now - spot_entry) * lot
gamma_pnl   = 0.5 * gamma * (spot_now - spot_entry)**2 * lot
vega_pnl    = vega * (iv_now - iv_entry) * lot
theta_pnl   = theta * elapsed_days * lot
unexplained = total_pnl - (delta_pnl + gamma_pnl + vega_pnl + theta_pnl)
```

---

## 9. Analytics Query Count Per Gate Evaluation

All calls made by `get_environment_score()` per tick per underlying:

```
get_environment_score()
    ├─ get_net_gex()           — 2 queries (current snap + peak scan)
    ├─ get_coc_latest()        — 1 query + _compute_vcoc (1 query)
    ├─ get_ivr_ivp()           — 2 queries (current ATM IV + history CTE)
    ├─ get_pcr()               — 1 query + _smoothed_obi (1 query)
    ├─ get_vex_cex_current()   — 1 query
    ├─ get_atm_obi()           — 1 query (3-CTE)
    └─ get_futures_obi()       — 1 query
                                  ─────────────
                                  ~11 queries per gate evaluation
```

`_build_gate_cache()` in the scheduler runs this once per unique underlying per tick, so `track_open_positions()` reuses the cached result at zero query cost.
