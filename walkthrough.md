# OptDash Full-Stack Review — Consolidated Findings

> Comprehensive review of the Options Analytics & AI Trading Engine backend.
> ~3,500 lines across 40+ Python modules reviewed.

---

## Executive Summary

OptDash is a **well-architected** trading system with many thoughtful fixes already applied (50+ documented fixes). The codebase demonstrates mature engineering practices: atomic transactions, idempotent migrations, SQL injection guards, proper thread-safety separation, and extensive defensive coding. However, several issues remain that could impact **financial accuracy** and **operational reliability**.

| Severity | Count | Description |
|----------|-------|-------------|
| 🔴 P0 | 2 | Bugs that can corrupt PnL or silently change AI behaviour |
| 🟠 P1 | 4 | Issues that reduce accuracy or create operational risk |
| 🟡 P2 | 5 | Hardcoded assumptions, code duplication, minor inconsistencies |
| 🔵 P3 | 3 | Test coverage, observability, and documentation gaps |
| ⚪ Strengths | — | Architecture patterns worth preserving |

---

## 🔴 P0 — Critical (Financial Risk)

### P0-1: [confidence.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/confidence.py) — `session_adjusted` flag is logically inverted
**File:** [confidence.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/confidence.py)

The `session_adjusted` flag compares `raw` (which is already the post-penalty sum) against the pre-penalty component total. After any session penalty is applied, `raw < (b1 + b2 + b3 + b4)` is **always True**, so `session_adjusted` is always reported as True even when the penalty was zero.

```python
# Current (line ~75-80):
raw = b1 + b2 + b3 + b4
if session == MarketSession.MIDDAY_CHOP:
    raw = int(raw * 0.85)
session_adjusted = raw < (b1 + b2 + b3 + b4)  # ← always True after penalty
```

**Impact:** Learning feedback loop receives incorrect metadata about which scores were session-adjusted. Downstream calibration that relies on this flag will be skewed.

**Fix:** Capture the pre-penalty sum before mutating `raw`:
```python
pre_penalty = b1 + b2 + b3 + b4
raw = pre_penalty
if session == MarketSession.MIDDAY_CHOP:
    raw = int(raw * 0.85)
session_adjusted = raw < pre_penalty
```

---

### P0-2: [deps.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py) — Duplicates [open_journal()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py#234-253) PRAGMAs, misses `busy_timeout`
**File:** [deps.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py#L83-L97)

[_open_journal_conn()](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py#83-98) duplicates the SQLite PRAGMA setup from `schema.py::open_journal()` but with a **different** PRAGMA: it sets `PRAGMA synchronous=NORMAL` (which [open_journal()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py#234-253) does NOT set) and **omits** `PRAGMA busy_timeout=5000` (which [open_journal()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py#234-253) DOES set). Two connections to the same WAL database with different synchronous modes is safe but inconsistent. Missing `busy_timeout` means the API connection will get immediate `SQLITE_BUSY` errors under contention instead of retrying for 5 seconds.

**Fix:** Replace [_open_journal_conn()](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py#83-98) body with a call to [open_journal()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py#234-253) from [schema.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py), then add `PRAGMA synchronous=NORMAL` on top if desired.

---

## 🟠 P1 — High (Accuracy / Reliability)

### P1-1: [tracker.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py) — Trailing stop hardcodes 0.90 multiplier
**File:** [tracker.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py)

The trailing stop calculates `trail_sl = peak_ltp * 0.90` with a hardcoded 10% trail-down. This should use a configurable `AI_TRAILING_SL_PCT` from [config.py](file:///Users/apple/Documents/Op/OptDash/optdash/config.py) to allow strategy tuning without code changes. Currently, changing the trailing stop requires a code deploy.

---

### P1-2: [tracker.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py) — [_snaps_since](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py#285-287) hardcodes 5-minute intervals
**File:** [tracker.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py)

The [_snaps_since()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py#285-287) function calculates snap count as `minutes_elapsed / 5`, hardcoding the 5-minute scheduler interval. If `SCHEDULER_INTERVAL_SECONDS` is changed to 10 minutes, the snap count will be 2x too high, leading to premature sustained NO_GO exits.

**Fix:** Use `settings.SCHEDULER_INTERVAL_SECONDS // 60` instead of `5`.

---

### P1-3: [iv.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/iv.py) — [_classify_shape](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/iv.py#188-197) uses falsy check on `near_iv`
**File:** [iv.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/iv.py#L188-L196)

```python
def _classify_shape(near_iv, far_iv):
    if not near_iv or not far_iv:  # ← 0.0 is falsy!
        return TermStructureShape.FLAT.value
```

A genuine `near_iv = 0.0` (deeply OTM near-expiry option) is treated as missing data and incorrectly classified as FLAT. Should use explicit `None` check: `if near_iv is None or far_iv is None`.

---

### P1-4: [report.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/learning/report.py) — Confidence threshold buckets skip 0–50 range
**File:** [report.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/learning/report.py#L102-L110)

[get_threshold_performance(conn, "confidence")](file:///Users/apple/Documents/Op/OptDash/optdash/ai/learning/stats.py#96-145) uses default buckets `[(0, 50), (50, 65), ...]`. The [confidence](file:///Users/apple/Documents/Op/OptDash/optdash/ai/confidence.py#6-88) field is an integer 0–100, validated by [get_threshold_performance](file:///Users/apple/Documents/Op/OptDash/optdash/ai/learning/stats.py#96-145). The (0, 50) bucket aggregates all low-confidence trades into one bin, masking the distribution of the worst-performing trades. Consider splitting into `[(0, 30), (30, 50), (50, 65), ...]` for better granularity.

---

## 🟡 P2 — Medium (Maintainability / Minor Issues)

### P2-1: [coc.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/coc.py) — [_compute_vcoc_from_series](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/coc.py#168-179) hardcodes 3-row lookback
**File:** [coc.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/coc.py#L168-L178)

Uses `rows[i-3]` assuming 5-minute snap intervals (3 × 5min = 15min). If the scheduler interval changes, this produces an incorrect V_CoC window. The docstring acknowledges this trade-off as intentional for the series path, but it should be flagged.

---

### P2-2: [ai.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ai.py) + [validators.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/validators.py) — Duplicate `SnapTime` type definition
**Files:** [ai.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ai.py#L22-L28) and [validators.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/validators.py#L34-L40)

`SnapTime` is defined identically in both files. The validators.py docstring explicitly calls this out as a known duplication. Should migrate [ai.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ai.py) to import from [validators.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/validators.py).

---

### P2-3: [pnl.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/pnl.py) + [environment.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/environment.py) — Duplicate [_snap_to_min](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/pnl.py#157-164) helper
Both files define their own [_snap_to_min(t: str) -> int](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/pnl.py#157-164). The implementation is slightly different (pnl.py uses `t[:5]` slicing, environment.py does not). Should be extracted to a shared `utils.py`.

---

### P2-4: [scheduler.py](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py) — [_today_str()](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py#82-84) uses `date.today()` without IST
**File:** [scheduler.py](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py#L82-L83)

```python
def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")
```

Uses `date.today()` (system timezone) while [_now_ist()](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py#78-80) and [_snap_time_str()](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py#86-99) use IST. If the server runs in UTC, [_today_str()](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py#82-84) returns the wrong date during the 00:00–05:30 UTC window (18:30–24:00 IST), causing the trade_date passed to [generate_recommendation()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/recommender.py#28-286) and [track_open_positions()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py#16-207) to be off by one day.

**Fix:** `return _now_ist().date().strftime("%Y-%m-%d")`

---

### P2-5: [ws.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ws.py) — Uses `scheduler_journal` connection (shared with scheduler)
**File:** [ws.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ws.py#L55)

The WS handler uses `app.state.scheduler_journal`, the same SQLite connection used by the scheduler tick. While both run on the event loop thread (no thread-safety violation), concurrent WS reads during a scheduler write could see uncommitted transaction state. WAL isolation mode mitigates this, but using a dedicated third connection would be cleaner.

---

## 🔵 P3 — Low (Improvements & Observability)

### P3-1: Zero test coverage
No test files exist anywhere in the project (`test_*.py`, `*_test.py`, `tests/`, `__tests__/`). This is the single biggest systemic risk. Every fix applied so far was discovered manually.

---

### P3-2: No authentication on API endpoints
All API endpoints (`/api/ai/accept`, `/api/ai/reject`, `/api/ai/close-trade`) are unprotected. In a single-user local deployment this is acceptable, but any network-accessible deployment is vulnerable.

---

### P3-3: Exception swallowing in analytics modules
Most analytics functions ([get_net_gex](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/gex.py#9-68), [get_coc_latest](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/coc.py#7-34), [get_ivr_ivp](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/iv.py#9-142), etc.) catch `Exception` and return `{}` or `[]`. While this prevents scheduler crashes, it silently hides data issues. Consider adding structured error counters (e.g., Prometheus metrics or an error log table) so silent failures are detectable.

---

## ⚪ Architecture Strengths (Preserve These)

| Pattern | Where | Why It Matters |
|---------|-------|----------------|
| **Column whitelist + bind params** | [trades.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/trades.py), [snaps.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/snaps.py), [shadow.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/shadow.py) | Prevents SQL injection via dict keys in f-string SQL |
| **Idempotent migrations** | [schema.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py) [_run_migrations()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py#275-329) | Safe to re-run on every startup; categorized error handling |
| **Atomic EOD transactions** | [eod.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/eod.py) read/write phase separation | Force-close + expire in one commit; rollback on failure |
| **GEX peak caching** | [scheduler.py](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py) `_gex_peak_cache` | Eliminates redundant full-day DuckDB scans per tick |
| **Gate pre-computation** | [scheduler.py](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py) [_build_gate_cache()](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py#129-180) | Avoids N+1 DuckDB round-trips for open positions |
| **Dual SQLite connections** | [deps.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py) API + scheduler separation | Thread-safe WAL access without shared mutable state |
| **Event-loop yielding** | [scheduler.py](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py), [ws.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ws.py) | `await asyncio.sleep(0)` between phases prevents blocking |
| **P2-E analytics isolation** | [recommender.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/recommender.py) | Analytics exceptions don't produce bad recommendations |
| **[open_journal()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py#234-253) factory** | [schema.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py) | Guarantees FK, WAL, busy_timeout on every connection |
| **Transition-based alerts** | [alerts.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/alerts.py) | Prevents alert spam by firing only on state transitions |

---

## Prioritized Action Plan

| Priority | Action | Effort | Impact |
|----------|--------|--------|--------|
| 🔴 1 | Fix [confidence.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/confidence.py) `session_adjusted` flag | 15 min | Correct learning feedback loop |
| 🔴 2 | Replace [_open_journal_conn](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py#83-98) in [deps.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py) with [open_journal()](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py#234-253) | 10 min | Add missing `busy_timeout`, remove duplication |
| 🟠 3 | Fix [_today_str()](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py#82-84) to use IST | 5 min | Prevent wrong date on UTC servers |
| 🟠 4 | Make trailing stop `0.90` configurable | 15 min | Enable strategy tuning without deploys |
| 🟠 5 | Fix [_snaps_since](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py#285-287) to use `SCHEDULER_INTERVAL_SECONDS` | 5 min | Prevent broken NO_GO logic at non-5min intervals |
| 🟠 6 | Fix [_classify_shape](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/iv.py#188-197) falsy check → `is None` | 5 min | Correct edge-case term structure classification |
| 🟡 7 | Consolidate `SnapTime` and [_snap_to_min](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/pnl.py#157-164) duplications | 30 min | Reduce maintenance burden |
| 🔵 8 | Add unit tests for AI + analytics modules | 2-3 days | Systemic risk reduction |
| 🔵 9 | Add API authentication (API key / JWT) | 1 day | Security for non-local deployments |
| 🔵 10 | Add error counters for swallowed exceptions | 1 day | Operational visibility |

---

## Files Reviewed

| Phase | Module | Files |
|-------|--------|-------|
| Phase 4 | AI Engine | [direction.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/direction.py), [confidence.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/confidence.py), [pre_flight.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/pre_flight.py), [quality.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/quality.py), [narrative.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/narrative.py), [recommender.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/recommender.py), [tracker.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/tracker.py), [shadow_tracker.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/shadow_tracker.py), [eod.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/eod.py), [schema.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/schema.py), [trades.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/trades.py), [snaps.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/snaps.py), [shadow.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/journal/shadow.py), [stats.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/learning/stats.py), [report.py](file:///Users/apple/Documents/Op/OptDash/optdash/ai/learning/report.py), [query.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/query.py) |
| Phase 3 | Analytics | [gex.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/gex.py), [coc.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/coc.py), [iv.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/iv.py), [pcr.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/pcr.py), [vex_cex.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/vex_cex.py), [screener.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/screener.py), [environment.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/environment.py), [alerts.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/alerts.py), [pnl.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/pnl.py), [microstructure.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/microstructure.py) |
| Phase 5 | API | [app.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/app.py), [deps.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py), [validators.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/validators.py), [market.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/market.py), [micro.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/micro.py), [screener.py](file:///Users/apple/Documents/Op/OptDash/optdash/analytics/screener.py), [ai.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ai.py), [ws.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/routers/ws.py) |
| Phase 6 | Scheduler | [scheduler.py](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py) |
| Phase 2 | Pipeline | [deps.py](file:///Users/apple/Documents/Op/OptDash/optdash/api/deps.py) (startup/shutdown lifecycle), [scheduler.py](file:///Users/apple/Documents/Op/OptDash/optdash/scheduler.py) (incremental pull) |
