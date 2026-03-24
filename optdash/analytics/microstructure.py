"""Microstructure analytics -- volume velocity."""
import duckdb
from loguru import logger
from optdash.metrics import record_error
from optdash.config import settings


def get_volume_velocity(conn: duckdb.DuckDBPyConnection, trade_date: str,
                        underlying: str, since_snap: str = None) -> list[dict]:
    """Full-day volume ratio vs rolling 10-snap median baseline.

    Index 0 (09:15 opening-auction snap) is excluded from all rolling
    baseline windows. Its volume is typically 3-5x a normal snap due to
    pre-market order accumulation and would suppress ratio readings for
    the first ~50 minutes of the session if left in the window.
    """
    try:
        params = [trade_date, underlying]
        snap_clause = ""
        if since_snap:
            snap_clause = " AND snap_time >= ?"
            params.append(since_snap)

        rows = conn.execute(f"""
            SELECT snap_time, SUM(volume) AS vol_total
            FROM options_data
            WHERE trade_date=? AND underlying=?{snap_clause}
            GROUP BY snap_time ORDER BY snap_time
        """, params).fetchall()
        if not rows:
            return []
        result = []
        vols = [r[1] or 0 for r in rows]
        for i, r in enumerate(rows):
            if i == 0:
                # Opening-auction snap: no prior baseline exists.
                # ratio=1.0 (neutral) so it never triggers a false SPIKE alert.
                # baseline stored as own volume for reference only.
                ratio    = 1.0
                baseline = vols[i]
            else:
                # Rolling median window starting at index 1 (never 0).
                # max(1, i - window_snaps) ensures the opening-auction snap is permanently
                # excluded even for i=1..window_snaps when the window would otherwise
                # reach back to index 0.
                window   = vols[max(1, i - settings.VOLUME_VELOCITY_BASELINE_SNAPS):i]
                if not window:
                    baseline = vols[i]
                else:
                    sw = sorted(window)
                    n = len(sw)
                    baseline = (sw[n//2 - 1] + sw[n//2]) / 2.0 if n % 2 == 0 else sw[n//2]
                ratio    = (vols[i] / baseline) if baseline else 1.0
            result.append({
                "snap_time":    r[0],
                "vol_total":    int(vols[i]),
                "baseline_vol": int(baseline),
                "volume_ratio": round(ratio, 2),
                "signal":       "SPIKE" if ratio >= settings.VOLUME_SPIKE_THRESHOLD else "NORMAL",
            })
        return result
    except Exception as e:
        record_error("get_volume_velocity")
        logger.warning("get_volume_velocity error: {}", e)
        return []
