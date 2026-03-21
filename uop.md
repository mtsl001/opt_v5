Now I have everything needed for a comprehensive deep-dive. Let me analyze every analytics module one by one.

***

# OptDash Analytics — Deep Research & Validation

Below is a thorough correctness check of each analytics module, with identified issues and actionable improvements.

***

## GEX (Gamma Exposure) — §3

### Formula Correctness

Your formula is **correct and matches industry standard**  [perfiliev](https://perfiliev.com/blog/how-to-calculate-gamma-exposure-and-zero-gamma-level/):

$$\text{GEX} = \gamma \times OI \times \text{lot\_size} \times \text{spot}^2 \times 0.01 \times \text{direction}$$

The `spot² × 0.01` scaling converts raw gamma into "dollar delta change per 1% move in the underlying" — identical to what Perfiliev and SpotGamma use  [perfiliev](https://perfiliev.com/blog/how-to-calculate-gamma-exposure-and-zero-gamma-level/). CE = +1, PE = −1 is consistent with the **dealer-perspective convention** where sold calls = positive GEX (stabilizing hedging) and sold puts = negative GEX (destabilizing hedging)  [strike-watch](https://www.strike-watch.com/lab/gamma-exposure-gex-dealer-hedging-shapes-price-action.php).

### ⚠️ Issues Found

- **pct_of_peak denominator risk:** The formula is `|current_gex| / day_peak_gex × 100`. If the day starts with negative GEX and peaks there, `day_peak_gex` is negative. The code must use `|day_peak_gex|` in the denominator — verify this in `gex.py`.
- **Gamma units from Upstox:** The `γ` from the BQ feed (BSM gamma) must be in **per-rupee** units (not per-percent). If Upstox provides gamma per 1% move, you must multiply by `spot/100` instead of `spot²×0.01`. This is a silent data contract issue worth explicitly documenting.
- **Multi-expiry aggregation:** Current GEX aggregates all tiers. TIER2/TIER3 gamma is far-dated and structurally weaker — blending it with TIER1 dilutes the intraday signal.

### 🔧 Improvements

- Track the **zero-GEX level** (strike where net GEX flips from positive to negative) — this is the strongest support/resistance level derived from GEX, widely used by SpotGamma and similar tools  [strike-watch](https://www.strike-watch.com/lab/gamma-exposure-gex-dealer-hedging-shapes-price-action.php).
- Add **TIER1-only GEX** as a separate signal to avoid far-expiry dilution.
- Store `pct_of_peak` as a raw column in Parquet so it can be trended over time.

***

## Cost-of-Carry (CoC) — §4

### Formula Correctness

`CoC = AVG(fut_price) - AVG(spot)` is **correct**. The positive = contango, negative = backwardation interpretation is standard  [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md).

### ⚠️ Issues Found

- **Absolute thresholds across different underlyings:** `V_CoC > 10` and `CoC < -5.0` are absolute point values. For NIFTY at ~24,000, 10 points ≈ 0.04%; for BANKNIFTY at ~52,000, 10 points ≈ 0.019%. The same threshold fires much easier for BANKNIFTY than for NIFTY — this is **not calibrated per-underlying**.
- **ATM OBI formula has a parenthesis ambiguity in the doc**  [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md):
  ```
  OBI = (CE_bid_flow - CE_ask_flow) - (PE_bid_flow - PE_ask_flow) / total_qty
  ```
  As written, only the PE term is divided by `total_qty` due to operator precedence. The correct formula should be:
  ```
  OBI = [(CE_bid_flow - CE_ask_flow) - (PE_bid_flow - PE_ask_flow)] / total_qty
  ```
  **Verify immediately in the actual Python code** — this is a critical potential bug.
- **AVG() averaging window is unspecified:** If `AVG(fut_price)` averages across multiple snaps in the DuckDB window, it dilutes the live CoC signal.

### 🔧 Improvements

- Replace absolute thresholds with **percentage-based thresholds**: `V_CoC > spot × 0.04%` for each underlying.
- Use **latest snap value** (not AVG) for CoC to ensure responsiveness; reserve AVG for the 15-min lookback only.
- Add **CoC vs DTE normalization**: As expiry nears, CoC naturally decays. Normalize: `normalized_CoC = CoC / (DTE / 365)` to compare across different expiry distances.

***

## PCR (Put-Call Ratio) — §5

### Formula Correctness

Your divergence approach is **valid and more sophisticated** than simple PCR  [bajajfinserv](https://www.bajajfinserv.in/put-call-ratio):

$$\text{Divergence} = \text{PCR\_Volume} - \text{PCR\_OI}$$

The contrarian interpretation (`div > 0.25` → retail panic puts → fade → bullish) aligns with how professional traders use PCR divergence  [navia.co](https://navia.co.in/blog/how-pro-traders-use-put-call-ratio-pcr-charts/).

### ⚠️ Issues Found

- **Fixed thresholds across all underlyings:** `div > 0.25` and `div < -0.20` are hardcoded. BANKNIFTY PCR behaves structurally different from NIFTY PCR due to its high-beta nature. The same divergence threshold can mean different things.
- **TIER1-only is correct** but misses a scenario: near-expiry (DTE ≤ 1), TIER1 options can have distorted PCR due to aggressive gamma scalping — this overlaps with Dealer O'Clock and should be cross-checked.
- **Asymmetric thresholds (0.25 vs -0.20)** are intentional but not documented with reasoning — this should be explicitly justified in the codebase.

### 🔧 Improvements

- Use a **rolling Z-score** of divergence instead of fixed thresholds: `Z = (div - div.mean(20d)) / div.std(20d)`. Flag when Z > 1.5 or Z < -1.5 for dynamic, self-calibrating alerts.
- Track **PCR Divergence trend** (is it building or reversing?) — a divergence that is shrinking is less actionable than one that is growing.
- Add **TIER2 PCR** as a secondary reference — institutional positioning often shows up in 2nd-nearest expiry OI before TIER1.

***

## IV Analytics — §6

### Formula Correctness

**IVR and IVP formulas are textbook-correct**  [algotest](https://algotest.in/blog/what-is-ivp-and-ivr-and-how-to-use-ivp-and-ivr/):

$$\text{IVR} = \frac{\text{ATM\_IV} - \text{IV\_Low}}{\text{IV\_High} - \text{IV\_Low}} \times 100$$

$$\text{IVP} = \frac{\text{days where ATM\_IV} \leq \text{current\_ATM\_IV}}{\text{total\_days}} \times 100$$

HV20 using log-returns × √252 is the **standard annualization formula** and is correct  [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md).

### ⚠️ Issues Found

- **IVR range distortion:** A single IV spike (e.g., budget day, election) inflates `IV_High` for the next 252 days, making every subsequent IVR reading artificially low. This is a known structural weakness of IVR vs IVP  [algotest](https://algotest.in/blog/what-is-ivp-and-ivr-and-how-to-use-ivp-and-ivr/).
- **HV20 uses `underlying_spot` (live intraday tick) not end-of-day close.** For meaningful historical volatility, you need **daily close-to-close returns**. Using a live intraday tick as "today's spot" gives an incomplete/noisy last observation.
- **IVP minimum sample guard (20 days)** returning `None` → Gate C5 treats as "IV not cheap" is conservative but correct. However, after system startup (backfill from 2026-02-17), the 20-day guard should be breached immediately — verify the backfill populates the IV history table correctly.
- **ATM definition as "within 1.5% of spot"** can include 3-4 strikes for NIFTY (50-pt intervals). Averaging IV across all of them smooths out skew information.

### 🔧 Improvements

- Use **IVP as the primary gate signal** (C5) rather than IVR — IVP is more robust to IV spike events  [algotest](https://algotest.in/blog/what-is-ivp-and-ivr-and-how-to-use-ivp-and-ivr/).
- Add **IV Skew** signal: `PE_ATM_IV - CE_ATM_IV` at the same strike. High put skew = elevated tail-risk hedging demand.
- For HV20, pull from a **dedicated daily OHLC table** rather than deriving from intraday snaps — this is cleaner and more accurate.
- Track **IV Crush zones** (pre/post events like RBI policy, budget, expiry) and reduce confidence scores around them.

***

## VEX / CEX — §7

### Formula Correctness

**VEX approximation is reasonable but not exact.** The true BSM vanna is:

$$\text{vanna} = -N'(d_1) \cdot \frac{d_2}{\sigma}$$

Your approximation `δ × (1 - |δ|) / (spot × σ × √T)` is a **shortcut** that avoids needing `d1`, `d2` directly. It works acceptably for near-ATM options but degrades for deep ITM/OTM  [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md).

**CEX approximation using `-θ / (spot × σ × √T)` is a rough proxy.** True BSM charm is:

$$\text{charm} = -N'(d_1) \cdot \frac{2rT - d_2 \cdot \sigma\sqrt{T}}{2T \cdot \sigma\sqrt{T}}$$

Using theta as a proxy for charm conflates **time value decay** with **delta's time sensitivity** — they're related but not equivalent, especially for ITM/OTM options.

### ⚠️ Issues Found

- **Clip at ±50 is aggressive** for ATM options where vanna/charm are expected to be moderate. If the feed provides accurate `delta`, `IV`, `DTE`, the clip should rarely trigger — if it's triggering often, it may signal a data quality issue upstream.
- **VEX thresholds are uniform for NIFTY and BANKNIFTY (both 0.50)** despite BANKNIFTY having 3× the notional per lot (lot=15, but much higher spot). The absolute threshold in ₹M may not be comparable across underlyings.
- **Dealer O'Clock start at 14:00** (30 min before 14:30 session boundary) is deliberately early — but this means Gate C10 fires on all trades from 14:00 regardless of how high their other scores are, which is overly restrictive on non-expiry days. This gate only fires when `DTE ≤ 1`, so it's fine — but the doc is worth clarifying.

### 🔧 Improvements

- Compute **full BSM vanna and charm** if risk-free rate (RBI repo rate) and dividend yield are available — the approximations introduce systematic error for large VEX/CEX readings.
- Add **VEX/CEX by strike visualization**: identify which strikes have the largest vanna/charm flows — these are the strongest dealer hedging pressure zones intraday.
- Make VEX thresholds **percentage of total market cap notional** rather than absolute ₹M values for fair cross-underlying comparison.

***

## Strike Screener & S_Score — §8

### Formula Correctness

The composite scoring logic is sound. Let me check the theoretical maximum:

| Factor | Weight | Max Input | Max Contribution |
|--------|--------|-----------|-----------------|
| W_DELTA | 4.0 | 0.50 (filter cap) | **2.0** |
| W_EFF_RATIO | 4.0 | 1.0 | 4.0 |
| W_LIQUIDITY | 3.0 | 1.0 | 3.0 |
| W_IV | 2.0 | 1.0 | 2.0 |
| W_THETA | 2.0 | 1.0 | 2.0 |
| W_GAMMA | 1.0 | 1.0 | 1.0 |
| W_VEGA | 1.0 | 1.0 | 1.0 |
| **Total × 10** | | | **150** |

### ⚠️ Issues Found

- **Delta is asymmetrically weighted:** W_DELTA = 4.0 but its practical max contribution is 2.0 (delta capped at 0.5 by filter), while W_EFF_RATIO = 4.0 contributes a full 4.0. This means **EFF_RATIO has 2× the effective weight of DELTA** — which may under-reward higher-delta (more directional) options  [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md).
- **W_IV penalizes all high-IV options regardless of market regime.** In a trending market with elevated IV, high-IV options may still be valid buys. The IV penalty should be conditioned on IVP, not raw IV.
- **Liquidity cap at 5 Cr notional** (`OI × ltp / 1e7 / 5.0`) means a BANKNIFTY ATM option at ₹500 LTP with 10,000 OI = ₹50 Cr notional still gets only max score. This normalization is too loose for BANKNIFTY.
- **Star rating ≥100 for ⭐⭐⭐⭐** with theoretical max of 150 is calibrated fine — top ~33% of possible range for 4-star.

### 🔧 Improvements

- **Normalize delta to its filtered range:** `(|delta| - 0.10) / (0.50 - 0.10)` → this scales delta contribution to 0–1 matching all other factors, giving W_DELTA its intended full weight.
- Add a **momentum factor**: `volume / avg_volume_20d` — options with above-average volume at the screened strike signal real interest.
- Add **bid-ask spread** as a liquidity penalty: tight spreads = easier entry/exit.
- Consider **time-of-day weighting** — opening session strikes deserve higher delta weights (more directional), closing session deserves higher theta-efficiency weight.

***

## Environment Gate — §10

### Design Correctness

The 11-point gate is well-structured. C9 (VEX, 2 pts) correctly receives double weight as a structural dealer-flow signal  [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md).

### ⚠️ Issues Found

- **C2 (V_CoC) and C3 (Futures OBI) are correlated:** Both fire when institutions are aggressively building futures positions (one measures the premium spike, the other measures the order queue). This is **partial double-counting** of the same signal source.
- **No volume/liquidity gate:** Low market volume (e.g., holidays, half-sessions) means all signal readings are unreliable, yet the gate has no provision for this.
- **C7 (Term Structure ≠ BACKWARDATION)** is a valid filter but fires rarely. It adds a point that is almost always scored, making the gate artificially higher most of the time — it's more of a negative-filter than a true positive signal.
- **C8 (Not Midday Chop)** only blocks 11:30–13:00. But the opening 9:15–9:30 is equally noisy due to pre-open auction price discovery — there's no opening turbulence guard.

### 🔧 Improvements

- Add **C11: Volume Guard** — check if current snap volume > 30% of the day's average snap volume. Prevents low-liquidity signal noise.
- Add a **9:15–9:30 opening turbulence guard** (analogous to C8) — first 2 snaps of the day are unreliable.
- Reclassify C7 as a **penalty gate** (−1 if BACKWARDATION) rather than a bonus gate — this better reflects its role as a veto condition.
- Consider making C2+C3 a **combined 2-point gate** (requiring both to fire for full points) to reduce double-counting.

***

## Directional Bias Engine — §11

### Weights Correctness

| Signal | Weight | Role | Issue |
|--------|--------|------|-------|
| V_CoC | 3 | Momentum | ✅ Correct |
| Futures OBI | 2 | Positioning | ⚠️ Correlated with V_CoC |
| VEX | 2 | Structural | ✅ Correct |
| ATM OBI | 1 | Short-term flow | ✅ Correct |
| PCR Divergence | 1 | Contrarian | ⚠️ Mixed signal type |

### ⚠️ Issues Found

- **PCR Divergence is a contrarian signal; the other 4 are directional/momentum signals.** Mixing contrarian and momentum signals in the same additive vote pool is theoretically problematic: in strong trending markets, PCR divergence can fight V_CoC and cancel conviction that should be high.
- **VCOC Spike Persistence (3 snaps = 15 min):** This is reasonable, but there's no decay mechanism — the spike contributes full weight=3 whether it fired 1 snap ago or 3 snaps ago.

### 🔧 Improvements

- **Separate contrarian signals (PCR Divergence) from momentum signals** — apply them as a conviction modifier (multiplier) rather than additive votes. For example: if PCR divergence confirms the direction → multiply final margin by 1.2; if it contradicts → multiply by 0.8.
- Add **decaying spike persistence** for V_CoC: full weight (3) in snap 1, reduced weight (2) in snap 2, minimal weight (1) in snap 3 of persistence window.
- Add a **NEUTRAL conviction threshold**: if `|CE_weight - PE_weight| ≤ 1`, it should be `WEAK_CE/PE`, not a full directional call — this improves precision by distinguishing strong from marginal direction.

***

## Confidence Score — §13

### Formula Correctness

**Signal Alignment bucket has an uncapped overflow risk**  [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md):

`margin × 7 + signal_count × 3` with max margin = 9, max signals = 5:
→ `9×7 + 5×3 = 63 + 15 = 78` which far exceeds the bucket max of 40.

The cap must be enforced in code as `min(40, margin×7 + signal_count×3)` — verify this exists in `confidence.py`.

**Historical Performance bucket:** `win_rate × 12` can theoretically = 12, but bucket max = 10. Either the multiplier should be `10` or there's an unchecked overflow  [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/81933033/b261935c-5128-4afe-b158-faab7a4b2587/optdash_technical_reference.md).

### ⚠️ Issues Found

- **Circular dependency:** Quality Score (§14) includes Confidence Score (C3 component), which itself includes Gate Score and Structural Quality. The final grade partially depends on itself through multiple hops — this doesn't cause errors but makes interpretation harder.
- **Win_rate × 12 with ≥5 trades minimum:** 5 trades is statistically insignificant. A 5-trade win rate of 80% could easily be luck. A minimum of 15–20 trades is more meaningful for historical performance to carry weight.
- **Closing Crush cap at 60** prevents high-confidence trades after 14:30 — this is conservative and correct for option buying but may miss genuine 3:15 PM momentum trades.

### 🔧 Improvements

- Explicitly document and enforce all bucket caps in code with `min()` guards.
- Raise historical performance minimum to **15 trades** and increase the bucket weight after 30+ trades (e.g., `min(15, win_rate × 15)` after 30 trades).
- Add a **session-specific confidence multiplier** instead of a flat -10 penalty for midday — e.g., apply penalty only when PCR divergence is also absent, allowing high-conviction midday trades when the signal stack is strong.

***

## Quality Score & Grades — §14

### Formula Correctness

$$\text{Quality} = \underbrace{\min(35,\ S\_Score/120 \times 35)}_{C1} + \underbrace{\min(35,\ gate\_score/11 \times 35)}_{C2} + \underbrace{\min(30,\ confidence/100 \times 30)}_{C3}$$

### ⚠️ Issues Found

- **C1 uses S_Score / 120 as denominator**, but S_Score can theoretically reach ~150. This means C1 maxes out at S_Score ≥ 120, treating anything above 120 as equally "perfect." The ⭐⭐⭐⭐ threshold at S_Score ≥ 100 and C1 max at S_Score = 120 are slightly inconsistent — consider using S_Score / 150 for full-range coverage.
- **C2 (gate_score / 11)** — a gate score of 7 (GO threshold) gives only `7/11 × 35 = 22.3 / 35`. A quality Grade A requires ≥ 80, which means you need C1 + C3 to cover the remaining 57.7+ points. This makes Gate Score underweighted in the Quality formula relative to its actual importance in the pipeline.

### 🔧 Improvements

- Rename gate component normalization to `gate_score / GATE_GO_THRESHOLD` so that a "GO" gate always yields a high quality score, not just `gate / max`.
- Add a **Recommendation Freshness** component: deduct points if the last recommendation was > 15 minutes old (stale data risk).
- Post-trade, feed the **actual P&L** back to Quality Score retrospectively — this creates a learning loop that can gradually recalibrate weights.

***

## Cross-Cutting Improvements

These apply across all analytics and are the highest-leverage upgrades:

- **Per-underlying threshold calibration:** V_CoC, CoC discount, VEX, and CEX all use absolute ₹ or point values that are not normalized per underlying — systematically review all thresholds and make them percentage-based or underlying-specific.
- **Signal correlation matrix:** V_CoC + Futures OBI are correlated; GEX decline + VEX are partially correlated. A correlation-weighted voting system would reduce redundancy and improve edge detection.
- **Data quality guard layer:** Add a pre-analytics validation step that checks for stale snaps (`record_time` lag > 7 minutes), zero-OI strikes, and missing futures prices before any signal computation runs.
- **Backtesting harness:** The `BACKFILL_START_DATE = 2026-02-17` means there's now ~4 weeks of historical Parquet data. Running each analytics module's signals against historical trade outcomes (journal SQLite) would empirically validate thresholds rather than relying on theoretical reasoning alone.
- **Stop-loss and target calibration:** `sl_price = ltp × 0.65` (35% SL) and `target = ltp × 1.50` (50% target) are fixed percentage-based rules that don't adapt to IV environment. In high-IV environments, options swing more — the SL/target should scale with `IVP` or `HV20`.