# OptDash v2.0

**Options Analytics & AI Trading Engine** — real-time dealer flow analysis and automated trade recommendations for NSE index options.

Tracks **NIFTY · BANKNIFTY · FINNIFTY · MIDCPNIFTY · NIFTYNXT50**.

---

## Architecture

```
optdash/
├── config.py                 ← Pydantic-settings config (all tunables in .env)
├── models/                   ← Enums (Direction, GateVerdict, ExitReason …)
├── utils.py                  ← Shared helpers (snap_to_min)
├── metrics.py                ← Thread-safe analytics error counters
│
├── pipeline/
│   ├── duckdb_gateway.py       ← In-process DuckDB over hive-partitioned Parquet
│   ├── writer.py               ← PyArrow Parquet writer with file-lock
│   ├── processor.py            ← BQ → enriched Parquet pipeline
│   └── incremental.py          ← Live incremental BQ pull per tick
│
├── analytics/
│   ├── gex.py                  ← Net GEX, regime, max pain (NumPy vectorised)
│   ├── coc.py                  ← Cost-of-Carry, V_CoC velocity, ATM/Futures OBI
│   ├── iv.py                   ← IVR, IVP, term structure, HV20
│   ├── pcr.py                  ← Put-Call ratio divergence, smoothed OBI
│   ├── vex_cex.py              ← Vanna/Charm exposure, dealer o'clock
│   ├── screener.py             ← S_score strike ranking (7-factor composite)
│   ├── environment.py          ← 11-point environment gate (GO/WAIT/NO_GO)
│   ├── microstructure.py       ← Volume velocity heatmap
│   ├── pnl.py                  ← Theta-SL curve, Greek PnL attribution
│   ├── alerts.py               ← Real-time alert engine
│   └── query.py                ← Shared DuckDB query helpers
│
├── ai/
│   ├── direction.py            ← 5-signal weighted directional voting
│   ├── confidence.py           ← 4-bucket confidence scorer (max 100)
│   ├── pre_flight.py           ← 7 hard blocking rules
│   ├── quality.py              ← A/B/C/D quality grade
│   ├── narrative.py            ← Template-based trade narrative
│   ├── recommender.py          ← Full recommendation orchestrator
│   ├── tracker.py              ← Live position tracker + trailing stop
│   ├── shadow_tracker.py       ← Hypothetical rejected-trade tracking
│   ├── eod.py                  ← Atomic EOD force-close sweep
│   ├── journal/                ← SQLite DAOs (trades, snaps, shadow, schema)
│   └── learning/               ← Win-rate stats & threshold performance
│
├── api/
│   ├── app.py                  ← FastAPI factory + /health endpoint
│   ├── deps.py                 ← DB lifecycle (DuckDB + SQLite)
│   ├── validators.py           ← Shared Pydantic types (SnapTime)
│   └── routers/
│       ├── market.py             ← Spot, GEX, CoC, gate endpoints
│       ├── micro.py              ← PCR, alerts, volume, VEX/CEX endpoints
│       ├── screener.py           ← Strikes, term structure endpoints
│       ├── ai.py                 ← Recommendations, journal, learning endpoints
│       └── ws.py                 ← WebSocket live snap feed
│
└── scheduler.py              ← APScheduler async tick (configurable interval)

frontend/                     ← Vite + React 18 + TypeScript + TailwindCSS + Recharts
```

---

## Quick Start

### Prerequisites

- **Python 3.11+**
- **Node.js 18+** (for frontend)

### Backend

```bash
# 1. Create virtual environment
python3.11 -m venv .venv

# 2. Install dependencies (editable + dev extras)
.venv/bin/pip install -e ".[dev]"

# 3. Copy env template and configure
cp .env.example .env
# Edit .env as needed (BQ credentials, data paths, etc.)

# 4. Create data directories
mkdir -p data/processed data/raw

# 5. Start API + scheduler
.venv/bin/python run_api.py
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The frontend runs on `http://localhost:5173` and proxies API calls to `http://localhost:8000`.

---

## Key Configuration

All settings live in `.env` (see `.env.example` for full reference). Key ones:

| Variable | Default | Description |
|---|---|---|
| `DATA_ROOT` | `data` | Root for Parquet hive partitions |
| `JOURNAL_DB_PATH` | `data/journal.db` | SQLite journal (trades, snaps, shadows) |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | API bind address |
| `UNDERLYINGS` | `["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50"]` | Tracked index list |
| `SCHEDULER_INTERVAL_SECONDS` | `300` | Tick interval (5 min) |
| `MARKET_HOLIDAYS` | *(2026 NSE calendar)* | ISO dates to skip |
| `GATE_GO_THRESHOLD` | `7` | Min gate score for GO verdict |
| `AI_SL_PCT` | `0.35` | Stop-loss as fraction of entry (35%) |
| `AI_TARGET_MULT` | `1.50` | Target = entry × this multiplier |

---

## API Endpoints

### Market Data

| Method | Path | Description |
|---|---|---|
| GET | `/api/market/spot` | Spot OHLC summary |
| GET | `/api/market/gex` | GEX series + regime |
| GET | `/api/market/coc` | CoC + V_CoC series |
| GET | `/api/market/environment` | 11-point environment gate |
| GET | `/api/market/max-pain` | Max pain strike |

### Microstructure

| Method | Path | Description |
|---|---|---|
| GET | `/api/micro/pcr` | PCR series + divergence |
| GET | `/api/micro/alerts` | Live alert feed |
| GET | `/api/micro/volume-velocity` | Volume velocity heatmap |
| GET | `/api/micro/vex-cex` | VEX/CEX series + dealer o'clock |

### Strike Screener

| Method | Path | Description |
|---|---|---|
| GET | `/api/screener/strikes` | Top-N strikes ranked by S_score |
| GET | `/api/screener/term-structure` | IV term structure by expiry tier |

### AI Trading Engine

| Method | Path | Description |
|---|---|---|
| GET | `/api/ai/recommendation/latest` | Latest pending trade card |
| GET | `/api/ai/position/live` | Current open position + snaps |
| GET | `/api/ai/position/snaps/{id}` | Position snap history |
| POST | `/api/ai/accept` | Accept recommendation |
| POST | `/api/ai/reject` | Reject (creates shadow trade) |
| POST | `/api/ai/close-trade` | Manual close |
| GET | `/api/ai/journal/history` | Paginated trade history |
| GET | `/api/ai/learning/report` | Win-rate & threshold analytics |

### System

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Uptime, DB status, analytics error counts |
| WS | `/ws/live` | WebSocket live snap feed |

---

## Data Flow

```
                    BigQuery (NSE feed)
                          │
                  run_incremental_pull()
                          │
                    ┌─────▼──────┐
                    │  Processor  │  enrich: Greeks, GEX, VEX, CEX, tiers
                    └─────┬──────┘
                          │
                   Parquet Writer
                  (hive-partitioned)
                          │
              data/processed/trade_date=YYYY-MM-DD/*.parquet
                          │
                   ┌──────▼───────┐
                   │   DuckDB     │  in-process, rolling window view
                   │  (LockedConn)│  thread-safe via RLock proxy
                   └──────┬───────┘
                          │
              ┌───────────┼───────────┐
              │           │           │
        Analytics    Environment    Screener
        (10 modules)   Gate (11pt)  (S_score)
              │           │           │
              └───────────┼───────────┘
                          │
                   ┌──────▼───────┐
                   │  Scheduler   │  async tick (every 5 min)
                   │  (APScheduler)│
                   └──────┬───────┘
                          │
          ┌───────────────┼───────────────┐
          │               │               │
     Recommender     Tracker        Shadow Tracker
     (direction →    (trailing SL,   (hypothetical
      confidence →    IV crush,       rejected-trade
      pre-flight →    gate NO_GO)     tracking)
      quality →                       
      narrative)                      
          │               │               │
          └───────────────┼───────────────┘
                          │
                   ┌──────▼───────┐
                   │  Journal DB  │  SQLite (WAL + FK + busy_timeout)
                   │  (trades,    │
                   │   snaps,     │
                   │   shadows)   │
                   └──────┬───────┘
                          │
                   ┌──────▼───────┐
                   │   FastAPI    │  REST + WebSocket
                   └──────┬───────┘
                          │
                   ┌──────▼───────┐
                   │   Frontend   │  React + Recharts
                   └──────────────┘
```

---

## Development

```bash
# Lint
.venv/bin/ruff check optdash/

# Type check
.venv/bin/mypy optdash/

# Tests
.venv/bin/pytest

# Format
.venv/bin/ruff format optdash/
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.11, FastAPI, Uvicorn, APScheduler |
| **Analytics DB** | DuckDB (in-process, Parquet views) |
| **Journal DB** | SQLite (WAL mode, foreign keys) |
| **Data Pipeline** | BigQuery, PyArrow, Pandas |
| **Frontend** | React 18, TypeScript, Vite, TailwindCSS, Recharts |
| **State Management** | Zustand, React Query |
