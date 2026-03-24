"""GEX analytics -- Net GEX series, regime, max pain, spot summary.

Upgrades in this version
------------------------
GEX-4  Persistent module-level peak cache keyed by (trade_date, underlying).
       At 1-min cadence (375 ticks/day) the old per-tick cache forced 5
       full-day DuckDB scans per tick. The day peak is monotonically
       non-decreasing intraday -- once computed it is valid until EOD.
       reset_peak_cache() must be called by the scheduler EOD sweep.

GEX-5  get_gex_series() now uses _get_gex_peak() for its pct_of_peak
       denominator instead of an in-memory max().  Both paths use the
       same source, eliminating live-tile vs chart inconsistency.

GEX-3  get_net_gex() adds regime_near / pct_near_of_peak driven by
       TIER1+TIER2 GEX only, so TIER3 (monthly/quarterly) OI does not
       dilute the intraday regime signal.  environment.py Gate C1 uses
       regime_near as primary.

GEX-2  get_zero_gamma_level() -- new function.  Finds the interpolated
       strike price at which cumulative per-strike GEX crosses zero.
       Merged into get_net_gex() response to avoid an extra BQ round-trip.

GEX-6  get_gex_momentum() -- new function.  Compares current GEX to the
       trailing N-snap average (N = settings.GEX_MOMENTUM_SNAPS, default 5).
       At 1-min cadence N=5 = 5-minute window.  Merged into get_net_gex().
"""
import numpy as np
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.models import GEXRegime
from optdash.metrics import record_error


# ---------------------------------------------------------------------------
# GEX-4: module-level persistent peak cache
# ---------------------------------------------------------------------------
# Keyed by (trade_date, underlying)          → float  (gex_all peak)
# Keyed by (trade_date, underlying, "near")  → float  (gex_near peak)
# Invalidated by reset_peak_cache() at EOD.
_PEAK_CACHE: dict[tuple, float] = {}


def reset_peak_cache(trade_date: str) -> None:
    """Discard all peak cache entries that are NOT for trade_date.

    Call this from the scheduler EOD sweep so the first tick of the
    next trading day recomputes the peak from a clean slate rather than
    reading yesterday's (stale) value.
    """
    stale = [k for k in _PEAK_CACHE if k[0] != trade_date]
    for k in stale:
        del _PEAK_CACHE[k]
    if stale:
        logger.debug("GEX peak cache: cleared {} stale entries for date != {}",
                     len(stale), trade_date)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_net_gex(
    conn:         duckdb.DuckDBPyConnection,
    trade_date:   str,
    snap_time:    str,
    underlying:   str,
    _peak_cache:  dict | None = None,
) -> dict:
    """Latest-snap GEX snapshot: regime, ZGL, momentum.

    _peak_cache: legacy per-tick dict from scheduler.  Still accepted for
    backwards compatibility but the module-level _PEAK_CACHE now takes
    precedence -- the per-tick dict is a no-op if the module cache already
    has the value.
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
        spot     = row[4]

        # GEX-4: module-level cache takes precedence; per-tick _peak_cache
        # is populated as a fallback for callers that do not use the module cache.
        peak      = _get_gex_peak(conn, trade_date, underlying)
        peak_near = _get_gex_peak(conn, trade_date, underlying, near_only=True)

        # Backfill legacy per-tick cache for scheduler._build_gate_cache()
        if _peak_cache is not None:
            _peak_cache.setdefault((trade_date, underlying), peak)

        # Fix D-1: treat None peak as "unavailable" (DuckDB error / stale cache)
        # so gate C1 is NOT falsely fired during infrastructure failures.
        # 0.0 peak was silently dividing gex_all/0 → pct=0 → c1_met=True (wrong).
        pct      = (abs(gex_all)  / peak      * 100) if (peak  and peak  > 0) else None
        pct_near = (abs(gex_near) / peak_near * 100) if (peak_near and peak_near > 0) else None

        # GEX-3: regime_near driven by TIER1+TIER2 only (intraday signal);
        # regime (all-inclusive) kept for API context and historical compat.
        # Fix D-1: when peak is unavailable (None), default pct to 0.0 for
        # regime classification — regime becomes POSITIVE_CHOP or NEGATIVE_TREND
        # based solely on GEX sign, without the declining-percentage gate.
        regime      = _classify_regime(gex_all,  pct if pct is not None else 0.0)
        regime_near = _classify_regime(gex_near, pct_near if pct_near is not None else 0.0)

        # GEX-2: Zero Gamma Level
        zgl_data = _compute_zero_gamma_level(conn, trade_date, snap_time,
                                             underlying, spot)

        # GEX-6: GEX Momentum (5-min trailing avg at 1-min cadence)
        momentum_data = _compute_gex_momentum(conn, trade_date, snap_time,
                                              underlying)

        return {
            "snap_time":        row[0],
            "gex_all_B":        round(gex_all,  3),
            "gex_near_B":       round(gex_near, 3),
            "gex_far_B":        round(gex_far,  3),
            "pct_of_peak":      round(pct,      1),
            "pct_near_of_peak": round(pct_near, 1),
            "regime":           regime.value,
            "regime_near":      regime_near.value,   # GEX-3: primary gate signal
            "spot":             spot,
            **zgl_data,                              # GEX-2: zgl, spot_vs_zgl, above_zgl
            **momentum_data,                         # GEX-6: gex_momentum_B, momentum_direction
        }
    except Exception as e:
        record_error("get_net_gex")
        logger.warning("get_net_gex error: {}", e)
        return {}


def get_gex_series(conn: duckdb.DuckDBPyConnection, trade_date: str,
                   underlying: str) -> list[dict]:
    """Full-day GEX series with pct_of_peak for charting.

    GEX-5: pct_of_peak now uses _get_gex_peak() (same DuckDB source as
    get_net_gex) instead of an in-memory series max().  Both live-tile
    and chart pct_of_peak now share a single denominator.
    """
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

        # GEX-5: use the same peak source as get_net_gex (DuckDB query, cached).
        peak      = _get_gex_peak(conn, trade_date, underlying)
        peak_near = _get_gex_peak(conn, trade_date, underlying, near_only=True)

        result = []
        for r in rows:
            gex         = r[1] or 0
            gex_n       = r[2] or 0
            pct_of_peak = round(abs(gex)   / peak      * 100, 1) if peak and peak > 0 else 0.0
            pct_near    = round(abs(gex_n) / peak_near * 100, 1) if peak_near and peak_near > 0 else 0.0
            regime      = _classify_regime(gex,   pct_of_peak)
            regime_near = _classify_regime(gex_n, pct_near)
            result.append({
                "snap_time":        r[0],
                "gex_all_B":        round(r[1] or 0, 3),
                "gex_near_B":       round(r[2] or 0, 3),
                "gex_far_B":        round(r[3] or 0, 3),
                "pct_of_peak":      pct_of_peak,
                "pct_near_of_peak": pct_near,
                "regime":           regime.value,
                "regime_near":      regime_near.value,
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


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

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


def _get_gex_peak(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    underlying: str,
    near_only:  bool = False,
) -> float | None:
    """Day peak absolute GEX (denominator for pct_of_peak).

    GEX-4: checks the module-level _PEAK_CACHE before querying DuckDB.
    The peak is monotonically non-decreasing intraday so the cached
    value is always valid until reset_peak_cache() is called at EOD.

    near_only=True: returns peak of TIER1+TIER2 GEX only (for regime_near).
    Cache key is (trade_date, underlying) for all-inclusive and
               (trade_date, underlying, "near") for near-only.
    """
    cache_key = (trade_date, underlying, "near") if near_only else (trade_date, underlying)
    cached = _PEAK_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        if near_only:
            row = conn.execute("""
                SELECT MAX(ABS(gex_sum)) FROM (
                    SELECT snap_time,
                           SUM(CASE WHEN expiry_tier IN ('TIER1','TIER2') THEN gex ELSE 0 END)
                           / ? AS gex_sum
                    FROM options_data
                    WHERE trade_date=? AND underlying=?
                    GROUP BY snap_time
                )
            """, [settings.GEX_SCALING, trade_date, underlying]).fetchone()
        else:
            row = conn.execute("""
                SELECT MAX(ABS(gex_sum)) FROM (
                    SELECT snap_time, SUM(gex) / ? AS gex_sum
                    FROM options_data
                    WHERE trade_date=? AND underlying=?
                    GROUP BY snap_time
                )
            """, [settings.GEX_SCALING, trade_date, underlying]).fetchone()

        result = float(row[0]) if row and row[0] else 0.0
        _PEAK_CACHE[cache_key] = result
        return result
    except Exception as e:
        # Fix D-1: log the failure so DuckDB errors are visible in monitoring.
        # Return None (not 0.0) so callers can detect unavailability and avoid
        # falsely firing the GEX-declining gate condition on infrastructure errors.
        logger.warning("_get_gex_peak failed for {}/{}: {}", trade_date, underlying, e)
        return None


def _compute_zero_gamma_level(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
    spot:       float | None,
) -> dict:
    """GEX-2: interpolated strike where cumulative GEX crosses zero.

    Uses TIER1+TIER2 only (intraday-relevant expiries).
    Returns zgl=None when no zero crossing exists in the strike ladder
    (e.g. all-positive or all-negative GEX profile — less common).

    spot_vs_zgl: % distance of spot above (+) or below (−) ZGL.
    above_zgl:   True when spot > ZGL (dealers long gamma → stabilising).
    """
    try:
        rows = conn.execute("""
            SELECT strike_price, SUM(gex) AS strike_gex
            FROM options_data
            WHERE trade_date = ? AND snap_time = ? AND underlying = ?
              AND expiry_tier IN ('TIER1', 'TIER2')
              AND option_type IN ('CE', 'PE')
            GROUP BY strike_price
            ORDER BY strike_price
        """, [trade_date, snap_time, underlying]).fetchall()

        if not rows:
            return {"zgl": None, "spot_vs_zgl": None, "above_zgl": None}

        strikes = np.array([r[0] for r in rows], dtype=float)
        gex_arr = np.array([r[1] or 0 for r in rows], dtype=float)
        cum_gex = np.cumsum(gex_arr)

        sign_changes = np.where(np.diff(np.sign(cum_gex)))[0]
        if len(sign_changes) == 0:
            return {"zgl": None, "spot_vs_zgl": None, "above_zgl": None}

        # Use the first zero crossing (lowest strike — primary ZGL)
        idx = sign_changes[0]
        # Fix D-2: Replace np.interp which requires monotonically increasing xp.
        # At the zero crossing, cum_gex changes sign so [cum_gex[idx], cum_gex[idx+1]]
        # is NOT monotonically increasing — np.interp produced wrong ZGL values.
        # Correct: standard two-point linear interpolation formula.
        #   zgl = s[idx] + (s[idx+1] - s[idx]) × (0 - cum_gex[idx])
        #                                        / (cum_gex[idx+1] - cum_gex[idx])
        denom = cum_gex[idx + 1] - cum_gex[idx]
        if abs(denom) < 1e-10:
            zgl = float((strikes[idx] + strikes[idx + 1]) / 2)
        else:
            zgl = float(strikes[idx] + (strikes[idx + 1] - strikes[idx]) *
                        (-cum_gex[idx]) / denom)

        dist_pct  = round((spot - zgl) / zgl * 100, 2) if (spot and zgl) else None
        above_zgl = bool(spot > zgl) if (spot is not None and zgl) else None

        return {
            "zgl":         round(zgl, 1),
            "spot_vs_zgl": dist_pct,   # + = above ZGL (stable), − = below (volatile)
            "above_zgl":   above_zgl,
        }
    except Exception as e:
        logger.debug("_compute_zero_gamma_level error: {}", e)
        return {"zgl": None, "spot_vs_zgl": None, "above_zgl": None}


def _compute_gex_momentum(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> dict:
    """GEX-6: GEX momentum vs trailing N-snap average.

    At 1-min cadence, N = GEX_MOMENTUM_SNAPS (default 5) = 5-minute window.
    Compares current snap GEX to the average of the prior N snaps.

    gex_momentum_B:    current GEX minus trailing avg (₹ Billions).
                       Positive = GEX building. Negative = GEX fading.
    momentum_direction: "BUILDING" / "FADING" / "FLAT"
                        FLAT when |change| < 5% of current |GEX|.
    """
    try:
        n = settings.GEX_MOMENTUM_SNAPS
        # Fetch last (n+1) snaps ordered desc: index 0 = current, rest = prior
        rows = conn.execute("""
            SELECT snap_time, SUM(gex) / ? AS gex_B
            FROM options_data
            WHERE trade_date = ? AND underlying = ? AND snap_time <= ?
            GROUP BY snap_time
            ORDER BY snap_time DESC
            LIMIT ?
        """, [settings.GEX_SCALING, trade_date, underlying, snap_time, n + 1]).fetchall()

        if not rows:
            return {"gex_momentum_B": None, "momentum_direction": None}

        current_gex = rows[0][1] or 0.0
        prior_snaps = [r[1] or 0.0 for r in rows[1:]]

        if not prior_snaps:
            return {"gex_momentum_B": None, "momentum_direction": None}

        trailing_avg = sum(prior_snaps) / len(prior_snaps)
        momentum     = current_gex - trailing_avg

        # FLAT if change is < 5% of the magnitude of current GEX
        flat_threshold = abs(current_gex) * 0.05
        if abs(momentum) < flat_threshold or abs(momentum) < 0.001:
            direction = "FLAT"
        elif momentum > 0:
            direction = "BUILDING"
        else:
            direction = "FADING"

        return {
            "gex_momentum_B":    round(momentum, 3),
            "momentum_direction": direction,
        }
    except Exception as e:
        logger.debug("_compute_gex_momentum error: {}", e)
        return {"gex_momentum_B": None, "momentum_direction": None}
