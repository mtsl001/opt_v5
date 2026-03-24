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
from optdash.analytics.microstructure import get_volume_velocity
from optdash.utils import snap_to_min


# Issue-9: delegated to shared optdash.utils.snap_to_min.
_snap_to_min = snap_to_min


def get_environment_score(
    conn:         duckdb.DuckDBPyConnection,
    trade_date:   str,
    snap_time:    str,
    underlying:   str,
    direction:    str | None = None,
    dte:          int | None = None,
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
        session = get_market_session(snap_time)
        if session == MarketSession.OPENING_TURBULENCE:
            return {
                "score": 0, "max_score": settings.GATE_MAX_SCORE,
                "verdict": GateVerdict.NO_GO.value, "conditions": {},
                "session": session.value,
                "error": "Blocked by OPENING_TURBULENCE session",
            }

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
        
        is_late_dte1 = (dealer_oc and dte is not None and dte <= 1)

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

        # C2 + C3: V_CoC velocity & Futures OBI combined momentum gate (2 pts)
        # Fix: Issue #3 - These measure the same institutional flow event,
        # so they must fire together to avoid double counting.
        _vcoc_bull = abs(settings.VCOC_BULL_THRESHOLD)
        _vcoc_bear = -_vcoc_bull  # symmetric today; override via VCOC_BEAR_THRESHOLD when added
        c2_met = vcoc > _vcoc_bull or vcoc < _vcoc_bear
        
        fut_obi_bear = settings.FUT_OBI_BEAR_THRESHOLD.get(underlying, -0.20)
        fut_obi_bull = abs(fut_obi_bear)
        c3_met = fut_bs < fut_obi_bear or fut_bs > fut_obi_bull

        c2c3_met = c2_met and c3_met
        conditions["vcoc_fut_combined"] = {
            "met": c2c3_met, 
            "value": f"VCoC:{round(vcoc,2)}|FUT:{round(fut_bs,3)}",
            "points": 2, 
            "note": "Combined V_CoC & Fut OBI momentum (2 pts)"
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

        # C5: IV cheap — threshold tightens when India VIX is elevated (1 pt)
        # When VIX_HIGH_THRESHOLD is breached, require IVP < VIX_HIGH_IVP_THRESHOLD
        # (default 35) instead of the normal < 50. This prevents "IV cheap" signal
        # from firing in high-fear regimes where IV can spike further intraday.
        india_vix  = iv_data.get("india_vix")
        vix_regime = iv_data.get("vix_regime", "UNKNOWN")
        ivp_val    = ivp if ivp is not None else 100.0

        if vix_regime == "HIGH":
            c5_threshold = settings.VIX_HIGH_IVP_THRESHOLD   # 35 when VIX elevated
            c5_note_suffix = f" | VIX={india_vix:.1f} HIGH → threshold={c5_threshold}"
        else:
            c5_threshold   = settings.VIX_NORMAL_IVP_THRESHOLD
            c5_note_suffix = f" | VIX={'N/A' if india_vix is None else f'{india_vix:.1f}'}"

        c5_met = False if is_late_dte1 else (ivp_val < c5_threshold)
        c5_pts = 0 if is_late_dte1 else 1
        conditions["ivp_cheap"] = {
            "met": c5_met, "value": round(ivp_val, 1),
            "points": c5_pts,
            "note": f"IVP = {ivp_val:.0f}th pct{c5_note_suffix}" + (" (Skipped - Dealer O'Clock)" if is_late_dte1 else "")
        }

        # C6: ATM OBI significant (1 pt)
        # Issue-R8: key is "obi_negative" for historical reasons but C6 awards
        # a point when OBI magnitude exceeds the threshold IN EITHER direction.
        # For CE trades negative OBI = retail panic puts = contrarian bullish.
        # For PE trades positive OBI = retail panic calls = contrarian bearish.
        # Both are valid entry signals on an options BUYING dashboard.
        c6_met = abs(obi) > settings.OBI_THRESHOLD
        conditions["obi_significant"] = {
            "met": c6_met, "value": round(obi, 4),
            "points": 1, "note": f"ATM OBI = {obi:+.4f}"
        }

        # C7: IV term structure not backwardation (1 pt)
        ts = iv_data.get("shape")
        if is_late_dte1:
            c7_score = 0
            c7_note = "Term structure skipped (Dealer O'Clock)"
            c7_met = False
        elif ts is None:
            c7_score = 0
            c7_note = "Term structure data unavailable"
            c7_met = False
        else:
            if ts == "CONTANGO":
                # Fix C-1: CONTANGO earns +1 gate point — reward for structurally
                # calm conditions where premium buyers have edge. Previously CONTANGO
                # scored 0 (same as FLAT), suppressing a valid positive signal.
                c7_score = 1
                c7_met   = True
                c7_note  = "Shape = CONTANGO ✓ (+1)"
            elif ts == "BACKWARDATION":
                c7_score = -1
                c7_met   = True
                c7_note  = "Shape = BACKWARDATION ⚠️ PENALTY -1"
            else:  # FLAT
                c7_score = 0
                c7_met   = False
                c7_note  = "Shape = FLAT (neutral)"

        conditions["term_structure_ok"] = {
            "met": c7_met, "value": ts or "UNKNOWN",
            "points": c7_score, "note": c7_note,
            # is_penalty only True for the BACKWARDATION case (negative points)
            "is_penalty": (c7_score < 0)
        }

        # C8: Session not midday chop (1 pt)
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
        c9_pts = 4 if is_late_dte1 else 2
        conditions["vex_aligned"] = {
            "met": c9_met, "value": round(vex_total, 2),
            "points": c9_pts, "note": f"VEX mechanical alignment ** ({c9_pts} pts)" + (" (Dealer O'Clock x2)" if is_late_dte1 else "")
        }

        # C10: Not Dealer O'Clock on DTE=1 * (1 pt bonus if safe)
        if is_late_dte1:
            c10_met = True
            c10_val = "CHARM_BONUS"
        else:
            c10_met = not dealer_oc
            c10_val = "SAFE" if c10_met else "DEALER_OCLOCK"

        conditions["not_charm_distortion"] = {
            "met": c10_met,
            "value": c10_val,
            "points": 1,
            "note": "Dealer O'Clock guard *" + (" (Inverted to bonus)" if is_late_dte1 else "")
        }

        # Bucket evaluation
        structure_pts = (
            (conditions["gex_declining"]["points"] if conditions["gex_declining"]["met"] else 0) +
            (conditions["vex_aligned"]["points"] if conditions["vex_aligned"]["met"] else 0)
        )
        momentum_pts = (
            (conditions["vcoc_fut_combined"]["points"] if conditions["vcoc_fut_combined"]["met"] else 0) +
            (conditions["obi_significant"]["points"] if conditions["obi_significant"]["met"] else 0)
        )
        context_pts = (
            (conditions["pcr_divergence"]["points"] if conditions["pcr_divergence"]["met"] else 0) +
            (conditions["ivp_cheap"]["points"] if conditions["ivp_cheap"]["met"] else 0) +
            (conditions["session_ok"]["points"] if conditions["session_ok"]["met"] else 0) +
            (conditions["not_charm_distortion"]["points"] if conditions["not_charm_distortion"]["met"] else 0)
        )

        bonus_score   = sum(c["points"] for c in conditions.values() if c["met"] and not c.get("is_penalty"))
        penalty_score = sum(c["points"] for c in conditions.values() if c["met"] and c.get("is_penalty"))
        score = max(0, min(bonus_score + penalty_score, settings.GATE_MAX_SCORE))

        _raw_max = sum(c["points"] for c in conditions.values() if c["met"] and not c.get("is_penalty"))
        # Fix C-2: use exact (c9_pts - 2) padding instead of magic +2 so a new
        # 2-pt condition doesn't silently bypass the overflow guard.
        # c9_pts is 4 on DTE=1 (Dealer O'Clock bonus) vs the normal 2-pt value,
        # giving exactly +2 headroom when needed and 0 otherwise.
        _dynamic_pad = c9_pts - 2  # 2 on DTE=1, 0 otherwise
        if _raw_max > settings.GATE_MAX_SCORE + _dynamic_pad:
            raise RuntimeError(
                f"Gate conditions sum to {_raw_max} pts but "
                f"GATE_MAX_SCORE={settings.GATE_MAX_SCORE}. "
                "Update GATE_MAX_SCORE and re-calibrate thresholds in config.py."
            )

        vol_data = get_volume_velocity(conn, trade_date, underlying)
        if vol_data:
            last_vol = vol_data[-1]
            snap_vol = last_vol["vol_total"]
            avg_snap_vol = last_vol["baseline_vol"]
        else:
            snap_vol, avg_snap_vol = 0, 0
        if avg_snap_vol is None or avg_snap_vol == 0:
            volume_ok = True
        else:
            volume_ok = snap_vol > 0.30 * avg_snap_vol

        if score >= settings.GATE_GO_THRESHOLD:
            verdict = GateVerdict.GO.value
        elif score >= settings.GATE_WAIT_THRESHOLD:
            verdict = GateVerdict.WAIT.value
        else:
            verdict = GateVerdict.NO_GO.value

        if verdict == GateVerdict.GO.value:
            if structure_pts < 1 or momentum_pts < 1 or context_pts < 1:
                verdict = GateVerdict.WAIT.value
            if not volume_ok:
                verdict = GateVerdict.WAIT.value
        elif verdict == GateVerdict.WAIT.value:
            # Fix C-3: downgrade WAIT → NO_GO on low volume.
            # Previous code set verdict = GateVerdict.WAIT.value (no-op).
            if not volume_ok:
                verdict = GateVerdict.NO_GO.value

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
    if s <= _snap_to_min(settings.SESSION_OPENING_TURBULENCE_END):
        return MarketSession.OPENING_TURBULENCE
    if s <= _snap_to_min(settings.SESSION_OPENING_END):
        return MarketSession.OPENING
    if s <= _snap_to_min(settings.SESSION_MIDDAY_START):
        return MarketSession.MIDMORNING
    if s <= _snap_to_min(settings.SESSION_MIDDAY_END):
        return MarketSession.MIDDAY_CHOP
    if s <= _snap_to_min(settings.SESSION_CLOSING_START):
        return MarketSession.AFTERNOON
    return MarketSession.CLOSING_CRUSH
