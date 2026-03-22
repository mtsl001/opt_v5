# OptDash v2.7.0

**Options Analytics & AI Trading Engine** — real-time dealer flow analysis and automated trade recommendations for NSE index options.

Tracks **NIFTY · BANKNIFTY · FINNIFTY · MIDCPNIFTY · NIFTYNXT50**.

---

## Quick Start

### Prerequisites

- **Python 3.11+**
- **Node.js 18+**

### Backend

```bash
# 1. Create virtual environment
python3.11 -m venv .venv

# 2. Install dependencies (editable + dev extras)
.venv/bin/pip install -e ".[dev]"

# 3. Configure environment
cp .env.example .env
# Edit .env — set BQ credentials, data paths, etc.

# 4. Create data directories
mkdir -p data/processed data/raw

# 5. Start API + scheduler
.venv/bin/python run_api.py
```

API available at `http://localhost:8000`. Health check: `http://localhost:8000/health`.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard available at `http://localhost:5173`, proxied to `http://localhost:8000`.

---

## Architecture

```
optdash/
├── config.py               ← Pydantic-settings — all tunables in .env
├── models/                 ← Enums (Direction, GateVerdict, ExitReason, …)
├── metrics.py              ← Thread-safe analytics error counters
├── utils.py                ← Shared helpers (snap_to_min, …)
│
├── pipeline/
│   ├── bq_client.py        ← BigQuery reader (backfill + incremental)
│   ├── processor.py        ← BQ → enriched Parquet (Greeks, GEX, VEX, CEX, tiers)
│   ├── writer.py           ← PyArrow Parquet writer with FileLock
│   ├── duckdb_gateway.py   ← In-process DuckDB over hive-partitioned Parquet (LockedConn)
│   ├── incremental.py      ← Live 5-min BQ pull with watermark tracking
│   ├── backfill.py         ← Historical BQ backfill
│   └── watermark.py        ← Atomic watermark persistence
│
├── analytics/
│   ├── gex.py              ← Net GEX, regime (3-way), max pain
│   ├── coc.py              ← Cost-of-Carry, V_CoC velocity (15-min wall-clock), ATM/Futures OBI
│   ├── iv.py               ← IVR, IVP (252-day), HV20, term structure shape
│   ├── pcr.py              ← PCR divergence, smoothed OBI
│   ├── vex_cex.py          ← Vanna/Charm exposure, per-underlying thresholds, Dealer O'Clock
│   ├── screener.py         ← 7-factor S_score composite strike ranking
│   ├── environment.py      ← 11-point environment gate (GO / WAIT / NO_GO)
│   ├── microstructure.py   ← Volume velocity heatmap (10-snap rolling median)
│   ├── pnl.py              ← Theta-SL curve, Greek PnL attribution, theta clock
│   ├── alerts.py           ← Transition-based alert engine with dedup
│   └── query.py            ← Shared DuckDB query helpers
│
├── ai/
│   ├── direction.py        ← 5-signal weighted directional voting
│   ├── confidence.py       ← 4-bucket confidence scorer (max 100)
│   ├── pre_flight.py       ← 7 hard blocking rules (incl. Dealer O'Clock, DTE=0)
│   ├── quality.py          ← A/B/C/D quality grade
│   ├── narrative.py        ← Template-based trade narrative (no LLM)
│   ├── recommender.py      ← Orchestrator: Direction → Gate → Screener → Pre-flight → Journal
│   ├── tracker.py          ← Live position tracker (trailing stop, IV crush, gate NO_GO auto-close)
│   ├── shadow_tracker.py   ← Hypothetical rejected-trade tracking
│   ├── eod.py              ← Atomic EOD force-close sweep with rollback
│   ├── journal/
│   │   ├── schema.py       ← SQLite DDL + idempotent migrations
│   │   ├── trades.py       ← Trades DAO (column-whitelist, pagination)
│   │   ├── shadow.py       ← Shadow trades DAO
│   │   └── snaps.py        ← Position snaps DAO
│   └── learning/
│       └── report.py       ← Win-rate stats & threshold performance
│
├── api/
│   ├── app.py              ← FastAPI factory + /health endpoint
│   ├── deps.py             ← DB lifecycle (DuckDB + dual SQLite connections)
│   ├── validators.py       ← Shared Pydantic types (SnapTime)
│   └── routers/
│       ├── market.py       ← Spot, GEX, CoC, gate endpoints
│       ├── micro.py        ← PCR, alerts, volume velocity, VEX/CEX endpoints
│       ├── screener.py     ← Strikes, term structure endpoints
│       ├── ai.py           ← Recommendations, accept/reject/close, journal, learning
│       └── ws.py           ← WebSocket live snap feed
│
└── scheduler.py            ← APScheduler async tick (default 5-min interval)

frontend/                   ← Vite + React 18 + TypeScript + TailwindCSS + Recharts
```

---

## Data Flow

```
BigQuery (NSE live feed)
        │
run_incremental_pull()  ← watermark-based, every tick
        │
  ┌─────▼──────┐
  │  Processor  │  enrich: Greeks, GEX, VEX, CEX, expiry_tier, DTE
  └─────┬──────┘
        │
   Parquet Writer  ← FileLock + atomic os.replace()
  (hive-partitioned by trade_date)
        │
data/processed/trade_date=YYYY-MM-DD/*.parquet
        │
  ┌─────▼──────┐
  │   DuckDB   │  :memory:, rolling window view, LockedConn (RLock proxy)
  └─────┬──────┘
        │
 Analytics (10 modules) + Environment Gate (11-pt)
        │
  ┌─────▼──────┐
  │  Scheduler  │  APScheduler async tick
  └─────┬──────┘
        │
 ┌──────┼──────┐
 │      │      │
Rec.  Tracker Shadow
 │      │      │
 └──────┼──────┘
        │
  SQLite Journal  ← WAL + FK + busy_timeout
  (trades, snaps, shadows)
        │
   FastAPI  ←→  React Frontend
```

---

## API Endpoints

### Market Data

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/market/spot` | Spot OHLC summary |
| GET | `/api/market/gex` | GEX series + regime (3-way) |
| GET | `/api/market/coc` | CoC + V_CoC series |
| GET | `/api/market/environment` | 11-point environment gate |
| GET | `/api/market/max-pain` | Max pain strike |

### Microstructure

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/micro/pcr` | PCR series + divergence |
| GET | `/api/micro/alerts` | Live alert feed |
| GET | `/api/micro/volume-velocity` | Volume velocity heatmap |
| GET | `/api/micro/vex-cex` | VEX/CEX series + Dealer O'Clock |
| GET | `/api/micro/vex-cex/by-strike` | Per-strike VEX/CEX breakdown |

### Strike Screener

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/screener/strikes` | Top-N strikes ranked by S_score |
| GET | `/api/screener/term-structure` | IV term structure by expiry tier |

### AI Trading Engine

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/ai/recommendation/latest` | Latest pending trade card |
| GET | `/api/ai/position/live` | Open position + live PnL |
| GET | `/api/ai/position/snaps/{id}` | Position snap history |
| POST | `/api/ai/accept` | Accept recommendation |
| POST | `/api/ai/reject` | Reject (creates shadow trade) |
| POST | `/api/ai/close-trade` | Manual close |
| GET | `/api/ai/journal/history` | Paginated trade history |
| GET | `/api/ai/learning/report` | Win-rate & threshold analytics |

### System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Uptime, DB status, analytics error counts |
| WS | `/ws/live` | WebSocket live snap feed |

---

## Key Configuration

All settings live in `.env` (see `.env.example` for full reference):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_ROOT` | `data` | Root for Parquet hive partitions |
| `JOURNAL_DB_PATH` | `data/journal.db` | SQLite journal (trades, snaps, shadows) |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | API bind address |
| `UNDERLYINGS` | `["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50"]` | Tracked indices |
| `SCHEDULER_INTERVAL_SECONDS` | `300` | Tick interval (5 min) |
| `MARKET_HOLIDAYS` | *(2026 NSE calendar)* | ISO dates to skip |
| `GATE_GO_THRESHOLD` | `7` | Min gate score for GO verdict |
| `AI_SL_PCT` | `0.35` | Stop-loss as fraction of entry premium (35%) |
| `AI_TARGET_MULT` | `1.50` | Target = entry × multiplier |
| `TRAILING_STOP_ACTIVATION` | `0.20` | Activate trailing stop at +20% PnL |
| `GATE_SUSTAINED_NO_GO_SNAPS` | `2` | Auto-close after N consecutive gate NO_GO |
| `PCR_Z_PANIC_THRESHOLD` | `1.5` | Z-score limit triggering retail panic alerts |
| `VIX_HIGH_THRESHOLD` | `20.0` | High VIX threshold adjusting IVP gates |
| `SKEW_ELEVATED_THRESHOLD` | `5.0` | Put-Call 25D IV difference threshold for alerts |
| `CONFIDENCE_B4_MIN_TRADES` | `5` | Cold-start minimum trades guard for B4 |
| `RISK_FREE_RATE` | `0.0625` | Base rate for exact BSM calculations |

---

## Development

```bash
# Lint
.venv/bin/ruff check optdash/

# Format
.venv/bin/ruff format optdash/

# Type check
.venv/bin/mypy optdash/

# Tests
.venv/bin/pytest
```

---

## Tech Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| **Runtime** | Python | 3.11+ |
| **API** | FastAPI + Uvicorn | 0.135 / 0.41 |
| **Scheduler** | APScheduler | 3.11 |
| **Analytics DB** | DuckDB (in-process, Parquet views) | 1.5 |
| **Journal DB** | SQLite (WAL mode, FK, busy_timeout) | built-in |
| **Data Pipeline** | BigQuery, PyArrow, Pandas | 19 / 2.3 |
| **ML / Greeks** | py_vollib, scipy, numpy | 1.0 / 1.17 / 1.26 |
| **Frontend** | React 18 + TypeScript + Vite | 18 / 5.4 / 5.2 |
| **Styling** | TailwindCSS | 3.4 |
| **Charts** | Recharts | 2.12 |
| **State** | Zustand + React Query | 4.5 / 5.28 |
