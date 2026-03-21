"""bq_client.py — BigQuery auth + pull functions with retry.

Singleton pattern: _bq_client created once on first call, reused forever.
Retry: tenacity exponential backoff — 4 attempts, 4↑60s wait.

table_fqn parameter controls which BQ table is queried:
  settings.BQ_FQN_ARCHIVE  (upxtx_ar) — full history → backfill
  settings.BQ_FQN_LIVE     (upxtx)    — rolling live  → gap fill + incremental

P1-3: IST/UTC seam
------------------
record_time in BQ is UTC-labelled but numerically IST (i.e. a 09:30 IST
trade is stored as 09:30 UTC, not 04:00 UTC).  Watermark strings are
stored as naive IST wall-clock values by watermark.py.

These two IST-numeric values cancel in TIMESTAMP('{watermark}') comparisons
today.  The risk is at re-ingestion: if BQ record_time is ever corrected to
true UTC, pull_incremental would silently skip 5h30m of data per tick (or
re-pull 5h30m of duplicates) with no error or log entry.

_assert_watermark_format() is called in pull_incremental as a tripwire:
it raises ValueError immediately if the watermark is tz-aware ('+05:30'
suffix) or otherwise not a bare 'YYYY-MM-DD HH:MM:SS' string.  This makes
a future UTC migration fail loudly at the seam rather than silently
corrupting the watermark advance logic.

Processor strips tz-info without conversion — see processor._strip_tz().

P2-9: singleton thread-safety
------------------------------
get_bq_client() acquires _bq_client_lock before the None check (classic
double-checked locking).  This prevents a TOCTOU race when two startup
tasks (run_backfill + run_gap_fill) are dispatched concurrently via
asyncio.to_thread().  The lock is only contended at first call; all
subsequent calls take a zero-overhead fast path (check before acquiring).
"""
from __future__ import annotations

import re
import threading

import pandas as pd
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from optdash.config import settings

_bq_client      = None              # module-level singleton
_bq_client_lock = threading.Lock()  # P2-9: guards singleton initialisation

# P1-3: BQ record_time is UTC-labelled but numerically IST.
# Watermark strings are naive IST wall-clock.  Both sides are IST-numeric,
# which makes the TIMESTAMP() comparison correct TODAY but fragile if BQ
# is ever re-ingested with true UTC values.  This constant makes the
# assumption explicit and grep-able; _assert_watermark_format() is the
# runtime tripwire that catches a misrouted tz-aware or UTC-corrected string.
_IST_AS_UTC_OFFSET_WARNING = (
    "BQ record_time is UTC-labelled / IST-numeric. "
    "Watermark is naive IST. Both sides cancel in TIMESTAMP() today. "
    "If BQ is corrected to true UTC, update watermark.py and this module together."
)

# Bare naive datetime pattern -- no tz suffix, no 'T' separator.
_NAIVE_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _assert_watermark_format(watermark: str) -> None:
    """Raise ValueError if watermark is not a bare 'YYYY-MM-DD HH:MM:SS' string.

    This is a tripwire for the IST/UTC seam (P1-3).  All current callers
    (watermark.py) produce the correct format; this guard catches:
      - A tz-aware string accidentally passed in ('+05:30' or 'Z' suffix).
      - A UTC-corrected value that would silently shift the watermark by 5h30m.
    Fires loudly at the data-ingestion seam rather than silently
    corrupting incremental pulls.
    """
    if not _NAIVE_DATETIME_RE.fullmatch(watermark):
        raise ValueError(
            f"P1-3 watermark format violation: expected bare 'YYYY-MM-DD HH:MM:SS', "
            f"got {watermark!r}.  "
            f"Hint: {_IST_AS_UTC_OFFSET_WARNING}"
        )


def get_bq_client():
    """Return singleton BQ client, creating it on first call.

    P2-9: double-checked locking pattern.
    Fast path (after initialisation): read _bq_client without acquiring
    the lock -- avoids lock contention on every BQ call.
    Slow path (first call only): acquire _bq_client_lock, re-check, build.
    The GIL makes the fast-path read atomic; the lock serialises the
    slow path so only one thread ever enters the credential-loading block.
    """
    global _bq_client
    # Fast path: already initialised -- no lock needed.
    if _bq_client is not None:
        return _bq_client
    # Slow path: first call (or concurrent first calls at startup).
    with _bq_client_lock:
        # Re-check inside the lock: a thread that waited here while another
        # thread built the client must not build a second one.
        if _bq_client is None:
            from google.oauth2 import service_account
            from google.cloud import bigquery

            creds = service_account.Credentials.from_service_account_file(
                str(settings.BQ_CREDENTIALS_PATH),
                scopes=["https://www.googleapis.com/auth/bigquery"],
            )
            _bq_client = bigquery.Client(
                project=settings.BQ_PROJECT,
                credentials=creds,
            )
            logger.info("BQ client initialised (project={})", settings.BQ_PROJECT)
    return _bq_client


def _cols() -> str:
    """Comma-separated SELECT column list from settings.BQ_SELECT_COLS."""
    return ", ".join(settings.BQ_SELECT_COLS)


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    reraise=True,
)
def pull_full_day(trade_date_str: str, table_fqn: str) -> pd.DataFrame:
    """Pull all rows for one calendar day from table_fqn.

    Used by backfill (upxtx_ar). Returns empty DataFrame if no rows.
    """
    query = (
        f"SELECT {_cols()} FROM `{table_fqn}` "
        f"WHERE DATE(record_time) = '{trade_date_str}' "
        f"ORDER BY record_time"
    )
    logger.debug("BQ pull_full_day: {} from {}", trade_date_str, table_fqn)
    df = get_bq_client().query(query).to_dataframe()
    logger.info("pull_full_day {}: {} rows", trade_date_str, len(df))
    return df


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    reraise=True,
)
def pull_incremental(watermark: str, table_fqn: str) -> pd.DataFrame:
    """Pull rows with record_time > watermark from table_fqn.

    Used by live incremental tick (upxtx). Returns empty DataFrame if
    no new rows since last watermark (normal between feed cadence windows).

    P1-3: _assert_watermark_format() raises ValueError if watermark is
    tz-aware or otherwise not a bare 'YYYY-MM-DD HH:MM:SS' string.
    This is the IST/UTC seam tripwire -- see module docstring.
    """
    _assert_watermark_format(watermark)   # P1-3 tripwire
    query = (
        f"SELECT {_cols()} FROM `{table_fqn}` "
        f"WHERE record_time > TIMESTAMP('{watermark}') "
        f"ORDER BY record_time"
    )
    logger.debug("BQ pull_incremental: watermark={}", watermark)
    df = get_bq_client().query(query).to_dataframe()
    if not df.empty:
        logger.info("pull_incremental: {} new rows since {}", len(df), watermark)
    return df


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    reraise=True,
)
def pull_day_gap(trade_date_str: str, from_watermark: str, table_fqn: str) -> pd.DataFrame:
    """Pull rows for one calendar day strictly after from_watermark.

    Used by gap fill (upxtx). Handles the 06:35 IST sync scenario:
    if upxtx is empty (post-sync, pre-market), returns empty DataFrame
    which gap_fill.py logs as INFO (not WARNING).

    P1-3: _assert_watermark_format() guards the from_watermark string
    at the same seam as pull_incremental.
    """
    _assert_watermark_format(from_watermark)   # P1-3 tripwire
    query = (
        f"SELECT {_cols()} FROM `{table_fqn}` "
        f"WHERE DATE(record_time) = '{trade_date_str}' "
        f"  AND record_time > TIMESTAMP('{from_watermark}') "
        f"ORDER BY record_time"
    )
    logger.debug("BQ pull_day_gap: {} after {}", trade_date_str, from_watermark)
    df = get_bq_client().query(query).to_dataframe()
    logger.info("pull_day_gap {}: {} rows", trade_date_str, len(df))
    return df


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    reraise=True,
)
def pull_vix_incremental(watermark: str, table_fqn: str) -> pd.DataFrame:
    """Pull INDEX rows from upxtx_vix with record_time > watermark.

    upxtx_vix contains India VIX and ~30 NSE index values recorded at the
    same 1-min cadence as upxtx.  We filter to instrument_type = 'INDEX'
    to exclude any future schema additions.

    Returns a DataFrame with columns:
      record_time, underlying, ltp

    P1-3: same IST/UTC seam as pull_incremental — watermark must be a bare
    'YYYY-MM-DD HH:MM:SS' string.  _assert_watermark_format() is the tripwire.
    """
    _assert_watermark_format(watermark)   # P1-3 tripwire
    query = (
        f"SELECT record_time, underlying, ltp "
        f"FROM `{table_fqn}` "
        f"WHERE instrument_type = 'INDEX' "
        f"  AND record_time > TIMESTAMP('{watermark}') "
        f"ORDER BY record_time"
    )
    logger.debug("BQ pull_vix_incremental: watermark={}", watermark)
    df = get_bq_client().query(query).to_dataframe()
    if not df.empty:
        logger.info(
            "pull_vix_incremental: {} index rows since {}", len(df), watermark
        )
    return df
