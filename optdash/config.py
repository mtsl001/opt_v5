"""Central settings - all tuneable constants in one place."""
import re
from datetime import date
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # -- Paths
    DATA_ROOT:       Path = Path("./data")
    JOURNAL_DB_PATH: Path = Path("./data/journal.db")

    # -- API
    API_HOST:     str       = "0.0.0.0"
    API_PORT:     int       = 8000
    LOG_LEVEL:    str       = "INFO"
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    # Fix-P1-5: UNDERLYINGS is declared before DEFAULT_UNDERLYING so the
    # _check_default_underlying validator can reference it via info.data.
    # All supported underlyings — single source of truth.
    # Add / remove here; every loop in the system reads this list.
    # SENSEX removed (BSE index — not tracked).
    UNDERLYINGS: list[str] = [
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"
    ]

    # Default underlying for endpoints that take an optional underlying param.
    # Override via DEFAULT_UNDERLYING= in .env. Must be a member of UNDERLYINGS.
    DEFAULT_UNDERLYING: str = "NIFTY"

    @field_validator("DEFAULT_UNDERLYING")
    @classmethod
    def _check_default_underlying(cls, v: str, info) -> str:
        underlyings = (info.data or {}).get("UNDERLYINGS", [])
        if underlyings and v not in underlyings:
            raise ValueError(
                f"DEFAULT_UNDERLYING={v!r} is not in UNDERLYINGS={underlyings}"
            )
        return v

    # -- Scheduler
    SCHEDULER_INTERVAL_SECONDS: int = 60   # 1-min tick (matches upxtx feed cadence)
    WS_INTERVAL_SECONDS:        int = 5    # WebSocket push cadence

    # M-5: WS_INTERVAL_SECONDS must be:
    #   > 0                          -- asyncio.sleep(0) is a tight loop that
    #                                   saturates the event loop, starving the
    #                                   scheduler tick and all HTTP handlers.
    #   < SCHEDULER_INTERVAL_SECONDS -- WS must push faster than the scheduler
    #                                   refreshes data; equal or greater means
    #                                   the live dashboard shows stale state.
    # Validator is placed after SCHEDULER_INTERVAL_SECONDS so info.data
    # contains the scheduler value when this check runs (pydantic validates
    # in field declaration order).
    @field_validator("WS_INTERVAL_SECONDS")
    @classmethod
    def _check_ws_interval(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(
                f"WS_INTERVAL_SECONDS must be > 0, got {v}. "
                "Setting to 0 creates an asyncio.sleep(0) tight loop that "
                "starves the scheduler tick and all HTTP/WS handlers."
            )
        scheduler_secs = (info.data or {}).get("SCHEDULER_INTERVAL_SECONDS", 300)
        if v >= scheduler_secs:
            raise ValueError(
                f"WS_INTERVAL_SECONDS={v} must be less than "
                f"SCHEDULER_INTERVAL_SECONDS={scheduler_secs}. "
                "The WebSocket push cadence must be faster than the scheduler "
                "refresh rate, otherwise the live dashboard shows stale data."
            )
        return v

    MARKET_OPEN:          str = "09:15"
    MARKET_CLOSE:         str = "15:30"
    EOD_FORCE_CLOSE_TIME: str = "15:20"
    EOD_SWEEP_TIME:       str = "15:25"

    # Fix-P1-7: all time-string fields validated to strict HH:MM (zero-padded).
    # Without this, "9:5" passes pydantic but breaks strptime and snap_time
    # string comparisons throughout the codebase.
    @field_validator(
        "MARKET_OPEN", "MARKET_CLOSE", "EOD_FORCE_CLOSE_TIME", "EOD_SWEEP_TIME",
        "SESSION_OPENING_END", "SESSION_MIDDAY_START", "SESSION_MIDDAY_END",
        "SESSION_CLOSING_START", "DEALER_OCLOCK_START",
        mode="before",
    )
    @classmethod
    def _check_hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}", str(v)):
            raise ValueError(f"Expected HH:MM format (e.g. '09:15'), got {v!r}")
        return v

    # NSE market holidays — scheduler skips all ticks on these dates so no
    # DuckDB scans or empty-data log noise accumulate on non-trading days.
    # Format: YYYY-MM-DD strings.
    # Set in .env as a JSON array (pydantic-settings parses it automatically):
    #   MARKET_HOLIDAYS=["2026-03-14","2026-04-14","2026-04-18"]
    # The scheduler reads this via settings.MARKET_HOLIDAYS; a missing / empty
    # list simply disables the holiday skip without any error.
    #
    # 2026 NSE TRADING holidays (dates where NSE is closed).
    # Source: Upstox market-holidays API (verified Mar 2026).
    # Excluded: settlement-only dates (Feb-19, Mar-19, Apr-01, Aug-26) and
    # Diwali Muhurat session (Nov-08) where NSE trades with modified hours.
    MARKET_HOLIDAYS: list[str] = [
        "2026-01-26",  # Republic Day
        "2026-03-03",  # Holi
        "2026-03-26",  # Ram Navami
        "2026-03-31",  # Mahavir Jayanti
        "2026-04-03",  # Good Friday
        "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
        "2026-05-01",  # Maharashtra Day
        "2026-05-28",  # Bakri Id / Eid-ul-Adha
        "2026-06-26",  # Muharram
        "2026-09-14",  # Ganesh Chaturthi
        "2026-10-02",  # Gandhi Jayanti
        "2026-10-20",  # Dussehra
        "2026-11-10",  # Diwali-Balipratipada
        "2026-11-24",  # Guru Nanak Jayanti
        "2026-12-25",  # Christmas
    ]

    # -- Per-Underlying Market Metadata
    # Fix-P1-6: inner-type annotations added to all per-underlying dicts so
    # pydantic validates element types when .env overrides supply JSON.
    # A single _check_underlying_coverage validator (below) confirms that every
    # known underlying has an entry in each dict — a missing key would cause a
    # silent None return and a TypeError (e.g. None * lot_size) mid-trade.

    # Lot sizes (NSE-defined - last verified Mar 2026; review on contract rollover).
    LOT_SIZES: dict[str, int] = {
        "NIFTY": 75, "BANKNIFTY": 15, "FINNIFTY": 40,
        "MIDCPNIFTY": 120, "NIFTYNXT50": 10,
    }
    # Strike price intervals in points between adjacent strikes.
    STRIKE_INTERVALS: dict[str, int] = {
        "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50,
        "MIDCPNIFTY": 25, "NIFTYNXT50": 50,
    }
    # Weekly expiry weekday per underlying (Python weekday: 0=Mon ... 4=Fri).
    # BANKNIFTY  -> Wednesday(2) [NSE moved from Thursday, effective Sep 2023],
    # FINNIFTY   -> Tuesday(1),
    # MIDCPNIFTY -> Monday(0),
    # NIFTYNXT50 -> Friday(4),
    # NIFTY      -> Thursday(3).
    EXPIRY_WEEKDAY: dict[str, int] = {
        "NIFTY": 3, "BANKNIFTY": 2,
        "FINNIFTY": 1,
        "MIDCPNIFTY": 0,
        "NIFTYNXT50": 4,
    }

    # -- Parquet / DuckDB
    PARQUET_DATE_COL:       str  = "trade_date"
    PARQUET_SNAP_COL:       str  = "snap_time"
    PARQUET_UNDERLYING_COL: str  = "underlying"
    EXPIRY_TIERS: dict = {"TIER1": 0, "TIER2": 1, "TIER3": 2}
    # Raw Parquet files (pre-enrichment) are purged after this many calendar days.
    # Processed Parquets are kept permanently as the audit trail.
    RAW_PARQUET_RETENTION_DAYS: int = 3
    # Rolling lookback window for the DuckDB options_data view.
    # Only the last N calendar days of data/processed/ are included in the
    # view, bounding scan cost regardless of how long the service runs.
    # 5 = full Mon-Fri week; increase for longer historical analytics.
    DUCK_VIEW_LOOKBACK_DAYS: int = 5

    # P2-14: FileLock timeout for the Parquet read-merge-write cycle in
    # writer.py.  If the lock cannot be acquired within this many seconds a
    # Timeout exception is raised, letting the scheduler log and skip the
    # tick rather than stacking up writers indefinitely.
    #
    # Default 30 preserves the previous hardcoded value.
    # Tune per deployment:
    #   WRITER_LOCK_TIMEOUT_SECONDS=60   # slow disk / NFS mount / large files
    #   WRITER_LOCK_TIMEOUT_SECONDS=10   # small VPS — fail fast
    WRITER_LOCK_TIMEOUT_SECONDS: int = 30

    # P2-3: DuckDB engine settings — moved from hardcoded PRAGMAs in
    # duckdb_gateway.startup() to config so they can be tuned per deployment
    # via .env without a code change.
    #
    # DUCKDB_THREADS: number of threads DuckDB uses for parallel query
    # execution.  Set to 0 to let DuckDB auto-detect the optimal count for
    # the host CPU (recommended on multi-core servers).  Default 4 preserves
    # the previous hardcoded value.
    #   Override: DUCKDB_THREADS=8   (8-core server)
    #             DUCKDB_THREADS=0   (auto-detect)
    #             DUCKDB_THREADS=1   (single-core VPS / container)
    #
    # DUCKDB_MEMORY_LIMIT: maximum memory DuckDB may use before spilling to
    # disk.  Should be set to ~75% of available RAM to leave headroom for
    # Python/FastAPI heap and the BQ/Parquet I/O buffers.
    # On an 8 GB server: 6GB.  On a 1 GB VPS: 512MB.
    # Default '2GB' preserves the previous hardcoded value.
    #   Override: DUCKDB_MEMORY_LIMIT=6GB
    #             DUCKDB_MEMORY_LIMIT=512MB
    DUCKDB_THREADS:       int = 4
    DUCKDB_MEMORY_LIMIT:  str = "2GB"

    # ── BigQuery connection ────────────────────────────────────────────────────
    # BQ_TABLE_ARCHIVE (upxtx_ar): full historical archive → used by backfill.
    # BQ_TABLE_LIVE    (upxtx):    rolling live feed (OPTIDX + FUTIDX + FUTSTK)
    #                              → used by gap fill + incremental.
    # BQ_TABLE_VIX     (upxtx_vix): India VIX + 30 index values (1-min feed)
    #                              → side-pull for VIX watermark.
    # upxtx is synced to upxtx_ar at 06:35 IST daily; upxtx then becomes empty
    # until NSE market opens at 09:15 IST.
    BQ_PROJECT:          str  = "universal-ion-437606-b7"
    BQ_DATASET:          str  = "bgquery"
    BQ_TABLE_ARCHIVE:    str  = "upxtx_ar"
    BQ_TABLE_LIVE:       str  = "upxtx"
    BQ_TABLE_VIX:        str  = "upxtx_vix"
    BQ_CREDENTIALS_PATH: Path = Path("service-account.json")

    # ── Pipeline / Watermark ──────────────────────────────────────────────────
    # WATERMARK_PATH: atomic JSON file tracking the last successfully processed
    # record_time so incremental and gap-fill pulls do not re-process rows.
    # BACKFILL_START_DATE: first trading day to pull from upxtx_ar.
    # BACKFILL_END_DATE:   leave empty ("") to auto-set to yesterday at runtime.
    # ENABLE_BACKFILL:     set False to skip backfill on startup (e.g. after
    #                      first full load, or during development).
    # ENABLE_GAP_FILL:     set False to skip gap fill on startup (e.g. during
    #                      development without BQ credentials, or when data is
    #                      already fully current).  Mirror of ENABLE_BACKFILL --
    #                      set both=false together for local dev without BQ.
    WATERMARK_PATH:      Path = Path("./data/watermark.json")
    BACKFILL_START_DATE: str  = "2026-02-17"
    BACKFILL_END_DATE:   str  = ""
    ENABLE_BACKFILL:     bool = True
    ENABLE_GAP_FILL:     bool = True

    # P1-4: validate BACKFILL_START_DATE as a strict ISO date (YYYY-MM-DD) at
    # Settings construction time.  Without this, a bad format (e.g. "17-02-2026"
    # from a misconfigured .env) passes pydantic silently, lets the process
    # start and serve API requests, then crashes the pipeline on the first
    # scheduler tick when watermark._initial_watermark() calls
    # date.fromisoformat() -- leaving the app in a dead-pipeline state.
    # Fail-fast: ValidationError is raised before uvicorn finishes startup.
    @field_validator("BACKFILL_START_DATE")
    @classmethod
    def _check_backfill_start_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"BACKFILL_START_DATE={v!r} is not a valid ISO date. "
                "Expected format: YYYY-MM-DD (e.g. '2026-02-17')."
            )
        return v

    # BQ columns fetched in every pull. processor.py maps these to PARQUET_SCHEMA.
    #
    # Excluded intentionally:
    #   close_price        — yesterday's settlement price; wrong as ltp fallback.
    #   last_trade_time    — not needed by any analytics module.
    #   high/low           — not used by any gate/screener (open kept for intrabar ref).
    #   pcr                — computed from OI sums by pcr.py; not stored raw.
    #   rho                — not provided by Upstox API.
    BQ_SELECT_COLS: list[str] = [
        "record_time",
        "underlying",
        "instrument_type",    # OPTIDX / FUTIDX → normalised to OPT / FUT by processor
        "instrument_key",     # used to identify FUT rows in processor; not written to Parquet
        "option_type",        # CE / PE; NULL for futures rows
        "expiry_date",        # M/D/YYYY in BQ → normalised to YYYY-MM-DD by processor
        "strike_price",
        "underlying_spot",    # → spot column in Parquet
        "open",               # intrabar open — stored as-is for future OHLC analytics
        "close",              # intraday running close → effective_ltp fallback (not close_price)
        "ltp",                # primary price
        "volume",
        "oi",
        "total_buy_qty",      # → bid_qty (cumulative day buy flow)
        "total_sell_qty",     # → ask_qty (cumulative day sell flow)
        # 5-level order book depth — bid side (L1 = best bid, L5 = deepest).
        # qty/price are the primary analytics inputs; orders = number of orders at that level.
        # NULL when fewer than N levels exist (illiquid / far-OTM strikes).
        "depth_bid1_qty", "depth_bid1_price", "depth_bid1_orders",
        "depth_bid2_qty", "depth_bid2_price", "depth_bid2_orders",
        "depth_bid3_qty", "depth_bid3_price", "depth_bid3_orders",
        "depth_bid4_qty", "depth_bid4_price", "depth_bid4_orders",
        "depth_bid5_qty", "depth_bid5_price", "depth_bid5_orders",
        # 5-level order book depth — ask side (L1 = best ask, L5 = deepest).
        "depth_ask1_qty", "depth_ask1_price", "depth_ask1_orders",
        "depth_ask2_qty", "depth_ask2_price", "depth_ask2_orders",
        "depth_ask3_qty", "depth_ask3_price", "depth_ask3_orders",
        "depth_ask4_qty", "depth_ask4_price", "depth_ask4_orders",
        "depth_ask5_qty", "depth_ask5_price", "depth_ask5_orders",
        "iv",                 # percentage, e.g. 21.33 (NOT decimal 0.2133)
        "delta",
        "theta",
        "gamma",
        "vega",
    ]

    # -- GEX
    GEX_NEAR_WEEKS:        int   = 2
    GEX_DECLINE_THRESHOLD: float = 0.70  # 70% of peak -> declining
    GEX_SCALING:           float = 1e9   # raw / 1B -> display in Rs B
    GEX_MOMENTUM_SNAPS:    int   = 5     # trailing avg window (snaps); 5 snaps = 5 min at 1-min cadence

    # -- CoC / V_CoC
    VCOC_BULL_THRESHOLD:     float = 10.0
    VCOC_BEAR_THRESHOLD:     float = -10.0
    VCOC_SPIKE_EXPIRY_SNAPS: int   = 3
    COC_DISCOUNT_THRESHOLD:  float = -5.0

    # -- CoC / V_CoC — annualized percentage thresholds (CoC-1)
    # These replace the absolute point thresholds above once calibrated.
    # Unit: annualized % (e.g. 5.0 = 5% annualized CoC velocity)
    # Formula used: (delta_coc / spot) * (365 / dte) * 100
    VCOC_BULL_THRESHOLD_PCT:     float = 5.0
    VCOC_BEAR_THRESHOLD_PCT:     float = -5.0
    COC_DISCOUNT_THRESHOLD_PCT:  float = -2.0

    # -- CoC fair-value adjustment (CoC-2 — used from Commit 3 onwards)
    RISK_FREE_RATE: float = 0.065   # 91-day T-bill; update quarterly
    DIVIDEND_YIELD: dict[str, float] = {
        "NIFTY": 0.012, "BANKNIFTY": 0.008, "FINNIFTY": 0.010,
        "MIDCPNIFTY": 0.006, "NIFTYNXT50": 0.006,
    }

    # -- VEX / CEX
    # VEX_BULL_THRESHOLD: global fallback for unknown underlyings only.
    # For all known underlyings, VEX_THRESHOLDS takes precedence.
    VEX_BULL_THRESHOLD:  float = 0.0
    # Per-underlying VEX magnitude thresholds (Rs M).
    # Scaled to index liquidity: liquid large-caps (NIFTY/BANKNIFTY) need a
    # higher bar to filter noise; illiquid underlyings need a lower bar.
    VEX_THRESHOLDS: dict[str, float] = {
        "NIFTY": 0.50, "BANKNIFTY": 0.50, "FINNIFTY": 0.25,
        "MIDCPNIFTY": 0.15, "NIFTYNXT50": 0.15,
    }
    CEX_STRONG_BID:    float = 20.0
    CEX_BID:           float = 5.0
    CEX_PRESSURE:      float = -20.0
    DEALER_OCLOCK_DTE: int   = 1

    # M-4: DEALER_OCLOCK_START is intentionally EARLIER than SESSION_CLOSING_START
    # (14:00 vs 14:30).  On DTE=1, dealer delta-hedging (charm flow) intensifies
    # from ~14:00, before the session boundary at 14:30.  Gate condition C10
    # uses DEALER_OCLOCK_START independently from C8 (SESSION_CLOSING_START) so
    # the 30-minute overlap window (14:00–14:30) correctly receives -1 gate point
    # (C10 fails: Dealer O'Clock active) while C8 stays +1 (still AFTERNOON
    # session, not MIDDAY_CHOP).
    #
    # !! DO NOT align these two values without re-calibrating the C8+C10
    # interaction in environment.py.  Aligning them to 14:30 silently removes
    # 30 minutes of DTE=1 charm-distortion protection with no test failure or
    # lint warning. !!
    DEALER_OCLOCK_START: str = "14:00"

    # Per-underlying CEX magnitude thresholds (Rs M) - scaled to index liquidity.
    CEX_CHARM_THRESHOLD: dict[str, float] = {
        "NIFTY": 20.0, "BANKNIFTY": 20.0, "FINNIFTY": 10.0,
        "MIDCPNIFTY": 5.0, "NIFTYNXT50": 5.0,
    }
    CEX_VANNA_THRESHOLD: dict[str, float] = {
        "NIFTY": 12.0, "BANKNIFTY": 12.0, "FINNIFTY": 6.0,
        "MIDCPNIFTY": 3.0, "NIFTYNXT50": 3.0,
    }

    # -- PCR divergence thresholds (asymmetric by design)
    # PCR divergence = pcr_vol - pcr_oi.
    # Positive divergence: volume PCR > OI PCR → retail accumulating puts faster
    #   than OI is building → fear/panic into puts (bearish retail sentiment).
    # Negative divergence: volume PCR < OI PCR → retail accumulating calls faster
    #   than OI is building → greed/panic into calls (bullish retail sentiment).
    #
    # Asymmetry rationale: fear spikes faster and harder than greed. Retail panic
    # into puts (VIX-driven, stop-loss driven) is a sharper, more reliable signal
    # than retail call-buying (which is diffuse and driven by FOMO). Therefore the
    # bull threshold (put-panic confirmation) is set higher (+0.25) than the bear
    # threshold (-0.20) in absolute terms.
    PCR_DIV_BULL_THRESHOLD: float = 0.25
    PCR_DIV_BEAR_THRESHOLD: float = -0.20

    # Rolling Z-score window for PCR divergence normalization (PCR-3).
    # Unit: number of intraday snaps. At 1-min cadence: 30 snaps = 30-min rolling window.
    # Increase for smoother Z-score; decrease for more reactive signal.
    PCR_ZSCORE_WINDOW: int = 30

    # PCR divergence trend lookback (PCR-4). Number of prior snaps for div_trend.
    # At 1-min cadence: 3 snaps = 3-min trend window.
    PCR_TREND_SNAPS: int = 3

    # PCR smoothed_obi trailing window (snaps).
    # At 1-min cadence: 10 snaps = 10-min smoothing (was 3-snap/15-min at 5-min cadence).
    # Increasing this smooths out single-strike L1 depth noise at the cost of
    # slightly delayed OBI reversals. 10 is a practical midpoint.
    PCR_OBI_SMOOTH_SNAPS: int = 10

    # -- OBI
    OBI_THRESHOLD: float = 0.10
    # Futures OBI threshold for Gate Condition 3 (sellers dominant).
    FUT_OBI_BEAR_THRESHOLD: dict[str, float] = {
        "NIFTY": -0.20, "BANKNIFTY": -0.20,
        "FINNIFTY": -0.25,
        "MIDCPNIFTY": -0.35,
        "NIFTYNXT50": -0.30,
    }

    # -- IV
    IV_LOOKBACK_DAYS: int = 252
    IVR_LOW_PERCENTILE:      float = 0.01   # IV-2: 1st percentile for IVR low anchor
    IVR_HIGH_PERCENTILE:     float = 0.99   # IV-2: 99th percentile for IVR high anchor
    TS_CONTANGO_SLOPE:       float =  0.30  # IV-3: annualized IV units per √day
    TS_BACKWARDATION_SLOPE:  float = -0.30
    VRP_OVERPRICED_THRESHOLD: float =  2.0   # IV-4: IV exceeds HV20 by 2 pts → sellers favored
    VRP_UNDERPRICED_THRESHOLD: float =  0.0   # IV-4: IV below HV20 → buyers favored

    # -- India VIX (new section)
    VIX_HIGH_THRESHOLD:      float = 20.0   # IV-6: VIX above this = HIGH regime
    VIX_HIGH_IVP_THRESHOLD:  float = 35.0   # IV-6b: IVP threshold when VIX is HIGH
    # IV crush HIGH severity Vega threshold -- per underlying.
    # Unit: option price points per 1% IV change (confirmed from screener
    # normalisation: vega / ltp / 0.50). NOT raw BSM decimal Vega.
    # Calibrated against typical ATM Vega for near-expiry options:
    #   NIFTY      ~8-15  pts (ATM, 3-7 DTE)   -> threshold 15
    #   BANKNIFTY  ~30-70 pts (ATM, 5-10 DTE)  -> threshold 30
    #   FINNIFTY   ~6-12  pts                   -> threshold 10
    #   MIDCPNIFTY ~3-8   pts                   -> threshold  8
    #   NIFTYNXT50 ~3-8   pts                   -> threshold  8
    IV_CRUSH_HIGH_VEGA: dict[str, float] = {
        "NIFTY": 15.0, "BANKNIFTY": 30.0, "FINNIFTY": 10.0,
        "MIDCPNIFTY": 8.0, "NIFTYNXT50": 8.0,
    }

    # Fix-P1-6 (continued): validator ensuring all per-underlying dicts have
    # complete key coverage for every underlying in UNDERLYINGS.
    @field_validator(
        "LOT_SIZES", "STRIKE_INTERVALS", "EXPIRY_WEEKDAY",
        "VEX_THRESHOLDS", "CEX_CHARM_THRESHOLD", "CEX_VANNA_THRESHOLD",
        "FUT_OBI_BEAR_THRESHOLD", "IV_CRUSH_HIGH_VEGA", "DIVIDEND_YIELD",
    )
    @classmethod
    def _check_underlying_coverage(cls, v: dict, info) -> dict:
        underlyings = (info.data or {}).get("UNDERLYINGS", [])
        missing = set(underlyings) - set(v.keys())
        if missing:
            raise ValueError(
                f"{info.field_name} is missing keys for underlyings: {missing}"
            )
        return v

    # -- Strike Screener
    SCREENER_TOP_N:             int   = 20
    SCREENER_MAX_MONEYNESS_PCT: float = 5.0
    SCREENER_MIN_LIQUIDITY_CR:  float = 0.5
    SCREENER_MIN_DELTA:         float = 0.10
    SCREENER_MAX_DELTA:         float = 0.50
    SCREENER_MIN_EFF_RATIO:     float = 0.10

    # -- S_score Weights
    W_DELTA:      float = 4.0
    W_EFF_RATIO:  float = 4.0
    W_LIQUIDITY:  float = 3.0
    W_IV:         float = 2.0
    W_THETA:      float = 2.0
    W_GAMMA:      float = 1.0
    W_VEGA:       float = 1.0
    # Star thresholds calibrated against the 0-~150 S_score scale.
    STAR_4_THRESHOLD: float = 100.0  # >=67% of max - excellent
    STAR_3_THRESHOLD: float =  80.0  # >=53% of max - good
    STAR_2_THRESHOLD: float =  60.0  # >=40% of max - acceptable

    # -- Environment Gate
    GATE_GO_THRESHOLD:   int = 7
    GATE_WAIT_THRESHOLD: int = 5
    GATE_MAX_SCORE:      int = 11

    # -- Session Boundaries
    SESSION_OPENING_END:   str = "10:15"
    SESSION_MIDDAY_START:  str = "11:30"
    SESSION_MIDDAY_END:    str = "13:00"
    # SESSION_CLOSING_START marks the AFTERNOON→CLOSING_CRUSH boundary (C8).
    # See DEALER_OCLOCK_START below for the related DTE=1 C10 boundary which
    # is intentionally set 30 minutes earlier at 14:00.
    SESSION_CLOSING_START: str = "14:30"

    # -- AI Recommender
    PREFLIGHT_MIN_GATE_SCORE:      int   = 5
    PREFLIGHT_MIN_CONFIDENCE:      int   = 50
    PREFLIGHT_MAX_THETA_RATIO:     float = 0.03
    PREFLIGHT_MAX_PAIN_PROXIMITY:  float = 0.005
    PREFLIGHT_MIN_SSCORE:          float = 60.0
    PREFLIGHT_DTE1_MIN_GATE:       int   = 7
    PREFLIGHT_DTE1_MIN_CONFIDENCE: int   = 65

    AI_SL_PCT:           float = 0.35
    AI_TARGET_MULT:      float = 1.50
    AI_EXPIRY_MAX_SNAPS: int   = 3

    @field_validator("AI_SL_PCT")
    @classmethod
    def _check_sl_pct(cls, v: float) -> float:
        if not (0.0 < v < 1.0):
            raise ValueError(f"AI_SL_PCT must be in (0, 1), got {v}")
        return v

    @field_validator("AI_TARGET_MULT")
    @classmethod
    def _check_target_mult(cls, v: float) -> float:
        if v <= 1.0:
            raise ValueError(f"AI_TARGET_MULT must be > 1.0, got {v}")
        return v

    TRAILING_STOP_ACTIVATION:   float = 0.20
    # Issue-2: trail-down percentage once trailing stop is activated.
    # 0.10 = trailing SL set at peak_ltp × (1 − 0.10) = peak × 0.90.
    # Override via TRAILING_STOP_TRAIL_PCT= in .env for strategy tuning.
    TRAILING_STOP_TRAIL_PCT:    float = 0.10
    GATE_SUSTAINED_NO_GO_SNAPS: int   = 2

    SESSION_MIDDAY_CONFIDENCE_PENALTY: int = 10
    SESSION_CLOSING_CONFIDENCE_CAP:    int = 60

    # -- Pipeline Greeks Safety
    # P0-3: cap for vanna values before VEX multiplication in processor.py.
    # Near-zero IV rows from the NSE feed produce a near-zero denominator in
    # the vanna approximation (δ × (1−|δ|) / (spot × σ × √T)), yielding
    # vanna of 100–10,000+.  These corrupt VEX totals permanently in Parquet.
    # Calibration: normal ATM vanna at 20% IV, 1-DTE ≈ 0.001; clip at 50
    # is ~50,000× the normal range — catches all noise without ever clipping
    # valid data.  Override via VANNA_CLIP= in .env if needed.
    VANNA_CLIP: float = 50.0

    # P0-2: cap for charm values before CEX multiplication in processor.py.
    # Same failure mode as VANNA_CLIP: near-zero IV rows produce a near-zero
    # denominator in the charm approximation (−θ / (spot × σ × √T)), yielding
    # charm of ±10,000–100,000 that is written permanently to Parquet and
    # dominates all CEX SUM() totals in vex_cex.py analytics.
    # Calibration: normal ATM charm at 20% IV, 1-DTE ≈ 0.001–0.01; clip at 50
    # is ~5,000× the normal range — symmetric with VANNA_CLIP.
    # Override via CHARM_CLIP= in .env if needed.
    CHARM_CLIP: float = 50.0

    # ── Computed BQ table FQNs (read-only properties) ────────────────────────
    # Placed after all field_validators to comply with pydantic-settings
    # class layout requirements. Override BQ_PROJECT / BQ_DATASET in .env
    # and these automatically reflect the change.
    @property
    def BQ_FQN_ARCHIVE(self) -> str:
        """Fully-qualified BQ table for historical archive (upxtx_ar)."""
        return f"{self.BQ_PROJECT}.{self.BQ_DATASET}.{self.BQ_TABLE_ARCHIVE}"

    @property
    def BQ_FQN_LIVE(self) -> str:
        """Fully-qualified BQ table for rolling live feed (upxtx)."""
        return f"{self.BQ_PROJECT}.{self.BQ_DATASET}.{self.BQ_TABLE_LIVE}"

    @property
    def BQ_FQN_VIX(self) -> str:
        """Fully-qualified BQ table for VIX + index feed (upxtx_vix)."""
        return f"{self.BQ_PROJECT}.{self.BQ_DATASET}.{self.BQ_TABLE_VIX}"


settings = Settings()
