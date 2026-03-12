"""GEX analytics -- Net GEX series, regime, max pain, spot summary."""
import numpy as np
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.models import GEXRegime
from optdash.metrics import record_error


def get_net_gex(
    conn:         duckdb.DuckDBPyConnection,
    trade_date:   str,
    snap_time:    str,
    underlying:   str,
    _peak_cache:  dict | None = None,
) -> dict:
    """Latest-snap GEX snapshot with regime classification.

    _peak_cache: optional dict shared by the caller across multiple get_net_gex
    calls within the same scheduler tick.
    """
    try:
        row = conn.execute("""
            SELECT
                snap_time,
                SUM(gex)                                                              AS gex_all_raw,
                SUM(CASE WHEN expiry_tier IN ('TIER1','TIER2') THEN gex ELSE 0 END)  AS gex_near_raw,
                SUM(CASE WHEN expiry_tier = 'TIER3'            THEN gex ELSE 0 END)  AS gex_far_raw,
                MAX(spot) AS spot
            FROM options_data
            WHERE trade_date = ? AND snap_time = ? AND underlying = ?
            GROUP BY snap_time
        """, [trade_date, snap_time, underlying]).fetchone()
        if not row:
            return {}
        gex_all  = (row[1] or 0) / settings.GEX_SCALING
        gex_near = (row[2] or 0) / settings.GEX_SCALING
        gex_far  = (row[3] or 0) / settings.GEX_SCALING

        cache_key = (trade_date, underlying)
        if _peak_cache is not None and cache_key in _peak_cache:
            peak = _peak_cache[cache_key]
        else:
            peak = _get_gex_peak(conn, trade_date, underlying)
            if _peak_cache is not None:
                _peak_cache[cache_key] = peak

        # Fix GEX-2: use != 0 guard (not truthy `if peak`) so a genuine
        # peak=0 day (all strikes perfectly delta-hedged, net GEX = 0 all day)
        # receives pct_of_peak=0 rather than 100.0.
        # Old behaviour: `if peak else 100.0` treated 0 as falsy, set pct=100,
        # then _classify_regime(0, 100) returned POSITIVE_CHOP (strong gamma
        # wall) -- the exact opposite of the correct POSITIVE_DECLINING
        # (neutral/declining gamma) classification for a zero-GEX environment.
        pct    = (abs(gex_all) / peak * 100) if peak != 0 else 0.0
        regime = _classify_regime(gex_all, pct)
        return {
            "snap_time":   row[0],
            "gex_all_B":  round(gex_all, 3),
            "gex_near_B": round(gex_near, 3),
            "gex_far_B":  round(gex_far, 3),
            "pct_of_peak": round(pct, 1),
            "regime": regime.value,
            "spot":   row[4],
        }
    except Exception as e:
        record_error("get_net_gex")
        logger.warning("get_net_gex error: {}", e)
        return {}


def get_gex_series(conn: duckdb.DuckDBPyConnection, trade_date: str,
                   underlying: str) -> list[dict]:
    """Full-day GEX series with pct_of_peak for charting."""
    try:
        rows = conn.execute("""
            SELECT
                snap_time,
                SUM(gex) / ?                                                              AS gex_all_B,
                SUM(CASE WHEN expiry_tier IN ('TIER1','TIER2') THEN gex ELSE 0 END) / ?  AS gex_near_B,
                SUM(CASE WHEN expiry_tier = 'TIER3'            THEN gex ELSE 0 END) / ?  AS gex_far_B
            FROM options_data
            WHERE trade_date = ? AND underlying = ?
            GROUP BY snap_time ORDER BY snap_time
        """, [settings.GEX_SCALING, settings.GEX_SCALING, settings.GEX_SCALING,
               trade_date, underlying]).fetchall()
        if not rows:
            return []
        # Fix GEX-2: use max(...) or 0 (not or 1.0) so a zero-GEX day produces
        # pct_of_peak=0 for every snap instead of artificially anchoring to 1.0.
        peak = max(abs(r[1] or 0) for r in rows) or 0.0
        result = []
        for r in rows:
            gex         = r[1] or 0
            # Fix GEX-2: consistent != 0 guard in the series path.
            pct_of_peak = round(abs(gex) / peak * 100, 1) if peak != 0 else 0.0
            regime      = _classify_regime(gex, pct_of_peak)
            result.append({
                "snap_time":   r[0],
                "gex_all_B":  round(r[1] or 0, 3),
                "gex_near_B": round(r[2] or 0, 3),
                "gex_far_B":  round(r[3] or 0, 3),
                "pct_of_peak": pct_of_peak,
                "regime": regime.value,
            })
        return result
    except Exception as e:
        logger.warning("get_gex_series error: {}", e)
        return []


def get_spot_summary(conn: duckdb.DuckDBPyConnection, trade_date: str,
                     underlying: str) -> dict:
    """Current spot with day OHLC and change pct."""
    try:
        row = conn.execute("""
            SELECT
                MAX(snap_time)           AS snap_time,
                arg_max(spot, snap_time) AS spot,
                arg_min(spot, snap_time) AS day_open,
                MAX(spot)                AS day_high,
                MIN(spot)                AS day_low
            FROM options_data
            WHERE trade_date = ? AND underlying = ?
        """, [trade_date, underlying]).fetchone()
        if not row or not row[1]:
            return {}
        spot  = row[1]
        open_ = row[2] or spot
        return {
            "snap_time":  row[0],
            "spot":       spot,
            "day_open":   open_,
            "day_high":   row[3],
            "day_low":    row[4],
            "change_pct": round((spot - open_) / open_ * 100, 2) if open_ else 0,
        }
    except Exception as e:
        logger.warning("get_spot_summary error: {}", e)
        return {}


def get_max_pain(
    conn:         duckdb.DuckDBPyConnection,
    trade_date:   str,
    snap_time:    str,
    underlying:   str,
    expiry_date:  str,
) -> dict:
    """Max pain strike via vectorised NumPy outer-subtraction."""
    try:
        rows = conn.execute("""
            SELECT strike_price,
                   SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END) AS ce_oi,
                   SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) AS pe_oi,
                   MAX(spot) AS spot
            FROM options_data
            WHERE trade_date=? AND snap_time=? AND underlying=? AND expiry_date=?
            GROUP BY strike_price ORDER BY strike_price
        """, [trade_date, snap_time, underlying, expiry_date]).fetchall()
        if not rows:
            return {"max_pain": None, "distance_pct": None}

        strikes_arr = np.array([r[0] for r in rows], dtype=float)
        ce_arr      = np.array([r[1] or 0 for r in rows], dtype=float)
        pe_arr      = np.array([r[2] or 0 for r in rows], dtype=float)
        spot        = rows[0][3] or 0

        diff        = strikes_arr[:, None] - strikes_arr[None, :]
        ce_pain_mat = np.maximum(0.0,  diff) * ce_arr
        pe_pain_mat = np.maximum(0.0, -diff) * pe_arr
        pain_arr    = ce_pain_mat.sum(axis=1) + pe_pain_mat.sum(axis=1)

        min_idx    = int(np.argmin(pain_arr))
        min_strike = float(strikes_arr[min_idx])

        dist = ((spot - min_strike) / min_strike * 100) if min_strike else None
        return {
            "max_pain":     min_strike,
            "distance_pct": round(dist, 3) if dist is not None else None,
            "spot":         spot,
        }
    except Exception as e:
        logger.warning("get_max_pain error: {}", e)
        return {"max_pain": None, "distance_pct": None}


def _classify_regime(gex: float, pct_of_peak: float) -> GEXRegime:
    """Three-state GEX regime classification.

    NEGATIVE_TREND:     gex < 0  (dealers net short gamma -- trending market)
    POSITIVE_DECLINING: gex >= 0 and pct_of_peak <= GEX_DECLINE_THRESHOLD
                        (gamma wall weakening or absent -- directional move building)
    POSITIVE_CHOP:      gex > 0 and pct_of_peak > GEX_DECLINE_THRESHOLD
                        (strong gamma wall -- mean-reversion / choppy)
    """
    if gex < 0:
        return GEXRegime.NEGATIVE_TREND
    if pct_of_peak <= settings.GEX_DECLINE_THRESHOLD * 100:
        return GEXRegime.POSITIVE_DECLINING
    return GEXRegime.POSITIVE_CHOP


def _get_gex_peak(conn: duckdb.DuckDBPyConnection, trade_date: str,
                  underlying: str) -> float:
    """Day peak absolute GEX (denominator for pct_of_peak).

    Fix GEX-2: returns 0.0 (not 1.0) when the day's peak is genuinely zero
    (all snaps have balanced GEX = 0). Returning 1.0 was a division guard
    that masked the zero case: get_net_gex then set pct_of_peak=0/1*100=0
    and _classify_regime(0, 0) returned POSITIVE_DECLINING -- accidentally
    correct, but only because 0/1=0 happened to give the right pct value.
    The critical failure was when gex_all was also non-zero but peak was
    substituted with 1.0: pct_of_peak became abs(gex_all)*100 (off by a
    factor of GEX_SCALING), producing wildly wrong regime classifications.
    The `!= 0` guard in get_net_gex and get_gex_series now handles the
    zero-peak case explicitly, making 0.0 the correct sentinel to return.
    """
    try:
        row = conn.execute("""
            SELECT MAX(ABS(gex_sum)) FROM (
                SELECT snap_time, SUM(gex) / ? AS gex_sum
                FROM options_data
                WHERE trade_date=? AND underlying=?
                GROUP BY snap_time
            )
        """, [settings.GEX_SCALING, trade_date, underlying]).fetchone()
        # Return 0.0 (not 1.0) when peak is None or genuinely 0 -- callers
        # use `if peak != 0` to guard division, so 0.0 is the correct sentinel.
        return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0
