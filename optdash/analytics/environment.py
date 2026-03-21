"""11-point Environment Gate -- GO / WAIT / NO_GO verdict."""
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.models import GateVerdict, MarketSession
from optdash.analytics.gex import get_net_gex
from optdash.analytics.coc import get_coc_latest, get_atm_obi, get_futures_obi
from optdash.analytics.iv  import get_ivr_ivp
from optdash.analytics.pcr import get_pcr
from optdash.analytics.vex_cex import get_vex_cex_current
from optdash.utils import snap_to_min


# Issue-9: delegated to shared optdash.utils.snap_to_min.
_snap_to_min = snap_to_min


def get_environment_score(
    conn:         duckdb.DuckDBPyConnection,
    trade_date:   str,
    snap_time:    str,
    underlying:   str,
    direction:    str | None = None,
    _peak_cache:  dict | None = None,
) -> dict:
    """
    11-point environment gate.
    Conditions 1-8: standard (1 pt each = 8 pts max).
    Condition 9:    VEX alignment ** (2 pts) -- requires direction.
    Condition 10:   Dealer O'Clock guard * (1 pt).
    Max = 11 pts.

    direction must be 'CE' or 'PE'. If None, C9 does not fire (0 pts).
    All production callers (recommender, tracker) always pass direction.

    _peak_cache: optional dict shared across multiple calls within the same
    scheduler tick. Format: {(trade_date, underlying): float}. When provided,
    get_net_gex() will populate and reuse the day-peak GEX value, avoiding
    repeated full-day DuckDB scans. Pass None (default) for single API calls.
    """
    try:
        gex_data = get_net_gex(conn, trade_date, snap_time, underlying,
                               _peak_cache=_peak_cache)
        coc_data = get_coc_latest(conn, trade_date, snap_time, underlying)
        iv_data  = get_ivr_ivp(conn, trade_date, snap_time, underlying)
        pcr_data = get_pcr(conn, trade_date, snap_time, underlying)
        vex_data = get_vex_cex_current(conn, trade_date, snap_time, underlying)
        atm_obi  = get_atm_obi(conn, trade_date, snap_time, underlying)
        fut_obi  = get_futures_obi(conn, trade_date, snap_time, underlying)

        gex_pct   = gex_data.get("pct_of_peak", 100.0)
        vcoc      = coc_data.get("v_coc_15m", 0.0)
        fut_bs    = fut_obi
        pcr_div   = pcr_data.get("pcr_divergence", 0.0)
        ivp       = iv_data.get("ivp")           # may be None if history unavailable
        obi       = atm_obi
        vex_total = vex_data.get("vex_total_M", 0.0)
        dealer_oc = vex_data.get("dealer_oclock", False)

        conditions: dict[str, dict] = {}

        # C1: GEX declining — use regime_near (TIER1+TIER2 only) as primary signal.
        # GEX-3: TIER3 (monthly/quarterly) OI dilutes gex_all near expiry.
        # regime_near reflects the intraday dealer positioning correctly.
        # Falls back to gex_pct (all-inclusive) when regime_near is absent.
        gex_pct_near = gex_data.get("pct_near_of_peak", gex_pct)
        c1_met = gex_pct_near <= settings.GEX_DECLINE_THRESHOLD * 100
        conditions["gex_declining"] = {
            "met": c1_met, "value": round(gex_pct_near, 1),
            "points": 1, "note": f"{gex_pct_near:.0f}% of peak (near-expiry GEX)"
        }

        # C2: V_CoC velocity (1 pt)
        # Split into explicit bull/bear checks rather than abs() == abs()
        # so future asymmetric threshold tuning (VCOC_BEAR_THRESHOLD != -VCOC_BULL)
        # is handled correctly without changing this logic.
        _vcoc_bull = abs(settings.VCOC_BULL_THRESHOLD)
        _vcoc_bear = -_vcoc_bull  # symmetric today; override via VCOC_BEAR_THRESHOLD when added
        c2_met = vcoc > _vcoc_bull or vcoc < _vcoc_bear
        conditions["vcoc_signal"] = {
            "met": c2_met, "value": round(vcoc, 2),
            "points": 1, "note": f"V_CoC 15m = {vcoc:+.2f}"
        }

        # C3: Futures OBI -- strong directional conviction (1 pt)
        # OptDash is an options BUYING dashboard (CE and PE buyers).
        # C3 fires on EITHER strong bearish OR strong bullish institutional
        # futures flow -- symmetric thresholds ensure CE trades can also earn
        # this point when buyers dominate the futures market.
        fut_obi_bear = settings.FUT_OBI_BEAR_THRESHOLD.get(underlying, -0.20)
        fut_obi_bull = abs(fut_obi_bear)
        c3_met = fut_bs < fut_obi_bear or fut_bs > fut_obi_bull
        conditions["fut_bs_ratio"] = {
            "met": c3_met, "value": round(fut_bs, 4),
            "points": 1,
            "note": f"Fut OBI = {fut_bs:.3f} (bear<{fut_obi_bear:.2f} | bull>{fut_obi_bull:.2f})"
        }

        pcr_tier  = pcr_data.get("tier_used", "TIER1")
        # C4: PCR divergence (1 pt)
        # Issue-6: use config thresholds (same as direction.py Signal 5) instead
        # of the hardcoded 0.15.  This ensures tuning PCR_DIV_*_THRESHOLD in
        # .env affects both the gate verdict and the directional bias consistently.
        c4_met = pcr_div > settings.PCR_DIV_BULL_THRESHOLD or pcr_div < settings.PCR_DIV_BEAR_THRESHOLD
        conditions["pcr_divergence"] = {
            "met": c4_met, "value": round(pcr_div, 4),
            "points": 1, "note": f"{pcr_tier} Divergence = {pcr_div:+.4f}"
        }

        # C5: IV cheap (IVP < 50) (1 pt)
        # Guard: use explicit None check so IVP=0 (historically cheapest IV)
        # is treated as valid (met=True) rather than coerced to 100 via `or`.
        ivp_val = ivp if ivp is not None else 100.0
        c5_met  = ivp_val < 50
        conditions["ivp_cheap"] = {
            "met": c5_met, "value": round(ivp_val, 1),
            "points": 1, "note": f"IVP = {ivp_val:.0f}th pct"
        }

        # C6: ATM OBI significant (1 pt)
        # Issue-R8: key is "obi_negative" for historical reasons but C6 awards
        # a point when OBI magnitude exceeds the threshold IN EITHER direction.
        # For CE trades negative OBI = retail panic puts = contrarian bullish.
        # For PE trades positive OBI = retail panic calls = contrarian bearish.
        # Both are valid entry signals on an options BUYING dashboard.
        c6_met = abs(obi) > settings.OBI_THRESHOLD
        conditions["obi_negative"] = {
            "met": c6_met, "value": round(obi, 4),
            "points": 1, "note": f"ATM OBI = {obi:+.4f}"
        }

        # C7: IV term structure not backwardation (1 pt)
        ts     = iv_data.get("shape", "FLAT")
        c7_met = ts != "BACKWARDATION"
        conditions["term_structure_ok"] = {
            "met": c7_met, "value": ts,
            "points": 1, "note": f"Shape = {ts}"
        }

        # C8: Session not midday chop (1 pt)
        session = get_market_session(snap_time)
        c8_met  = session != MarketSession.MIDDAY_CHOP
        conditions["session_ok"] = {
            "met": c8_met, "value": session.value,
            "points": 1, "note": f"Session = {session.value}"
        }

        # C9: VEX aligned with direction ** (2 pts)
        # Only fires when the caller explicitly provides direction ('CE' or 'PE').
        # direction=None: C9 does NOT fire (c9_met stays False). This is
        # intentional -- the VEX bonus must be earned against a known trade
        # type. Removing the old 'direction is None' fallback prevents
        # inflated gate scores from API callers that omit direction.
        #
        # Fix ENV-1: apply the same per-underlying VEX threshold used by
        # _classify_vex() in vex_cex.py and direction.py Signal 3.
        # The previous bare `vex_total > 0` check let any non-zero VEX earn
        # 2 gate points regardless of whether it crossed the meaningful
        # threshold, creating an incoherent gate/direction scoring pair.
        c9_met  = False
        vex_thr = settings.VEX_THRESHOLDS.get(underlying, settings.VEX_BULL_THRESHOLD)
        if direction == "CE" and vex_total > vex_thr:
            c9_met = True
        elif direction == "PE" and vex_total < -vex_thr:
            c9_met = True
        conditions["vex_aligned"] = {
            "met": c9_met, "value": round(vex_total, 2),
            "points": 2, "note": "VEX mechanical alignment ** (2 pts)"
        }

        # C10: Not Dealer O'Clock on DTE=1 * (1 pt bonus if safe)
        c10_met = not dealer_oc
        conditions["not_charm_distortion"] = {
            "met": c10_met,
            "value": "SAFE" if c10_met else "DEALER_OCLOCK",
            "points": 1,
            "note": "Dealer O'Clock guard *"
        }

        _raw_max = sum(c["points"] for c in conditions.values())
        # RuntimeError replaces assert — fires even under python -O.
        # Caught by the outer except → logged as FATAL → returns NO_GO,
        # blocking all recommendations until config.py is corrected.
        if _raw_max > settings.GATE_MAX_SCORE:
            raise RuntimeError(
                f"Gate conditions sum to {_raw_max} pts but "
                f"GATE_MAX_SCORE={settings.GATE_MAX_SCORE}. "
                "Update GATE_MAX_SCORE and re-calibrate thresholds in config.py."
            )

        score   = min(
            sum(c["points"] for c in conditions.values() if c["met"]),
            settings.GATE_MAX_SCORE,
        )
        verdict = (
            GateVerdict.GO.value   if score >= settings.GATE_GO_THRESHOLD   else
            GateVerdict.WAIT.value if score >= settings.GATE_WAIT_THRESHOLD  else
            GateVerdict.NO_GO.value
        )

        return {
            "score":      score,
            "max_score":  settings.GATE_MAX_SCORE,
            "verdict":    verdict,
            "conditions": conditions,
            "session":    session.value,
        }

    except Exception as e:
        logger.error(
            "get_environment_score FATAL: {} {} {} | {}",
            underlying, trade_date, snap_time, e,
            exc_info=True,
        )
        return {
            "score":      0,
            "max_score":  settings.GATE_MAX_SCORE,
            "verdict":    GateVerdict.NO_GO.value,
            "conditions": {},
            "session":    "",
            "error":      str(e),
        }


def get_market_session(snap_time: str) -> MarketSession:
    """Return the market session bucket for a given snap_time (HH:MM).

    Uses integer-minute arithmetic via _snap_to_min() to avoid lexicographic
    string comparison pitfalls (e.g. '9:15' < '09:30' is False as strings).
    """
    s = _snap_to_min(snap_time)
    if s <= _snap_to_min(settings.SESSION_OPENING_END):
        return MarketSession.OPENING
    if s <= _snap_to_min(settings.SESSION_MIDDAY_START):
        return MarketSession.MIDMORNING
    if s <= _snap_to_min(settings.SESSION_MIDDAY_END):
        return MarketSession.MIDDAY_CHOP
    if s <= _snap_to_min(settings.SESSION_CLOSING_START):
        return MarketSession.AFTERNOON
    return MarketSession.CLOSING_CRUSH
