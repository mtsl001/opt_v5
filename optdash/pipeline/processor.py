"""processor.py — Transform raw BQ DataFrame into PARQUET_SCHEMA-compliant rows.

BQ → Parquet column mapping (full):
  record_time         → snap_time (floor to SCHEDULER_INTERVAL_SECONDS, HH:MM), trade_date (YYYY-MM-DD)
  underlying          → underlying (direct)
  instrument_type     → instrument_type (OPTIDX→OPT, FUTIDX→FUT)
  option_type         → option_type (direct; NULL for FUT rows)
  expiry_date         → expiry_date (M/D/YYYY → YYYY-MM-DD ISO)
  strike_price        → strike_price (float64)
  underlying_spot     → spot (float64)
  ltp + close         → ltp (COALESCE: ltp first, then intraday close fallback)
  volume              → volume (int64)
  oi                  → oi (int64)
  total_buy_qty       → bid_qty (int64)
  total_sell_qty      → ask_qty (int64)
  open                → open (float64)
  depth_bid1_qty      → bid1_qty (int64)    ◄ NEW: live L1 best-bid qty
  depth_bid1_price    → bid1_price (float64) ◄ NEW: live L1 best-bid price
  depth_ask1_qty      → ask1_qty (int64)    ◄ NEW: live L1 best-ask qty
  depth_ask1_price    → ask1_price (float64) ◄ NEW: live L1 best-ask price
  iv/delta/theta/gamma/vega → direct (float64)
  (computed)          → fut_price (near-FUT ltp back-filled per snap_time)
  (computed)          → dte ((expiry_date − trade_date).days)
  (computed)          → expiry_tier (TIER1≤15, TIER2 16–45, TIER3>45)
  (computed)          → gex (γ × OI × lot × spot² × 0.01 × dir; CE=+1, PE=−1)
  (computed)          → vex (OI × lot × vanna × spot / 1e6)
  (computed)          → cex (OI × lot × charm / 1e6)

Columns NOT written to Parquet:
  instrument_key  — used only to identify FUT rows internally
  close_price     — yesterday's settlement; wrong as ltp fallback
  last_trade_time — not needed by any analytics module
  rho             — not provided by Upstox / BQ feed

P2-1: vectorisation
--------------------
snap_time floor, dte → expiry_tier, and sqrt_t inside _compute_gex_vex_cex
are now fully vectorised using dt.floor(freq_str), pd.cut(), and np.where +
np.sqrt().  The old _assign_tier apply(lambda) loop and the math.sqrt
apply(lambda) loop are removed.  See _compute_snap_and_dates and
_compute_gex_vex_cex docstrings for details.

Entry point: process_and_write(df, duck_conn=None) → new watermark str | None
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from optdash.config import settings
from optdash.pipeline.writer import write_snap, parquet_path
from optdash.pipeline.watermark import to_str as wm_str

# GEX sign convention: CE=+1 (dealers net long gamma — pinning effect)
#                      PE=−1 (dealers net short gamma — directional pressure)
# Matches gex.py _classify_regime(): SUM(gex) > 0 → POSITIVE_CHOP.
_GEX_SIGN  = {"CE": +1, "PE": -1}
_VEX_SCALE = 1e6   # Rs M — matches vex_cex.py / 1e6 divisor
_CEX_SCALE = 1e6

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
]


def process_and_write(df: pd.DataFrame, duck_conn=None) -> str | None:
    """Main entry point: transform BQ DataFrame and write Parquet files.

    Parameters
    ----------
    df:         Raw BQ DataFrame from bq_client pull functions.
    duck_conn:  Optional live DuckDB connection. When provided and a new
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
    # underlyings are processed.  Each underlying that creates a new partition
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
            logger.error("processor: failed for {}: {}", underlying, e)
            raise

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
    # Distinct from bid_qty/ask_qty (cumulative day totals).
    # NULL when fewer than N levels exist (illiquid / far-OTM strikes).
    # Loop handles all 5 levels DRY-ly; qty/orders → Int64, price → float64.
    for lvl in range(1, 6):
        for side in ("bid", "ask"):
            src_q = f"depth_{side}{lvl}_qty"
            src_p = f"depth_{side}{lvl}_price"
            src_o = f"depth_{side}{lvl}_orders"
            df[f"{side}{lvl}_qty"]    = pd.to_numeric(df.get(src_q), errors="coerce").astype("Int64")
            df[f"{side}{lvl}_price"]  = pd.to_numeric(df.get(src_p), errors="coerce")
            df[f"{side}{lvl}_orders"] = pd.to_numeric(df.get(src_o), errors="coerce").astype("Int64")

    # open: intrabar open price (new field; absent in older BQ pulls → NaN)
    df["open"] = pd.to_numeric(df.get("open"), errors="coerce")

    # Greek + price casts to float64
    for col in ["strike_price", "iv", "delta", "theta", "gamma", "vega"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # OI / volume casts
    df["oi"]     = pd.to_numeric(df["oi"],     errors="coerce").astype("Int64")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("Int64")

    return df


def _compute_snap_and_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Compute snap_time, trade_date, dte, expiry_tier from normalised columns.

    P2-1 vectorisation changes
    --------------------------
    snap_time: dt.floor() is already vectorised.  The floor frequency is
    now derived from SCHEDULER_INTERVAL_SECONDS (same formula as
    scheduler._snap_time_str) so snap keys match DuckDB query keys at any
    configured tick interval.  The old hardcoded '5min' broke non-5-minute
    deployments silently.

    expiry_tier: replaced _assign_tier + .apply(lambda) with pd.cut().
    pd.cut() is a single C-level array pass (~2,500× faster on a full
    snap of 2,500 OPT rows).  Bins:
      dte in [0, 15]  → TIER1
      dte in (15, 45] → TIER2
      dte in (45, ∞)  → TIER3
      dte < 0 or NaN  → None  (FUT rows, expired strikes)
    """
    df = df.copy()

    # P2-1: derive floor frequency from config, not hardcoded '5min'.
    interval_mins = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)
    freq_str      = f"{interval_mins}min"

    df["snap_time"]  = df["_rt"].dt.floor(freq_str).dt.strftime("%H:%M")
    df["trade_date"] = df["_rt"].dt.date.astype(str)

    # dte: calendar days from trade_date to expiry_date
    df["_td"] = pd.to_datetime(df["trade_date"])
    df["_ed"] = pd.to_datetime(df["expiry_date"])
    df["dte"] = (df["_ed"] - df["_td"]).dt.days.astype("Int32")

    # P2-1: vectorised expiry_tier via pd.cut().
    # dte is Int32 (nullable int); cast to float64 for pd.cut (handles NA as NaN).
    dte_f = df["dte"].astype("float64")
    df["expiry_tier"] = pd.cut(
        dte_f,
        bins=[-0.001, 15.0, 45.0, float("inf")],
        labels=["TIER1", "TIER2", "TIER3"],
        right=True,   # intervals: (left, right] -- dte=15 → TIER1, dte=16 → TIER2
    ).astype(object)  # convert Categorical to object so NaN becomes None
    # dte < 0 (FUT / expired) fell outside all bins as NaN → already None.
    # Explicit: force negative-dte rows to None for clarity.
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

    P0-B: dte=0 (expiry-day settlement rows) are now excluded from the
    near-month candidate set.  On rollover day both the expiring contract
    (dte=0) and the new near-month (dte=7) are present in the feed.
    The old filter (dte >= 0) always selected the expiring row because
    sort_values("dte").first() picks the minimum.  The expiring contract's
    ltp is the settlement-converging price — not the rolled-forward price —
    producing wrong CoC, screener eff_ratio, and gate scores for the
    entire rollover session without any log entry.

    Fix: filter to dte > 0 so the expiry-day settlement contract is never
    selected as near-month.  If only dte=0 futures are available (rare
    edge case where the feed contains only the expiring contract), the
    warning below fires and fut_price remains NaN — the correct fallback
    rather than a stale settlement price.

    Merged left so OPT rows without a matching snap get NaN fut_price.
    """
    df = df.copy()
    df["fut_price"] = np.nan

    # instrument_type must already be normalised to 'FUT' (done in _normalize_types).
    # P0-B: dte > 0 — exclude expiry-day settlement rows from near-month selection.
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

    # Near-month: minimum dte (> 0) per snap_time
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

    GEX formula:
      γ × OI × lot_size × spot² × 0.01 × dir
      dir: CE=+1 (dealers long gamma, pinning), PE=−1 (dealers short gamma)

    VEX (Vanna Exposure) — approximate BSM:
      vanna ≈ δ × (1 − |δ|) / (spot × σ × √T)
      vex   = OI × lot × vanna × spot / 1e6

    CEX (Charm Exposure) — approximate BSM:
      charm ≈ −θ / (spot × σ × √T)
      cex   = OI × lot × charm / 1e6

    iv is percentage (e.g. 21.33) — divide by 100 to get decimal σ.
    dte=0 (expiry day): sqrt_t=NaN → vex/cex=NaN (GEX still valid).

    Units: vex and cex are stored in Parquet already divided by 1e6
    (i.e. in Rs M units).  vex_cex.py analytics query SUM(vex) directly
    without any further scaling — the /1e6 here and the SQL are the
    single point of scaling.  Do NOT add another /1e6 in analytics SQL.

    P0-3: vanna is clipped to [−VANNA_CLIP, +VANNA_CLIP] before the VEX
    multiplication.  Near-zero IV rows from the NSE feed produce a
    near-zero denominator, yielding vanna of 100–10,000+ that permanently
    corrupts VEX totals in Parquet.  See config.py VANNA_CLIP.

    P0-2: charm is clipped to [−CHARM_CLIP, +CHARM_CLIP] before the CEX
    multiplication.  Same failure mode as vanna.  See config.py CHARM_CLIP.

    P2-1: sqrt_t is now computed via np.where + np.sqrt (vectorised C-level
    array operation) instead of .apply(lambda x: math.sqrt(x) if x > 0
    else np.nan).  The math.sqrt apply loop ran in pure Python over all
    OPT rows (~2,500 per snap); np.where handles dte=0 (NaN) safely in
    a single SIMD pass.
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

    # GEX
    spot_sq      = opts["spot"] ** 2
    opts["gex"]  = (
        opts["gamma"] * opts["oi"] * lot_size * spot_sq * 0.01 * opts["_dir"]
    )

    # P2-1: vectorised sqrt_t via np.where + np.sqrt.
    # dte=0 (expiry day) → sqrt_t=NaN so vex/cex remain NaN for expiry rows
    # (GEX is still computed above -- gamma is valid on expiry day).
    # dte is Int32 (nullable); astype float64 converts NA → NaN automatically.
    dte_f  = opts["dte"].astype("float64")
    sigma  = opts["iv"] / 100.0
    sqrt_t = np.where(dte_f > 0, np.sqrt(dte_f / 365.0), np.nan)
    denom  = pd.Series(
        opts["spot"].values * sigma.values * sqrt_t,
        index=opts.index,
    ).replace(0, np.nan)

    # VEX — stored in Rs M (already divided by 1e6 = _VEX_SCALE).
    # vex_cex.py queries SUM(vex) directly; no further /1e6 in SQL.
    vanna        = opts["delta"] * (1.0 - opts["delta"].abs()) / denom
    vanna        = vanna.clip(-settings.VANNA_CLIP, settings.VANNA_CLIP)  # P0-3
    opts["vex"]  = (opts["oi"] * lot_size * vanna * opts["spot"]) / _VEX_SCALE

    # CEX — stored in Rs M (already divided by 1e6 = _CEX_SCALE).
    # vex_cex.py queries SUM(cex) directly; no further /1e6 in SQL.
    charm        = -opts["theta"] / denom
    charm        = charm.clip(-settings.CHARM_CLIP, settings.CHARM_CLIP)  # P0-2
    opts["cex"]  = (opts["oi"] * lot_size * charm) / _CEX_SCALE

    # Explicit float64 cast avoids FutureWarning about setting incompatible
    # dtype (opts may contain pd.NA from nullable-int OI arithmetic).
    for col in ("gex", "vex", "cex"):
        df.loc[mask, col] = opts[col].astype("float64").values
    return df


def _write_trade_date(
    underlying:  str,
    trade_date:  str,
    td_df:       pd.DataFrame,
    duck_conn,
    _refreshed:  set[str],
) -> None:
    """Write all snaps for one (underlying, trade_date) pair.

    Each snap is written individually via write_snap() which handles
    read-merge-rewrite atomically under FileLock.

    P0-A: refresh_views() is called at most once per trade_date across
    all underlyings processed in the same batch.  _refreshed is a set
    passed in from process_and_write(); the first underlying that creates
    a new partition directory triggers the refresh and adds trade_date to
    the set.  Subsequent underlyings for the same new date skip the call,
    preventing 4 redundant DROP+CREATE+validate cycles that would each
    block analytics for ~50-200ms.

    The new_partition flag is evaluated before write_snap() so it
    correctly reflects whether the directory existed before this call
    (write_snap calls mkdir internally).
    """
    data_root     = Path(settings.DATA_ROOT)
    path          = parquet_path(data_root, trade_date, underlying)
    # P0-A: capture existence state BEFORE write_snap() creates the directory.
    new_partition = not path.parent.exists()

    # Select output columns in PARQUET_SCHEMA order; fill any absent with NaN
    out_df = td_df.reindex(columns=_OUT_COLS)

    for snap_time, snap_df in out_df.groupby("snap_time"):
        write_snap(data_root, trade_date, underlying, snap_df.reset_index(drop=True))

    n_snaps = out_df["snap_time"].nunique()
    logger.debug(
        "processor: {}/{} — {} snaps ({} rows)",
        trade_date, underlying, n_snaps, len(out_df),
    )

    # P0-A: only refresh once per trade_date per batch, regardless of how
    # many underlyings are written in this call.  RLock is reentrant so the
    # call is safe even if the scheduler already holds _view_lock.
    if new_partition and duck_conn is not None and trade_date not in _refreshed:
        from optdash.pipeline.duckdb_gateway import refresh_views
        try:
            refresh_views(duck_conn)
            logger.info("DuckDB view refreshed (new partition: {})", trade_date)
            _refreshed.add(trade_date)
        except Exception as e:
            logger.error("refresh_views after new partition failed: {}", e)
