"""Strike screener — S_score ranking with star ratings.

S_score (0–~150) is a weighted composite of 7 independent factors:
  1. delta        — directional sensitivity (proximity to ATM)
  2. theta ratio  — time-decay efficiency (W_THETA cap: 5% daily decay)
  3. liquidity    — OI × LTP in Cr (capped at 5 Cr)
  4. IV           — lower IV preferred for entry (cap at 100)
  5. gamma        — convexity / acceleration (capped at 0.01)
  6. vega         — IV sensitivity (capped at 50)
  7. eff_ratio    — theta/LTP efficiency at 10% cap (different from W_THETA's 5%)

Theoretical max = (W_DELTA×0.50 + W_EFF_RATIO + W_LIQUIDITY + W_IV
                   + W_THETA + W_GAMMA + W_VEGA) × 10
               = (2.0 + 4.0 + 3.0 + 2.0 + 2.0 + 1.0 + 1.0) × 10 = 150
delta is capped at 0.50 by the SCREENER_MAX_DELTA filter.
Typical well-screened option scores 70–120.
"""
import duckdb
from loguru import logger
from optdash.config import settings


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
                    ABS(o.theta) / NULLIF(o.ltp, 0)                       AS eff_ratio,
                    (
                        -- 1. Delta: directional sensitivity
                        ? * ABS(o.delta)
                        -- 2. Theta efficiency (cap at 5% daily decay ratio)
                      + ? * (1.0 - LEAST(1.0, ABS(o.theta) / NULLIF(o.ltp, 0) / 0.05))
                        -- 3. Liquidity (cap at 5 Cr OI×LTP)
                      + ? * LEAST(1.0, o.oi * o.ltp / 1e7 / 5.0)
                        -- 4. IV: lower is better (cap at 100)
                      + ? * (1.0 - LEAST(1.0, o.iv / 100.0))
                        -- 5. Gamma: convexity (cap at 0.01)
                      + ? * LEAST(1.0, ABS(o.gamma) * 100)
                        -- 6. Vega: IV sensitivity (cap at 50)
                      + ? * LEAST(1.0, ABS(o.vega) / 50.0)
                        -- 7. Eff-ratio: theta/LTP at 10% cap (distinct from term 2)
                      + ? * (1.0 - LEAST(1.0, ABS(o.theta) / NULLIF(o.ltp, 0) / 0.10))
                    ) * 10                                                 AS s_score
                FROM options_data o, spot_cte s
                WHERE o.trade_date=? AND o.snap_time=? AND o.underlying=?
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
            settings.W_DELTA, settings.W_THETA, settings.W_LIQUIDITY,
            settings.W_IV,    settings.W_GAMMA,  settings.W_VEGA, settings.W_EFF_RATIO,
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
        return [
            {k: (round(v, 4) if isinstance(v, float) else v)
             for k, v in zip(cols, r)}
            for r in rows
        ]
    except Exception as e:
        logger.warning("get_strikes error: {}", e)
        return []
