"""VEX/CEX analytics — Vanna Exposure + Charm Exposure.

Classifier parameter change (Fix-B):
  _classify_vex(vex_total, underlying) and
  _classify_cex(cex_total, underlying) now look up per-underlying
  thresholds from VEX_THRESHOLDS / CEX_CHARM_THRESHOLD / CEX_VANNA_THRESHOLD
  in config, instead of using global scalar constants for all indices.
"""
from datetime import date

import duckdb
from loguru import logger
from optdash.config import settings
from optdash.models import VexSignal, CexSignal
from optdash.metrics import record_error


def get_vex_cex_current(conn: duckdb.DuckDBPyConnection, trade_date: str,
                        snap_time: str, underlying: str) -> dict:
    """Current VEX/CEX snapshot."""
    try:
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN option_type='CE' THEN vex ELSE 0 END) / 1e6  AS vex_ce_M,
                SUM(CASE WHEN option_type='PE' THEN vex ELSE 0 END) / 1e6  AS vex_pe_M,
                SUM(vex) / 1e6                                              AS vex_total_M,
                SUM(CASE WHEN option_type='CE' THEN cex ELSE 0 END) / 1e6  AS cex_ce_M,
                SUM(CASE WHEN option_type='PE' THEN cex ELSE 0 END) / 1e6  AS cex_pe_M,
                SUM(cex) / 1e6                                              AS cex_total_M,
                AVG(spot)                                                   AS spot,
                MIN(dte)                                                    AS dte
            FROM options_data
            WHERE trade_date=? AND snap_time=? AND underlying=?
              AND expiry_tier='TIER1'
        """, [trade_date, snap_time, underlying]).fetchone()
        if not row:
            return {}
        vex_total = row[2] or 0
        cex_total = row[5] or 0
        dte       = row[7] or 7
        dealer_oc  = _is_dealer_oclock(snap_time, dte, underlying, trade_date)
        vex_signal = _classify_vex(vex_total, underlying)
        cex_signal = _classify_cex(cex_total, underlying)
        interp     = _interpret(vex_signal, cex_signal, dealer_oc)
        return {
            "snap_time": snap_time,
            "vex_ce_M": round(row[0] or 0, 2), "vex_pe_M": round(row[1] or 0, 2),
            "vex_total_M": round(vex_total, 2),
            "cex_ce_M": round(row[3] or 0, 2), "cex_pe_M": round(row[4] or 0, 2),
            "cex_total_M": round(cex_total, 2),
            "spot": row[6], "dte": dte,
            "vex_signal": vex_signal, "cex_signal": cex_signal,
            "dealer_oclock": dealer_oc, "interpretation": interp,
        }
    except Exception as e:
        record_error("get_vex_cex_current")
        logger.warning("get_vex_cex_current error: {}", e)
        return {}


def get_vex_cex_full(conn: duckdb.DuckDBPyConnection, trade_date: str,
                     snap_time: str, underlying: str) -> dict:
    """Full series + by-strike breakdown."""
    series    = _get_vex_cex_series(conn, trade_date, underlying)
    by_strike = _get_by_strike(conn, trade_date, snap_time, underlying)
    current   = get_vex_cex_current(conn, trade_date, snap_time, underlying)
    return {
        "series": series, "by_strike": by_strike,
        "current": current if current else None,
        "dealer_oclock": current.get("dealer_oclock", False),
        "interpretation": current.get("interpretation", ""),
    }


def _get_vex_cex_series(conn, trade_date, underlying) -> list[dict]:
    try:
        rows = conn.execute("""
            SELECT snap_time,
                SUM(vex)/1e6 AS vex_total_M,
                SUM(CASE WHEN option_type='CE' THEN vex ELSE 0 END)/1e6 AS vex_ce_M,
                SUM(CASE WHEN option_type='PE' THEN vex ELSE 0 END)/1e6 AS vex_pe_M,
                SUM(cex)/1e6 AS cex_total_M,
                SUM(CASE WHEN option_type='CE' THEN cex ELSE 0 END)/1e6 AS cex_ce_M,
                SUM(CASE WHEN option_type='PE' THEN cex ELSE 0 END)/1e6 AS cex_pe_M,
                AVG(spot) AS spot, MIN(dte) AS dte
            FROM options_data
            WHERE trade_date=? AND underlying=? AND expiry_tier='TIER1'
            GROUP BY snap_time ORDER BY snap_time
        """, [trade_date, underlying]).fetchall()
        result = []
        for r in rows:
            vex, cex, dte = r[1] or 0, r[4] or 0, r[8] or 7
            dealer_oc  = _is_dealer_oclock(r[0], dte, underlying, trade_date)
            # Pass underlying so per-underlying thresholds are applied
            # consistently across the series (not just the current snap).
            vex_sig = _classify_vex(vex, underlying)
            cex_sig = _classify_cex(cex, underlying)
            result.append({
                "snap_time": r[0],
                "vex_total_M": round(vex, 2), "vex_ce_M": round(r[2] or 0, 2),
                "vex_pe_M": round(r[3] or 0, 2), "cex_total_M": round(cex, 2),
                "cex_ce_M": round(r[5] or 0, 2), "cex_pe_M": round(r[6] or 0, 2),
                "spot": r[7], "dte": dte, "dealer_oclock": dealer_oc,
                "vex_signal": vex_sig, "cex_signal": cex_sig,
                "interpretation": _interpret(vex_sig, cex_sig, dealer_oc),
            })
        return result
    except Exception as e:
        logger.warning("_get_vex_cex_series error: {}", e)
        return []


def _get_by_strike(conn, trade_date, snap_time, underlying) -> list[dict]:
    try:
        rows = conn.execute("""
            SELECT strike_price, option_type,
                   (strike_price - AVG(spot) OVER()) / NULLIF(AVG(spot) OVER(), 0) * 100 AS moneyness_pct,
                   SUM(vex)/1e6 AS vex_M, SUM(cex)/1e6 AS cex_M,
                   SUM(oi) AS oi, AVG(iv) AS iv, MIN(dte) AS dte
            FROM options_data
            WHERE trade_date=? AND snap_time=? AND underlying=? AND expiry_tier='TIER1'
            GROUP BY strike_price, option_type ORDER BY strike_price
        """, [trade_date, snap_time, underlying]).fetchall()
        return [{
            "strike_price": r[0], "option_type": r[1],
            # Fix VEX-2: return None for missing/zero spot rows instead of
            # coercing NULL moneyness to 0.0 via `or 0`, which misclassifies
            # every strike as exactly at-the-money when AVG(spot) is absent.
            # NULLIF in the SQL prevents DivisionByZero; None here lets the
            # frontend display "N/A" correctly rather than rendering a phantom
            # 0% moneyness band across all strikes.
            "moneyness_pct": round(r[2], 2) if r[2] is not None else None,
            "vex_M": round(r[3] or 0, 2), "cex_M": round(r[4] or 0, 2),
            "oi": r[5], "iv": round(r[6] or 0, 2), "dte": r[7],
        } for r in rows]
    except Exception as e:
        logger.warning("_get_by_strike error: {}", e)
        return []


def _classify_vex(vex_total: float, underlying: str = "") -> str:
    """Classify VEX signal using per-underlying threshold.

    Threshold lookup order:
      1. VEX_THRESHOLDS[underlying]   (per-underlying, primary)
      2. VEX_BULL_THRESHOLD           (global fallback for unknown underlyings)

    Threshold is now applied symmetrically:
      > +threshold -> VEX_BULLISH
      < -threshold -> VEX_BEARISH
      otherwise   -> NEUTRAL

    Fix-B: previously VEX_BULL_THRESHOLD was 0.0 for all underlyings,
    causing any non-zero VEX to fire a directional signal (pure noise).
    """
    threshold = settings.VEX_THRESHOLDS.get(underlying, settings.VEX_BULL_THRESHOLD)
    if vex_total > threshold:
        return VexSignal.VEX_BULLISH.value
    if vex_total < -threshold:
        return VexSignal.VEX_BEARISH.value
    return VexSignal.NEUTRAL.value


def _classify_cex(cex_total: float, underlying: str = "") -> str:
    """Classify CEX signal using per-underlying thresholds.

    Level mapping:
      strong_thr  <- CEX_CHARM_THRESHOLD[underlying]  -> STRONG_CHARM_BID
      bid_thr     <- CEX_VANNA_THRESHOLD[underlying]  -> CHARM_BID
      -strong_thr                                     -> CHARM_PRESSURE (symmetric)

    Fix-B: previously used global scalars CEX_STRONG_BID=20.0 and CEX_BID=5.0
    for all underlyings. MIDCPNIFTY and NIFTYNXT50 have ~10x lower CEX
    magnitudes than BANKNIFTY, so they never crossed the global thresholds.
    """
    strong_thr   = settings.CEX_CHARM_THRESHOLD.get(underlying, settings.CEX_STRONG_BID)
    bid_thr      = settings.CEX_VANNA_THRESHOLD.get(underlying, settings.CEX_BID)
    pressure_thr = -strong_thr  # symmetric negative

    if cex_total >= strong_thr:
        return CexSignal.STRONG_CHARM_BID.value
    if cex_total >= bid_thr:
        return CexSignal.CHARM_BID.value
    if cex_total <= pressure_thr:
        return CexSignal.CHARM_PRESSURE.value
    return CexSignal.NEUTRAL.value


def _is_dealer_oclock(snap_time: str, dte: int, underlying: str, trade_date: str) -> bool:
    """True when DTE<=1, snap_time >= DEALER_OCLOCK_START, AND today is the correct
    weekly expiry weekday for this underlying.

    Fix VEX-1: use date.fromisoformat(trade_date) instead of datetime.now(IST).
    The previous wall-clock weekday caused _is_dealer_oclock to return the wrong
    result for any historical data review, backtest, or replay session run on a
    different calendar day than the data being processed (e.g. reviewing last
    Tuesday's FINNIFTY data on a Wednesday would incorrectly report no dealer
    O'Clock activity, suppressing the correct charm-flow interpretation).

    Removed IST / ZoneInfo dependency -- no longer needed after this fix.

    Rationale: each underlying has a different expiry day --
      FINNIFTY   -> Tuesday   (weekday 1)
      MIDCPNIFTY -> Monday    (weekday 0)
      NIFTYNXT50 -> Friday    (weekday 4)
      SENSEX     -> Friday    (weekday 4)
      NIFTY / BANKNIFTY -> Thursday (weekday 3)  [default]

    Applying a single Thursday-centric window to all underlyings causes
    false O'Clock badges and corrupts Gate bonus points on non-Thursday days.
    """
    if dte > settings.DEALER_OCLOCK_DTE:
        return False
    if snap_time < settings.DEALER_OCLOCK_START:
        return False
    expected_weekday = settings.EXPIRY_WEEKDAY.get(underlying, 3)  # default Thursday
    today_weekday    = date.fromisoformat(trade_date).weekday()
    return today_weekday == expected_weekday


def _interpret(vex_signal: str, cex_signal: str, dealer_oc: bool) -> str:
    if dealer_oc:
        return "Dealer O'Clock active — charm flows dominate expiry day mechanics."
    if vex_signal == VexSignal.VEX_BULLISH.value:
        return "IV drop forces dealer buying — bullish mechanical bias."
    if vex_signal == VexSignal.VEX_BEARISH.value:
        return "IV rise forces dealer selling — bearish mechanical pressure."
    if cex_signal == CexSignal.STRONG_CHARM_BID.value:
        return "Strong charm bid — time decay buying pressure supports upside."
    return "No dominant dealer flow signal."
