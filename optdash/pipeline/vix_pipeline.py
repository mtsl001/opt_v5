"""vix_pipeline.py — Lightweight side-pull from upxtx_vix (India VIX + indices).

Called by scheduler.py tick() independently of the main options pull.
upxtx_vix contains 30 NSE index values (including India VIX) at 1-min cadence.

Storage layout
--------------
data/processed/vix/trade_date=YYYY-MM-DD/vix.parquet

Schema: one row per snap_time — wide format with one column per index.
Only the three primary index values are stored (others ignored):
  india_vix   — India VIX (the key risk signal)
  nifty_50    — Nifty 50 spot
  nifty_bank  — Bank Nifty spot

Watermark
---------
Separate watermark file: data/vix_watermark.json
Advances independently of the main options watermark so a VIX BQ outage
does not affect options ingestion or vice versa.

P1-3 seam: same IST/UTC numeric equality applies — watermark is naive IST,
BQ record_time is UTC-labelled IST-numeric.  Same cancellation logic holds.

Design notes
------------
- Non-fatal by design: scheduler tick catches all exceptions and logs them
  so a VIX pull failure never stops options ingestion.
- No FileLock needed: single writer (the scheduler tick), single file per
  calendar day. PyArrow atomic .tmp → .replace() is sufficient.
- VIX data is additive context — not consumed by any existing analytics
  module yet.  Future gate / IV module can read via DuckDB:
    SELECT * FROM read_parquet('data/processed/vix/**/*.parquet')
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger

from optdash.config import settings
from optdash.pipeline.bq_client import pull_vix_incremental

# Underlying labels as returned by upxtx_vix
_VIX_UNDERLYING       = "India VIX"
_NIFTY50_UNDERLYING   = "Nifty 50"
_NIFTYBANK_UNDERLYING = "Nifty Bank"

_VIX_SUBDIR         = "vix"
_VIX_PARTITION_PFX  = "trade_date="
_VIX_WATERMARK_FILE = "vix_watermark.json"

VIX_PARQUET_SCHEMA = pa.schema([
    pa.field("snap_time",  pa.string(),  nullable=False),
    pa.field("trade_date", pa.string(),  nullable=False),
    pa.field("india_vix",  pa.float64(), nullable=True),
    pa.field("nifty_50",   pa.float64(), nullable=True),
    pa.field("nifty_bank", pa.float64(), nullable=True),
])


# ── Watermark helpers ─────────────────────────────────────────────────────────

def _wm_path() -> Path:
    return Path(settings.DATA_ROOT) / _VIX_WATERMARK_FILE


def _load_vix_watermark() -> str:
    """Load VIX watermark; default to BACKFILL_START_DATE 00:00:00."""
    p = _wm_path()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            return data.get("watermark", f"{settings.BACKFILL_START_DATE} 00:00:00")
        except Exception:
            pass
    return f"{settings.BACKFILL_START_DATE} 00:00:00"


def _save_vix_watermark(wm: str) -> None:
    p = _wm_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"watermark": wm}))
    tmp.replace(p)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_vix_pull() -> bool:
    """Pull new VIX/index rows from upxtx_vix and write to processed/vix/.

    Returns True if new data was written, False if no new rows.
    Never raises — non-fatal by design.
    """
    try:
        wm = _load_vix_watermark()
        df = pull_vix_incremental(wm, settings.BQ_FQN_VIX)

        if df is None or df.empty:
            return False

        new_wm = _process_and_write_vix(df)
        if new_wm and new_wm > wm:
            _save_vix_watermark(new_wm)
            logger.debug("VIX pull complete — watermark → {}", new_wm)
            return True

        return False

    except Exception as e:
        logger.error("vix_pipeline: pull failed — {}", e)
        return False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _process_and_write_vix(df: pd.DataFrame) -> str | None:
    """Transform raw INDEX rows → per-snap wide Parquet rows.

    Input df columns: record_time, underlying, ltp
    Output Parquet columns: snap_time, trade_date, india_vix, nifty_50, nifty_bank
    """
    df = df.copy()

    # Strip tz-info (same IST/UTC seam as main processor._strip_tz())
    rt = pd.to_datetime(df["record_time"])
    df["_rt"] = rt.dt.tz_convert(None) if rt.dt.tz is not None else rt

    new_wm_ts = df["_rt"].max()

    # Derive snap_time and trade_date using same floor as processor
    interval_mins = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    freq_str = f"{interval_mins}min"
    df["snap_time"]  = df["_rt"].dt.floor(freq_str).dt.strftime("%H:%M")
    df["trade_date"] = df["_rt"].dt.date.astype(str)

    df["ltp"] = pd.to_numeric(df["ltp"], errors="coerce")

    # Filter to the three underlyings we care about
    wanted = {_VIX_UNDERLYING, _NIFTY50_UNDERLYING, _NIFTYBANK_UNDERLYING}
    df = df[df["underlying"].isin(wanted)].copy()

    if df.empty:
        logger.warning("vix_pipeline: no known index underlyings found in pull")
        return None

    # For each (trade_date, snap_time), take last ltp per underlying
    # (multiple rows per snap can occur if the cadence is faster than the floor)
    agg = (
        df.groupby(["trade_date", "snap_time", "underlying"])["ltp"]
        .last()
        .reset_index()
    )

    # Pivot to wide format: one column per underlying
    wide = agg.pivot_table(
        index=["trade_date", "snap_time"],
        columns="underlying",
        values="ltp",
        aggfunc="last",
    ).reset_index()
    wide.columns.name = None

    # Rename to clean column names, handle missing underlyings gracefully
    rename_map = {
        _VIX_UNDERLYING:       "india_vix",
        _NIFTY50_UNDERLYING:   "nifty_50",
        _NIFTYBANK_UNDERLYING: "nifty_bank",
    }
    for src, dst in rename_map.items():
        if src in wide.columns:
            wide = wide.rename(columns={src: dst})
        else:
            wide[dst] = float("nan")

    # Write per trade_date
    for trade_date, td_df in wide.groupby("trade_date"):
        _write_vix_day(str(trade_date), td_df.reset_index(drop=True))

    # Return watermark as naive IST string
    return str(new_wm_ts)[:19]


def _write_vix_day(trade_date: str, df: pd.DataFrame) -> None:
    """Write / merge VIX rows for one calendar day."""
    vix_root  = Path(settings.DATA_ROOT) / _VIX_SUBDIR
    part_dir  = vix_root / f"{_VIX_PARTITION_PFX}{trade_date}"
    part_dir.mkdir(parents=True, exist_ok=True)
    path      = part_dir / "vix.parquet"
    tmp_path  = path.with_suffix(".tmp")

    out_cols = ["snap_time", "trade_date", "india_vix", "nifty_50", "nifty_bank"]
    out_df = df.reindex(columns=out_cols)

    try:
        if path.exists():
            existing = pd.read_parquet(path)
            existing = existing[~existing["snap_time"].isin(set(out_df["snap_time"]))]
            merged = pd.concat([existing, out_df], ignore_index=True)
        else:
            merged = out_df.copy()

        merged = merged.sort_values("snap_time", ignore_index=True)

        table = pa.Table.from_pandas(
            merged, schema=VIX_PARQUET_SCHEMA, preserve_index=False, safe=False
        )
        pq.write_table(table, tmp_path, compression="snappy")
        tmp_path.replace(path)

        logger.debug(
            "[VIX Writer] {} — {} snaps -> {}",
            trade_date, len(merged), path.name,
        )
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
