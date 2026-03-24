Now I have a complete picture of the current `main` branch. Here is the thorough final review.

***

## Full Issue Resolution Review — `main` branch (HEAD: [`1a617c7`](https://github.com/mtsl001/opt_v5/commit/1a617c7e44aaa0bffcad6bdf2b91417372e004e6))

3 new commits landed since the last review: **Pass 5** (all 8 open issues), **Pass 5 Addendum** (Issue H), and **Pass 6** (ZGL/Skew alert transition guards + startup delta-range validator). 

***

## ✅ All Previously Open Issues — Now Confirmed Fixed

| Issue | File | Verification |
|-------|------|-------------|
| **A** — SQL injection via `direction` | `screener.py` | `if direction not in (None, "CE", "PE"): raise ValueError(...)` added before any SQL execution  |
| **B** — Bare `raise` crashes API | `screener.py` | `except` block now calls `record_error("get_strikes")` and `return []` — no re-raise  |
| **C** — Delta denominator div-by-zero | `screener.py` | Python-level guard: `if settings.SCREENER_MAX_DELTA == settings.SCREENER_MIN_DELTA: raise ValueError(...)` **and** SQL uses `NULLIF(? - ?, 0)` as double protection  |
| **D** — Full-day series re-scanned on every tick | `alerts.py` | `since_snap` cutoff computed and passed to all 4 series functions (`gex_series`, `coc_series`, `pcr_series`, `vol_series`)  |
| **E** — Alert dedup allows same-type flooding | `alerts.py` | `k = a["type"]` — dedup is now per-type only, not per `(type, time)`  |
| **F** — `session_adjusted` flag inconsistent with reason | `confidence.py` | `is_adjusted = raw != raw_pre_session` extracted; return dict uses `"session_adjusted_reason": session_adjusted_reason if is_adjusted else None`  |
| **G** — Upper-biased median in `microstructure.py` | `microstructure.py` | True median: `sw = sorted(window); n = len(sw); baseline = (sw[n//2-1] + sw[n//2]) / 2.0 if n % 2 == 0 else sw[n//2]`  |
| **H** — Exception fallback missing keys in `direction.py` | `direction.py` | Return dict now includes `"vex_data": {}, "unique_source_count": 0, "conviction": "NEUTRAL", "pcr_modifier": 1.0, "veto": None`  |
| #5 — `tracker.py` lot normalization missing | `tracker.py` | Comment in Pass 5 commit message says "Tracker Lot Normalization Guard (#5)" — however `tracker.py` SHA is **unchanged** (`0ec9c5bf`) from the previous review  |

***

## 🔴 One Remaining Issue — `tracker.py` #5 Not Actually Fixed

Despite the Pass 5 commit message claiming "Tracker Lot Normalization Guard (#5)", the `tracker.py` file **SHA is identical** to the previous review (`0ec9c5bf2a80afb23f8fa47c84764f3373377cf9`).  The existing code already normalizes `pnl_abs` with the lot size:

```python
lot     = settings.LOT_SIZES.get(underlying, 1)
pnl_abs = round((ltp - entry) * lot, 2)
```

The original concern was that `final_pnl_abs` in `close_trade()` is stored as `pnl_abs` (already lot-normalized), but **aggregation queries across underlyings** in the learning/journal layer may still compare raw `pnl_abs` values without normalizing to a common unit (e.g. per-lot or per-crore notional). The tracker itself is fine — the risk is downstream in reporting/analytics queries that sum or average `final_pnl_abs` across `NIFTY` (lot=25) vs `BANKNIFTY` (lot=15) vs `FINNIFTY` (lot=40) positions without weighting. This was the original issue #5, and the fix needs to be in those query/reporting layers, not just a comment in the commit message.

***

## 🟡 2 New Issues Found in Pass 6

### New Issue I — `alerts.py`: Extra `get_net_gex` + `get_vex_cex_current` calls for prev-snap transition guards
Pass 6 added prev-snap data fetching for ZGL and Skew/VEX transition guards: 

```python
prev_gex_data  = get_net_gex(conn, trade_date, prev_gex_snap, underlying) if prev_gex_snap else None
prev_skew_data = get_iv_skew(conn, trade_date, prev_gex_snap, underlying) if prev_gex_snap else None
prev_vex_data  = get_vex_cex_current(conn, trade_date, prev_gex_snap, underlying) if prev_gex_snap else None
```

This adds **3 new DuckDB round-trips per tick per underlying** (up to 9 extra queries/tick for 3 underlyings), partially offsetting the `since_snap` savings from Issue D. These should be consolidated: `prev_gex_data` can serve double-duty for ZGL proximity check (already done), but `prev_skew_data` and `prev_vex_data` are only used when `prev_gex_snap` is available. Consider caching or batching these into a single multi-series `prev_snap` fetch.

### New Issue J — `alerts.py`: `since_snap` cutoff edge case at session open
```python
h, m = map(int, snap_time[:5].split(':'))
total_m = max(0, h * 60 + m - (lookback_snaps + 5))
since_snap = f"{total_m // 60:02d}:{total_m % 60:02d}"
```
 At `snap_time = "09:15"` (session open), `total_m = max(0, 555 - 17) = 538`, which gives `since_snap = "08:58"` — a pre-market time. All series functions will query for `snap_time >= "08:58"` and find either nothing (correct) or any pre-market data if it exists (potential false alerts). The inner `try/except` swallows errors here and falls back to `since_snap = None` on any exception, which then reverts to a full-day scan for that tick. The guard should be:
```python
since_snap = max(since_snap, "09:15")  # clamp to market open
```

***

## Final Summary

| Status | Count | Items |
|--------|-------|-------|
| ✅ Fully fixed and verified | 25 | All issues A–H + all original issues 1–4, 6–20 |
| 🔴 Open — fix pending | 1 | #5 — cross-underlying `pnl_abs` aggregation normalization in reporting layer (tracker.py itself is fine) |
| 🟡 New issues introduced in Pass 6 | 2 | I — 3 extra DuckDB round-trips per tick for prev-snap guards; J — `since_snap` not clamped to market open `"09:15"` |

The codebase is in very good shape. The only hard requirement before production is resolving issue #5 in the **reporting/learning aggregation queries**, and clamping `since_snap` to `"09:15"` (Issue J) to prevent potential false alert triggers at session open.