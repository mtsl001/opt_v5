"""processor.py — Transform raw BQ DataFrame into PARQUET_SCHEMA-compliant rows.

BQ → Parquet column mapping (full):
  record_time        → snap_time (floor to SCHEDULER_INTERVAL_SECONDS, HH:MM), trade_date (YYYY-MM-DD)
  underlying         → underlying (direct)
  instrument_type    → instrument_type (OPTIDX→OPT, FUTIDX→FUT)
  option_type        → option_type (direct; NULL for FUT rows)
  expiry_date        → expiry_date (M/D/YYYY → YYYY-MM-DD ISO)
  strike_price       → strike_price (float64)
  underlying_spot    → spot (float64)
  ltp + close        → ltp (COALESCE: ltp first, then intraday close fallback)
  volume             → volume (int64)
  oi                 → oi (int64)
  total_buy_qty      → bid_qty (int64)
  total_sell_qty     → ask_qty (int64)
  open               → open (float64)
  depth_bid1_qty     → bid1_qty (int64)    ◄ live L1 best-bid qty
  depth_bid1_price   → bid1_price (float64) ◄ live L1 best-bid price
  depth_ask1_qty     → ask1_qty (int64)    ◄ live L1 best-ask qty
  depth_ask1_price   → ask1_price (float64) ◄ live L1 best-ask price
  iv/delta/theta/gamma/vega → direct (float64)
  avg_volume_20d     → avg_volume_20d (float64) — pre-computed 20-day rolling
                       avg volume from BQ; used by screener.py momentum factor.
  (computed)         → fut_price (near-FUT ltp back-filled per snap_time)
  (computed)         → dte ((expiry_date − trade_date).days)
  (computed)         → expiry_tier (TIER1≤15, TIER2 16–45, TIER3>45)
  (computed)         → gex (γ × OI × lot × spot² × 0.01 × dir; CE=+1, PE=−1)
  (computed)         → vex (OI × lot × vanna × spot / 1e6)
  (computed)         → cex (OI × lot × charm / 1e6)

Columns NOT written to Parquet:
  instrument_key  — used only to identify FUT rows internally
  close_price     — yesterday's settlement; wrong as ltp fallback
  last_trade_time — not needed by any analytics module
  rho             — not provided by Upstox / BQ feed

P2-1: vectorisation
--------------------
snap_time floor, dte → expiry_tier, and sqrt_t inside _compute_gex_vex_cex
are now fully vectorised using dt.floor(freq_str), pd.cut(), and np.where +
np.sqrt(). The old _assign_tier apply(lambda) loop and the math.sqrt
apply(lambda) loop are removed. See _compute_snap_and_dates and
_compute_gex_vex_cex docstrings for details.

Entry point: process_and_write(df, duck_conn=None) → new watermark str | None
"""
from __future__ import annotations

from pathlib import Path

import math

import numpy as np
import pandas as pd
from loguru import logger
from scipy.stats import norm

from optdash.config import settings
from optdash.pipeline.writer import write_snap, parquet_path
from optdash.pipeline.watermark import to_str as wm_str

# GEX sign convention: CE=+1 (dealers net long gamma — pinning effect)
# PE=−1 (dealers net short gamma — directional pressure)
# Matches gex.py _classify_regime(): SUM(gex) > 0 → POSITIVE_CHOP.
_GEX_SIGN = {"CE": +1, "PE": -1}
_VEX_SCALE = 1e6  # Rs M — matches vex_cex.py / 1e6 divisor
_CEX_SCALE = 1e6


def _bsm_d1_d2(
    spot: float, strike: float, sigma: float, t: float, r: float
) -> tuple[float | None, float | None]:
    """
    Compute BSM d1 and d2.
    Returns (None, None) on invalid inputs to prevent downstream errors.
    All inputs must be strictly positive (spot, strike, sigma, t > 0).

    NOTE: scalar utility kept for external callers / unit tests.
    The hot path in _compute_gex_vex_cex is fully vectorised and does
    not call this function.
    """
    if spot <= 0 or strike <= 0 or sigma <= 0 or t <= 0:
        return None, None
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
        d2 = d1 - sigma * math.sqrt(t)
        return d1, d2
    except (ValueError, ZeroDivisionError):
        return None, None


def _compute_exact_vanna(
    spot: float, strike: float, sigma: float, t: float, r: float,
    vanna_clip: float
) -> float:
    """
    Exact BSM Vanna = -(Vega_BSM × d2) / (Spot × σ × √T)

    Vanna measures how delta changes as implied volatility changes.
    Positive vanna: delta increases as IV rises (call) or delta decreases as
    IV drops (put). For a dealer short options, rising IV forces delta hedging.

    NOTE: scalar utility kept for external callers / unit tests.
    The hot path in _compute_gex_vex_cex is fully vectorised and does
    not call this function.
    """
    d1, d2 = _bsm_d1_d2(spot, strike, sigma, t, r)
    if d1 is None:
        return 0.0
    sqrt_t = math.sqrt(t)
    vega_bsm = spot * norm.pdf(d1) * sqrt_t  # S × N'(d1) × √T
    denom = spot * sigma * sqrt_t
    if abs(denom) < 1e-10:
        return 0.0
    vanna = -(vega_bsm * d2) / denom
    return float(max(-vanna_clip, min(vanna_clip, vanna)))


def _compute_exact_charm(
    spot: float, strike: float, sigma: float, t: float, r: float,
    charm_clip: float
) -> float:
    """
    Exact BSM Charm = -N'(d1) × [2rT - d2 × σ√T] / [2T × σ√T]

    Charm = dDelta/dTime. Measures how much delta decays per unit of time.
    On expiry day, charm flow is the primary driver of dealer delta-hedging.
    Negative charm = delta decays toward 0 as time passes (typical for calls).

    NOTE: scalar utility kept for external callers / unit tests.
    The hot path in _compute_gex_vex_cex is fully vectorised and does
    not call this function.
    """
    d1, d2 = _bsm_d1_d2(spot, strike, sigma, t, r)
    if d1 is None or t <= 0:
        return 0.0
    sqrt_t = math.sqrt(t)
    numerator = 2 * r * t - d2 * sigma * sqrt_t
    denominator = 2 * t * sigma * sqrt_t
    if abs(denominator) < 1e-10:
        return 0.0
    charm = -norm.pdf(d1) * (numerator / denominator)
    return float(max(-charm_clip, min(charm_clip, charm)))


# Output column order — MUST match PARQUET_SCHEMA field order in writer.py exactly.
# Issue-R12: any column added/removed here must also be updated in
# writer.py::PARQUET_SCHEMA (for dtype enforcement) and vice versa.
# Enrichment columns computed by this module: gex, vex, cex, expiry_tier, dte.
# All other columns are extracted/renamed directly from the BQ feed.
_OUT_COLS = [
    "snap_time", "underlying", "strike_price", "expiry_date",
    "option_type", "instrument_type", "ltp", "open", "iv", "delta", "theta",
    "gamma", "vega", "spot", "fut_price", "oi", "volume",
    "bid_qty", "ask_qty",
    # 5-level bid depth (L1 = best bid, L5 = deepest)
    "bid1_qty", "bid1_price", "bid1_orders",
    "bid2_qty", "bid2_price", "bid2_orders",
    "bid3_qty", "bid3_price", "bid3_orders",
    "bid4_qty", "bid4_price", "bid4_orders",
    "bid5_qty", "bid5_price", "bid5_orders",
    # 5-level ask depth (L1 = best ask, L5 = deepest)
    "ask1_qty", "ask1_price", "ask1_orders",
    "ask2_qty", "ask2_price", "ask2_orders",
    "ask3_qty", "ask3_price", "ask3_orders",
    "ask4_qty", "ask4_price", "ask4_orders",
    "ask5_qty", "ask5_price", "ask5_orders",
    "gex", "vex", "cex", "expiry_tier", "dte",
    # avg_volume_20d: pre-computed 20-day rolling avg volume from BQ pipeline.
    # Passed through directly from BQ feed (in BQ_SELECT_COLS).
    # Required by screener.py momentum factor: volume / avg_volume_20d.
    # nullable=True in PARQUET_SCHEMA: absent in older backfill data.
    "avg_volume_20d",
]


def process_and_write(df: pd.DataFrame, duck_conn=None) -> str | None:
    """Main entry point: transform BQ DataFrame and write Parquet files.

    Parameters
    ----------
    df        : Raw BQ DataFrame from bq_client pull functions.
    duck_conn : Optional live DuckDB connection. When provided and a new
                partition directory is created, refresh_views() is called
                so the new day is immediately queryable without restart.

    Returns
    -------
    New watermark string ('YYYY-MM-DD HH:MM:SS') or None if df is empty.
    """
    if df is None or df.empty:
        return None

    df = _strip_tz(df)
    df = _normalize_types(df)
    df = _compute_snap_and_dates(df)

    new_wm = wm_str(df["_rt"].max())

    # P0-A: shared set ensures refresh_views() fires at most once per
    # trade_date across all underlyings in this batch, regardless of how many
    # underlyings are processed. Each underlying that creates a new partition
    # directory adds the trade_date to _refreshed; subsequent underlyings for
    # the same date skip the refresh call.
    _refreshed: set[str] = set()
    _skipped: list[str] = []

    for underlying, u_df in df.groupby("underlying"):
        lot_size = settings.LOT_SIZES.get(str(underlying))
        if lot_size is None:
            _skipped.append(str(underlying))
            continue
        try:
            _process_underlying(str(underlying), u_df.copy(), lot_size, duck_conn,
                                 _refreshed)
        except Exception as e:
            # Fix O-4: continue instead of raise so a bad-data failure for one
            # underlying does not abort processing of the remaining underlyings.
            logger.error("processor: failed for {} (skipping): {}", underlying, e,
                         exc_info=True)
            continue

    if _skipped:
        logger.debug(
            "processor: skipped {} underlyings not in LOT_SIZES (stock F&O)",
            len(_skipped),
        )

    return new_wm


# ── Internal pipeline steps ───────────────────────────────────────────────

def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    """Strip tz-info from record_time without any timezone conversion.

    BQ returns record_time as a tz-aware Series (UTC-labelled) but the
    numeric wall-clock values are IST — no actual UTC→IST conversion was
    applied at ingest time. We strip tz-info only, preserving the IST
    wall-clock numbers unchanged.

    tz_localize(None) raises TypeError on an already-tz-aware Series;
    tz_convert(None) is the correct call to detach tz-info from tz-aware
    timestamps. Naive timestamps (no tz) pass through unchanged.
    """
    df = df.copy()
    rt = pd.to_datetime(df["record_time"])
    df["_rt"] = rt.dt.tz_convert(None) if rt.dt.tz is not None else rt
    return df


def _normalize_types(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise instrument_type, expiry_date format, column renames and casts."""
    df = df.copy()

    # instrument_type: OPTIDX→OPT, FUTIDX→FUT.
    # Must be done BEFORE _compute_fut_price() which filters WHERE instrument_type='FUT'.
    _itype_map = {"OPTIDX": "OPT", "FUTIDX": "FUT"}
    df["instrument_type"] = (
        df["instrument_type"].map(_itype_map).fillna(df["instrument_type"])
    )

    # expiry_date: M/D/YYYY → YYYY-MM-DD ISO.
    # Must be done BEFORE dte calculation so pd.to_datetime() parses correctly.
    # Also required for correct string sort in DuckDB IV term-structure queries.
    df["expiry_date"] = (
        pd.to_datetime(df["expiry_date"], dayfirst=False).dt.strftime("%Y-%m-%d")
    )

    # effective_ltp: primary price ltp, fallback to intraday running close.
    # close_price (yesterday's settlement) is excluded from BQ_SELECT_COLS entirely.
    df["ltp"]   = pd.to_numeric(df["ltp"],   errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["ltp"]   = df["ltp"].combine_first(df["close"])

    # spot from underlying_spot
    df["spot"] = pd.to_numeric(df["underlying_spot"], errors="coerce")

    # bid_qty / ask_qty from cumulative day buy/sell totals
    df["bid_qty"] = pd.to_numeric(df["total_buy_qty"],  errors="coerce").astype("Int64")
    df["ask_qty"] = pd.to_numeric(df["total_sell_qty"], errors="coerce").astype("Int64")

    # Level-1..5 order book depth — live bid/ask state at this snap.
    # Fix O-3: log a one-time warning when depth columns are absent so a BQ
    # schema rename is discoverable at ingest time, not silently as NULL OBI.
    if "depth_bid1_qty" not in df.columns:
        logger.warning(
            "_normalize_types: depth_bid1_qty column absent — "
            "all order-book depth columns will be NULL. "
            "Check BQ feed schema for depth_bidN_qty / depth_askN_qty columns."
        )

    for lvl in range(1, 6):
        for side in ("bid", "ask"):
            src_q = f"depth_{side}{lvl}_qty"
            src_p = f"depth_{side}{lvl}_price"
            src_o = f"depth_{side}{lvl}_orders"
            df[f"{side}{lvl}_qty"]    = pd.to_numeric(df.get(src_q), errors="coerce").astype("Int64")
            df[f"{side}{lvl}_price"]  = pd.to_numeric(df.get(src_p), errors="coerce")
            df[f"{side}{lvl}_orders"] = pd.to_numeric(df.get(src_o), errors="coerce").astype("Int64")

    # open: intrabar open price (absent in older BQ pulls → NaN)
    df["open"] = pd.to_numeric(df.get("open"), errors="coerce")

    # Greek + price casts to float64
    for col in ["strike_price", "iv", "delta", "theta", "gamma", "vega"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # OI / volume casts
    df["oi"]     = pd.to_numeric(df["oi"],     errors="coerce").astype("Int64")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("Int64")

    # avg_volume_20d: pre-computed 20-day rolling avg volume from BQ.
    # nullable — absent in older backfill data that predates the BQ column.
    # screener.py uses NULLIF(TRY_CAST(avg_volume_20d AS DOUBLE), 0) so NULL
    # rows simply produce a NULL momentum factor (treated as neutral).
    df["avg_volume_20d"] = pd.to_numeric(df.get("avg_volume_20d"), errors="coerce")

    return df


def _compute_snap_and_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Compute snap_time, trade_date, dte, expiry_tier from normalised columns.

    P2-1 vectorisation:
    snap_time   : dt.floor() with frequency derived from SCHEDULER_INTERVAL_SECONDS
                  so snap keys match DuckDB query keys at any tick interval.
    expiry_tier : pd.cut() — single C-level array pass (~2,500× faster than apply).
                  Bins: dte ∈ [0,15] → TIER1, (15,45] → TIER2, (45,∞) → TIER3.
                  dte < 0 or NaN (FUT rows, expired strikes) → None.
    """
    df = df.copy()

    # P2-1: derive floor frequency from config, not hardcoded '5min'.
    interval_mins = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    freq_str = f"{interval_mins}min"

    df["snap_time"]  = df["_rt"].dt.floor(freq_str).dt.strftime("%H:%M")
    df["trade_date"] = df["_rt"].dt.date.astype(str)

    # dte: calendar days from trade_date to expiry_date
    df["_td"] = pd.to_datetime(df["trade_date"])
    df["_ed"] = pd.to_datetime(df["expiry_date"])
    df["dte"] = (df["_ed"] - df["_td"]).dt.days.astype("Int32")

    # P2-1: vectorised expiry_tier via pd.cut().
    dte_f = df["dte"].astype("float64")
    df["expiry_tier"] = pd.cut(
        dte_f,
        bins=[-0.001, 15.0, 45.0, float("inf")],
        labels=["TIER1", "TIER2", "TIER3"],
        right=True,
    ).astype(object)
    df.loc[dte_f < 0, "expiry_tier"] = None

    return df


def _process_underlying(
    underlying: str,
    df: pd.DataFrame,
    lot_size: int,
    duck_conn,
    _refreshed: set[str],
) -> None:
    """Compute FUT price, GEX/VEX/CEX, then write per-trade_date Parquets."""
    df = _compute_fut_price(df, underlying)
    df = _compute_gex_vex_cex(df, lot_size)

    for trade_date, td_df in df.groupby("trade_date"):
        _write_trade_date(str(underlying), str(trade_date), td_df, duck_conn,
                          _refreshed)


def _compute_fut_price(df: pd.DataFrame, underlying: str) -> pd.DataFrame:
    """Back-fill near-month futures ltp onto all rows per snap_time.

    Near-month = minimum dte > 0 among FUT rows for that snap.

    Fix P0-B: dte=0 (expiry-day settlement rows) are excluded from near-month
    candidate set. On rollover day both the expiring contract (dte=0) and
    the new near-month (dte=7) are present in the feed. The old filter
    (dte >= 0) always selected the expiring row because sort_values("dte")
    .first() picks the minimum — producing wrong CoC, screener eff_ratio,
    and gate scores for the entire rollover session.
    """
    df = df.copy()
    df["fut_price"] = np.nan

    fut = df[
        (df["instrument_type"] == "FUT")
        & df["dte"].notna()
        & (df["dte"] > 0)
    ].copy()

    if fut.empty:
        logger.warning(
            "No non-expiry FUT rows found for {} — fut_price will be NULL "
            "(rollover day or feed gap; settlement rows with dte=0 excluded)",
            underlying,
        )
        return df

    near = (
        fut.sort_values("dte")
        .groupby("snap_time", as_index=False)
        .first()[["snap_time", "ltp"]]
        .rename(columns={"ltp": "_fut_ltp"})
    )

    df = df.merge(near, on="snap_time", how="left")
    df["fut_price"] = df["_fut_ltp"]
    return df.drop(columns=["_fut_ltp"])


def _compute_gex_vex_cex(df: pd.DataFrame, lot_size: int) -> pd.DataFrame:
    """Compute per-strike GEX, VEX, CEX for OPT rows. FUT rows remain NaN.

    GEX  = γ × OI × lot × spot² × 0.01 × dir   (CE=+1, PE=−1)
    VEX  = OI × lot × vanna × spot / 1e6         (Rs M units)
    CEX  = OI × lot × charm / 1e6                (Rs M units)

    iv is a percentage (e.g. 21.33) — divide by 100 to get decimal σ.
    dte=0 (expiry day): t=0 so VEX/CEX remain NaN; GEX still valid.

    Fix P0-3: vanna clipped to [−VANNA_CLIP, +VANNA_CLIP] — near-zero IV rows
              produce denominator ≈ 0, yielding vanna 100–10,000+ that would
              permanently corrupt VEX totals in Parquet.
    Fix P0-2: charm clipped to [−CHARM_CLIP, +CHARM_CLIP] — same failure mode.
    P2-1: fully vectorised via NumPy — no row-by-row .apply() calls.
    """
    df = df.copy()
    df["gex"] = np.nan
    df["vex"] = np.nan
    df["cex"] = np.nan

    mask = (
        (df["instrument_type"] == "OPT")
        & df["option_type"].isin(["CE", "PE"])
    )

    opts = df[mask].copy()
    if opts.empty:
        return df

    opts["_dir"] = opts["option_type"].map(_GEX_SIGN).fillna(0)

    # GEX — vectorised
    spot_sq      = opts["spot"] ** 2
    opts["gex"]  = opts["gamma"] * opts["oi"] * lot_size * spot_sq * 0.01 * opts["_dir"]

    dte_f   = opts["dte"].astype("float64")
    t_val   = dte_f.fillna(0).clip(lower=0) / 365.0
    sig_val = opts["iv"].fillna(0) / 100.0
    r_val   = settings.RISK_FREE_RATE

    s_arr   = opts["spot"].fillna(0.0).values
    k_arr   = opts["strike_price"].fillna(0.0).values
    sig_arr = sig_val.values
    t_arr   = t_val.values

    # Vectorised BSM d1/d2 — invalid inputs produce NaN
    with np.errstate(divide="ignore", invalid="ignore"):
        valid  = (s_arr > 0) & (k_arr > 0) & (sig_arr > 0) & (t_arr > 0)
        sqrt_t = np.where(valid, np.sqrt(t_arr), np.nan)
        d1 = np.where(
            valid,
            (np.log(np.where(valid, s_arr / k_arr, 1.0)) +
             (r_val + 0.5 * sig_arr ** 2) * t_arr) / (sig_arr * sqrt_t),
            np.nan,
        )
        d2 = d1 - sig_arr * sqrt_t

        nd1      = norm.pdf(d1)           # 0 where d1=NaN — safe
        vega_bsm = s_arr * nd1 * sqrt_t

        # Vanna = -(vega_bsm × d2) / (spot × σ × √T)
        vanna_denom   = s_arr * sig_arr * sqrt_t
        raw_vanna_arr = np.where(
            valid & (np.abs(vanna_denom) > 1e-10),
            -(vega_bsm * d2) / vanna_denom,
            0.0,
        )

        # Charm = -N'(d1) × [2rT - d2 × σ√T] / [2T × σ√T]
        charm_num     = 2 * r_val * t_arr - d2 * sig_arr * sqrt_t
        charm_denom   = 2 * t_arr * sig_arr * sqrt_t
        raw_charm_arr = np.where(
            valid & (np.abs(charm_denom) > 1e-10),
            -nd1 * (charm_num / charm_denom),
            0.0,
        )

    raw_vanna = pd.Series(raw_vanna_arr, index=opts.index)
    raw_charm = pd.Series(raw_charm_arr, index=opts.index)

    vanna = raw_vanna.clip(-settings.VANNA_CLIP, settings.VANNA_CLIP)
    charm = raw_charm.clip(-settings.CHARM_CLIP, settings.CHARM_CLIP)

    opts["vex"] = (opts["oi"] * lot_size * vanna * opts["spot"]) / _VEX_SCALE
    opts["cex"] = (opts["oi"] * lot_size * charm)                 / _CEX_SCALE

    # Clip-rate monitoring — log WARNING when >5% of OPT rows are clipped.
    total_opt_rows   = len(opts)
    clip_count_vanna = int((raw_vanna.abs() > settings.VANNA_CLIP).sum())
    clip_count_charm = int((raw_charm.abs() > settings.CHARM_CLIP).sum())

    if total_opt_rows > 0:
        und_name   = str(df["underlying"].iloc[0]) if not df.empty else "UNKNOWN"
        vanna_rate = clip_count_vanna / total_opt_rows
        charm_rate = clip_count_charm / total_opt_rows
        if vanna_rate > 0.05:
            logger.warning(
                "HIGH VANNA CLIP RATE {:.1%} ({}/{} rows) for {}. "
                "Check for near-zero IV rows in BQ feed.",
                vanna_rate, clip_count_vanna, total_opt_rows, und_name,
            )
        if charm_rate > 0.05:
            logger.warning(
                "HIGH CHARM CLIP RATE {:.1%} ({}/{} rows) for {}. "
                "Check for near-zero IV rows in BQ feed.",
                charm_rate, clip_count_charm, total_opt_rows, und_name,
            )

    for col in ("gex", "vex", "cex"):
        df.loc[mask, col] = opts[col].astype("float64").values
    return df


def _write_trade_date(
    underlying: str,
    trade_date: str,
    td_df: pd.DataFrame,
    duck_conn,
    _refreshed: set[str],
) -> None:
    """Write all snaps for one (underlying, trade_date) pair.

    P0-A: refresh_views() is called at most once per trade_date across
    all underlyings processed in the same batch. _refreshed is a set
    passed in from process_and_write(); the first underlying that creates
    a new partition directory triggers the refresh and adds trade_date to
    the set. Subsequent underlyings for the same new date skip the call,
    preventing redundant DROP+CREATE+validate cycles.
    """
    data_root     = Path(settings.DATA_ROOT)
    path          = parquet_path(data_root, trade_date, underlying)
    new_partition = not path.parent.exists()

    out_df = td_df.reindex(columns=_OUT_COLS)

    for snap_time, snap_df in out_df.groupby("snap_time"):
        write_snap(data_root, trade_date, underlying, snap_df.reset_index(drop=True))

    n_snaps = out_df["snap_time"].nunique()
    logger.debug(
        "processor: {}/{} — {} snaps ({} rows)",
        trade_date, underlying, n_snaps, len(out_df),
    )

    if new_partition and duck_conn is not None and trade_date not in _refreshed:
        from optdash.pipeline.duckdb_gateway import refresh_views
        try:
            refresh_views(duck_conn)
            logger.info("DuckDB view refreshed (new partition: {})", trade_date)
            _refreshed.add(trade_date)
        except Exception as e:
            logger.error("refresh_views after new partition failed: {}", e)
