"""AI endpoints - recommendation, accept/reject, position, journal, learning."""
import json
import sqlite3
import threading
import time
from fastapi import APIRouter, Depends, Query, HTTPException
from loguru import logger
from pydantic import BaseModel, field_validator

from optdash.api.deps import get_journal
from optdash.api.validators import SnapTime    # Issue-8: single source of truth
from optdash.ai.journal import trades, shadow, snaps
from optdash.ai.learning.report import build_learning_report
from optdash.analytics.pnl import build_theta_sl_series
from optdash.config import settings
from optdash.models import TradeStatus, ExitReason

router = APIRouter()



# -- Request schemas ----------------------------------------------------------

class AcceptRequest(BaseModel):
    trade_id:           int
    snap_time:          SnapTime
    actual_entry_price: float | None = None

    @field_validator("actual_entry_price")
    @classmethod
    def _positive_price(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("actual_entry_price must be a positive number")
        return v


class RejectRequest(BaseModel):
    trade_id: int
    reason:   str
    note:     str | None = None


class CloseRequest(BaseModel):
    trade_id:   int
    exit_price: float
    snap_time:  SnapTime


# -- Learning report TTL cache ------------------------------------------------
# P1-1: _report_cache_lock guards _report_cache against concurrent access from
# anyio's thread pool.  FastAPI runs sync (def) endpoints in worker threads --
# two simultaneous /learning/report requests at the TTL boundary both miss the
# cache and both call build_learning_report() (8 SQLite queries) concurrently
# on the shared app.state.journal connection (check_same_thread=False, not
# truly thread-safe).  The lock uses a double-checked locking pattern:
#   1. Fast path: acquire lock, check hit, return immediately (microseconds).
#   2. Miss path: release lock, run expensive query outside the lock so other
#      endpoints are not blocked during the ~200ms SQLite scan.
#   3. Write path: re-acquire lock, re-check expiry (another thread may have
#      just written a fresh result while we were computing), then write.
_report_cache:      dict[int, dict] = {}
_report_cache_lock: threading.Lock  = threading.Lock()
_REPORT_TTL = 60   # seconds


# -- Response helpers ---------------------------------------------------------

def _hydrate_trade(trade: dict | None) -> dict | None:
    """Deserialize JSON-string fields in a raw journal trade dict."""
    if trade is None:
        return None
    result = dict(trade)
    _JSON_FIELDS: dict[str, object] = {
        "conf_buckets":      {},
        "direction_signals": [],
    }
    for field, default in _JSON_FIELDS.items():
        raw = result.get(field)
        if isinstance(raw, str):
            try:
                result[field] = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning(
                    "_hydrate_trade: failed to parse '{}' for trade_id={} "
                    "-- raw value: {!r:.120} -- defaulting to {}",
                    field,
                    result.get("id"),
                    raw,
                    default,
                )
                result[field] = default
    return result


# -- Endpoints ----------------------------------------------------------------

@router.get("/recommendation/latest")
def latest_recommendation(
    underlying: str = Query(settings.DEFAULT_UNDERLYING),
    jconn: sqlite3.Connection = Depends(get_journal),
):
    pending = trades.get_pending_trades(jconn, underlying=underlying)
    if not pending:
        return {"status": "NO_RECOMMENDATION"}
    return _hydrate_trade(pending[0])


@router.get("/position/live")
def live_position(
    underlying: str = Query(settings.DEFAULT_UNDERLYING),
    jconn: sqlite3.Connection = Depends(get_journal),
):
    open_trades = trades.get_open_trades(jconn, underlying=underlying)
    if not open_trades:
        return {"status": "NO_POSITION"}
    return _hydrate_trade(open_trades[0])


@router.get("/position/snaps/{trade_id}")
def position_snaps(
    trade_id: int,
    jconn: sqlite3.Connection = Depends(get_journal),
):
    trade = trades.get_trade(jconn, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    snap_list = snaps.get_snaps_for_trade(jconn, trade_id)
    return {
        "trade":           _hydrate_trade(trade),
        "snaps":           snap_list,
        "theta_sl_series": build_theta_sl_series(trade, snap_list),
    }


@router.post("/accept")
def accept(
    req:   AcceptRequest,
    jconn: sqlite3.Connection = Depends(get_journal),
):
    trade = trades.get_trade(jconn, req.trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    if trade["status"] != TradeStatus.GENERATED.value:
        raise HTTPException(
            400, f"Cannot accept trade in status '{trade['status']}'"
        )
    trades.accept_trade(jconn, req.trade_id, req.snap_time, req.actual_entry_price)
    return {"status": "accepted", "trade_id": req.trade_id}


@router.post("/reject")
def reject(
    req:   RejectRequest,
    jconn: sqlite3.Connection = Depends(get_journal),
):
    trade = trades.get_trade(jconn, req.trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    if trade["status"] != TradeStatus.GENERATED.value:
        raise HTTPException(
            400,
            f"Can only reject GENERATED recommendations "
            f"(current status='{trade['status']}'). "
            "Use /close-trade to exit a live position."
        )
    try:
        # Fix SHA-2: pass sl_price and target_price so shadow_tracker.py
        # evaluates the hypothetical position using the parameters that were
        # active when the recommendation was generated, not live config values.
        shadow.create_shadow(jconn, {
            "trade_id":      req.trade_id,
            "trade_date":    trade["trade_date"],
            "underlying":    trade["underlying"],
            "option_type":   trade["option_type"],
            "strike_price":  trade["strike_price"],
            "expiry_date":   trade["expiry_date"],
            "entry_premium": trade["entry_premium"],
            "sl_price":      trade["sl_price"],
            "target_price":  trade["target_price"],
        }, commit=False)
        trades.reject_trade(
            jconn, req.trade_id, req.reason, req.note, commit=False
        )
        jconn.commit()
    except Exception as exc:
        jconn.rollback()
        raise HTTPException(
            500, f"Reject failed and was rolled back: {exc}"
        ) from exc
    return {"status": "rejected", "trade_id": req.trade_id}


@router.post("/close-trade")
def close_position(
    req:   CloseRequest,
    jconn: sqlite3.Connection = Depends(get_journal),
):
    """Manually close a live (ACCEPTED) position."""
    trade = trades.get_trade(jconn, req.trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    if trade["status"] != TradeStatus.ACCEPTED.value:
        raise HTTPException(400, f"Trade not open (status='{trade['status']}')")

    underlying = trade["underlying"]

    # Fix API-2: explicit None check so actual_entry_price=0.0 (data corruption)
    # is not silently promoted to entry_premium via the `or` falsy coercion.
    # entry_premium is always set at recommendation time and validated > 0 by
    # the screener; actual_entry_price is set on ACCEPT and validated > 0 by
    # AcceptRequest._positive_price.  A stored 0 means journal corruption --
    # surface it as a 400 rather than recording a phantom pnl_pct of 0.
    _actual = trade.get("actual_entry_price")
    entry   = _actual if _actual is not None else trade.get("entry_premium")
    if not entry:
        raise HTTPException(
            400,
            "Cannot compute PnL: no valid entry price on record for this trade. "
            "Check actual_entry_price / entry_premium in the journal."
        )

    lot     = settings.LOT_SIZES.get(underlying, 1)
    pnl_abs = round((req.exit_price - entry) * lot, 2)
    pnl_pct = round((req.exit_price - entry) / entry * 100, 2)

    trades.close_trade(jconn, req.trade_id, {
        "exit_premium":   req.exit_price,
        "exit_snap_time": req.snap_time,
        "exit_reason":    ExitReason.MANUAL_EXIT.value,
        "final_pnl_abs":  pnl_abs,
        "final_pnl_pct":  pnl_pct,
    })
    return {"status": "closed", "trade_id": req.trade_id, "pnl_pct": pnl_pct}


@router.get("/journal/history")
def trade_history(
    page:       int        = Query(1,  ge=1),
    per_page:   int        = Query(20, ge=5, le=100),
    underlying: str | None = Query(None),
    status:     str | None = Query(None),
    jconn: sqlite3.Connection = Depends(get_journal),
):
    result = trades.get_trade_history(
        jconn, page=page, per_page=per_page,
        underlying=underlying, status=status
    )
    if isinstance(result, dict) and "trades" in result:
        result["trades"] = [_hydrate_trade(t) for t in result["trades"]]
    elif isinstance(result, list):
        result = [_hydrate_trade(t) for t in result]
    return result


@router.get("/learning/report")
def learning_report(
    days: int = Query(30, ge=7, le=365),
    jconn: sqlite3.Connection = Depends(get_journal),
):
    """Return the learning report for the last `days` trading days.

    P1-1: double-checked locking pattern against _report_cache_lock.
    - Fast path (cache hit): lock acquired for microseconds, result returned.
    - Miss path: expensive build_learning_report() runs OUTSIDE the lock
      so other HTTP endpoints are not blocked during the ~200ms SQLite scan.
    - Write path: re-acquire lock and re-check expiry before writing, so a
      second thread that waited at the lock boundary does not overwrite a
      result just written by the first thread.
    """
    # --- fast path: cache hit ---
    with _report_cache_lock:
        cached = _report_cache.get(days)
        if cached and (time.monotonic() - cached["ts"]) < _REPORT_TTL:
            return cached["data"]

    # --- miss path: build outside the lock ---
    data = build_learning_report(jconn, days=days)

    # --- write path: double-check before storing ---
    with _report_cache_lock:
        cached = _report_cache.get(days)
        if not cached or (time.monotonic() - cached["ts"]) >= _REPORT_TTL:
            _report_cache[days] = {"data": data, "ts": time.monotonic()}

    return data
