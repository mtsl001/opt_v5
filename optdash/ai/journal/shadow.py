"""Shadow trades DAO."""
import sqlite3

# ---------------------------------------------------------------------------
# Column whitelists — guard f-string SQL builders against caller-controlled
# dict keys (F12).  Values are still bound as SQL parameters (?); the risk
# is in the *column names*, which must be validated before interpolation.
# ---------------------------------------------------------------------------
_ALLOWED_SHADOW_COLS: frozenset[str] = frozenset({
    "trade_id", "trade_date", "underlying", "option_type",
    "strike_price", "expiry_date", "entry_premium",
    "final_pnl_pct", "final_pnl_abs", "outcome", "closed_snap", "is_closed",
    "sl_price", "target_price",
})

_ALLOWED_SHADOW_SNAP_COLS: frozenset[str] = frozenset({
    "shadow_id", "snap_time", "ltp", "pnl_pct",
    "hit_sl", "hit_target",
})


def create_shadow(conn: sqlite3.Connection, data: dict, commit: bool = True) -> int:
    """Insert a new shadow trade record.

    commit=True  (default): commit immediately -- safe for standalone calls.
    commit=False: leave the INSERT in the current implicit transaction so the
    caller can bundle it with other writes (e.g. reject_trade) and commit once.
    """
    # F12: validate column names before interpolating into the SQL f-string.
    unknown = set(data.keys()) - _ALLOWED_SHADOW_COLS
    if unknown:
        raise ValueError(f"create_shadow: unknown column(s): {unknown}")

    cols         = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    cur = conn.execute(
        f"INSERT INTO shadow_trades ({cols}) VALUES ({placeholders})",
        list(data.values())
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def get_active_shadows(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    """Return open shadows for the given trade_date only.

    Used by shadow_tracker.py for intraday tracking — prior-day shadows
    are not trackable intraday since historical DuckDB data for prior days
    is not in the active rolling view. For EOD orphan finalization, use
    get_all_unclosed_shadows() instead.
    """
    cur = conn.execute(
        """SELECT * FROM shadow_trades
           WHERE trade_date=? AND is_closed=0
           ORDER BY id""",
        [trade_date]
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_all_unclosed_shadows(conn: sqlite3.Connection) -> list[dict]:
    """Return ALL open shadows regardless of trade_date.

    P4-F10: used by eod.finalize_all_shadows() to sweep orphan shadows from
    prior days when the app crashed before EOD. Those shadows kept is_closed=0
    indefinitely under the old date-filtered get_active_shadows() call,
    silently inflating shadow_total and corrupting learning win-rate stats.
    """
    cur = conn.execute(
        "SELECT * FROM shadow_trades WHERE is_closed=0 ORDER BY id"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def insert_shadow_snap(
    conn:   sqlite3.Connection,
    data:   dict,
    commit: bool = True,
) -> None:
    """Insert a shadow position snap.

    commit=True  (default): commit immediately -- safe for standalone calls.
    commit=False: leave the INSERT in the current implicit transaction so the
    caller can bundle it with close_shadow() and commit once, making the
    snap write and the is_closed flag update atomic (F16).
    """
    # F12: validate column names before interpolating into the SQL f-string.
    unknown = set(data.keys()) - _ALLOWED_SHADOW_SNAP_COLS
    if unknown:
        raise ValueError(f"insert_shadow_snap: unknown column(s): {unknown}")

    cols         = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    conn.execute(
        f"INSERT INTO shadow_snaps ({cols}) VALUES ({placeholders})",
        list(data.values())
    )
    if commit:
        conn.commit()


def close_shadow(
    conn:      sqlite3.Connection,
    shadow_id: int,
    data:      dict,
    commit:    bool = True,
) -> None:
    """Update a shadow trade to is_closed=1 with final PnL and outcome.

    commit=True  (default): commit immediately -- safe for standalone callers
    such as shadow_tracker.py (intraday SL/target hit path).
    commit=False: leave the UPDATE in the caller's open transaction so multiple
    close_shadow() calls can be batched atomically (P1-3: used by
    finalize_all_shadows() in eod.py to wrap all EOD shadow closes in a
    single try/rollback block, preventing a partial-close state on failure).

    H-2: final_pnl_abs (monetary, lot-adjusted) is now persisted alongside
    final_pnl_pct so opportunity-cost Rs figures are available in reports.
    Callers that do not compute pnl_abs (e.g. legacy paths) may omit the key;
    the column will remain NULL for those rows.
    """
    conn.execute(
        """UPDATE shadow_trades
           SET is_closed=1,
               final_pnl_pct=?,
               final_pnl_abs=?,
               outcome=?,
               closed_snap=?
           WHERE id=?""",
        [
            data["final_pnl_pct"],
            data.get("final_pnl_abs"),   # None-safe: omitted by callers that don't compute it
            data["outcome"],
            data["closed_snap"],
            shadow_id,
        ]
    )
    if commit:
        conn.commit()


def get_shadow_history(
    conn: sqlite3.Connection,
    days: int = 30,
    underlying: str | None = None,
) -> list[dict]:
    # Part-6-F7: LEFT JOIN so orphaned shadow rows (parent trade manually
    # deleted before FK enforcement was active) are included in the history
    # rather than silently dropped, which would understate shadow_total in
    # the learning report.
    q = """SELECT st.*, t.narrative, t.gate_score, t.confidence
           FROM shadow_trades st
           LEFT JOIN trades t ON t.id = st.trade_id
           WHERE st.trade_date >= date('now', ?)
           AND st.is_closed=1"""
    params: list = [f"-{days} days"]
    if underlying:
        q += " AND st.underlying=?"
        params.append(underlying)
    q += " ORDER BY st.trade_date DESC"
    cur  = conn.execute(q, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
