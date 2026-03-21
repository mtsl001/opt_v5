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
        obi          = _smoothed_obi(conn, trade_date, snap_time, underlying)

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
            "signal":         _pcr_signal(primary_div),
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
        rows = conn.execute("""
            SELECT
                snap_time,
                pcr_vol_t1, pcr_oi_t1, ROUND(pcr_vol_t1 - pcr_oi_t1, 4) AS div_t1,
                pcr_vol_t2, pcr_oi_t2, ROUND(pcr_vol_t2 - pcr_oi_t2, 4) AS div_t2,
                dte_t1,
                obi,
                AVG(obi) OVER (
                    ORDER BY snap_time
                    ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
                ) AS smoothed_obi
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
                    (SUM(COALESCE(bid1_qty,0)) - SUM(COALESCE(ask1_qty,0))) /
                    NULLIF(SUM(COALESCE(bid1_qty,0) + COALESCE(ask1_qty,0)), 0)                               AS obi
                FROM options_data
                WHERE trade_date=? AND underlying=? AND expiry_tier IN ('TIER1', 'TIER2')
                GROUP BY snap_time
            ) sub
            ORDER BY snap_time
        """, [trade_date, underlying]).fetchall()

        result = []
        for r in rows:
            pcr_vol_t1 = r[1] or 1.0
            pcr_oi_t1  = r[2] or 1.0
            div_t1     = r[3] or 0.0
            pcr_vol_t2 = r[4] or 1.0
            pcr_oi_t2  = r[5] or 1.0
            div_t2     = r[6] or 0.0
            dte_t1     = int(r[7] or 99)
            
            use_tier2 = (dte_t1 <= 1)
            primary_div = div_t2 if use_tier2 else div_t1
            tier_used   = "TIER2" if use_tier2 else "TIER1"
            
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
                "signal":         _pcr_signal(primary_div),
                "smoothed_obi":   round(r[9] or 0.0, 4),
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
) -> float:
    """3-snap trailing average of OBI (15-min smoothing)."""
    try:
        rows = conn.execute("""
            SELECT
                (SUM(COALESCE(bid1_qty,0)) - SUM(COALESCE(ask1_qty,0))) /
                NULLIF(SUM(COALESCE(bid1_qty,0) + COALESCE(ask1_qty,0)), 0) AS obi
            FROM options_data
            WHERE trade_date=? AND underlying=? AND snap_time <= ?
              AND expiry_tier='TIER1'
            GROUP BY snap_time
            ORDER BY snap_time DESC
            LIMIT 3
        """, [trade_date, underlying, snap_time]).fetchall()
        if not rows:
            return 0.0
        return sum(r[0] or 0 for r in rows) / len(rows)
    except Exception:
        return 0.0


def _pcr_signal(div: float) -> str:
    if div > settings.PCR_DIV_BULL_THRESHOLD:
        return "RETAIL_PANIC_PUTS"
    if div < settings.PCR_DIV_BEAR_THRESHOLD:
        return "RETAIL_PANIC_CALLS"
    if abs(div) > 0.10:
        return "DIVERGENCE_BUILDING"
    return "BALANCED"
