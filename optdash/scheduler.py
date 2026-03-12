"""APScheduler -- drives recommendation generation and position tracking.

Tick structure (every SCHEDULER_INTERVAL_SECONDS, market hours only):
  0. Live BQ incremental pull (Step 0)
                                  -> pull new snaps from upxtx since watermark
  1. Expire stale pending recommendations  -> once per tick (all underlyings)
  2. Pre-compute gate cache for open trades -> once per tick (all open positions)
  3. Generate recommendations              -> once per underlying
  4. Track open positions                  -> once per tick (uses gate cache)
  5. Track shadow positions                -> once per tick (all shadow trades)
  6. EOD sweep                             -> once per calendar day

DuckDB connection
-----------------
The scheduler reuses the shared in-process DuckDB gateway connection via
duckdb_gateway.get_conn().  The old pattern (duckdb.connect(duck_path,
read_only=True) per tick) opened a file-based connection that had no
options_data Parquet view registered, causing every analytics call to fail
silently with 'Table options_data not found'.

SQLite connection
-----------------
The scheduler reuses the shared SQLite connection passed in via
journal_conn (owned by deps.startup / app.state.journal).  The old
pattern (_make_jconn per tick) opened a new connection every 5 minutes,
creating ~72 open/close cycles per trading day and bypassing the shared
WAL-mode connection already maintained by the API layer.

Event-loop yielding
-------------------
Each major synchronous phase in tick() is followed by
`await asyncio.sleep(0)` to release the event loop between phases.
This allows HTTP request handlers and WS message dispatch to run
between analytics phases without blocking for the full tick duration.
All DuckDB calls remain on the same thread -- the shared in-process
:memory: connection is not safe for concurrent multi-thread access.

P1-P2-8: first-day partition refresh
--------------------------------------
Step 0 passes get_conn() to run_incremental_pull() so processor.py
calls refresh_views() immediately when a new trade_date= partition
directory is created (first tick of a new trading day).  Previously
duck_conn=None caused the new partition to be invisible to DuckDB
until the EOD refresh_views() call at 15:25 -- a full-day blackout.

P2-7: EOD done_flags always set
---------------------------------
eod_force_close() and finalize_all_shadows() both `raise` on failure.
Previously a raise propagated to the outer tick() except, which logged
it but did NOT set done_flags[trade_date].  The next tick re-entered the
EOD block -- wasteful and noisy.  Fix: wrap both in individual try/except
and set done_flags[trade_date] = True unconditionally before return.
Both functions are idempotent so re-running on a partial-failure day at
next startup is safe (they filter ACCEPTED / is_closed=0 rows only).
"""
import asyncio
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from optdash.config import settings
from optdash.ai.recommender import generate_recommendation
from optdash.ai.tracker import track_open_positions, expire_stale_recommendations
from optdash.ai.shadow_tracker import track_shadow_positions
from optdash.ai.eod import eod_force_close, finalize_all_shadows
from optdash.ai.journal.trades import get_open_trades
from optdash.analytics.environment import get_environment_score
from optdash.pipeline.purge import purge_old_raw_parquets
from optdash.pipeline.duckdb_gateway import get_conn, refresh_views

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _today_str() -> str:
    # Issue-6: use IST-aware date to stay consistent with _snap_time_str().
    # date.today() uses the system timezone, which would produce wrong dates
    # on a UTC server during 00:00–05:30 UTC (05:30–11:00 IST morning session).
    return _now_ist().date().strftime("%Y-%m-%d")


def _snap_time_str() -> str:
    """Round down to nearest scheduler-interval snap (HH:MM).

    Fix S-3: interval_mins derived from SCHEDULER_INTERVAL_SECONDS so
    the snap key produced here always matches the keys written by the BQ
    feed pipeline and read by DuckDB queries. Hardcoded // 5 produced
    wrong snap keys at any non-5-minute interval (e.g. 10-min ticks),
    causing every analytics call to return empty data silently.
    """
    now           = _now_ist()
    interval_mins = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    mins          = (now.minute // interval_mins) * interval_mins
    return f"{now.hour:02d}:{mins:02d}"


def _is_market_hours() -> bool:
    now = _now_ist()
    if now.weekday() >= 5:          # Sat / Sun
        return False
    # F10: check NSE market holidays so scheduler skips all 468 ticks per
    # holiday day rather than running analytics that return empty data.
    # getattr with [] default: startup never fails if MARKET_HOLIDAYS is not
    # yet declared in config.py. Populate with YYYY-MM-DD strings:
    #   MARKET_HOLIDAYS=["2026-03-14","2026-04-18"] in .env or config.py.
    holidays = getattr(settings, "MARKET_HOLIDAYS", [])
    if _today_str() in set(holidays):
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= t <= (15 * 60 + 30)


def _is_eod() -> bool:
    # P2-6: correctness depends on EOD_SWEEP_TIME being zero-padded HH:MM,
    # which is enforced by the _check_hhmm validator in config.py.
    # _snap_time_str() always produces zero-padded output (f"{h:02d}:{m:02d}").
    # Both sides are zero-padded HH:MM, so lexicographic >= is correct.
    return _snap_time_str() >= settings.EOD_SWEEP_TIME


def _eod_done_today(done_flags: dict) -> bool:
    return done_flags.get(_today_str(), False)


def _build_gate_cache(
    duck,
    trade_date:       str,
    snap_time:        str,
    jconn:            sqlite3.Connection,
    _gex_peak_cache:  dict | None = None,
) -> dict:
    """Pre-compute gate scores for all currently open positions.

    Fix-L (F-04): track_open_positions() was calling get_environment_score()
    (7 DuckDB aggregations) once per open trade. This pre-computation runs
    the gate exactly once per unique underlying and passes the result in so
    track_open_positions() can skip the per-position re-query.

    Gate is computed with the actual option_type (direction) so C9 (2 pts)
    scores correctly and NO_GO verdicts are not under-counted.

    _gex_peak_cache: mutable dict passed in from tick(). When provided,
    get_environment_score() -> get_net_gex() will populate it on first use
    per (trade_date, underlying) and reuse it on subsequent calls, eliminating
    redundant full-day DuckDB peak scans across _build_gate_cache and
    generate_recommendation within the same tick.
    """
    open_trades = get_open_trades(jconn)
    cache: dict[str, dict] = {}
    for t in open_trades:
        underlying = t["underlying"]
        if underlying in cache:
            # F9: design invariant -- pre-flight Rule 6 in the recommender
            # guarantees at most one ACCEPTED trade per underlying at any
            # time, so the first match is always the only match. The cache
            # key is `underlying` (not `(underlying, option_type)`) by
            # design. IMPORTANT: if multi-leg support is added in future,
            # the key MUST be changed to (underlying, option_type) and all
            # callers updated, otherwise gate scores for the second leg
            # will silently use the first leg's direction.
            continue
        try:
            cache[underlying] = get_environment_score(
                duck, trade_date, snap_time,
                underlying,
                direction=t["option_type"],
                _peak_cache=_gex_peak_cache,
            )
        except Exception as e:
            logger.warning("gate_cache pre-compute failed for {}: {}", underlying, e)
            cache[underlying] = {
                "score": 0, "verdict": "NO_GO",
                "conditions": {}, "session": ""
            }
    return cache


def create_scheduler(
    journal_conn: sqlite3.Connection,
) -> AsyncIOScheduler:
    """
    Returns a configured AsyncIOScheduler.
    Call scheduler.start() inside FastAPI lifespan (after deps.startup()).

    journal_conn: shared SQLite connection owned by deps.startup()
    (stored on app.state.journal).  The scheduler reuses this connection
    directly -- no new SQLite connections are opened per tick.

    Note: duck_path removed -- the scheduler now uses the shared DuckDB
    gateway connection (get_conn()) so it reads the same registered
    Parquet view as the API.  duckdb_gateway.startup() must be called
    first -- deps.startup() does this automatically.
    """
    done_flags: dict[str, bool] = {}

    async def tick() -> None:
        if not _is_market_hours():
            return

        trade_date = _today_str()
        snap_time  = _snap_time_str()

        try:
            # Shared in-process DuckDB -- gateway owns its lifecycle.
            duck  = get_conn()
            # Shared SQLite -- lifecycle owned by deps.shutdown().
            # Do NOT close jconn here.
            jconn = journal_conn

            # -- Step 0: Live BQ incremental pull -----------------------------
            # Pull new rows from upxtx since last watermark, write to today's
            # Parquet file atomically. Blocking BQ I/O off the event loop.
            # Import inside try so a missing google-cloud-bigquery install
            # does not prevent the tick from running on non-BQ deployments.
            # Non-fatal: exception logged, tick continues with last available
            # data so recommendations and gate scores are not blocked.
            #
            # P1-P2-8: pass duck (LockedConn) to run_incremental_pull so that
            # processor._write_trade_date() calls refresh_views() immediately
            # when the first tick of a new trading day creates a new partition
            # directory.  Previously duck_conn=None meant the new day's
            # Parquet was invisible to DuckDB until the EOD refresh at 15:25
            # -- a full-day analytics blackout.
            try:
                from optdash.pipeline.incremental import run_incremental_pull
                new_data = await asyncio.to_thread(
                    run_incremental_pull, duck_conn=duck
                )
                if new_data:
                    logger.debug("[{}] Incremental BQ snap ingested.", snap_time)
            except Exception as inc_err:
                logger.error("Incremental BQ pull failed @ {}: {}", snap_time, inc_err)
            await asyncio.sleep(0)

            # -- EOD sweep (once per calendar day) ----------------------------
            if _is_eod() and not _eod_done_today(done_flags):
                logger.info("Running EOD sweep for {}", trade_date)

                # P2-7: wrap each EOD step in its own try/except.
                # done_flags[trade_date] is set UNCONDITIONALLY below so a
                # failure in either step does not cause the EOD block to
                # re-enter on subsequent ticks.  Both functions are idempotent
                # (filter ACCEPTED / is_closed=0 rows), so any unclosed
                # positions will be swept by the next startup's gap fill.
                eod_ok = True

                try:
                    eod_force_close(duck, jconn, trade_date)
                except Exception as eod_err:
                    eod_ok = False
                    logger.error(
                        "EOD force-close FAILED for {} -- open positions may remain. "
                        "Check journal on next startup.",
                        trade_date, exc_info=True,
                    )

                try:
                    finalize_all_shadows(duck, jconn, trade_date)
                except Exception as shadow_err:
                    eod_ok = False
                    logger.error(
                        "EOD finalize_shadows FAILED for {} -- shadows may remain open. "
                        "Will retry on next EOD run via get_all_unclosed_shadows().",
                        trade_date, exc_info=True,
                    )

                if not eod_ok:
                    logger.warning(
                        "EOD sweep for {} completed with errors -- done_flags set to "
                        "prevent re-entry. Manual review recommended.",
                        trade_date,
                    )

                # Purge stale raw Parquets (once per day at EOD).
                try:
                    purge_old_raw_parquets(
                        Path(settings.DATA_ROOT),
                        settings.RAW_PARQUET_RETENTION_DAYS,
                    )
                except Exception as purge_err:
                    logger.error("raw Parquet purge failed: {}", purge_err)

                # Roll the DuckDB view forward so tomorrow's new partition
                # directory is visible on the first tick after midnight.
                try:
                    refresh_views(duck)
                    logger.info("DuckDB view refreshed for next trading day.")
                except Exception as rv_err:
                    logger.error("refresh_views failed: {}", rv_err, exc_info=True)

                # Trim done_flags to last 7 entries to prevent unbounded growth.
                if len(done_flags) > 7:
                    oldest = sorted(done_flags)[0]
                    del done_flags[oldest]

                # P2-7: set UNCONDITIONALLY -- even if eod_force_close or
                # finalize_all_shadows raised.  Prevents re-entry on subsequent
                # ticks.  Any cleanup is handled at next startup by idempotent
                # get_open_trades / get_all_unclosed_shadows filters.
                done_flags[trade_date] = True
                return  # skip normal tick on the EOD sweep tick itself

            # F8: block all normal-tick work on any tick AFTER EOD has already
            # been processed today. Without this, the 15:30 tick (the last tick
            # that _is_market_hours() passes) falls through to Steps 1-5,
            # including generate_recommendation(). That issues a post-market
            # trade card that persists overnight as a stale GENERATED
            # recommendation until expire_stale_recommendations fires next morning.
            if _eod_done_today(done_flags):
                return

            # -- Step 1: Expire stale pending recommendations ------------------
            expire_stale_recommendations(jconn, trade_date, snap_time)
            # Yield: allow HTTP/WS handlers queued during Step 1 to run before
            # the heavier gate-cache computation in Step 2.
            await asyncio.sleep(0)

            # -- GEX peak cache (shared across Steps 2 & 3) -------------------
            # Created fresh each tick so stale values from a prior tick never
            # leak. Being a plain dict it is populated in-place by
            # get_net_gex() -> _get_gex_peak() on first access per underlying,
            # then reused for all subsequent calls within the same tick.
            _gex_peak_cache: dict = {}

            # -- Step 2: Pre-compute gate cache for all open positions ---------
            # get_environment_score runs 7 DuckDB aggregations per underlying.
            # Caching here avoids repeating those queries once per open trade
            # inside track_open_positions() (Fix-L / F-04).
            # _gex_peak_cache is populated in-place during this call so
            # Step 3 (generate_recommendation) reuses peak values already
            # computed for open-position underlyings.
            gate_cache = _build_gate_cache(
                duck, trade_date, snap_time, jconn,
                _gex_peak_cache=_gex_peak_cache,
            )
            # Yield: allow HTTP/WS handlers to run before the recommender loop.
            await asyncio.sleep(0)

            # -- Step 3: Generate new recommendations (per underlying) ---------
            for underlying in settings.UNDERLYINGS:
                rec = generate_recommendation(
                    duck, jconn, trade_date, snap_time, underlying
                )
                if rec:
                    logger.info(
                        "[{}] Recommendation issued: {} {} @ {}",
                        snap_time,
                        underlying,
                        rec.get("option_type"),
                        rec.get("strike_price"),
                    )
                # Yield after each underlying so HTTP/WS handlers can interleave
                # between per-underlying recommendation runs.
                await asyncio.sleep(0)

            # -- Step 4: Track all open positions (gate_cache avoids N+1) ------
            track_open_positions(duck, jconn, trade_date, snap_time,
                                 gate_cache=gate_cache)
            # Yield: allow HTTP/WS handlers to run before shadow tracking.
            await asyncio.sleep(0)

            # -- Step 5: Track all shadow positions ----------------------------
            track_shadow_positions(duck, jconn, trade_date, snap_time)
            # No yield needed -- end of tick.

        except Exception as e:
            logger.error("Scheduler tick error @ {}: {}", snap_time, e, exc_info=True)

    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        tick,
        trigger="interval",
        seconds=settings.SCHEDULER_INTERVAL_SECONDS,
        id="main_tick",
        max_instances=1,   # never overlap ticks
        coalesce=True,     # skip missed ticks on wake-up
    )
    return scheduler
