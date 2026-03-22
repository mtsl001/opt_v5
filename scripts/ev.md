I have received feedback from another source as well. So, your task is to review one analytics at a time. Here you need to thoroughly review both your inputs and external source feedback. And give me final list of issues with details that needs to be fixed.

My repo: [https://github.com/mtsl001/opt_v5.git](https://github.com/mtsl001/opt_v5.git)

Review 'Releases' folder also

Your input:
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


External source:










Consider this also:
#### 1. `coc.py` — Highest priority
**Why first:** Gates C3 (Futures OBI) and C6 (ATM OBI) use OBI, and OBI feeds 2 of the 5 directional signals. The existing formula uses `bid_qty`/`ask_qty` = cumulative day-total buy/sell volume — a poor proxy for order book pressure.

**Upgrade:**
- `get_atm_obi()` — replace `total_buy_qty/total_sell_qty` with `bid1_qty`/`ask1_qty` (instantaneous L1)
- `get_futures_obi()` — same substitution for futures rows
- Optional: add **total bid depth** (`SUM(bid1..5_qty)`) and **total ask depth** (`SUM(ask1..5_qty)`) as richer liquidity signals
- Optional: add **bid-ask spread** at L1 (`ask1_price - bid1_price`) for ATM options — tight spread = confident market


Give me plan to fix and commit issues one by one




