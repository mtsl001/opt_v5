# OptDash — Part 6: Position Tracking & Alerts

---

## 1. Live Position Tracker (`ai/tracker.py`)

`track_open_positions(duck, jconn, trade_date, snap_time, gate_cache=None)` runs on every scheduler tick for every `ACCEPTED` trade.

### 1.1 Per-Tick Processing

```python
for trade in get_open_trades(jconn):
    current = fetch_strike_current(duck, trade_date, snap_time,
                                   trade["underlying"], trade["strike_price"],
                                   trade["expiry_date"], trade["option_type"])
    if not current:
        continue   # feed gap — skip snap, do not close

    ltp  = current["ltp"]
    pnl  = (ltp - actual_entry_price) / actual_entry_price * 100
```

`fetch_strike_current` is a shared helper in `analytics/query.py`. It returns `None` on feed gaps — the tracker never closes a position on a feed gap.

### 1.2 Trailing Stop (`TRAILING_SL_HIT`)

```python
ACTIVATION = settings.TRAILING_STOP_ACTIVATION    # default 0.20 (+20% PnL)
TRAIL_PCT   = settings.TRAILING_STOP_TRAIL_PCT     # configurable, default e.g. 0.10

if pnl_pct >= ACTIVATION * 100:
    trailing_sl_active = True
    trailing_sl = ltp * (1 - TRAIL_PCT)   # trail below current LTP

# Trailing SL can only ratchet up, never down
trailing_sl = max(trailing_sl, previous_trailing_sl or 0)

if trailing_sl_active and ltp <= trailing_sl:
    exit_reason = ExitReason.TRAILING_SL_HIT
```

### 1.3 IV Crush Detection

```python
iv_now   = current["iv"]
iv_entry = trade["entry_iv"]          # stored at ACCEPTED time

iv_drop_pct = (iv_entry - iv_now) / iv_entry * 100
severity    = (
    IVCrushSeverity.SEVERE if iv_drop_pct >= settings.IV_CRUSH_SEVERE_THRESHOLD else
    IVCrushSeverity.MILD   if iv_drop_pct >= settings.IV_CRUSH_MILD_THRESHOLD   else
    IVCrushSeverity.NONE
)
```

### 1.4 Gate Re-Evaluation (Gate Cache)

The gate is NOT re-computed per trade — it is pre-computed once per underlying per tick by `_build_gate_cache()` in the scheduler:

```python
gate_cache = _build_gate_cache(duck, trade_date, snap_time, jconn,
                               _gex_peak_cache=_gex_peak_cache)
```

`track_open_positions(gate_cache=gate_cache)` looks up `gate_cache[underlying]` directly — zero extra DuckDB calls.

### 1.5 Consecutive NO_GO Counter

```python
def _consecutive_no_go_count(snaps: list[dict]) -> int:
    count = 0
    for snap in reversed(snaps):
        if snap["gate_verdict"] == GateVerdict.NO_GO.value:
            count += 1
        else:
            break
    return count

if _consecutive_no_go_count(recent_snaps) >= settings.GATE_SUSTAINED_NO_GO_SNAPS:
    exit_reason = ExitReason.GATE_NO_GO
```

### 1.6 Exit Priority

When multiple conditions trigger simultaneously, exit reasons are prioritised:

```
1. SL_HIT           (hard stop — always first check)
2. TARGET_HIT
3. TRAILING_SL_HIT
4. GATE_NO_GO
5. IV_CRUSH (SEVERE)
```

The first matching condition triggers the close in that tick.

### 1.7 Atomic Snap + Close

When a closing snap is detected, the snap INSERT and `close_trade` UPDATE are committed atomically:

```python
is_closing = exit_reason is not None
snaps.insert_snap(jconn, snap_data, commit=not is_closing)  # hold transaction open
if is_closing:
    trades.update_trade(jconn, trade_id, close_fields)  # same transaction
    jconn.commit()   # single flush
```

This prevents a crash between INSERT and UPDATE leaving `status=ACCEPTED` with a final snap but no close record.

---

## 2. Gate Cache Optimisation (`_build_gate_cache`)

`get_environment_score()` makes ~11 DuckDB queries. Called once per open trade without caching = **11 × N queries** per tick. `_build_gate_cache()` reduces this to **11 × unique_underlyings**:

```python
for t in get_open_trades(jconn):
    if t["underlying"] not in cache:
        cache[t["underlying"]] = get_environment_score(
            duck, trade_date, snap_time, t["underlying"],
            direction=t["option_type"],
            _peak_cache=_gex_peak_cache,   # shared GEX peak cache within tick
        )
```

`_gex_peak_cache` is a plain `dict` populated in-place by `_get_gex_peak()` on first access per underlying within a tick. The shared cache spans both `_build_gate_cache()` and `generate_recommendation()` in the same tick — peak scans never repeat.

---

## 3. Alerts Engine (`analytics/alerts.py`)

`get_alerts(conn, trade_date, snap_time, underlying) → list[dict]`

Alerts fire when market signals cross thresholds **between two consecutive snaps** (transition-based, not level-based).

### 3.1 Alert Types

| Alert | Trigger |
|---|---|
| `GEX_FLIP` | `net_gex` sign changes (positive ↔ negative) |
| `VCOC_SPIKE_BULL` | `v_coc_15m >= VCOC_BULL_THRESHOLD` |
| `VCOC_SPIKE_BEAR` | `v_coc_15m <= VCOC_BEAR_THRESHOLD` |
| `PCR_EXTREME` | PCR divergence outside `[BEAR_THR, BULL_THR]` |
| `IV_SPIKE` | ATM IV jumps > 20% vs previous snap |
| `HIGH_CONVICTION_BEAR` | Put Skew STEEPENING simultaneously whilst VEX is Bearish |
| `APPROACHING_ZGL` | Spot within `ZGL_PROXIMITY_PCT` of Zero Gamma Level |
| `BELOW_ZGL` | Spot crosses below Zero Gamma Level |
| `VOLUME_SURGE` | Volume > `VOLUME_SPIKE_RATIO × rolling_median` |
| `DEALER_OCLOCK` | DTE=1 + time >= `DEALER_OCLOCK_START` on expiry weekday |

### 3.2 Severity

| Level | Examples |
|---|---|
| `HIGH` | `GEX_FLIP`, `DEALER_OCLOCK`, `VCOC_SPIKE_BEAR`, `HIGH_CONVICTION_BEAR`, `BELOW_ZGL` |
| `MEDIUM` | `PCR_EXTREME`, `IV_SPIKE`, `APPROACHING_ZGL` |
| `LOW` | `VOLUME_SURGE` |

### 3.3 Alert Format

```json
{
  "alert_type": "GEX_FLIP",
  "severity": "HIGH",
  "message": "GEX flipped negative — gamma support removed",
  "value": -1.23,
  "snap_time": "11:05"
}
```

---

## 4. Volume Velocity (`analytics/microstructure.py`)

`get_volume_velocity(conn, trade_date, snap_time, underlying) → dict`

Detects unusual volume by comparing current snap to a rolling historical median:

```python
rolling_snaps = last N snaps (VOLUME_LOOKBACK_SNAPS, default 6)
rolling_med   = median(snap_volumes)
ratio         = current_volume / max(rolling_med, 1)

signal = "SPIKE" if ratio >= VOLUME_SPIKE_RATIO else "NORMAL"

# Heatmap matrix: option_type × strike_price → volume intensity
```

Returns per-strike volume intensities for the heatmap panel in the frontend. Errors caught and counted via `record_error("get_volume_velocity")`.
