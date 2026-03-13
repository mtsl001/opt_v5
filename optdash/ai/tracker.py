"""Live position tracking -- called every scheduler tick."""
import duckdb
import sqlite3
from datetime import date
from loguru import logger
from optdash.config import settings
from optdash.models import IVCrushSeverity, ExitReason, TradeStatus
from optdash.analytics.environment import get_environment_score
from optdash.analytics.pnl import compute_theta_sl, compute_pnl_attribution
from optdash.ai.journal import trades, snaps
# EOD-1: re-exported from the canonical location for backwards compatibility.
# All new callers should import from optdash.analytics.query directly.
from optdash.analytics.query import fetch_strike_current as _fetch_strike_current


def track_open_positions(
    conn:       duckdb.DuckDBPyConnection,
    jconn:      sqlite3.Connection,
    trade_date: str,
    snap_time:  str,
    gate_cache: dict | None = None,
) -> None:
    """Check every ACCEPTED trade and update position snaps.

    gate_cache: optional pre-computed gate scores keyed by underlying.
    When provided by the scheduler tick, get_environment_score() is skipped
    for cached underlyings, eliminating N+1 DuckDB round-trips (Fix-L / F-04).
    When absent (e.g. direct API calls), gate is computed fresh per trade.
    """
    open_trades = trades.get_open_trades(jconn)
    for trade in open_trades:
        underlying = trade["underlying"]
        strike     = trade["strike_price"]
        expiry     = trade["expiry_date"]
        opt_type   = trade["option_type"]

        current = _fetch_strike_current(
            conn, trade_date, snap_time, underlying, strike, expiry, opt_type
        )
        if not current:
            logger.warning("No data for {} {} at {}", underlying, strike, snap_time)
            continue

        ltp   = current["ltp"]
        iv    = current["iv"]
        delta = current["delta"]
        theta = current["theta"]
        vega  = current["vega"]

        # P0-1 fix: explicit None check so actual_entry_price=0.0 (data
        # corruption / test trade) is not silently promoted to entry_premium
        # via the `or` falsy coercion.  Mirrors the identical fix already
        # applied in eod.py (Fix EOD-2) and api/routers/ai.py (Fix API-2).
        # All PnL, SL, trailing-stop, and attribution calculations downstream
        # use this value -- a wrong entry here corrupts every metric for the
        # lifetime of the position.
        _actual = trade["actual_entry_price"]
        entry   = _actual if _actual is not None else trade["entry_premium"]
        lot   = settings.LOT_SIZES.get(underlying, 1)
        # Fix-K (F-01): pnl_abs is monetary (point_diff * lot); pnl_pct stays per-unit %
        pnl_abs = round((ltp - entry) * lot, 2)
        pnl_pct = round((ltp - entry) / entry * 100, 2) if entry else 0.0

        # Theta SL
        t_elapsed   = _minutes_since_entry(trade["snap_time"], snap_time)
        sl_adjusted = compute_theta_sl(entry, trade["theta"] or 0, t_elapsed)

        theta_sl_status = (
            "STOP_HIT"                 if ltp < sl_adjusted else
            "GUARANTEED_PROFIT_ZONE"   if pnl_pct > 50      else
            "PROFIT_ZONE_PARTIAL_EXIT" if pnl_pct > 30      else
            "IN_TRADE"
        )

        # Trailing stop -- P4-F8: guard peak_ltp against None.
        # snaps.get_peak_ltp() returns None on the first scheduler tick after
        # ACCEPT (no position_snaps rows exist yet). The previous code called
        # `peak_ltp * 0.90` unconditionally, raising TypeError and killing the
        # entire tick silently for every open trade.
        #
        # trailing_active is True only when the dynamic trail (peak * 0.90)
        # actually exceeds the theta SL, i.e. when the trailing rail -- not the
        # base theta SL -- is the governing stop level. This lets the exit block
        # emit the correct ExitReason (TRAILING_STOP_HIT vs SL_HIT).
        trailing_active = False
        if pnl_pct >= settings.TRAILING_STOP_ACTIVATION * 100:
            peak_ltp = snaps.get_peak_ltp(jconn, trade["id"])
            if peak_ltp is not None:
                dynamic_trail   = peak_ltp * (1.0 - settings.TRAILING_STOP_TRAIL_PCT)
                trail_sl        = max(dynamic_trail, sl_adjusted)
                trailing_active = dynamic_trail > sl_adjusted
            else:
                trail_sl = sl_adjusted
        else:
            trail_sl = sl_adjusted

        # IV crush -- resolve per-underlying Vega threshold from config dict.
        # Vega is stored in option price points per 1% IV change; thresholds
        # are calibrated per index (see IV_CRUSH_HIGH_VEGA in config.py).
        iv_change    = iv - (trade["iv_at_entry"] or iv)
        iv_crush_thr = settings.IV_CRUSH_HIGH_VEGA.get(underlying, 15.0)
        iv_crush = (
            IVCrushSeverity.HIGH.value
            if iv_change < -3.0 and abs(vega or 0) > iv_crush_thr
            else IVCrushSeverity.LOW.value  if iv_change < -1.0
            else IVCrushSeverity.NONE.value
        )

        # Gate check
        # Fix-L (F-04): use pre-computed gate from scheduler tick cache if
        # available -- avoids re-running 7 DuckDB aggregations per open position.
        # Falls back to fresh computation if called without cache (e.g. direct API).
        if gate_cache and underlying in gate_cache:
            gate = gate_cache[underlying]
            # M-3: gate_cache entries built by _build_gate_cache() fall back to
            # {"score": 0, "verdict": "NO_GO", "error": str(e)} on exception.
            # Log a warning here so a caching failure is visible per-trade in
            # the scheduler log, rather than silently appearing as a real NO_GO
            # and potentially triggering spurious GATE_NO_GO exits.
            if gate.get("error"):
                logger.warning(
                    "track_open_positions: gate for {} is an error fallback "
                    "(cache build failed: {}). NO_GO verdict may be misleading "
                    "for trade id={}; position will not be force-exited on this "
                    "verdict alone.",
                    underlying, gate["error"], trade["id"],
                )
                # Issue-11: override the artificial NO_GO verdict so the
                # consecutive-NO_GO counter does NOT advance on infrastructure
                # failures.  Without this, a DuckDB crash lasting 2+ ticks
                # (10 min at default interval) would trigger GATE_NO_GO exits
                # on all open positions — not because the market environment
                # is hostile, but because the gate computation failed.
                # The snap record stores "GATE_ERROR" for auditability.
                gate = {**gate, "verdict": "GATE_ERROR"}
        else:
            gate = get_environment_score(
                conn, trade_date, snap_time, underlying, direction=opt_type
            )
        gate_no_go = gate["verdict"] == "NO_GO"

        # PnL attribution
        pnl_attr = compute_pnl_attribution(
            entry={
                "spot":      trade["spot_at_entry"],
                "iv":        trade["iv_at_entry"],
                "delta":     trade["delta"],
                "gamma":     trade["gamma"],
                "vega":      trade["vega"],
                "theta":     trade["theta"],
                "premium":   entry,               # use actual entry price
                "snap_time": trade["snap_time"],
            },
            current={
                "spot":      current.get("spot"),
                "iv":        iv,
                "ltp":       ltp,
                "snap_time": snap_time,
            },
        )

        # Write snap -- P6-E: commit=False batches all snaps for this tick.
        # Auto-close path (trades.close_trade, commit=True default) will flush
        # the buffered snap + the CLOSED update together atomically if the
        # position exits. For positions that stay open, the single jconn.commit()
        # after the loop issues one WAL flush for all remaining snaps -- replacing
        # the previous N individual flushes (one per open trade per tick).
        snaps.insert_snap(jconn, {
            "trade_id":        trade["id"],
            "snap_time":       snap_time,
            "ltp":             ltp,
            "pnl_abs":         pnl_abs,
            "pnl_pct":         pnl_pct,
            "sl_adjusted":     trail_sl,
            "theta_sl_status": theta_sl_status,
            "iv":              iv,
            "iv_crush":        iv_crush,
            "gate_score":      gate["score"],
            "gate_verdict":    gate["verdict"],
            "delta_pnl":       pnl_attr["delta_pnl"],
            "gamma_pnl":       pnl_attr["gamma_pnl"],
            "vega_pnl":        pnl_attr["vega_pnl"],
            "theta_pnl":       pnl_attr["theta_pnl"],
            "unexplained":     pnl_attr["unexplained"],
        }, commit=False)

        # Auto-close logic
        exit_reason = None
        if ltp <= trail_sl:
            # P4-F8: emit the correct exit reason based on which stop governed.
            # THETA_SL_HIT  -- option decayed past the theta-adjusted SL.
            # TRAILING_STOP_HIT -- trailing rail (peak*0.90) was the active stop.
            # SL_HIT        -- plain fixed SL hit before any trailing activation.
            exit_reason = (
                ExitReason.THETA_SL_HIT.value      if theta_sl_status == "STOP_HIT"
                else ExitReason.TRAILING_STOP_HIT.value if trailing_active
                else ExitReason.SL_HIT.value
            )
        elif ltp >= trade["target_price"]:
            exit_reason = ExitReason.TARGET_HIT.value
        elif gate_no_go and _consecutive_no_go_count(
            jconn, trade["id"]
        ) >= settings.GATE_SUSTAINED_NO_GO_SNAPS:
            exit_reason = ExitReason.GATE_NO_GO.value
        elif iv_crush == IVCrushSeverity.HIGH.value and pnl_pct < -15:
            exit_reason = ExitReason.IV_CRUSH.value

        if exit_reason:
            # close_trade() uses commit=True (default): flushes the buffered
            # insert_snap row + this CLOSED update in one atomic write.
            trades.close_trade(jconn, trade["id"], {
                "exit_premium":   ltp,
                "exit_snap_time": snap_time,
                "exit_reason":    exit_reason,
                "final_pnl_abs":  pnl_abs,
                "final_pnl_pct":  pnl_pct,
            })
            logger.info(
                "Auto-closed {} {} @ {:.1f} | reason={} | pnl={:+.1f}%",
                underlying, opt_type, ltp, exit_reason, pnl_pct
            )

    # P6-E: single WAL flush for all snaps buffered during this tick.
    # Positions that auto-closed above were already flushed by close_trade();
    # this commit covers the remaining open-position snaps -- one disk sync
    # per tick regardless of how many positions are being tracked.
    jconn.commit()


def expire_stale_recommendations(
    jconn:      sqlite3.Connection,
    trade_date: str,
    snap_time:  str,
) -> None:
    """Mark GENERATED trades as EXPIRED after AI_EXPIRY_MAX_SNAPS unactioned.

    P0-2 fix: prior-session recommendations are expired immediately on the
    first tick of a new trading day, before the intraday age check runs.

    Root cause: _snaps_since() uses max(0, ...) internally, so a stale
    recommendation from 15:25 yesterday produces age=0 at 09:15 today --
    never >= AI_EXPIRY_MAX_SNAPS (e.g. 3).  The recommendation then blocks
    generate_recommendation() for the entire new session (get_pending_trades
    returns it via max_age_days=1) until EOD sweep at 15:25.

    Fix: any GENERATED trade whose trade_date is strictly earlier than
    today's trade_date is an orphan from a prior session and is expired
    immediately with a distinct state_reason so it is visible in the log
    and learning report without polluting intraday expiry metrics.

    P1-11: date comparison now uses date objects (date.fromisoformat()) rather
    than raw string inequality.  ISO string ordering is lexicographically
    correct for YYYY-MM-DD, but a malformed date (e.g. '11-03-2026' from a
    locale change or misconfigured parameter) silently inverts the comparison
    instead of raising. Parsing both sides makes the failure loud (ValueError
    in the scheduler log) rather than silently allowing stale recommendations
    to persist across sessions indefinitely.

    H-3: all update_status() calls now use commit=False; a single
    jconn.commit() after the loop issues one WAL flush for all expiries
    (N → 1). This matches the batched-commit pattern used by eod_force_close()
    (EOD-3) and track_open_positions() (P6-E), and makes the expiry set
    atomic: either all land or none do on a mid-loop crash.
    """
    today_date = date.fromisoformat(trade_date)   # P1-11: parse once, reuse below
    pending = trades.get_pending_trades(jconn)
    expired_count = 0
    for trade in pending:
        # P1-11: parse trade_date as a date object for type-safe comparison.
        # Raises ValueError (logged by scheduler) if either date string is
        # malformed, making the format problem immediately visible.
        try:
            trade_dt = date.fromisoformat(trade["trade_date"])
        except (ValueError, TypeError) as e:
            logger.error(
                "P1-11: malformed trade_date={!r} for trade id={} -- skipping expiry check: {}",
                trade["trade_date"], trade["id"], e,
            )
            continue

        # P0-2: expire prior-session orphans immediately -- do not attempt
        # intraday age arithmetic across a session boundary.
        if trade_dt < today_date:
            trades.update_status(
                jconn, trade["id"], TradeStatus.EXPIRED.value,
                state_reason="Stale recommendation from prior session -- expired on new day open",
                commit=False,   # H-3: batch; single commit after loop
            )
            logger.info(
                "P0-2: expired prior-session recommendation id={} "
                "underlying={} trade_date={} (current session: {})",
                trade["id"], trade["underlying"], trade["trade_date"], trade_date,
            )
            expired_count += 1
            continue

        # Same-session intraday expiry: age out unactioned recommendations
        # after AI_EXPIRY_MAX_SNAPS × 5-minute ticks.
        age = _snaps_since(trade["snap_time"], snap_time)
        if age >= settings.AI_EXPIRY_MAX_SNAPS:
            trades.update_status(
                jconn, trade["id"], TradeStatus.EXPIRED.value,
                state_reason="Not actioned within expiry window",
                commit=False,   # H-3: batch; single commit after loop
            )
            expired_count += 1

    # H-3: single WAL flush for all expiries processed in this call.
    # If expired_count == 0 this is a no-op commit (harmless).
    jconn.commit()
    # H-3: summary log so scheduler output shows how many were swept per tick.
    if expired_count > 0:
        logger.info(
            "expire_stale_recommendations: {} recommendation(s) expired "
            "(trade_date={} snap_time={}).",
            expired_count, trade_date, snap_time,
        )


def _minutes_since_entry(entry_snap: str, current_snap: str) -> int:
    try:
        h1, m1 = map(int, entry_snap[:5].split(":"))
        h2, m2 = map(int, current_snap[:5].split(":"))
        return max(0, (h2 * 60 + m2) - (h1 * 60 + m1))
    except Exception:
        return 0


def _snaps_since(entry_snap: str, current_snap: str) -> int:
    # Issue-3: derive interval from config instead of hardcoding 5 minutes.
    interval = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    return _minutes_since_entry(entry_snap, current_snap) // interval


def _consecutive_no_go_count(jconn: sqlite3.Connection, trade_id: int) -> int:
    """Count the most-recent consecutive NO_GO gate verdicts for a trade.

    P4-F9: LIMIT was hardcoded to 10. If GATE_SUSTAINED_NO_GO_SNAPS were
    raised above 10 in config.py, this function would silently only see
    the last 10 rows, so the threshold could never be reached without any
    error or log warning. Fix:
      1. Read n from settings at call time so LIMIT always matches the threshold.
      2. RuntimeError replaces assert -- guard fires even under python -O.
      3. Embed n into LIMIT via f-string (safe: n is an int from settings,
         not user-controlled input).
    """
    n = settings.GATE_SUSTAINED_NO_GO_SNAPS
    # RuntimeError replaces assert -- fires even under python -O.
    # Outside the inner try-except so misconfiguration propagates to the
    # scheduler tick error handler rather than being swallowed as return 0.
    if not (1 <= n <= 50):
        raise RuntimeError(
            f"GATE_SUSTAINED_NO_GO_SNAPS={n} out of safe range [1, 50]. "
            "Raise the SQL LIMIT cap in _consecutive_no_go_count if needed."
        )
    try:
        rows = jconn.execute(
            f"SELECT gate_verdict FROM position_snaps "
            f"WHERE trade_id=? ORDER BY snap_time DESC LIMIT {n}",
            [trade_id]
        ).fetchall()
        count = 0
        for r in rows:
            if r[0] == "NO_GO":
                count += 1
            else:
                break
        return count
    except Exception:
        return 0
