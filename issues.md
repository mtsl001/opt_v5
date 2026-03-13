# OptDash — Code Review Report (origin/main @ 06903ed)

> Full-stack code review of the current `origin/main` branch.
> Each issue verified line-by-line against source. No test-coverage items.
> Scope: backend Python + frontend React. Personal-use deployment (no auth/rate-limit issues).

---

## Part A — User's 10 Commits Review

The user pushed 10 commits (a805138..06903ed) addressing prior findings. All changes are **well-structured, correctly annotated, and properly implemented**:

| Commit | Fix ID | Assessment |
|---|---|---|
| `22cd300` | P2-D: missing `idx_shadow_snaps_shadow_id` migration | ✅ Correct |
| `f0f68fd` | H-2: `final_pnl_abs` added to `shadow_trades` schema + migration | ✅ Correct |
| `868b454` | H-2: `final_pnl_abs` in `shadow.py` DAO `_ALLOWED_SHADOW_COLS` + `close_shadow` | ✅ Correct — uses `data.get("final_pnl_abs")` for None-safety |
| `d83f04e` | H-3: batch commits in `expire_stale_recommendations` (N→1 WAL flush) | ✅ Correct — atomic with summary log |
| `0c4767e` | M-1: remove bare except from `_nearest_expiry` | ✅ Correct — caller gets `exc_info=True` |
| `c832c99` | M-2 + M-3: validate `done_flags` keys; upgrade `gate_cache` to `logger.error` | ✅ Correct — `date.fromisoformat()` validation, `"error"` key in fallback dict |
| `41f112d` | M-4 + M-5: `DEALER_OCLOCK_START` comment; `WS_INTERVAL_SECONDS` cross-validator | ✅ Correct — prevents asyncio tight-loop + stale WS |
| `a4c3b40` | H-2b: intraday shadow close now computes `pnl_abs` | ✅ Correct — matches `finalize_all_shadows` formula |
| `06903ed` | H-2b: `pnl_abs` in `shadow_tracker` log + H-3 expiry summary | ✅ Correct |

**Notable additions in user's commits:**
- `processor.py`: P0-A (refresh DuckDB once per trade_date per batch) and P0-B (`dte > 0` for near-month futures selection — prevents rollover-day settlement price corruption). Both are **correct and important fixes**.
- `vex_cex.py`: Enhanced unit docs for VEX/CEX scaling (stored as Rs M in Parquet, SUM() directly in SQL — no double-scaling).
- `trades.py`: Removed phantom `recommendation_snap_time` from `_ALLOWED_TRADE_COLS` (N-3 fix — column doesn't exist in schema).

**Verdict: All 10 commits are clean. No regressions introduced.**

---

## Part B — Remaining Issues (New Findings)

---

## Issue Index

| # | Severity | Module | Title |
|---|---|---|---|
| 1 | 🟠 P1 | `tracker.py` | Trailing stop `0.90` multiplier still hardcoded |
| 2 | 🟠 P1 | `tracker.py` | `_snaps_since` still hardcodes `// 5` |
| 3 | 🟡 P2 | `scheduler.py` | `_today_str()` still uses system-local `date.today()` |
| 4 | 🟡 P2 | `ai.py` + `validators.py` | Duplicate `SnapTime` type definition |
| 5 | 🟡 P2 | `pnl.py` + `environment.py` | Duplicate `_snap_to_min` helper |
| 6 | 🟡 P2 | `environment.py` | Gate C4 (PCR divergence) hardcodes `> 0.15` threshold |
| 7 | 🟡 P2 | `environment.py` | C9 `vex_aligned` awards 2 pts — docs and frontend expect 1 |
| 8 | 🟡 P2 | `gex.py` | `pct_of_peak` uses `abs(gex_all)` — loses sign information |
| 9 | 🟡 P2 | `coc.py` | `_compute_vcoc_from_series` lookback rounds down at non-divisible intervals |
| 10 | 🟡 P2 | Frontend | `EnvironmentPanel` condition labels don't match backend keys |
| 11 | 🔵 P3 | `tracker.py` | Gate-cache error fallback still triggers `GATE_NO_GO` exits |
| 12 | 🔵 P3 | `config.py` | Missing `TRAILING_STOP_TRAIL_PCT` config entry |
| 13 | 🔵 P3 | `iv.py` | `atm_iv` falsy guard catches `0.0` as missing |

---

## 🟠 P1 — High

### Issue 1: `tracker.py` — Trailing stop `0.90` multiplier still hardcoded

**File:** `optdash/ai/tracker.py` — line 89

```python
dynamic_trail = peak_ltp * 0.90
```

The trailing stop trail width (10% below peak) is hardcoded. `TRAILING_STOP_ACTIVATION` (0.20) is configurable in `config.py`, but the actual trail percentage has no config entry. Changing the trail width requires a code change + redeploy.

**Impact:** Cannot tune trailing stop decay via `.env`. Forces redeploy for every trail-width experiment.

**Fix:**
1. Add to `config.py`: `TRAILING_STOP_TRAIL_PCT: float = 0.10`
2. Update `tracker.py`: `dynamic_trail = peak_ltp * (1.0 - settings.TRAILING_STOP_TRAIL_PCT)`

---

### Issue 2: `tracker.py` — `_snaps_since` still hardcodes `// 5`

**File:** `optdash/ai/tracker.py` — line 286 (approx)

```python
def _snaps_since(entry_snap: str, current_snap: str) -> int:
    return _minutes_since_entry(entry_snap, current_snap) // 5
```

The `5` assumes `SCHEDULER_INTERVAL_SECONDS = 300`. Note that Issue 5 (the same hardcoded interval in `direction.py::_is_vcoc_spike_active`) **was fixed by the user** — but the identical pattern in `tracker.py::_snaps_since` **was not**.

At `SCHEDULER_INTERVAL_SECONDS = 600`:
- `_snaps_since` returns 2× the actual snap count
- `expire_stale_recommendations` expires trades prematurely (at half the intended time)
- `_consecutive_no_go_count` uses the snap count differently (queries DB directly), so it is NOT affected

**Fix:**
```python
def _snaps_since(entry_snap: str, current_snap: str) -> int:
    interval = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    return _minutes_since_entry(entry_snap, current_snap) // interval
```

---

## 🟡 P2 — Medium

### Issue 3: `scheduler.py` — `_today_str()` uses `date.today()` not IST-aware

**File:** `optdash/scheduler.py` — lines 82-83

```python
def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")
```

Uses system timezone, not IST. `_now_ist()` and `_snap_time_str()` correctly use IST (`Asia/Kolkata`). If deployed on a UTC server, `date.today()` returns yesterday's date during 00:00–05:30 UTC (05:30–11:00 IST — morning trading hours), causing all DuckDB queries to hit yesterday's partition.

**Current deployment:** IST machine — no bug. Latent bug for cloud deployment.

**Fix:** `return _now_ist().date().strftime("%Y-%m-%d")`

---

### Issue 4: `ai.py` + `validators.py` — Duplicate `SnapTime` type

**Files:** `optdash/api/routers/ai.py` lines ~22-28, `optdash/api/validators.py` lines ~34-40

`SnapTime` is defined identically in both files. `validators.py` module docstring acknowledges this as a known cleanup item. Divergence risk if regex is updated in one but not the other.

**Fix:** In `ai.py`, replace inline definition with `from optdash.api.validators import SnapTime`.

---

### Issue 5: `pnl.py` + `environment.py` — Duplicate `_snap_to_min` helper

Two independent definitions of `_snap_to_min(t: str) -> int` with slightly different error handling:
- `pnl.py`: uses `t[:5]` slicing + try/except fallback
- `environment.py`: bare split, no fallback

**Fix:** Extract to `optdash/utils.py`:
```python
def snap_to_min(t: str) -> int:
    h, m = map(int, t[:5].split(":"))
    return h * 60 + m
```

---

### Issue 6: `environment.py` — Gate C4 hardcodes `abs(pcr_div) > 0.15`

**File:** `optdash/analytics/environment.py`

```python
c4_met = abs(pcr_div) > 0.15
```

All other gate conditions use `settings.*` thresholds, but C4 hardcodes `0.15`. The direction module correctly uses `settings.PCR_DIV_BULL_THRESHOLD` and `settings.PCR_DIV_BEAR_THRESHOLD`. C4 should use the same thresholds for consistency.

**Fix:**
```python
c4_met = pcr_div > settings.PCR_DIV_BULL_THRESHOLD or pcr_div < settings.PCR_DIV_BEAR_THRESHOLD
```

---

### Issue 7: `environment.py` — C9 `vex_aligned` awards 2 pts

**File:** `optdash/analytics/environment.py`

```python
conditions["vex_aligned"] = {
    ...
    "points": 2, "note": "VEX mechanical alignment ** (2 pts)"
}
```

The gate has 10 conditions (C1-C8 = 1pt each = 8 pts, C9 = 2pts = 10 pts, C10 = 1pt = 11 pts total = `GATE_MAX_SCORE`). This is internally consistent. However:

1. The updated documentation (Part7_Environment_Gate.md) describes 11 conditions with a "max 2 pts" bonus section — implying C10 and C11 are both bonus conditions. The actual code has only **10 conditions** (C1–C10) with C9 being a 2-point condition.
2. The frontend `EnvironmentPanel` renders conditions generically but shows `max_score` from the API. The mismatch between docs (11 conditions) and code (10 conditions) can confuse future development.

**Fix:** Update Part7 documentation to match code (10 conditions, C9=2pts), or split C9 into two 1-point conditions if 11 conditions is the desired design.

---

### Issue 8: `gex.py` — `pct_of_peak` uses absolute value

**File:** `optdash/analytics/gex.py`

```python
pct = (abs(gex_all) / peak * 100) if peak != 0 else 0.0
```

`pct_of_peak` is always positive regardless of GEX sign. This is correct for the regime classifier (`_classify_regime` checks `gex < 0` separately), but the `GEX_DECLINE_THRESHOLD` comparison in `environment.py` C1:

```python
c1_met = gex_pct <= settings.GEX_DECLINE_THRESHOLD * 100
```

treats a negative GEX day (gex_all = -2B, peak = 5B → pct = 40%) the same as a declining positive day (gex_all = 2B, peak = 5B → pct = 40%). This is by design (both represent weakened gamma), but the environment gate C1 key `gex_declining` is misleading when GEX is actually negative (it's not "declining" — it's actively negative).

**Impact:** Cosmetic/documentation. The logic produces correct trading signals.

---

### Issue 9: `coc.py` — Non-divisible interval lookback

**File:** `optdash/analytics/coc.py`

```python
lookback = max(1, 15 // interval)
```

At `SCHEDULER_INTERVAL_SECONDS = 420` (7-minute ticks), `interval = 7`, `lookback = 15 // 7 = 2` — actual window = 14 min (not 15). At 8-minute ticks: `15 // 8 = 1` — actual window = 8 min. The rounding error is minor for the charting endpoint but could be addressed with `round(15 / interval)` instead of integer division.

**Impact:** Low. Only affects the charting series, not the live V_CoC used in recommendations (which uses wall-clock window).

---

### Issue 10: Frontend — `EnvironmentPanel` condition labels mismatch

**File:** `frontend/src/components/panels/EnvironmentPanel.tsx`

```tsx
const CONDITION_LABELS: Record<string, string> = {
  trend_bullish:   'Trend Bullish',  trend_bearish:  'Trend Bearish',
  gex_positive:    'GEX Regime',     coc_bullish:    'CoC Bullish',
  coc_bearish:     'CoC Bearish',    pcr_favorable:  'PCR Favorable',
  iv_normal:       'IV Normal',      volume_ok:      'Volume OK',
  no_spike:        'No Spike',       direction_conf: 'Dir Confidence',
  theta_burn:      'Theta Burn OK',
}
```

The backend returns condition keys: `gex_declining`, `vcoc_signal`, `fut_bs_ratio`, `pcr_divergence`, `ivp_cheap`, `obi_negative`, `term_structure_ok`, `session_ok`, `vex_aligned`, `not_charm_distortion`. **None of the frontend label keys match.** The panel falls back to displaying raw keys (`CONDITION_LABELS[key] ?? key`), so the UI shows `gex_declining` instead of a human-readable label.

**Fix:** Update `CONDITION_LABELS` to match actual backend keys:
```tsx
const CONDITION_LABELS: Record<string, string> = {
  gex_declining:       'GEX Declining',
  vcoc_signal:         'V_CoC Signal',
  fut_bs_ratio:        'Futures Flow',
  pcr_divergence:      'PCR Divergence',
  ivp_cheap:           'IV Cheap',
  obi_negative:        'ATM OBI',
  term_structure_ok:   'Term Structure',
  session_ok:          'Session',
  vex_aligned:         'VEX Aligned',
  not_charm_distortion:'No Dealer O\'Clock',
}
```

---

## 🔵 P3 — Low

### Issue 11: `tracker.py` — Gate-cache error fallback still triggers exits

Although the user's M-3 fix correctly logs a warning when a gate_cache entry is an error fallback, the code **does not skip** the consecutive-NO_GO counter for that position:

```python
if gate.get("error"):
    logger.warning(...)
    # ← continues to use gate["verdict"] = "NO_GO" for snap recording
```

A DuckDB crash lasting 2+ ticks (10 min) would trigger `GATE_NO_GO` exits on all open positions — not because the environment is hostile, but because the gate computation failed.

**Fix:** When `gate.get("error")` is truthy, either:
1. Skip the NO_GO counter increment for that snap, or
2. Set `gate_verdict` to `"ERROR"` (a non-NO_GO value) in the snap record

---

### Issue 12: `config.py` — Missing `TRAILING_STOP_TRAIL_PCT` config entry

Companion to Issue 1. The config has `TRAILING_STOP_ACTIVATION` but no `TRAILING_STOP_TRAIL_PCT`. The 10% trail width is hardcoded in `tracker.py` as `* 0.90`.

---

### Issue 13: `iv.py` — `atm_iv` falsy guard catches `0.0`

**File:** `optdash/analytics/iv.py`

```python
atm_iv = cur[0] if cur else None
if not atm_iv:        # ← 0.0 is falsy!
    return {}
```

Same class of bug as the old `_classify_shape` issue (which was correctly fixed to use `is None`). If ATM IV is exactly 0.0 (deeply OTM, worthless option near expiry), the function returns `{}` instead of computing IVR/IVP.

**Practical likelihood:** Very low for ATM options on NIFTY/BANKNIFTY. But the fix is trivial:
```python
if atm_iv is None:
    return {}
```

---

## Previously Reported Issues — Now Fixed ✅

| Original # | Title | Status |
|---|---|---|
| 1 | `deps.py` missing `busy_timeout` | ✅ Fixed — now delegates to `open_journal()` |
| 4 | `iv.py` `_classify_shape` falsy check | ✅ Fixed — uses `is None` |
| 5 | `direction.py` hardcoded 5-min interval | ✅ Fixed — uses `settings.SCHEDULER_INTERVAL_SECONDS // 60` |
| 7 | `deps.py` duplicates `open_journal()` | ✅ Fixed — single factory |
| 10 | `coc.py` hardcoded 3-row lookback | ✅ Fixed — uses `15 // interval` |
| 11 | Exception swallowing hides failures | ✅ Fixed — `metrics.py` + `/health` endpoint |
| — | Shadow `final_pnl_abs` missing | ✅ Fixed by user (H-2 commits) |
| — | `done_flags` lost on restart | ✅ Fixed by user (P1-B) |
| — | `_nearest_expiry` bare except | ✅ Fixed by user (M-1) |
| — | Processor rollover-day settlement | ✅ Fixed by user (P0-B) |
| — | Phantom `recommendation_snap_time` | ✅ Fixed by user (N-3) |

---

## Summary

| Severity | Count | Effort |
|---|---|---|
| 🟠 P1 | 2 | ~15 min |
| 🟡 P2 | 7 | ~1 hour |
| 🔵 P3 | 3 | ~30 min |
| **Total** | **12** | **~1.75 hours** |

The codebase is in **good shape overall**. The user's 10 commits address real issues correctly. The remaining findings are mostly configuration-vs-hardcoded consistency (P1-1/2), documentation-code alignment (P2-7), and a frontend label mismatch (P2-10 — the most visible issue to end users).
