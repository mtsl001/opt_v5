# OptDash — Part 7: Environment Gate

The Environment Gate is the system's primary filter for issuing or holding trades. It evaluates market conditions and returns a structured score with per-condition detail for auditability.

---

## 1. Function Signature

```python
def get_environment_score(
    conn:        duckdb.DuckDBPyConnection,
    trade_date:  str,
    snap_time:   str,
    underlying:  str,
    direction:   str | None = None,   # "CE", "PE", or None
    _peak_cache: dict | None = None,  # shared GEX peak cache within a tick
) -> dict
```

---

## 2. Return Format

```python
{
    "score":    int,          # 0..11
    "maxscore": int,          # 11 (settings.GATE_MAX_SCORE)
    "verdict":  "GO" | "WAIT" | "NO_GO",
    "session":  str,          # MarketSession value
    "conditions": {
        "<key>": {
            "met":       bool,
            "value":     Any,
            "threshold": str,
            "points":    int,
            "note":      str,
            "is_bonus":  bool
        },
        ...
    }
}
```

The `conditions` dict is rendered verbatim in the UI gate panel and stored in recommendation logs. Every gate decision is fully auditable.

---

## 3. Verdict Thresholds

| Verdict | Score | Config | Effect |
|---|---|---|---|
| `GO` | ≥ `GATE_GO_THRESHOLD` | default 7 | Environment acceptable |
| `WAIT` | ≥ `GATE_WAIT_THRESHOLD` | default 5 | Mixed signals |
| `NO_GO` | < `GATE_WAIT_THRESHOLD` | — | Hostile; no trade, exit position |

`GATE_MAX_SCORE = 11` (9 core + 2 bonus).

---

## 4. The 11 Conditions

### Core Conditions — 9 pts

| # | Key | Signal Source | Pass Condition | Points |
|---|---|---|---|---|
| C1 | `gex_regime` | `get_net_gex()` | regime is `NEGATIVE_TREND` or `POSITIVE_DECLINING` | 1 |
| C2 | `vcoc_signal` | `get_coc_latest()` | `abs(v_coc_15m) >= VCOC_BULL_THRESHOLD` (default 10.0) | 1 |
| C3 | `futures_flow` | `get_futures_obi()` | `abs(fut_obi) >= OBI_THRESHOLD` (default 0.10) | 1 |
| C4 | `pcr_divergence` | `get_pcr()` | `pcr_div >= PCR_DIV_BULL_THR` or `<= PCR_DIV_BEAR_THR` | 1 |
| C5 | `ivp_cheap` | `get_ivr_ivp()` | `(ivp if ivp is not None else 100) < 50` | 1 |
| C6 | `obi_signal` | `get_atm_obi()` | `abs(obi) >= OBI_THRESHOLD` (default 0.10) | 1 |
| C7 | `vcoc_direction` | `get_coc_latest()` | V_CoC sign aligns with `direction` (CE → bull, PE → bear) | 1 |
| C8 | `term_structure` | `get_term_structure()` | shape is `CONTANGO` | 1 |
| C9 | `session_ok` | `get_market_session()` | session is not `MIDDAY_CHOP` | 1 |

### Bonus Conditions — 2 pts (require `direction` param)

| # | Key | Pass Condition |
|---|---|---|
| C10 | `vex_aligned` | VEX signal aligns with `direction` (CE→VEX_BULLISH, PE→VEX_BEARISH) |
| C11 | `not_dealer_oclock` | `not dealer_oclock` — DTE ≤ 1 AND time ≥ `DEALER_OCLOCK_START` AND correct expiry weekday |

> Gates C7, C10, C11 require `direction != None`. When `direction is None`, those conditions score 0 and are noted as "Pass direction=CE/PE to evaluate".

---

## 5. IVP None Guard

```python
ivp_val = iv_data.get("ivp")
# Guard: None when < 20 trading days of history
# Use 100 (conservative "not cheap") so gate C5 fails safely on fresh deploys
ivp_for_gate = ivp_val if ivp_val is not None else 100.0
met = ivp_for_gate < 50
```

`ivp or 100` is **not used** — falsy coercion would misclassify IVP=0 (cheapest IV in history) as "not cheap".

---

## 6. Scoring

```python
score   = sum(c["points"] for c in conditions.values())
verdict = (
    GateVerdict.GO   if score >= settings.GATE_GO_THRESHOLD  else
    GateVerdict.WAIT if score >= settings.GATE_WAIT_THRESHOLD else
    GateVerdict.NO_GO
)
```

---

## 7. Gate in Recommendation Flow

1. **Step 1 — NOGO abort**: `get_environment_score()` called in recommender; if `verdict == NO_GO`, generation stops immediately.
2. **Pre-flight PF-1**: `gate_score >= PREFLIGHT_MIN_GATE_SCORE` (default 5) — a separate lower bar than `GO`.
3. **DTE=1 bar**: When nearest expiry DTE ≤ 1, pre-flight requires `gate_score >= PREFLIGHT_DTE1_MIN_GATE` (default 7).

---

## 8. Gate in Position Tracking

Gate is pre-computed once per unique underlying per tick (`_build_gate_cache`) and passed into `track_open_positions`:

```python
nogo_count = _consecutive_no_go_count(recent_snaps)
if nogo_count >= settings.GATE_SUSTAINED_NO_GO_SNAPS:   # default 2
    close_trade(exit_reason=ExitReason.GATE_NO_GO)
```

This exits positions from structurally deteriorating environments even before the premium hits the SL price.

---

## 9. API Endpoint

```
GET /api/market/environment
    ?trade_date=2026-03-12
    &snap_time=10:15
    &underlying=NIFTY
    &direction=CE          # optional; required for C7, C10, C11
```
