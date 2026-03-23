"""Market data endpoints — spot, GEX, CoC, environment gate."""
from fastapi import APIRouter, Depends, Query
from optdash.api.deps import get_duck
from optdash.api.validators import TradeDate, SnapTime
from optdash.analytics.gex import get_net_gex, get_gex_series, get_spot_summary
from optdash.analytics.coc import get_coc_latest, get_coc_series
from optdash.analytics.environment import get_environment_score
from optdash.config import settings

router = APIRouter()
# DEFAULT_UNDERLYING constant removed -- use settings.DEFAULT_UNDERLYING so
# any .env override is respected without redeploying router code.


@router.get("/spot")
def spot(
    trade_date: TradeDate = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    """
    Returns current spot with full-day OHLC and change_pct.
    Uses get_spot_summary() (arg_max/arg_min) to ensure spot = latest snap,
    day_open = first snap, day_high/low = true intraday range.
    """
    result = get_spot_summary(duck, trade_date, underlying)
    if not result:
        return {"error": "no data"}
    return result


@router.get("/gex")
def gex(
    trade_date: TradeDate = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    return get_gex_series(duck, trade_date, underlying)


@router.get("/gex/current")
def gex_current(
    trade_date: TradeDate = Query(...),
    snap_time:  SnapTime  = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    return get_net_gex(duck, trade_date, snap_time, underlying)


@router.get("/coc")
def coc(
    trade_date: TradeDate = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    return get_coc_series(duck, trade_date, underlying)


@router.get("/coc/current")
def coc_current(
    trade_date: TradeDate = Query(...),
    snap_time:  SnapTime  = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    return get_coc_latest(duck, trade_date, snap_time, underlying)


@router.get("/environment")
def environment(
    trade_date: TradeDate  = Query(...),
    snap_time:  SnapTime   = Query(...),
    underlying: str        = Query(settings.DEFAULT_UNDERLYING),
    direction:  str | None = Query(None),
    duck = Depends(get_duck),
):
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

    return get_environment_score(duck, trade_date, snap_time, underlying, direction, dte)
