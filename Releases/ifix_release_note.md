# OptDash Release Note - iFix Updates (March 24, 2026)

This release addresses three high-priority fixes identified during the `ifix_plan.md` review to improve signal reliability and ensure correct pipeline functionality.

## 1. Microstructure Baseline Stabilization
- **Issue:** The volume velocity baseline was hardcoded to a short 10-snap window, making the readings oversensitive to localized intraday surges.
- **Resolution:** Introduced a tunable `VOLUME_VELOCITY_BASELINE_SNAPS` parameter in `config.py` and increased the default window to **30 snaps** (30 minutes).
- **Impact:** Generates a statistically stable baseline comparison that correctly filters intraday noise for more accurate Volume Spike detection.

## 2. Early-Session Alert Suppression
- **Issue:** V_CoC and Volume Spike alerts triggered unreliable signals at the market open due to overnight gaps and first-tick anomalies.
- **Resolution:** Created a global tunable `ALERT_OPENING_SUPPRESS_END` (set to `09:25`) in `config.py` and implemented suppression thresholds for single-snap and multi-snap paths in `alerts.py`.
- **Impact:** Filters out unstable alert behavior during the first 10 minutes of trading while preserving responsive signaling immediately after the opening volatility subsides.

## 3. Pipeline Data Resolution (`avg_volume_20d`)
- **Issue:** The screener analytics were missing `avg_volume_20d` from `BQ_SELECT_COLS`, preventing S_scores from calculating successfully and halting AI recommender suggestions.
- **Resolution:** Appended `avg_volume_20d` to `BQ_SELECT_COLS` within `config.py`.
- **Impact:** Ensures BigQuery tables fetch all necessary dimensions required by factor calculations, restoring momentum scores and unblocking S_score calculations.

---
**Files affected:**
- `optdash/config.py`
- `optdash/analytics/microstructure.py`
- `optdash/analytics/alerts.py`
- `ifix_plan.md`
