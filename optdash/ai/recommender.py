"""Full recommendation generation flow -- orchestrates all AI modules."""
import json
import duckdb
import sqlite3
from loguru import logger
from zoneinfo import ZoneInfo

from optdash.config import settings
from optdash.models import Direction, TradeStatus
from optdash.analytics.environment import get_environment_score, get_market_session
from optdash.analytics.gex import get_net_gex, get_max_pain
from optdash.analytics.iv import get_ivr_ivp
# Fix-G: import retained as fallback for when direction.py returns no vex_data
# (e.g. exception path). Primary path reads vex_data from dir_res["vex_data"].
from optdash.analytics.vex_cex import get_vex_cex_current
from optdash.analytics.screener import get_strikes
from optdash.ai.direction import get_directional_bias
from optdash.ai.confidence import compute_confidence
from optdash.ai.narrative import build_narrative
from optdash.ai.pre_flight import run_pre_flight
from optdash.ai.quality import compute_quality_score
from optdash.ai.journal import trades
from optdash.ai.learning import stats

IST = ZoneInfo("Asia/Kolkata")


def generate_recommendation(
    conn:       duckdb.DuckDBPyConnection,
    jconn:      sqlite3.Connection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> dict | None:
    """
    Called every scheduler tick for each underlying.
    Returns the generated trade card dict, or None if no recommendation issued.

    Analytics isolation policy (P2-E):
      A failed analytics call BLOCKS the recommendation for this tick.
      Rationale: every analytics result feeds either the confidence score
      or a pre-flight hard rule.  Letting a recommendation through with a
      degraded empty-dict default would produce an artificially low/wrong
      confidence or gate score -- a silent bad recommendation is worse than
      no recommendation.  Each call is wrapped individually so the failure
      reason is logged with full context and the next tick retries cleanly.
    """
    # Guard: open position exists -- never recommend while in trade.
    open_trades = trades.get_open_trades(jconn, underlying=underlying)
    if open_trades:
        return None

    # Guard: pending recommendation already issued
    pending = trades.get_pending_trades(jconn, underlying=underlying)
    if pending:
        return None

    # -- Step 1: Direction first -- bail early on NEUTRAL to skip expensive calls
    dir_res = get_directional_bias(conn, trade_date, snap_time, underlying)
    if dir_res["direction"] == Direction.NEUTRAL.value:
        return None

    direction = dir_res["direction"]
    session   = get_market_session(snap_time)

    # Fetch nearest expiry early to compute DTE for the environment gate (Fix-P1-12/Issue #5)
    try:
        nearest_expiry = _nearest_expiry(conn, trade_date, snap_time, underlying)
    except Exception:
        logger.warning(
            "P2-E: _nearest_expiry failed for {} {} {} -- skipping tick",
            underlying, trade_date, snap_time, exc_info=True,
        )
        return None

    if nearest_expiry is None:
        logger.info(
            "P1-12: no TIER1 expiry found for {} {} {} -- skipping tick "
            "(post-rollover window or missing Parquet data)",
            underlying, trade_date, snap_time,
        )
        return None

    from datetime import datetime
    try:
        t_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
        e_date = datetime.strptime(nearest_expiry, "%Y-%m-%d").date()
        dte = (e_date - t_date).days
    except Exception:
        dte = None

    # -- Step 2: Gate -- direction-aware so C9 (VEX, 2 pts) scores correctly
    gate = get_environment_score(conn, trade_date, snap_time, underlying, direction=direction, dte=dte)

    # -- Step 3: Supporting analytics (P2-E: each call isolated)
    # iv_data
    try:
        iv_data = get_ivr_ivp(conn, trade_date, snap_time, underlying)
    except Exception:
        logger.warning(
            "P2-E: get_ivr_ivp failed for {} {} {} -- skipping tick",
            underlying, trade_date, snap_time, exc_info=True,
        )
        return None

    # gex_data
    try:
        gex_data = get_net_gex(conn, trade_date, snap_time, underlying)
    except Exception:
        logger.warning(
            "P2-E: get_net_gex failed for {} {} {} -- skipping tick",
            underlying, trade_date, snap_time, exc_info=True,
        )
        return None

    # vex_data -- P4-F2: prefer dir_res["vex_data"] to avoid a second DuckDB
    # round-trip; fallback to get_vex_cex_current() only when key is absent
    # (direction.py exception path). {} is a valid result (no VEX signal).
    try:
        vex_data = (
            dir_res["vex_data"] if "vex_data" in dir_res
            else get_vex_cex_current(conn, trade_date, snap_time, underlying, gex_data=gex_data)
        )
    except Exception:
        logger.warning(
            "P2-E: vex_data resolution failed for {} {} {} -- skipping tick",
            underlying, trade_date, snap_time, exc_info=True,
        )
        return None

    dealer_oc = vex_data.get("dealer_oclock", False)

    # P1-12: nearest_expiry must be non-None to proceed.
    #
    # Root cause: on post-rollover Friday (current weekly expiry has passed,
    # next weekly not yet in Parquet), _nearest_expiry() returns None.
    # The previous code set max_pain={} and fell through to pre-flight, where
    # max_pain.get('distance_pct', 99) returned 99.0 -- silently passing
    # Rule 4 (max pain proximity) with a fake 'safely far' value.  This
    # bypassed the most important proximity guard on expiry Friday, the
    # highest-risk window in the weekly options cycle (charm distortion,
    # early close, thin book).
    #
    # Fix: if TIER1 expiry chain is absent, the data is incomplete -- skip
    # this tick and wait for the next scheduler cycle.  The skip is logged
    # as INFO so the operator knows why no recommendation was issued.
    #
    # M-1: _nearest_expiry() no longer has an internal try/except.  Any
    # DuckDB error (gateway crash, dropped view, Parquet corruption) now
    # propagates here and is caught by the P2-E guard below, which logs
    # with exc_info=True.  This makes errors distinguishable from legitimate
    # no-data conditions (nearest_expiry=None -> INFO log).
    # nearest_expiry is already fetched before the gate to pass DTE.

    try:
        max_pain = get_max_pain(
            conn, trade_date, snap_time, underlying, expiry_date=nearest_expiry
        )
    except Exception:
        logger.warning(
            "P2-E: get_max_pain failed for {} {} {} -- skipping tick",
            underlying, trade_date, snap_time, exc_info=True,
        )
        return None

    # -- Best strike selection
    try:
        strike_list = get_strikes(
            conn, trade_date, snap_time, underlying, top_n=settings.SCREENER_TOP_N,
            direction=direction
        )
    except Exception:
        logger.warning(
            "P2-E: get_strikes failed for {} {} {} -- skipping tick",
            underlying, trade_date, snap_time, exc_info=True,
        )
        return None

    candidates = [s for s in strike_list if s["option_type"] == direction]
    if not candidates:
        return None
        
    tier1_candidates = [s for s in candidates if s.get("expiry_tier") == "TIER1"]
    strike = tier1_candidates[0] if tier1_candidates else candidates[0]

    # Guard: zero or negative LTP means illiquid / expired strike -- skip
    entry_premium = strike.get("ltp") or 0
    if entry_premium <= 0:
        logger.warning(
            "Zero LTP for {} {} {} -- skipping recommendation",
            underlying, direction, strike.get("strike_price")
        )
        return None

    # -- Learning context (session + direction specific win-rate)
    learning = stats.get_session_stats(
        jconn, underlying=underlying, direction=direction, session=session
    )

    # -- Confidence score
    try:
        conf_result = compute_confidence(
            gate_score=gate["score"],
            direction_result=dir_res,
            iv_data=iv_data,
            gex_data=gex_data,
            vex_data=vex_data,
            strike=strike,
            learning_stats=learning,
            session=session,
        )
    except Exception:
        logger.warning(
            "P2-E: compute_confidence failed for {} {} {} -- skipping tick",
            underlying, trade_date, snap_time, exc_info=True,
        )
        return None
    confidence = conf_result["confidence"]

    # -- Pre-flight hard rules
    passed, failures = run_pre_flight(
        gate_score=gate["score"],
        confidence=confidence,
        strike=strike,
        gex_data={**gex_data, 
                  "max_pain_distance_pct": max_pain.get("distance_pct", 99),
                  "direction_margin": dir_res.get("margin", 0)},
        dealer_oclock=dealer_oc,
    )
    if not passed:
        logger.info(
            "Pre-flight failed for {} {}: {}", underlying, snap_time, failures
        )
        return None

    # -- SL / Target
    iv_base   = settings.AI_SL_IV_BASE.get(underlying, 25.0)
    iv_entry  = strike.get("iv") or iv_base
    iv_sl_adj = max(0.20, min(0.45, settings.AI_SL_PCT + (iv_entry - iv_base) * settings.AI_SL_IV_STEP))
    iv_tgt_adj = settings.AI_TARGET_MULT
    sl     = round(entry_premium * (1 - iv_sl_adj), 2)
    target = round(entry_premium * iv_tgt_adj, 2)

    risk   = entry_premium - sl
    reward = target - entry_premium
    if risk > 0 and (reward / risk) < (settings.AI_MIN_RR_RATIO - 0.01):
        logger.info(
            "R:R {:.2f} below minimum {:.2f} for {} {} @ {} -- skipping",
            reward / risk, settings.AI_MIN_RR_RATIO,
            underlying, direction, strike["strike_price"]
        )
        return None

    # -- Quality grade
    cold_start = conf_result.get("cold_start", False)
    raw_confidence = sum([
        conf_result["buckets"].get("signal_alignment", 0),
        conf_result["buckets"].get("gate_score", 0),
        conf_result["buckets"].get("structural", 0)
    ])
    quality = compute_quality_score(strike, gate["score"], confidence, cold_start=cold_start, raw_confidence=raw_confidence)
    if quality.get("quality_score", 0) < settings.PREFLIGHT_MIN_QUALITY_SCORE:
        logger.info("Quality grade {} below minimum for {} {} -- skipping",
                    quality.get("grade"), underlying, snap_time)
        return None

    # -- Narrative
    narrative = build_narrative(
        direction=direction,
        conviction=dir_res.get("conviction", "MODERATE"),
        pcr_modifier=dir_res.get("pcr_modifier", 1.0),
        gate_score=gate["score"],
        gate_verdict=gate["verdict"],
        direction_signals=dir_res["signals"],
        iv_data=iv_data,
        gex_data=gex_data,
        vex_data=vex_data,
        session=session,
        dealer_oclock=dealer_oc,
    )

    # -- Write to journal
    # P2-A: wrap create_trade() so a DB error (disk full, schema mismatch,
    # constraint violation) does not propagate to the scheduler tick handler
    # and leave both guards (open_trades, pending_trades) clear -- which would
    # cause generate_recommendation() to re-enter and attempt a duplicate
    # recommendation on the very next tick.  Return None cleanly; the next
    # tick retries from scratch.
    try:
        buckets = conf_result["buckets"]
        buckets["cold_start"] = conf_result.get("cold_start", False)
        
        trade_id = trades.create_trade(jconn, {
            "trade_date":        trade_date,
            "snap_time":         snap_time,
            "underlying":        underlying,
            "option_type":       direction,
            "strike_price":      strike["strike_price"],
            "expiry_date":       strike["expiry_date"],
            "entry_premium":     entry_premium,
            "sl_price":          sl,
            "target_price":      target,
            "confidence":        confidence,
            "gate_score":        gate["score"],
            "gate_verdict":      gate["verdict"],
            "s_score":           strike["s_score"],
            "quality_grade":     quality["grade"],
            "quality_score":     quality["quality_score"],
            "direction_signals": json.dumps(dir_res["signals"]),
            "narrative":         narrative,
            "status":            TradeStatus.GENERATED.value,
            "session":           session.value,
            "delta":             strike.get("delta"),
            "theta":             strike.get("theta"),
            "vega":              strike.get("vega"),
            "gamma":             strike.get("gamma"),
            "iv_at_entry":       strike.get("iv"),
            "spot_at_entry":     gex_data.get("spot"),
            "dte":               strike.get("dte"),
            "conf_buckets":      json.dumps(buckets),
        })
    except Exception:
        logger.error(
            "P2-A: create_trade() failed for {} {} {} -- recommendation not written",
            underlying, trade_date, snap_time, exc_info=True,
        )
        return None

    logger.info(
        "Recommendation: {} {} {} @ {:.1f} | conf={}% gate={} session={}",
        underlying, direction, strike["strike_price"],
        entry_premium, confidence, gate["score"], session.value,
    )
    return trades.get_trade(jconn, trade_id)


def _nearest_expiry(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> str | None:
    """Return the nearest TIER1 expiry that has not yet passed.

    P4-F1: added AND expiry_tier='TIER1' so post-rollover Friday morning
    MIN(expiry_date) cannot return the just-expired weekly contract.
    Changed expiry_date >= trade_date column-self-ref to a parameterised
    bind so the intent is explicit and safe against schema renames.

    Returns None when no TIER1 expiry exists for today -- caller (P1-12)
    must treat None as a hard skip, not a safe default.

    M-1: internal try/except removed.  The previous bare `except Exception:
    return None` made the caller's exc_info=True P2-E guard unreachable,
    causing DuckDB errors (crashed gateway, dropped Parquet view) to produce
    the same INFO log as a legitimate post-rollover no-data condition.
    Exceptions now propagate to the caller which logs them with the full
    stack trace so the two failure modes are distinguishable in production.
    """
    row = conn.execute("""
        SELECT MIN(expiry_date) FROM options_data
        WHERE trade_date=? AND snap_time=? AND underlying=?
          AND expiry_tier='TIER1'
          AND CAST(expiry_date AS DATE) >= CAST(? AS DATE)
    """, [trade_date, snap_time, underlying, trade_date]).fetchone()
    return row[0] if row else None
