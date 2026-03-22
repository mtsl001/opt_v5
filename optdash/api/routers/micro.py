"""Microstructure endpoints — PCR, alerts, volume velocity, VEX/CEX."""
from fastapi import APIRouter, Depends, Query
from optdash.api.deps import get_duck
from optdash.api.validators import TradeDate, SnapTime
from optdash.analytics.pcr import get_pcr_series
from optdash.analytics.alerts import get_alerts
from optdash.analytics.microstructure import get_volume_velocity
from optdash.analytics.vex_cex import get_vex_cex_full
from optdash.config import settings

router = APIRouter()
# DEFAULT_UNDERLYING constant removed -- use settings.DEFAULT_UNDERLYING so
# any .env override is respected without redeploying router code.


@router.get("/pcr")
def pcr(
    trade_date: TradeDate = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    return get_pcr_series(duck, trade_date, underlying)


@router.get("/alerts")
def alerts(
    trade_date: TradeDate = Query(...),
    snap_time:  SnapTime  = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    return get_alerts(duck, trade_date, snap_time, underlying)


@router.get("/volume-velocity")
def volume_velocity(
    trade_date: TradeDate = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    return get_volume_velocity(duck, trade_date, underlying)


@router.get("/vex-cex")
def vex_cex(
    trade_date: TradeDate = Query(...),
    snap_time:  SnapTime  = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    return get_vex_cex_full(duck, trade_date, snap_time, underlying)


@router.get("/vex-cex/by-strike")
def vex_cex_by_strike(
    trade_date: TradeDate = Query(...),
    snap_time:  SnapTime  = Query(...),
    underlying: str       = Query(settings.DEFAULT_UNDERLYING),
    duck = Depends(get_duck),
):
    """
    Per-strike VEX and CEX breakdown for frontend heatmap rendering.
    Returns list of {strike_price, option_type, moneyness_pct, vex_M, cex_M, oi, iv, dte}.
    Sorted by strike_price ascending.
    """
    from optdash.analytics.vex_cex import _get_by_strike
    return _get_by_strike(duck, trade_date, snap_time, underlying)
