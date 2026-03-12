# OptDash — Part 2: Data Pipeline & DuckDB Gateway

---

## 1. Overview

OptDash pulls live NSE options chain data from **BigQuery** (BQ) via an incremental watermark-based pull on every scheduler tick. Pulled data is enriched with Greeks, dealer exposure columns, and expiry tiers, then written to **per-underlying Parquet files** under `data/processed/`. DuckDB reads this directory as an in-process columnar view for all analytics.

This design decouples feed ingestion from analytics computation — analytics never wait for a network call, and historical data is always available for replay and backtesting.

---

## 2. Processed Parquet Layout

Parquet files follow a **Hive partition layout**:

```
data/
└── processed/
    ├── trade_date=2026-03-12/
    │   ├── NIFTY.parquet           ← all snaps for NIFTY on that day
    │   ├── BANKNIFTY.parquet
    │   ├── FINNIFTY.parquet
    │   ├── MIDCPNIFTY.parquet
    │   └── NIFTYNXT50.parquet
    ├── trade_date=2026-03-11/
    │   └── ...
    └── raw/                        ← raw BQ extract (separate, NOT read by DuckDB view)
        └── trade_date=2026-03-12/
            └── *.parquet
```

DuckDB extracts `trade_date` automatically from the hive directory name. Each file contains all snaps for one underlying for one day.

> **Important**: DuckDB reads only `data/processed/`. The `data/raw/` subtree has a different schema (no Greeks, no enriched columns) and must never be included in the view.

---

## 3. Live BQ Incremental Pull (`pipeline/incremental.py`)

Every scheduler tick calls `run_incremental_pull(duck_conn)`:

```
run_incremental_pull()
    │
    ├─ Query BQ for rows newer than last watermark
    ├─ Enrich via processor.process_snapshot()
    ├─ Write to data/processed/trade_date=.../UNDERLYING.parquet
    │   (merge with existing day file, filelock for concurrency safety)
    └─ If new trade_date partition created: refresh_views(duck_conn)
       (makes new-day data visible to DuckDB without process restart)
```

The pull is non-blocking — run via `asyncio.to_thread()` in the scheduler tick so it never delays the event loop.

---

## 4. Data Enrichment (`pipeline/processor.py`)

The processor enriches raw BQ options data with:

| Enrichment Column | Source / Formula |
|---|---|
| `gex` | `gamma × oi × spot² × 0.01` (sign: CE=positive, PE=negative) |
| `vex` | Vanna × OI (per Black-Scholes) |
| `cex` | Charm × OI (per Black-Scholes) |
| `expiry_tier` | `TIER1` (nearest expiry), `TIER2`, `TIER3` |
| `dte` | Calendar days to expiry |
| `bid_qty` / `ask_qty` | Mapped from `total_buy_qty` / `total_sell_qty` |
| `instrument_type` | `OPT` for options, `FUT` for futures rows |

PyArrow writes files with a canonical schema enforced at write time. `filelock` serialises any concurrent write access to the same Parquet file.

---

## 5. DuckDB Gateway (`pipeline/duckdb_gateway.py`)

### 5.1 Connection & PRAGMAs

```python
_conn = duckdb.connect(database=":memory:", read_only=False)
_conn.execute(f"PRAGMA threads={settings.DUCKDB_THREADS}")         # default 4
_conn.execute(f"PRAGMA memory_limit='{settings.DUCKDB_MEMORY_LIMIT}'")  # default '2GB'
```

### 5.2 LockedConn — Thread-Safe Proxy

All callers receive a `LockedConn` proxy instead of the raw `DuckDBPyConnection`:

```python
class LockedConn:
    def execute(self, query, parameters=None):
        _view_lock.acquire()
        try:
            return self._real.execute(query, parameters)
        finally:
            _view_lock.release()
```

`_view_lock` is a `threading.RLock`. This serialises all analytics queries and blocks them during `refresh_views()` when the catalog entry is momentarily absent during `CREATE OR REPLACE VIEW`. **No caller needs to acquire the lock explicitly** — it is structural.

### 5.3 View Registration

```python
real.execute(
    "CREATE OR REPLACE VIEW options_data AS "
    "SELECT * FROM read_parquet($1, hive_partitioning=true, union_by_name=true)",
    [globs],   # list of per-day *.parquet glob strings
)
```

**Key parameters:**
- `$1` — path never interpolated into SQL; prevents injection
- `hive_partitioning=true` — auto-detects `trade_date=YYYY-MM-DD` from directory name
- `union_by_name=true` — merges Parquet files with different column sets by name; missing columns become NULL
- `globs` — rolling window of the last `DUCK_VIEW_LOOKBACK_DAYS` calendar days (default 5)

### 5.4 Rolling Window (`_build_rolling_globs`)

Only the most recent `DUCK_VIEW_LOOKBACK_DAYS` directories are included in the view. This bounds Parquet scan time and memory regardless of how long the service has been running.

```python
today = datetime.now(IST).date()   # IST-aware, never system-local
for i in range(lookback_days):
    d = today - timedelta(days=i)
    day_dir = processed / f"trade_date={d:%Y-%m-%d}"
    if day_dir.exists():
        globs.append(str(day_dir / "*.parquet"))
```

### 5.5 Schema Validation

After each `refresh_views()`, `_validate_view_schema()` checks that all `REQUIRED_COLUMNS` are present:

```python
REQUIRED_COLUMNS = frozenset({
    "trade_date", "snap_time", "underlying", "strike_price", "expiry_date",
    "option_type", "instrument_type", "ltp", "iv", "delta", "theta",
    "gamma", "vega", "spot", "fut_price", "oi", "volume",
    "bid_qty", "ask_qty",       # OBI columns
    "gex", "vex", "cex",        # dealer exposure
    "expiry_tier", "dte",
})
```

On startup (`raise_on_error=True`) a missing column crashes the process immediately. On EOD refresh it only logs an error.

### 5.6 View Refresh at Day Rollover

`refresh_views(duck)` is called at EOD so the new day's partition directory becomes visible on the first tick after midnight without a process restart.

---

## 6. Ticker Tick Steps (Scheduler)

The scheduler tick (`optdash/scheduler.py`) runs every `SCHEDULER_INTERVAL_SECONDS` (default 300 s) and executes these steps in order, each yielding to the event loop between steps:

```
tick()
    ├─ Step 0: run_incremental_pull()   [BQ pull → Parquet, asyncio.to_thread]
    │   (non-fatal: exception logged, tick continues)
    │
    ├─ EOD block (once/day when _is_eod() and not _eod_done_today()):
    │   ├─ eod_force_close(duck, jconn, trade_date)
    │   ├─ finalize_all_shadows(duck, jconn, trade_date)
    │   ├─ purge_old_raw_parquets(DATA_ROOT, RAW_PARQUET_RETENTION_DAYS)
    │   ├─ refresh_views(duck)
    │   └─ done_flags[trade_date] = True  (prevents re-entry)
    │
    ├─ Step 1: expire_stale_recommendations(jconn, trade_date, snap_time)
    ├─ Step 2: _build_gate_cache(duck, trade_date, snap_time, jconn, _gex_peak_cache)
    ├─ Step 3: generate_recommendation() × each UNDERLYING
    ├─ Step 4: track_open_positions(duck, jconn, trade_date, snap_time, gate_cache)
    └─ Step 5: track_shadow_positions(duck, jconn, trade_date, snap_time)
```

Each step is wrapped in its own `try/except` at the underlying level so a failure in one underlying never blocks the next.

---

## 7. Snap Time Calculation

The scheduler always computes the canonical snap time by rounding down to the nearest interval:

```python
def _snap_time_str() -> str:
    now = _now_ist()
    interval_mins = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    mins = (now.minute // interval_mins) * interval_mins
    return f"{now.hour:02d}:{mins:02d}"
```

This ensures the snap key always matches the keys written by the BQ pipeline.

---

## 8. Error Handling

| Scenario | Behaviour |
|---|---|
| `data/processed` empty at startup | Warning logged, view not registered; analytics return empty results |
| New Parquet file written mid-session | Visible on next DuckDB query automatically |
| New trade-date partition created by BQ pull | `refresh_views()` called immediately so new day is visible same tick |
| Missing required column | `RuntimeError` on startup (fail-fast); error log on EOD refresh |
| BQ pull fails | Error logged, tick continues with last cached Parquet data |
| analytics function raises | `record_error(fn_name)` called; exception caught; empty result returned |

---

## 9. Key Configuration

| Setting | Default | Description |
|---|---|---|
| `DATA_ROOT` | `data` | Root for Parquet files |
| `DUCK_VIEW_LOOKBACK_DAYS` | `5` | Rolling window days in DuckDB view |
| `DUCKDB_THREADS` | `4` | DuckDB parallelism |
| `DUCKDB_MEMORY_LIMIT` | `2GB` | DuckDB memory cap |
| `SCHEDULER_INTERVAL_SECONDS` | `300` | Tick interval (5 min) |
| `EOD_FORCE_CLOSE_TIME` | `15:20` | Force-close all ACCEPTED trades |
| `EOD_SWEEP_TIME` | `15:25` | Finalize shadows + refresh view |
| `RAW_PARQUET_RETENTION_DAYS` | `3` | Purge old raw Parquets after N days |
| `MARKET_HOLIDAYS` | *(2026 NSE calendar)* | ISO date strings to skip |
