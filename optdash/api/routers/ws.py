"""WebSocket -- streams live snap updates to the frontend every 5 seconds."""
import asyncio
import json
from datetime import date
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from loguru import logger

from optdash.analytics.gex import get_net_gex
from optdash.analytics.coc import get_coc_latest
from optdash.analytics.environment import get_environment_score
from optdash.analytics.pcr import get_pcr
from optdash.analytics.vex_cex import get_vex_cex_current
from optdash.analytics.alerts import get_alerts
from optdash.ai.journal import trades
from optdash.config import settings

router = APIRouter()


@router.websocket("/live")
async def live_feed(
    ws:         WebSocket,
    trade_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    snap_time:  str = Query("LIVE"),
    underlying: str = Query(settings.DEFAULT_UNDERLYING),   # F4: honour config
):
    await ws.accept()

    # Fix WS-3: validate trade_date is a real calendar date after ws.accept()
    # so the client receives a structured error over the WS channel.
    # The Query pattern above rejects malformed strings at the HTTP upgrade
    # level (400 before ws.accept()); this check catches structurally valid
    # but impossible dates (e.g. '2026-02-30', '2026-13-01') that pass the
    # regex but would cause every DuckDB bind to return empty results silently
    # for the full lifetime of the connection.
    try:
        date.fromisoformat(trade_date)
    except ValueError:
        await ws.send_json({
            "error": f"Invalid trade_date '{trade_date}': not a real calendar date. "
                     "Format: YYYY-MM-DD (e.g. '2026-03-09')."
        })
        await ws.close()
        return

    duck = ws.app.state.duck

    # F2: use the dedicated event-loop SQLite connection.
    # app.state.journal is shared with FastAPI's anyio thread pool (HTTP
    # request handlers). sqlite3.Connection is NOT thread-safe; using it
    # here from the async event loop alongside concurrent HTTP requests
    # produces silent data races and potential OperationalError crashes.
    # app.state.scheduler_journal is the connection reserved for all
    # async / event-loop callers (scheduler tick + this WS handler).
    journal = ws.app.state.scheduler_journal

    # F4: reject an unknown underlying immediately after accept() so the
    # client receives a clear error rather than a stream of empty payloads
    # for the full lifetime of the connection (potentially 30+ minutes).
    if underlying not in settings.UNDERLYINGS:
        await ws.send_json({
            "error": f"Unknown underlying '{underlying}'. "
                     f"Valid values: {settings.UNDERLYINGS}"
        })
        await ws.close()
        return

    logger.info("WS connect: {} {} {}", underlying, trade_date, snap_time)
    try:
        while True:
            eff_snap = (
                _latest_snap(duck, trade_date, underlying)
                if snap_time == "LIVE"
                else snap_time
            )

            if not eff_snap:
                await ws.send_json({"error": "no data"})
                await asyncio.sleep(settings.WS_INTERVAL_SECONDS)
                continue

            payload = await _build_payload(duck, journal, trade_date, eff_snap, underlying)
            await ws.send_text(json.dumps(payload, default=str))
            await asyncio.sleep(settings.WS_INTERVAL_SECONDS)

    except WebSocketDisconnect:
        logger.info("WS disconnect: {} {}", underlying, snap_time)
    except Exception as e:
        logger.warning("WS error: {}", e)
        await ws.close()


def _latest_snap(duck, trade_date: str, underlying: str) -> str | None:
    try:
        row = duck.execute(
            """SELECT MAX(snap_time) FROM options_data
               WHERE trade_date=? AND underlying=?""",
            [trade_date, underlying]
        ).fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.warning("WS _latest_snap failed for {}/{}: {}", underlying, trade_date, e)
        return None


async def _build_payload(
    duck, journal, trade_date: str, snap_time: str, underlying: str
) -> dict:
    """Async: build the full WS payload, yielding between each analytics phase.

    Each `await asyncio.sleep(0)` releases the event loop for one iteration
    so HTTP request handlers and other WS connections can be serviced between
    analytics calls. All DuckDB access remains on the same thread -- the
    shared in-process :memory: connection is not safe for concurrent
    multi-thread access.
    """
    try:
        # F3: fetch open trades FIRST so direction is available for the gate.
        # C9 (VEX alignment, 2 pts) only evaluates when direction is passed to
        # get_environment_score(). Calling without direction made the WS gate
        # score permanently 0-2 pts lower than the recommender's internal gate.
        open_t    = trades.get_open_trades(journal, underlying=underlying)
        direction = open_t[0]["option_type"] if open_t else None
        await asyncio.sleep(0)  # yield: allow other handlers to run

        dte = None
        try:
            row = duck.execute(
                "SELECT MIN(expiry_date) FROM options_data WHERE trade_date=? AND snap_time=? AND underlying=? AND expiry_tier='TIER1' AND expiry_date >= ?",
                [trade_date, snap_time, underlying, trade_date]
            ).fetchone()
            if row and row[0]:
                from datetime import datetime
                t_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
                e_date = datetime.strptime(row[0], "%Y-%m-%d").date()
                dte = (e_date - t_date).days
        except Exception:
            pass

        env = get_environment_score(duck, trade_date, snap_time, underlying,
                                    direction=direction, dte=dte)
        await asyncio.sleep(0)  # yield: env score runs 7 aggregations

        gex = get_net_gex(duck, trade_date, snap_time, underlying)
        coc = get_coc_latest(duck, trade_date, snap_time, underlying)
        await asyncio.sleep(0)  # yield

        pcr = get_pcr(duck, trade_date, snap_time, underlying)
        vex = get_vex_cex_current(duck, trade_date, snap_time, underlying)
        await asyncio.sleep(0)  # yield

        alerts  = get_alerts(duck, trade_date, snap_time, underlying)
        pending = trades.get_pending_trades(journal, underlying=underlying)
        await asyncio.sleep(0)  # yield

        return {
            "snap_time":     snap_time,
            "underlying":    underlying,
            "environment":   env,
            "gex":           gex,
            "coc":           coc,
            "pcr":           pcr,
            "vex":           vex,
            "alerts":        alerts[:5],
            # [0] = most-recent (get_pending_trades ORDER BY created_at DESC)
            "pending_trade": pending[0] if pending else None,
            "open_trade":    open_t[0]  if open_t  else None,
        }
    except Exception as e:
        logger.warning("WS _build_payload error: {}", e)
        return {"error": str(e), "snap_time": snap_time}
