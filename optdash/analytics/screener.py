"""Strike screener — S_score ranking with star ratings.

S_score (0–~180) is a weighted composite of 7 independent factors:
  1. delta        — directional sensitivity (normalized 0-1)
  2. liquidity    — OI × LTP (capped per underlying) with bid-ask spread penalty
  3. IV           — lower IV preferred for entry (gated by IVP < 50)
  4. gamma        — convexity / acceleration (capped at 0.01)
  5. vega         — IV sensitivity (capped at 50)
  6. eff_ratio    — theta/delta efficiency at 10% cap
  7. momentum     — volume / avg_volume_20d (capped at 3.0x)

Theoretical max = (W_DELTA×1.0 + W_LIQUIDITY×1.0 + W_IV×1.0
                   + W_GAMMA×1.0 + W_VEGA×1.0 + W_EFF_RATIO×1.0 + W_MOMENTUM×3.0) × 10
               = (4.0 + 3.0 + 2.0 + 1.0 + 1.0 + 4.0 + 3.0) × 10 = 180
delta is capped at 0.65 by the SCREENER_MAX_DELTA filter.
Typical well-screened option scores 70–130.
"""
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.analytics.iv import get_ivr_ivp


def get_strikes(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
    top_n:      int = 20,
    direction:  str | None = None,
) -> list[dict]:
    """
    Return top_n strikes ranked by S_score.
    Filters: moneyness ≤5%, delta 0.10–0.50, liquidity_cr ≥0.5.

    Fix-J: optional `direction` parameter ("CE" or "PE").
      When provided, adds AND o.option_type = ? to the ranked CTE.
      When omitted (None), both CE and PE are returned (backwards-compatible).

      Direction filter is injected as a Python f-string clause with a
      separate params list entry to avoid DuckDB nullable-param quirks.
    """
    try:
        # Fix-J: build optional direction filter in Python rather than
        # relying on (? IS NULL OR ...) to sidestep DuckDB NULL param issues.
        direction_clause = "AND o.option_type = ?" if direction else ""

        # Fetch IVP for Issue #4 to gate the IV penalty
        iv_data = get_ivr_ivp(conn, trade_date, snap_time, underlying)
        ivp = iv_data.get("ivp")
        iv_penalty_gate = 1.0 if (ivp is not None and ivp < 50) else 0.0

        min_dte_row = conn.execute(
            "SELECT MIN(dte) FROM options_data WHERE trade_date=? AND snap_time=? AND underlying=? AND expiry_tier IN ('TIER1', 'TIER2')",
            [trade_date, snap_time, underlying]
        ).fetchone()
        min_dte = min_dte_row[0] if min_dte_row and min_dte_row[0] is not None else 99
        eff_cap = settings.SCREENER_MIN_EFF_RATIO
        if min_dte <= 2:
            eff_cap = max(eff_cap, 0.20)  # Relaxed eff cap for DTE<=2

        result = conn.execute(f"""
            WITH spot_cte AS (
                SELECT AVG(spot) AS spot
                FROM options_data
                WHERE trade_date=? AND snap_time=? AND underlying=?
            ),
            ranked AS (
                SELECT
                    o.expiry_date,
                    o.expiry_tier,
                    o.dte,
                    o.option_type,
                    o.strike_price,
                    o.ltp,
                    o.iv,
                    o.delta,
                    o.theta,
                    o.gamma,
                    o.vega,
                    (o.strike_price - s.spot) / s.spot * 100              AS moneyness_pct,
                    o.oi * o.ltp / 1e7                                     AS liquidity_cr,
                    ABS(o.theta) / NULLIF(ABS(o.delta), 0)                 AS eff_ratio,
                    (
                        -- 1. Delta: directional sensitivity (normalized)
                        ? * (ABS(o.delta) - ?) / (? - ?)
                        -- 2. Liquidity (capped per underlying) with bid-ask spread penalty
                      + ? * LEAST(1.0, o.oi * o.ltp / 1e7 / ?)
                          * (1.0 - LEAST(1.0, COALESCE(o.depth_ask1_price - o.depth_bid1_price, 0) / NULLIF(o.ltp * 0.05, 0)))
                        -- 3. IV: lower is better (gated by IVP < 50)
                      + ? * ? * (1.0 - LEAST(1.0, o.iv / 100.0))
                        -- 4. Gamma: convexity (cap at 0.01)
                      + ? * LEAST(1.0, ABS(o.gamma) * 100)
                        -- 5. Vega: IV sensitivity (cap at 50)
                      + ? * LEAST(1.0, ABS(o.vega) / 50.0)
                        -- 6. Eff-ratio: theta/delta at parameterised cap
                      + ? * (1.0 - LEAST(1.0, ABS(o.theta) / NULLIF(ABS(o.delta), 0) / ?))
                        -- 7. Momentum signal: volume / avg_volume_20d (cap at 3x)
                      + ? * LEAST(3.0, o.volume / NULLIF(TRY_CAST(o.avg_volume_20d AS DOUBLE), 0))
                    ) * 10                                                 AS s_score
                FROM options_data o, spot_cte s
                WHERE o.trade_date=? AND o.snap_time=? AND o.underlying=?
                  AND o.expiry_tier IN ('TIER1', 'TIER2')
                  AND ABS((o.strike_price - s.spot) / s.spot * 100) <= ?
                  AND ABS(o.delta) BETWEEN ? AND ?
                  AND o.oi * o.ltp / 1e7 >= ?
                  AND o.ltp > 0
                  {direction_clause}
            )
            SELECT *,
                CASE
                    WHEN s_score >= ? THEN 4
                    WHEN s_score >= ? THEN 3
                    WHEN s_score >= ? THEN 2
                    ELSE 1
                END AS stars
            FROM ranked
            ORDER BY s_score DESC
            LIMIT ?
        """, [
            trade_date, snap_time, underlying,
            settings.W_DELTA, settings.SCREENER_MIN_DELTA, settings.SCREENER_MAX_DELTA, settings.SCREENER_MIN_DELTA,
            settings.W_LIQUIDITY, settings.LIQUIDITY_CAP_CR.get(underlying, 10.0),
            settings.W_IV, iv_penalty_gate,
            settings.W_GAMMA,
            settings.W_VEGA,
            settings.W_EFF_RATIO, eff_cap,
            settings.W_MOMENTUM,
            trade_date, snap_time, underlying,
            settings.SCREENER_MAX_MONEYNESS_PCT,
            settings.SCREENER_MIN_DELTA, settings.SCREENER_MAX_DELTA,
            settings.SCREENER_MIN_LIQUIDITY_CR,
            # Append direction only when filter is active
            *([direction] if direction else []),
            settings.STAR_4_THRESHOLD, settings.STAR_3_THRESHOLD, settings.STAR_2_THRESHOLD,
            top_n,
        ])

        # Issue-R11: derive column names from the cursor description instead of
        # a hardcoded list.  If the SQL SELECT changes (add/remove a computed
        # column), the output dict automatically reflects it — no manual sync
        # needed and no silent value-shift bugs.
        cols = [d[0] for d in result.description]
        rows = result.fetchall()
        
        rows_out = [
            {k: (round(v, 4) if isinstance(v, float) else v)
             for k, v in zip(cols, r)}
            for r in rows
        ]
        
        # Add direction alignment flag for frontend visual cues.
        # direction_aligned = True when the option side matches the requested direction.
        # When direction=None, all rows default to True (no filtering context).
        if direction:
            for row_dict in rows_out:
                row_dict["direction_aligned"] = (row_dict.get("option_type") == direction)
        else:
            for row_dict in rows_out:
                row_dict["direction_aligned"] = True

        return rows_out
    except Exception as e:
        logger.warning("get_strikes internal error: {}", e, exc_info=True)
        raise
