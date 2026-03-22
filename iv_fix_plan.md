# OptDash — VEX/CEX & IV Analytics Fix Plan: Part A

**Target modules:** `vex_cex.py` · `iv.py` · `processor.py` · `config.py` · `ai/confidence.py`  
**Release:** v2.5.x → v2.6.0  
**Branch to create:** `fix/vex-iv-analytics-part-a`

> **For the junior developer:**  
> Every issue below tells you (a) what is currently wrong and where,  
> (b) the exact replacement code, and (c) the correct commit message.  
> Follow the commit order in the Summary Table at the end.  
> Do NOT touch any file not listed in an issue. Run tests after each commit.

---

## Pre-Flight Checklist

```bash
git pull origin main
git checkout -b fix/vex-iv-analytics-part-a
# Confirm baseline
python -c "from optdash.config import settings; print(settings.VRP_OVERPRICED_THRESHOLD)"
```

---
""")

# ---- IV-OPEN-1
sections.append("""## Issue IV-OPEN-1 — `vrp_regime` and VIX gate thresholds not in config

**Severity:** Medium | **File:** `optdash/config.py`

### What is wrong

`iv.py` already uses `settings.VRP_OVERPRICED_THRESHOLD`, `settings.VRP_UNDERPRICED_THRESHOLD`,
`settings.VIX_HIGH_THRESHOLD`, and `settings.VIX_HIGH_IVP_THRESHOLD`. But these constants
have no comments in `config.py`, making them impossible to tune safely via `.env`.

### Fix

Find the `# -- IV` section in `config.py` (near `IV_LOOKBACK_DAYS`).  
Add or update the following block immediately below `IV_LOOKBACK_DAYS`:

```python
# VRP = ATM_IV - HV20  (both in %, e.g. IV=18.5, HV20=14.2 → VRP=+4.3)
# OVERPRICED  (VRP > +2.0): options expensive vs realised vol — sellers edge.
#             Buyers should require stronger signal confirmation.
# UNDERPRICED (VRP <  0.0): options cheaper than realised vol — buyers edge.
#             Mathematical edge for option buying is highest here.
# FAIR        (0.0 ≤ VRP ≤ 2.0): normal conditions.
VRP_OVERPRICED_THRESHOLD:  float = 2.0
VRP_UNDERPRICED_THRESHOLD: float = 0.0

# India VIX gate parameters
# VIX_HIGH_THRESHOLD:     VIX above this tightens Gate C5 IVP requirement.
# VIX_HIGH_IVP_THRESHOLD: In high-VIX regime, IVP must be < 35 (not < 50)
#                         to score the "IV cheap" gate point.
VIX_HIGH_THRESHOLD:     float = 20.0
VIX_HIGH_IVP_THRESHOLD: float = 35.0
```

Add this validator inside the `Settings` class after the VRP fields:

```python
@field_validator("VRP_UNDERPRICED_THRESHOLD")
@classmethod
def _check_vrp_thresholds(cls, v: float, info) -> float:
    overpriced = (info.data or {}).get("VRP_OVERPRICED_THRESHOLD", 2.0)
    if v >= overpriced:
        raise ValueError(
            f"VRP_UNDERPRICED_THRESHOLD={v} must be < VRP_OVERPRICED_THRESHOLD={overpriced}."
        )
    return v
```

### Commit message

```
fix(config): expose VRP and VIX gate thresholds as named config constants (IV-OPEN-1)
```

---
""")

# ---- IV-OPEN-2
sections.append("""## Issue IV-OPEN-2 — VRP not wired into Confidence Score

**Severity:** Medium | **File:** `optdash/ai/confidence.py`  
**Depends on:** IV-OPEN-1 (config constants must exist first)

### What is wrong

`iv.py` computes `vrp_regime` but the Confidence Score Structural Quality bucket (max 25 pts)
ignores it entirely. When `vrp_regime = "UNDERPRICED"`, options are mathematically cheap
for buyers — this is the highest-conviction entry context and should boost confidence.

### Find the Structural Quality bucket

Look in `confidence.py` for code awarding points for IVP, Contango, S_Score, GEX, VEX.
It looks roughly like:

```python
sq = 0
if ivp is not None and ivp < 50:
    sq += 6
if shape == "CONTANGO":
    sq += 4
if s_score > 80:
    sq += 7
if gex_declining:
    sq += 5
if vex_aligned:
    sq += 3
structural_quality = min(25, sq)
```

### Fix

After the `vex_aligned` block, add:

```python
# VRP bonus (+3): when VRP < 0, options are genuinely underpriced vs realised vol.
# This is the statistically strongest entry context for option buyers.
# Source: iv_data["vrp_regime"] from get_ivr_ivp().
vrp_regime = iv_data.get("vrp_regime", "UNKNOWN")
if vrp_regime == "UNDERPRICED":
    sq += 3
# Note: min(25, sq) cap below handles overflow — no change to cap line needed.
```

**Check the function signature** of `compute_confidence()`. It must receive `iv_data` as a
parameter. If it doesn't:
1. Add `iv_data: dict` to the signature.
2. Find where `compute_confidence()` is called (likely in `scheduler.py` or `recommender.py`)
   and pass `iv_data` from the `get_ivr_ivp()` result already fetched there.

### Commit message

```
feat(confidence): add VRP_UNDERPRICED bonus to structural quality bucket +3 pts (IV-OPEN-2)
```

---
""")

# ---- VEX-3
sections.append("""## Issue VEX-3 — Clip trigger rate is invisible (hides data quality problems)

**Severity:** Medium | **File:** `optdash/processor.py`

### What is wrong

`VANNA_CLIP = CHARM_CLIP = 50.0` silently truncates extreme vanna/charm values.
If 20-30% of rows are being clipped, it means near-zero IV rows in the upstream BQ feed
are corrupting the computation. There is currently no way to detect this.

### Fix

In `processor.py`, find the function that computes vanna/charm per row (likely `_compute_gex_vex_cex`
or the main enrichment loop). Add counters before the loop and a warning check after:

```python
# --- Before the row-processing loop ---
clip_count_vanna = 0
clip_count_charm = 0
total_opt_rows   = 0

# --- Inside the loop, after computing raw vanna (BEFORE applying clip) ---
# (raw_vanna is the value before max(-CLIP, min(CLIP, ...)) )
if abs(raw_vanna) > settings.VANNA_CLIP:
    clip_count_vanna += 1
vanna = max(-settings.VANNA_CLIP, min(settings.VANNA_CLIP, raw_vanna))

if abs(raw_charm) > settings.CHARM_CLIP:
    clip_count_charm += 1
charm = max(-settings.CHARM_CLIP, min(settings.CHARM_CLIP, raw_charm))

total_opt_rows += 1

# --- After the loop ---
if total_opt_rows > 0:
    vanna_rate = clip_count_vanna / total_opt_rows
    charm_rate = clip_count_charm / total_opt_rows
    if vanna_rate > 0.05:
        logger.warning(
            "HIGH VANNA CLIP RATE {:.1%} ({}/{} rows) for {}. "
            "Check for near-zero IV rows in BQ feed.",
            vanna_rate, clip_count_vanna, total_opt_rows, underlying
        )
    if charm_rate > 0.05:
        logger.warning(
            "HIGH CHARM CLIP RATE {:.1%} ({}/{} rows) for {}. "
            "Check for near-zero IV rows in BQ feed.",
            charm_rate, clip_count_charm, total_opt_rows, underlying
        )
```

### Commit message

```
feat(processor): add clip-rate monitoring for vanna/charm noise filter (VEX-3)

Logs WARNING when >5% of rows hit VANNA_CLIP or CHARM_CLIP per snap.
Surfaces upstream data quality problems that were previously silent.
```

---
""")

# ---- VEX-4
sections.append("""## Issue VEX-4 — BANKNIFTY VEX threshold identical to NIFTY despite lower notional/lot

**Severity:** Medium | **File:** `optdash/config.py`

### What is wrong

```python
VEX_THRESHOLDS = {"NIFTY": 0.50, "BANKNIFTY": 0.50, ...}
```

VEX formula: `OI × lot_size × vanna × spot / 1e6`  
Notional multiplier = spot × lot_size:
- NIFTY: 24000 × 75 = **1,800,000** per lot
- BANKNIFTY: 52000 × 15 = **780,000** per lot  

BANKNIFTY's notional per lot is ~43% of NIFTY. The same absolute ₹M threshold
is proportionally harder for BANKNIFTY to cross — VEX signals are under-triggering.

### Fix

In `config.py`, update `VEX_THRESHOLDS`:

```python
VEX_THRESHOLDS: dict[str, float] = {
    # Threshold = minimum |VEX| in Rs M to classify as VEX_BULLISH/BEARISH.
    # Calibration (Mar 2026) based on notional-per-lot = spot × lot_size:
    #   NIFTY:       ~24000 × 75  = 1.80M  → 0.50 (high notional, strong signal)
    #   BANKNIFTY:   ~52000 × 15  = 0.78M  → 0.35 (lower notional per lot;
    #                                               threshold reduced proportionally)
    #   FINNIFTY:    ~25000 × 40  = 1.00M  → 0.25 (smaller OI base)
    #   MIDCPNIFTY:  ~12000 × 120 = 1.44M  → 0.15 (thin liquidity)
    #   NIFTYNXT50:  ~80000 × 10  = 0.80M  → 0.15 (very thin OI)
    # Review after 30 live trading days.
    "NIFTY":      0.50,
    "BANKNIFTY":  0.35,   # was 0.50
    "FINNIFTY":   0.25,
    "MIDCPNIFTY": 0.15,
    "NIFTYNXT50": 0.15,
}
```

### Commit message

```
fix(config): recalibrate BANKNIFTY VEX threshold 0.50→0.35 with notional docs (VEX-4)
```

---
""")

# ---- VEX-5
sections.append("""## Issue VEX-5 — CEX charm direction ignores GEX sign (directional logic is wrong)

**Severity:** Medium | **Files:** `optdash/analytics/vex_cex.py`

### What is wrong

`_interpret()` labels `STRONG_CHARM_BID` as bullish regardless of dealer positioning.

**The truth:**
- If `net_GEX > 0` (dealers net LONG options): charm decay forces them to **SELL** underlying → bearish drift.
- If `net_GEX < 0` (dealers net SHORT options): charm decay forces them to **BUY** underlying → bullish support.

### Fix

**Step 1:** Add import at top of `vex_cex.py`:

```python
from optdash.analytics.gex import get_net_gex
```

> Before adding this, open `gex.py` and check its imports — confirm it does NOT import
> anything from `vex_cex.py`. If it does, skip this import and use the alternative below.

**Step 2:** In `get_vex_cex_current()`, after fetching the VEX/CEX row from DuckDB, add:

```python
# Fetch net GEX sign to determine charm hedging direction (VEX-5)
gex_data = get_net_gex(conn, trade_date, snap_time, underlying)
net_gex   = gex_data.get("gex_all_B", 0.0)
```

**Step 3:** In `get_vex_cex_current()`, update the `_interpret()` call:

```python
# Change:
interp = _interpret(vex_signal, cex_signal, dealer_oc)
# To:
interp = _interpret(vex_signal, cex_signal, dealer_oc, net_gex)
```

Also add `net_gex` to the returned dict:

```python
return {
    ...
    "net_gex_B":    round(net_gex, 3),   # add this line
    "interpretation": interp,
}
```

**Step 4:** Replace the `_interpret()` function entirely:

```python
def _interpret(vex_signal: str, cex_signal: str,
               dealer_oc: bool, net_gex: float = 0.0) -> str:
    \"\"\"
    Human-readable VEX/CEX signal interpretation.
    CEX direction is GEX-conditional:
      net_gex > 0 → dealers long options → charm forces them to SELL (bearish)
      net_gex < 0 → dealers short options → charm forces them to BUY (bullish)
    \"\"\"
    if dealer_oc:
        if cex_signal in (CexSignal.STRONG_CHARM_BID.value, CexSignal.CHARM_BID.value):
            if net_gex < 0:
                return ("Dealer O'Clock ACTIVE — GEX negative, charm forces dealer "
                        "BUYING. Bullish pinning/squeeze likely into expiry.")
            return ("Dealer O'Clock ACTIVE — GEX positive, charm forces dealer "
                    "SELLING. Bearish drift / pin below likely.")
        return "Dealer O'Clock active — charm flows dominate expiry day mechanics."

    if vex_signal == VexSignal.VEX_BULLISH.value:
        return "IV drop forces dealer buying — bullish mechanical bias."
    if vex_signal == VexSignal.VEX_BEARISH.value:
        return "IV rise forces dealer selling — bearish mechanical pressure."

    if cex_signal == CexSignal.STRONG_CHARM_BID.value:
        if net_gex < 0:
            return ("Strong charm: dealers short options → buying underlying "
                    "for delta hedge → bullish support.")
        return ("Strong charm: dealers long options → selling underlying "
                "for delta hedge → mild bearish drift.")
    if cex_signal == CexSignal.CHARM_BID.value:
        return "Charm bid building — monitor for directional confirmation."
    if cex_signal == CexSignal.CHARM_PRESSURE.value:
        return "Charm pressure — delta hedging adding supply."
    return "No dominant dealer flow signal."
```

**Step 5:** In `_get_vex_cex_series()`, update `_interpret()` call to pass neutral fallback:

```python
# Change:
"interpretation": _interpret(vex_sig, cex_sig, dealer_oc),
# To:
"interpretation": _interpret(vex_sig, cex_sig, dealer_oc, net_gex=0.0),
```

**Alternative if circular import:** If `gex.py` imports from `vex_cex.py`, do NOT add the gex
import. Instead, have `environment.py` pass `net_gex` down to `get_vex_cex_current()` as a
parameter, since `environment.py` already calls `get_net_gex()` independently.

### Commit message

```
fix(vex_cex): make CEX charm direction conditional on GEX sign (VEX-5)

Charm-driven delta-hedging direction flips based on whether dealers are
net long (GEX>0 → sell underlying) or net short (GEX<0 → buy underlying).
_interpret() now accepts net_gex float. Series path uses 0.0 fallback.
```

---
""")

# ---- VEX-1 and VEX-2 combined
sections.append("""## Issues VEX-1 + VEX-2 — Exact BSM Vanna and Charm (commit together)

**Severity:** HIGH | **Files:** `optdash/processor.py`, `optdash/config.py`  
**⚠️ Parquet breaking change — backfill required after deploy**

---

### VEX-1: Vanna approximation formula is wrong for OTM strikes

**Current code in processor.py:**

```python
sigma  = iv / 100
sqrt_t = math.sqrt(dte / 365)
vanna  = delta * (1 - abs(delta)) / (spot * sigma * sqrt_t)
vanna  = max(-settings.VANNA_CLIP, min(settings.VANNA_CLIP, vanna))
```

**Problem:** `δ × (1-|δ|)` mimics vanna's ATM peak but diverges significantly for OTM options
(delta 0.10–0.30) where retail clustering occurs.  
**True BSM vanna:**

```
Vanna = -(Vega_BSM × d2) / (Spot × σ × √T)
where d1 = [ln(S/K) + (r + 0.5σ²)T] / (σ√T)
      d2 = d1 - σ√T
      Vega_BSM = S × N'(d1) × √T
```

---

### VEX-2: Charm approximation conflates theta with delta time-sensitivity

**Current code in processor.py:**

```python
charm = -theta / (spot * sigma * sqrt_t)
charm = max(-settings.CHARM_CLIP, min(settings.CHARM_CLIP, charm))
```

**Problem:** Theta and charm are related near-ATM but diverge strongly for ITM/OTM options.  
For ITM options: theta is large, charm is small. For OTM near expiry: charm accelerates while
theta is small. CEX readings for the strikes you want to trade are systematically wrong.  
**True BSM charm:**

```
Charm = -N'(d1) × [2rT - d2 × σ√T] / [2T × σ√T]
```

---

### Fix — Step 1: Add `RISK_FREE_RATE` to `config.py`

In the `# -- IV` section, add:

```python
# Risk-free rate for exact BSM Greek computation.
# Use RBI repo rate (annualised decimal). Update when RBI changes policy rate.
# Current RBI repo rate: 6.25% as of Mar 2026.
# Override in .env: RISK_FREE_RATE=0.0650
RISK_FREE_RATE: float = 0.0625
```

---

### Fix — Step 2: Add helper functions in `processor.py`

At the top of `processor.py`, add this import if not already present:

```python
from scipy.stats import norm   # add to requirements.txt if scipy not present
```

Then add these two helper functions (add near the top of the file, outside any class):

```python
import math
from scipy.stats import norm

def _bsm_d1_d2(
    spot: float, strike: float, sigma: float, t: float, r: float
) -> tuple[float | None, float | None]:
    \"\"\"
    Compute BSM d1 and d2.
    Returns (None, None) on invalid inputs to prevent downstream errors.
    All inputs must be strictly positive (spot, strike, sigma, t > 0).
    \"\"\"
    if spot <= 0 or strike <= 0 or sigma <= 0 or t <= 0:
        return None, None
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
        d2 = d1 - sigma * math.sqrt(t)
        return d1, d2
    except (ValueError, ZeroDivisionError):
        return None, None


def _compute_exact_vanna(
    spot: float, strike: float, sigma: float, t: float, r: float,
    vanna_clip: float
) -> float:
    \"\"\"
    Exact BSM Vanna = -(Vega_BSM × d2) / (Spot × σ × √T)

    Vanna measures how delta changes as implied volatility changes.
    Positive vanna: delta increases as IV rises (call) or delta decreases as
    IV drops (put). For a dealer short options, rising IV forces delta hedging.
    \"\"\"
    d1, d2 = _bsm_d1_d2(spot, strike, sigma, t, r)
    if d1 is None:
        return 0.0
    sqrt_t   = math.sqrt(t)
    vega_bsm = spot * norm.pdf(d1) * sqrt_t      # S × N'(d1) × √T
    denom    = spot * sigma * sqrt_t
    if abs(denom) < 1e-10:
        return 0.0
    vanna = -(vega_bsm * d2) / denom
    return float(max(-vanna_clip, min(vanna_clip, vanna)))


def _compute_exact_charm(
    spot: float, strike: float, sigma: float, t: float, r: float,
    charm_clip: float
) -> float:
    \"\"\"
    Exact BSM Charm = -N'(d1) × [2rT - d2 × σ√T] / [2T × σ√T]

    Charm = dDelta/dTime. Measures how much delta decays per unit of time.
    On expiry day, charm flow is the primary driver of dealer delta-hedging.
    Negative charm = delta decays toward 0 as time passes (typical for calls).
    \"\"\"
    d1, d2 = _bsm_d1_d2(spot, strike, sigma, t, r)
    if d1 is None or t <= 0:
        return 0.0
    sqrt_t     = math.sqrt(t)
    numerator  = 2 * r * t - d2 * sigma * sqrt_t
    denominator = 2 * t * sigma * sqrt_t
    if abs(denominator) < 1e-10:
        return 0.0
    charm = -norm.pdf(d1) * (numerator / denominator)
    return float(max(-charm_clip, min(charm_clip, charm)))
```

---

### Fix — Step 3: Replace old vanna/charm calls in the row processing loop

Find the existing vanna/charm computation block (look for `delta * (1 - abs(delta))`).
Replace the entire block with:

```python
# Compute time parameters once per row
t      = max(row.get("dte", 0), 0) / 365.0
sigma  = (row.get("iv") or 0.0) / 100.0
r      = settings.RISK_FREE_RATE
_spot  = row.get("spot") or 0.0
_strike = row.get("strike_price") or 0.0

# Exact BSM vanna (VEX-1)
raw_vanna = _compute_exact_vanna(_spot, _strike, sigma, t, r, settings.VANNA_CLIP)
vanna = raw_vanna  # already clipped inside helper

# Exact BSM charm (VEX-2)
raw_charm = _compute_exact_charm(_spot, _strike, sigma, t, r, settings.CHARM_CLIP)
charm = raw_charm  # already clipped inside helper
```

---

### ⚠️ PARQUET BREAKING CHANGE — Action Required Before Deploying

The `vanna` and `charm` columns in Parquet change their values after this fix.
Historical Parquets contain old approximate values. You must choose one option:

**Option A (Recommended — Clean):**
```bash
# 1. Stop the scheduler
# 2. Delete all processed Parquets
rm -rf data/processed/
# 3. Reset the watermark
echo '{}' > data/watermark.json
# 4. Restart — backfill will regenerate all data with exact Greeks
```

**Option B (Acceptable — Gradual):**
Deploy the code change without deleting Parquets. New snaps will have exact values;
old snaps will have approximate values. The `DUCK_VIEW_LOOKBACK_DAYS=5` rolling window
means data fully transitions in 5 trading days. Signal inconsistency exists during transition.

**Recommendation: Option A.** Deploy on a non-trading day (weekend).

---

### `requirements.txt` check

Run: `pip show scipy`

If scipy is not installed, add to `requirements.txt`:
```
scipy>=1.11
```

---

### Commit message (commit VEX-1 and VEX-2 together)

```
fix(processor): replace approximate vanna+charm with exact BSM formulas (VEX-1, VEX-2)

VEX-1 — Vanna:
  was:  δ×(1-|δ|) / (S×σ×√T)
  now:  -(Vega_BSM×d2) / (S×σ×√T)  [exact BSM partial derivative]

VEX-2 — Charm:
  was:  -θ / (S×σ×√T)
  now:  -N'(d1)×[2rT-d2σ√T] / [2Tσ√T]  [exact BSM partial derivative]

- Added RISK_FREE_RATE = 0.0625 to config.py (RBI repo rate Mar 2026)
- Added _bsm_d1_d2(), _compute_exact_vanna(), _compute_exact_charm() helpers
- Both formulas still clipped at ±VANNA_CLIP / ±CHARM_CLIP (noise safety)
- scipy.stats.norm required — added to requirements.txt
- ⚠️ Parquet breaking change: delete data/processed/ + reset watermark before deploy
```

---
""")

# ---- RELEASE NOTES + SUMMARY
sections.append("""## Final Commit — Release Notes

Create `Releases/v2.6.0.md` with this content:

```markdown
# Release Notes — v2.6.0

**Date:** YYYY-MM-DD
**Type:** VEX/CEX & IV Analytics Reliability
**Branch:** fix/vex-iv-analytics-part-a

## Summary
Replaced approximate BSM Greek formulas with exact partial derivatives for VEX and CEX.
Exposed all IV/VIX gate thresholds as named config constants. Wired VRP signal into
Confidence Score. Fixed charm direction to correctly depend on net GEX sign.

## Changes

### VEX-1 & VEX-2: Exact BSM Vanna and Charm (processor.py, config.py)
- Vanna: replaced heuristic δ×(1-|δ|)/(S×σ×√T) with exact -(Vega_BSM×d2)/(S×σ×√T)
- Charm: replaced theta-proxy -θ/(S×σ×√T) with exact BSM charm formula
- Added _bsm_d1_d2() helper and RISK_FREE_RATE=0.0625 config constant
- ⚠️ Parquet breaking change: data/processed/ deleted + full backfill run on deploy

### VEX-3: Clip Rate Monitoring (processor.py)
- WARNING log fires when >5% of rows hit VANNA_CLIP or CHARM_CLIP in any snap
- Surfaces upstream data quality issues (near-zero IV rows from BQ feed)

### VEX-4: BANKNIFTY VEX Threshold Recalibrated (config.py)
- BANKNIFTY threshold: 0.50 → 0.35
- Added notional-per-lot calibration comments for all underlyings

### VEX-5: CEX Charm Direction now GEX-Conditional (vex_cex.py)
- _interpret() accepts net_gex parameter
- STRONG_CHARM_BID / Dealer O'Clock interpretation flips based on sign(net_GEX)
- net_gex_B field added to get_vex_cex_current() response payload

### IV-OPEN-1 & IV-OPEN-3: Config Constants Exposed (config.py)
- VRP_OVERPRICED_THRESHOLD = 2.0
- VRP_UNDERPRICED_THRESHOLD = 0.0
- VIX_HIGH_THRESHOLD = 20.0
- VIX_HIGH_IVP_THRESHOLD = 35.0
- Validator added to guard VRP_UNDERPRICED < VRP_OVERPRICED

### IV-OPEN-2: VRP into Confidence Score (ai/confidence.py)
- vrp_regime == "UNDERPRICED" adds +3 pts to Structural Quality bucket (max 25 cap preserved)
```

---

## Commit Order (follow exactly)

| Order | Issue | Files | Backfill? |
|-------|-------|-------|-----------|
| 1 | IV-OPEN-1 | `config.py` | No |
| 2 | IV-OPEN-2 | `ai/confidence.py` | No |
| 3 | VEX-3 | `processor.py` | No |
| 4 | VEX-4 | `config.py` | No |
| 5 | VEX-5 | `analytics/vex_cex.py` | No |
| 6 | VEX-1 + VEX-2 | `processor.py`, `config.py` | **YES — delete data/processed/** |
| 7 | Release notes | `Releases/v2.6.0.md` | No |

After all commits:

```bash
git push origin fix/vex-iv-analytics-part-a
# Open PR targeting main
# Title: "VEX/CEX & IV Analytics Reliability — Part A (v2.6.0)"
```

> **Part B** will cover: S_Score delta normalization, PCR Z-score divergence,
> Skew × VEX convergence alert, GEX zero-level computation, and Confidence Score
> historical performance minimum trade count fix.
