"""Alert generation -- transition-based signals from last 60 min of data."""
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.models import AlertType, AlertSeverity
from optdash.metrics import record_error
from optdash.analytics.gex import get_gex_series, get_net_gex
from optdash.analytics.coc import get_coc_series
from optdash.analytics.pcr import get_pcr_series
from optdash.analytics.microstructure import get_volume_velocity
from optdash.analytics.iv import get_iv_skew
from optdash.analytics.vex_cex import get_vex_cex_current

def _check_skew_vex_convergence(
    skew_data: dict,
    vex_data:  dict,
    underlying: str,
    snap_time: str,
) -> dict | None:
    """
    HIGH_CONVICTION_BEAR alert fires when:
      1. Skew is STEEPENING (put-wing demand accelerating)
      AND
      2. VEX total is below -threshold (bearish vanna pressure from dealers)
    """
    if not settings.SKEW_STEEPENING_VEX_CONFIRM:
        return None

    skew_direction = skew_data.get("skew_direction", "UNKNOWN")
    vex_total      = vex_data.get("vex_total_M", 0.0)
    vex_threshold  = settings.VEX_THRESHOLDS.get(underlying, settings.VEX_BULL_THRESHOLD)

    skew_steepening = (skew_direction == "STEEPENING")
    vex_bearish     = (vex_total < -vex_threshold)

    if skew_steepening and vex_bearish:
        return _make_alert(
            time=snap_time,
            type_=AlertType.HIGH_CONVICTION_BEAR,
            severity=AlertSeverity.HIGH,
            direction="PE",
            headline=f"Skew steepening ({skew_data.get('skew', 'N/A')}%) + VEX bearish ({round(vex_total, 2)} Rs M)",
            message="Vanna-accelerated selling pressure confirmed. High-conviction downside setup.",
        )
    return None

def _check_zgl_proximity(gex_data: dict, spot: float | None, snap_time: str) -> dict | None:
    """
    Fires APPROACHING_ZGL / BELOW_ZGL alert when spot moves around the
    Zero Gamma Level.
    """
    zgl       = gex_data.get("zgl")
    above_zgl = gex_data.get("above_zgl")
    dist_pct  = gex_data.get("spot_vs_zgl")

    if zgl is None or dist_pct is None:
        return None

    proximity_threshold = settings.ZGL_PROXIMITY_PCT

    if above_zgl is False:
        return _make_alert(
            time=snap_time,
            type_=AlertType.BELOW_ZGL,
            severity=AlertSeverity.HIGH,
            direction=None,
            headline=f"Below Zero Gamma Level ({zgl})",
            message=(
                f"Spot is {abs(dist_pct):.1f}% BELOW Zero Gamma Level. "
                "Dealers net short gamma — trending/volatile regime. Avoid mean-reversion strategies."
            ),
        )

    if above_zgl is True and abs(dist_pct) < proximity_threshold:
        return _make_alert(
            time=snap_time,
            type_=AlertType.APPROACHING_ZGL,
            severity=AlertSeverity.MEDIUM,
            direction=None,
            headline=f"Approaching Zero Gamma Level ({zgl})",
            message=(
                f"Spot is within {abs(dist_pct):.1f}% of Zero Gamma Level. "
                "If spot crosses below ZGL, regime flips to trending. Monitor closely."
            ),
        )
    return None


def get_alerts(
    conn: duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
    lookback_snaps: int = 12,
) -> list[dict]:
    """Return list of alerts from the last N snaps (ordered newest first).

    lookback_snaps=12 covers 12 minutes at 1-min cadence (not 60 min).
    Increase to 60 for a 60-minute lookback window at 1-min cadence.
    """
    alerts = []

    try:
        gex_series = get_gex_series(conn, trade_date, underlying)
        coc_series = get_coc_series(conn, trade_date, underlying)
        pcr_series = get_pcr_series(conn, trade_date, underlying)
        vol_series = get_volume_velocity(conn, trade_date, underlying)

        def recent(series):
            filtered = [s for s in series if s["snap_time"] <= snap_time]
            return filtered[-lookback_snaps:]

        gex_w = recent(gex_series)
        coc_w = recent(coc_series)
        pcr_w = recent(pcr_series)
        vol_w = recent(vol_series)

        # Alert: GEX decline below 70% (already has transition guard)
        if len(gex_w) >= 2:
            if gex_w[-1]["pct_of_peak"] < 70 and gex_w[-2]["pct_of_peak"] >= 70:
                alerts.append(_make_alert(
                    time=gex_w[-1]["snap_time"],
                    type_=AlertType.GEX_DECLINE,
                    severity=AlertSeverity.HIGH,
                    direction=None,
                    headline="GEX crossed below 70% of peak",
                    message=f"Net GEX declined to {gex_w[-1]['pct_of_peak']:.0f}% -- gamma pin weakening, directional move easier.",
                ))

        # Alert: V_CoC velocity spike
        # F13a: transition guard -- only fire when signal changes FROM a
        # non-velocity state TO a velocity state. Without this guard the
        # alert fires on every tick while the signal holds, flooding the feed.
        _velocity_signals = ("VELOCITY_BULL", "VELOCITY_BEAR")
        if len(coc_w) >= 2:
            cur_coc  = coc_w[-1]
            prev_coc = coc_w[-2]
            if (cur_coc["signal"]  in _velocity_signals and
                    prev_coc["signal"] not in _velocity_signals):
                if cur_coc["snap_time"] >= settings.ALERT_OPENING_SUPPRESS_END:
                    vcoc = cur_coc.get("v_coc_15m", 0)
                    dir_ = "CE" if vcoc > 0 else "PE"
                    alerts.append(_make_alert(
                        time=cur_coc["snap_time"],
                        type_=AlertType.COC_VELOCITY,
                        severity=AlertSeverity.HIGH,
                        direction=dir_,
                        headline=f"V_CoC velocity spike: {vcoc:+.1f}",
                        message=f"Cost-of-carry velocity {vcoc:+.1f} indicates "
                                f"{'institutional long accumulation' if vcoc > 0 else 'institutional unwinding'}.",
                    ))
        elif len(coc_w) == 1 and coc_w[0]["signal"] in _velocity_signals:
            if coc_w[0]["snap_time"] >= settings.ALERT_OPENING_SUPPRESS_END:
                vcoc = coc_w[0].get("v_coc_15m", 0)
                dir_ = "CE" if vcoc > 0 else "PE"
                alerts.append(_make_alert(
                    time=coc_w[0]["snap_time"],
                    type_=AlertType.COC_VELOCITY,
                    severity=AlertSeverity.HIGH,
                    direction=dir_,
                    headline=f"V_CoC velocity spike: {vcoc:+.1f}",
                    message=f"Cost-of-carry velocity {vcoc:+.1f} indicates "
                            f"{'institutional long accumulation' if vcoc > 0 else 'institutional unwinding'}.",
                ))

        # Alert: PCR divergence threshold cross (already has transition guard)
        # Issue-R1: use config thresholds (matching C4 gate + direction.py Signal 5)
        # instead of hardcoded 0.20.  This ensures tuning PCR_DIV_*_THRESHOLD in
        # .env consistently affects gate, direction, AND alerts together.
        if len(pcr_w) >= 2:
            cur  = pcr_w[-1]["pcr_divergence"]
            prev = pcr_w[-2]["pcr_divergence"]
            bull_thr = settings.PCR_DIV_BULL_THRESHOLD
            bear_thr = settings.PCR_DIV_BEAR_THRESHOLD
            cur_diverged = cur > bull_thr or cur < bear_thr
            prev_normal  = bull_thr >= prev >= bear_thr
            if cur_diverged and prev_normal:
                dir_   = "CE" if cur > 0 else "PE"
                sev    = AlertSeverity.HIGH if abs(cur) > 0.30 else AlertSeverity.MEDIUM
                label  = "Retail panic puts" if cur > 0 else "Retail panic calls"
                alerts.append(_make_alert(
                    time=pcr_w[-1]["snap_time"],
                    type_=AlertType.PCR_DIVERGENCE,
                    severity=sev,
                    direction=dir_,
                    headline=f"PCR divergence: {label} ({cur:+.3f})",
                    message=f"PCR Vol-OI spread crossed {cur:+.3f} -- retail positioning diverging, fade signal.",
                ))

        # Alert: Volume spike
        # F13b: same transition guard pattern as V_CoC above.
        if len(vol_w) >= 2:
            cur_vol  = vol_w[-1]
            prev_vol = vol_w[-2]
            if cur_vol["signal"] == "SPIKE" and prev_vol["signal"] != "SPIKE":
                if cur_vol["snap_time"] >= settings.ALERT_OPENING_SUPPRESS_END:
                    ratio = cur_vol["volume_ratio"]
                    sev   = AlertSeverity.HIGH if ratio >= 3.0 else AlertSeverity.MEDIUM
                    alerts.append(_make_alert(
                        time=cur_vol["snap_time"],
                        type_=AlertType.VOLUME_SPIKE,
                        severity=sev,
                        direction=None,
                        headline=f"Volume spike: {ratio:.1f}x baseline",
                        message=f"Current volume {ratio:.1f}x above rolling median -- unusual activity detected.",
                    ))
        elif len(vol_w) == 1 and vol_w[0]["signal"] == "SPIKE":
            if vol_w[0]["snap_time"] >= settings.ALERT_OPENING_SUPPRESS_END:
                ratio = vol_w[0]["volume_ratio"]
                sev   = AlertSeverity.HIGH if ratio >= 3.0 else AlertSeverity.MEDIUM
                alerts.append(_make_alert(
                    time=vol_w[0]["snap_time"],
                    type_=AlertType.VOLUME_SPIKE,
                    severity=sev,
                    direction=None,
                    headline=f"Volume spike: {ratio:.1f}x baseline",
                    message=f"Current volume {ratio:.1f}x above rolling median -- unusual activity detected.",
                ))

        # ZGL proximity (B-7) / Need gex_current for VEX caching
        gex_current = get_net_gex(conn, trade_date, snap_time, underlying)
        spot = gex_current.get("spot")

        # Skew x VEX convergence (B-2)
        skew_data = get_iv_skew(conn, trade_date, snap_time, underlying)
        vex_data  = get_vex_cex_current(conn, trade_date, snap_time, underlying, gex_data=gex_current)
        skew_vex_alert = _check_skew_vex_convergence(skew_data, vex_data, underlying, snap_time)
        if skew_vex_alert:
            alerts.append(skew_vex_alert)
            
        zgl_alert = _check_zgl_proximity(gex_current, spot, snap_time)
        if zgl_alert:
            alerts.append(zgl_alert)

    except Exception as e:
        record_error("get_alerts")
        logger.warning("get_alerts error: {}", e)

    seen = set()
    unique = []
    for a in sorted(alerts, key=lambda x: x["time"], reverse=True):
        k = (a["type"], a["time"])
        if k not in seen:
            seen.add(k)
            unique.append(a)
    return unique


def _make_alert(time, type_: AlertType, severity: AlertSeverity,
                direction, headline, message) -> dict:
    return {
        "time":      time,
        "type":      type_.value,
        "severity":  severity.value,
        "direction": direction,
        "headline":  headline,
        "message":   message,
    }
