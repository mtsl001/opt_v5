"""Parquet writer -- 1 file per underlying per calendar day.

Each 1-minute scheduler tick calls write_snap() for every underlying.
Because Parquet does not support native append, the writer uses a
read-merge-rewrite pattern:

  1. If a file already exists for today's underlying, load it.
  2. Drop any existing rows for ALL incoming snap_times (idempotent re-runs).
  3. Concat the new rows, sort by snap_time, write back atomically.

Atomicity
---------
Writes go to a ``.tmp`` sibling first, then Path.replace() performs an
atomic rename (POSIX syscall) so DuckDB always sees either the previous
complete file or the new complete file -- never a partial write.

Concurrency
-----------
filelock.FileLock serialises concurrent read-merge-write access on a
``.lock`` sibling.  This guards against scheduler-restart overlaps where
two writer instances could otherwise race on the same file.

File layout
-----------
  data/processed/trade_date=YYYY-MM-DD/
      NIFTY.parquet
      BANKNIFTY.parquet
      FINNIFTY.parquet
      ...

The ``trade_date=`` directory prefix is the hive partition key DuckDB
uses for partition pruning -- trade_date is auto-extracted from the
path and does not need to be stored as a column.  The ``underlying``
column is stored inside each file so analytics queries can filter by
both dimensions.

Performance
-----------
At 75 snaps x ~500 strikes per underlying, a full daily file is
~37 500 rows.  pyarrow write of that size is typically <50 ms --
well within the 5-minute scheduler tick budget.

Schema enforcement (W-1)
------------------------
PARQUET_SCHEMA declares explicit dtypes for all 28 analytics columns.
Passing it to pa.Table.from_pandas() prevents pandas dtype inference
from producing int32/float32 columns on snaps where certain rows are
absent (e.g. no futures rows -> Greeks inferred as int64 instead of
float64). DuckDB union_by_name=true silently NULLs any column whose
dtype drifts between daily files, corrupting gate scores and GEX
calculations without any error or log entry.

Column notes
------------
- option_type: nullable=True because FUT rows have no option_type (NULL).
  Previously nullable=False caused ArrowInvalid crash on every futures write.
- bid_qty / ask_qty: mapped from BQ total_buy_qty / total_sell_qty.
  Cumulative day buy/sell flow totals.  Required by coc.py get_atm_obi(),
  get_futures_obi() and pcr.py _smoothed_obi().
- bid1_qty / bid1_price: live L1 best-bid from depth_bid1_qty/price.
  Instantaneous order book state. NULL when order book side is empty.
- ask1_qty / ask1_price: live L1 best-ask from depth_ask1_qty/price.
  Analytics upgrades (coc.py / pcr.py) will use these for true-OBI.
- open: intrabar open price. NULL when absent in older BQ backfill data.
- vex / cex: Vanna Exposure and Charm Exposure, computed by processor.py.
  Required by vex_cex.py analytics.
- s_score: REMOVED. Computed live by screener.py -- must never be stored
  in Parquet (per-snap value would be stale immediately after write).
- rho: NOT ADDED. Not provided by Upstox API / BQ feed.

P2-14: FileLock timeout
-----------------------
The advisory lock timeout is read from settings.WRITER_LOCK_TIMEOUT_SECONDS
(default 30 s, same as the previous hardcoded value) so it can be tuned
per deployment in .env without a code change.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from filelock import FileLock
from loguru import logger

from optdash.config import settings

PROCESSED_SUBDIR   = "processed"
_PARTITION_PREFIX  = "trade_date="

# Canonical Parquet schema for all processed files.
# Enforced at write time so dtype drift between daily files never produces
# silent NULL columns when DuckDB reads them via union_by_name=true.
#
# Rules:
#   - nullable=False: column must be present in every row (write fails loudly
#     on missing data so upstream BQ feed gaps are caught at ingest time).
#   - nullable=True:  column may be NULL (Greeks absent for spot/futures rows;
#     dte absent for non-expiring instruments; option_type NULL for FUT rows).
#   - All price/Greek columns are float64 -- prevents int32/float32 drift from
#     pandas inference that breaks DuckDB predicate pushdown min/max stats.
#
# MIGRATION NOTE: removing or renaming a column here is a breaking schema
# change. Update REQUIRED_COLUMNS in duckdb_gateway.py to match any additions.
PARQUET_SCHEMA = pa.schema([
    pa.field("snap_time",       pa.string(),  nullable=False),
    pa.field("underlying",      pa.string(),  nullable=False),
    pa.field("strike_price",    pa.float64(), nullable=True),   # NULL for FUT rows
    pa.field("expiry_date",     pa.string(),  nullable=False),
    pa.field("option_type",     pa.string(),  nullable=True),   # NULL for FUT rows
    pa.field("instrument_type", pa.string(),  nullable=True),
    pa.field("ltp",             pa.float64(), nullable=True),
    pa.field("open",            pa.float64(), nullable=True),   # intrabar open
    pa.field("iv",              pa.float64(), nullable=True),
    pa.field("delta",           pa.float64(), nullable=True),
    pa.field("theta",           pa.float64(), nullable=True),
    pa.field("gamma",           pa.float64(), nullable=True),
    pa.field("vega",            pa.float64(), nullable=True),
    pa.field("spot",            pa.float64(), nullable=True),
    pa.field("fut_price",       pa.float64(), nullable=True),
    pa.field("oi",              pa.int64(),   nullable=True),
    pa.field("volume",          pa.int64(),   nullable=True),
    pa.field("bid_qty",         pa.int64(),   nullable=True),   # cumulative day buy flow
    pa.field("ask_qty",         pa.int64(),   nullable=True),   # cumulative day sell flow
    # 5-level bid depth — L1 = best bid, L5 = deepest.
    # qty/orders: int64 (lot counts); price: float64.
    # NULL when fewer than N levels exist (illiquid / far-OTM strikes).
    pa.field("bid1_qty",        pa.int64(),   nullable=True),
    pa.field("bid1_price",      pa.float64(), nullable=True),
    pa.field("bid1_orders",     pa.int64(),   nullable=True),
    pa.field("bid2_qty",        pa.int64(),   nullable=True),
    pa.field("bid2_price",      pa.float64(), nullable=True),
    pa.field("bid2_orders",     pa.int64(),   nullable=True),
    pa.field("bid3_qty",        pa.int64(),   nullable=True),
    pa.field("bid3_price",      pa.float64(), nullable=True),
    pa.field("bid3_orders",     pa.int64(),   nullable=True),
    pa.field("bid4_qty",        pa.int64(),   nullable=True),
    pa.field("bid4_price",      pa.float64(), nullable=True),
    pa.field("bid4_orders",     pa.int64(),   nullable=True),
    pa.field("bid5_qty",        pa.int64(),   nullable=True),
    pa.field("bid5_price",      pa.float64(), nullable=True),
    pa.field("bid5_orders",     pa.int64(),   nullable=True),
    # 5-level ask depth — L1 = best ask, L5 = deepest.
    pa.field("ask1_qty",        pa.int64(),   nullable=True),
    pa.field("ask1_price",      pa.float64(), nullable=True),
    pa.field("ask1_orders",     pa.int64(),   nullable=True),
    pa.field("ask2_qty",        pa.int64(),   nullable=True),
    pa.field("ask2_price",      pa.float64(), nullable=True),
    pa.field("ask2_orders",     pa.int64(),   nullable=True),
    pa.field("ask3_qty",        pa.int64(),   nullable=True),
    pa.field("ask3_price",      pa.float64(), nullable=True),
    pa.field("ask3_orders",     pa.int64(),   nullable=True),
    pa.field("ask4_qty",        pa.int64(),   nullable=True),
    pa.field("ask4_price",      pa.float64(), nullable=True),
    pa.field("ask4_orders",     pa.int64(),   nullable=True),
    pa.field("ask5_qty",        pa.int64(),   nullable=True),
    pa.field("ask5_price",      pa.float64(), nullable=True),
    pa.field("ask5_orders",     pa.int64(),   nullable=True),
    pa.field("gex",             pa.float64(), nullable=True),
    pa.field("vex",             pa.float64(), nullable=True),   # Vanna Exposure
    pa.field("cex",             pa.float64(), nullable=True),   # Charm Exposure
    pa.field("expiry_tier",     pa.string(),  nullable=True),
    pa.field("dte",             pa.int32(),   nullable=True),
    # REMOVED: s_score  -- computed live by screener.py, never stored in Parquet
    # REMOVED: rho      -- not available from Upstox API / BQ feed
])


def parquet_path(data_root: Path, trade_date: str, underlying: str) -> Path:
    """Return the canonical Parquet path for a given day + underlying."""
    return (
        data_root
        / PROCESSED_SUBDIR
        / f"{_PARTITION_PREFIX}{trade_date}"
        / f"{underlying}.parquet"
    )


def write_snap(
    data_root:  Path,
    trade_date: str,
    underlying: str,
    snap_df:    pd.DataFrame,
) -> None:
    """Merge *snap_df* rows into today's Parquet file for *underlying*.

    Parameters
    ----------
    data_root:  root data directory (settings.DATA_ROOT)
    trade_date: ISO date string, e.g. '2026-03-06'
    underlying: instrument name, e.g. 'NIFTY'
    snap_df:    DataFrame for this snap -- must include a 'snap_time' column
    """
    if snap_df.empty:
        return

    path = parquet_path(data_root, trade_date, underlying)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Advisory lock: serialises concurrent read-merge-write on the same file.
    # P2-14: timeout read from settings so it can be tuned per deployment
    # without a code change.  Default 30 s preserves the previous behaviour.
    # Raise on timeout rather than stacking up writers behind the lock.
    lock_path = path.with_suffix(".lock")
    with FileLock(str(lock_path), timeout=settings.WRITER_LOCK_TIMEOUT_SECONDS):
        _write_snap_locked(path, snap_df, trade_date, underlying)


def _write_snap_locked(
    path:       Path,
    snap_df:    pd.DataFrame,
    trade_date: str,
    underlying: str,
) -> None:
    """Core read-merge-write logic, called with the file lock already held."""
    tmp_path = path.with_suffix(".tmp")
    try:
        if path.exists():
            existing = pd.read_parquet(path)
            # F3 fix: drop ALL incoming snap_times, not just iloc[0].
            # A backfill/retry batch may cover multiple snap windows; dropping
            # only the first snap_time left duplicates for the rest.
            incoming_snaps = set(snap_df["snap_time"].unique())
            existing = existing[
                ~existing["snap_time"].isin(incoming_snaps)
            ]
            merged = pd.concat([existing, snap_df], ignore_index=True)
        else:
            merged = snap_df.copy()

        merged = merged.sort_values(
            ["snap_time", "strike_price", "option_type"],
            ignore_index=True,
        )

        # Fix W-1: enforce PARQUET_SCHEMA so every daily file has identical
        # dtypes. safe=False raises ArrowInvalid immediately on cast failure
        # rather than silently coercing bad values to NULL (safe=True default).
        # This surfaces upstream BQ feed dtype changes at write time, not at
        # query time when analytics return silent NULLs for gate/GEX columns.
        try:
            table = pa.Table.from_pandas(
                merged,
                schema=PARQUET_SCHEMA,
                preserve_index=False,
                safe=False,
            )
        except (pa.lib.ArrowInvalid, pa.lib.ArrowTypeError) as schema_err:
            logger.error(
                "[Writer] Schema cast failed for {}/{}: {}. "
                "Verify BQ feed column dtypes match PARQUET_SCHEMA.",
                trade_date, underlying, schema_err,
            )
            raise

        # F2 fix: write to .tmp then atomically rename.
        # Path.replace() is an atomic rename syscall on POSIX; DuckDB always
        # sees either the previous complete file or this new complete file.
        pq.write_table(
            table, tmp_path,
            compression="snappy",
            write_statistics=True,   # enables min/max pushdown in DuckDB
        )
        tmp_path.replace(path)   # atomic on POSIX (same filesystem)

        logger.debug(
            "[Writer] {}/{} -- {} rows -> {}",
            trade_date, underlying, len(merged), path.name,
        )

    except Exception:
        # Never leave a partial .tmp behind; the original file is untouched.
        tmp_path.unlink(missing_ok=True)
        raise
