# OptDash — Part 8: Learning Engine

The Learning Engine analyses closed trade history to compute performance statistics, win rates by bucket, and a calibration report. It lives in `optdash/ai/learning/` and is accessed via the `GET /api/ai/learning/report` endpoint.

---

## 1. Module Structure

```
ai/learning/
├── stats.py    ← get_session_stats(), get_threshold_performance()
└── report.py   ← build_learning_report()
```

There are no other sub-modules. The learning engine does not auto-update weights or retrain models — it computes read-only aggregate statistics from the `trades` table.

---

## 2. Session Stats (`learning/stats.py`)

### `get_session_stats(conn, underlying=None, direction=None, session=None, min_trades=10) → dict`

Computes win rate and average PnL for a filter combination. Falls back to global stats if the bucket has fewer than `min_trades` closed trades.

```sql
SELECT
    COUNT(*)                                             AS total,
    SUM(CASE WHEN final_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
    AVG(final_pnl_pct)                                   AS avg_pnl,
    AVG(confidence)                                      AS avg_conf,
    AVG(gate_score)                                      AS avg_gate
FROM trades
WHERE status='CLOSED' AND final_pnl_pct IS NOT NULL
  AND <dynamic filters>
```

**Cold-start guard:** `win_rate` is `None` when `total=0` — never coerced to a fictitious 50%. Callers check `total < 5` or `is_fallback` before using the value.

**Returns:**
```python
{
    "win_rate":       float | None,   # None when total == 0
    "avg_pnl":        float,
    "total_trades":   int,
    "avg_confidence": float,
    "avg_gate":       float,
    "is_fallback":    bool,   # True when global stats substituted for bucket
}
```

`is_fallback=True` means the bucket had fewer than `min_trades` closed trades and the query fell back to global stats. `confidence.py` Bucket 4 zeroes B4 when `is_fallback=True` or `total_trades < 5`.

### `get_threshold_performance(conn, threshold_field, buckets=None) → list[dict]`

Win rate breakdown by score bucket for calibration analysis.

```python
# threshold_field must be one of:
_ALLOWED_THRESHOLD_FIELDS = {"confidence", "gate_score", "s_score"}
# Any other value raises ValueError (prevents SQL injection)
```

Default buckets: `[(0,50), (50,65), (65,75), (75,85), (85,101)]`

Each bucket is validated: `lo < hi` (raises `ValueError` if not, preventing silent empty results).

**Returns:**
```python
[
    {
        "bucket":   "0-50",
        "total":    int,
        "wins":     int,
        "win_rate": float | None,   # None when total == 0
        "avg_pnl":  float,
    },
    ...
]
```

---

## 3. Learning Report (`learning/report.py`)

### `build_learning_report(conn, days=30) → dict`

Builds a comprehensive performance report for the last `days` trading days.

```python
since = (datetime.now(IST).date() - timedelta(days=days)).isoformat()

# Closed trades in window
trades = conn.execute("SELECT ... FROM trades WHERE status='CLOSED' AND trade_date >= ?", [since])
```

**Report contents:**

```python
{
    "period_days":       int,
    "total_trades":      int,
    "total_closed":      int,
    "win_rate":          float | None,
    "avg_pnl_pct":       float,
    "by_underlying":     {
        "NIFTY": {"total": ..., "wins": ..., "win_rate": ..., "avg_pnl": ...},
        ...
    },
    "by_direction":      {"CE": {...}, "PE": {...}},
    "by_session":        {session: {...}, ...},
    "by_grade":          {"A": {...}, "B": {...}, "C": {...}, "D": {...}},
    "confidence_calibration": [
        {"bucket": "65-75", "win_rate": 0.62, "count": 18}, ...
    ],
    "gate_calibration":  [...],
    "shadow_regret": {
        "CLEAN_MISS":  int,   # rejected/expired trades that would have profited >30%
        "GOOD_SKIP":   int,   # correctly avoided trades that lost >20%
        "BREAK_EVEN":  int,
        "RISKY_MISS":  int,
    },
}
```

### Report Caching (`api/routers/ai.py`)

The learning report is expensive (multiple aggregation queries). The API endpoint caches the last result for 60 seconds (`_REPORT_TTL = 60`):

```python
_report_cache:      dict[int, dict] = {}    # {days: report}
_report_cache_lock: threading.Lock  = threading.Lock()
```

Uses a double-checked locking pattern — the lock is acquired for microseconds on cache hit (fast path) and held only during computation on cache miss. Thread-safe for FastAPI's anyio thread pool.

---

## 4. Shadow Regret Analysis

Every `REJECTED` or `EXPIRED` trade spawns a shadow trade tracked every tick until it hits its hypothetical SL or target (or EOD).

Shadow outcomes feed into `shadow_regret` in the learning report:

| Outcome | Threshold |
|---|---|
| `CLEAN_MISS` | pnl_pct > +30% — costly rejection |
| `GOOD_SKIP` | pnl_pct < –20% — correct decision |
| `BREAK_EVEN` | abs(pnl_pct) < 5% |
| `RISKY_MISS` | Everything else |

A high `CLEAN_MISS` count signals that the pre-flight or confidence thresholds may be too conservative.

---

## 5. Feedback Loop to Confidence Scoring

`get_session_stats()` is called in the recommendation flow (Step 10) to populate `learning_stats`:

```python
learning_stats = get_session_stats(
    jconn,
    underlying=underlying,
    direction=direction,
    session=session,
)
compute_confidence(..., learning_stats=learning_stats)
```

Bucket 4 grants 0–10 pts based on `learning_stats["win_rate"]`. This makes the system self-calibrating: a direction+underlying bucket with a proven 70%+ win rate gets more confidence credit than an untested bucket.
