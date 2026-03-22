Good — the three PCR bugs from the previous review are already fixed . Now the focused review on the `_smoothed_obi()` upgrade request.

***

# `_smoothed_obi()` Review & Commit Plan

## Current State of the Code

The L1 depth substitution (`bid1_qty`/`ask1_qty`) **is already done** in `_smoothed_obi()` :

```python
(SUM(COALESCE(bid1_qty,0)) - SUM(COALESCE(ask1_qty,0))) /
NULLIF(SUM(COALESCE(bid1_qty,0) + COALESCE(ask1_qty,0)), 0) AS obi
```

The OBI source column upgrade is complete. What remains is the **smoothing window decision**: the function still uses `LIMIT 3` with a stale docstring that says "15-min smoothing" — which was true at 5-min cadence but is now **3-minute smoothing** at 1-min cadence .

***

## The Decision: Keep 3 Snaps or Expand?

This is the core question. Here is the full analysis:

**At 5-min cadence (old):** `LIMIT 3` = last 15 minutes. A meaningful smoothing window that filtered out single-snap noise while remaining intraday-responsive.

**At 1-min cadence (now):** `LIMIT 3` = last 3 minutes. This is almost no smoothing at all — a single aggressive print at one strike will fully distort `smoothed_obi` for 3 minutes and then vanish. The smoothing effect is essentially gone.

**What does `smoothed_obi` feed?** Looking at the return payload — it's returned in both `get_pcr()` and `get_pcr_series()`. It's a UI/charting metric, not a Gate input (Gate C3 uses `get_futures_obi()` directly from `coc.py`, Gate C6 uses `get_atm_obi()` also from `coc.py`). So the risk of changing the window is low — it only affects how the frontend chart renders OBI smoothness.

**Recommendation: `PCR_OBI_SMOOTH_SNAPS: int = 10`** (10-minute smoothing at 1-min cadence).

- `LIMIT 3` → 3 min is too reactive, essentially raw L1 at 1-min resolution
- `LIMIT 15` → 15 min matches the old feel but at 1-min cadence risks missing intraday turning points that develop in 5–8 minutes
- `LIMIT 10` → 10 min is the right middle ground: absorbs micro-noise from illiquid strikes, still responds to genuine OBI regime shifts within 2 bars

Also: the `get_pcr_series()` inner SQL uses a hardcoded `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW` (= 3 rows) for `smoothed_obi`. This must be updated to match the config value or the series and the live tile will use different smoothing windows — a silent inconsistency .

***

## Issues Found

### 🔴 PCR-OBI-1: Series and Live Tile Use Different Smoothing Windows

`_smoothed_obi()` (used by `get_pcr()`) uses `LIMIT 3` .
`get_pcr_series()` SQL uses `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW` (also 3 rows) .

They match today but both are hardcoded — if you change `_smoothed_obi()` to `LIMIT 10`, the series stays at 3. The series SQL must derive its window from the same config key.

### 🟡 PCR-OBI-2: Stale Docstring

`_smoothed_obi()` docstring says `"3-snap trailing average of OBI (15-min smoothing)"` — this was accurate at 5-min cadence. At 1-min cadence it's misleading. Must be updated regardless of what window you choose.

### 🟡 PCR-OBI-3: `_smoothed_obi()` Still Filters Only `TIER1`

```sql
AND expiry_tier='TIER1'
```

The main `get_pcr()` function now queries both `TIER1` and `TIER2` and selects the primary tier dynamically. But `_smoothed_obi()` always uses `TIER1` — on expiry day when `tier_used = 'TIER2'`, the `smoothed_obi` is computed from TIER1 OBI while the PCR divergence signal is from TIER2. Conceptually inconsistent: the OBI should match the same expiry tier as the primary divergence signal.

***

## Commit Plan

### Commit 1 — Add `PCR_OBI_SMOOTH_SNAPS` to `config.py`

**Files:** `optdash/config.py` only

Add after `PCR_TREND_SNAPS`:
```python
# PCR smoothed_obi trailing window (snaps).
# At 1-min cadence: 10 snaps = 10-min smoothing (was 3-snap/15-min at 5-min cadence).
# Increasing this smooths out single-strike L1 depth noise at the cost of
# slightly delayed OBI reversals. 10 is a practical midpoint.
PCR_OBI_SMOOTH_SNAPS: int = 10
```

**Commit message:**
```
config: add PCR_OBI_SMOOTH_SNAPS=10 for 10-min OBI smoothing at 1-min cadence

At 5-min cadence, LIMIT 3 = 15-min window.
At 1-min cadence, LIMIT 3 = 3-min window — effectively no smoothing.
New config key allows tuning without code change.
```

***

### Commit 2 — Fix `_smoothed_obi()` and Series SQL

**Files:** `optdash/analytics/pcr.py`

**Change 1 — `_smoothed_obi()`: window from config, tier parameter, updated docstring:**

```python
def _smoothed_obi(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
    tier:       str = "TIER1",
) -> float:
    """Trailing average of per-snap L1 OBI over PCR_OBI_SMOOTH_SNAPS snaps.

    Window: settings.PCR_OBI_SMOOTH_SNAPS (default 10 snaps = 10 min at 1-min cadence).
    Source: bid1_qty/ask1_qty (instantaneous L1 depth, not cumulative day flow).
    Tier: matches the primary PCR tier (TIER1 normally, TIER2 on expiry day).
    COALESCE guards handle NULL depth on far-OTM/illiquid strikes.
    """
    try:
        limit = settings.PCR_OBI_SMOOTH_SNAPS
        rows = conn.execute("""
            SELECT
                (SUM(COALESCE(bid1_qty,0)) - SUM(COALESCE(ask1_qty,0))) /
                NULLIF(SUM(COALESCE(bid1_qty,0) + COALESCE(ask1_qty,0)), 0) AS obi
            FROM options_data
            WHERE trade_date=? AND underlying=? AND snap_time <= ?
              AND expiry_tier=?
            GROUP BY snap_time
            ORDER BY snap_time DESC
            LIMIT ?
        """, [trade_date, underlying, snap_time, tier, limit]).fetchall()
        if not rows:
            return 0.0
        return sum(r[0] or 0 for r in rows) / len(rows)
    except Exception:
        return 0.0
```

**Change 2 — `get_pcr()`: pass `tier_used` to `_smoothed_obi()`:**

```python
# Replace:
obi = _smoothed_obi(conn, trade_date, snap_time, underlying)

# With:
obi = _smoothed_obi(conn, trade_date, snap_time, underlying, tier=tier_used)
```

**Change 3 — `get_pcr_series()` inner SQL: replace hardcoded `2 PRECEDING` with config value:**

```python
w_obi = settings.PCR_OBI_SMOOTH_SNAPS - 1   # add this line near w_z and w_t

# In f-string SQL, replace:
AVG(obi) OVER (
    ORDER BY snap_time
    ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
) AS smoothed_obi

# With:
AVG(obi) OVER (
    ORDER BY snap_time
    ROWS BETWEEN {w_obi} PRECEDING AND CURRENT ROW
) AS smoothed_obi
```

**Change 4 — `get_pcr_series()` inner sub-query OBI: also apply tier filter:**

The series sub-query currently computes `obi` across `TIER1 + TIER2` combined:
```sql
WHERE trade_date=? AND underlying=? AND expiry_tier IN ('TIER1', 'TIER2')
-- This means OBI is a blended T1+T2 value in the series
```

Since in the series you don't know `tier_used` per row until after you compute `dte_t1`, the cleanest fix is to **compute both tier OBIs in the sub-query** and select the right one in Python — same pattern already used for `div_mean`/`div_std`:

```sql
-- In sub-query SELECT, replace the single obi with:
(SUM(CASE WHEN expiry_tier='TIER1' THEN COALESCE(bid1_qty,0) ELSE 0 END) -
 SUM(CASE WHEN expiry_tier='TIER1' THEN COALESCE(ask1_qty,0) ELSE 0 END)) /
NULLIF(SUM(CASE WHEN expiry_tier='TIER1' THEN COALESCE(bid1_qty,0)+COALESCE(ask1_qty,0) ELSE 0 END), 0) AS obi_t1,

(SUM(CASE WHEN expiry_tier='TIER2' THEN COALESCE(bid1_qty,0) ELSE 0 END) -
 SUM(CASE WHEN expiry_tier='TIER2' THEN COALESCE(ask1_qty,0) ELSE 0 END)) /
NULLIF(SUM(CASE WHEN expiry_tier='TIER2' THEN COALESCE(bid1_qty,0)+COALESCE(ask1_qty,0) ELSE 0 END), 0) AS obi_t2
```

Then in the outer window:
```sql
AVG(obi_t1) OVER (ORDER BY snap_time ROWS BETWEEN {w_obi} PRECEDING AND CURRENT ROW) AS smoothed_obi_t1,
AVG(obi_t2) OVER (ORDER BY snap_time ROWS BETWEEN {w_obi} PRECEDING AND CURRENT ROW) AS smoothed_obi_t2,
```

In Python: `smoothed_obi = smoothed_obi_t2 if use_tier2 else smoothed_obi_t1`

**Commit message:**
```
fix(pcr): PCR-OBI-1/2/3 — fix smoothed_obi window, tier alignment, and stale docstring

PCR_OBI_SMOOTH_SNAPS=10 applied to both _smoothed_obi() (live tile)
and get_pcr_series() window function — series and tile now use
identical smoothing (was hardcoded LIMIT 3 / ROWS BETWEEN 2 in both).

_smoothed_obi() now accepts tier= param; get_pcr() passes tier_used
so OBI tier matches primary PCR divergence tier (TIER1 normally,
TIER2 on expiry day).

get_pcr_series() computes obi_t1 and obi_t2 separately in sub-query
and selects per-row based on dte_t1 <= 1 condition.

Docstring updated: "15-min smoothing" → "10-min smoothing at 1-min cadence".
```

***

## Summary

| | Before | After |
|---|---|---|
| OBI source | `bid_qty`/`ask_qty` cumulative | `bid1_qty`/`ask1_qty` L1 ✅ already done |
| Smoothing window (live) | `LIMIT 3` = 3 min | `LIMIT PCR_OBI_SMOOTH_SNAPS` = 10 min |
| Smoothing window (series) | hardcoded `ROWS BETWEEN 2 PRECEDING` | `ROWS BETWEEN {w_obi} PRECEDING` = 10 min |
| Tier alignment | always TIER1 | matches `tier_used` (TIER1 or TIER2) |
| Live tile vs series consistency | both 3 snaps (matched) | both 10 snaps (matched via shared config key) |