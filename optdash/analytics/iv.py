"""IV analytics -- IVR, IVP, term structure, shape detection."""
from datetime import datetime, timedelta
import duckdb
import math
from loguru import logger
from optdash.config import settings
from optdash.models import TermStructureShape
from optdash.metrics import record_error


def get_ivr_ivp(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> dict:
    """IVR and IVP for ATM TIER1 options."""
    try:
        # Current ATM IV
        cur = conn.execute("""
            WITH spot_cte AS (
                SELECT AVG(spot) AS spot FROM options_data
                WHERE trade_date=? AND snap_time=? AND underlying=?
            )
            SELECT AVG(o.iv) AS atm_iv
            FROM options_data o, spot_cte s
            WHERE o.trade_date=? AND o.snap_time=? AND o.underlying=?
              AND o.expiry_tier='TIER1'
              AND ABS(o.strike_price - s.spot) = (
                  SELECT MIN(ABS(strike_price - s.spot))
                  FROM options_data
                  WHERE trade_date=? AND snap_time=? AND underlying=?
                    AND expiry_tier='TIER1'
              )
        """, [
            trade_date, snap_time, underlying,
            trade_date, snap_time, underlying,
            trade_date, snap_time, underlying,
        ]).fetchone()
        atm_iv = cur[0] if cur else None
        # Issue-13: explicit None check — `not 0.0` is True in Python, so the
        # old falsy guard rejected a legitimate ATM IV of exactly 0.0 (deeply
        # OTM near expiry).  Same class of bug as the _classify_shape fix.
        if atm_iv is None:
            return {}

        # Historical IV stats (lookback window)
        # DuckDB cannot parameterize INTERVAL literals, so we compute the
        # lookback date in Python (same pattern as the IVP query below).
        hist_start = (
            datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=settings.IV_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

        hist = conn.execute("""
            SELECT
                PERCENTILE_CONT(?) WITHIN GROUP (ORDER BY daily_atm_iv) AS iv_low,
                PERCENTILE_CONT(?) WITHIN GROUP (ORDER BY daily_atm_iv) AS iv_high,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY daily_atm_iv) AS iv_median
            FROM (
                SELECT trade_date, AVG(iv) AS daily_atm_iv
                FROM options_data
                WHERE underlying=? AND expiry_tier='TIER1'
                  AND trade_date < ?
                  AND trade_date >= ?
                GROUP BY trade_date
            )
        """, [
            settings.IVR_LOW_PERCENTILE,
            settings.IVR_HIGH_PERCENTILE,
            underlying, trade_date, hist_start
        ]).fetchone()

        iv_low    = hist[0] if hist and hist[0] else atm_iv * 0.5
        iv_high   = hist[1] if hist and hist[1] else atm_iv * 1.5
        iv_median = hist[2] if hist and hist[2] else atm_iv

        ivr = (
            round((atm_iv - iv_low) / (iv_high - iv_low) * 100, 1)
            if iv_high > iv_low else 50.0
        )

        # IVP: percentile rank of current IV vs historical daily distribution.
        # F11: add consistent lookback window (same IV_LOOKBACK_DAYS as IVR)
        # and a minimum-sample guard: fewer than 20 trading days of history
        # produces a meaningless rank -- return None so gate C5 falls back to
        # the conservative "not cheap" posture (ivp_val = 100.0).
        lookback_date = (
            datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=settings.IV_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

        pct_row = conn.execute("""
            WITH daily_ivs AS (
                SELECT trade_date, AVG(iv) AS d
                FROM options_data
                WHERE underlying=? AND expiry_tier='TIER1'
                  AND trade_date < ?
                  AND trade_date >= ?
                GROUP BY trade_date
            )
            SELECT
                SUM(CASE WHEN d <= ? THEN 1 ELSE 0 END) AS below,
                COUNT(*) AS total
            FROM daily_ivs
        """, [underlying, trade_date, lookback_date, atm_iv]).fetchone()

        below = int(pct_row[0] or 0) if pct_row else 0
        total = int(pct_row[1] or 0) if pct_row else 0
        if total < 20:
            # Insufficient history -- return None; gate C5 treats this as
            # "IV not cheap" (ivp_val = 100.0) via the None guard in environment.py.
            ivp = None
        else:
            ivp = round(below / total * 100, 1)

        # HV20 -- 20-day realised volatility
        # F10: triple-nested query so LAG() window runs over the full ordered
        # history first; only then does the outer LIMIT 22 select the 22 most-
        # recent daily returns. The prior single-subquery form applied LIMIT
        # before LAG, making lag-pairs from a DESC-ordered cut-off set.
        hv20_row = conn.execute("""
            SELECT STDDEV(daily_ret) * SQRT(252) * 100 AS hv20
            FROM (
                SELECT daily_ret
                FROM (
                    SELECT
                        trade_date,
                        LN(
                            LAST(spot ORDER BY snap_time) /
                            LAG(LAST(spot ORDER BY snap_time)) OVER (ORDER BY trade_date)
                        ) AS daily_ret
                    FROM options_data
                    WHERE underlying=? AND trade_date <= ?
                    GROUP BY trade_date
                    ORDER BY trade_date DESC
                ) all_rets
                LIMIT 22
            )
        """, [underlying, trade_date]).fetchone()
        hv20 = round(hv20_row[0], 1) if hv20_row and hv20_row[0] else None

        vrp = round(atm_iv - hv20, 2) if hv20 is not None else None

        vrp_regime = (
            "OVERPRICED"  if vrp is not None and vrp >  settings.VRP_OVERPRICED_THRESHOLD  else
            "UNDERPRICED" if vrp is not None and vrp <  settings.VRP_UNDERPRICED_THRESHOLD else
            "FAIR"        if vrp is not None else
            "UNKNOWN"
        )

        return {
            "atm_iv":       round(atm_iv, 2),
            "ivr":          ivr,
            "ivp":          ivp,
            "iv_low":       round(iv_low, 2),
            "iv_high":      round(iv_high, 2),
            "iv_median":    round(iv_median, 2),
            "hv20":         hv20,
            "vrp":          vrp,
            "iv_hv_spread": vrp,        # backward-compat alias — keeps frontend working
            "vrp_regime":   vrp_regime,
            "shape":        get_term_structure(
                                conn, trade_date, snap_time, underlying
                            ).get("shape", "FLAT"),
        }
    except Exception as e:
        record_error("get_ivr_ivp")
        logger.warning("get_ivr_ivp error: {}", e)
        return {}


def get_term_structure(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> dict:
    """ATM IV per expiry tier with shape classification."""
    try:
        rows = conn.execute("""
            WITH spot_cte AS (
                SELECT AVG(spot) AS spot FROM options_data
                WHERE trade_date=? AND snap_time=? AND underlying=?
            )
            SELECT
                o.expiry_date,
                o.expiry_tier,
                MIN(o.dte)      AS dte,
                AVG(o.iv)       AS atm_iv,
                AVG(o.theta)    AS avg_theta
            FROM options_data o, spot_cte s
            WHERE o.trade_date=? AND o.snap_time=? AND o.underlying=?
              AND ABS(o.strike_price - s.spot) <= s.spot * 0.015
            GROUP BY o.expiry_date, o.expiry_tier
            ORDER BY dte
        """, [
            trade_date, snap_time, underlying,
            trade_date, snap_time, underlying,
        ]).fetchall()
        if not rows:
            return {"series": [], "shape": "FLAT", "near_iv": None, "far_iv": None}
        series = [{
            "expiry_date": r[0], "expiry_tier": r[1], "dte": r[2],
            "atm_iv":      round(r[3] or 0, 2),
            "avg_theta":   round(r[4] or 0, 4),
        } for r in rows]
        near_iv = series[0]["atm_iv"]  if series          else None
        far_iv  = series[-1]["atm_iv"] if len(series) > 1 else near_iv
        near_dte = int(series[0]["dte"] or 1) if series else 30
        far_dte  = int(series[-1]["dte"] or 60) if len(series) > 1 else near_dte
        shape   = _classify_shape(near_iv, far_iv, near_dte, far_dte)
        return {"series": series, "shape": shape, "near_iv": near_iv, "far_iv": far_iv}
    except Exception as e:
        logger.warning("get_term_structure error: {}", e)
        return {"series": [], "shape": "FLAT", "near_iv": None, "far_iv": None}


def _classify_shape(
    near_iv:  float | None,
    far_iv:   float | None,
    near_dte: int = 30,
    far_dte:  int = 60,
) -> str:
    # Issue-4: explicit None check — `not 0.0` is True in Python, so the old
    # `not near_iv` guard misclassified genuine zero IV as missing data.
    # near_iv == 0 guard prevents ZeroDivisionError on far_iv / near_iv.
    if near_iv is None or far_iv is None or near_iv == 0:
        return TermStructureShape.FLAT.value
    denom = math.sqrt(max(far_dte, 1)) - math.sqrt(max(near_dte, 1))
    if denom <= 0:
        # Same DTE or near > far — cannot compute meaningful slope
        return TermStructureShape.FLAT.value
    slope = (far_iv - near_iv) / denom
    if slope > settings.TS_CONTANGO_SLOPE:
        return TermStructureShape.CONTANGO.value
    if slope < settings.TS_BACKWARDATION_SLOPE:
        return TermStructureShape.BACKWARDATION.value
    return TermStructureShape.FLAT.value
