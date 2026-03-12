# OptDash — Part 4: AI Engine

All AI logic lives in `optdash/ai/`. It is entirely **rule-based and template-driven** — no LLM, no external model, no internet calls. Every decision is explainable, auditable, and reproducible.

---

## 1. Recommendation Generation Flow (`ai/recommender.py`)

```
generate_recommendation(duck, jconn, trade_date, snap_time, underlying)
        │
        ├─ [Guard] ACCEPTED trade exists for underlying? → return None
        ├─ [Guard] GENERATED (pending) recommendation exists? → return None
        │
        ├─ [1] get_environment_score()     → gate dict (score, verdict, conditions)
        │       uses _peak_cache shared within tick
        ├─ [2] get_market_session()        → MarketSession enum
        ├─ [3] get_directional_bias()      → direction, margin, signals[]
        ├─ [4] get_ivr_ivp()              → ivr, ivp, shape, hv20
        ├─ [5] get_net_gex()              → gex, regime, pct_of_peak, spot
        ├─ [6] get_vex_cex_current()      → vex_signal, cex_signal, dealer_oclock
        ├─ [7] get_max_pain()             → max_pain, distance_pct
        ├─ [8] get_nearest_expiry()       → expiry_date, dte
        │
        ├─ [Guard] direction == NEUTRAL? → return None
        ├─ [Guard] nearest_expiry is None? → return None
        │
        ├─ [9]  get_strikes(direction=direction)  → top-N strikes by S_score
        ├─ [Guard] No candidates for direction? → return None
        ├─ strike = candidates[0]   (highest S_score)
        ├─ [Guard] strike.ltp <= 0? → return None
        │
        ├─ [10] get_session_stats()       → win_rate, total_trades, is_fallback
        ├─ [11] compute_confidence()      → confidence 0–100, buckets, session_adjusted
        ├─ [12] run_pre_flight()          → passed bool, failures list
        ├─ [Guard] Pre-flight failed? → log failures, return None
        │
        ├─ SL     = round(ltp × (1 – AI_SL_PCT), 2)
        ├─ Target = round(ltp × AI_TARGET_MULT, 2)
        ├─ [13] compute_quality_score()  → grade A/B/C/D, raw_score
        ├─ [14] build_narrative()        → trade explanation text
        ├─ [15] trades.create_trade()    → trade_id (SQLite INSERT)
        │
        └─ return trade dict
```

---

## 2. Directional Bias (`ai/direction.py`)

### 2.1 Five Independent Signals

| Signal | CE Condition | PE Condition |
|---|---|---|
| **S1: V_CoC** | `v_coc >= VCOC_BULL_THRESHOLD` | `v_coc <= VCOC_BEAR_THRESHOLD` |
| **S2: PCR Divergence** | `pcr_div > PCR_DIV_BULL_THRESHOLD` | `pcr_div < PCR_DIV_BEAR_THRESHOLD` |
| **S3: VEX** | `vex_signal == VEX_BULLISH` | `vex_signal == VEX_BEARISH` |
| **S4: Futures OBI** | `fut_obi > per-underlying bull threshold` | `fut_obi < per-underlying bear threshold` |
| **S5: ATM OBI** | `atm_obi > OBI_THRESHOLD` | `atm_obi < –OBI_THRESHOLD` |

Each signal returns `CE`, `PE`, or `NEUTRAL`. `NEUTRAL` signals are excluded from the vote.

### 2.2 V_CoC Spike Detection (`_is_vcoc_spike_active`)

A separate 15-minute V_CoC spike is detected independently and can contribute an additional signal weight. The lookback is calculated dynamically:

```python
interval  = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)  # minutes per snap
n         = settings.VCOC_SPIKE_EXPIRY_SNAPS
earliest  = max(0, h * 60 + m - n * interval - 15)
```

### 2.3 Voting Resolution

```python
ce_votes  = count(s == "CE"  for s in active_signals)
pe_votes  = count(s == "PE"  for s in active_signals)
margin    = abs(ce_votes - pe_votes)

direction = (
    Direction.CE      if ce_votes > pe_votes else
    Direction.PE      if pe_votes > ce_votes else
    Direction.NEUTRAL  # tie → no trade
)
```

Return includes `signals` list (each entry: `{signal, vote, value, weight}`) and `margin`.

---

## 3. Confidence Scoring (`ai/confidence.py`)

Confidence is a **0–100 score** composed of four independent buckets. Session adjustments can raise or lower the final value.

### Bucket 1 — Signal Alignment (max 40 pts)

```python
b1 = min(40, margin * 7 + signal_count * 3)
```

Lowering the margin coefficient to 7 (from an earlier 8) creates headroom so `signal_count×3` contributes meaningfully at all margin values.

| Scenario | B1 |
|---|---|
| margin=1, count=2 | min(40, 7+6) = 13 |
| margin=3, count=4 | min(40, 21+12) = 33 |
| margin=5, count=5 | min(40, 35+15) = 40 |

### Bucket 2 — Gate Score (max 25 pts)

```python
gate_max = settings.GATE_MAX_SCORE or 10
b2 = min(25, int((gate_score / gate_max) * 25))
```

Example: gate=9/11 → `int(9/11 * 25)` = 20 pts.

### Bucket 3 — Structural Quality (max 25 pts)

| Condition | Points | Guard |
|---|---|---|
| `ivp < 50` | +6 | `ivp if ivp is not None else 100` |
| IV shape is `CONTANGO` | +4 | |
| `s_score > 80` | +7 | `s_score or 0` |
| GEX regime is `NEGATIVE_TREND` or `POSITIVE_DECLINING` | +5 | |
| `VEX_BULLISH` and direction `CE` | +3 | |
| `VEX_BEARISH` and direction `PE` | +3 | |

Max without cap = 6+4+7+5+3 = 25.

### Bucket 4 — Historical Performance (max 10 pts)

```python
is_fallback  = learning_stats.get("is_fallback", False)
total_trades = learning_stats.get("total_trades", 0)
if is_fallback or total_trades < 5:
    b4 = 0   # cold-start guard
else:
    raw_wr = learning_stats.get("win_rate")
    win_rate = (raw_wr / 100) if raw_wr is not None else 0.5
    b4 = min(10, int(win_rate * 12))
```

`win_rate` may be `None` when no closed trades exist (cold-start) — `None` is never silently converted to a fictitious 50%.

### Session Adjustments

```python
if session == MarketSession.MIDDAY_CHOP:
    raw -= settings.SESSION_MIDDAY_CONFIDENCE_PENALTY   # default –10
if session == MarketSession.CLOSING_CRUSH:
    raw = min(raw, settings.SESSION_CLOSING_CONFIDENCE_CAP)  # default cap 60

confidence = max(0, min(100, raw))
```

`session_adjusted = raw != (b1 + b2 + b3 + b4)` — exposed in the response.

---

## 4. Pre-Flight Checks (`ai/pre_flight.py`)

Pre-flight is a set of **7 hard binary rules**. Any failure blocks the recommendation entirely.

| Rule | Pass Condition | Config Default |
|---|---|---|
| PF-1: Min Gate Score | `gate_score >= PREFLIGHT_MIN_GATE_SCORE` | 5 |
| PF-2: Min Confidence | `confidence >= PREFLIGHT_MIN_CONFIDENCE` | 50 |
| PF-3: Max Theta Ratio | `abs(theta) / ltp <= PREFLIGHT_MAX_THETA_RATIO` | 0.03 |
| PF-4: Max Pain Distance | `abs(max_pain_dist_pct) <= PREFLIGHT_MAX_PAIN_PROXIMITY` | 0.5% |
| PF-5: Not Zero LTP | `ltp > 0` | — |
| PF-6: DTE=0 Guard | Rejects recommendation if `dte == 0` | — |
| PF-7: DTE=1 Higher Bar | When `dte == 1`: gate ≥ `PREFLIGHT_DTE1_MIN_GATE` and confidence ≥ `PREFLIGHT_DTE1_MIN_CONFIDENCE` | 7 / 65 |

Returns `(passed: bool, failures: list[str])`. Failure strings are stored in the log.

> **PF-4 Guard**: `max_pain` may be `None` if no options data exists. The check is skipped (not failed) when `max_pain is None`.

---

## 5. Quality Score (`ai/quality.py`)

Assigns a **letter grade A/B/C/D** at recommendation time.

```python
w_s = settings.QUALITY_W_SSCORE      # default 0.40
w_g = settings.QUALITY_W_GATE        # default 0.35
w_c = settings.QUALITY_W_CONFIDENCE  # default 0.25

raw = (
    w_s * min(1.0, s_score / 150.0) +
    w_g * (gate_score / (gate_max or 1)) +
    w_c * (confidence / 100.0)
) * 100
```

| Grade | Threshold |
|---|---|
| A | ≥ 80 |
| B | ≥ 60 |
| C | ≥ 40 |
| D | < 40 |

---

## 6. Trade Narrative (`ai/narrative.py`)

All narratives are **template-based** — deterministic, zero-latency, data-backed. No LLM.

```python
build_narrative(direction, gate_score, gate_verdict,
                direction_signals, iv_data, gex_data,
                vex_data, session, dealer_oclock) -> str
```

Sentences are selected and formatted based on which data is available. If a value is `None`, the corresponding sentence is omitted entirely (never "N/A" or exception).

The top signal (highest weight in `direction_signals`) is shown first, followed by gate score, IV regime, GEX pct_of_peak, and dealer o'clock warning if active.

---

## 7. SL and Target Calculation

```python
entry_premium = strike["ltp"]   # validated > 0 before this point
sl_price     = round(entry_premium * (1 - settings.AI_SL_PCT), 2)
target_price = round(entry_premium * settings.AI_TARGET_MULT, 2)
```

| Setting | Default | Meaning |
|---|---|---|
| `AI_SL_PCT` | 0.35 | Stop-loss 35% below entry |
| `AI_TARGET_MULT` | 1.50 | Target 50% above entry |

These are **initial values**. The live tracker modifies the effective stop dynamically via the theta-adjusted SL and trailing stop.

---

## 8. What Blocks a Recommendation?

| Block Point | Condition |
|---|---|
| Already open | `ACCEPTED` trade exists for this underlying |
| Pending | `GENERATED` (unactioned) recommendation exists |
| Neutral direction | Direction vote is a tie |
| No expiry | Nearest expiry is `None` |
| No candidates | No strikes match direction + filters |
| Zero LTP | Best candidate has ltp ≤ 0 |
| Pre-flight failure | Any of the 7 hard rules fails |

Only after passing every gate does the trade card get written to the journal.
