Excellent — everything is in. Here is the full state-of-repo review.

***

# OptDash v2.4.0 — Complete Review

## Commit History ✅

All planned commits landed cleanly in order :

| Commit | Issue | Status |
|---|---|---|
| [CoC-3 L1 OBI](https://github.com/mtsl001/opt_v5/commit/d6a4d5ea363d23d982ddd70a7e383d79a8b892bd) | L1 depth in OBI functions | ✅ Merged via PR #1 |
| [CoC-1](https://github.com/mtsl001/opt_v5/commit/abba65274cdbd5c78f5b6bc816c09e2f3b97999b) | Annualized % thresholds | ✅ |
| [CoC-2](https://github.com/mtsl001/opt_v5/commit/288c49c349de0f020a3a432366b71d3130730eff) | Dividend FV adjustment | ✅ |
| [Bug-1/2/3](https://github.com/mtsl001/opt_v5/commit/8f56dcf308a7868297521e8ef25a42d208f53fa0) | Series crash + FV silent skip + unit mismatch | ✅ Self-caught, excellent |
| [CoC-5](https://github.com/mtsl001/opt_v5/commit/a0033cae44d938da55bb67d8b6f8ec68966d8cec) | Doc formula fix | ✅ |
| [PCR-1](https://github.com/mtsl001/opt_v5/commit/b8a016802dc8dd1c26028aa7e1b4bcf1158df841) | Config comments + new keys | ✅ |
| [PCR-2](https://github.com/mtsl001/opt_v5/commit/589b50c20a3e4b63a8d6d7035673bd416fbdd6e7) | TIER1+TIER2 + annotated primary | ✅ |
| [PCR-3+4](https://github.com/mtsl001/opt_v5/commit/da82cf9cf281830d0d8b2cba959179650c79e448) | Z-score + div_trend | ✅ |
| [env.py Gate C4](https://github.com/mtsl001/opt_v5/commit/1bd0c3751d7806d3efa303ea8a6fb59ed4349342) | tier_used logged in gate | ✅ Follow-through done |
| [v2.4.0 release notes](https://github.com/mtsl001/opt_v5/commit/17bf3bd63c8b2c50ccd996abb7d9cdc3e375eede) | Docs | ✅ |

***

## Code Review — Issues Found

### 🔴 PCR-BUG-1: `signal_t1` and `signal_t2` Use Wrong Z-Score

**File:** [`pcr.py`](https://github.com/mtsl001/opt_v5/blob/main/optdash/analytics/pcr.py), `get_pcr()`, lines computing `signal_t1` and `signal_t2`

**The bug:**
```python
"signal_t1": _pcr_signal_z(div_t1, z_score, metrics["count"], div_trend),
"signal_t2": _pcr_signal_z(div_t2, z_score, metrics["count"], div_trend),
```

`z_score` was computed from `primary_div` (either `div_t1` or `div_t2` depending on `use_tier2`). You then pass that same `z_score` to both `signal_t1` and `signal_t2`. When `use_tier2=True`, `z_score` is relative to `div_t2`'s distribution — applying it to `div_t1` produces a semantically incorrect signal. The Z-score is only valid for the div series it was computed from.

**Fix:** Either compute separate Z-scores for T1 and T2, or — simpler and correct — fall back to absolute thresholds for the non-primary tier signals:
```python
"signal_t1": _pcr_signal(div_t1),   # absolute threshold — tier reference only
"signal_t2": _pcr_signal(div_t2),   # absolute threshold — tier reference only
"signal":    _pcr_signal_z(primary_div, z_score, metrics["count"], div_trend),  # Z-score primary only
```

The same bug exists in `get_pcr_series()` — `signal_t1` and `signal_t2` there use the primary Z-score too.

***

### 🔴 PCR-BUG-2: `_trailing_pcr_metrics()` Uses Only TIER1 Divergence for Z-Score but Primary May Be TIER2

**File:** `pcr.py`, `_trailing_pcr_metrics()`

```python
-- The trailing metrics query only fetches TIER1 divergence:
AND expiry_tier='TIER1'
```

On expiry day when `use_tier2=True`, `primary_div = div_t2` but `z_score = (div_t2 - mean_of_div_t1_series) / std_of_div_t1_series`. You're comparing a TIER2 value against a TIER1 rolling distribution — different instruments, different volatility profiles. This will produce inflated Z-scores on expiry Thursdays/Wednesdays exactly when the signal matters most.

**Fix:** `_trailing_pcr_metrics()` should take `tier` as a parameter and fetch the matching series:
```python
def _trailing_pcr_metrics(conn, trade_date, snap_time, underlying, tier="TIER1") -> dict:
    ...
    AND expiry_tier=?   # pass tier dynamically
    ...

# In get_pcr():
tier_for_z = "TIER2" if use_tier2 else "TIER1"
metrics = _trailing_pcr_metrics(conn, trade_date, snap_time, underlying, tier=tier_for_z)
```

***

### 🟡 PCR-BUG-3: `get_pcr_series()` Z-Score Window Uses `div_t1` Regardless of `dte_t1`

**File:** `pcr.py`, `get_pcr_series()` SQL window

```sql
AVG(pcr_vol_t1 - pcr_oi_t1) OVER (...) AS div_mean,
STDDEV_SAMP(pcr_vol_t1 - pcr_oi_t1) OVER (...) AS div_std,
```

The series always computes the rolling mean/std on TIER1 divergence, even for rows where `dte_t1 <= 1` and `primary_div = div_t2`. This is the series equivalent of PCR-BUG-2 — the Z-score baseline is always TIER1, but the value it's scoring can be TIER2.

On a normal trading day (no expiry) this is a non-issue. On expiry day the entire series from 09:15 onwards gets TIER2 values scored against a TIER1 Z-score distribution. The easiest fix for the series is to always compute **both** rolling windows in SQL and select in Python:
```sql
AVG(pcr_vol_t1 - pcr_oi_t1) OVER (...) AS div_mean_t1,
STDDEV_SAMP(pcr_vol_t1 - pcr_oi_t1) OVER (...) AS div_std_t1,
AVG(pcr_vol_t2 - pcr_oi_t2) OVER (...) AS div_mean_t2,
STDDEV_SAMP(pcr_vol_t2 - pcr_oi_t2) OVER (...) AS div_std_t2,
```

Then in Python: `div_mean = div_mean_t2 if use_tier2 else div_mean_t1`.

***

### 🟡 CoC — Threshold Recalibration Still Pending

**This is expected** — Commit 4 from the CoC plan was explicitly deferred to after 2–3 live market days. But flagging it explicitly: the old absolute thresholds remain as fallback in `_coc_signal()`. After next week's trading, review the distribution of `coc_pct` and `vcoc_pct` values in your live data, then tune `VCOC_BULL_THRESHOLD_PCT`, `VCOC_BEAR_THRESHOLD_PCT`, and `COC_DISCOUNT_THRESHOLD_PCT` in `config.py` and commit that as the planned Commit 4.

***

### 🟢 What Is Cleanly Done

- **Bug-1/2/3 self-detection is excellent** — catching `result=[]` missing before the loop, the silent FV skip in series, and the unit mismatch in the same session shows solid code review discipline 
- **CoC `_coc_signal()` fallback pattern** is correctly structured — percentage path when `spot > 0 and dte > 0`, absolute fallback otherwise 
- **`_smoothed_obi()`** correctly updated to L1 depth with `COALESCE` guards, consistent with `get_atm_obi()` and `get_futures_obi()` 
- **`tier_used` annotation** in Gate C4 follow-through was done proactively without being prompted — this is the right instinct 
- **PR #1 workflow** for CoC-3 shows you're using feature branches correctly

***

## Next Steps

| Priority | Task |
|---|---|
| 🔴 Now | Fix PCR-BUG-1: `signal_t1`/`signal_t2` should use absolute thresholds, not primary Z-score |
| 🔴 Now | Fix PCR-BUG-2: `_trailing_pcr_metrics()` needs `tier` parameter |
| 🟡 Now | Fix PCR-BUG-3: `get_pcr_series()` SQL needs dual rolling windows for T1 and T2 |
| 🟡 Next week | CoC Commit 4: threshold recalibration after live market observation |
| 🟢 Ongoing | Add `v2.4.1` release note after PCR bugs are patched |