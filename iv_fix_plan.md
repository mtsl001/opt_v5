# IV Analytics — Fix Plan for Junior Developer
## `iv.py` + `environment.py` + `config.py`
**Version:** v2.3.0 target  
**Repo:** https://github.com/mtsl001/opt_v5  
**Reviewer:** Senior Dev notes consolidated from two external sources + code audit  
**Author of this doc:** Perplexity AI review session, 2026-03-21  

---

## Overview

Six issues were found in the IV analytics module (`optdash/analytics/iv.py`).  
They are grouped into **6 commits**, ordered by dependency and risk.

| ID | Issue | Severity | Files | Gate Impact |
|----|-------|----------|-------|-------------|
| IV-1 | HV20 uses daily `MAX(spot)` — should be day-close | 🔴 HIGH | `iv.py` | `iv_hv_spread` / VRP accuracy |
| IV-2 | IVR uses raw MIN/MAX — one spike distorts 252-day range | 🔴 HIGH | `iv.py`, `config.py` | Display only (IVP drives C5) |
| IV-3 | Term structure uses static ratio — not DTE-normalized | 🔴 HIGH | `iv.py`, `config.py` | Gate C7 directly |
| IV-4 | VRP not named / classified — `iv_hv_spread` is ambiguous | 🟡 MED | `iv.py`, `config.py` | None (display) |
| IV-5 | `iv_hv_spread` silently returns `0.0` when `hv20` is None | 🟡 MED | `iv.py` | None (display) |
| IV-6 | India VIX not integrated into Gate C5 | 🟡 MED | `iv.py`, `environment.py`, `config.py` | Gate C5 directly |

> **NOT in this plan** (already correct, no action needed):
> - Gate C5 already uses IVP, not IVR ✓
> - ATM definition in `get_ivr_ivp()` uses exact single closest strike ✓  
> - IVP has a 20-day minimum sample guard ✓  
> - IVP query and IVR query share the same `IV_LOOKBACK_DAYS` window ✓  
> - `iv_hv_spread` already exists (just needs renaming per IV-4) ✓

---

## Prerequisite: Understand the Code Structure

Before starting, read these files in order:

1. `optdash/analytics/iv.py` — the module you will change
2. `optdash/analytics/environment.py` — Gate C5 reads from `iv.py`
3. `optdash/config.py` — all threshold constants live here
4. `optdash/pipeline/vix_pipeline.py` — VIX parquet layout
5. `data/processed/vix/trade_date=YYYY-MM-DD/vix.parquet` — VIX data file

Key facts:
- `get_ivr_ivp()` is called by `get_environment_score()` in `environment.py`
- The DuckDB view `options_data` covers `data/processed/trade_date=*/` files
- VIX data lives in a **separate** path: `data/processed/vix/trade_date=*/vix.parquet`
- VIX is NOT in the `options_data` view — it must be read with `read_parquet()` directly

---

## Commit 1 — IV-1: Fix HV20 Daily Close (replace `MAX(spot)` → last snap)

### Why
HV20 is computed from `MAX(spot)` grouped by `trade_date`. This picks the intraday
HIGH, not the market close. Log returns computed from `HIGH(day_N) / HIGH(day_N-1)`
overstate realized volatility during trending days. The correct value is the last
`spot` reading of each day — the 15:29 or 15:30 snap at 1-min cadence.

### Files Changed
- `optdash/analytics/iv.py` — `get_ivr_ivp()` HV20 SQL only

### Exact Change

**In `get_ivr_ivp()`**, find the `hv20_row` SQL block (around line 90) and replace
the innermost query.

**OLD SQL** (inside the triple-nested query):
```sql
SELECT
    trade_date,
    LN(
        MAX(spot) /
        LAG(MAX(spot)) OVER (ORDER BY trade_date)
    ) AS daily_ret
FROM options_data
WHERE underlying=? AND trade_date <= ?
GROUP BY trade_date
ORDER BY trade_date DESC
```

**NEW SQL** (replace `MAX(spot)` with `LAST`):
```sql
SELECT
    trade_date,
    LN(
        LAST(spot ORDER BY snap_time) /
        LAG(LAST(spot ORDER BY snap_time)) OVER (ORDER BY trade_date)
    ) AS daily_ret
FROM options_data
WHERE underlying=? AND trade_date <= ?
GROUP BY trade_date
ORDER BY trade_date DESC
```

> `LAST(spot ORDER BY snap_time)` picks the spot value at the latest snap
> of each day — a much better proxy for the NSE closing price than daily high.

### No Other Changes Needed
The outer structure (triple-nested + `LIMIT 22`) stays the same. Only the
`MAX(spot)` expressions inside are replaced.

### How to Test
After the change, print `hv20` for a known date and compare to NSE's official
annualized HV. The new value should be noticeably closer to the official figure
on trending days.

### Commit Message
```
fix(iv): IV-1 — replace MAX(spot) with LAST(spot) for HV20 daily close

HV20 log returns now computed from last snap of each day (≈15:29-15:30)
instead of daily intraday high. Prevents upward bias in realized vol
on trending days. Triple-nested query structure unchanged.
```

---

## Commit 2 — IV-2: Fix IVR Outlier Trap (percentile caps on 252-day range)

### Why
`iv_low = MIN(daily_atm_iv)` and `iv_high = MAX(daily_atm_iv)` over 252 days.
One event spike (e.g. election day IV jumps to 80%) permanently pins `iv_high = 80`
for the next year. Every subsequent IVR reading will be near zero even when IV is
genuinely elevated at 25%.

The fix: replace `MIN`/`MAX` with **1st percentile** and **99th percentile**.
A single spike event will no longer anchor the range.

### Files Changed
- `optdash/analytics/iv.py` — `get_ivr_ivp()` historical stats SQL
- `optdash/config.py` — add two new constants

### Step 1 — Add constants to `config.py`

Find the `# -- IV` section (around line with `IV_LOOKBACK_DAYS`) and add:

```python
IVR_LOW_PERCENTILE:  float = 0.01   # 1st percentile — strips black-swan spikes
IVR_HIGH_PERCENTILE: float = 0.99   # 99th percentile
```

### Step 2 — Update `iv.py` hist SQL

**OLD SQL** (the `hist` query in `get_ivr_ivp()`):
```sql
SELECT
    MIN(daily_atm_iv) AS iv_low,
    MAX(daily_atm_iv) AS iv_high,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY daily_atm_iv) AS iv_median
FROM (
    SELECT trade_date, AVG(iv) AS daily_atm_iv
    FROM options_data
    WHERE underlying=? AND expiry_tier='TIER1'
      AND trade_date < ?
      AND trade_date >= ?
    GROUP BY trade_date
)
```

**NEW SQL**:
```sql
SELECT
    PERCENTILE_CONT(?) WITHIN GROUP (ORDER BY daily_atm_iv) AS iv_low,
    PERCENTILE_CONT(?) WITHIN GROUP (ORDER BY daily_atm_iv) AS iv_high,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY daily_atm_iv) AS iv_median
FROM (
    SELECT trade_date, AVG(iv) AS daily_atm_iv
    FROM options_data
    WHERE underlying=? AND expiry_tier='TIER1'
      AND trade_date < ?
      AND trade_date >= ?
    GROUP BY trade_date
)
```

Update the parameter list from:
```python
conn.execute(sql, [underlying, trade_date, hist_start])
```
To:
```python
conn.execute(sql, [
    settings.IVR_LOW_PERCENTILE,   # 1st param: PERCENTILE_CONT(?) for iv_low
    settings.IVR_HIGH_PERCENTILE,  # 2nd param: PERCENTILE_CONT(?) for iv_high
    underlying, trade_date, hist_start
])
```

### Note on Existing Fallbacks
The existing fallback logic is correct and does not change:
```python
iv_low    = hist[0] if hist and hist[0] else atm_iv * 0.5
iv_high   = hist[1] if hist and hist[1] else atm_iv * 1.5
```

### Commit Message
```
fix(iv): IV-2 — replace MIN/MAX with percentile caps for IVR range

iv_low / iv_high now use 1st and 99th percentile (configurable via
IVR_LOW_PERCENTILE / IVR_HIGH_PERCENTILE in config.py).
Prevents single black-swan event from permanently anchoring the
252-day IV range and making IVR read near-zero for months.
```

---

## Commit 3 — IV-3: Fix Term Structure Shape (slope normalization)

### Why
Current code: `ratio = far_iv / near_iv`, fires CONTANGO if `ratio > 1.05`.
Problem: on expiry week, near-expiry has 1–2 DTE and TIER2 has 25+ DTE.
Normal carry makes `ratio ≈ 1.15–1.25` even in a completely healthy market.
Gate C7 will fire BACKWARDATION/CONTANGO when nothing unusual is happening.

The fix: replace the static ratio with a **slope per √DTE unit** — this
normalizes the curve so the same threshold applies across all expiry distances.

Formula:
```
Slope = (IV_far - IV_near) / (√DTE_far - √DTE_near)
```

### Files Changed
- `optdash/analytics/iv.py` — `_classify_shape()` signature and logic, `get_term_structure()` call
- `optdash/config.py` — add two new slope thresholds

### Step 1 — Add constants to `config.py`

Find the `# -- IV` section and add:
```python
TS_CONTANGO_SLOPE:      float =  0.30   # annualized IV units per √day
TS_BACKWARDATION_SLOPE: float = -0.30
```

> Start with ±0.30. After one week of live data, check how often these fire
> and tune up or down.

### Step 2 — Update `_classify_shape()` in `iv.py`

**OLD:**
```python
def _classify_shape(near_iv: float | None, far_iv: float | None) -> str:
    if near_iv is None or far_iv is None or near_iv == 0:
        return TermStructureShape.FLAT.value
    ratio = far_iv / near_iv
    if ratio > 1.05:
        return TermStructureShape.CONTANGO.value
    if ratio < 0.95:
        return TermStructureShape.BACKWARDATION.value
    return TermStructureShape.FLAT.value
```

**NEW:**
```python
import math   # add at top of file if not already present

def _classify_shape(
    near_iv:  float | None,
    far_iv:   float | None,
    near_dte: int = 30,
    far_dte:  int = 60,
) -> str:
    if near_iv is None or far_iv is None or near_iv == 0:
        return TermStructureShape.FLAT.value
    denom = math.sqrt(max(far_dte, 1)) - math.sqrt(max(near_dte, 1))
    if denom <= 0:
        # Same DTE or near > far — cannot compute meaningful slope
        return TermStructureShape.FLAT.value
    slope = (far_iv - near_iv) / denom
    if slope > settings.TS_CONTANGO_SLOPE:
        return TermStructureShape.CONTANGO.value
    if slope < settings.TS_BACKWARDATION_SLOPE:
        return TermStructureShape.BACKWARDATION.value
    return TermStructureShape.FLAT.value
```

### Step 3 — Pass `dte` values from `get_term_structure()`

`get_term_structure()` already queries `MIN(o.dte) AS dte` per row.
The `series` list already has `"dte"` in each entry.
Update the `_classify_shape()` call at the end of `get_term_structure()`:

**OLD:**
```python
shape = _classify_shape(near_iv, far_iv)
```

**NEW:**
```python
near_dte = int(series[0]["dte"] or 1)
far_dte  = int(series[-1]["dte"] or 60)
shape    = _classify_shape(near_iv, far_iv, near_dte, far_dte)
```

### Commit Message
```
fix(iv): IV-3 — replace static ratio with DTE-normalized slope for term structure

_classify_shape() now computes (IV_far - IV_near) / (√DTE_far - √DTE_near).
Prevents false CONTANGO on expiry week when normal carry widens the ratio.
New config keys: TS_CONTANGO_SLOPE, TS_BACKWARDATION_SLOPE (±0.30 default).
Gate C7 now fires only on genuine term structure distortion.
```

---

## Commit 4 — IV-4 + IV-5: VRP Naming + None Fix

### Why
**IV-4:** `iv_hv_spread` is confusing. The field is the Volatility Risk Premium
(VRP = ATM_IV − HV20). Traders reading the API response see `iv_hv_spread = 4.2`
with no context. Rename to `vrp` and add a `vrp_regime` label.

**IV-5:** When `hv20 is None` (first 22 trading days), the code does:
```python
"iv_hv_spread": round(atm_iv - (hv20 or atm_iv), 2)
```
`(None or atm_iv)` evaluates to `atm_iv`, so spread = 0.0 — which looks like
"IV equals realized vol" when actually there's no HV data at all.

Both are fixed in the same commit since they touch the same lines.

### Files Changed
- `optdash/analytics/iv.py` — `get_ivr_ivp()` return dict
- `optdash/config.py` — add VRP thresholds

### Step 1 — Add constants to `config.py`
```python
VRP_OVERPRICED_THRESHOLD:  float =  2.0   # IV exceeds HV20 by 2 pts → sellers favored
VRP_UNDERPRICED_THRESHOLD: float =  0.0   # IV below HV20 → buyers favored
```

### Step 2 — Update the return dict in `get_ivr_ivp()`

**OLD:**
```python
"iv_hv_spread": round(atm_iv - (hv20 or atm_iv), 2),
```

**NEW:**
```python
vrp = round(atm_iv - hv20, 2) if hv20 is not None else None

vrp_regime = (
    "OVERPRICED"  if vrp is not None and vrp >  settings.VRP_OVERPRICED_THRESHOLD  else
    "UNDERPRICED" if vrp is not None and vrp <  settings.VRP_UNDERPRICED_THRESHOLD else
    "FAIR"        if vrp is not None else
    "UNKNOWN"
)
```

Then in the return dict, replace:
```python
"iv_hv_spread": round(atm_iv - (hv20 or atm_iv), 2),
```
With:
```python
"vrp":          vrp,
"iv_hv_spread": vrp,        # backward-compat alias — keeps frontend working
"vrp_regime":   vrp_regime,
```

> `iv_hv_spread` is kept as an alias so any existing frontend or API consumer
> that reads `iv_hv_spread` does not break. Remove it in a future cleanup.

### Commit Message
```
fix(iv): IV-4 IV-5 — rename iv_hv_spread to vrp, fix None propagation

iv_hv_spread = None is now correctly returned when hv20 is unavailable
(was silently returning 0.0 due to `hv20 or atm_iv` coercion).
vrp_regime label added: OVERPRICED / UNDERPRICED / FAIR / UNKNOWN.
iv_hv_spread kept as backward-compat alias.
```

---

## Commit 5 — IV-6: India VIX Integration into Gate C5

### Why
Gate C5 currently fires on `ivp_val < 50` (IV cheap = buy options).
India VIX from `data/processed/vix/` is already collected every minute
by `vix_pipeline.py` but no analytics module reads it yet.

When India VIX > 20 (elevated fear), even a low IVP reading (e.g. IVP = 30)
should NOT be treated as "IV cheap to buy" — the market is in high-volatility
regime and option prices can spike further intraday.

This commit adds a VIX-adjusted penalty to the C5 note and IVP display,
without changing Gate C5's pass/fail logic (that is Commit 6).

### Files Changed
- `optdash/analytics/iv.py` — new `get_india_vix()` helper
- `optdash/config.py` — add `VIX_HIGH_THRESHOLD`

### Step 1 — Add constant to `config.py`
```python
# -- India VIX
VIX_HIGH_THRESHOLD: float = 20.0   # VIX above this = elevated regime
```

### Step 2 — Add `get_india_vix()` to `iv.py`

Add this new function at the bottom of `iv.py` (before the closing):

```python
def get_india_vix(trade_date: str, snap_time: str) -> float | None:
    """Read India VIX for the given snap from the VIX parquet side-file.

    VIX data lives outside the main options_data DuckDB view — it is stored
    at data/processed/vix/trade_date=YYYY-MM-DD/vix.parquet and must be
    read directly with read_parquet(), not via the conn object.

    Returns the float VIX value, or None if the file does not exist yet
    (e.g. market not open, VIX pull lagging, or first startup).
    Non-fatal by design — a VIX read failure must never crash IV analytics.
    """
    try:
        from pathlib import Path
        import pyarrow.parquet as pq
        vix_path = (
            Path(settings.DATA_ROOT) / "vix"
            / f"trade_date={trade_date}" / "vix.parquet"
        )
        if not vix_path.exists():
            return None
        table = pq.read_table(
            vix_path,
            columns=["snap_time", "india_vix"],
            filters=[("snap_time", "==", snap_time)],
        )
        if table.num_rows == 0:
            # Exact snap not found — return latest available snap
            full = pq.read_table(vix_path, columns=["snap_time", "india_vix"])
            df = full.to_pandas().dropna(subset=["india_vix"])
            if df.empty:
                return None
            return float(df.sort_values("snap_time").iloc[-1]["india_vix"])
        val = table.to_pandas()["india_vix"].iloc[0]
        return float(val) if val is not None else None
    except Exception as e:
        logger.debug("get_india_vix: read failed (non-critical) — {}", e)
        return None
```

### Step 3 — Add `india_vix` to `get_ivr_ivp()` return dict

At the end of `get_ivr_ivp()`, before the final `return {}`, add:

```python
india_vix      = get_india_vix(trade_date, snap_time)
vix_regime     = (
    "HIGH" if india_vix is not None and india_vix > settings.VIX_HIGH_THRESHOLD
    else "NORMAL" if india_vix is not None
    else "UNKNOWN"
)

return {
    ...existing keys...,
    "india_vix":  round(india_vix, 2) if india_vix is not None else None,
    "vix_regime": vix_regime,
}
```

### How to Test
1. Verify `data/processed/vix/trade_date=YYYY-MM-DD/vix.parquet` exists
2. Call `/api/iv?underlying=NIFTY&trade_date=...&snap_time=...`
3. Check response contains `"india_vix": 18.5, "vix_regime": "NORMAL"`
4. When VIX > 20, response shows `"vix_regime": "HIGH"`

### Commit Message
```
feat(iv): IV-6a — add India VIX read to get_ivr_ivp() response

New helper get_india_vix() reads from data/processed/vix/ Parquet
side-file. Returns None non-fatally when file unavailable.
get_ivr_ivp() now returns india_vix + vix_regime fields.
VIX_HIGH_THRESHOLD = 20.0 added to config.py.
Gate C5 logic unchanged — see next commit.
```

---

## Commit 6 — IV-6b: VIX Penalty in Gate C5

### Why
After Commit 5, `india_vix` is available in `iv_data`. This commit uses it
to apply a **display note** and **adjusted IVP threshold** in Gate C5:

- Normal: C5 passes when `ivp < 50`
- When VIX is HIGH (> 20): C5 only passes when `ivp < 35`
  (tighter bar — must be in bottom 35th percentile to qualify as "cheap"
  in a high-fear regime)

### Files Changed
- `optdash/analytics/environment.py` — C5 gate logic block
- `optdash/config.py` — add `VIX_HIGH_IVP_THRESHOLD`

### Step 1 — Add constant to `config.py`
```python
VIX_HIGH_IVP_THRESHOLD: float = 35.0   # IVP must be below this when VIX is HIGH
# Normal threshold is still IVP < 50 (reuses existing gate logic)
```

### Step 2 — Update Gate C5 in `environment.py`

Find the C5 block (currently around line 80):

**OLD:**
```python
# C5: IV cheap (IVP < 50) (1 pt)
ivp_val = ivp if ivp is not None else 100.0
c5_met  = ivp_val < 50
conditions["ivp_cheap"] = {
    "met": c5_met, "value": round(ivp_val, 1),
    "points": 1, "note": f"IVP = {ivp_val:.0f}th pct"
}
```

**NEW:**
```python
# C5: IV cheap — threshold tightens when India VIX is elevated (1 pt)
# When VIX_HIGH_THRESHOLD is breached, require IVP < VIX_HIGH_IVP_THRESHOLD
# (default 35) instead of the normal < 50. This prevents "IV cheap" signal
# from firing in high-fear regimes where IV can spike further intraday.
india_vix  = iv_data.get("india_vix")
vix_regime = iv_data.get("vix_regime", "UNKNOWN")
ivp_val    = ivp if ivp is not None else 100.0

if vix_regime == "HIGH":
    c5_threshold = settings.VIX_HIGH_IVP_THRESHOLD   # 35 when VIX elevated
    c5_note_suffix = f" | VIX={india_vix:.1f} HIGH → threshold={c5_threshold}"
else:
    c5_threshold   = 50.0
    c5_note_suffix = f" | VIX={'N/A' if india_vix is None else f'{india_vix:.1f}'}"

c5_met = ivp_val < c5_threshold
conditions["ivp_cheap"] = {
    "met": c5_met, "value": round(ivp_val, 1),
    "points": 1,
    "note": f"IVP = {ivp_val:.0f}th pct{c5_note_suffix}"
}
```

### Important: Gate Score Does Not Change
The C5 point weight remains 1. The max score stays 11. The only change is the
IVP threshold used to decide pass/fail, and the note text shown to the trader.

### How to Test
- VIX = 18, IVP = 40: C5 passes (40 < 50, normal threshold)
- VIX = 22, IVP = 40: C5 fails (40 > 35, HIGH threshold)  
- VIX = 22, IVP = 28: C5 passes (28 < 35, HIGH threshold still met)
- VIX = None, IVP = 40: C5 passes (unknown VIX → use normal threshold)

### Commit Message
```
feat(iv): IV-6b — VIX-adjusted IVP threshold in Gate C5

When india_vix > VIX_HIGH_THRESHOLD (20.0), Gate C5 requires
IVP < VIX_HIGH_IVP_THRESHOLD (35.0) instead of normal < 50.
Prevents "IV cheap" signal in elevated fear regimes.
Gate max score (11) and all other gates unchanged.
VIX_HIGH_IVP_THRESHOLD = 35.0 added to config.py.
```

---

## Final Commit Order Summary

| Order | Commit | Depends On | Risk |
|-------|--------|-----------|------|
| 1 | IV-1: HV20 → `LAST(spot)` | None | Low |
| 2 | IV-2: IVR percentile caps | None (can parallel with 1) | Low |
| 3 | IV-3: Term structure slope | None (can parallel with 1,2) | Medium — recalibrate TS thresholds |
| 4 | IV-4 + IV-5: VRP rename + None fix | None | Low |
| 5 | IV-6a: India VIX read | None | Low (non-fatal) |
| 6 | IV-6b: VIX Gate C5 penalty | Commit 5 (needs `india_vix` in iv_data) | Medium — verify thresholds |

---

## Config Changes Summary

All new keys to add to `config.py` under their respective sections:

```python
# -- IV (existing section)
IVR_LOW_PERCENTILE:      float = 0.01   # IV-2: 1st percentile for IVR low anchor
IVR_HIGH_PERCENTILE:     float = 0.99   # IV-2: 99th percentile for IVR high anchor
TS_CONTANGO_SLOPE:       float = 0.30   # IV-3: slope threshold for CONTANGO
TS_BACKWARDATION_SLOPE:  float = -0.30  # IV-3: slope threshold for BACKWARDATION
VRP_OVERPRICED_THRESHOLD: float = 2.0   # IV-4: IV exceeds HV20 by this → OVERPRICED
VRP_UNDERPRICED_THRESHOLD: float = 0.0  # IV-4: IV below HV20 → UNDERPRICED

# -- India VIX (new section)
VIX_HIGH_THRESHOLD:      float = 20.0   # IV-6: VIX above this = HIGH regime
VIX_HIGH_IVP_THRESHOLD:  float = 35.0   # IV-6b: IVP threshold when VIX is HIGH
```

---

## Testing Checklist Before Merging

After all 6 commits are done:

- [ ] `get_ivr_ivp()` returns `vrp`, `iv_hv_spread` (alias), `vrp_regime`
- [ ] `get_ivr_ivp()` returns `india_vix` and `vix_regime`  
- [ ] `vrp = None` when `hv20 is None` (not `0.0`)
- [ ] HV20 changes on a trending day vs flat day (sanity check vs old value)
- [ ] IVR no longer reads near-zero after a past spike event (verify with backfill data)
- [ ] Term structure CONTANGO/BACKWARDATION does not fire on expiry week for no reason
- [ ] Gate C5 note shows VIX value in the `conditions` response
- [ ] Gate C5 fails when IVP = 40 AND VIX > 20 (threshold = 35)
- [ ] Gate C5 passes when IVP = 40 AND VIX < 20 (threshold = 50)
- [ ] Gate C5 passes (normal) when `india_vix = None`
- [ ] Service starts cleanly — no import errors, no pydantic validation errors

---

*Document generated: 2026-03-21*  
*Next review: after v2.3.0 is live for 3 trading days — recalibrate TS_CONTANGO_SLOPE and VIX_HIGH_IVP_THRESHOLD*
