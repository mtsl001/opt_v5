"""Confidence scoring — four independent buckets, capped at 100."""
from optdash.config import settings
from optdash.models import MarketSession, GEXRegime


def compute_confidence(
    gate_score:       int,
    direction_result: dict,
    iv_data:          dict,
    gex_data:         dict,
    vex_data:         dict,
    strike:           dict,
    learning_stats:   dict,
    session:          MarketSession,
) -> dict:
    """
    Bucket 1: Signal Alignment     (max 40 pts)
    Bucket 2: Gate Score           (max 25 pts)
    Bucket 3: Structural Quality   (max 25 pts)
    Bucket 4: Historical Perf      (max 10 pts)
    """
    margin       = direction_result.get("margin", 0)
    unique_source_count = direction_result.get("unique_source_count", len(direction_result.get("signals", [])))
    direction    = direction_result.get("direction", "")

    # Bucket 1: signal strength
    # Fix CONF-3: changed from margin*8 + signal_count*2 to margin*7 + signal_count*3.
    # Old formula: at margin>=5, margin*8>=40=cap, so signal_count*2 could never push
    # the total beyond the cap -- the signal-diversity term was effectively dead at all
    # high-conviction setups (exactly when it should reward wide signal agreement most).
    # New formula: lowering the margin coefficient to 7 creates headroom so
    # unique_source_count*3 contributes at margin<=5 (e.g. margin=3, count=5: 21+15=36 vs 24).
    # Max score still hits cap at margin=6+ so genuinely dominant setups are unaffected.
    b1 = min(settings.CONFIDENCE_B1_MAX, margin * 7 + unique_source_count * 3)

    # Bucket 2: gate adequacy — P4-F5: corrected multiplier from 30 → 25.
    # Fix K-1: removed dead `or 10` fallback — GATE_MAX_SCORE is always set
    # and validated in config.py; a zero GATE_MAX_SCORE would be a config bug
    # that should raise, not silently substitute 10.
    gate_max = settings.GATE_MAX_SCORE
    b2 = min(settings.CONFIDENCE_B2_MAX, int((gate_score / gate_max) * settings.CONFIDENCE_B2_MAX))

    # Bucket 3: structural quality
    ivp_val = iv_data.get("ivp")
    b3 = 0
    if (ivp_val if ivp_val is not None else 100) < 50: b3 += 6
    if iv_data.get("shape") == "CONTANGO":              b3 += 4
    if (strike.get("s_score") or 0) > 80:              b3 += 7
    gex_regime = gex_data.get("regime", "")
    if gex_regime in (GEXRegime.NEGATIVE_TREND.value, GEXRegime.POSITIVE_DECLINING.value):
        b3 += 5
    vex_sig = vex_data.get("vex_signal", "")
    if vex_sig == "VEX_BULLISH" and direction == "CE":  b3 += 3
    if vex_sig == "VEX_BEARISH" and direction == "PE":  b3 += 3

    # VRP bonus (+3): when VRP < 0, options are genuinely underpriced vs realised vol.
    # This is the statistically strongest entry context for option buyers.
    # Source: iv_data["vrp_regime"] from get_ivr_ivp().
    vrp_regime = iv_data.get("vrp_regime", "UNKNOWN")
    if vrp_regime == "UNDERPRICED":
        b3 += 3

    b3 = min(settings.CONFIDENCE_B3_MAX, b3)

    # Bucket 4: historical performance — P4-F14b: cold-start guard.
    # Fix LEARN-2 compatibility: win_rate may now be None when total_trades=0.
    # The existing cold-start guard (is_fallback or total_trades < 5) already
    # zeroes B4 in that case, so None can never reach the arithmetic below.
    # The explicit None guard is an extra safety net for future callers.
    is_fallback  = learning_stats.get("is_fallback", False)
    total_trades = learning_stats.get("total_trades", 0)
    cold_start   = False
    if is_fallback or total_trades < settings.CONFIDENCE_B4_MIN_TRADES:
        b4 = 0
        cold_start = True
    else:
        raw_wr = learning_stats.get("win_rate")
        win_rate = (raw_wr / 100) if raw_wr is not None else 0.5
        b4 = min(settings.CONFIDENCE_B4_MAX, int(win_rate * settings.CONFIDENCE_B4_SCALE))

    if cold_start:
        B_ACTIVE_MAX = settings.CONFIDENCE_B1_MAX + settings.CONFIDENCE_B2_MAX + settings.CONFIDENCE_B3_MAX
        raw = int((b1 + b2 + b3) * (100.0 / B_ACTIVE_MAX))
    else:
        raw = b1 + b2 + b3 + b4

    raw_pre_session = raw

    # Session adjustments
    session_adjusted_reason = None
    if session == MarketSession.MIDDAY_CHOP:
        # Defaults to 1.0 (no confirm) if missing on VEX/exception paths
        pcr_mod = direction_result.get("pcr_modifier", 1.0)
        # Fix K-2: direct attribute access instead of getattr() with a default.
        # If SESSION_MIDDAY_SMART_PENALTY is ever removed from config, this
        # raises AttributeError immediately rather than silently falling back.
        smart_penalty = settings.SESSION_MIDDAY_SMART_PENALTY
        if smart_penalty and b1 >= 35 and pcr_mod > 1.0:
            raw -= 5
            session_adjusted_reason = "MIDDAY_PENALTY_SMART"
        else:
            raw -= settings.SESSION_MIDDAY_CONFIDENCE_PENALTY
            session_adjusted_reason = "MIDDAY_PENALTY"
    if session == MarketSession.CLOSING_CRUSH:
        if raw > settings.SESSION_CLOSING_CONFIDENCE_CAP:
            session_adjusted_reason = "CLOSING_CAP"
        raw = min(raw, settings.SESSION_CLOSING_CONFIDENCE_CAP)

    confidence = max(0, min(100, raw))

    is_adjusted = raw != raw_pre_session
    return {
        "confidence": confidence,
        "buckets": {
            "signal_alignment": b1,
            "gate_score":       b2,
            "structural":       b3,
            "historical":       b4,
        },
        "session_adjusted": is_adjusted,
        "session_adjusted_reason": session_adjusted_reason if is_adjusted else None,
        "cold_start":       cold_start,
    }
