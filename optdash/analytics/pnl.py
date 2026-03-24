"""PnL analytics -- theta SL, Greeks attribution, theta clock."""
from typing import Any
from loguru import logger
from optdash.config import settings
from optdash.utils import snap_to_min


def compute_theta_sl(entry_premium: float, theta: float, minutes_elapsed: int) -> float:
    """
    Theta-adjusted stop loss.
    SL floor rises as theta erodes time value -- locks in decay-adjusted minimum exit.
    Uses AI_SL_PCT from config (default 0.35 --> SL at 65% of adjusted entry).
    """
    sl_multiplier  = 1.0 - settings.AI_SL_PCT    # honours config -- not hardcoded
    # Q-2: derive trading session length dynamically, same as compute_pnl_attribution.
    # Avoids silent error when SEBI extends/shortens market hours or muhurat sessions.
    TRADING_MINS   = _minutes_between("09:15", settings.SESSION_CLOSING_START) or 390
    theta_per_min  = abs(theta or 0) / TRADING_MINS
    decay          = theta_per_min * minutes_elapsed
    adjusted_entry = max(0.01, entry_premium - decay)
    return round(adjusted_entry * sl_multiplier, 2)


def compute_pnl_attribution(
    entry:   dict[str, Any],
    current: dict[str, Any],
) -> dict[str, float]:
    """
    Greeks-based PnL decomposition.
    Actual PnL = delta_pnl + gamma_pnl + vega_pnl + theta_pnl + unexplained.

    Note on theta_pnl accuracy: the formula divides entry-snap theta by the
    full 6.5-hour trading session. For short intraday holds (<= 90 min) this
    is a reasonable first-order approximation. For holds > 120 min on near-
    expiry options (DTE <= 3) theta is convex and entry-theta understates
    erosion, inflating `unexplained`. A full fix requires recording per-snap
    theta in position_snaps and integrating -- deferred as a schema change.
    """
    try:
        spot_chg = (current.get("spot", 0) or 0) - (entry.get("spot", 0) or 0)
        iv_chg   = (current.get("iv",   0) or 0) - (entry.get("iv",   0) or 0)

        entry_t   = entry.get("snap_time",   "09:15")
        current_t = current.get("snap_time", "09:15")
        minutes   = _minutes_between(entry_t, current_t)

        delta = entry.get("delta",  0) or 0
        gamma = entry.get("gamma",  0) or 0
        vega  = entry.get("vega",   0) or 0
        theta = entry.get("theta",  0) or 0

        delta_pnl = round(delta * spot_chg, 2)
        gamma_pnl = round(0.5 * gamma * spot_chg ** 2, 2)
        vega_pnl  = round(vega * iv_chg, 2)
        
        TRADING_MINS = _minutes_between("09:15", settings.SESSION_CLOSING_START)
        theta_pnl = round(theta / TRADING_MINS * minutes, 2)

        # Warn when theta linearity assumption is most strained.
        if minutes > 120 and (entry.get("dte") or 99) <= 3:
            logger.debug(
                "theta_pnl may understate decay: {}min hold on DTE={} option "
                "-- unexplained={:.2f} includes convexity error",
                minutes, entry.get("dte"),
                round((delta_pnl + gamma_pnl + vega_pnl + theta_pnl)
                      - round((current.get("ltp", 0) or 0) - (entry.get("premium", 0) or 0), 2), 2),
            )

        theo_pnl    = delta_pnl + gamma_pnl + vega_pnl + theta_pnl
        entry_p     = entry.get("premium", 0)  or 0
        actual_ltp  = current.get("ltp",   0)  or 0
        actual_pnl  = round(actual_ltp - entry_p, 2)
        unexplained = round(actual_pnl - theo_pnl, 2)

        return {
            "delta_pnl":       delta_pnl,
            "gamma_pnl":       gamma_pnl,
            "vega_pnl":        vega_pnl,
            "theta_pnl":       theta_pnl,
            "theoretical_pnl": theo_pnl,
            "actual_pnl":      actual_pnl,
            "unexplained":     unexplained,
        }
    except Exception as e:
        logger.warning("compute_pnl_attribution error: {}", e)
        return {
            "delta_pnl": 0, "gamma_pnl": 0, "vega_pnl": 0, "theta_pnl": 0,
            "theoretical_pnl": 0, "actual_pnl": 0, "unexplained": 0,
        }


def compute_theta_clock(
    theta:     float,
    dte:       int,
    ltp:       float,
    target:    float,
    snap_time: str = "09:15",
    status:    str = "IN_TRADE",
) -> dict:
    """
    Time remaining before theta erosion exceeds the gap to target.
    Returns hours_remaining and is_urgent flag.

    snap_time: current snap in 'HH:MM' format -- used to derive remaining
               market minutes so theta_per_hour scales correctly intraday.
               Defaults to '09:15' (most conservative) for old callers that
               do not pass snap_time.
    status:    only IN_TRADE / ACCEPTED positions need a theta clock;
               returns null result for all other statuses.
    """
    if status not in ("IN_TRADE", "ACCEPTED"):
        return {"hours_remaining": None, "is_urgent": False}

    close_str = getattr(settings, 'SESSION_CLOSING_START', '15:30')
    try:
        h, m = map(int, close_str.split(':'))
        _MARKET_CLOSE_MIN = h * 60 + m
    except Exception:
        _MARKET_CLOSE_MIN = 15 * 60 + 30          # 15:30 in minutes fallback
    snap_min          = _snap_to_min(snap_time)
    remaining_min     = max(30, _MARKET_CLOSE_MIN - snap_min)  # floor: 30 min

    theta_per_hour = abs(theta or 0) / (remaining_min / 60)
    gap_to_target  = max(0, (target or ltp) - (ltp or 0))

    if theta_per_hour <= 0 or gap_to_target <= 0:
        return {"hours_remaining": None, "is_urgent": False}

    hours = gap_to_target / theta_per_hour
    return {"hours_remaining": round(hours, 1), "is_urgent": hours < 1.5}


def build_theta_sl_series(trade: dict, snaps: list[dict]) -> list[dict]:
    """Build per-snap theta SL series for the position chart.

    Uses actual_entry_price (slippage-adjusted fill) when set, falling back to
    entry_premium. This matches the same fallback pattern used in tracker.py
    and eod.py, ensuring the SL curve is anchored to the real fill price.

    Issue-R13: the first snap is now included in the result.  Previously
    it was used only as the `prev` reference and never appended, causing
    the chart to start at the second snap.  For quick SL-hit trades with
    only 1-2 snaps, this produced an empty or single-point chart.
    """
    sl_multiplier = 1.0 - settings.AI_SL_PCT   # consistent with compute_theta_sl
    result  = []
    # F6 fix: anchor SL curve to actual fill price, not recommendation premium.
    entry   = trade.get("actual_entry_price") or trade["entry_premium"]
    theta   = trade.get("theta") or 0
    entry_t = trade.get("snap_time", "09:15")
    for snap in snaps:
        mins = _minutes_between(entry_t, snap["snap_time"])
        sl   = compute_theta_sl(entry, theta, mins)
        result.append({
            "snap_time":      snap["snap_time"],
            "entry_premium":  entry,
            "theta_daily":    theta,
            "sl_base":        round(entry * sl_multiplier, 2),
            "sl_adjusted":    sl,
            "current_ltp":    snap.get("ltp"),
            "unrealised_pnl": round((snap.get("ltp") or entry) - entry, 2),
            "pnl_pct":        snap.get("pnl_pct"),
            "status":         snap.get("theta_sl_status", "IN_TRADE"),
            "delta_pnl":      snap.get("delta_pnl"),
            "gamma_pnl":      snap.get("gamma_pnl"),
            "vega_pnl":       snap.get("vega_pnl"),
            "theta_pnl":      snap.get("theta_pnl"),
            "unexplained":    snap.get("unexplained"),
        })
    return result


# Issue-9: delegated to shared optdash.utils.snap_to_min.
_snap_to_min = snap_to_min


def _minutes_between(t1: str, t2: str) -> int:
    """Minutes between two HH:MM strings."""
    try:
        h1, m1 = map(int, t1[:5].split(":"))
        h2, m2 = map(int, t2[:5].split(":"))
        return max(0, (h2 * 60 + m2) - (h1 * 60 + m1))
    except Exception:
        return 0
