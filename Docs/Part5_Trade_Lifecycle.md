# OptDash — Part 5: Trade Lifecycle

A trade moves through a strict, one-way state machine. Every transition is written to SQLite, auditable, and irreversible. The journal is the single source of truth for all positions.

---

## 1. State Machine

```
GENERATED ──► ACCEPTED ──► CLOSED
         │
         ├──► REJECTED  (→ shadow trade created)
         │
         └──► EXPIRED   (→ shadow trade created)
```

| Status | Set By | Description |
|---|---|---|
| `GENERATED` | `recommender.generate_recommendation()` | Recommendation written; awaiting user action |
| `ACCEPTED` | `POST /api/ai/accept` | Position live; tracker records snaps every tick |
| `REJECTED` | `POST /api/ai/reject` | User rejected; shadow trade created |
| `EXPIRED` | `expire_stale_recommendations()` | Not actioned within `AI_EXPIRY_MAX_SNAPS` snaps |
| `CLOSED` | `tracker.track_open_positions()` or `eod.eod_force_close()` | Terminal state |

Only one `ACCEPTED` trade per underlying is permitted — enforced at generation time in `recommender.py`.

---

## 2. Journal Schema (`ai/journal/schema.py`)

### 2.1 `trades` Table

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | INTEGER PK | N | Auto-increment primary key |
| `underlying` | TEXT | N | `NIFTY`, `BANKNIFTY`, etc. |
| `trade_date` | TEXT | N | `YYYY-MM-DD` |
| `option_type` | TEXT | N | `CE` or `PE` |
| `strike_price` | REAL | N | Recommended strike |
| `expiry_date` | TEXT | N | Option expiry `YYYY-MM-DD` |
| `dte` | INTEGER | N | Days to expiry at generation |
| `entry_premium` | REAL | N | LTP at recommendation time |
| `sl_price` | REAL | N | Initial SL = `entry × (1 – AI_SL_PCT)` |
| `target_price` | REAL | N | Initial target = `entry × AI_TARGET_MULT` |
| `actual_entry_price` | REAL | Y | Fill price set at `POST /accept` |
| `confidence` | INTEGER | N | 0–100 at generation |
| `quality_grade` | TEXT | N | A / B / C / D |
| `gate_score` | INTEGER | N | Environment gate score at generation |
| `status` | TEXT | N | `TradeStatus` enum value |
| `exit_reason` | TEXT | Y | `ExitReason` enum value |
| `exit_premium` | REAL | Y | LTP at close |
| `final_pnl_pct` | REAL | Y | `(exit – actual_entry) / actual_entry × 100` |
| `accepted_at` | TEXT | Y | IST ISO timestamp |
| `closed_at` | TEXT | Y | IST ISO timestamp |
| `session` | TEXT | Y | `MarketSession` value at generation |
| `narrative` | TEXT | Y | Full narrative text |
| `signals` | TEXT | Y | JSON list of signal dicts |

### 2.2 `position_snaps` Table

One row per scheduler tick per `ACCEPTED` trade.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `trade_id` | INTEGER FK | References `trades.id` |
| `snap_time` | TEXT | `HH:MM` |
| `ltp` | REAL | Current option LTP |
| `pnl_pct` | REAL | Running PnL vs `actual_entry_price` |
| `gate_score` | INTEGER | Gate score at this snap |
| `gate_verdict` | TEXT | `GO` / `WAIT` / `NO_GO` |
| `iv_crush_severity` | TEXT | `NONE` / `MILD` / `SEVERE` |
| `trailing_sl_active` | INTEGER | 1 if trailing stop engaged |
| `trailing_sl` | REAL | Current trailing SL level |

### 2.3 `shadow_trades` Table

Created for every `REJECTED` or `EXPIRED` trade.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `trade_id` | INTEGER FK | References `trades.id` |
| `underlying` | TEXT | |
| `entry_premium` | REAL | LTP at rejection/expiry time |
| `sl_price` | REAL | Hypothetical SL (stored at creation) |
| `target_price` | REAL | Hypothetical target (stored at creation) |
| `is_closed` | INTEGER | 0 = tracking active, 1 = closed |
| `final_pnl_pct` | REAL | Hypothetical terminal PnL |
| `outcome` | TEXT | `ShadowOutcome` enum value |

> `sl_price` and `target_price` are stored at shadow creation time (not recomputed from live config) so config changes don't retroactively move the goalposts on in-flight hypotheticals.

### 2.4 `shadow_snaps` Table

One row per tick per active shadow trade. Same structure as `position_snaps` with `shadow_id FK → shadow_trades.id`.

---

## 3. Trade CRUD (`ai/journal/trades.py`)

All update operations are built via a column whitelist:

```python
_ALLOWED_UPDATE_COLS = frozenset({"status", "actual_entry_price", "accepted_at",
                                  "exit_premium", "exit_reason", "final_pnl_pct",
                                  "closed_at"})
```

Any attempt to update an unlisted column raises `ValueError`. This prevents unintended mutation of immutable trade fields.

**Key functions:**

| Function | Action |
|---|---|
| `create_trade(jconn, fields)` | INSERT with `status=GENERATED` |
| `get_latest_generated(jconn, underlying)` | Fetch pending recommendation |
| `get_open_trade(jconn, underlying)` | Fetch the single ACCEPTED trade |
| `get_open_trades(jconn)` | All ACCEPTED trades |
| `update_trade(jconn, trade_id, updates)` | Whitelist-guarded UPDATE |
| `get_trade_history(jconn, page, per_page, underlying, status)` | Paginated CLOSED/all history |
| `expire_stale_recommendations(jconn, trade_date, snap_time)` | Mark old GENERATED → EXPIRED |

---

## 4. Position Snaps DAO (`ai/journal/snaps.py`)

```python
insert_snap(jconn, snap_dict, commit=True)
get_snaps_for_trade(jconn, trade_id) -> list[dict]
get_recent_snaps(jconn, trade_id, n) -> list[dict]
```

`commit=False` mode is used when the snap INSERT and a subsequent trade close must be atomic (committed together by the caller).

---

## 5. Shadow Tracking

### Creation

`POST /api/ai/reject` creates a shadow trade immediately at rejection time, storing the hypothetical `sl_price` and `target_price` from the recommendation.

`expire_stale_recommendations()` sets `status=EXPIRED`; the shadow is created at expiry time.

### Tracking (`ai/shadow_tracker.py`)

Every scheduler tick, for each active shadow:

```python
ltp = fetch_strike_current(conn, trade_date, snap_time, ...)["ltp"]
pnl = (ltp - entry_premium) / entry_premium * 100

hit_sl  = ltp <= sl_price  (or fallback to live config for legacy rows)
hit_tgt = ltp >= target_price

if hit_sl or hit_tgt:
    shadow.insert_shadow_snap(jconn, ..., commit=False)   # stays uncommitted
    shadow.close_shadow(jconn, shadow_id, ...)             # commits both atomically
```

Atomic commit prevents the "zombie shadow" bug where a crash between INSERT and UPDATE left `is_closed=0` forever.

### Outcome Classification (`_classify_shadow_outcome`)

| Outcome | Condition |
|---|---|
| `CLEAN_MISS` | pnl_pct > 30% — costly rejection |
| `GOOD_SKIP` | pnl_pct < –20% — correct rejection |
| `BREAK_EVEN` | abs(pnl_pct) < 5% |
| `RISKY_MISS` | Everything else |

---

## 6. EOD Sweep (`ai/eod.py`)

Fired once per day at `EOD_SWEEP_TIME` (default 15:25), uses done_flags dict to prevent re-entry on subsequent ticks.

```python
def eod_force_close(duck, jconn, trade_date):
    open_trades = get_open_trades(jconn)
    for t in open_trades:
        ltp = fetch_strike_current(duck, ...) or {"ltp": t["entry_premium"]}
        pnl = (ltp - actual_entry) / actual_entry * 100
        # Atomic: INSERT snap (commit=False) + UPDATE trade + commit()
        ...

def finalize_all_shadows(duck, jconn, trade_date):
    # Close all shadows that are still open at EOD
    shadows = shadow.get_all_unclosed_shadows(jconn, trade_date)
    for s in shadows:
        outcome = _classify_shadow_outcome(pnl_pct)
        shadow.close_shadow(jconn, s["id"], {...})
```

Each step is wrapped in its own `try/except` in the scheduler; `done_flags[trade_date]` is set unconditionally so a failure doesn't cause re-entry.

---

## 7. Exit Reasons

| Code | Trigger |
|---|---|
| `SL_HIT` | `ltp <= trade.sl_price` |
| `TARGET_HIT` | `ltp >= trade.target_price` |
| `TRAILING_SL_HIT` | Trailing stop activated and `ltp <= trailing_sl` |
| `GATE_NO_GO` | N consecutive `NO_GO` gate snaps (default N=2) |
| `IV_CRUSH` | Severe IV drop detected |
| `MANUAL` | User via `POST /api/ai/close-trade` |
| `EOD_FORCE` | EOD sweep |
