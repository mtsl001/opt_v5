# OptDash — Part 10: Configuration & Deployment

---

## 1. Configuration System

All settings are managed via `optdash/config.py` using **Pydantic-Settings**. Values are loaded from the `.env` file in the project root (or environment variables).

```python
from optdash.config import settings
print(settings.SCHEDULER_INTERVAL_SECONDS)  # 300
```

There is no hardcoded constant elsewhere — every tunable value reads from `settings.*`.

---

## 2. Full Configuration Reference

### Data & Database

| Variable | Default | Description |
|---|---|---|
| `DATA_ROOT` | `data` | Root directory for Parquet files |
| `JOURNAL_DB_PATH` | `data/journal.db` | SQLite journal path |
| `DUCKDB_THREADS` | `4` | DuckDB CPU parallelism |
| `DUCKDB_MEMORY_LIMIT` | `2GB` | DuckDB memory cap |
| `DUCK_VIEW_LOOKBACK_DAYS` | `5` | Rolling Parquet window |
| `RAW_PARQUET_RETENTION_DAYS` | `3` | Days before raw BQ Parquets are purged |

### API

| Variable | Default | Description |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Uvicorn bind host |
| `API_PORT` | `8000` | Uvicorn bind port |
| `LOG_LEVEL` | `INFO` | Loguru log level |
| `CORS_ORIGINS` | `["http://localhost:5173","http://localhost:3000"]` | Allowed CORS origins |

### Underlyings

| Variable | Default | Description |
|---|---|---|
| `UNDERLYINGS` | `["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50"]` | Tracked index list (JSON list) |
| `DEFAULT_UNDERLYING` | `NIFTY` | Default for API endpoints without `underlying` param |

### Scheduler

| Variable | Default | Description |
|---|---|---|
| `SCHEDULER_INTERVAL_SECONDS` | `300` | Tick interval. All snap-count lookbacks derive from this |
| `WS_INTERVAL_SECONDS` | `5` | WebSocket push interval |

### Market Timing

| Variable | Default | Description |
|---|---|---|
| `MARKET_OPEN` | `09:15` | First valid tick |
| `MARKET_CLOSE` | `15:30` | Last valid tick |
| `EOD_FORCE_CLOSE_TIME` | `15:20` | Force-close all ACCEPTED trades |
| `EOD_SWEEP_TIME` | `15:25` | Finalize shadows + DuckDB view refresh |
| `MARKET_HOLIDAYS` | *(2026 NSE calendar)* | ISO date strings to skip ticks entirely |

> `EOD_SWEEP_TIME` comparison is lexicographic `>=` with the zero-padded snap key (`HH:MM`). Both sides are always zero-padded, so this is correct.

### Session Boundaries

| Variable | Default |
|---|---|
| `SESSION_OPENING_END` | `10:15` |
| `SESSION_MIDDAY_START` | `11:30` |
| `SESSION_MIDDAY_END` | `13:00` |
| `SESSION_CLOSING_START` | `14:30` |
| `DEALER_OCLOCK_START` | `14:00` |

### Environment Gate

| Variable | Default | Description |
|---|---|---|
| `GATE_GO_THRESHOLD` | `7` | Min score for GO verdict |
| `GATE_WAIT_THRESHOLD` | `5` | Min score for WAIT verdict |
| `GATE_MAX_SCORE` | `11` | Maximum gate score (9 core + 2 bonus) |

### AI Recommender

| Variable | Default | Description |
|---|---|---|
| `PREFLIGHT_MIN_GATE_SCORE` | `5` | PF-1: minimum gate score |
| `PREFLIGHT_MIN_CONFIDENCE` | `50` | PF-2: minimum confidence |
| `PREFLIGHT_MAX_THETA_RATIO` | `0.03` | PF-3: max abs(theta)/ltp |
| `PREFLIGHT_MAX_PAIN_PROXIMITY` | `0.005` | PF-4: max pain distance (0.5%) |
| `PREFLIGHT_DTE1_MIN_GATE` | `7` | Higher gate bar when DTE=1 |
| `PREFLIGHT_DTE1_MIN_CONFIDENCE` | `65` | Higher confidence bar when DTE=1 |
| `AI_SL_PCT` | `0.35` | Stop-loss as fraction of entry (35%) |
| `AI_TARGET_MULT` | `1.50` | Target = entry × this |
| `AI_EXPIRY_MAX_SNAPS` | `3` | Snaps before GENERATED expires |

### Position Management

| Variable | Default | Description |
|---|---|---|
| `TRAILING_STOP_ACTIVATION` | `0.20` | Activate trailing stop at +20% PnL |
| `TRAILING_STOP_TRAIL_PCT` | configurable | Trail below current LTP by this fraction |
| `GATE_SUSTAINED_NO_GO_SNAPS` | `2` | Consecutive NO_GO snaps before auto-close |
| `ZGL_PROXIMITY_PCT` | `0.5` | Fire APPROACHING_ZGL alert when Spot is within X% of ZGL |

### Confidence Scoring Parameters

| Variable | Default |
|---|---|
| `SESSION_MIDDAY_CONFIDENCE_PENALTY` | `10` |
| `SESSION_CLOSING_CONFIDENCE_CAP` | `60` |
| `CONFIDENCE_B4_MIN_TRADES` | `5` |
| `CONFIDENCE_B4_SCALE` | `12` |

### Strike Screener

| Variable | Default | Description |
|---|---|---|
| `SCREENER_TOP_N` | `20` | Max strikes returned |
| `SCREENER_MAX_MONEYNESS_PCT` | `5.0` | Max % away from spot |
| `SCREENER_MIN_LIQUIDITY_CR` | `0.5` | Min OI×LTP in crores |
| `SCREENER_MIN_DELTA` | `0.10` | Min absolute delta |
| `SCREENER_MAX_DELTA` | `0.50` | Max absolute delta |
| `W_DELTA` | `4.0` | S_score weight: delta |
| `W_THETA` | `2.0` | S_score weight: theta ratio |
| `W_LIQUIDITY` | `3.0` | S_score weight: notional liquidity |
| `W_IV` | `2.0` | S_score weight: IV cheapness |
| `W_GAMMA` | `1.0` | S_score weight: gamma |
| `W_VEGA` | `1.0` | S_score weight: vega |
| `W_EFF_RATIO` | `4.0` | S_score weight: efficiency ratio |
| `STAR_4_THRESHOLD` | `100.0` | ⭐⭐⭐⭐ minimum |
| `STAR_3_THRESHOLD` | `80.0` | ⭐⭐⭐ minimum |
| `STAR_2_THRESHOLD` | `60.0` | ⭐⭐ minimum |

### IV Analytics

| Variable | Default |
|---|---|
| `IV_LOOKBACK_DAYS` | `252` |
| `VRP_OVERPRICED_THRESHOLD` | `2.0` |
| `VRP_UNDERPRICED_THRESHOLD` | `0.0` |
| `VIX_HIGH_THRESHOLD` | `20.0` |
| `VIX_HIGH_IVP_THRESHOLD` | `35.0` |
| `RISK_FREE_RATE` | `0.0625` |
| `SKEW_ELEVATED_THRESHOLD` | `5.0` | 

### GEX Analytics

| Variable | Default | Description |
|---|---|---|
| `GEX_SCALING` | `1000000000.0` | Divisor to convert raw GEX to B (billions) |
| `GEX_DECLINE_THRESHOLD` | `0.70` | pct_of_peak below this → POSITIVE_DECLINING |

### CoC / V_CoC

| Variable | Default |
|---|---|
| `VCOC_BULL_THRESHOLD` | `10.0` |
| `VCOC_BEAR_THRESHOLD` | `-10.0` |
| `VCOC_SPIKE_EXPIRY_SNAPS` | `3` |
| `COC_DISCOUNT_THRESHOLD` | `-5.0` |

### OBI

| Variable | Default |
|---|---|
| `OBI_THRESHOLD` | `0.10` |

### PCR Analytics

| Variable | Default |
|---|---|
| `PCR_Z_PANIC_THRESHOLD` | `1.5` |
| `PCR_Z_BUILDING_THRESHOLD` | `0.8` |
| `PCR_Z_FADING_TREND` | `0.05` |

---

## 3. Quick Start

```bash
# 1. Create venv
python3.11 -m venv .venv

# 2. Install dependencies (editable + dev extras)
.venv/bin/pip install -e ".[dev]"

# 3. Configure
cp .env.example .env
# Edit .env — at minimum confirm DATA_ROOT and UNDERLYINGS

# 4. Create data directories
mkdir -p data/processed data/raw

# 5. Start API + scheduler (do NOT run until data is available)
.venv/bin/python run_api.py

# 6. Frontend
cd frontend && npm install && npm run dev
```

---

## 4. Development Tools

```bash
# Lint
.venv/bin/ruff check optdash/

# Type check
.venv/bin/mypy optdash/

# Tests
.venv/bin/pytest

# Syntax check all files
python -c "import ast, os; [ast.parse(open(p).read()) for p in
  (os.path.join(d,f) for d,_,fs in os.walk('optdash') for f in fs if f.endswith('.py'))]"
```

---

## 5. Data Requirements

The application requires Parquet data in `data/processed/` with the schema described in Part 1, Section 6. Without data:

- DuckDB view registers as empty
- All analytics return `{}` or `[]`
- Gate score = 0 (NO_GO) — no recommendations are generated
- The `/health` endpoint still returns `status: ok`

To backfill historical data use `run_pipeline.py`, which pulls from BigQuery.

---

## 6. Operational Notes

- **Timezone**: All timestamps in IST (`Asia/Kolkata`). Do not run on a system with `TZ=UTC` without verifying IST conversion in `scheduler.py::_now_ist()`.
- **SQLite WAL**: `busy_timeout=5000` and `synchronous=NORMAL` are set by `open_journal()`. Do not change to `FULL` — it would make the scheduler tick too slow.
- **DuckDB memory**: Increase `DUCKDB_MEMORY_LIMIT` if running >5 underlyings or >5 lookback days on a low-RAM machine.
- **Port conflicts**: Default port 8000. Change via `API_PORT` in `.env`.
- **Log rotation**: Loguru logs to stdout by default. Redirect with `python run_api.py 2>&1 | tee app.log` or configure Loguru rotation in `config.py`.
