"""SQLite journal schema — CREATE TABLE + INDEX statements.

All tables use CREATE TABLE IF NOT EXISTS so init_db() is safely idempotent
and can be called on every fresh connection without side effects.

For existing databases, _run_migrations() uses ALTER TABLE to add new columns
and CREATE INDEX IF NOT EXISTS to add new indexes.

SQLite OperationalError handling in _run_migrations():
  - "duplicate column name": ALTER TABLE re-run on a column that already
    exists -- silent pass (idempotent).
  - "already exists": CREATE INDEX re-run on older SQLite builds that do
    not honour IF NOT EXISTS -- silent pass (idempotent).
  - "no such table": a table was not created (e.g. OOM mid-executescript);
    RuntimeError with a targeted diagnostic message.
  - anything else: RuntimeError with the raw SQL + error so startup fails
    loudly (disk full, locked, wrong column type, etc.).

Connection entry-point
----------------------
Always open journal connections via open_journal(path) defined below.
This guarantees that PRAGMA foreign_keys, WAL mode, and busy_timeout
are set on every connection -- SQLite resets them to defaults on each
new connection object, so they must be re-applied every time.
"""
import sqlite3
from pathlib import Path

CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date          TEXT    NOT NULL,
    -- snap_time = AI generation snap; immutable after INSERT.
    -- Do NOT overwrite on ACCEPT -- use accept_snap_time for that.
    snap_time           TEXT    NOT NULL,
    underlying          TEXT    NOT NULL,
    -- Fix-P1-12: CHECK constraint rejects any value outside CE/PE.
    -- Prevents Direction.NEUTRAL or a typo from being stored silently.
    option_type         TEXT    NOT NULL CHECK(option_type IN ('CE','PE')),
    strike_price        REAL    NOT NULL,
    expiry_date         TEXT    NOT NULL,
    dte                 INTEGER,
    entry_premium       REAL    NOT NULL,
    actual_entry_price  REAL,               -- set on ACCEPT (slippage-adjusted)
    sl_price            REAL    NOT NULL,
    target_price        REAL    NOT NULL,
    exit_premium        REAL,
    exit_snap_time      TEXT,
    exit_reason         TEXT,               -- ExitReason enum value
    final_pnl_abs       REAL,
    final_pnl_pct       REAL,
    confidence          INTEGER NOT NULL,
    gate_score          INTEGER NOT NULL,
    -- Fix-P1-12: CHECK constraints on gate_verdict and status.
    gate_verdict        TEXT    NOT NULL
                        CHECK(gate_verdict IN ('GO','WAIT','NO_GO')),
    s_score             REAL,
    quality_grade       TEXT,
    direction_signals   TEXT,               -- JSON blob
    narrative           TEXT,
    -- Fix-P1-12: status CHECK ensures only valid TradeStatus values are stored.
    status              TEXT    NOT NULL DEFAULT 'GENERATED'
                        CHECK(status IN ('GENERATED','ACCEPTED','REJECTED','EXPIRED','CLOSED')),
    rejection_reason    TEXT,
    rejection_note      TEXT,
    session             TEXT,               -- MarketSession enum value
    delta               REAL,
    theta               REAL,
    vega                REAL,
    gamma               REAL,
    iv_at_entry         REAL,
    spot_at_entry       REAL,
    conf_buckets        TEXT,               -- JSON blob
    -- Fix-P1-14: acceptance snap stored separately so snap_time (generation
    -- time) is never overwritten. The generation-to-acceptance delta is a
    -- key learning signal preserved by this separation.
    accept_snap_time    TEXT,               -- set on ACCEPT; snap_time stays immutable
    created_at          TEXT    DEFAULT (datetime('now')),
    updated_at          TEXT    DEFAULT (datetime('now'))
);
"""

CREATE_POSITION_SNAPS = """
CREATE TABLE IF NOT EXISTS position_snaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    snap_time       TEXT    NOT NULL,
    ltp             REAL,
    pnl_abs         REAL,
    pnl_pct         REAL,
    sl_adjusted     REAL,
    theta_sl_status TEXT,
    iv              REAL,
    iv_crush        TEXT,
    gate_score      INTEGER,
    gate_verdict    TEXT,
    delta_pnl       REAL,
    gamma_pnl       REAL,
    vega_pnl        REAL,
    theta_pnl       REAL,
    unexplained     REAL,
    created_at      TEXT DEFAULT (datetime('now'))
);
"""

CREATE_SHADOWS = """
CREATE TABLE IF NOT EXISTS shadow_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    trade_date      TEXT    NOT NULL,
    underlying      TEXT    NOT NULL,
    option_type     TEXT    NOT NULL,
    strike_price    REAL    NOT NULL,
    expiry_date     TEXT    NOT NULL,
    entry_premium   REAL    NOT NULL,
    -- P0-5: sl_price and target_price were present in _ALLOWED_SHADOW_COLS
    -- (shadow.py) but absent from this DDL, causing OperationalError on any
    -- create_shadow() call that passes these keys (e.g. the rejection path).
    sl_price        REAL,
    target_price    REAL,
    final_pnl_pct   REAL,
    outcome         TEXT,               -- ShadowOutcome enum value
    closed_snap     TEXT,
    is_closed       INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
"""

CREATE_SHADOW_SNAPS = """
CREATE TABLE IF NOT EXISTS shadow_snaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shadow_id   INTEGER NOT NULL REFERENCES shadow_trades(id) ON DELETE CASCADE,
    snap_time   TEXT    NOT NULL,
    ltp         REAL,
    pnl_pct     REAL,
    hit_sl      INTEGER DEFAULT 0,
    hit_target  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);
"""

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_status          ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_date            ON trades(trade_date);
CREATE INDEX IF NOT EXISTS idx_trades_underlying      ON trades(underlying);
CREATE INDEX IF NOT EXISTS idx_trades_status_ul       ON trades(status, underlying);
CREATE INDEX IF NOT EXISTS idx_trades_session         ON trades(session);
-- Fix-P1-16: composite (trade_id, snap_time) replaces single-column (trade_id).
-- get_snaps_for_trade() ORDER BY snap_time ASC and get_latest_snap()
-- ORDER BY snap_time DESC LIMIT 1 both become index-only range scans.
CREATE INDEX IF NOT EXISTS idx_snaps_trade_time       ON position_snaps(trade_id, snap_time);
CREATE INDEX IF NOT EXISTS idx_shadow_trade_id        ON shadow_trades(trade_id);
-- Fix-P1-17: composite (trade_date, is_closed) for get_active_shadows().
-- WHERE trade_date=? AND is_closed=0 is now a covering range scan;
-- previously idx_shadow_date left a full row-level filter on is_closed.
CREATE INDEX IF NOT EXISTS idx_shadow_active          ON shadow_trades(trade_date, is_closed);
CREATE INDEX IF NOT EXISTS idx_shadow_snaps_shadow_id ON shadow_snaps(shadow_id);
"""

# ---------------------------------------------------------------------------
# Additive-only migrations for existing databases.
#
# WHY THIS LIST EXISTS
# --------------------
# CREATE TABLE IF NOT EXISTS creates ALL columns for fresh installs.
# Existing databases (created before a column was added) need ALTER TABLE.
# SQLite raises OperationalError("duplicate column name") on re-runs;
# _run_migrations() catches that silently so every entry is idempotent.
#
# RULES FOR ADDING NEW COLUMNS
# ----------------------------
# 1. Add the column to CREATE_TRADES above (for fresh installs).
# 2. Add a corresponding ALTER TABLE line here (for existing databases).
# 3. Never remove or reorder existing entries -- they are idempotent no-ops
#    on databases that already have the column.
#
# RULES FOR ADDING NEW INDEXES
# ----------------------------
# 1. Add the CREATE INDEX IF NOT EXISTS to CREATE_INDEXES above.
# 2. Add the same CREATE INDEX IF NOT EXISTS here for existing databases.
# 3. _run_migrations() silences "already exists" for CREATE INDEX re-runs
#    on older SQLite builds that do not honour IF NOT EXISTS.
# ---------------------------------------------------------------------------
_MIGRATIONS = [
    # -- original column -------------------------------------------------------
    "ALTER TABLE trades ADD COLUMN session            TEXT",

    # -- columns added after initial schema release ----------------------------
    # Fix-A: these columns were present in CREATE_TRADES DDL but missing
    # from _MIGRATIONS, causing OperationalError on upgrade installs.
    "ALTER TABLE trades ADD COLUMN actual_entry_price REAL",
    "ALTER TABLE trades ADD COLUMN s_score            REAL",
    "ALTER TABLE trades ADD COLUMN quality_grade      TEXT",
    "ALTER TABLE trades ADD COLUMN direction_signals  TEXT",
    "ALTER TABLE trades ADD COLUMN narrative          TEXT",
    "ALTER TABLE trades ADD COLUMN rejection_reason   TEXT",
    "ALTER TABLE trades ADD COLUMN rejection_note     TEXT",
    "ALTER TABLE trades ADD COLUMN delta              REAL",
    "ALTER TABLE trades ADD COLUMN theta              REAL",
    "ALTER TABLE trades ADD COLUMN vega               REAL",
    "ALTER TABLE trades ADD COLUMN gamma              REAL",
    "ALTER TABLE trades ADD COLUMN iv_at_entry        REAL",
    "ALTER TABLE trades ADD COLUMN spot_at_entry      REAL",
    "ALTER TABLE trades ADD COLUMN conf_buckets       TEXT",

    # Fix-P1-14: accept_snap_time stores the acceptance snap; snap_time now
    # remains the immutable AI generation time for the lifetime of the row.
    "ALTER TABLE trades ADD COLUMN accept_snap_time   TEXT",

    # Fix-P1-16: add composite covering index for position_snaps tick queries.
    # Existing databases keep idx_snaps_trade (harmless single-col); the new
    # composite index is preferred by the query planner for ORDER BY snap_time.
    "CREATE INDEX IF NOT EXISTS idx_snaps_trade_time ON position_snaps(trade_id, snap_time)",

    # Fix-P1-17: add composite index for get_active_shadows() active-shadow scan.
    # Existing databases already have idx_shadow_date; this adds the is_closed
    # predicate so WHERE trade_date=? AND is_closed=0 is a single covering scan.
    "CREATE INDEX IF NOT EXISTS idx_shadow_active ON shadow_trades(trade_date, is_closed)",

    # P2-D: idx_shadow_snaps_shadow_id was present in CREATE_INDEXES for fresh
    # installs but absent from _MIGRATIONS, so existing databases never received
    # it.  get_shadow_snaps() WHERE shadow_id=? did a full shadow_snaps table
    # scan on every position-tracking tick.  Added here so upgrade installs
    # get the index applied on next startup.
    "CREATE INDEX IF NOT EXISTS idx_shadow_snaps_shadow_id ON shadow_snaps(shadow_id)",

    # P0-5: sl_price and target_price were present in _ALLOWED_SHADOW_COLS
    # (shadow.py) but absent from the CREATE_SHADOWS DDL and this migration
    # list.  Any create_shadow() call passing either key raised OperationalError
    # at the first trade rejection.  Added to CREATE_SHADOWS for fresh installs;
    # these ALTER TABLE entries backfill existing databases.
    "ALTER TABLE shadow_trades ADD COLUMN sl_price     REAL",
    "ALTER TABLE shadow_trades ADD COLUMN target_price REAL",
]


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def open_journal(path: Path) -> sqlite3.Connection:
    """Open a journal SQLite connection with all required per-connection PRAGMAs.

    Fix-P1-15: PRAGMA foreign_keys, WAL mode, and busy_timeout are
    per-connection SQLite settings that default to OFF/DELETE/0 on every new
    connection. Previously they were only set inside init_db(), so sched_conn
    in deps.py and any test fixture using bare sqlite3.connect() had foreign-
    key enforcement silently disabled: ON DELETE CASCADE on position_snaps and
    shadow_trades would not fire, accumulating orphan rows invisibly.

    This factory must be used for every journal connection in the codebase
    (deps.py _open_journal_conn, scheduler.py, and test fixtures).
    """
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # write-ahead log for concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")        # enable ON DELETE CASCADE etc.
    conn.execute("PRAGMA busy_timeout=5000")      # wait up to 5 s instead of SQLITE_BUSY
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db(conn) -> None:
    """Create all tables and indexes, then apply any pending column migrations.
    Idempotent -- safe to call on every new connection.
    """
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        CREATE_TRADES
        + CREATE_POSITION_SNAPS
        + CREATE_SHADOWS
        + CREATE_SHADOW_SNAPS
        + CREATE_INDEXES
    )
    conn.commit()
    _run_migrations(conn)


def _run_migrations(conn) -> None:
    """Apply ALTER TABLE and CREATE INDEX migrations for existing databases.

    Each migration is attempted once.  Three OperationalError subtypes are
    handled explicitly:

    1. "duplicate column name"  -- ALTER TABLE re-run on an existing column.
       Silent pass: idempotent, expected on every upgrade after the column
       was first added.

    2. "already exists"         -- CREATE INDEX re-run on older SQLite builds
       (< 3.3.0) that do not honour IF NOT EXISTS on indexes.
       Silent pass: idempotent, index is already present.

    3. "no such table"          -- CREATE INDEX failed because the target
       table was never created (e.g. OOM or disk-full mid-executescript in
       init_db()).  RuntimeError with a targeted diagnostic directing the
       operator to check whether init_db() completed successfully.

    4. anything else            -- RuntimeError wrapping the raw SQL + error
       so startup fails loudly (disk full, locked, wrong column type, etc.).
       The column / index does not exist; the first query against it will
       raise a cryptic error with no trace back to the failed migration
       unless we surface it here.

    Fix-P1-13: previously all non-duplicate OperationalErrors were silently
    swallowed via bare `except Exception: pass`, hiding disk-full / locked /
    wrong-type failures at startup.
    """
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column name" in msg:
                # ALTER TABLE re-run -- column already exists, safe to ignore.
                pass
            elif "already exists" in msg:
                # CREATE INDEX re-run on older SQLite without IF NOT EXISTS
                # support -- index already present, safe to ignore.
                pass
            elif "no such table" in msg:
                raise RuntimeError(
                    f"Migration failed: target table missing for:\n  {sql!r}\n"
                    f"SQLite error: {e}\n"
                    "Check that init_db() completed successfully before "
                    "_run_migrations() was called (executescript may have "
                    "been interrupted by OOM or disk-full)."
                ) from e
            else:
                raise RuntimeError(
                    f"Migration failed (unexpected error): {sql!r}\n{e}"
                ) from e
