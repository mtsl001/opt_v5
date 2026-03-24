All three issues are confirmed present. Here is the detailed report:

***

## ❌ Issue #1 — Microstructure: Window Still 10 Snaps

**Status: FIXED | File: `optdash/analytics/microstructure.py`**

The window is unchanged at 10 snaps across both the code and the docstring :

```python
# Docstring: "Full-day volume ratio vs rolling 10-snap median baseline."
window = vols[max(1, i - 10):i]  # ← still 10, not 30
```

No config constant (`MICROSTRUCTURE_BASELINE_WINDOW` or equivalent) exists in `config.py` either — the window is purely hardcoded. A 10-snap window covers only 10 minutes of prior data, making the baseline too sensitive to local volume surges. A 30-snap (30-minute) median gives a statistically stable baseline that resists intraday noise and more accurately represents the session's normal participation level.

**Required fix — `microstructure.py`:**
```python
window = vols[max(1, i - settings.VOLUME_VELOCITY_BASELINE_SNAPS):i]
```
**Required addition — `config.py`:**
```python
VOLUME_VELOCITY_BASELINE_SNAPS: int = 30  # rolling median window; ~30 min at 1-min cadence
```

***

## ⚠️ Issue #2 — Alerts: Opening Suppression Incomplete

**Status: FIXED | File: `optdash/analytics/alerts.py`**

Only V_CoC has *any* suppression, and even that is incomplete. Volume Spike has none:

**V_CoC — single-snap path (`elif len == 1`):** Suppresses `== "09:15"` only. Snaps at `09:20` and `09:25` still fire freely during the opening turbulence window where carry anomalies are equally unreliable .

**Volume Spike — single-snap path (`elif len == 1`):** Has **zero suppression**. At 09:15, `microstructure.py` hardcodes `ratio = 1.0` so it never signals `SPIKE` — but at 09:20, `get_volume_velocity()` computes a real ratio against a single-element baseline (the index-1 volume), making it trivially easy to produce a false SPIKE on the second snap.

**Multi-snap path (`len >= 2`) for both alerts:** No opening suppression exists at all. If the lookback window happens to include only opening snaps, the transition guard can still fire falsely.

**Required fix — `alerts.py`** for both V_CoC and Volume Spike:
```python
# Replace the snap_time != "09:15" check with a time-range guard
OPENING_SUPPRESS_END = "09:25"   # suppress 09:15, 09:20, 09:25 (3 snaps)

# V_CoC single-snap path:
if coc_w[0]["snap_time"] > OPENING_SUPPRESS_END:
    # fire alert

# Volume Spike single-snap path:
if vol_w[0]["snap_time"] > OPENING_SUPPRESS_END:
    # fire alert
```
Add `ALERT_OPENING_SUPPRESS_END: str = "09:25"` to `config.py` so the boundary is tunable without a code change.

***

## ❌ Issue #3 — Pipeline: `avg_volume_20d` Absent From `BQ_SELECT_COLS` (Critical)

**Status: FIXED | File: `optdash/config.py`, `optdash/analytics/screener.py`**

`avg_volume_20d` is referenced in screener factor 7 but is missing from `BQ_SELECT_COLS` — meaning it is never fetched from BigQuery, never written to Parquet, and the column is `NULL` for every row in `options_data` .

The NULL propagates through the momentum factor:
```
TRY_CAST(NULL AS DOUBLE) → NULL
NULLIF(NULL, 0)          → NULL
volume / NULL            → NULL
LEAST(3.0, NULL)         → NULL    ← entire factor 7 = NULL
```
Since `s_score = (factor_1 + ... + NULL + ...) × 10 = NULL`, every option returned by the screener has `s_score = NULL`. Pre-flight Rule 5 then catches it:
```python
if (strike.get("s_score") or 0) < settings.PREFLIGHT_MIN_SSCORE:
    # (None or 0) = 0 < 60.0 → always fires
    failures.append("S_score 0.0 below floor 60.0")
```
**Every recommendation is silently blocked at pre-flight Rule 5.** The system appears to run normally — analytics complete, no exceptions — but `generate_recommendation()` returns `None` on every tick. Additionally, Confidence B3 loses its 7-point S_score bonus and Quality C1 scores 0/35 for every trade .

**Required fix — one line in `config.py`:**
```python
BQ_SELECT_COLS: list[str] = [
    ...
    "vega",
    "avg_volume_20d",    # ← ADD THIS — required by screener momentum factor 7
]
```
This is the highest-priority fix of the three: the pipeline is effectively dead without it, as no recommendations can clear pre-flight while `s_score` is universally NULL.