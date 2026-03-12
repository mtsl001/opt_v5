"""Shared FastAPI dependencies -- DuckDB + SQLite connection management.

DuckDB lifecycle
----------------
deps.startup() owns the FULL DuckDB lifecycle:
  1. Calls duckdb_gateway.startup() -- creates the in-process :memory:
     connection and registers the rolling processed/ Parquet view.
  2. Stores the returned connection on app.state.duck so all API routers
     receive it via Depends(get_duck).
  3. deps.shutdown() calls duckdb_gateway.shutdown() to close the
     connection cleanly before the process exits.

SQLite lifecycle
----------------
deps.startup() opens TWO SQLite connections to the same WAL-mode database:

  app.state.journal            -- used by API request handlers.  FastAPI
                                  runs sync (def) endpoints in anyio's
                                  thread pool via anyio.to_thread.run_sync(),
                                  so this connection lives in a worker thread.

  app.state.scheduler_journal  -- used exclusively by the APScheduler tick,
                                  which is a coroutine running in the asyncio
                                  event loop thread.

Two separate connections are required because Python's sqlite3.Connection is
NOT thread-safe.  check_same_thread=False only disables the safety check; it
does not add actual thread-safety.  Sharing one Connection across threads can
silently corrupt its internal state.

SQLite's WAL mode coordinates concurrent writes between the two connections at
the file level safely -- this is exactly the use-case WAL was designed for.

Ordering guarantee (P2-F10)
---------------------------
app.state.scheduler_journal is opened AFTER init_db(jconn) returns.
SQLite WAL mode guarantees that any connection opened after a commit
immediately sees the committed schema.  This ensures the scheduler
cannot write against a partially-migrated schema on fast startup.

Prerequisite: _run_migrations() must propagate real errors (not swallow
them with bare except: pass) -- fixed in P1-F13 / schema.py.  Without
that fix, init_db() can return early with an incomplete schema and
this ordering guarantee is vacuous.

BQ pipeline startup (Steps 3 + 4)
----------------------------------
Step 3: run_backfill(duck_conn) -- pulls full days from upxtx_ar for
  any trading day not already present in processed/.  Fatal on failure
  (run_backfill raises) so the process does not start with a silent
  historical gap.  Wrapped in try/except so BQ connectivity issues on
  non-BQ deployments do not block the FastAPI process permanently.
  refresh_views() is called after to surface new partition directories.

Step 4: run_gap_fill(duck_conn) -- fills missing intraday windows
  between the saved watermark and now from upxtx.  Non-fatal: logged
  only if unavailable so a pre-market startup succeeds regardless of
  BQ connectivity.  refresh_views() called after in case new partitions
  were created by the gap fill.

Both BQ steps run inside asyncio.to_thread() so blocking BQ I/O and
Parquet writes never block the FastAPI event loop thread.

This means both the full-stack run (run_api.py) and the standalone DB-only
mode (uvicorn optdash.api.app:app) work correctly without any extra DuckDB
wiring in the entry points -- the lifespan just calls deps.startup/shutdown.
"""
import asyncio
import sqlite3
from fastapi import FastAPI, Request
from loguru import logger

from optdash.config import settings
from pathlib import Path
from optdash.ai.journal.schema import init_db, open_journal
from optdash.pipeline.duckdb_gateway import (
    startup  as duck_startup,
    shutdown as duck_shutdown,
    get_conn as get_duck_conn,  # noqa: F401  (kept for external callers)
    refresh_views,
)


def _open_journal_conn(path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, FK enforcement, and busy_timeout.

    Issue-1 fix: delegates to schema.py::open_journal() — the canonical
    connection factory that guarantees WAL + foreign_keys + busy_timeout=5000
    on every connection.  Previously this function duplicated those PRAGMAs
    but omitted busy_timeout, causing immediate SQLITE_BUSY errors under
    concurrent API + scheduler writes instead of retrying for 5 seconds.

    synchronous=NORMAL is added on top: safe with WAL, ~3x faster writes.
    """
    conn = open_journal(Path(path))
    conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, ~3x faster writes
    return conn



async def startup(app: FastAPI) -> None:
    """Initialise DuckDB gateway, SQLite journal, then run BQ pipeline steps."""
    # ── Step 1: DuckDB ────────────────────────────────────────────────────────
    # Creates the in-process :memory: connection and registers the rolling
    # processed/ Parquet view. Must happen first -- all API endpoints depend on it.
    duck_conn      = duck_startup()
    app.state.duck = duck_conn
    logger.info("DuckDB gateway initialised -- in-memory connection ready.")

    # ── Step 2: SQLite journal ────────────────────────────────────────────────
    logger.info("Connecting SQLite journal: {}", settings.JOURNAL_DB_PATH)

    # Connection 1: API layer -- used by FastAPI sync endpoints
    # (anyio thread pool, separate OS thread from the event loop).
    jconn = _open_journal_conn(settings.JOURNAL_DB_PATH)
    init_db(jconn)               # create tables + indexes + all migrations (idempotent)
    app.state.journal = jconn

    # Connection 2: Scheduler -- opened AFTER init_db() fully returns.
    # WAL mode guarantees this connection sees the fully committed schema
    # immediately (P2-F10 ordering guarantee -- see module docstring).
    sched_conn = _open_journal_conn(settings.JOURNAL_DB_PATH)
    app.state.scheduler_journal = sched_conn

    logger.info(
        "SQLite journal ready -- 2 connections opened "
        "(API thread-pool conn + scheduler event-loop conn)."
    )

    # ── Step 3: BQ backfill ───────────────────────────────────────────────────
    # Pull full historical days from upxtx_ar for any trading day not already
    # present in processed/. run_backfill() is idempotent and fatal on failure.
    # asyncio.to_thread: blocking BQ I/O + Parquet writes off the event loop.
    try:
        from optdash.pipeline.backfill import run_backfill
        await asyncio.to_thread(run_backfill, duck_conn)
        # Refresh DuckDB view so any newly created partition directories are
        # immediately visible to the API without requiring a process restart.
        refresh_views(duck_conn)
        logger.info("BQ backfill complete.")
    except Exception as backfill_err:
        # Non-fatal guard: allows the app to start on environments where BQ
        # credentials are not configured (e.g. development / CI).
        # On production, run_backfill() raises immediately so this log is the
        # alert signal -- monitor for "BQ backfill failed" in production logs.
        logger.error("BQ backfill failed (app will start with available data): {}", backfill_err)

    # ── Step 4: BQ gap fill ───────────────────────────────────────────────────
    # Fill missing intraday windows between the saved watermark and now.
    # Non-fatal by design (gap_fill handles 0-row 06:35 sync window gracefully).
    try:
        from optdash.pipeline.gap_fill import run_gap_fill
        await asyncio.to_thread(run_gap_fill, duck_conn)
        # Refresh again in case gap fill created new partition directories
        # (e.g. today's directory did not exist yet at Step 3 time).
        refresh_views(duck_conn)
        logger.info("BQ gap fill complete.")
    except Exception as gap_err:
        logger.error("BQ gap fill failed (app will start with available data): {}", gap_err)


async def shutdown(app: FastAPI) -> None:
    """Close DuckDB connection and both SQLite journal connections."""
    duck_shutdown()
    for attr in ("journal", "scheduler_journal"):
        # Use getattr with a default of None so a never-set attribute
        # (e.g. startup crashed before _open_journal_conn) doesn't raise
        # AttributeError and mask the original startup failure in the log.
        conn = getattr(app.state, attr, None)
        if conn is not None:
            try:
                conn.close()
            except Exception as e:
                logger.warning("Error closing SQLite connection '{}': {}", attr, e)


def get_duck(request: Request):
    return request.app.state.duck


def get_journal(request: Request) -> sqlite3.Connection:
    return request.app.state.journal
