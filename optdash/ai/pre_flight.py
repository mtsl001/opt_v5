"""Pre-flight checks -- 7 hard blocking rules."""
from optdash.config import settings
from optdash.models import MarketSession


def run_pre_flight(
    gate_score:    int,
    confidence:    int,
    strike:        dict,
    gex_data:      dict,
    dealer_oclock: bool,
) -> tuple[bool, list[str]]:
    """Returns (passed: bool, failures: list[str])"""
    failures = []
    dte = strike.get("dte")

    # Rule 1: Gate score floor
    if gate_score < settings.PREFLIGHT_MIN_GATE_SCORE:
        failures.append(
            f"Gate {gate_score} below minimum {settings.PREFLIGHT_MIN_GATE_SCORE}"
        )

    # Rule 2: Confidence floor
    if confidence < settings.PREFLIGHT_MIN_CONFIDENCE:
        failures.append(
            f"Confidence {confidence}% below minimum {settings.PREFLIGHT_MIN_CONFIDENCE}%"
        )

    # Rule 3: Theta/premium ratio -- DTE-scaled
    theta = abs(strike.get("theta") or 0)
    ltp   = strike.get("ltp") or 0

    if dte is not None and dte <= 0:
        # Fix L-1: theta check intentionally skipped on DTE=0 (expiry morning).
        # The theta/ltp ratio becomes numerically explosive when LTP approaches
        # zero on deep-OTM strikes at expiry — the ratio loses all signal value.
        # Alternative risk controls for DTE=0 are: Rule 7 (gate floor), Rule 8
        # (confidence floor), and the DTE=0 Dealer O'Clock hard block.
        theta_cap = None
    elif dte is not None and dte <= 2:
        theta_cap = settings.PREFLIGHT_THETA_RATIO_DTE12
    else:
        theta_cap = settings.PREFLIGHT_MAX_THETA_RATIO

    if theta_cap is not None and ltp > 0 and (theta / ltp) > theta_cap:
        failures.append(
            f"Theta/premium {theta/ltp:.1%} exceeds {theta_cap:.0%} "
            f"(DTE={dte}) -- excessive daily decay vs premium"
        )

    # Rule 4: Max Pain proximity -- P4-F7: explicit None check so 0.0 (spot
    # exactly on max pain -- the most magnetically dangerous location) is NOT
    # coerced to 1.0 by the falsy `or` operator, which previously silenced the
    # proximity block entirely when spot == max pain.
    # Absent distance defaults to 99.0 (safely far) so the guard only fires
    # on real proximity data, not on missing data.
    raw_dist      = gex_data.get("max_pain_distance_pct")
    max_pain_dist = raw_dist if raw_dist is not None else 99.0
    if abs(max_pain_dist) < settings.PREFLIGHT_MAX_PAIN_PROXIMITY_PCT:
        failures.append(
            f"Spot within {abs(max_pain_dist):.2f}% of max pain "
            f"(threshold {settings.PREFLIGHT_MAX_PAIN_PROXIMITY_PCT:.1f}%) -- stop-hunt zone"
        )

    # Rule 5: S_score floor
    if (strike.get("s_score") or 0) < settings.PREFLIGHT_MIN_SSCORE:
        failures.append(
            f"S_score {strike.get('s_score', 0):.1f} below floor {settings.PREFLIGHT_MIN_SSCORE}"
        )

    # Rule 6 removed: the open-trade guard is enforced by generate_recommendation()
    # before run_pre_flight() is ever called (recommender.py lines ~38-40).
    # existing_open_trades was always 0 here -- the check could never fire.

    # Rule 7: DTE<=1 elevated requirements -- P4-F6: covers DTE=0 (expiry morning,
    # 09:15-14:00) as well as DTE=1 (day before expiry). The old strict `== 1`
    # check left DTE=0 completely unguarded -- the highest-risk expiry window.
    # Guarded with explicit None check: if screener omits DTE, we skip the rule
    # rather than blocking every trade on missing data.
    if dte is not None and dte <= 1:
        if gate_score < settings.PREFLIGHT_DTE1_MIN_GATE:
            failures.append(
                f"DTE<=1 requires gate >= {settings.PREFLIGHT_DTE1_MIN_GATE}, got {gate_score}"
            )
        if confidence < settings.PREFLIGHT_DTE1_MIN_CONFIDENCE:
            failures.append(
                f"DTE<=1 requires confidence >= {settings.PREFLIGHT_DTE1_MIN_CONFIDENCE}%, "
                f"got {confidence}%"
            )
        direction_margin = gex_data.get("direction_margin", 0)  # pass from recommender
        if direction_margin < settings.PREFLIGHT_DTE1_MIN_MARGIN:  # e.g. 5
            failures.append(
                f"DTE<=1 requires margin >= {settings.PREFLIGHT_DTE1_MIN_MARGIN}, got {direction_margin}"
            )

    # Rule 8 (renumbered 7 after Rule 6 removal): Dealer O'Clock hard block on DTE<=1
    # Fix PF-1: explicit None guard so DTE=0 (expiry morning, highest-risk window)
    # is correctly blocked. The previous `or 99` coercion treated 0 as falsy and
    # substituted 99, making the rule permanently skip on expiry morning even
    # though DTE=0 is exactly the condition it was designed to catch.
    # Absent DTE (None) still defaults to 99 -- rule skips on missing data as intended.
    if dealer_oclock and (dte if dte is not None else 99) <= 1:
        failures.append(
            "DEALER O'CLOCK on expiry day -- charm distortion blocks entry"
        )

    return (len(failures) == 0, failures)
