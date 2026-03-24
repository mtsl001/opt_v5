Now I have read every file in the analytics layer, plus `ai/` (direction, confidence, tracker), `pnl.py`, `gex.py`, `iv.py`, `coc.py`, `pcr.py`, `vex_cex.py`, `screener.py`, `alerts.py`, and `environment.py`. Here is the complete, thorough review.

***

## Thorough Code Review — `main` HEAD [`1a617c7`](https://github.com/mtsl001/opt_v5/commit/1a617c7e44aaa0bffcad6bdf2b91417372e004e6)

***

## ✅ Fully Resolved — All Previously Raised Issues

Every issue raised across reviews A–H and #1–20 is confirmed fixed. Key evidence per file: 

***

## 🔴 Open Issues (Confirmed on This Pass)


***

### Issue I — `alerts.py`: 3 extra DuckDB queries per tick for prev-snap transition guards

```python
prev_gex_data  = get_net_gex(conn, trade_date, prev_gex_snap, underlying) if prev_gex_snap else None
prev_skew_data = get_iv_skew(conn, trade_date, prev_gex_snap, underlying) if prev_gex_snap else None
prev_vex_data  = get_vex_cex_current(conn, trade_date, prev_gex_snap, underlying) if prev_gex_snap else None
```
 These 3 new point-in-time queries (added in Pass 6 for Issue-5 transition guards) fire on every `get_alerts()` call per underlying — up to 9 extra queries/tick for 3 underlyings. `prev_gex_data` already passes `gex_data=` into `get_vex_cex_current`, but the prev-snap call at line 3 above does **not** pass `gex_data=prev_gex_data`. This means `prev_vex_data` internally calls `get_net_gex()` again for the same prev snap — making it **4 extra queries** per underlying, not 3.

**Fix:** Pass `gex_data=prev_gex_data` into the third call:
```python
prev_vex_data = get_vex_cex_current(
    conn, trade_date, prev_gex_snap, underlying,
    gex_data=prev_gex_data          # ← avoids a 4th round-trip
) if prev_gex_snap else None
```

***

### Issue J — `alerts.py`: `since_snap` not clamped to market open `"09:15"`

```python
total_m = max(0, h * 60 + m - (lookback_snaps + 5))
since_snap = f"{total_m // 60:02d}:{total_m % 60:02d}"
```
 At session open (`snap_time = "09:15"`, `lookback_snaps=12`), this produces `since_snap = "08:58"`, a pre-market time. Options data virtually never exists before `09:15`, so the query is harmless in practice, but if any stray pre-market row exists (e.g. from a feed restart), it will be included in series windows and could trigger false transition alerts.

**Fix (one line):**
```python
since_snap = max(since_snap, "09:15")
```

***

## 🟡 New Issues Found on This Thorough Pass

### Issue K — `environment.py`: C7 BACKWARDATION `is_penalty` sets `points=1` but never subtracts

```python
elif ts == "BACKWARDATION":
    c7_score = 1
    c7_met   = True
    c7_note  = "Shape = BACKWARDATION ⚠️ PENALTY -1"
    # is_penalty flag is set separately below...
```
```python
conditions["term_structure_ok"] = {
    "met": c7_met, "value": ts or "UNKNOWN",
    "points": c7_score,        # ← c7_score = 1
    "is_penalty": (c7_score < 0)   # ← c7_score=1 → is_penalty = False !
}
```
 `c7_score` is set to `1` for BACKWARDATION (not `-1`), so `c7_score < 0` is **always False**. The `is_penalty` flag therefore never activates for BACKWARDATION, meaning:
- `bonus_score` adds 1 point (wrong — should not).
- `penalty_score` adds 0 (wrong — should add 1).
- Net effect: BACKWARDATION scores **+1** instead of **-1** — a **2-point swing** vs intended behaviour.

**Fix:**
```python
elif ts == "BACKWARDATION":
    c7_score = -1           # ← negative so is_penalty = (c7_score < 0) = True
    c7_met   = True
    c7_note  = "Shape = BACKWARDATION ⚠️ PENALTY -1"
```
And in `bonus_score`/`penalty_score` accumulation, `abs(c7_score)` should be used for the penalty:
```python
penalty_score = sum(abs(c["points"]) for c in conditions.values()
                    if c.get("is_penalty") and c["met"])
```

***

### Issue L — `environment.py`: `get_volume_velocity` called with full-day scan every tick

```python
vol_data = get_volume_velocity(conn, trade_date, underlying)
```
 This call at the bottom of `get_environment_score()` has **no `since_snap` argument**, so it fetches the entire day's volume series on every gate evaluation. Unlike `alerts.py` (fixed in Issue D), `environment.py` has not adopted the `since_snap` cutoff. At 375 ticks/day × 3 underlyings × every open position gate check = thousands of full-day DuckDB volume scans per day.

**Fix:**
```python
vol_data = get_volume_velocity(conn, trade_date, underlying,
                               since_snap=snap_time)  # only need last snap
# Then: last_vol = vol_data[-1] if vol_data else None
```
Since only `vol_data[-1]` is ever used, fetching the whole day is pure waste.

***

### Issue M — `gex.py`: `_get_gex_peak` caches stale `0.0` for missing data permanently

```python
result = float(row[0]) if row and row[0] else 0.0
_PEAK_CACHE[cache_key] = result   # ← caches 0.0 on data absence
return result
```
 If `_get_gex_peak()` is called at `09:15` (first tick, no data yet) or during a DuckDB lag, `row[0]` is `None`, `result = 0.0`, and `0.0` is cached permanently for the day. All subsequent calls hit the cache and return `0.0` — meaning `pct_of_peak` is always `None` (protected by the `if peak and peak > 0` guard) for the entire session, silently suppressing Gate C1 for all 375 ticks.

**Fix:** Do not cache `0.0` — only cache values `> 0`:
```python
if result > 0:
    _PEAK_CACHE[cache_key] = result
return result
```

***

### Issue N — `iv.py`: `get_ivr_ivp` embeds `get_term_structure()` call inside its return statement

```python
return {
    ...
    "shape": get_term_structure(
        conn, trade_date, snap_time, underlying
    ).get("shape", "FLAT"),
}
```
 `get_term_structure()` executes a 2-CTE DuckDB query on every `get_ivr_ivp()` call. `get_ivr_ivp()` is called from `environment.py` (every gate tick), `screener.py` (every screener call), and `confidence.py` (every recommendation). There is no caching. This is a hidden N+1 query embedded inside a function that already runs 3 other queries — and callers don't know they are paying for it.

**Fix:** Hoist the call and pass the result in:
```python
ts_data = get_term_structure(conn, trade_date, snap_time, underlying)
return {
    ...
    "shape": ts_data.get("shape", "FLAT"),
}
```
This doesn't reduce the number of calls but makes the cost visible and allows callers to reuse `ts_data` directly if needed.

***

### Issue O — `coc.py`: `_coc_signal` default `dte=None` raises `AttributeError` on fallback path

```python
def _coc_signal(coc, vcoc, spot=0, dte=None, coc_fv_premium=None) -> str:
    if spot > 0 and dte is not None:
        safe_dte = max(1, dte)
        ...
    # Fallback to absolute thresholds when spot/dte unavailable
    if vcoc > settings.VCOC_BULL_THRESHOLD:
        ...
```
 The docstring says `dte=None` is a "safety net to raise AttributeError rather than silently produce a wrong signal when dte is omitted." But the fallback block compares `vcoc > settings.VCOC_BULL_THRESHOLD` — this uses the **absolute** threshold, not the annualized `%` threshold used in the primary path. If `get_coc_latest()` somehow returns `spot=0` (zero-fill on a corrupt row), the fallback fires with wrong thresholds. The primary and fallback paths use different threshold types (`VCOC_BULL_THRESHOLD` in absolute Rs vs `VCOC_BULL_THRESHOLD_PCT` in % terms) for the same signal, making the signal inconsistent when the fallback activates.

**Fix:** At minimum, log a warning when the fallback path fires so it is observable:
```python
if not (spot > 0 and dte is not None):
    logger.warning("_coc_signal fallback path: spot={} dte={} — using absolute thresholds", spot, dte)
```

***

### Issue P — `pcr.py`: `_trailing_pcr_metrics` filters by `instrument_type='OPT'` but `get_pcr_series` does not

In `_trailing_pcr_metrics`:
```python
AND instrument_type='OPT'  -- Fix F-1: exclude FUT rows
```
 In `get_pcr_series`, the inner subquery has no `instrument_type` filter:
```sql
FROM options_data
WHERE trade_date=? AND underlying=? AND expiry_tier IN ('TIER1', 'TIER2')
-- ← no instrument_type='OPT' filter
```
 This means `get_pcr_series` includes FUT rows (where `option_type` is NULL or `'FUT'`) in its TIER1/TIER2 PCR computation, while `get_pcr` (via `_trailing_pcr_metrics`) correctly excludes them. The live tile PCR divergence value and the charted historical series are computed from **different row sets** — the series is systematically noisier and will not match the live tile.

**Fix:** Add `AND instrument_type='OPT'` to the `get_pcr_series` inner subquery's WHERE clause.

***

### Issue Q — `pnl.py`: `compute_theta_sl` uses hardcoded `6.5 * 60` session length

```python
theta_per_min = abs(theta or 0) / (6.5 * 60)
```
 This magic number (390 minutes) is also used in `compute_pnl_attribution` and `build_theta_sl_series`. If market hours ever change (e.g. SEBI extends to 5pm), or for weekend/muhurat trading sessions, this silently produces wrong SL and attribution values. `settings.SESSION_CLOSING_START` is already used in `compute_theta_clock` for remaining-minutes computation, confirming config-driven session length is available.

**Fix (low priority but clean):**
```python
TRADING_MINS = _snap_to_min(settings.SESSION_CLOSING_START) - _snap_to_min("09:15")
theta_per_min = abs(theta or 0) / TRADING_MINS
```

***

### Issue R — `vex_cex.py`: `_get_vex_cex_series` uses only `expiry_tier='TIER1'`, `get_vex_cex_current` uses `TIER1+TIER2`

`_get_vex_cex_series`:
```sql
WHERE trade_date=? AND underlying=? AND expiry_tier='TIER1'
```
`get_vex_cex_current`:
```sql
AND expiry_tier IN ('TIER1', 'TIER2')
```
 The historical series chart and the live tile use different strike universes. The live tile includes TIER2 expiry VEX/CEX pressure, the series does not. On expiry weeks when TIER2 has significant open interest, the series will understate VEX/CEX and the transition from TIER1-only to TIER1+TIER2 (live) will appear as a sudden jump.

**Fix:** Either include `TIER1+TIER2` in the series as well, or document the deliberate difference with a clear comment.

***

## Final Scoreboard

| Status | Count | Issues |
|--------|-------|--------|
| ✅ Confirmed fixed | 25+ | All previously raised A–H, #1–20 |
| 🔴 Critical — fix before production | 2 | **K** (BACKWARDATION penalty never fires — 2pt gate swing), **M** (stale `0.0` peak cached for entire day) |
| 🟠 High priority | 3 | **L** (full-day volume scan on every gate tick), **P** (PCR series includes FUT rows, live tile does not), **I** (4th hidden DuckDB round-trip in alerts) |
| 🟡 Medium priority | 3 | **J** (`since_snap` not clamped to `09:15`), **N** (hidden term-structure query inside `get_ivr_ivp`), **R** (VEX series TIER1-only vs live tile TIER1+TIER2) |
| 🔵 Low priority / tech debt | 2 | **O** (inconsistent fallback threshold type in `_coc_signal`), **Q** (hardcoded 390-min session length in `pnl.py`) |

**Issues K and M are the most urgent** — K silently awards a +2 gate score bonus to BACKWARDATION market conditions (the exact opposite of intended behaviour), and M silently disables Gate C1 for the entire session whenever the peak is first queried on an empty or lagging first tick.