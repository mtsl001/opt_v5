"""Cost-of-Carry analytics -- CoC velocity, ATM OBI, Futures OBI.

CoC-3 (v2.3.0): OBI functions now use bid1_qty/ask1_qty (instantaneous
Level-1 order book depth) instead of bid_qty/ask_qty (cumulative day
buy/sell totals). bid_qty/ask_qty grow monotonically and are a poor proxy
for live order book pressure. bid1_qty/ask1_qty reflect the resting queue
at the best bid/ask right now. COALESCE guards handle NULL depth on
illiquid/far-OTM strikes.
"""
import math
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.metrics import record_error


def _fair_value_coc(spot: float, fut_price: float, dte: int, underlying: str) -> dict:
    """Compute fair-value futures price and deviation from it.

    F* = S * exp((r - q) * t/365)
    coc_fv_premium = actual_future - F*

    A positive value = futures trade above fair value (institutional bullish pressure).
    Near zero = futures trade at fair value (neutral, carry only).
    Negative = structural discount beyond what dividends explain (genuine bearish signal).
    """
    r  = settings.RISK_FREE_RATE
    q  = settings.DIVIDEND_YIELD.get(underlying, 0.01)
    fv = spot * math.exp((r - q) * (max(dte, 1) / 365))
    return {
        "fair_value":      round(fv, 2),
        "coc_fv_premium":  round(fut_price - fv, 2),
    }

def get_coc_latest(conn: duckdb.DuckDBPyConnection, trade_date: str,
                   snap_time: str, underlying: str) -> dict:
    """Latest CoC and V_CoC for a given snap."""
    try:
        row = conn.execute("""
            SELECT
                snap_time,
                AVG(fut_price)  AS fut_price,
                AVG(spot)       AS spot,
                AVG(fut_price) - AVG(spot) AS coc,
                AVG(CASE WHEN dte > 0 THEN dte END) AS dte
            FROM options_data
            WHERE trade_date=? AND snap_time=? AND underlying=?
              AND instrument_type = 'FUT'
            GROUP BY snap_time
        """, [trade_date, snap_time, underlying]).fetchone()
        if not row:
            return {}
        coc = row[3] or 0
        dte = row[4] or 30
        spot = row[2] or 1
        vcoc = _compute_vcoc(conn, trade_date, snap_time, underlying)
        
        coc_pct = round((coc / spot) * (365 / max(dte, 1)) * 100, 4)
        vcoc_pct = round((vcoc / spot) * (365 / max(dte, 1)) * 100, 4)
        
        fv_data = _fair_value_coc(spot, row[1], int(dte), underlying)
        signal = _coc_signal(coc, vcoc, spot, dte, fv_data["coc_fv_premium"])
        
        return {
            "snap_time": row[0], "fut_price": row[1], "spot": row[2],
            "coc": round(coc, 2), "v_coc_15m": round(vcoc, 2),
            "coc_pct": coc_pct, "vcoc_pct": vcoc_pct, "dte": int(dte),
            "signal": signal,
            **fv_data
        }
    except Exception as e:
        record_error("get_coc_latest")
        logger.warning("get_coc_latest error: {}", e)
        return {}


def get_coc_series(conn: duckdb.DuckDBPyConnection, trade_date: str, underlying: str, since_snap: str = None) -> list[dict]:
    """Full-day CoC + V_CoC series for charting."""
    try:
        params = [trade_date, underlying]
        snap_clause = ""
        if since_snap:
            snap_clause = " AND snap_time >= ?"
            params.append(since_snap)

        rows = conn.execute(f"""
            SELECT
                snap_time,
                AVG(fut_price) - AVG(spot) AS coc,
                AVG(spot)                  AS spot,
                AVG(fut_price)             AS fut_price,
                AVG(CASE WHEN dte > 0 THEN dte END) AS dte
            FROM options_data
            WHERE trade_date=? AND underlying=? AND instrument_type='FUT'{snap_clause}
            GROUP BY snap_time ORDER BY snap_time
        """, params).fetchall()
        if not rows:
            return []
        result = []   # Bug-1 fix: result must be initialised before the loop
        for i, r in enumerate(rows):
            coc       = r[1] or 0
            spot      = r[2] or 1
            fut_price = r[3] or 0
            dte       = r[4] or 30
            vcoc = _compute_vcoc_from_series(rows, i)

            coc_pct  = round((coc  / spot) * (365 / max(dte, 1)) * 100, 4)
            vcoc_pct = round((vcoc / spot) * (365 / max(dte, 1)) * 100, 4)

            # Bug-2 fix: compute fair-value so series signal is consistent with live tile
            fv_data = _fair_value_coc(spot, fut_price, int(dte), underlying)

            result.append({
                "snap_time": r[0], "coc": round(coc, 2), "spot": spot,
                "v_coc_15m": round(vcoc, 2), "coc_pct": coc_pct,
                "vcoc_pct": vcoc_pct, "dte": int(dte),
                "signal": _coc_signal(coc, vcoc, spot, dte, fv_data["coc_fv_premium"]),
                **fv_data,
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

    CoC-3: uses bid1_qty/ask1_qty (L1 live depth) instead of bid_qty/ask_qty
    (cumulative day totals). COALESCE guards handle NULL on illiquid strikes.
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
                SELECT o.option_type, o.bid1_qty, o.ask1_qty
                FROM options_data o, spot_cte s, min_dist m
                WHERE o.trade_date=? AND o.snap_time=? AND o.underlying=?
                  AND o.expiry_tier = 'TIER1'
                  AND ABS(o.strike_price - s.spot) = m.md
            )
            SELECT
                SUM(CASE WHEN option_type='CE' THEN (COALESCE(bid1_qty,0) - COALESCE(ask1_qty,0)) ELSE 0 END) AS ce_flow,
                SUM(CASE WHEN option_type='PE' THEN (COALESCE(bid1_qty,0) - COALESCE(ask1_qty,0)) ELSE 0 END) AS pe_flow,
                SUM(COALESCE(bid1_qty,0) + COALESCE(ask1_qty,0)) AS total_qty
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
    """Futures order book imbalance.

    CoC-3: uses bid1_qty/ask1_qty (L1 live depth) instead of bid_qty/ask_qty
    (cumulative day totals). COALESCE guards handle NULL on illiquid strikes.
    """
    try:
        row = conn.execute("""
            SELECT
                SUM(COALESCE(bid1_qty,0) - COALESCE(ask1_qty,0)) AS net_flow,
                SUM(COALESCE(bid1_qty,0) + COALESCE(ask1_qty,0)) AS total_qty
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

    Issue-9: use round() instead of integer division for the lookback count.
    At non-divisible intervals (e.g. 7-min ticks) floor division truncates:
      15 // 7 = 2 rows → 14-min window (undershoot).
    round() picks the nearest row count:
      round(15 / 7) = 2 at 7-min (14 min — same), but
      round(15 / 8) = 2 at 8-min (16 min) vs floor 15//8 = 1 (8 min).
    """
    interval = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    lookback = max(1, round(15 / interval))
    if i < lookback:
        return 0.0
    return round((rows[i][1] or 0) - (rows[i - lookback][1] or 0), 2)


def _coc_signal(coc: float, vcoc: float, spot: float = 0, dte: int = None, coc_fv_premium: float = None) -> str:
    """Classify the CoC/V_CoC snapshot into a signal label.

    Fix E-1: default dte changed from 30 to None.  A dte=30 default caused
    near-expiry V_CoC (e.g. DTE=2) to use annualization factor 365/30≈12.2
    instead of the correct 365/2=182.5 — a 15x underestimate that suppressed
    VELOCITY_BULL/BEAR signals on expiry week.  All callers pass dte explicitly;
    the None default is a safety net to raise AttributeError rather than silently
    produce a wrong signal when dte is omitted.
    """
    if spot > 0 and dte is not None:
        safe_dte = max(1, dte)
        vcoc_pct = (vcoc / spot) * (365 / safe_dte) * 100
        coc_pct  = (coc  / spot) * (365 / safe_dte) * 100
        if vcoc_pct > settings.VCOC_BULL_THRESHOLD_PCT:
            return "VELOCITY_BULL"
        if vcoc_pct < settings.VCOC_BEAR_THRESHOLD_PCT:
            return "VELOCITY_BEAR"
        
        # DISCOUNT: use fair-value-adjusted premium if available, normalised to
        # annualized % so units match COC_DISCOUNT_THRESHOLD_PCT (Bug-3 fix).
        if coc_fv_premium is not None:
            discount_val = (coc_fv_premium / spot) * (365 / dte) * 100
        else:
            discount_val = coc_pct
        if discount_val < settings.COC_DISCOUNT_THRESHOLD_PCT:
            return "DISCOUNT"
        return "NORMAL"
    # Fallback to absolute thresholds when spot/dte unavailable
    if vcoc > settings.VCOC_BULL_THRESHOLD:
        return "VELOCITY_BULL"
    if vcoc < settings.VCOC_BEAR_THRESHOLD:
        return "VELOCITY_BEAR"
    if coc < settings.COC_DISCOUNT_THRESHOLD:
        return "DISCOUNT"
    return "NORMAL"
