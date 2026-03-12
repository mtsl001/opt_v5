"""Directional bias engine -- weighted signal voting.

Signal weights:
  V_CoC velocity spike  -> weight 3  (strongest: order-flow momentum)
  Futures OBI           -> weight 2  (institutional positioning)
  VEX alignment         -> weight 2  (dealer mechanical flow)
  ATM OBI               -> weight 1  (options order imbalance)
  PCR divergence        -> weight 1  (retail sentiment contra-indicator)

Max CE/PE weight = 9 (all signals same direction).
Ties (ce_weight == pe_weight) yield NEUTRAL -- no edge when signals cancel.

Fix-G: get_directional_bias() now includes 'vex_data' in its return dict
so callers (recommender.py) can read the already-computed VEX snapshot
instead of issuing a second get_vex_cex_current() round-trip.
"""
import duckdb
from loguru import logger
from optdash.config import settings
from optdash.models import Direction
from optdash.analytics.coc import get_coc_latest, get_atm_obi, get_futures_obi
from optdash.analytics.vex_cex import get_vex_cex_current
from optdash.analytics.pcr import get_pcr


def get_directional_bias(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> dict:
    try:
        coc  = get_coc_latest(conn, trade_date, snap_time, underlying)
        vex  = get_vex_cex_current(conn, trade_date, snap_time, underlying)
        obi  = get_atm_obi(conn, trade_date, snap_time, underlying)
        pcr  = get_pcr(conn, trade_date, snap_time, underlying)
        fobi = get_futures_obi(conn, trade_date, snap_time, underlying)

        signals: list[dict] = []

        # ── Signal 1: V_CoC velocity (weight 3) ───────────────────────────────────────────
        vcoc        = coc.get("v_coc_15m") or 0
        vcoc_active = _is_vcoc_spike_active(conn, trade_date, snap_time, underlying)
        if vcoc > settings.VCOC_BULL_THRESHOLD or (vcoc_active and vcoc > 0):
            signals.append({"signal": "VCOC_BULL", "weight": 3,
                             "direction": Direction.CE.value, "value": vcoc})
        elif vcoc < settings.VCOC_BEAR_THRESHOLD or (vcoc_active and vcoc < 0):
            signals.append({"signal": "VCOC_BEAR", "weight": 3,
                             "direction": Direction.PE.value, "value": vcoc})

        # ── Signal 2: Futures OBI (weight 2) ───────────────────────────────────────
        # FUT_OBI_BEAR_THRESHOLD is a per-underlying dict -- resolve before comparison.
        fut_obi_thr = settings.FUT_OBI_BEAR_THRESHOLD.get(underlying, -0.20)
        if fobi < fut_obi_thr:
            signals.append({"signal": "FUT_OBI_BEAR", "weight": 2,
                             "direction": Direction.PE.value, "value": fobi})
        elif fobi > abs(fut_obi_thr):
            signals.append({"signal": "FUT_OBI_BULL", "weight": 2,
                             "direction": Direction.CE.value, "value": fobi})

        # ── Signal 3: VEX alignment (weight 2) ──────────────────────────────────────
        # Fix-E: use per-underlying threshold (same dict as _classify_vex)
        # instead of bare > 0 / < 0. This prevents weight-2 ghost votes when
        # VEX noise oscillates around zero on low-liquidity underlyings.
        vex_total = vex.get("vex_total_M", 0) or 0
        vex_thr   = settings.VEX_THRESHOLDS.get(underlying, settings.VEX_BULL_THRESHOLD)
        if vex_total > vex_thr:
            signals.append({"signal": "VEX_BULL", "weight": 2,
                             "direction": Direction.CE.value, "value": vex_total})
        elif vex_total < -vex_thr:
            signals.append({"signal": "VEX_BEAR", "weight": 2,
                             "direction": Direction.PE.value, "value": vex_total})

        # ── Signal 4: ATM OBI (weight 1) ────────────────────────────────────────────
        if obi > settings.OBI_THRESHOLD:
            signals.append({"signal": "OBI_BULL", "weight": 1,
                             "direction": Direction.CE.value, "value": obi})
        elif obi < -settings.OBI_THRESHOLD:
            signals.append({"signal": "OBI_BEAR", "weight": 1,
                             "direction": Direction.PE.value, "value": obi})

        # ── Signal 5: PCR divergence (weight 1) ───────────────────────────────────────
        div = pcr.get("pcr_divergence", 0)
        if div > settings.PCR_DIV_BULL_THRESHOLD:
            signals.append({"signal": "PCR_RETAIL_PUTS", "weight": 1,
                             "direction": Direction.CE.value, "value": div})
        elif div < settings.PCR_DIV_BEAR_THRESHOLD:
            signals.append({"signal": "PCR_RETAIL_CALLS", "weight": 1,
                             "direction": Direction.PE.value, "value": div})

        ce_weight = sum(s["weight"] for s in signals if s["direction"] == Direction.CE.value)
        pe_weight = sum(s["weight"] for s in signals if s["direction"] == Direction.PE.value)

        # No signals at all
        if ce_weight == 0 and pe_weight == 0:
            return {"direction": Direction.NEUTRAL.value, "ce_weight": 0,
                    "pe_weight": 0, "margin": 0, "signals": [],
                    "vex_data": vex}

        # Tie -- contradictory signals cancel, no tradeable edge
        if ce_weight == pe_weight:
            return {
                "direction": Direction.NEUTRAL.value,
                "ce_weight": ce_weight,
                "pe_weight": pe_weight,
                "margin":    0,
                "signals":   signals,
                "vex_data":  vex,
            }

        direction = Direction.CE.value if ce_weight > pe_weight else Direction.PE.value
        return {
            "direction": direction,
            "ce_weight": ce_weight,
            "pe_weight": pe_weight,
            "margin":    abs(ce_weight - pe_weight),
            "signals":   signals,
            # Fix-G: expose vex_data so recommender.py can read the already-computed
            # VEX snapshot without a second get_vex_cex_current() round-trip.
            "vex_data":  vex,
        }

    except Exception as e:
        # logger.exception appends full traceback automatically (Loguru).
        # underlying/trade_date/snap_time in the message make every silent
        # NEUTRAL in production logs attributable to a specific instrument
        # and time window.
        # vex may not be bound if exception occurred before get_vex_cex_current();
        # recommender.py falls back to a fresh fetch via the key-presence check.
        logger.exception(
            "get_directional_bias failed for {}/{}/{}: {}",
            underlying, trade_date, snap_time, e,
        )
        return {"direction": Direction.NEUTRAL.value, "ce_weight": 0,
                "pe_weight": 0, "margin": 0, "signals": []}


def _is_vcoc_spike_active(
    conn:       duckdb.DuckDBPyConnection,
    trade_date: str,
    snap_time:  str,
    underlying: str,
) -> bool:
    """True if any of the last VCOC_SPIKE_EXPIRY_SNAPS snaps had a V_CoC spike.

    Fix-F: replaced N+1 query pattern with a single batch fetch.

    Algorithm:
      1. Compute the earliest snap we could need as a V_CoC lookback anchor:
           earliest = snap_time - (N * 5 min) - 15 min
      2. Fetch all CoC values (fut_price - spot) in [earliest, snap_time]
         in one query, sorted ASC.
      3. For each of the N most-recent target snaps (in-memory slice),
         find the oldest snap in its [t-15min, t) window (the anchor),
         compute vcoc = |coc(t) - coc(anchor)|.
      4. Return True on the first snap where vcoc > VCOC_BULL_THRESHOLD.

    Query cost: 1 (was 1 + 2N = 7 for N=3; 85% reduction per underlying).
    """
    try:
        n         = settings.VCOC_SPIKE_EXPIRY_SNAPS
        threshold = abs(settings.VCOC_BULL_THRESHOLD)

        # Issue-5: derive snap interval from config instead of hardcoding 5.
        interval  = max(1, settings.SCHEDULER_INTERVAL_SECONDS // 60)

        # Compute earliest minute we need for any 15-min lookback anchor
        h, m         = map(int, snap_time.split(":"))
        earliest_min = max(0, h * 60 + m - n * interval - 15)
        cutoff       = f"{earliest_min // 60:02d}:{earliest_min % 60:02d}"

        rows = conn.execute("""
            SELECT snap_time, AVG(fut_price) - AVG(spot) AS coc
            FROM options_data
            WHERE trade_date=? AND underlying=? AND instrument_type='FUT'
              AND snap_time <= ? AND snap_time >= ?
            GROUP BY snap_time ORDER BY snap_time ASC
        """, [trade_date, underlying, snap_time, cutoff]).fetchall()

        if len(rows) < 2:
            return False

        coc_map    = {r[0]: (r[1] or 0.0) for r in rows}  # snap -> coc
        all_times  = [r[0] for r in rows]                  # ASC sorted
        # N most-recent target snaps are the last N items in the ASC list
        target_snaps = all_times[-n:]

        for t in reversed(target_snaps):   # most-recent first; exit early on spike
            h2, m2 = map(int, t.split(":"))
            t_min  = h2 * 60 + m2
            if t_min < 15:
                continue
            window_cutoff = f"{(t_min - 15) // 60:02d}:{(t_min - 15) % 60:02d}"
            # Oldest available snap inside the 15-min window before t
            anchor = next(
                (st for st in all_times if window_cutoff <= st < t),
                None,
            )
            if anchor is None:
                continue
            if abs(coc_map[t] - coc_map[anchor]) > threshold:
                return True

        return False

    except Exception:
        return False
