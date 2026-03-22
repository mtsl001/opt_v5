# OptDash — VEX/CEX & IV Analytics Fix Plan: Part B

**Target modules:** `ai/confidence.py` · `analytics/screener.py` · `analytics/pcr.py` · `analytics/vex_cex.py` · `analytics/iv.py` · `config.py`  
**Release:** v2.6.0 → v2.6.1  
**Branch to create:** `fix/analytics-part-b`  

> **For the junior developer:**  
> Part B covers the remaining open issues from the consolidated review.  
> Every section gives you the exact current code (from the live repo), the precise  
> replacement, and the commit message. Follow the Commit Order table at the end.  
> Do NOT touch any file not listed in an issue.

---

## Pre-Flight Checklist

# Create Part B branch
git checkout -b fix/analytics-part-b

# Sanity check — these should all exist after Part A
python -c "from optdash.config import settings; print(settings.RISK_FREE_RATE)"
python -c "from optdash.config import settings; print(settings.VRP_OVERPRICED_THRESHOLD)"
```

---
""")

# ---------- ISSUE B-1: VEX-6 per-strike VEX/CEX viz ----------
sections.append("""## Issue B-1 (VEX-6) — Per-Strike VEX/CEX Data Already Exists, Missing from API Route

**Severity:** Low  
**File:** `optdash/api/routes/vex_cex_router.py` (or wherever API routes live)  
**Note:** `_get_by_strike()` in `vex_cex.py` already computes per-strike VEX/CEX and  
`get_vex_cex_full()` already returns `by_strike` in its response payload.  
The data EXISTS — it is just not exposed as a dedicated endpoint.

### Verify it is wired up

Open the FastAPI router file (search for `vex` in the `api/routes/` folder).  
Look for a route like:

```python
@router.get("/vex-cex/full")
async def vex_cex_full(...):
    return get_vex_cex_full(conn, trade_date, snap_time, underlying)
```

If this route exists and `by_strike` is in the response, **this issue is already resolved**.  
Check by calling: `GET /api/vex-cex/full?underlying=NIFTY&...` and confirming `by_strike` is present.

### Fix — Only if `by_strike` endpoint is missing

If there is no dedicated per-strike endpoint, add one to the router:

```python
@router.get("/vex-cex/by-strike")
async def vex_cex_by_strike(
    underlying: str,
    trade_date: str,
    snap_time:  str,
    conn = Depends(get_db_conn),
):
    \"\"\"
    Per-strike VEX and CEX breakdown for frontend heatmap rendering.
    Returns list of {strike_price, option_type, moneyness_pct, vex_M, cex_M, oi, iv, dte}.
    Sorted by strike_price ascending.
    \"\"\"
    from optdash.analytics.vex_cex import _get_by_strike
    return _get_by_strike(conn, trade_date, snap_time, underlying)
```

**Note:** `_get_by_strike` is already imported and returns the correct schema  
with `moneyness_pct` None-safe (the NULL NULLIF fix is already in the live code).

### Commit message

```
feat(api): expose per-strike VEX/CEX as dedicated endpoint /vex-cex/by-strike (VEX-6)

Data was already computed by _get_by_strike() in vex_cex.py and included
in the /vex-cex/full response. Added dedicated route for frontend heatmap
rendering without requiring the full payload.
```

---
""")

# ---------- ISSUE B-2: VEX-7 Skew x VEX convergence alert ----------
sections.append("""## Issue B-2 (VEX-7) — Skew × VEX Convergence Alert is Absent

**Severity:** Low  
**Files:** `optdash/analytics/iv.py`, `optdash/analytics/alerts.py`, `optdash/config.py`

### What is wrong

When the 25-Delta put IV minus 25-Delta call IV (put skew) steepens **simultaneously** with  
`VEX < -threshold` (bearish vanna pressure), it is a high-conviction downside signal.  
This composite alert does not currently exist anywhere in the codebase.

### Step 1 — Add a skew computation function to `iv.py`

At the bottom of `iv.py`, add this new function:

```python
def get_iv_skew(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> dict:
    \"\"\"
    Compute 25-delta put/call IV skew for TIER1 options.

    Skew = IV of the strike closest to 25-delta PUT minus
           IV of the strike closest to 25-delta CALL.

    Positive skew: put wing more expensive than call wing (normal for indices).
    Rising skew: fear increasing, put demand accelerating.
    This is the standard skew measure used by dealers and vol desks.

    Returns:
        skew:           float — current skew value (in IV %)
        put_25d_iv:     float — IV of nearest 25-delta put strike
        call_25d_iv:    float — IV of nearest 25-delta call strike
        skew_direction: str   — \"STEEPENING\" / \"FLATTENING\" / \"FLAT\" / \"UNKNOWN\"
        skew_regime:    str   — \"ELEVATED\" (> threshold) / \"NORMAL\" / \"UNKNOWN\"
    \"\"\"
    try:
        # Find nearest-to-0.25-delta strikes for CE and PE
        row = conn.execute(\"\"\"
            WITH t1 AS (
                SELECT option_type, strike_price, iv, delta
                FROM options_data
                WHERE trade_date=? AND snap_time=? AND underlying=?
                  AND expiry_tier='TIER1' AND iv > 0
            ),
            puts AS (
                SELECT iv, delta, strike_price
                FROM t1
                WHERE option_type='PE'
                ORDER BY ABS(ABS(delta) - 0.25)
                LIMIT 1
            ),
            calls AS (
                SELECT iv, delta, strike_price
                FROM t1
                WHERE option_type='CE'
                ORDER BY ABS(ABS(delta) - 0.25)
                LIMIT 1
            )
            SELECT p.iv AS put_iv, c.iv AS call_iv
            FROM puts p, calls c
        \"\"\", [trade_date, snap_time, underlying]).fetchone()

        if not row or row[0] is None or row[1] is None:
            return {"skew": None, "put_25d_iv": None, "call_25d_iv": None,
                    "skew_direction": "UNKNOWN", "skew_regime": "UNKNOWN"}

        put_iv  = float(row[0])
        call_iv = float(row[1])
        skew    = round(put_iv - call_iv, 2)

        # Trailing skew trend: compare to skew 3 snaps ago
        prev_row = conn.execute(\"\"\"
            WITH ordered AS (
                SELECT snap_time
                FROM options_data
                WHERE trade_date=? AND underlying=? AND snap_time < ?
                GROUP BY snap_time
                ORDER BY snap_time DESC
                LIMIT 3
            ),
            oldest AS (SELECT MIN(snap_time) AS st FROM ordered),
            puts AS (
                SELECT o.iv
                FROM options_data o, oldest old_s
                WHERE o.trade_date=? AND o.snap_time=old_s.st
                  AND o.underlying=? AND o.expiry_tier='TIER1'
                  AND o.option_type='PE' AND o.iv > 0
                ORDER BY ABS(ABS(o.delta) - 0.25) LIMIT 1
            ),
            calls AS (
                SELECT o.iv
                FROM options_data o, oldest old_s
                WHERE o.trade_date=? AND o.snap_time=old_s.st
                  AND o.underlying=? AND o.expiry_tier='TIER1'
                  AND o.option_type='CE' AND o.iv > 0
                ORDER BY ABS(ABS(o.delta) - 0.25) LIMIT 1
            )
            SELECT p.iv, c.iv FROM puts p, calls c
        \"\"\", [trade_date, underlying, snap_time,
                trade_date, underlying,
                trade_date, underlying]).fetchone()

        skew_direction = "UNKNOWN"
        if prev_row and prev_row[0] is not None and prev_row[1] is not None:
            prev_skew = float(prev_row[0]) - float(prev_row[1])
            delta_skew = skew - prev_skew
            if delta_skew > 0.3:
                skew_direction = "STEEPENING"
            elif delta_skew < -0.3:
                skew_direction = "FLATTENING"
            else:
                skew_direction = "FLAT"

        skew_threshold = settings.SKEW_ELEVATED_THRESHOLD   # add to config (see Step 2)
        skew_regime = "ELEVATED" if skew > skew_threshold else "NORMAL"

        return {
            "skew":           skew,
            "put_25d_iv":     round(put_iv, 2),
            "call_25d_iv":    round(call_iv, 2),
            "skew_direction": skew_direction,
            "skew_regime":    skew_regime,
        }
    except Exception as e:
        logger.warning("get_iv_skew error: {}", e)
        return {"skew": None, "put_25d_iv": None, "call_25d_iv": None,
                "skew_direction": "UNKNOWN", "skew_regime": "UNKNOWN"}
```

### Step 2 — Add `SKEW_ELEVATED_THRESHOLD` to `config.py`

In the `# -- IV` section:

```python
# Skew = 25D Put IV - 25D Call IV (in IV %)
# Typical NIFTY skew range: 2-6%. Above 6% = elevated fear premium.
# SKEW_ELEVATED_THRESHOLD: above this, put-skew is considered "elevated".
# Used in Skew×VEX convergence alert (B-2).
SKEW_ELEVATED_THRESHOLD: float = 5.0

# SKEW_STEEPENING_VEX_CONFIRM: when skew is STEEPENING and VEX < -threshold,
# this triggers HIGH_CONVICTION_BEAR alert.
# These two signals together confirm vanna-accelerated selling pressure.
SKEW_STEEPENING_VEX_CONFIRM: bool = True
```

### Step 3 — Add the composite alert to `alerts.py`

Open `alerts.py` and find the section that generates alert objects (look for a list/dict  
being built with alert names like `"GEX_FLIP"`, `"CHARM_ACTIVE"`, etc.).

Add a new alert generator function:

```python
def _check_skew_vex_convergence(
    skew_data: dict,
    vex_data:  dict,
    underlying: str,
) -> dict | None:
    \"\"\"
    HIGH_CONVICTION_BEAR alert fires when:
      1. Skew is STEEPENING (put-wing demand accelerating)
      AND
      2. VEX total is below -threshold (bearish vanna pressure from dealers)

    These two signals together confirm that:
      a) Market participants are buying puts aggressively (steepening skew)
      b) Dealers are forced to short the underlying as IV rises (negative VEX)
    This combination has historically preceded violent, accelerating downside moves.
    \"\"\"
    if not settings.SKEW_STEEPENING_VEX_CONFIRM:
        return None

    skew_direction = skew_data.get("skew_direction", "UNKNOWN")
    skew_regime    = skew_data.get("skew_regime", "UNKNOWN")
    vex_total      = vex_data.get("vex_total_M", 0.0)
    vex_threshold  = settings.VEX_THRESHOLDS.get(underlying, settings.VEX_BULL_THRESHOLD)

    # Both conditions must be true simultaneously
    skew_steepening = (skew_direction == "STEEPENING")
    vex_bearish     = (vex_total < -vex_threshold)

    if skew_steepening and vex_bearish:
        return {
            "alert_type":  "HIGH_CONVICTION_BEAR",
            "severity":    "HIGH",
            "message": (
                f"Skew steepening ({skew_data.get('skew', 'N/A')}%) + "
                f"VEX bearish ({round(vex_total, 2)} Rs M) — "
                f"vanna-accelerated selling pressure confirmed. "
                f"High-conviction downside setup."
            ),
            "skew":       skew_data.get("skew"),
            "vex_total_M": vex_total,
        }
    return None
```

Then in the main `get_alerts()` function in `alerts.py`, add the call:

```python
# Import at top if not present:
from optdash.analytics.iv      import get_iv_skew
from optdash.analytics.vex_cex import get_vex_cex_current

# Inside get_alerts():
skew_data = get_iv_skew(conn, trade_date, snap_time, underlying)
vex_data  = get_vex_cex_current(conn, trade_date, snap_time, underlying)

skew_vex_alert = _check_skew_vex_convergence(skew_data, vex_data, underlying)
if skew_vex_alert:
    alerts.append(skew_vex_alert)
```

### Commit message

```
feat(iv,alerts): add Skew×VEX convergence HIGH_CONVICTION_BEAR alert (VEX-7)

- New get_iv_skew() in iv.py: computes 25-delta put/call skew + 3-snap trend
- New _check_skew_vex_convergence() in alerts.py: fires HIGH_CONVICTION_BEAR
  when skew STEEPENING AND VEX < -threshold simultaneously
- Added SKEW_ELEVATED_THRESHOLD=5.0 and SKEW_STEEPENING_VEX_CONFIRM=True to config.py
```

---
""")

# ---------- ISSUE B-3: S_Score delta component ----------
sections.append("""## Issue B-3 — S_Score Delta Component is Asymmetric (CE vs PE scoring)

**Severity:** Medium  
**File:** `optdash/analytics/screener.py`

### What is wrong

Current S_Score delta term (line in `screener.py`):

```python
? * ABS(o.delta)   -- W_DELTA coefficient
```

For a **call (CE)**: delta is positive (0 to +1). `ABS(delta)` = delta. Higher delta = higher score.  
For a **put (PE)**: delta is negative (-1 to 0). `ABS(delta)` = |delta|. Same scoring — OK.

**However**, the direction filter (`AND o.option_type = ?`) already restricts results to one  
side when `direction` is passed. The issue is that delta 0.50 = ATM scores highest, and  
delta 0.10 = deep OTM scores lowest. This is **correct for buyers** — ATM has highest gamma  
and is the preferred entry for an options buying strategy.

**BUT** the screener cap `SCREENER_MAX_DELTA = 0.50` means the delta term can only reach  
`W_DELTA × 0.50 = 2.0 × 0.50 = 1.0` as its maximum contribution, capped at half the  
theoretical max (2.0). The docstring says:  
`"delta is capped at 0.50 by the SCREENER_MAX_DELTA filter"` — so this is intentional.

**The real problem:** When `direction=None` (both CE and PE returned), a PE with  
delta = -0.40 and a CE with delta = +0.40 score identically. But if the market context  
is bearish (VEX bearish, GEX negative), returning CE options with 4-star S_Score in the  
same list as PE options is misleading — the score does not encode directional alignment.

### Fix — Add a `direction_bonus` column to the S_Score output

Do NOT change the S_Score formula itself (risk of breaking existing star thresholds).  
Instead, add a `direction_alignment` field to the returned row that the frontend can use  
for visual flagging:

In `get_strikes()`, after the `return [...]` list comprehension, add a post-processing step:

```python
rows_out = [
    {k: (round(v, 4) if isinstance(v, float) else v)
     for k, v in zip(cols, r)}
    for r in rows
]

# Add direction alignment flag for frontend visual cues.
# direction_aligned = True when the option side matches the requested direction.
# When direction=None, all rows default to True (no filtering context).
if direction:
    for row_dict in rows_out:
        row_dict["direction_aligned"] = (row_dict.get("option_type") == direction)
else:
    for row_dict in rows_out:
        row_dict["direction_aligned"] = True

return rows_out
```

This gives the frontend a clean boolean to show/grey-out misaligned options  
without changing the numeric S_Score or breaking existing threshold calibrations.

### Commit message

```
feat(screener): add direction_aligned flag to S_Score output rows (B-3)

When direction='CE' or 'PE', marks each row with direction_aligned=True/False
so the frontend can visually flag options that match the trade direction.
S_Score formula and star thresholds are unchanged.
```

---
""")

# ---------- ISSUE B-4: PCR Z-score window not in config ----------
sections.append("""## Issue B-4 — PCR Z-score Constants are Config-Exposed but Need Review

**Severity:** Low (documentation + one logic gap)  
**File:** `optdash/analytics/pcr.py`, `optdash/config.py`

### What is well-implemented (do NOT change)

Looking at the live `pcr.py`:
- `_trailing_pcr_metrics()` computes rolling mean/std over `PCR_ZSCORE_WINDOW` snaps ✅
- `_pcr_signal_z()` uses Z > 1.5 / Z < -1.5 for panic signals, falls back to absolute thresholds when window is not filled ✅
- `div_trend` (LAG-based momentum) correctly softens panic signals on reversion (`DIVERGENCE_FADING`) ✅
- `get_pcr_series()` uses SQL window functions for the series path ✅

### What is missing — Z-score threshold not in config

`_pcr_signal_z()` has **hardcoded Z thresholds**:

```python
# Current hardcoded values in pcr.py — lines inside _pcr_signal_z():
if z > 1.5:
    signal = "RETAIL_PANIC_PUTS"
elif z < -1.5:
    signal = "RETAIL_PANIC_CALLS"
elif abs(z) > 0.8:
    signal = "DIVERGENCE_BUILDING"
```

And the divergence fading thresholds:

```python
if signal == "RETAIL_PANIC_PUTS" and div_trend < -0.05:
    signal = "DIVERGENCE_FADING"
elif signal == "RETAIL_PANIC_CALLS" and div_trend > 0.05:
    signal = "DIVERGENCE_FADING"
```

These 4 values cannot be tuned without editing source code.

### Fix — Add Z-score signal thresholds to `config.py`

In the `# -- PCR` section:

```python
# PCR Z-score signal thresholds.
# Z-score = (current_div - rolling_mean) / rolling_std
# over PCR_ZSCORE_WINDOW snaps (default 20).
#
# PCR_Z_PANIC_THRESHOLD:     |Z| > this → RETAIL_PANIC_PUTS / RETAIL_PANIC_CALLS
# PCR_Z_BUILDING_THRESHOLD:  |Z| > this → DIVERGENCE_BUILDING (weaker signal)
# PCR_Z_FADING_TREND:        div_trend magnitude to trigger DIVERGENCE_FADING override.
#   Positive value: when puts-panic but div is falling by this much, signal softens.
PCR_Z_PANIC_THRESHOLD:    float = 1.5
PCR_Z_BUILDING_THRESHOLD: float = 0.8
PCR_Z_FADING_TREND:       float = 0.05
```

### Fix — Replace hardcoded values in `pcr.py`

In `_pcr_signal_z()`, replace the hardcoded literals:

```python
def _pcr_signal_z(div: float, z: float, snap_count: int, div_trend: float) -> str:
    signal = "BALANCED"
    if snap_count >= settings.PCR_ZSCORE_WINDOW:
        if z > settings.PCR_Z_PANIC_THRESHOLD:
            signal = "RETAIL_PANIC_PUTS"
        elif z < -settings.PCR_Z_PANIC_THRESHOLD:
            signal = "RETAIL_PANIC_CALLS"
        elif abs(z) > settings.PCR_Z_BUILDING_THRESHOLD:
            signal = "DIVERGENCE_BUILDING"
    else:
        # Fallback to absolute thresholds before window is filled
        if div > settings.PCR_DIV_BULL_THRESHOLD:
            signal = "RETAIL_PANIC_PUTS"
        elif div < settings.PCR_DIV_BEAR_THRESHOLD:
            signal = "RETAIL_PANIC_CALLS"
        elif abs(div) > 0.10:
            signal = "DIVERGENCE_BUILDING"

    # Reversion softener
    if signal == "RETAIL_PANIC_PUTS" and div_trend < -settings.PCR_Z_FADING_TREND:
        signal = "DIVERGENCE_FADING"
    elif signal == "RETAIL_PANIC_CALLS" and div_trend > settings.PCR_Z_FADING_TREND:
        signal = "DIVERGENCE_FADING"

    return signal
```

### Commit message

```
fix(pcr,config): expose PCR Z-score signal thresholds as named config constants (B-4)

Replaced hardcoded 1.5 / 0.8 / 0.05 literals in _pcr_signal_z() with
PCR_Z_PANIC_THRESHOLD, PCR_Z_BUILDING_THRESHOLD, PCR_Z_FADING_TREND.
No logic change — pure parameterization for safe runtime tuning.
```

---
""")

# ---------- ISSUE B-5: Confidence Score Bucket 4 min trade count ----------
sections.append("""## Issue B-5 — Confidence Bucket 4 Minimum Trade Count is Hardcoded

**Severity:** Low  
**File:** `optdash/ai/confidence.py`, `optdash/config.py`

### What is wrong

In the live `confidence.py` (Bucket 4, Historical Performance):

```python
# Current hardcoded minimum in confidence.py line ~51:
if is_fallback or total_trades < 5:
    b4 = 0
```

The value `5` is a magic number. It cannot be tuned via config.  
A project with 50 live days of data might want `total_trades < 20` before trusting  
win_rate. A backtester might want `< 3`. Currently it requires a code edit.

Additionally, the win_rate formula:

```python
win_rate = (raw_wr / 100) if raw_wr is not None else 0.5
b4 = min(10, int(win_rate * 12))
```

`win_rate * 12` means 100% win rate → b4 = 12, but `min(10, ...)` caps at 10.  
A win rate of 84% gives exactly 10 pts. Below 84%, b4 scales linearly.  
The `12` multiplier is also a magic number — if the bucket max changes from 10,  
this formula silently breaks (e.g. bucket max bumped to 15 in future).

### Fix — Add to `config.py` under `# -- Confidence` section

```python
# Bucket 4: Historical Performance gate.
# Minimum number of closed trades required before win_rate is trusted.
# Below this threshold, B4 = 0 (cold-start protection).
CONFIDENCE_B4_MIN_TRADES: int = 5

# Bucket 4 max raw points (before min() cap).
# Keep in sync with Bucket 4 cap in compute_confidence().
# Formula: b4 = min(CONFIDENCE_B4_MAX, int(win_rate * CONFIDENCE_B4_SCALE))
# At 100% win rate: int(1.0 * 12) = 12, capped to 10 = max pts.
# At 83.3% win rate: int(0.833 * 12) = 9 pts.
CONFIDENCE_B4_MAX:   int = 10
CONFIDENCE_B4_SCALE: int = 12   # denominator ceiling; keep at 1.2× B4_MAX
```

### Fix — Update `confidence.py` Bucket 4

```python
# Bucket 4: historical performance — cold-start guard
is_fallback  = learning_stats.get("is_fallback", False)
total_trades = learning_stats.get("total_trades", 0)

if is_fallback or total_trades < settings.CONFIDENCE_B4_MIN_TRADES:
    b4 = 0
else:
    raw_wr   = learning_stats.get("win_rate")
    win_rate = (raw_wr / 100) if raw_wr is not None else 0.5
    b4       = min(settings.CONFIDENCE_B4_MAX,
                   int(win_rate * settings.CONFIDENCE_B4_SCALE))
```

### Commit message

```
fix(confidence,config): expose Bucket 4 min_trades and scale as config constants (B-5)

Replaced hardcoded `total_trades < 5` and win_rate multiplier `12`
with CONFIDENCE_B4_MIN_TRADES=5 and CONFIDENCE_B4_SCALE=12.
No logic change — pure parameterization.
```

---
""")

# ---------- ISSUE B-6: VRP regime not in get_ivr_ivp response to recommender ----------
sections.append("""## Issue B-6 — Recommender Does Not Pass `iv_data` to `compute_confidence()` (Part A dependency verify)

**Severity:** Medium  
**File:** `optdash/ai/recommender.py`  
**Note:** This is the wiring fix that makes Part A's IV-OPEN-2 (VRP into Confidence) actually work.

### Background

Part A added `vrp_regime == "UNDERPRICED"` as a +3 pt bonus in `confidence.py` Bucket 3.  
That change requires `iv_data` to be passed into `compute_confidence()`.

Looking at the live `confidence.py`:  
```python
def compute_confidence(
    gate_score, direction_result, iv_data, gex_data, vex_data, strike, learning_stats, session
):
```

`iv_data` **is already a parameter** — `confidence.py` already receives it.  
The question is whether `recommender.py` passes the correct `iv_data` dict.

### Verify in `recommender.py`

Search for the `compute_confidence(` call. It should look like:

```python
conf_result = compute_confidence(
    gate_score       = gate_data["score"],
    direction_result = direction_result,
    iv_data          = iv_data,           # ← must be the dict from get_ivr_ivp()
    gex_data         = gex_data,
    vex_data         = vex_data,
    strike           = best_strike,
    learning_stats   = learning_stats,
    session          = MarketSession(gate_data["session"]),
)
```

**If `iv_data` is present and comes from `get_ivr_ivp()` — no change needed.**

### Fix — Only if iv_data is missing or wrong

If `recommender.py` passes `iv_data={}` or omits it, find the line that calls `get_ivr_ivp`:

```python
iv_data = get_ivr_ivp(conn, trade_date, snap_time, underlying)
```

Confirm this line runs **before** `compute_confidence()` is called.  
If `get_ivr_ivp` is not called at all in the recommender, add it:

```python
from optdash.analytics.iv import get_ivr_ivp

# Inside the main recommend() function, alongside other analytics calls:
iv_data  = get_ivr_ivp(conn, trade_date, snap_time, underlying)
```

Then pass it to `compute_confidence()` as shown above.

### Commit message

```
fix(recommender): ensure iv_data from get_ivr_ivp() is passed to compute_confidence() (B-6)

Wires the vrp_regime field (added in Part A IV-OPEN-2) into the confidence
score computation. iv_data must reach confidence.py for the VRP bonus to fire.
```

---
""")

# ---------- ISSUE B-7: GEX ZGL not surfaced in environment gate ----------
sections.append("""## Issue B-7 — GEX Zero Gamma Level (ZGL) is Computed but Not Used in Any Gate or Signal

**Severity:** Medium  
**File:** `optdash/analytics/environment.py`, `optdash/analytics/alerts.py`

### Background

`gex.py` already computes the Zero Gamma Level (ZGL) and returns it in `get_net_gex()`:

```python
# From gex.py get_net_gex() return dict:
"zgl":         round(zgl, 1),        # strike where cumulative GEX = 0
"spot_vs_zgl": dist_pct,             # % of spot above (+) or below (-) ZGL
"above_zgl":   above_zgl,            # True = stabilising, False = volatile
```

This data is fetched in `environment.py` as `gex_data` but **none of the 10 gate conditions  
uses `zgl`, `spot_vs_zgl`, or `above_zgl`**. The Zero Gamma Level is the most important  
structural level in the market — below ZGL, dealers are net short gamma and markets trend  
aggressively. This should influence at minimum a pre-flight warning.

### Fix — Add ZGL proximity alert to `alerts.py`

In `alerts.py`, add a new alert generator:

```python
def _check_zgl_proximity(gex_data: dict, spot: float | None) -> dict | None:
    \"\"\"
    Fires APPROACHING_ZGL alert when spot is within ZGL_PROXIMITY_PCT of the
    Zero Gamma Level. Below ZGL, dealers are net short gamma and markets
    trend aggressively — mean-reversion strategies should be avoided.

    above_zgl=True:  spot above ZGL → dealers long gamma → mean-reverting.
    above_zgl=False: spot below ZGL → dealers short gamma → trending/volatile.
    \"\"\"
    zgl       = gex_data.get("zgl")
    above_zgl = gex_data.get("above_zgl")
    dist_pct  = gex_data.get("spot_vs_zgl")

    if zgl is None or dist_pct is None:
        return None

    proximity_threshold = settings.ZGL_PROXIMITY_PCT   # add to config (see below)

    if above_zgl is False:
        # Spot is BELOW ZGL — dealers short gamma — trending regime
        return {
            "alert_type": "BELOW_ZGL",
            "severity":   "HIGH",
            "message": (
                f"Spot is {abs(dist_pct):.1f}% BELOW Zero Gamma Level ({zgl}). "
                f"Dealers net short gamma — trending/volatile regime. "
                f"Avoid mean-reversion strategies. Momentum trades favoured."
            ),
            "zgl": zgl, "spot_vs_zgl": dist_pct,
        }

    if above_zgl is True and abs(dist_pct) < proximity_threshold:
        # Spot above ZGL but approaching from above — watch for flip
        return {
            "alert_type": "APPROACHING_ZGL",
            "severity":   "MEDIUM",
            "message": (
                f"Spot is within {abs(dist_pct):.1f}% of Zero Gamma Level ({zgl}). "
                f"If spot crosses below ZGL, regime flips to trending. Monitor closely."
            ),
            "zgl": zgl, "spot_vs_zgl": dist_pct,
        }
    return None
```

Add `ZGL_PROXIMITY_PCT` to `config.py` under `# -- GEX`:

```python
# ZGL_PROXIMITY_PCT: distance from ZGL (as % of spot) at which to fire
# APPROACHING_ZGL alert. E.g. 0.5 = alert when spot is within 0.5% of ZGL.
ZGL_PROXIMITY_PCT: float = 0.5
```

In the main `get_alerts()` function, add the call:

```python
gex_data = get_net_gex(conn, trade_date, snap_time, underlying)
spot     = gex_data.get("spot")
zgl_alert = _check_zgl_proximity(gex_data, spot)
if zgl_alert:
    alerts.append(zgl_alert)
```

### Commit message

```
feat(alerts,config): add ZGL proximity and below-ZGL regime alerts (B-7)

Uses Zero Gamma Level data already computed by get_net_gex() (GEX-2).
- BELOW_ZGL (HIGH): spot below ZGL → dealers short gamma → trending regime
- APPROACHING_ZGL (MEDIUM): spot within ZGL_PROXIMITY_PCT=0.5% of ZGL
- Added ZGL_PROXIMITY_PCT=0.5 to config.py
```

---
""")

# ---------- RELEASE NOTES + COMMIT ORDER TABLE ----------
sections.append("""## Final Commit — Release Notes

Create `Releases/v2.7.0.md`:

```markdown
# Release Notes — v2.7.0

**Date:** YYYY-MM-DD
**Type:** Analytics Completeness — Strike Screener, PCR, Skew, ZGL, Confidence
**Branch:** fix/analytics-part-b
**Prerequisite:** v2.6.0 (Part A) must be on main

## Summary
Surfaced per-strike VEX/CEX as a dedicated API endpoint. Added Skew×VEX
convergence alert for high-conviction downside detection. Exposed PCR Z-score
thresholds and Confidence Bucket 4 parameters as named config constants.
Added ZGL proximity alerts. Direction alignment flag added to S_Score rows.

## Changes

### B-1 (VEX-6): Per-Strike VEX/CEX API Endpoint
- Exposed _get_by_strike() as GET /api/vex-cex/by-strike dedicated route
- Enables frontend heatmap rendering of strike-level dealer exposure

### B-2 (VEX-7): Skew × VEX Convergence Alert
- New get_iv_skew() in iv.py: 25-delta put/call skew + 3-snap trend direction
- New HIGH_CONVICTION_BEAR alert in alerts.py: fires when skew STEEPENING
  and VEX < -threshold simultaneously
- New config constants: SKEW_ELEVATED_THRESHOLD=5.0, SKEW_STEEPENING_VEX_CONFIRM=True

### B-3: S_Score Direction Alignment Flag
- Added direction_aligned boolean field to each screener row
- Enables frontend to grey-out or flag options that misalign with trade direction
- S_Score formula and star thresholds unchanged

### B-4: PCR Z-score Thresholds in Config
- Replaced hardcoded 1.5/0.8/0.05 in _pcr_signal_z() with config constants
- New: PCR_Z_PANIC_THRESHOLD=1.5, PCR_Z_BUILDING_THRESHOLD=0.8, PCR_Z_FADING_TREND=0.05

### B-5: Confidence Bucket 4 Parameters in Config
- Replaced hardcoded `total_trades < 5` with CONFIDENCE_B4_MIN_TRADES=5
- Replaced hardcoded win_rate multiplier 12 with CONFIDENCE_B4_SCALE=12

### B-6: Recommender iv_data Wiring Verified
- Confirmed/fixed that get_ivr_ivp() result reaches compute_confidence()
- Activates the VRP_UNDERPRICED +3 pt bonus added in v2.6.0

### B-7: ZGL Proximity Alerts
- New _check_zgl_proximity() in alerts.py
- BELOW_ZGL (HIGH severity): spot below Zero Gamma Level
- APPROACHING_ZGL (MEDIUM): spot within 0.5% of ZGL from above
- New config constant: ZGL_PROXIMITY_PCT=0.5
```

---

## Commit Order (follow exactly)

| Order | Issue | Files | Risk |
|-------|-------|-------|------|
| 1 | B-4 | `config.py`, `analytics/pcr.py` | Low — pure parameterization |
| 2 | B-5 | `config.py`, `ai/confidence.py` | Low — pure parameterization |
| 3 | B-6 | `ai/recommender.py` | Low — wiring verify/fix |
| 4 | B-3 | `analytics/screener.py` | Low — additive field only |
| 5 | B-1 | `api/routes/` router file | Low — new endpoint |
| 6 | B-2 | `config.py`, `analytics/iv.py`, `analytics/alerts.py` | Medium — new functions |
| 7 | B-7 | `config.py`, `analytics/alerts.py` | Medium — new alert logic |
| 8 | Release notes | `Releases/v2.7.0.md` | None |

After all commits:

```bash
git push origin fix/analytics-part-b
# Open PR targeting main
# Title: "Analytics Completeness — Skew, ZGL, PCR, Screener, Confidence (v2.7.0)"
```

---

## Cross-Reference: All Open Issues Status After Part A + Part B

| Issue ID | Description | Part | Status After |
|----------|-------------|------|--------------|
| IV-OPEN-1 | VRP/VIX thresholds in config | A | ✅ Done |
| IV-OPEN-2 | VRP into Confidence Score | A | ✅ Done |
| IV-OPEN-3 | VIX gate params in config | A | ✅ Done (via IV-OPEN-1) |
| VEX-1 | Exact BSM vanna | A | ✅ Done |
| VEX-2 | Exact BSM charm | A | ✅ Done |
| VEX-3 | Clip rate monitoring | A | ✅ Done |
| VEX-4 | BANKNIFTY VEX threshold | A | ✅ Done |
| VEX-5 | CEX direction via GEX sign | A | ✅ Done |
| VEX-6 | Per-strike VEX/CEX API | B | ✅ Done (B-1) |
| VEX-7 | Skew×VEX convergence alert | B | ✅ Done (B-2) |
| B-3 | S_Score direction alignment | B | ✅ Done |
| B-4 | PCR Z-score thresholds in config | B | ✅ Done |
| B-5 | Confidence B4 min_trades in config | B | ✅ Done |
| B-6 | Recommender iv_data wiring | B | ✅ Done |
| B-7 | ZGL proximity alerts | B | ✅ Done |
""")
