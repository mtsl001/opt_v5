# OptDash — Part 1: Architecture & System Overview

---

## 1. Introduction

OptDash is a real-time options analytics and AI-powered trade recommendation engine for Indian equity derivatives (NSE). It processes live intraday options chain data every 5 minutes via BigQuery, computes multi-dimensional market signals, scores the trading environment, and generates fully-explained trade recommendations with dynamic stop-loss management.

Designed for a single retail/semi-professional trader who wants institutional-grade signal intelligence without manually interpreting 10+ data dimensions simultaneously.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          OptDash v2.0                           │
│                                                                 │
│  ┌──────────────┐    ┌───────────────┐    ┌───────────────────┐ │
│  │  BigQuery    │    │  DuckDB       │    │   FastAPI         │ │
│  │  BQ Feed     │───▶│  (in-process) │───▶│   REST + WS       │ │
│  │  (live pull) │    │  Parquet view │    │   API Layer       │ │
│  └──────────────┘    └───────────────┘    └───────────────────┘ │
│         │                   │                       │            │
│         │           ┌───────┴──────┐       ┌────────┴─────────┐ │
│         │           │  APScheduler │       │  WebSocket        │ │
│         │           │  Async Tick  │       │  Live Feed (/ws)  │ │
│         │           └───────┬──────┘       └────────┬─────────┘ │
│         │                   │                       │            │
│   ┌─────▼─────┐     ┌───────▼──────┐               │            │
│   │  Parquet  │     │  AI Engine   │               │            │
│   │  (files)  │     │  Recommender │               │            │
│   └───────────┘     └───────┬──────┘               │            │
│                             │                       │            │
│                     ┌───────▼──────┐               │            │
│                     │  SQLite      │◀──────────────┘            │
│                     │  Journal DB  │                             │
│                     └─────────────┘                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Technology Stack

| Layer | Technology | Version | Purpose |
|---|---|---|---|
| Language | Python | 3.11+ | Core runtime |
| Web Framework | FastAPI | ≥0.111 | REST API + WebSocket |
| ASGI Server | Uvicorn (standard) | ≥0.29 | Production server |
| Analytics DB | DuckDB | ≥0.10, <2.0 | In-process columnar analytics |
| Journal DB | SQLite 3 | WAL mode | Trade state, positions, learning |
| Scheduler | APScheduler | ≥3.10, <4.0 | Async market tick |
| Config | Pydantic-Settings | ≥2.2, <3.0 | `.env` typed configuration |
| Logging | Loguru | ≥0.7 | Structured logging |
| Options Math | py_vollib | ≥1.0 | Black-Scholes Greeks |
| Data Science | pandas, numpy, scipy | pinned | Data processing |
| Data Pipeline | PyArrow, filelock | pinned | Parquet read/write |
| Build | Hatchling | – | PEP 517 build backend |
| Frontend | React 18, TypeScript, Vite, TailwindCSS, Recharts | – | UI |

---

## 4. Repository Structure

```
OptDash/
├── optdash/
│   ├── config.py                  # All settings — pydantic-settings from .env
│   ├── scheduler.py               # APScheduler async tick definition
│   ├── utils.py                   # Shared helpers: snap_to_min()
│   ├── metrics.py                 # Thread-safe analytics error counters
│   ├── models/
│   │   └── enums.py               # All enumerations (Direction, GateVerdict, …)
│   ├── pipeline/
│   │   ├── duckdb_gateway.py      # DuckDB :memory: + LockedConn + Parquet view
│   │   ├── processor.py           # BQ data enrichment → Parquet writer pipeline
│   │   ├── writer.py              # PyArrow Parquet writer with filelock
│   │   └── incremental.py         # Live incremental BQ snap pull per tick
│   ├── analytics/
│   │   ├── query.py               # Shared: fetch_strike_current()
│   │   ├── gex.py                 # GEX, regime, max pain, spot summary
│   │   ├── coc.py                 # CoC, V_CoC, ATM OBI, Futures OBI
│   │   ├── iv.py                  # IVR, IVP, HV20, term structure
│   │   ├── pcr.py                 # PCR divergence, smoothed OBI
│   │   ├── vex_cex.py             # Vanna/Charm exposure, dealer o'clock
│   │   ├── screener.py            # 7-factor S_score strike ranker
│   │   ├── environment.py         # 11-point environment gate
│   │   ├── microstructure.py      # Volume velocity heatmap
│   │   ├── pnl.py                 # Theta-SL curve, Greek PnL attribution
│   │   └── alerts.py              # Real-time alert engine
│   ├── ai/
│   │   ├── direction.py           # 5-signal weighted directional vote
│   │   ├── confidence.py          # 4-bucket confidence scorer (0–100)
│   │   ├── pre_flight.py          # 7 hard blocking rules
│   │   ├── quality.py             # A/B/C/D quality grade
│   │   ├── narrative.py           # Template-based trade narrative
│   │   ├── recommender.py         # Full recommendation orchestrator
│   │   ├── tracker.py             # Live position tracker + trailing stop
│   │   ├── shadow_tracker.py      # Hypothetical rejected-trade tracking
│   │   ├── eod.py                 # Atomic EOD force-close + shadow finalize
│   │   ├── journal/
│   │   │   ├── schema.py          # SQLite DDL, migrations, open_journal()
│   │   │   ├── trades.py          # Trades DAO (CRUD, column whitelist)
│   │   │   ├── snaps.py           # Position snaps DAO
│   │   │   └── shadow.py          # Shadow trades DAO
│   │   └── learning/
│   │       ├── stats.py           # Win-rate stats, threshold performance
│   │       └── report.py          # Comprehensive learning report
│   └── api/
│       ├── app.py                 # FastAPI factory + /health endpoint
│       ├── deps.py                # DB lifecycle (DuckDB + SQLite)
│       ├── validators.py          # Shared Pydantic types (SnapTime)
│       └── routers/
│           ├── market.py          # /api/market/* endpoints
│           ├── micro.py           # /api/micro/* endpoints
│           ├── screener.py        # /api/screener/* endpoints
│           ├── ai.py              # /api/ai/* endpoints
│           └── ws.py              # WebSocket /ws/live
├── frontend/                      # Vite + React 18 + TypeScript + TailwindCSS
├── pyproject.toml
├── .env.example
├── run_api.py                     # Entry point
└── Docs/
```

---

## 5. Two-Database Architecture

### 5.1 DuckDB — Columnar Analytics

- **Connection**: In-process `:memory:`, wrapped in `LockedConn` proxy (thread-safe)
- **Data source**: `data/processed/trade_date=YYYY-MM-DD/*.parquet` (hive-partitioned)
- **Access**: Read-only analytical queries (GROUP BY, window functions, aggregations)
- **Thread safety**: `LockedConn` acquires `_view_lock` (RLock) for every `.execute()` call; `refresh_views()` acquires it exclusively during view swap
- **Schema validation**: `REQUIRED_COLUMNS` checked on startup and EOD refresh
- **Lifecycle**: `duckdb_gateway.startup()` → `shutdown()`

### 5.2 SQLite — Journal Database

- **Connection**: File-based, WAL mode, `check_same_thread=False`
- **Factory**: Always opened via `schema.open_journal(path)` which sets `WAL + foreign_keys=ON + busy_timeout=5000`
- **Two connections**: API requests use `app.state.journal`; scheduler tick uses `app.state.scheduler_journal` — never shared across threads
- **Tables**: `trades`, `position_snaps`, `shadow_trades`, `shadow_snaps`
- **Migrations**: `_run_migrations()` adds columns/indexes idempotently

---

## 6. `options_data` Parquet Schema

All analytics queries target a single DuckDB view `options_data`:

| Column | Type | Description |
|---|---|---|
| `trade_date` | VARCHAR | `YYYY-MM-DD` (Hive partition key) |
| `snap_time` | VARCHAR | `HH:MM` (every scheduler interval) |
| `underlying` | VARCHAR | `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `NIFTYNXT50` |
| `instrument_type` | VARCHAR | `OPT` or `FUT` |
| `option_type` | VARCHAR | `CE` or `PE` |
| `strike_price` | DOUBLE | Strike price |
| `expiry_date` | VARCHAR | `YYYY-MM-DD` |
| `expiry_tier` | VARCHAR | `TIER1` (nearest), `TIER2`, `TIER3` |
| `dte` | INTEGER | Days to expiry |
| `ltp` | DOUBLE | Last traded price |
| `bid_qty` | DOUBLE | Bid quantity (from total_buy_qty) |
| `ask_qty` | DOUBLE | Ask quantity (from total_sell_qty) |
| `volume` | DOUBLE | Volume at snap |
| `oi` | DOUBLE | Open interest |
| `iv` | DOUBLE | Implied volatility (annualised, decimal) |
| `delta` | DOUBLE | Option delta |
| `gamma` | DOUBLE | Option gamma |
| `theta` | DOUBLE | Option theta (daily) |
| `vega` | DOUBLE | Option vega |
| `spot` | DOUBLE | Underlying spot price |
| `fut_price` | DOUBLE | Nearest futures price |
| `gex` | DOUBLE | Gamma Exposure (pre-computed) |
| `vex` | DOUBLE | Vanna Exposure (pre-computed) |
| `cex` | DOUBLE | Charm Exposure (pre-computed) |

> `union_by_name=true` is set so older Parquet files with different schemas fill missing columns with NULL. `REQUIRED_COLUMNS` validation on startup catches schema gaps immediately.

---

## 7. Application Startup Sequence

```
run_api.py → uvicorn
       │
       ▼
[1] FastAPI lifespan begins
       │
       ▼
[2] duckdb_gateway.startup()
    ├─ Create :memory: DuckDB connection
    ├─ PRAGMA threads={DUCKDB_THREADS}         (default 4)
    ├─ PRAGMA memory_limit='{DUCKDB_MEMORY_LIMIT}'  (default '2GB')
    ├─ _build_rolling_globs() — IST-aware rolling window
    ├─ CREATE OR REPLACE VIEW options_data AS read_parquet(...)
    └─ _validate_view_schema() — crash on missing REQUIRED_COLUMNS
       │
       ▼
[3] deps.startup(app)
    ├─ Open two SQLite connections via open_journal()
    │   ├─ app.state.journal            ← API thread pool
    │   └─ app.state.scheduler_journal  ← scheduler tick
    ├─ init_db() on both — CREATE TABLE IF NOT EXISTS + migrations
    └─ Wrap both in LockedConn
       │
       ▼
[4] create_scheduler(journal_conn) → scheduler.start()
    └─ AsyncIOScheduler (IST timezone)
       IntervalTrigger: seconds=SCHEDULER_INTERVAL_SECONDS
       max_instances=1, coalesce=True
       │
       ▼
[5] App ready — accepting requests

[6] Each tick: tick()
    ├─ Step 0: run_incremental_pull(duck_conn=duck)  [BQ pull, non-blocking]
    ├─ EOD sweep (once/day, at EOD_SWEEP_TIME, set done_flags)
    ├─ Step 1: expire_stale_recommendations()
    ├─ Step 2: _build_gate_cache() — pre-compute 1 gate call per underlying
    ├─ Step 3: generate_recommendation() × UNDERLYINGS
    ├─ Step 4: track_open_positions(gate_cache=...)
    └─ Step 5: track_shadow_positions()
```

---

## 8. Supported Underlyings

| Underlying | Full Name | Default Expiry Day |
|---|---|---|
| `NIFTY` | Nifty 50 | Thursday |
| `BANKNIFTY` | Bank Nifty | Thursday |
| `FINNIFTY` | Nifty Financial Services | Tuesday |
| `MIDCPNIFTY` | Nifty Midcap Select | Monday |
| `NIFTYNXT50` | Nifty Next 50 | Friday |

Expiry weekdays are used by `_is_dealer_oclock()` to correctly identify expiry-day charm flow. Per-underlying VEX and CEX thresholds are configured in `settings.VEX_THRESHOLDS`, `settings.CEX_CHARM_THRESHOLD`, `settings.CEX_VANNA_THRESHOLD`.

---

## 9. Market Sessions

| Session | Default Range | Confidence Effect |
|---|---|---|
| `OPENING` | 09:15 – 10:15 | Standard |
| `MIDMORNING` | 10:15 – 11:30 | Standard |
| `MIDDAY_CHOP` | 11:30 – 13:00 | –10 penalty |
| `AFTERNOON` | 13:00 – 14:30 | Standard |
| `CLOSING_CRUSH` | 14:30 – 15:30 | Capped at 60 |

---

## 10. Key Design Principles

1. **No LLM dependency** — all narratives are template-based, deterministic, data-backed
2. **Parameterised queries** — all DuckDB/SQLite queries use `?` or `$1` binding
3. **Explicit None guards** — IVP=0, LTP=0, `actual_entry_price=0` all handled explicitly; no `x or default` falsy coercions
4. **Thread-safe DuckDB** — `LockedConn` proxy serialises all analytics via `_view_lock` (RLock)
5. **SQLite thread isolation** — API and scheduler each own their own connection; never shared
6. **Column whitelists** — all DAO INSERT/UPDATE builders validate keys against `_ALLOWED_*_COLS` frozensets
7. **Idempotent init** — `init_db()` and `_run_migrations()` are safe to call on every connection
8. **Atomic EOD** — `eod_force_close()` and `finalize_all_shadows()` use `commit=False` + single `commit()` for all-or-nothing writes
9. **IST-aware time** — all timestamps, date boundaries, and scheduler logic use `ZoneInfo("Asia/Kolkata")`
10. **Error counter exposure** — all analytics `except` blocks call `record_error(fn_name)`; counts exposed via `/health`
