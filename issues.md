# OptDash — Verified Issues & Fixes

> Full-stack code review findings.
> Each issue has been verified line-by-line against source code.
> Scoped to personal-use deployment (no auth/rate-limiting issues). No test-coverage items.

---

## Issue Index

| # | Severity | Module | Title |
|---|----------|--------|-------|
| 1 | 🔴 P0 | `deps.py` | Missing `busy_timeout` PRAGMA — SQLITE_BUSY on contention |
| 2 | 🟠 P1 | `tracker.py` | Trailing stop `0.90` multiplier is hardcoded |
| 3 | 🟠 P1 | `tracker.py` | `_snaps_since` hardcodes `// 5` instead of using config |
| 4 | 🟠 P1 | `iv.py` | `_classify_shape` uses falsy check — `0.0` treated as missing |
| 5 | 🟠 P1 | `direction.py` | `_is_vcoc_spike_active` hardcodes `5`-minute snap interval |
| 6 | 🟡 P2 | `scheduler.py` | `_today_str()` uses `date.today()` not IST-aware |
| 7 | 🟡 P2 | `deps.py` | `_open_journal_conn` duplicates `open_journal()` from schema.py |
| 8 | 🟡 P2 | `ai.py` + `validators.py` | Duplicate `SnapTime` type definition |
| 9 | 🟡 P2 | `pnl.py` + `environment.py` | Duplicate `_snap_to_min` helper |
| 10 | 🟡 P2 | `coc.py` | `_compute_vcoc_from_series` hardcodes 3-row lookback |
| 11 | 🔵 P3 | All analytics | Exception swallowing hides failures silently |

---

## 🔴 P0 — Critical

### Issue 1: `deps.py` — Missing `busy_timeout` PRAGMA

**File:** `optdash/api/deps.py` — [lines 83–97](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py#L83-L97)

**What's wrong:**

`_open_journal_conn()` opens both the API and scheduler SQLite connections but does NOT set `PRAGMA busy_timeout`. Meanwhile, `schema.py::open_journal()` (the canonical connection factory described in the schema module docstring as "must be used for every journal connection") DOES set `PRAGMA busy_timeout=5000`.

```python
# deps.py _open_journal_conn (lines 83–97):
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA foreign_keys=ON")
# ← no busy_timeout

# schema.py open_journal (lines 247–252):
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA busy_timeout=5000")   # ← present here
```

**Impact:**

Without `busy_timeout`, when the scheduler writes position snaps (every 5 min) at the exact moment an API endpoint tries to accept/reject a trade, SQLite returns `SQLITE_BUSY` **immediately** instead of retrying for up to 5 seconds. WAL mode reduces lock contention significantly, but on heavy ticks (EOD sweep with multiple writes) the window of contention widens. The result is a `sqlite3.OperationalError: database is locked` bubbling up as a 500 to the API caller or a failed scheduler tick.

**How to fix:**

Replace the body of `_open_journal_conn` with a call to `open_journal()` from `schema.py`, then add the extra `PRAGMA synchronous=NORMAL` on top:

```python
from optdash.ai.journal.schema import open_journal

def _open_journal_conn(path: str) -> sqlite3.Connection:
    conn = open_journal(Path(path))
    conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, ~3x faster
    return conn
```

This ensures every connection consistently gets WAL + FK + busy_timeout, and any future PRAGMAs added to `open_journal()` automatically apply to all connections.

---

## 🟠 P1 — High

### Issue 2: `tracker.py` — Trailing stop `0.90` multiplier is hardcoded

**File:** `optdash/ai/tracker.py` — [line 89](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py#L89)

**What's wrong:**

```python
# line 89:
dynamic_trail = peak_ltp * 0.90
```

The trailing stop trail-down percentage (10%) is hardcoded as `0.90`. The codebase already has `TRAILING_STOP_ACTIVATION` (0.20) in config.py with proper validator, but the actual trail width has no config entry. This means changing the trailing stop percentage requires a code change and redeploy rather than an `.env` edit.

For context, the related config values are:
- `AI_SL_PCT = 0.35` (base SL, configurable, has validator) — `config.py` line 377
- `TRAILING_STOP_ACTIVATION = 0.20` (+20% PnL to activate trailing) — `config.py` line 395
- Trailing trail-down = 10% — **hardcoded**, no config

**Impact:**

Cannot tune the trailing stop decay without modifying code. In an iterative strategy development cycle, this forces a redeploy for every trail-width experiment.

**How to fix:**

1. Add to `config.py`:
```python
TRAILING_STOP_TRAIL_PCT: float = 0.10   # trail 10% below peak
```

2. Update `tracker.py` line 89:
```python
dynamic_trail = peak_ltp * (1.0 - settings.TRAILING_STOP_TRAIL_PCT)
```

---

### Issue 3: `tracker.py` — `_snaps_since` hardcodes `// 5`

**File:** `optdash/ai/tracker.py` — [line 286](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py#L286)

**What's wrong:**

```python
# line 286:
def _snaps_since(entry_snap: str, current_snap: str) -> int:
    return _minutes_since_entry(entry_snap, current_snap) // 5
```

The `5` assumes `SCHEDULER_INTERVAL_SECONDS = 300` (5 minutes). The scheduler already uses `settings.SCHEDULER_INTERVAL_SECONDS` in `_snap_time_str()` (line 96) and it's configurable in `config.py` (line 49). If this setting is ever changed (e.g. to 600 for 10-min ticks), `_snaps_since` will compute 2× the actual snap count, causing `expire_stale_recommendations` to expire recommendations prematurely (at half the intended time).

**Impact:**

At `SCHEDULER_INTERVAL_SECONDS = 600`:
- A recommendation generated at 10:00 with `AI_EXPIRY_MAX_SNAPS = 3` should expire after 30 minutes (3 × 10 min)
- `_snaps_since` returns `30 // 5 = 6` snaps → already exceeds threshold `3` → expires after only 15 min

The same miscalculation also feeds `_consecutive_no_go_count` in the sustained NO_GO exit logic — a position would exit after half the intended NO_GO duration.

**How to fix:**

```python
def _snaps_since(entry_snap: str, current_snap: str) -> int:
    interval = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    return _minutes_since_entry(entry_snap, current_snap) // interval
```

---

### Issue 4: `iv.py` — `_classify_shape` uses falsy check on `near_iv`

**File:** `optdash/analytics/iv.py` — [lines 188–196](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/iv.py#L188-L196)

**What's wrong:**

```python
# lines 188–189:
def _classify_shape(near_iv: float | None, far_iv: float | None) -> str:
    if not near_iv or not far_iv:     # ← 0.0 is falsy in Python!
        return TermStructureShape.FLAT.value
```

In Python, `not 0.0` evaluates to `True`. So if a deeply OTM near-expiry option has `near_iv = 0.0` (legitimate: near zero implied vol on a worthless option), the function incorrectly returns FLAT instead of computing the ratio. The same applies to `far_iv = 0.0`.

Furthermore, `near_iv = 0.0` would cause a `ZeroDivisionError` on line 191 (`far_iv / near_iv`) if the falsy check hadn't caught it first — but the guard should differentiate between "no data" (None) and "zero value" (0.0).

**Impact:**

Term structure shape is used in:
- `confidence.py` bucket B3: CONTANGO earns +4 points
- `environment.py` gate condition C7: BACKWARDATION costs −1 point

An incorrect FLAT classification doesn't directly cause financial harm (FLAT is the neutral/conservative result), but it suppresses legitimate CONTANGO/BACKWARDATION signals when near-term IV happens to be exactly zero.

**Practical likelihood:** Low — ATM IV is almost never exactly 0.0 for liquid NIFTY/BANKNIFTY options. But the fix is trivial.

**How to fix:**

```python
def _classify_shape(near_iv: float | None, far_iv: float | None) -> str:
    if near_iv is None or far_iv is None or near_iv == 0:
        return TermStructureShape.FLAT.value
    ratio = far_iv / near_iv
    ...
```

The `near_iv == 0` guard explicitly prevents `ZeroDivisionError` while allowing `0.0` `far_iv` to produce a valid ratio of `0.0 / near_iv = 0.0 < 0.95 → BACKWARDATION`.

---

### Issue 5: `direction.py` — `_is_vcoc_spike_active` hardcodes 5-minute interval

**File:** `optdash/ai/direction.py` — [line 166](file:///Users/apple/Documents/Op/OptDash/optdash/ai/direction.py#L166)

**What's wrong:**

```python
# line 166:
earliest_min = max(0, h * 60 + m - n * 5 - 15)
#                                     ^^^
# Hardcoded 5-minute snap interval
```

This function computes a lookback window for V_CoC spike detection. The `n * 5` assumes each snap is 5 minutes apart. At a 10-minute tick interval, the lookback window covers only half the intended duration.

**Impact:**

Same class of issue as Issue 3. If `SCHEDULER_INTERVAL_SECONDS` is changed, the V_CoC spike detection window contracts/expands incorrectly. At 10-min intervals, a genuine V_CoC spike from 2 snaps ago would fall outside the computed lookback window and be missed, reducing directional signal sensitivity.

**How to fix:**

```python
interval = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
earliest_min = max(0, h * 60 + m - n * interval - 15)
```

---

## 🟡 P2 — Medium

### Issue 6: `scheduler.py` — `_today_str()` uses system-local `date.today()`

**File:** `optdash/scheduler.py` — [lines 82–83](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py#L82-L83)

**What's wrong:**

```python
# line 82–83:
def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")
```

`date.today()` uses the **system timezone**, while `_now_ist()` and `_snap_time_str()` use IST (`Asia/Kolkata`). Both are used in the same `tick()` function. If the system timezone is IST (current deployment), there's no bug. But if the app is ever deployed on a UTC server (cloud VM, Docker without TZ), `date.today()` returns the wrong date during 00:00–05:30 UTC (which is 05:30–11:00 IST — the morning trading hours).

In that scenario, `trade_date` passed to `generate_recommendation` and `track_open_positions` would be yesterday's date while `snap_time` is today's first snap — causing all DuckDB queries to query yesterday's partition while the pipeline writes to today's partition. Result: empty analytics, zero recommendations for the entire morning session.

Compare with `_snap_time_str()` which correctly uses `_now_ist()`:
```python
def _snap_time_str() -> str:
    now = _now_ist()     # ← IST-aware
    ...
```

**Impact for current deployment:** None — runs on IST machine. But it's a latent bug for any cloud deployment.

**How to fix:**

```python
def _today_str() -> str:
    return _now_ist().date().strftime("%Y-%m-%d")
```

One-line change, no side effects.

---

### Issue 7: `deps.py` — `_open_journal_conn` duplicates `open_journal()`

**File:** `optdash/api/deps.py` — [lines 83–97](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py#L83-L97) vs `optdash/ai/journal/schema.py` — [lines 234–252](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py#L234-L252)

**What's wrong:**

Two independent functions do essentially the same thing — open a SQLite journal connection with PRAGMAs — but with slightly different PRAGMA sets:

| PRAGMA | `schema.py::open_journal()` | `deps.py::_open_journal_conn()` |
|--------|---------------------------|-------------------------------|
| `journal_mode=WAL` | ✅ | ✅ |
| `foreign_keys=ON` | ✅ | ✅ |
| `busy_timeout=5000` | ✅ | ❌ missing |
| `synchronous=NORMAL` | ❌ not set | ✅ |

The `schema.py` module docstring explicitly states: *"Always open journal connections via `open_journal(path)`. This guarantees that PRAGMA foreign_keys, WAL mode, and busy_timeout are set on every connection."* — but `deps.py` doesn't use it.

**Impact:**

This is the underlying cause of Issue 1 (missing `busy_timeout`). Beyond that, it creates a maintenance hazard: future PRAGMAs added to `open_journal()` won't apply to the API/scheduler connections opened by `deps.py`.

**How to fix:**

Same as Issue 1's fix — replace `_open_journal_conn` with a call to `open_journal()` plus the extra `synchronous=NORMAL`.

---

### Issue 8: `ai.py` + `validators.py` — Duplicate `SnapTime` type definition

**Files:**
- `optdash/api/routers/ai.py` — [lines 22–28](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ai.py#L22-L28)
- `optdash/api/validators.py` — [lines 34–40](file:///Users/apple/Documents/Op/OptDash/optdash/api/validators.py#L34-L40)

**What's wrong:**

`SnapTime` is defined identically in both files:
```python
SnapTime = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
        strip_whitespace=True,
    ),
]
```

The `validators.py` module docstring even acknowledges this: *"Identical to the SnapTime in ai.py -- both are defined here as the single source of truth; ai.py will migrate to this import in a future cleanup commit."*

**Impact:**

If the regex pattern is updated in one file but not the other, the two endpoints will silently accept different snap time formats. Pure maintenance risk, no current functional bug.

**How to fix:**

In `ai.py`, replace the inline `SnapTime` definition with:
```python
from optdash.api.validators import SnapTime
```

---

### Issue 9: `pnl.py` + `environment.py` — Duplicate `_snap_to_min` helper

**Files:**
- `optdash/analytics/pnl.py` — [lines 157–163](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/pnl.py#L157-L163)
- `optdash/analytics/environment.py` — [lines 13–21](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/environment.py#L13-L21)

**What's wrong:**

Both files define `_snap_to_min(t: str) -> int` to convert `HH:MM` to minutes-since-midnight, but with slightly different implementations:

```python
# pnl.py (line 160): uses t[:5] slicing + try/except fallback
h, m = map(int, t[:5].split(":"))   # returns 555 (09:15) on failure

# environment.py (line 20): bare split, no fallback
h, m = map(int, t.split(":"))       # raises on malformed input
```

The `pnl.py` version is more defensive (catches malformed strings), while the `environment.py` version is simpler but will crash on unexpected input formats (e.g. `"09:15:00"` would produce `ValueError` on `int("00")` — actually wait, that would still work. But `"9:15 AM"` would fail).

**Impact:**

Code duplication. No current functional difference for well-formed `HH:MM` inputs.

**How to fix:**

Extract to a shared utility module (e.g. `optdash/utils.py`):
```python
def snap_to_min(t: str) -> int:
    """Convert 'HH:MM' to integer minutes-since-midnight."""
    h, m = map(int, t[:5].split(":"))
    return h * 60 + m
```

Import from both modules.

---

### Issue 10: `coc.py` — `_compute_vcoc_from_series` hardcodes 3-row lookback

**File:** `optdash/analytics/coc.py` — [lines 168–178](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/coc.py#L168-L178)

**What's wrong:**

```python
# line 176–178:
def _compute_vcoc_from_series(rows: list, i: int) -> float:
    if i < 3:
        return 0.0
    return round((rows[i][1] or 0) - (rows[i - 3][1] or 0), 2)
```

The `3` assumes 5-minute snap intervals (3 × 5 = 15 minutes), matching the desired 15-minute V_CoC velocity window. The docstring acknowledges this intentionally: *"Uses index-3 (3 rows back = 15 min at 5-min cadence) for performance"*.

This is used only in `get_coc_series()` (full-day charting), NOT in the live `_compute_vcoc()` which correctly uses a wall-clock 15-minute window. So the impact is limited to the charting endpoint `/api/market/coc`.

**Impact:**

At a non-5-minute tick interval, the full-day CoC chart would show V_CoC computed over incorrect time windows. The live recommendation path (via `_compute_vcoc`) is NOT affected — it uses real wall-clock arithmetic.

**How to fix:**

```python
def _compute_vcoc_from_series(rows: list, i: int) -> float:
    lookback = max(1, 15 // max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60))
    if i < lookback:
        return 0.0
    return round((rows[i][1] or 0) - (rows[i - lookback][1] or 0), 2)
```

---

## 🔵 P3 — Low

### Issue 11: All analytics modules — Exception swallowing hides failures

**Files:** `gex.py`, `coc.py`, `iv.py`, `pcr.py`, `vex_cex.py`, `microstructure.py`, `alerts.py`

**What's wrong:**

Every analytics function catches `Exception` at the top level and returns an empty dict/list:

```python
# Pattern repeated 15+ times across analytics modules:
try:
    ... (actual logic)
except Exception as e:
    logger.warning("function_name error: {}", e)
    return {}   # or []
```

This is **intentionally defensive** — the docstrings and fix comments make clear that keeping the scheduler running is the priority. However, there's no structured way to detect that a function has been failing silently for multiple ticks. The only signal is `logger.warning()` lines in the log, which require active log monitoring.

**Impact:**

If DuckDB returns corrupted data or a schema change breaks a query, the function silently returns `{}` every tick. The scheduler continues running but produces recommendations with degraded data — the confidence score drops (due to fewer B3 structural points from missing iv_data/gex_data) but the recommendation is still issued unless pre-flight blocks it.

The recommender (`recommender.py`) handles this correctly for its own calls via the P2-E isolation policy (skips the tick entirely when analytics raise), but the `environment.py` gate computes its own analytics internally and those catch-all handlers can mask failures.

**Suggestion (not a code fix):**

Add a simple in-memory error counter that the `/health` endpoint can expose:

```python
# In a shared module (e.g. optdash/metrics.py):
from collections import defaultdict
error_counts: dict[str, int] = defaultdict(int)

# In each analytics except block:
except Exception as e:
    error_counts["get_net_gex"] += 1
    logger.warning(...)
    return {}

# In /health:
@app.get("/health")
async def health():
    return {"status": "ok", "analytics_errors": dict(error_counts)}
```

---

## Verified Non-Issues (Previously Reported, Now Invalidated)

### ~~`confidence.py` `session_adjusted` flag~~ — **NOT A BUG**

Originally reported as P0-1. On re-examination, the logic is correct:

```python
# Line 68: raw = b1 + b2 + b3 + b4
# Lines 71–74: raw is mutated in-place by session adjustments
# Line 86: session_adjusted = raw != (b1 + b2 + b3 + b4)
```

`b1`, `b2`, `b3`, `b4` are **local variables that are NOT mutated** by the session adjustment code. The re-expression `(b1 + b2 + b3 + b4)` on line 86 re-computes the pre-adjustment sum from the unchanged locals. So `raw != (b1 + b2 + b3 + b4)` correctly evaluates to `True` exactly when a session adjustment was applied. The comparison is valid.

### ~~Missing authentication~~ — **Not applicable** (personal use)

### ~~Zero test coverage~~ — **Deferred** (per user direction)

### ~~`ws.py` shared `scheduler_journal` connection~~ — **By design**

Both the WS handler and scheduler tick run on the same event loop thread. Python's asyncio is single-threaded cooperative multitasking — they cannot execute concurrently. WAL isolation ensures read consistency. This is architecturally sound.

---

## Summary

| Severity | Count | Effort to Fix All |
|----------|-------|-------------------|
| 🔴 P0 | 1 | ~10 min |
| 🟠 P1 | 4 | ~30 min |
| 🟡 P2 | 5 | ~45 min |
| 🔵 P3 | 1 | ~2 hours (if implemented) |
| **Total** | **11** | **~1.5 hours** |
