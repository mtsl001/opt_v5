"""Trades DAO — CRUD for the trades table."""
import sqlite3

# ---------------------------------------------------------------------------
# Allowed column sets -- validated before any f-string SQL construction (F12)
# ---------------------------------------------------------------------------
_ALLOWED_TRADE_COLS: frozenset[str] = frozenset({
    "trade_date", "snap_time", "accept_snap_time", "underlying", "option_type",
    "strike_price", "expiry_date", "dte", "entry_premium", "actual_entry_price",
    "sl_price", "target_price", "exit_premium", "exit_snap_time", "exit_reason",
    "final_pnl_abs", "final_pnl_pct", "confidence", "gate_score", "gate_verdict",
    "s_score", "quality_grade", "direction_signals", "narrative", "status",
    "rejection_reason", "rejection_note", "session", "delta", "theta", "vega",
    "gamma", "iv_at_entry", "spot_at_entry", "conf_buckets",
    # N-3 fix: removed phantom 'recommendation_snap_time' -- that column does
    # not exist in the schema.  The correct column name is 'accept_snap_time'
    # (added in Fix-P1-14, declared in CREATE_TRADES and _MIGRATIONS).
    # 'accept_snap_time' is already listed above; no new entry needed here.
})

# ---------------------------------------------------------------------------
# P2-5: upper-bound guard for caller-supplied row limits.
# ---------------------------------------------------------------------------
_LIMIT_MIN: int = 1
_LIMIT_MAX: int = 500


def _check_limit(value: int, name: str = "limit") -> None:
    """Raise ValueError if *value* is outside [_LIMIT_MIN, _LIMIT_MAX].

    P2-5: get_open_trades / get_pending_trades / get_trade_history all
    interpolate the caller-supplied limit directly into an f-string SQL
    fragment.  An unchecked large value causes a full-table scan that is
    serialised to a list[dict] in RAM -- silent, unbounded, no error.
    A value of 0 returns 0 rows without error, causing callers to silently
    skip the recommendation / gate cycle.
    """
    if not (_LIMIT_MIN <= value <= _LIMIT_MAX):
        raise ValueError(
            f"{name} must be in [{_LIMIT_MIN}, {_LIMIT_MAX}], got {value!r}"
        )


def create_trade(conn: sqlite3.Connection, data: dict) -> int:
    """Insert a new trade row and return the new row id.

    Raises ValueError if *data* contains any key not in _ALLOWED_TRADE_COLS
    (prevents f-string SQL injection via unvalidated dict keys).
    """
    unknown = set(data.keys()) - _ALLOWED_TRADE_COLS
    if unknown:
        raise ValueError(f"create_trade: unknown column(s): {unknown}")
    cols         = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    cur = conn.execute(
        f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
        list(data.values())
    )
    conn.commit()
    return cur.lastrowid


def get_trade(conn: sqlite3.Connection, trade_id: int) -> dict | None:
    return _fetchone(
        conn, "SELECT * FROM trades WHERE id=?", [trade_id]
    )


def get_open_trades(
    conn:         sqlite3.Connection,
    underlying:   str | None = None,
    max_age_days: int        = 3,
    limit:        int        = 50,
) -> list[dict]:
    """Return ACCEPTED trades younger than *max_age_days* calendar days.

    max_age_days=3 (default): a trade open for 3 calendar days is almost
    certainly a bug artifact (missed EOD close), not a live position.
    Filtering stale rows prevents the scheduler from issuing one DuckDB
    round-trip per stale trade per tick indefinitely.

    limit: capped at _LIMIT_MAX (500) -- raises ValueError if exceeded.
    P2-5: unchecked limit caused silent full-table scans serialised to RAM.
    """
    _check_limit(limit)
    q      = "SELECT * FROM trades WHERE status='ACCEPTED' AND trade_date >= date('now', ?)"
    params = [f"-{max_age_days} days"]
    if underlying:
        q += " AND underlying=?"
        params.append(underlying)
    q += f" ORDER BY created_at DESC LIMIT {limit}"
    return _fetchall(conn, q, params)


def get_pending_trades(
    conn:         sqlite3.Connection,
    underlying:   str | None = None,
    max_age_days: int        = 1,
    limit:        int        = 50,
) -> list[dict]:
    """Return GENERATED (pending) trades younger than *max_age_days* calendar days.

    max_age_days=1 (default): a recommendation that was never
    accepted/rejected after 1 day is expired stale data.

    limit: capped at _LIMIT_MAX (500) -- raises ValueError if exceeded.
    P2-5: unchecked limit caused silent full-table scans serialised to RAM.
    """
    _check_limit(limit)
    q      = "SELECT * FROM trades WHERE status='GENERATED' AND trade_date >= date('now', ?)"
    params = [f"-{max_age_days} days"]
    if underlying:
        q += " AND underlying=?"
        params.append(underlying)
    q += f" ORDER BY created_at DESC LIMIT {limit}"
    return _fetchall(conn, q, params)


def get_latest_trade(
    conn: sqlite3.Connection,
    underlying: str,
) -> dict | None:
    return _fetchone(
        conn,
        "SELECT * FROM trades WHERE underlying=? ORDER BY created_at DESC LIMIT 1",
        [underlying],
    )


def get_trade_history(
    conn:       sqlite3.Connection,
    page:       int = 1,
    per_page:   int = 20,
    underlying: str | None = None,
    status:     str | None = None,
) -> dict:
    """Return a paginated slice of the trades table.

    per_page: capped at _LIMIT_MAX (500) -- raises ValueError if exceeded.
    P2-5: unchecked per_page caused silent full-table scans serialised to RAM.
    """
    _check_limit(per_page, name="per_page")
    offset = (page - 1) * per_page
    where_clauses: list[str] = []
    params:        list      = []

    if underlying:
        where_clauses.append("underlying=?")
        params.append(underlying)
    if status:
        where_clauses.append("status=?")
        params.append(status)

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM trades {where}", params
    ).fetchone()[0]

    trade_rows = _fetchall(
        conn,
        f"SELECT * FROM trades {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    )

    return {
        "trades":   trade_rows,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
    }


def update_status(
    conn:         sqlite3.Connection,
    trade_id:     int,
    status:       str,
    state_reason: str | None = None,
    commit:       bool       = True,
) -> None:
    """Update trade status and optionally persist a state_reason.

    COALESCE(?, rejection_reason) writes the new reason when provided,
    and preserves the existing value when state_reason is None --
    so a bare status update never clears a previously recorded reason.

    commit=True  (default): commit immediately -- safe for standalone calls.
    commit=False: leave the UPDATE in the current implicit transaction so
    the caller can bundle multiple status updates (e.g. EOD sweep) into
    one atomic transaction and commit once at the end (P6-F5).
    """
    conn.execute(
        """UPDATE trades
           SET status=?,
               rejection_reason=COALESCE(?, rejection_reason),
               updated_at=datetime('now')
           WHERE id=?""",
        [status, state_reason, trade_id],
    )
    if commit:
        conn.commit()


def accept_trade(
    conn:               sqlite3.Connection,
    trade_id:           int,
    accept_snap_time:   str,
    actual_entry_price: float | None = None,
) -> None:
    """Accept a generated trade recommendation and record the acceptance snap.

    Fix-P1-14: snap_time (the AI generation snap) is intentionally NOT
    updated here. It is set at INSERT time and must remain immutable so that:
      - build_theta_sl_series() anchors the theta-SL curve at generation time.
      - The generation-to-acceptance delta (snap_time vs accept_snap_time)
        is preserved as a learning signal for the AI feedback loop.
      - Session attribution based on snap_time is always the generation
        session, not the (potentially different) acceptance session.

    accept_snap_time is written to the dedicated column added in schema.py.
    """
    conn.execute(
        """UPDATE trades
           SET status='ACCEPTED',
               actual_entry_price=COALESCE(?, entry_premium),
               accept_snap_time=?,
               updated_at=datetime('now')
           WHERE id=?""",
        [actual_entry_price, accept_snap_time, trade_id]
    )
    conn.commit()


def reject_trade(
    conn:     sqlite3.Connection,
    trade_id: int,
    reason:   str,
    note:     str | None = None,
    commit:   bool = True,
) -> None:
    """Update trade status to REJECTED.

    commit=True  (default): commit immediately -- safe for standalone calls.
    commit=False: leave the UPDATE in the current implicit transaction so
    the caller can bundle it with shadow.create_shadow() and commit once,
    making both writes atomic (used by the /reject API endpoint).
    """
    conn.execute(
        """UPDATE trades
           SET status='REJECTED', rejection_reason=?, rejection_note=?,
               updated_at=datetime('now')
           WHERE id=?""",
        [reason, note, trade_id]
    )
    if commit:
        conn.commit()


def close_trade(
    conn:     sqlite3.Connection,
    trade_id: int,
    data:     dict,
    commit:   bool = True,
) -> None:
    """Close a trade and record exit data.

    commit=True  (default): commit immediately -- safe for standalone calls.
    commit=False: leave the UPDATE in the current implicit transaction so
    the caller can batch multiple closes (e.g. EOD sweep) into one atomic
    transaction and commit once at the end (P6-F5 EOD atomicity fix).
    """
    conn.execute(
        """UPDATE trades
           SET status='CLOSED',
               exit_premium=?,
               exit_snap_time=?,
               exit_reason=?,
               final_pnl_abs=?,
               final_pnl_pct=?,
               updated_at=datetime('now')
           WHERE id=?""",
        [
            data["exit_premium"],
            data["exit_snap_time"],
            data["exit_reason"],
            data["final_pnl_abs"],
            data["final_pnl_pct"],
            trade_id,
        ]
    )
    if commit:
        conn.commit()


# -- Helpers ------------------------------------------------------------------

def _fetchone(
    conn:   sqlite3.Connection,
    q:      str,
    params: list,
) -> dict | None:
    """Execute query, return first row as dict or None.

    Uses cursor.description zip so it works correctly regardless of
    whether row_factory=sqlite3.Row is set on the connection (F14).
    dict(row) was previously used, which silently returns integer-keyed
    dicts on connections without row_factory.
    """
    cur = conn.execute(q, params)
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetchall(conn: sqlite3.Connection, q: str, params: list) -> list[dict]:
    """Execute query and return list of dicts.

    Uses cursor.description zip -- works correctly regardless of whether
    row_factory=sqlite3.Row is set on the connection.
    Single-pass: one execute, one fetchall.
    """
    cur  = conn.execute(q, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
