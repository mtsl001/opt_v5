"""PCR analytics — Put-Call ratio divergence and OBI smoothing."""
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.metrics import record_error


def get_pcr(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> dict:
    """Current PCR snapshot with divergence signal."""
    try:
        row = conn.execute("""
            SELECT
                -- TIER1 aggregates
                SUM(CASE WHEN option_type='PE' AND expiry_tier='TIER1' THEN volume ELSE 0 END) /
                NULLIF(SUM(CASE WHEN option_type='CE' AND expiry_tier='TIER1' THEN volume ELSE 0 END), 0) AS pcr_vol_t1,

                SUM(CASE WHEN option_type='PE' AND expiry_tier='TIER1' THEN oi ELSE 0 END) /
                NULLIF(SUM(CASE WHEN option_type='CE' AND expiry_tier='TIER1' THEN oi ELSE 0 END), 0)     AS pcr_oi_t1,

                -- TIER2 aggregates
                SUM(CASE WHEN option_type='PE' AND expiry_tier='TIER2' THEN volume ELSE 0 END) /
                NULLIF(SUM(CASE WHEN option_type='CE' AND expiry_tier='TIER2' THEN volume ELSE 0 END), 0) AS pcr_vol_t2,

                SUM(CASE WHEN option_type='PE' AND expiry_tier='TIER2' THEN oi ELSE 0 END) /
                NULLIF(SUM(CASE WHEN option_type='CE' AND expiry_tier='TIER2' THEN oi ELSE 0 END), 0)     AS pcr_oi_t2,

                -- DTE for tier-selection logic (min DTE of TIER1 = near-expiry contract)
                MIN(CASE WHEN expiry_tier='TIER1' THEN dte END)                                           AS dte_t1

            FROM options_data
            WHERE trade_date=? AND snap_time=? AND underlying=?
              AND expiry_tier IN ('TIER1', 'TIER2')
        """, [trade_date, snap_time, underlying]).fetchone()
        
        if not row:
            return {}
            
        pcr_vol_t1, pcr_oi_t1 = row[0] or 1.0, row[1] or 1.0
        pcr_vol_t2, pcr_oi_t2 = row[2] or 1.0, row[3] or 1.0
        dte_t1 = int(row[4] or 99)
        div_t1 = round(pcr_vol_t1 - pcr_oi_t1, 4)
        div_t2 = round(pcr_vol_t2 - pcr_oi_t2, 4)

        # Primary tier selection — annotated, never silent
        use_tier2 = (dte_t1 <= 1)
        primary_div  = div_t2 if use_tier2 else div_t1
        tier_used    = "TIER2" if use_tier2 else "TIER1"
        obi          = _smoothed_obi(conn, trade_date, snap_time, underlying, tier=tier_used)

        metrics = _trailing_pcr_metrics(conn, trade_date, snap_time, underlying, tier=tier_used)
        div_std = metrics["div_std"]
        z_score = round((primary_div - metrics["div_mean"]) / div_std, 3) if div_std > 0.001 else 0.0
        
        div_lag = metrics["div_lag"]
        div_trend = round(primary_div - div_lag, 4) if div_lag is not None else 0.0
        
        signal = _pcr_signal_z(primary_div, z_score, metrics["count"], div_trend)

        return {
            "snap_time":      snap_time,
            "dte_t1":         dte_t1,
            "tier_used":      tier_used,          # explicit — never hidden
            # TIER1
            "pcr_vol_t1":     round(pcr_vol_t1, 3),
            "pcr_oi_t1":      round(pcr_oi_t1, 3),
            "div_t1":         div_t1,
            "signal_t1":      _pcr_signal(div_t1),
            # TIER2
            "pcr_vol_t2":     round(pcr_vol_t2, 3),
            "pcr_oi_t2":      round(pcr_oi_t2, 3),
            "div_t2":         div_t2,
            "signal_t2":      _pcr_signal(div_t2),
            # Primary (what Gate C4 and frontend should use)
            "pcr_divergence": primary_div,        # backward-compatible key name kept
            "z_score":        z_score,
            "div_trend":      div_trend,
            "signal":         signal,
            "smoothed_obi":   round(obi, 4),
        }
    except Exception as e:
        record_error("get_pcr")
        logger.warning("get_pcr error: {}", e)
        return {}


def get_pcr_series(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    underlying: str,
) -> list[dict]:
    """Full-day PCR series with per-snap 3-period smoothed OBI.

    smoothed_obi is computed as a 3-row rolling average of the per-snap
    OBI using a SQL window function — single query, no N+1 round trips.
    """
    try:
        w_z = settings.PCR_ZSCORE_WINDOW - 1
        w_t = settings.PCR_TREND_SNAPS
        w_obi = settings.PCR_OBI_SMOOTH_SNAPS - 1
        rows = conn.execute(f"""
            SELECT
                snap_time,
                pcr_vol_t1, pcr_oi_t1, ROUND(pcr_vol_t1 - pcr_oi_t1, 4) AS div_t1,
                pcr_vol_t2, pcr_oi_t2, ROUND(pcr_vol_t2 - pcr_oi_t2, 4) AS div_t2,
                dte_t1,
                obi_t1, obi_t2,
                AVG(pcr_vol_t1 - pcr_oi_t1) OVER (
                    ORDER BY snap_time
                    ROWS BETWEEN {w_z} PRECEDING AND CURRENT ROW
                ) AS div_mean_t1,
                STDDEV_SAMP(pcr_vol_t1 - pcr_oi_t1) OVER (
                    ORDER BY snap_time
                    ROWS BETWEEN {w_z} PRECEDING AND CURRENT ROW
                ) AS div_std_t1,
                AVG(pcr_vol_t2 - pcr_oi_t2) OVER (
                    ORDER BY snap_time
                    ROWS BETWEEN {w_z} PRECEDING AND CURRENT ROW
                ) AS div_mean_t2,
                STDDEV_SAMP(pcr_vol_t2 - pcr_oi_t2) OVER (
                    ORDER BY snap_time
                    ROWS BETWEEN {w_z} PRECEDING AND CURRENT ROW
                ) AS div_std_t2,
                LAG(pcr_vol_t1 - pcr_oi_t1, {w_t}) OVER (
                    ORDER BY snap_time
                ) AS div_lag_t1,
                LAG(pcr_vol_t2 - pcr_oi_t2, {w_t}) OVER (
                    ORDER BY snap_time
                ) AS div_lag_t2,
                AVG(obi_t1) OVER (
                    ORDER BY snap_time
                    ROWS BETWEEN {w_obi} PRECEDING AND CURRENT ROW
                ) AS smoothed_obi_t1,
                AVG(obi_t2) OVER (
                    ORDER BY snap_time
                    ROWS BETWEEN {w_obi} PRECEDING AND CURRENT ROW
                ) AS smoothed_obi_t2
            FROM (
                SELECT
                    snap_time,
                    SUM(CASE WHEN option_type='PE' AND expiry_tier='TIER1' THEN volume ELSE 0 END) /
                    NULLIF(SUM(CASE WHEN option_type='CE' AND expiry_tier='TIER1' THEN volume ELSE 0 END), 0) AS pcr_vol_t1,
                    SUM(CASE WHEN option_type='PE' AND expiry_tier='TIER1' THEN oi ELSE 0 END) /
                    NULLIF(SUM(CASE WHEN option_type='CE' AND expiry_tier='TIER1' THEN oi ELSE 0 END), 0)     AS pcr_oi_t1,
                    SUM(CASE WHEN option_type='PE' AND expiry_tier='TIER2' THEN volume ELSE 0 END) /
                    NULLIF(SUM(CASE WHEN option_type='CE' AND expiry_tier='TIER2' THEN volume ELSE 0 END), 0) AS pcr_vol_t2,
                    SUM(CASE WHEN option_type='PE' AND expiry_tier='TIER2' THEN oi ELSE 0 END) /
                    NULLIF(SUM(CASE WHEN option_type='CE' AND expiry_tier='TIER2' THEN oi ELSE 0 END), 0)     AS pcr_oi_t2,
                    MIN(CASE WHEN expiry_tier='TIER1' THEN dte END)                                           AS dte_t1,
                    (SUM(CASE WHEN expiry_tier='TIER1' THEN COALESCE(bid1_qty,0) ELSE 0 END) -
                     SUM(CASE WHEN expiry_tier='TIER1' THEN COALESCE(ask1_qty,0) ELSE 0 END)) /
                    NULLIF(SUM(CASE WHEN expiry_tier='TIER1' THEN COALESCE(bid1_qty,0)+COALESCE(ask1_qty,0) ELSE 0 END), 0) AS obi_t1,
                    (SUM(CASE WHEN expiry_tier='TIER2' THEN COALESCE(bid1_qty,0) ELSE 0 END) -
                     SUM(CASE WHEN expiry_tier='TIER2' THEN COALESCE(ask1_qty,0) ELSE 0 END)) /
                    NULLIF(SUM(CASE WHEN expiry_tier='TIER2' THEN COALESCE(bid1_qty,0)+COALESCE(ask1_qty,0) ELSE 0 END), 0) AS obi_t2
                FROM options_data
                WHERE trade_date=? AND underlying=? AND expiry_tier IN ('TIER1', 'TIER2')
                GROUP BY snap_time
            ) sub
            ORDER BY snap_time
        """, [trade_date, underlying]).fetchall()

        result = []
        for i, r in enumerate(rows):
            pcr_vol_t1 = r[1] or 1.0
            pcr_oi_t1  = r[2] or 1.0
            div_t1     = r[3] or 0.0
            pcr_vol_t2 = r[4] or 1.0
            pcr_oi_t2  = r[5] or 1.0
            div_t2     = r[6] or 0.0
            dte_t1     = int(r[7] or 99)
            
            div_mean_t1  = r[10] or 0.0
            div_std_t1   = r[11] or 0.0
            div_mean_t2  = r[12] or 0.0
            div_std_t2   = r[13] or 0.0
            div_lag_t1   = r[14]
            div_lag_t2   = r[15]
            smoothed_obi_t1 = r[16] or 0.0
            smoothed_obi_t2 = r[17] or 0.0
            
            use_tier2 = (dte_t1 <= 1)
            primary_div = div_t2 if use_tier2 else div_t1
            tier_used   = "TIER2" if use_tier2 else "TIER1"
            
            div_mean = div_mean_t2 if use_tier2 else div_mean_t1
            div_std  = div_std_t2 if use_tier2 else div_std_t1
            div_lag  = div_lag_t2 if use_tier2 else div_lag_t1
            smoothed_obi = smoothed_obi_t2 if use_tier2 else smoothed_obi_t1
            
            z_score = round((primary_div - div_mean) / div_std, 3) if div_std > 0.001 else 0.0
            div_trend = round(primary_div - div_lag, 4) if div_lag is not None else 0.0
            
            result.append({
                "snap_time":      r[0],
                "dte_t1":         dte_t1,
                "tier_used":      tier_used,
                "pcr_vol_t1":     round(pcr_vol_t1, 3),
                "pcr_oi_t1":      round(pcr_oi_t1, 3),
                "div_t1":         div_t1,
                "signal_t1":      _pcr_signal(div_t1),
                "pcr_vol_t2":     round(pcr_vol_t2, 3),
                "pcr_oi_t2":      round(pcr_oi_t2, 3),
                "div_t2":         div_t2,
                "signal_t2":      _pcr_signal(div_t2),
                "pcr_divergence": primary_div,
                "z_score":        z_score,
                "div_trend":      div_trend,
                "signal":         _pcr_signal_z(primary_div, z_score, i + 1, div_trend),
                "smoothed_obi":   round(smoothed_obi, 4),
            })
        return result
    except Exception as e:
        logger.warning("get_pcr_series error: {}", e)
        return []


def _smoothed_obi(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
    tier:       str = "TIER1",
) -> float:
    """Trailing average of per-snap L1 OBI over PCR_OBI_SMOOTH_SNAPS snaps.

    Window: settings.PCR_OBI_SMOOTH_SNAPS (default 10 snaps = 10 min at 1-min cadence).
    Source: bid1_qty/ask1_qty (instantaneous L1 depth, not cumulative day flow).
    Tier: matches the primary PCR tier (TIER1 normally, TIER2 on expiry day).
    COALESCE guards handle NULL depth on far-OTM/illiquid strikes.
    """
    try:
        limit = settings.PCR_OBI_SMOOTH_SNAPS
        rows = conn.execute("""
            SELECT
                (SUM(COALESCE(bid1_qty,0)) - SUM(COALESCE(ask1_qty,0))) /
                NULLIF(SUM(COALESCE(bid1_qty,0) + COALESCE(ask1_qty,0)), 0) AS obi
            FROM options_data
            WHERE trade_date=? AND underlying=? AND snap_time <= ?
              AND expiry_tier=?
            GROUP BY snap_time
            ORDER BY snap_time DESC
            LIMIT ?
        """, [trade_date, underlying, snap_time, tier, limit]).fetchall()
        if not rows:
            return 0.0
        return sum(r[0] or 0 for r in rows) / len(rows)
    except Exception:
        return 0.0


def _trailing_pcr_metrics(conn: duckdb.DuckDBPyConnection, trade_date: str, snap_time: str, underlying: str, tier: str = "TIER1") -> dict:
    """Compute rolling Z-score parameters and divergence trend for the latest snapshot."""
    try:
        limit_rows = max(settings.PCR_ZSCORE_WINDOW, settings.PCR_TREND_SNAPS + 1)
        rows = conn.execute("""
            SELECT
                (SUM(CASE WHEN option_type='PE' THEN volume ELSE 0 END) /
                 NULLIF(SUM(CASE WHEN option_type='CE' THEN volume ELSE 0 END), 0)) -
                (SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) /
                 NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END), 0)) AS div_t1
            FROM options_data
            WHERE trade_date=? AND underlying=? AND snap_time <= ?
              AND expiry_tier=?
              AND instrument_type='OPT'  -- Fix F-1: exclude FUT rows (option_type=NULL)
            GROUP BY snap_time
            ORDER BY snap_time DESC
            LIMIT ?
        """, [trade_date, underlying, snap_time, tier, limit_rows]).fetchall()
        
        if not rows:
            return {"div_mean": 0.0, "div_std": 0.0, "div_lag": None, "count": 0}
            
        divs = [r[0] or 0.0 for r in rows]
        
        z_divs = divs[:settings.PCR_ZSCORE_WINDOW]
        count = len(z_divs)
        mean = sum(z_divs) / count
        std = 0.0
        if count > 1:
            variance = sum((x - mean) ** 2 for x in z_divs) / (count - 1)
            std = variance ** 0.5
            
        lag_idx = min(settings.PCR_TREND_SNAPS, len(divs) - 1)
        div_lag = divs[lag_idx] if len(divs) > settings.PCR_TREND_SNAPS else None
        
        return {"div_mean": mean, "div_std": std, "div_lag": div_lag, "count": count}
    except Exception:
        return {"div_mean": 0.0, "div_std": 0.0, "div_lag": None, "count": 0}

def _pcr_signal_z(div: float, z: float, snap_count: int, div_trend: float) -> str:
    """Evaluate PCR divergence heavily relying on Z-score to auto-calibrate."""
    signal = "BALANCED"
    if snap_count >= settings.PCR_ZSCORE_WINDOW:
        # Z-score regime — normalized signal
        if z > settings.PCR_Z_PANIC_THRESHOLD:
            signal = "RETAIL_PANIC_PUTS"
        elif z < -settings.PCR_Z_PANIC_THRESHOLD:
            signal = "RETAIL_PANIC_CALLS"
        elif abs(z) > settings.PCR_Z_BUILDING_THRESHOLD:
            signal = "DIVERGENCE_BUILDING"
    else:
        # Fallback to absolute thresholds before window is filled
        if div > settings.PCR_DIV_BULL_THRESHOLD:
            signal = "RETAIL_PANIC_PUTS"
        elif div < settings.PCR_DIV_BEAR_THRESHOLD:
            signal = "RETAIL_PANIC_CALLS"
        elif abs(div) > 0.10:
            signal = "DIVERGENCE_BUILDING"
            
    # Blunt signal on reversion:
    if signal == "RETAIL_PANIC_PUTS" and div_trend < -settings.PCR_Z_FADING_TREND:
        signal = "DIVERGENCE_FADING"
    elif signal == "RETAIL_PANIC_CALLS" and div_trend > settings.PCR_Z_FADING_TREND:
        signal = "DIVERGENCE_FADING"
        
    return signal

def _pcr_signal(div: float) -> str:
    """Provide absolute threshold signal when Z-score is not applicable."""
    if div > settings.PCR_DIV_BULL_THRESHOLD:
        return "RETAIL_PANIC_PUTS"
    if div < settings.PCR_DIV_BEAR_THRESHOLD:
        return "RETAIL_PANIC_CALLS"
    if abs(div) > 0.10:
        return "DIVERGENCE_BUILDING"
    return "BALANCED"
