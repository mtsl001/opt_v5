"""gap_fill.py — Recover missing intraday windows from upxtx at startup.

06:35 IST sync schedule context:
  After 06:35, upxtx is emptied (yesterday's rows moved to upxtx_ar).
  upxtx remains empty until NSE market opens at 09:15 IST.
  pull_day_gap() returning 0 rows during this window is expected:
  logged as INFO (not WARNING), and backfill has already covered that
  day from upxtx_ar.

Non-fatal: per-date failures are logged and the loop continues so a
single bad day does not block the app from starting. Contrast with
backfill.py which is fatal (historical gaps are harder to recover).

P2-10: watermark advance policy for empty BQ results on past days
-----------------------------------------------------------------
An empty pull_day_gap() result for a past day must NOT advance the
watermark to 15:30:00.  upxtx (the live table) is emptied daily at
06:35 -- so a 0-row result for a past day means either the data is
already covered by backfill (upxtx_ar path) or there was a BQ outage.
Advancing the watermark on 0 rows would permanently mark that day as
complete and make any missing data unrecoverable without manual surgery.
The safe default is: skip without advancing, log a WARNING, and let
the next startup retry.  Backfill.py (upxtx_ar path) sets the watermark
independently when it successfully writes data for a past day.

Entry point: run_gap_fill(duck_conn=None)
"""
from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from optdash.config import settings
from optdash.pipeline.bq_client import pull_day_gap
from optdash.pipeline.market_calendar import (
    get_trading_days, today_ist, is_within_market_hours,
)
from optdash.pipeline.processor import process_and_write
from optdash.pipeline.watermark import load as wm_load, save as wm_save


def run_gap_fill(duck_conn=None) -> None:
    """Fill all missing intraday windows between the saved watermark and now.

    Iterates over every trading day from (watermark_date + 1) through today
    inclusive, pulling rows from upxtx that are newer than the watermark.
    """
    if not settings.ENABLE_GAP_FILL:
        logger.info("Gap fill disabled (ENABLE_GAP_FILL=false) — skipping")
        return

    wm      = wm_load()
    wm_dt   = datetime.strptime(wm, "%Y-%m-%d %H:%M:%S")
    wm_date = wm_dt.date()
    today   = today_ist()

    logger.info("Gap fill: watermark={}, today={}", wm, today)

    # Dates to fill: from day after watermark_date through today inclusive.
    # If watermark_date == today, all_dates is empty but today is added below.
    start_fill = wm_date + timedelta(days=1)
    all_dates  = list(get_trading_days(start_fill, today)) if start_fill <= today else []

    # Always include today (even when watermark_date == today and start_fill > today)
    if today not in all_dates:
        all_dates.append(today)

    for d in all_dates:
        ds       = d.strftime("%Y-%m-%d")
        is_today = (d == today)
        # For the watermark day itself, use exact watermark timestamp to avoid re-pulling
        # already-processed rows. For all other days start from midnight.
        pull_from = wm if d == wm_date else f"{ds} 00:00:00"

        try:
            df = pull_day_gap(ds, pull_from, settings.BQ_FQN_LIVE)

            if df.empty:
                if is_today and not is_within_market_hours():
                    # Post-06:35 sync, pre-09:15 open: expected empty.
                    # Backfill (upxtx_ar path) has already covered this day.
                    logger.info(
                        "Gap fill: {} — 0 rows (post-sync window; market not open yet)", ds
                    )
                elif is_today:
                    # During market hours: no new rows yet since last watermark.
                    logger.debug("Gap fill: {} — 0 rows (already current)", ds)
                else:
                    # P2-10: empty result for a PAST day.
                    # upxtx is the live table -- it is emptied at 06:35 daily.
                    # A 0-row result means either:
                    #   a) backfill already covered this day via upxtx_ar, OR
                    #   b) there was a BQ outage / data gap.
                    # Do NOT advance the watermark to EOD in case (b).
                    # Log a WARNING so the operator is alerted.
                    # The next startup will retry this date range.
                    # Backfill.py sets the watermark independently when it
                    # successfully writes data for this day from upxtx_ar.
                    logger.warning(
                        "Gap fill: {} — 0 rows from upxtx for a past day. "
                        "Watermark NOT advanced (possible BQ outage or data gap). "
                        "Backfill should have covered this via upxtx_ar. "
                        "If backfill also missed it, manual re-ingest is needed.",
                        ds,
                    )
                continue

            new_wm = process_and_write(df, duck_conn=duck_conn)
            if new_wm and new_wm > wm:
                wm_save(new_wm)
                wm = new_wm
                logger.info("Gap fill: {} filled — watermark → {}", ds, new_wm)

        except Exception as e:
            # Non-fatal: log and continue so a single bad day does not block startup.
            logger.error("Gap fill: FAILED for {} — {} (continuing)", ds, e)
