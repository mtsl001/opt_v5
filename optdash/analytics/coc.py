"""Cost-of-Carry analytics -- CoC velocity, ATM OBI, Futures OBI."""
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.metrics import record_error


def get_coc_latest(conn: duckdb.DuckDBPyConnection, trade_date: str,
                   snap_time: str, underlying: str) -> dict:
    """Latest CoC and V_CoC for a given snap."""
    try:
        row = conn.execute("""
            SELECT
                snap_time,
                AVG(fut_price)  AS fut_price,
                AVG(spot)       AS spot,
                AVG(fut_price) - AVG(spot) AS coc
            FROM options_data
            WHERE trade_date=? AND snap_time=? AND underlying=?
              AND instrument_type = 'FUT'
            GROUP BY snap_time
        """, [trade_date, snap_time, underlying]).fetchone()
        if not row:
            return {}
        coc = row[3] or 0
        vcoc = _compute_vcoc(conn, trade_date, snap_time, underlying)
        signal = _coc_signal(coc, vcoc)
        return {
            "snap_time": row[0], "fut_price": row[1], "spot": row[2],
            "coc": round(coc, 2), "v_coc_15m": round(vcoc, 2), "signal": signal,
        }
    except Exception as e:
        record_error("get_coc_latest")
        logger.warning("get_coc_latest error: {}", e)
        return {}


def get_coc_series(conn: duckdb.DuckDBPyConnection, trade_date: str, underlying: str) -> list[dict]:
    """Full-day CoC + V_CoC series for charting."""
    try:
        rows = conn.execute("""
            SELECT
                snap_time,
                AVG(fut_price) - AVG(spot) AS coc,
                AVG(spot) AS spot
            FROM options_data
            WHERE trade_date=? AND underlying=? AND instrument_type='FUT'
            GROUP BY snap_time ORDER BY snap_time
        """, [trade_date, underlying]).fetchall()
        if not rows:
            return []
        result = []
        for i, r in enumerate(rows):
            coc  = r[1] or 0
            vcoc = _compute_vcoc_from_series(rows, i)
            result.append({
                "snap_time": r[0], "coc": round(coc, 2), "spot": r[2],
                "v_coc_15m": round(vcoc, 2), "signal": _coc_signal(coc, vcoc),
            })
        return result
    except Exception as e:
        logger.warning("get_coc_series error: {}", e)
        return []


def get_atm_obi(conn: duckdb.DuckDBPyConnection, trade_date: str,
                snap_time: str, underlying: str) -> float:
    """ATM options order book imbalance (CE vs PE bid/ask sizes at ATM).

    F12 fix: replaced ORDER BY dist LIMIT 4 with a two-CTE approach:
      1. spot_cte -- current spot price
      2. min_dist -- exact minimum distance from spot to any TIER1 strike
    Only rows where ABS(strike - spot) == min_dist are selected, guaranteeing
    exactly 1 CE row + 1 PE row at the closest strike regardless of how many
    Parquet rows exist per option at that strike. Previously LIMIT 4 could
    return 3 CE + 1 PE (or 4 CE + 0 PE) when spot sat on a strike boundary
    or when one option type had more rows than the other, skewing OBI.
    """
    try:
        row = conn.execute("""
            WITH spot_cte AS (
                SELECT AVG(spot) AS spot FROM options_data
                WHERE trade_date=? AND snap_time=? AND underlying=?
            ),
            min_dist AS (
                SELECT MIN(ABS(o.strike_price - s.spot)) AS md
                FROM options_data o, spot_cte s
                WHERE o.trade_date=? AND o.snap_time=? AND o.underlying=?
                  AND o.expiry_tier = 'TIER1'
            ),
            atm AS (
                SELECT o.option_type, o.bid_qty, o.ask_qty
                FROM options_data o, spot_cte s, min_dist m
                WHERE o.trade_date=? AND o.snap_time=? AND o.underlying=?
                  AND o.expiry_tier = 'TIER1'
                  AND ABS(o.strike_price - s.spot) = m.md
            )
            SELECT
                SUM(CASE WHEN option_type='CE' THEN (bid_qty - ask_qty) ELSE 0 END) AS ce_flow,
                SUM(CASE WHEN option_type='PE' THEN (bid_qty - ask_qty) ELSE 0 END) AS pe_flow,
                SUM(bid_qty + ask_qty) AS total_qty
            FROM atm
        """, [
            trade_date, snap_time, underlying,   # spot_cte
            trade_date, snap_time, underlying,   # min_dist
            trade_date, snap_time, underlying,   # atm
        ]).fetchone()
        if not row or not row[2]:
            return 0.0
        return round(((row[0] or 0) - (row[1] or 0)) / (row[2] or 1), 4)
    except Exception as e:
        logger.debug("get_atm_obi error (non-critical): {}", e)
        return 0.0


def get_futures_obi(conn: duckdb.DuckDBPyConnection, trade_date: str,
                    snap_time: str, underlying: str) -> float:
    """Futures order book imbalance."""
    try:
        row = conn.execute("""
            SELECT
                SUM(bid_qty - ask_qty)        AS net_flow,
                SUM(bid_qty + ask_qty)        AS total_qty
            FROM options_data
            WHERE trade_date=? AND snap_time=? AND underlying=?
              AND instrument_type='FUT'
        """, [trade_date, snap_time, underlying]).fetchone()
        if not row or not row[1]:
            return 0.0
        return round((row[0] or 0) / (row[1] or 1), 4)
    except Exception as e:
        logger.debug("get_futures_obi error (non-critical): {}", e)
        return 0.0


def _compute_vcoc(conn: duckdb.DuckDBPyConnection, trade_date: str,
                  snap_time: str, underlying: str) -> float:
    """V_CoC 15-min velocity -- CoC diff over a true 15-minute time window.

    Computes a Python-side HH:MM cutoff (snap_time minus 15 minutes) and
    filters the DB query by snap_time >= cutoff.  This guarantees the
    window is always anchored to wall-clock time: if a 5-min snap is
    missed (feed gap, broker outage, restart), the query still returns
    only rows that fall within the genuine 15-min boundary rather than
    silently extending the window to 20-25 min (the old LIMIT 4 flaw).

    Returns 0.0 if fewer than 2 snaps exist in the window (e.g. early
    morning or very sparse data) -- same safe default as before.
    """
    try:
        h, m       = map(int, snap_time.split(":"))
        total_min  = h * 60 + m - 15
        if total_min < 0:
            return 0.0
        cutoff = f"{total_min // 60:02d}:{total_min % 60:02d}"
        rows = conn.execute("""
            SELECT snap_time, AVG(fut_price) - AVG(spot) AS coc
            FROM options_data
            WHERE trade_date=? AND underlying=? AND instrument_type='FUT'
              AND snap_time <= ? AND snap_time >= ?
            GROUP BY snap_time ORDER BY snap_time DESC
        """, [trade_date, underlying, snap_time, cutoff]).fetchall()
        if len(rows) < 2:
            return 0.0
        return round((rows[0][1] or 0) - (rows[-1][1] or 0), 2)
    except Exception:
        return 0.0


def _compute_vcoc_from_series(rows: list, i: int) -> float:
    """V_CoC from pre-fetched series (used in get_coc_series).

    Issue-10: lookback derived from SCHEDULER_INTERVAL_SECONDS so the 15-min
    V_CoC window remains correct at non-5-min tick intervals.
    """
    interval = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    lookback = max(1, 15 // interval)
    if i < lookback:
        return 0.0
    return round((rows[i][1] or 0) - (rows[i - lookback][1] or 0), 2)


def _coc_signal(coc: float, vcoc: float) -> str:
    if vcoc > settings.VCOC_BULL_THRESHOLD:
        return "VELOCITY_BULL"
    if vcoc < settings.VCOC_BEAR_THRESHOLD:
        return "VELOCITY_BEAR"
    if coc < settings.COC_DISCOUNT_THRESHOLD:
        return "DISCOUNT"
    return "NORMAL"
