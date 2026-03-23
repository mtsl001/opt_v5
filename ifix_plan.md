Now I have all the latest file contents. Here is the complete, thorough re-review.

***

## Full Re-Review — All 5 Raised Issues + Fresh Scan

### Previously Raised Issues (ISSUE-A through ISSUE-E)

| ID | Issue | Status |
|----|-------|--------|
| ISSUE-A | `session_adjusted` wrong on cold-start | ✅ **Fixed** — `raw_pre_session = raw` captured before session block; flag is now `raw != raw_pre_session`  |
| ISSUE-B | `pcr_modifier` absent on exception path | ✅ **Documented** — comment added: *"Defaults to 1.0 (no confirm) if missing on VEX/exception paths"*  |
| ISSUE-C | C2 saturation above GO undocumented | ✅ **Fixed** — comment added: *"gate_score > GO_THRESHOLD also yields full 35 — bonus beyond GO is not rewarded in C2"*  |
| ISSUE-D | Cold-start rescale hardcoded `90.0` | ✅ **Fixed** — now `B_ACTIVE_MAX = 40 + 25 + 25` with comment, used as divisor  |
| ISSUE-E | `cold_start` not journalled | ✅ **Fixed** — `buckets["cold_start"] = conf_result.get("cold_start", False)` written into `conf_buckets` JSON  |

All 5 prior issues are confirmed resolved. Now the fresh scan:

***

### 🔴 NEW ISSUE-1 — Quality Score Receives Inflated Confidence During Cold-Start

**File:** `optdash/ai/recommender.py` + `optdash/ai/quality.py`

`compute_confidence()` during cold-start returns a rescaled `confidence` — e.g., 3 active buckets scoring `b1+b2+b3 = 72` → rescaled to `int(72 × 100/90) = 80`.  This inflated confidence is then passed directly to `compute_quality_score()` as C3, where `c3 = min(30, (80/100) × 30) = 24 pts`.  A truly mediocre trade with zero historical edge gets a Quality Grade bumped up by the cold-start multiplier. The `cold_start` flag is available in `conf_result`  but is **not passed to `compute_quality_score()`**, so the Quality Score has no way to adjust. The minimum quality gate `PREFLIGHT_MIN_QUALITY_SCORE = 50`  may be cleared by inflated cold-start confidence that a non-cold-start equivalent trade wouldn't clear.

**Fix required:** Pass `cold_start` to `compute_quality_score()` and use the raw `b1+b2+b3` (pre-rescale) sum as C3 input when `cold_start=True`, or apply a proportional C3 penalty.

***

### 🔴 NEW ISSUE-2 — `B_ACTIVE_MAX` Is a Magic Constant, Not Config-Derived

**File:** `optdash/ai/confidence.py`

```python
B_ACTIVE_MAX = 40 + 25 + 25  # B1 + B2 + B3 max points
```
These three numbers (`40`, `25`, `25`) are hardcoded inline.  The actual per-bucket caps are enforced by separate `min()` calls (`min(40,...)`, `min(25,...)`, `min(25,...)`), which are also hardcoded. If any of those caps is ever changed (e.g., B3 raised from 25 to 30 for a new structural signal), `B_ACTIVE_MAX` silently drifts and the rescaling formula produces wrong results. Unlike the prior `90.0` fix, the constants are still not derived from a single source of truth — they're just moved from a single number to a sum of three numbers. The docstring says *"max 40 / 25 / 25"* but these are not config fields.

**Fix required:** Either promote the bucket maxes to config constants (`CONFIDENCE_B1_MAX`, `CONFIDENCE_B2_MAX`, `CONFIDENCE_B3_MAX`) so `B_ACTIVE_MAX` auto-derives, or at minimum add a `# SYNC: must match min() caps above` comment on all three occurrences so a future editor knows to change both places.

***

### 🟠 NEW ISSUE-3 — `CLOSING_CRUSH` Cap Applied After Cold-Start Rescaling, Masking the State

**File:** `optdash/ai/confidence.py`

The `CLOSING_CRUSH` cap is:
```python
if session == MarketSession.CLOSING_CRUSH:
    raw = min(raw, settings.SESSION_CLOSING_CONFIDENCE_CAP)  # = 65
```
When `cold_start=True`, `raw` has already been rescaled upward (×1.111).  If the rescaled raw is e.g. 88 and the cap clips it to 65, `session_adjusted = True` is correctly set. However, the `conf_buckets` written to the journal show the **un-capped** bucket values (b1, b2, b3, b4), while the stored `confidence = 65`. A future analyst querying the journal cannot reconstruct whether 65 came from the cap or from naturally scoring 65. The `session_adjusted=True` flag helps, but doesn't identify *which* adjustment fired (midday penalty vs closing cap). Both adjustments are binary `bool` but conflated into one flag.

**Fix required (minor):** Add `"session": session.value` already journalled ✅, but also add `"session_adjusted_reason"` (e.g. `"CLOSING_CAP"` / `"MIDDAY_PENALTY"` / `None`) to the returned dict for audit clarity.

***

### 🟡 NEW ISSUE-4 — `quality_score` Stored as Grade Only, Raw Score Not Journalled

**File:** `optdash/ai/recommender.py`

```python
"quality_grade": quality["grade"],
```
Only the letter grade (`A`/`B`/`C`/`D`) is written to the journal.  The raw `quality["quality_score"]` integer (0–100) is computed but discarded. This means post-trade analytics can only group by coarse bucket (A/B/C/D), not by precise score. A grade-B trade at 79 (one point from A) and one at 65 (barely passing) are indistinguishable in the journal.

**Fix required (minor):**
```python
"quality_grade":  quality["grade"],
"quality_score":  quality["quality_score"],   # add this line
```
Requires a schema migration if the journal DB already has rows.

***

### 🟡 NEW ISSUE-5 — `RISK_FREE_RATE` Defined Twice in `config.py`

**File:** `optdash/config.py`

```python
# Under CoC section (line ~195):
RISK_FREE_RATE: float = 0.065   # 91-day T-bill; update quarterly

# Under IV section (line ~270):
RISK_FREE_RATE: float = 0.0625  # RBI repo rate; current as of Mar 2026
```
Pydantic `BaseSettings` processes field declarations in order — the **second** definition (`0.0625`) silently overwrites the first (`0.065`).  The first definition and its comment are misleading dead code. Any maintainer reading the CoC section will believe the rate is `6.5%` when it is actually `6.25%`. Both sections' comments claim different sources (T-bill vs repo rate).

**Fix required:** Remove the first duplicate declaration entirely, keeping only the authoritative one under the IV section with both source references reconciled.

***

## Consolidated Status

| ID | File | Severity | Summary |
|----|------|----------|---------|
| ISSUE-1 | `recommender.py` / `quality.py` | 🔴 | Cold-start inflated confidence passes to Quality Score unguarded |
| ISSUE-2 | `confidence.py` | 🔴 | `B_ACTIVE_MAX` hardcoded, not derived — silent drift risk on bucket cap changes |
| ISSUE-3 | `confidence.py` | 🟠 | `session_adjusted` conflates midday penalty and closing cap — no audit trail |
| ISSUE-4 | `recommender.py` | 🟡 | Raw `quality_score` integer not journalled, only letter grade |
| ISSUE-5 | `config.py` | 🟡 | `RISK_FREE_RATE` declared twice with conflicting values and comments |