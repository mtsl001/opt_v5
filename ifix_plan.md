## Cross-Cutting Improvements

These apply across all analytics and are the highest-leverage upgrades:

- **Per-underlying threshold calibration:** V_CoC, CoC discount, VEX, and CEX all use absolute ₹ or point values that are not normalized per underlying — systematically review all thresholds and make them percentage-based or underlying-specific.
- **Signal correlation matrix:** V_CoC + Futures OBI are correlated; GEX decline + VEX are partially correlated. A correlation-weighted voting system would reduce redundancy and improve edge detection.
- **Data quality guard layer:** Add a pre-analytics validation step that checks for stale snaps (`record_time` lag > 7 minutes), zero-OI strikes, and missing futures prices before any signal computation runs.
- **Backtesting harness:** The `BACKFILL_START_DATE = 2026-02-17` means there's now ~4 weeks of historical Parquet data. Running each analytics module's signals against historical trade outcomes (journal SQLite) would empirically validate thresholds rather than relying on theoretical reasoning alone.
- **Stop-loss and target calibration:** `sl_price = ltp × 0.65` (35% SL) and `target = ltp × 1.50` (50% target) are fixed percentage-based rules that don't adapt to IV environment. In high-IV environments, options swing more — the SL/target should scale with `IVP` or `HV20`.

---

# Full Codebase Review — Issues Found
*Review date: 2026-03-24 | All issues from local codebase only*

---

## A. `optdash/models/enums.py`

### A-1 — `AlertType` missing used types
**File:** `enums.py` line 72–83  
**Severity:** Bug  
`alerts.py` emits alerts with `type_="HIGH_CONVICTION_BEAR"` and `type_="BELOW_ZGL"` / `"APPROACHING_ZGL"` as raw strings via `_make_alert()` using `AlertType` enum entries. However, `AlertType` does **not** contain `HIGH_CONVICTION_BEAR`, `BELOW_ZGL`, or `APPROACHING_ZGL` members. The skew-VEX and ZGL alert dicts are constructed manually (bypassing `_make_alert`) with string literals, but the enum coverage is absent and deduplication key `a["type"]` behaves inconsistently — typed alerts use `.value` while manual alerts use raw strings. This prevents downstream type-safe serialization.  
**Fix:** Add `HIGH_CONVICTION_BEAR`, `BELOW_ZGL`, `APPROACHING_ZGL` to `AlertType` enum and route `_check_skew_vex_convergence` and `_check_zgl_proximity` through `_make_alert()`.

---

## B. `optdash/analytics/alerts.py`

### B-1 — `_check_skew_vex_convergence` bypasses `_make_alert`, missing fields
**File:** `alerts.py` lines 36–46  
**Severity:** Medium  
The dict returned by `_check_skew_vex_convergence()` includes extra keys (`skew`, `vex_total_M`) that `_make_alert()` outputs don't have. More critically, the deduplication loop at line 237 uses `a["type"]` — but this alert's type is the raw string `"HIGH_CONVICTION_BEAR"` (not an `AlertType` enum `.value`). If `_make_alert` is ever called with the same type string it won't deduplicate against the manual dict correctly.  
**Fix:** Route through `_make_alert()` using the proper `AlertType` enum member.

### B-2 — `_check_zgl_proximity` bypasses `_make_alert`, same issue
**File:** `alerts.py` lines 63–85  
**Severity:** Medium  
Same as B-1: manual dict with raw `type` strings `"BELOW_ZGL"` / `"APPROACHING_ZGL"` bypasses the standard alert factory. Deduplication at line 237 is inconsistent.  
**Fix:** Same as B-1 — add enum members, route through `_make_alert()`.

### B-3 — Opening suppression uses `>` not `>=`
**File:** `alerts.py` lines 136, 149, 192, 204  
**Severity:** Low  
```python
if cur_coc["snap_time"] > settings.ALERT_OPENING_SUPPRESS_END:
```
This suppresses alerts at exactly `09:25` (the configured endpoint), but the intent is to fire alerts *starting* at `09:25`. The condition should be `>=` so alerts at exactly `09:25` are not suppressed. Currently the first valid alert snap (`09:25`) is silently dropped.  
**Fix:** Change all four occurrences from `>` to `>=`.

### B-4 — `lookback_snaps` default covers only 12 snaps but docstring says "60 min"
**File:** `alerts.py` line 94–96  
**Severity:** Low  
```python
def get_alerts(..., lookback_snaps: int = 12) -> list[dict]:
    """Return list of alerts from the last 60 min ..."""
```
At 1-min cadence, 12 snaps = 12 min, not 60. The docstring is wrong OR the default should be `60`.  
**Fix:** Either change default to `60` or update docstring to say "12 snaps / 12 minutes at 1-min cadence".

---

## C. `optdash/analytics/environment.py`

### C-1 — `c7_score` and `c7_met` semantics inverted
**File:** `environment.py` lines 161–168  
**Severity:** Bug  
```python
c7_score = -1 if ts == "BACKWARDATION" else 0
c7_met = (ts == "BACKWARDATION")
```
`c7_met = True` when BACKWARDATION, and `is_penalty = True`. In the bucket accumulation at line 233:
```python
bonus_score = sum(c["points"] for c in conditions.values() if c["met"] and not c.get("is_penalty"))
penalty_score = sum(c["points"] for c in conditions.values() if c["met"] and c.get("is_penalty"))
```
When BACKWARDATION (`c7_met=True`, `is_penalty=True`), `penalty_score += c7_score = -1`. So `score = bonus + (-1) = bonus - 1`. This is correct.  
But: when the term structure is CONTANGO or FLAT (`c7_met=False`), no points are added. The gate should *reward* good term structure (CONTANGO gets +1), not just warn on BACKWARDATION. The comment says "IV term structure not backwardation (1 pt)" implying a positive point, but today the only scoring is the -1 penalty. A CONTANGO structure never earns any point. The old system had `c7_score = 0` for CONTANGO which means CONTANGO earns 0 pts (same as FLAT). This is a missed positive signal.  
**Fix:** Add `c7_score = +1 if ts == "CONTANGO" else 0` for non-penalty path and add normal accumulation. Update `GATE_MAX_SCORE`.

### C-2 — `_raw_max` guard has magic `+2` padding that hides real overflows
**File:** `environment.py` line 238  
**Severity:** Low  
```python
if _raw_max > settings.GATE_MAX_SCORE + 2:
```
The `+2` was added to accommodate the DTE=1 bonus where C9 goes from 2 pts to 4 pts (`+2`). But this means a real overflow (e.g. adding a new 2-pt condition) also silently passes the guard until `_raw_max > 13`. A cleaner approach would compute `_dynamic_max` based on `c9_pts` value rather than using a constant padding.  
**Fix:** Replace `GATE_MAX_SCORE + 2` with `settings.GATE_MAX_SCORE + (c9_pts - 2)` to be exact.

### C-3 — `volume_ok` guard at GO level but not WAIT (line 265 is a no-op)
**File:** `environment.py` lines 259–266  
**Severity:** Low  
```python
elif verdict == GateVerdict.WAIT.value:
    if not volume_ok:
        verdict = GateVerdict.WAIT.value  # no-op!
```
When verdict is already WAIT, setting it to WAIT again is a no-op. The intent was probably to downgrade WAIT to NO_GO on low volume — but it doesn't. This means low-volume conditions during WAIT periods are silently ignored.  
**Fix:** Change to `verdict = GateVerdict.NO_GO.value` or remove the `elif` block if WAIT-on-low-volume is not intended.

---

## D. `optdash/analytics/gex.py`

### D-1 — `_get_gex_peak` silently returns 0.0 on exception, masking errors
**File:** `gex.py` lines 334–336  
**Severity:** Medium  
```python
    except Exception:
        return 0.0
```
When `_get_gex_peak()` fails (DuckDB error, schema change), it returns `0.0`. The caller then computes `pct_of_peak = abs(gex_all) / 0.0` — but this is guarded by `if peak != 0 else 0.0`, so pct becomes `0.0`. Gate C1 then sees `gex_pct_near=0.0 <= 70%` → `c1_met=True` — **falsely fires GEX declining on every tick during a DuckDB error**. This is a silent safety inversion: an infrastructure failure causes the gate to erroneously approve trades.  
**Fix:** Log the exception with `logger.warning` and return a sentinel like `None` or `-1.0`, with callers treating `None` as "unavailable" (set `c1_met=False`).

### D-2 — `_compute_zero_gamma_level` uses linear interpolation with swapped axes
**File:** `gex.py` lines 380–384  
**Severity:** Bug  
```python
zgl = float(np.interp(
    0,
    [cum_gex[idx], cum_gex[idx + 1]],   # xp — must be monotonically increasing
    [strikes[idx], strikes[idx + 1]],    # fp — values to interpolate
))
```
`np.interp(x, xp, fp)` requires `xp` to be monotonically increasing, but `cum_gex` values at the zero crossing have opposite signs (by definition of the sign change). If `cum_gex[idx] > 0` and `cum_gex[idx+1] < 0` (downward crossing), `xp` is **not monotonically increasing** — `np.interp` will produce wrong results. The correct formula is:  
```python
# Linear interpolation: zgl = s[idx] + (s[idx+1]-s[idx]) * (-cum_gex[idx]) / (cum_gex[idx+1]-cum_gex[idx])
```
**Fix:** Replace `np.interp` with explicit linear interpolation formula.

---

## E. `optdash/analytics/coc.py`

### E-1 — `_coc_signal` default `dte=30` produces misleading annualization
**File:** `coc.py` line 251  
**Severity:** Low  
```python
def _coc_signal(coc, vcoc, spot=0, dte=30, coc_fv_premium=None):
```
When called without `dte`, the formula `(vcoc / spot) * (365 / 30) * 100` uses `dte=30`. If the actual DTE is  e.g. 2, the annualization factor is `365/30 ≈ 12.17` instead of the correct `365/2 = 182.5` — a 15× underestimate. This means near-expiry V_CoC signals use a much weaker scaling, possibly causing the signal to stay `NORMAL` when it should be `VELOCITY_BULL/BEAR`.  
All actual callers do pass `dte`, but the default masks this risk for future callers.  
**Fix:** Change default to `dte=None` and add `if spot > 0 and dte is not None and dte > 0:` guard.

### E-2 — `get_coc_series` uses `result = []` at correct place but comment says "Bug-1 fix"
**File:** `coc.py` line 93  
**Severity:** Info  
The comment `# Bug-1 fix: result must be initialised before the loop` is present, which is correct. This is already fixed, just noting it for audit completeness.

---

## F. `optdash/analytics/pcr.py`

### F-1 — `_trailing_pcr_metrics` query missing `option_type` filter — computes FUT rows into PCR
**File:** `pcr.py` lines 262–272  
**Severity:** Bug  
```sql
SELECT
    (SUM(CASE WHEN option_type='PE' THEN volume ELSE 0 END) /
     NULLIF(SUM(CASE WHEN option_type='CE' THEN volume ELSE 0 END), 0)) -
    (SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) /
     NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END), 0)) AS div_t1
FROM options_data
WHERE trade_date=? AND underlying=? AND snap_time <= ? AND expiry_tier=?
GROUP BY snap_time
```
This query has no `instrument_type = 'OPT'` filter. FUT rows have `option_type = NULL`, so they contribute to the ELSE 0 paths, diluting CE/PE sums by including FUT volume/OI in the total denominator scan. This should be harmless since FUT `option_type` IS NULL (not CE/PE), but it's an unnecessary table scan of FUT rows. Compare with the main `get_pcr` query which correctly filters `expiry_tier IN ('TIER1', 'TIER2')` but also lacks an explicit `instrument_type='OPT'` filter.  
**Fix:** Add `AND instrument_type = 'OPT'` to both `_trailing_pcr_metrics` and the inner subquery of `get_pcr_series`.

### F-2 — `get_pcr_series` column index mapping for window columns is off-by-one-risk
**File:** `pcr.py` lines 177–184  
**Severity:** Medium  
The SQL returns columns in this order:
```
0:snap_time, 1:pcr_vol_t1, 2:pcr_oi_t1, 3:div_t1, 4:pcr_vol_t2, 5:pcr_oi_t2, 6:div_t2,
7:dte_t1, 8:obi_t1, 9:obi_t2, 10:div_mean_t1, 11:div_std_t1, 12:div_mean_t2, 13:div_std_t2,
14:div_lag_t1, 15:div_lag_t2, 16:smoothed_obi_t1, 17:smoothed_obi_t2
```
But the Python code accesses `r[8]` through `r[15]` and assigns them as `div_mean_t1 = r[8]`, etc. Let's verify: the inner subquery outputs `snap_time, pcr_vol_t1, pcr_oi_t1, pcr_vol_t2, pcr_oi_t2, dte_t1, obi_t1, obi_t2`. The outer query adds window columns. **The mismatch risk**: if the inner-subquery column `obi_t1` (position 6 in inner) becomes position 8 in the outer due to the 3 computed outer columns (`div_t1`, `div_t2` are SQL expressions in inner, not re-selected in outer)… The outer SELECT re-selects from `sub` which already has `div_t1` as position 3 (`ROUND(pcr_vol_t1 - pcr_oi_t1, 4) AS div_t1`). Mapping looks correct but brittle — should use `result.description` like `screener.py` does.  
**Fix:** Use `cursor.description` column names instead of positional indexing, matching the pattern in `screener.py`.

---

## G. `optdash/analytics/iv.py`

### G-1 — `get_india_vix` reads Parquet directly with `pyarrow`, bypassing DuckDB view
**File:** `iv.py` lines 260–281  
**Severity:** Medium  
`get_india_vix()` uses a direct `pyarrow.parquet.read_table()` call with a hard-coded path `data/vix/trade_date=.../vix.parquet`, completely outside DuckDB. This:
1. Opens a new pyarrow file read on every analytics call (not cached)
2. The path is built from `settings.DATA_ROOT / "vix" / f"trade_date={trade_date}" / "vix.parquet"` — this hardcodes the vix partition scheme which is different from the main processed data path
3. The `import` of `pyarrow.parquet` is inside the function — pyarrow must be installed but isn't verified at startup  
**Fix:** Consider registering a DuckDB view for VIX data so it can be queried via `conn` like all other analytics, ensuring the same DuckDB caching and view refresh lifecycle applies.

### G-2 — `_classify_shape` returns `FLAT` as default even when contango/backwardation threshold is exactly 0
**File:** `iv.py` lines 235–246  
**Severity:** Info  
If `TS_CONTANGO_SLOPE` or `TS_BACKWARDATION_SLOPE` is set to `0.0` in config, then:
- `slope > 0.0` → CONTANGO
- `slope < 0.0` → BACKWARDATION  
But `slope == 0.0` → FLAT (correctly)  
This is fine, no bug. But documents the edge case.

### G-3 — `get_ivr_ivp` IVP uses `trade_date < ?` (exclusive today) but IVR uses same lookback
**File:** `iv.py` lines 91–113  
**Severity:** Info  
Both IVP and IVR exclude today (`trade_date < ?`) from historical comparisons, which is correct (today's IV isn't a completed daily data point yet). This is consistent and correct. Documents for clarity.

---

## H. `optdash/analytics/vex_cex.py`

### H-1 — `_get_vex_cex_series` passes `net_gex=0.0` hardcoded to `_interpret()`
**File:** `vex_cex.py` line 120  
**Severity:** Medium  
```python
"interpretation": _interpret(vex_sig, cex_sig, dealer_oc, net_gex=0.0),
```
The series function always passes `net_gex=0.0` to `_interpret()`, meaning the interpretation text for the entire day chart always uses the "GEX neutral" branch rather than the actual GEX sign at each snap. The Dealer O'Clock interpretation ("GEX negative → dealer buying bullish" vs "GEX positive → dealer selling bearish") is always wrong in the series.  
**Fix:** Fetch `gex_all_B` per snap from the series data (join with GEX series) or pass per-row `net_gex` from the already-fetched row (`r[7]` is `spot`, need a `gex_all_B` column added to the query).

---

## I. `optdash/analytics/microstructure.py`

### I-1 — Volume spike threshold hardcoded at `>= 2.0` (not configurable)
**File:** `microstructure.py` line 48  
**Severity:** Low  
```python
"signal": "SPIKE" if ratio >= 2.0 else "NORMAL",
```
The spike threshold `2.0` is hardcoded. `VCOC_SPIKE_EXPIRY_SNAPS` and other constants are configurable but this one isn't. If the threshold needs tuning (e.g. for more volatile underlyings like BANKNIFTY where 2x might be too easy), there's no `.env` override.  
**Fix:** Add `VOLUME_SPIKE_THRESHOLD: float = 2.0` to `config.py` (and validate `> 1.0`), use `settings.VOLUME_SPIKE_THRESHOLD` here.

---

## J. `optdash/ai/direction.py`

### J-1 — `_vcoc_spike_age` return type is `int` but declared/used as `bool` in early return
**File:** `direction.py` line 232  
**Severity:** Bug  
```python
if len(rows) < 2:
    return False   # ← returns bool
```
But the function's docstring says "Returns True if..." and the return type is `int` (0 = no spike, 1–N = age). The early `return False` is inconsistent — should be `return 0`. The caller at line 44:
```python
spike_age = _vcoc_spike_age(...)
if vcoc > settings.VCOC_BULL_THRESHOLD or (spike_age > 0 and vcoc > 0):
```
`spike_age > 0` evaluates `False > 0` which is `0 > 0 = False` in Python, so it accidentally works (since `False == 0`). But `if vcoc > 0 and spike_age > 0` with `spike_age=False` is semantically wrong.  
**Fix:** Change `return False` to `return 0` on line 232.

### J-2 — `unique_source_count` decremented before FOBI weight is capped, double-counting
**File:** `direction.py` lines 88–96  
**Severity:** Medium  
```python
vcoc_fired = next((s for s in signals if "VCOC" in s["signal"]), None)
fobi_fired = next((s for s in signals if "FUT_OBI" in s["signal"]), None)
if vcoc_fired and fobi_fired and vcoc_fired["direction"] == fobi_fired["direction"]:
    fobi_fired["weight"] = 1  # cap combined to 3+1=4

# Count unique sources
unique_source_count = len(signals)
if vcoc_fired and fobi_fired and vcoc_fired["direction"] == fobi_fired["direction"]:
    unique_source_count -= 1
```
The weight cap mutates `fobi_fired["weight"]` in-place. Then `unique_source_count` is also decremented. This effectively penalizes both weight AND count for the same co-firing event. In `confidence.py` B1 formula: `margin * 7 + unique_source_count * 3` — a co-firing VCoC+FUT reduces count by 1, further reducing confidence. This double-penalty may be intentional but should be documented.  
**No immediate fix required** — document the design choice explicitly.

### J-3 — PCR signal (#5) weight is defined in docstring (weight 1) but never added to `signals`
**File:** `direction.py` line 84–132  
**Severity:** Medium  
The docstring at lines 4–10 includes PCR divergence as Signal 5 with weight 1:
```
PCR divergence        → weight 1  (retail sentiment contra-indicator)
Max CE/PE weight = 9 (all signals same direction).
```
But looking at the actual code, PCR divergence is NOT added to `signals`. Instead it's used as a `pcr_modifier` (float multiplier on `margin_adjusted`) after the direction is determined. This means:
1. `unique_source_count` never includes PCR (even when div > threshold)
2. The confidence formula `unique_source_count * 3` never gets the PCR contribution
3. Max vote weight is actually `3 + 2 + 2 + 1 = 8`, not 9 as stated in docstring  
**Fix:** Update docstring to match actual behavior. If PCR should contribute to voting: add it as a proper signal with weight 1.

---

## K. `optdash/ai/confidence.py`

### K-1 — `b2` gate score multiplier uses `gate_max` instead of `GATE_MAX_SCORE` directly
**File:** `confidence.py` line 37–38  
**Severity:** Low  
```python
gate_max = settings.GATE_MAX_SCORE or 10
b2 = min(settings.CONFIDENCE_B2_MAX, int((gate_score / gate_max) * settings.CONFIDENCE_B2_MAX))
```
`settings.GATE_MAX_SCORE or 10` — the `or 10` fallback is dead code since `GATE_MAX_SCORE=11` is always set (and validated). If it were 0, `or 10` would silently substitute, masking the real config error. Use `settings.GATE_MAX_SCORE` directly and handle the `0` case with an explicit validator in `config.py`.

### K-2 — `SESSION_MIDDAY_SMART_PENALTY` used with `getattr` suggesting it may not exist
**File:** `confidence.py` line 91  
**Severity:** Low  
```python
smart_penalty = getattr(settings, "SESSION_MIDDAY_SMART_PENALTY", False)
```
But `SESSION_MIDDAY_SMART_PENALTY: bool = True` is explicitly declared in `config.py` line 626. The `getattr` is unnecessary defensive coding that masks whether the field actually exists at import time. If the field is removed from config, the attribute access silently falls back to `False` instead of raising `AttributeError`, hiding the bug.  
**Fix:** Use `settings.SESSION_MIDDAY_SMART_PENALTY` directly.

---

## L. `optdash/ai/pre_flight.py`

### L-1 — Rule 3 skips theta check when `dte <= 0` but DTE=0 is the **highest** theta risk day
**File:** `pre_flight.py` lines 33–34  
**Severity:** Medium  
```python
if dte is not None and dte <= 0:
    theta_cap = None   # skip theta check
```
On expiry morning (`dte=0`), options have the highest theta — yet the check is explicitly skipped. The intent seems to be "theta ratio is meaningless on expiry" since time value is near zero, but the implicit risk is that deeply ITM options on expiry morning can have effectively zero LTP (illiquid) with high absolute theta — the ratio `theta/ltp` would explode. The current code bypasses this entirely.  
**Fix:** Leave as-is but add an explicit comment explaining the rationale, or add an alternative liquidity block for `dte=0`.

### L-2 — `max_pain_dist` threshold comparison uses `* 100` but setting is already in `%`
**File:** `pre_flight.py` line 54  
**Severity:** Bug  
```python
if abs(max_pain_dist) < settings.PREFLIGHT_MAX_PAIN_PROXIMITY * 100:
```
`PREFLIGHT_MAX_PAIN_PROXIMITY = 0.005` (in `config.py`), so `0.005 * 100 = 0.5%`. But `max_pain_distance_pct` from `get_max_pain()` is:
```python
dist = ((spot - min_strike) / min_strike * 100)  # already in %
```
So the check is: `abs(dist_pct) < 0.5`. This seems intentional. But `PREFLIGHT_MAX_PAIN_PROXIMITY` is named "proximity" and set to `0.005` — it looks like it's stored as a decimal fraction (0.5%) but the `* 100` conversion in pre_flight implies it's stored as fraction. The config comment says `0.005 = alert when spot is within 0.5% of ZGL` for `ZGL_PROXIMITY_PCT` (which is `0.5`, stored as actual %), but `PREFLIGHT_MAX_PAIN_PROXIMITY = 0.005` appears to be stored as a fraction (0.5%), not as %. This naming inconsistency is confusing and error-prone.  
**Fix:** Rename `PREFLIGHT_MAX_PAIN_PROXIMITY` to `PREFLIGHT_MAX_PAIN_PROXIMITY_PCT` and store as `0.5` (matching the `ZGL_PROXIMITY_PCT` convention), then remove the `* 100` multiplication.

---

## M. `optdash/ai/quality.py`

### M-1 — `_SSCORE_MAX` computed at module load using `settings.W_MOMENTUM * 3.0` but docstring says "cap at 3.0x"
**File:** `quality.py` lines 21–30  
**Severity:** Low  
```python
_SSCORE_MAX = (
    settings.W_DELTA * 0.50
    + settings.W_EFF_RATIO
    + ...
    + settings.W_MOMENTUM * 3.0
) * 10
```
The `W_MOMENTUM * 3.0` assumes the momentum factor is always at its maximum cap of 3.0x. But in `screener.py` the momentum is computed as `LEAST(3.0, volume / avg_volume_20d)`, so 3.0 is the maximum possible cap. The normalizer is thus:  
`_SSCORE_MAX ≈ (4×0.5 + 4 + 3 + 2 + 1 + 1 + 1×3) × 10 = (2+4+3+2+1+1+3) × 10 = 160`  
But the docstring says "Theoretical max = 180" and the module comment says delta cap is at `SCREENER_MAX_DELTA (0.50)`, not `0.65`. This is correct. The `_SSCORE_NORM = _SSCORE_MAX * 0.80 ≈ 128` which matches the "practical 99th-pct ≈ 128" comment. No real bug, but the `_SSCORE_MAX` should match the `screener.py` exact formula normalization (W_DELTA * 1.0 appears in screener.py comment, but quality.py uses 0.50).  
**Fix:** Cross-verify with screener's actual maximum achievable s_score in production data.

### M-2 — Circular dependency between Gate, Confidence, and Quality acknowledged but not guarded
**File:** `quality.py` lines 35–36  
**Severity:** Info  
The docstring explicitly notes: "Note: Circular dependency risk. gate_score aligns dynamically within C2 while confidence (C3) inherently uses gate_score already through Confidence B2."  
Quality score C2 uses gate_score directly and C3 uses full confidence which already incorporates gate_score via B2. This means gate_score influences quality 2× (once in C2 directly, once via B2→confidence→C3). A high gate_score inflates quality above what raw condition quality would suggest. This is by design but should be reviewed — if confidence is already capturing gate depth, C2 becomes partially redundant.  
**No fix required** — document or restructure C2 to use a gate quality metric independent of confidence.

---

## N. `optdash/ai/recommender.py`

### N-1 — `iv_tgt_adj` logic appears to have wrong variable name
**File:** `recommender.py` lines 239–240  
**Severity:** Bug  
```python
iv_sl_adj  = max(0.20, min(0.45, settings.AI_SL_PCT + (iv_entry - iv_base) * settings.AI_SL_IV_STEP))
iv_tgt_adj = max(settings.AI_TARGET_MULT, 1.0 + iv_sl_adj * settings.AI_MIN_RR_RATIO)
```
`iv_tgt_adj` is named like it's an IV-adjusted target multiplier, but it's computed as `max(AI_TARGET_MULT, 1.0 + iv_sl_adj * AI_MIN_RR_RATIO)`. With defaults: `max(1.50, 1.0 + iv_sl_adj * 2.0)`. When `iv_sl_adj = 0.35` (default): `max(1.50, 1.0 + 0.70) = max(1.50, 1.70) = 1.70`. So the actual target becomes `entry * 1.70` not `entry * 1.50` as the config implies. The variability is intentional — higher SL% → higher target. But the variable name `iv_tgt_adj` is confusing since it's not purely IV-driven.  
**No code fix required** — rename for clarity: `target_mult = max(settings.AI_TARGET_MULT, ...)`.

### N-2 — `raw_confidence` for quality score uses only 3 out of 4 buckets
**File:** `recommender.py` lines 256–261  
**Severity:** Medium  
```python
raw_confidence = sum([
    conf_result["buckets"].get("signal_alignment", 0),
    conf_result["buckets"].get("gate_score", 0),
    conf_result["buckets"].get("structural", 0)
])
quality = compute_quality_score(strike, gate["score"], confidence, cold_start=cold_start, raw_confidence=raw_confidence)
```
`raw_confidence` deliberately excludes `historical` (B4). The intent is that during `cold_start`, quality is graded on the first 3 buckets only (B1+B2+B3). But `compute_quality_score` uses `raw_confidence` only when `cold_start=True`:
```python
c3_input = raw_confidence if cold_start else confidence
```
The max of B1+B2+B3 is 40+25+25=90, but `c3 = min(30, (c3_input/100)*30)`. So when `cold_start=True` and all buckets are maxed: `c3 = min(30, 0.90*30) = 27`. When not cold start and confidence=100: `c3 = min(30, 1.0*30) = 30`. The 3-point gap between cold-start and warm-start quality C3 is correct by design.  
**Fix required:** None — document the intent.

---

## O. `optdash/pipeline/processor.py`

### O-1 — `avg_volume_20d` present in `BQ_SELECT_COLS` but NOT in `_OUT_COLS` or written to Parquet
**File:** `processor.py` lines 136–154; `config.py` line 302  
**Severity:** Critical Bug  
`avg_volume_20d` is fetched from BigQuery (`BQ_SELECT_COLS`) but is **not** listed in `_OUT_COLS`. In `_write_trade_date`:
```python
out_df = td_df.reindex(columns=_OUT_COLS)
```
This drops `avg_volume_20d` from the output. The screener SQL reads `o.avg_volume_20d` from `options_data` (the DuckDB view over Parquet files), which will always return `NULL` since it was never written to Parquet. Momentum factor in the S_score is always `0` even though the data was fetched.  
**Fix:** Add `"avg_volume_20d"` to `_OUT_COLS` in processor.py AND add corresponding entry in `writer.py::PARQUET_SCHEMA`.

### O-2 — `_compute_gex_vex_cex` uses `apply(lambda row: ...)` for vanna/charm — vectorization regression
**File:** `processor.py` lines 477–483  
**Severity:** Performance  
The P2-1 vectorization comment in the docstring says `sqrt_t` is vectorized, but vanna and charm still use `opts.apply(lambda row: ..., axis=1)` — pure Python row-by-row. For 2,500 OPT rows per snap this is the biggest remaining bottleneck. The comment says "VEX-1 & VEX-2: Exact BSM vanna and charm. Compute using helpers with inf clip..." but the helpers `_compute_exact_vanna` and `_compute_exact_charm` are scalar functions.  
**Fix:** Vectorize using NumPy arrays in `_compute_gex_vex_cex`. The BSM formulas can be fully vectorized with `np.log`, `norm.pdf` on arrays.

### O-3 — `_normalize_types` calls `df.get(src_q)` which silently returns `None` for missing columns
**File:** `processor.py` line 269  
**Severity:** Medium  
```python
df[f"{side}{lvl}_qty"] = pd.to_numeric(df.get(src_q), errors="coerce").astype("Int64")
```
`df.get("depth_bid1_qty")` on a DataFrame returns `None` if the column doesn't exist (not a KeyError), and `pd.to_numeric(None)` returns `NaN`, which then becomes `pd.NA` in Int64. This is intentional for backwards compatibility with older BQ pulls missing depth columns. **However**: if the column truly exists but with a wrong name (e.g. capitalization change in BQ schema), the error is silently swallowed. A log warning would help diagnosis.  
**Fix:** Log a one-time warning when depth columns are absent.

### O-4 — `_process_underlying` calls `raise` on exception, breaking the whole batch for one failure
**File:** `processor.py` lines 196–198  
**Severity:** Medium  
```python
except Exception as e:
    logger.error("processor: failed for {}: {}", underlying, e)
    raise
```
If BANKNIFTY processing fails (e.g. bad data), the `raise` propagates out of `process_and_write`, skipping all remaining underlyings (FINNIFTY, MIDCPNIFTY, NIFTYNXT50). This is a design choice ("fail fast"), but means a single underlying's bad data blocks analytics for all underlyings.  
**Fix:** Consider `continue` instead of `raise` so other underlyings are processed. Log the error but don't abort the batch.

---

## P. `optdash/analytics/screener.py`

### P-1 — Screener SQL references `o.avg_volume_20d` directly from `options_data` (DuckDB view)
**File:** `screener.py` line 99  
**Severity:** Critical (linked to O-1)  
```sql
+ ? * LEAST(3.0, o.volume / NULLIF(TRY_CAST(o.avg_volume_20d AS DOUBLE), 0))
```
`avg_volume_20d` is not in `_OUT_COLS` (processor.py) so it's never written to Parquet → always NULL in DuckDB → `NULLIF(TRY_CAST(NULL AS DOUBLE), 0)` = NULL → `volume / NULL` = NULL → `LEAST(3.0, NULL)` = NULL → s_score momentum factor = NULL. The entire `s_score` becomes NULL (because NULL + number = NULL in SQL), causing `ORDER BY s_score DESC` to sort NULL rows unpredictably.  
**Impact:** All s_score values are NULL → `PREFLIGHT_MIN_SSCORE` check always fails → no recommendations ever issued.  
**Fix:** Linked to O-1. Add `avg_volume_20d` to `_OUT_COLS` in processor.py and PARQUET_SCHEMA in writer.py.

### P-2 — `eff_cap` relaxed to `max(eff_cap, 0.20)` for DTE<=2, but hardcoded `0.20`
**File:** `screener.py` lines 59–60  
**Severity:** Low  
```python
if min_dte <= 2:
    eff_cap = max(eff_cap, 0.20)
```
`0.20` is hardcoded but should reference a config constant (e.g. `SCREENER_EFF_RATIO_DTE2 = 0.20`) to be tunable via `.env`.  
**Fix:** Add `SCREENER_EFF_RATIO_DTE2: float = 0.20` to config.

---

## Q. `optdash/scheduler.py`

### Q-1 — `_is_eod()` uses string comparison `>=` which is lexicographic
**File:** `scheduler.py` line 219  
**Severity:** Low (already mitigated by validator)  
```python
return _snap_time_str() >= settings.EOD_SWEEP_TIME
```
The config validator enforces zero-padded `HH:MM` for all time fields, so lexicographic comparison is safe (e.g. `"15:25" >= "15:25"` is correct). But this relies on the validator — if someone overrides `EOD_SWEEP_TIME` via a non-validated path, string comparison could give wrong results. Documented for awareness.

### Q-2 — `eod_ok` variable is set to `False` on error but never checked after finalize_all_shadows block
**File:** `scheduler.py` lines 383–404  
**Severity:** Low  
```python
eod_ok = True
try: eod_force_close(...)
except: eod_ok = False; logger.error(...)
try: finalize_all_shadows(...)
except: eod_ok = False; logger.error(...)
if not eod_ok:
    logger.warning("EOD sweep for {} completed with errors...")
```
The `eod_ok` flag drives the warning log but does nothing else. `done_flags[trade_date]` is still set to `True` (correct by design). The EOD sweep is not retried. The warning is purely informational. This is by design but `eod_ok` itself is misleadingly named since partial EOD failure is treated as "done".  
**No code fix** — rename `eod_ok` to `eod_clean` or add comment explaining partial-failure semantics.

---

## R. `optdash/config.py`

### R-1 — `GATE_MAX_SCORE = 11` but `_raw_max` can legitimately reach 15 on DTE=1
**File:** `config.py` line 552; `environment.py` line 196  
**Severity:** Info  
On DTE=1 Dealer O'Clock: C9 offers 4 pts (not 2), so total possible = `1+4+2+1+1+1+1+1+4=16` with all conditions met (C7 penalty = -1). The guard `if _raw_max > settings.GATE_MAX_SCORE + 2` = `> 13` — with DTE=1 bonus `_raw_max` = `1+4+2+1+1+1+1+1=12` non-penalty points, which is within `13`. But `final score = min(bonus+penalty, GATE_MAX_SCORE)` so the 12 raw is capped to 11. This is consistent but the `GATE_MAX_SCORE` name implies the absolute maximum achievable score, which it is (via the `min()` cap). Documented for clarity.

### R-2 — `PREFLIGHT_MIN_SSCORE = 60.0` but s_score NULL issue means it always fails (linked to O-1/P-1)
**File:** `config.py` line 570  
**Severity:** Critical (linked to O-1)  
When `avg_volume_20d` is NULL in Parquet (because it's not in `_OUT_COLS`), the screener SQL computes `s_score = NULL`. Then `pre_flight.py` line 61:
```python
if (strike.get("s_score") or 0) < settings.PREFLIGHT_MIN_SSCORE:
```
`(None or 0) = 0 < 60.0` → this fires → recommendation blocked. All recommendations that make it past the direction+gate checks are blocked at pre-flight by this NULL s_score.  
**Fix:** Fix O-1 (add `avg_volume_20d` to `_OUT_COLS`).

### R-3 — `CONFIDENCE_B4_SCALE: int = 10` makes max B4 = `min(10, int(1.0 * 10)) = 10 = CONFIDENCE_B4_MAX`
**File:** `config.py` lines 622–624  
**Severity:** Info  
`b4 = min(CONFIDENCE_B4_MAX, int(win_rate * CONFIDENCE_B4_SCALE))` = `min(10, int(win_rate * 10))`. At `win_rate=1.0`: `min(10, 10) = 10`. At `win_rate=0.5`: `min(10, 5) = 5`. This is correct, but if `CONFIDENCE_B4_SCALE` is changed independently of `CONFIDENCE_B4_MAX`, the cap may never be reached (if SCALE < MAX) or always hit (if SCALE > MAX). The two constants should be linked.  
**Fix:** Validate `CONFIDENCE_B4_SCALE >= CONFIDENCE_B4_MAX` in a field validator to ensure the cap is reachable.

---

## S. Summary Table

| # | File | Issue | Severity | Type |
|---|------|--------|----------|------|
| A-1 | `enums.py` | `AlertType` missing HIGH_CONVICTION_BEAR, BELOW_ZGL, APPROACHING_ZGL | Medium | Missing enum |
| B-1 | `alerts.py` | Skew-VEX alert bypasses `_make_alert`, inconsistent type field | Medium | Logic |
| B-2 | `alerts.py` | ZGL alert bypasses `_make_alert`, inconsistent type field | Medium | Logic |
| B-3 | `alerts.py` | Opening suppression uses `>` not `>=` — drops first valid snap | Low | Off-by-one |
| B-4 | `alerts.py` | `lookback_snaps=12` docstring says "60 min" — wrong at 1-min cadence | Low | Doc/Logic |
| C-1 | `environment.py` | CONTANGO never earns +1 gate point — missed positive signal | Medium | Logic |
| C-2 | `environment.py` | `_raw_max` guard uses magic `+2` — hides real overflows | Low | Guard |
| C-3 | `environment.py` | WAIT volume guard is a no-op (sets WAIT→WAIT) | Low | Logic |
| D-1 | `gex.py` | Peak cache returns 0.0 on exception → C1 falsely fires | Medium | Safety |
| D-2 | `gex.py` | ZGL interpolation uses non-monotonic xp — wrong result | **Critical** | Calc bug |
| E-1 | `coc.py` | `_coc_signal` default `dte=30` underestimates near-expiry V_CoC | Low | Default |
| F-1 | `pcr.py` | `_trailing_pcr_metrics` scans FUT rows (no instrument_type filter) | Low | Performance |
| F-2 | `pcr.py` | `get_pcr_series` uses positional column indices — brittle | Medium | Code quality |
| G-1 | `iv.py` | `get_india_vix` bypasses DuckDB, uses raw pyarrow per call | Medium | Design |
| H-1 | `vex_cex.py` | Series function always passes `net_gex=0.0` → wrong interpretation | Medium | Logic |
| I-1 | `microstructure.py` | Volume spike threshold `2.0` is hardcoded, not configurable | Low | Config |
| J-1 | `direction.py` | `_vcoc_spike_age` returns `False` (bool) instead of `0` (int) on early exit | Medium | Type bug |
| J-2 | `direction.py` | Co-firing VCoC+FUT penalizes both weight AND count (double-penalty) | Low | Design |
| J-3 | `direction.py` | PCR not added to `signals` but max weight stated as 9 in docstring | Medium | Doc/Logic |
| K-1 | `confidence.py` | `gate_max or 10` dead fallback; direct access is cleaner | Low | Code quality |
| K-2 | `confidence.py` | `getattr(settings, "SESSION_MIDDAY_SMART_PENALTY")` hides missing-field bugs | Low | Code quality |
| L-1 | `pre_flight.py` | Theta check skipped on DTE=0 (expiry morning — highest risk) | Medium | Logic |
| L-2 | `pre_flight.py` | `PREFLIGHT_MAX_PAIN_PROXIMITY` naming inconsistency (fraction vs %) | Medium | Config naming |
| M-1 | `quality.py` | `_SSCORE_MAX` delta factor uses 0.50 — verify against screener max | Low | Calc |
| M-2 | `quality.py` | Gate score influences quality score twice (C2 direct + C3 via confidence) | Low | Design |
| N-1 | `recommender.py` | `iv_tgt_adj` variable name misleading; target can exceed `AI_TARGET_MULT` | Low | Naming |
| N-2 | `recommender.py` | `raw_confidence` excludes B4 intentionally — document | Low | Doc |
| **O-1** | `processor.py` | **`avg_volume_20d` fetched from BQ but NOT in `_OUT_COLS` → never written to Parquet** | **CRITICAL** | **Pipeline bug** |
| O-2 | `processor.py` | Vanna/charm still use row-by-row `apply` — vectorization incomplete | Medium | Performance |
| O-3 | `processor.py` | Missing depth columns silently swallowed with no log warning | Low | Diagnostics |
| O-4 | `processor.py` | `raise` on per-underlying failure aborts entire batch | Medium | Resilience |
| **P-1** | `screener.py` | **`avg_volume_20d` NULL in DuckDB → s_score NULL → all recommendations blocked** | **CRITICAL** | **Downstream of O-1** |
| P-2 | `screener.py` | `eff_cap = 0.20` for DTE<=2 is hardcoded, not configurable | Low | Config |
| Q-1 | `scheduler.py` | EOD time comparison uses lexicographic `>=` (safe but fragile) | Low | Robustness |
| Q-2 | `scheduler.py` | `eod_ok=False` on partial failure drives only a log warning | Low | Naming |
| R-1 | `config.py` | `GATE_MAX_SCORE=11` vs DTE=1 potential 12-pt raw — documented | Info | Config |
| R-2 | `config.py` | `PREFLIGHT_MIN_SSCORE=60` always fails when `avg_volume_20d` is NULL | Critical | Downstream |
| R-3 | `config.py` | `CONFIDENCE_B4_SCALE` and `B4_MAX` not cross-validated | Low | Config |

---

## T. Priority Action Plan

### 🔴 MUST FIX IMMEDIATELY (system broken)

1. **O-1 + P-1 + R-2**: Add `avg_volume_20d` to `processor._OUT_COLS` AND `writer.PARQUET_SCHEMA`.  
   Without this fix, all s_scores are NULL, pre-flight Rule 5 blocks every recommendation, the system emits zero trade cards in production.

2. **D-2**: Fix ZGL linear interpolation in `gex.py` — the non-monotonic `xp` to `np.interp` produces wrong ZGL values, causing BELOW_ZGL and APPROACHING_ZGL alerts to fire at incorrect levels.

### 🟠 HIGH PRIORITY (logic correctness)

3. **B-3**: Change opening suppression from `>` to `>=` in `alerts.py` — first valid snap is silently dropped.
4. **D-1**: Return `None` (not `0.0`) from `_get_gex_peak` on exception — current behavior falsely fires GEX declining gate on DuckDB errors.
5. **H-1**: Pass actual `gex_all_B` to `_interpret()` in `_get_vex_cex_series` — always `net_gex=0.0` gives wrong Dealer O'Clock interpretation.
6. **J-1**: Change `return False` to `return 0` in `_vcoc_spike_age` — type consistency.
7. **A-1 + B-1 + B-2**: Add missing `AlertType` enum members; route manual alert dicts through `_make_alert()`.

### 🟡 MEDIUM PRIORITY (reliability / accuracy)

8. **C-1**: Add +1 gate point for CONTANGO term structure.
9. **C-3**: Fix `WAIT volume guard` no-op (WAIT→WAIT should be WAIT→NO_GO or removed).
10. **J-3**: Update direction.py docstring — PCR is not a vote signal (weight 1), it's a modifier.
11. **L-2**: Rename `PREFLIGHT_MAX_PAIN_PROXIMITY` to `PREFLIGHT_MAX_PAIN_PROXIMITY_PCT` and store as `0.5`.
12. **O-4**: Change `raise` to `continue` + log in `_process_underlying` for per-underlying failure isolation.
13. **F-2**: Use `cursor.description` column names in `get_pcr_series` instead of positional indices.

### 🟢 LOW PRIORITY (code quality / config)

14. **I-1**: Add `VOLUME_SPIKE_THRESHOLD` to `config.py`.
15. **K-2**: Replace `getattr(settings, "SESSION_MIDDAY_SMART_PENALTY", False)` with direct attribute access.
16. **O-2**: Vectorize `_compute_exact_vanna` and `_compute_exact_charm` using NumPy arrays.
17. **P-2**: Add `SCREENER_EFF_RATIO_DTE2` to `config.py`.
18. **R-3**: Add cross-validator for `CONFIDENCE_B4_SCALE >= CONFIDENCE_B4_MAX`.
