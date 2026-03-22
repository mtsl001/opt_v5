# OptDash — Part 9: API Reference

The FastAPI application is created via `api/app.py::create_app()` and served by `run_api.py` on `uvicorn`. All endpoints are prefixed correctly and return JSON.

---

## 1. Application Setup

```python
app = create_app()    # FastAPI factory
app.include_router(market_router,   prefix="/api/market")
app.include_router(micro_router,    prefix="/api/micro")
app.include_router(screener_router, prefix="/api/screener")
app.include_router(ai_router,       prefix="/api/ai")
app.add_api_websocket_route("/ws/live", ws_endpoint)
```

CORS is configured from `settings.CORS_ORIGINS` (default: `http://localhost:5173`, `http://localhost:3000`).

---

## 2. System Endpoints

### `GET /health`

Returns service health including analytics error counts:

```json
{
  "status": "ok",
  "uptime_s": 3640,
  "duck_db_ok": true,
  "journal_ok": true,
  "analytics_errors": {
    "get_net_gex": 0,
    "get_coc_latest": 1,
    "get_ivr_ivp": 0,
    ...
  }
}
```

`analytics_errors` is populated by `record_error(fn_name)` calls in analytics module `except` blocks and exposed via `get_error_counts()` from `optdash/metrics.py`.

---

## 3. Market Data Endpoints (`routers/market.py`)

All endpoints accept `trade_date`, `snap_time`, and `underlying` as query parameters.

| Method | Path | Description |
|---|---|---|
| GET | `/api/market/spot` | OHLC spot summary for the day |
| GET | `/api/market/gex` | GEX series (gex_all_B, gex_near_B, regime, pct_of_peak) |
| GET | `/api/market/gex/current` | Single-snap GEX reading |
| GET | `/api/market/coc` | CoC + V_CoC full-day series |
| GET | `/api/market/coc/current` | Single-snap CoC reading |
| GET | `/api/market/environment` | 11-point environment gate |
| GET | `/api/market/max-pain` | Max pain strike + distance from current spot |
| GET | `/api/market/dates` | Available trading dates |
| GET | `/api/market/snaps` | Available snap times for a date |

**Example: Environment Gate**

```
GET /api/market/environment
    ?trade_date=2026-03-12&snap_time=10:15&underlying=NIFTY&direction=CE
```

Returns:
```json
{
  "score": 9,
  "maxscore": 11,
  "verdict": "GO",
  "session": "MIDMORNING",
  "conditions": {
    "gex_regime": {"met": true, "value": "NEGATIVE_TREND", "points": 1, ...},
    ...
  }
}
```

---

## 4. Microstructure Endpoints (`routers/micro.py`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/micro/pcr` | PCR vol + OI + divergence signal |
| GET | `/api/micro/vex-cex` | VEX/CEX series + dealer o'clock |
| GET | `/api/micro/vex-cex/by-strike` | Per-strike VEX and CEX breakdowns formatted for UI Heatmaps |
| GET | `/api/micro/volume-velocity` | Volume heatmap per strike |
| GET | `/api/micro/alerts` | Live alerts list |

---

## 5. Screener Endpoints (`routers/screener.py`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/screener/strikes` | Top-N strikes ranked by S_score (7-factor) |
| GET | `/api/screener/term-structure` | ATM IV per expiry tier with shape |

**Screener parameters:**
- `underlying`, `trade_date`, `snap_time`
- `direction` — optional; filters to CE or PE strikes only
- `top_n` — default `SCREENER_TOP_N` (20)

---

## 6. AI Engine Endpoints (`routers/ai.py`)

### Read Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/ai/recommendation/latest` | Latest pending trade card for an underlying |
| GET | `/api/ai/position/live` | Current ACCEPTED position + latest snap |
| GET | `/api/ai/position/snaps/{trade_id}` | Full snap history for a trade |
| GET | `/api/ai/journal/history` | Paginated trade history |
| GET | `/api/ai/learning/report` | Learning report (cached 60s) |

### Write Endpoints

| Method | Path | Body | Action |
|---|---|---|---|
| POST | `/api/ai/accept` | `{trade_id, snap_time, actual_entry_price?}` | Accept recommendation |
| POST | `/api/ai/reject` | `{trade_id, reason, note?}` | Reject → create shadow |
| POST | `/api/ai/close-trade` | `{trade_id, exit_price, snap_time}` | Manual close |

**Accept request:**
```json
{
  "trade_id": 42,
  "snap_time": "10:15",
  "actual_entry_price": 87.5   // optional; defaults to entry_premium
}
```

`actual_entry_price` validation: if provided, must be `> 0`. All PnL calculations use `actual_entry_price`, not the original `entry_premium`.

**Reject request:**
```json
{
  "trade_id": 42,
  "reason": "LOW_CONFIDENCE",
  "note": "IV looks stretched"
}
```

Shadow trade is created immediately on reject, using the recommendation's `sl_price` and `target_price`.

### `SnapTime` Type

`snap_time` fields accept `HH:MM` (08:00–23:59), validated by `validators.py::SnapTime`:

```python
SnapTime = Annotated[str, Field(pattern=r"^\d{2}:\d{2}$")]
```

---

## 7. WebSocket Feed (`routers/ws.py`)

```
WS /ws/live
   ?underlying=NIFTY
   &trade_date=2026-03-12
```

Emits a JSON payload every `WS_INTERVAL_SECONDS` (default 5) with:
- Latest spot, GEX, CoC, PCR, VEX snapshot
- Current open position (if any)
- Latest pending recommendation (if any)
- Active alerts

The WebSocket uses `asyncio.sleep(WS_INTERVAL_SECONDS)` — non-blocking, runs on the event loop.

---

## 8. Dependency Injection (`api/deps.py`)

```python
def get_duck()    -> LockedConn:      return app.state.duck
def get_journal() -> sqlite3.Connection: return app.state.journal
```

`get_duck()` and `get_journal()` are FastAPI `Depends()` injectors. The two connections are opened at app startup and closed at shutdown — never per-request.

The scheduler uses `app.state.scheduler_journal` (a separate SQLite connection opened by `deps.startup()`) so the scheduler tick never shares a `sqlite3.Connection` with the API thread pool.

---

## 9. Error Handling Strategy

| Layer | On Error |
|---|---|
| Analytics function | `record_error(fn_name)`, return `{}` or `[]` |
| Recommendation | Log failures, return `None` (no trade issued) |
| API endpoint | Return `200 {}` for missing analytics rather than `500` |
| Scheduler tick | Per-step `try/except`; one step failure never kills the tick |
| EOD sweep | Per-function `try/except`; `done_flags` set unconditionally |
