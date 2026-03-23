"""Template-based trade narrative — no LLM, every sentence backed by data."""
from optdash.config import settings
from optdash.models import MarketSession


def build_narrative(
    direction:         str,
    conviction:        str,
    pcr_modifier:      float,
    gate_score:        int,
    gate_verdict:      str,
    direction_signals: list[dict],
    iv_data:           dict,
    gex_data:          dict,
    vex_data:          dict,
    session:           MarketSession,
    dealer_oclock:     bool,
) -> str:
    parts = []

    if conviction == "STRONG":
        parts.append("STRONG CONVICTION setup — all institutional signals aligned.")
    else:
        parts.append(f"{conviction} setup — proceed with smaller size.")

    if abs(pcr_modifier - 1.0) > 0.05:
        parts.append(f"PCR multiplier at {pcr_modifier:.2f}x adjusting context.")

    # Guard: skip top-signal sentence if value is None (signal unavailable)
    top_signal = max(direction_signals, key=lambda s: s["weight"], default=None)
    if top_signal and top_signal.get("value") is not None:
        sig  = top_signal["signal"]
        val  = top_signal["value"]
        bull = direction == "CE"

        if sig.startswith("VCOC"):
            parts.append(
                f"V_CoC velocity at {val:+.1f} signals "
                f"{'institutional long accumulation' if bull else 'institutional unwinding'}."
            )
        elif sig.startswith("VEX"):
            parts.append(
                f"VEX at {val:+.1f}M — IV drop mechanics force dealer "
                f"{'buying' if bull else 'selling'} ({'bullish' if bull else 'bearish'} bias)."
            )
        elif sig.startswith("PCR"):
            parts.append(
                f"PCR divergence {val:+.2f}: "
                f"{'retail panic puts signal smart money bullish positioning' if bull else 'retail call euphoria — fade signal'}."
            )
        elif sig.startswith("FUT_OBI"):
            parts.append(
                f"Futures OBI {val:+.3f} confirms "
                f"{'institutional buying pressure' if bull else 'institutional selling pressure'}."
            )
        elif sig.startswith("OBI"):
            parts.append(
                f"ATM order book imbalance {val:+.4f} — "
                f"{'call side absorbing flow' if bull else 'put side absorbing flow'}."
            )

    parts.append(f"Environment gate {gate_score}/{settings.GATE_MAX_SCORE} ({gate_verdict}).")

    ivp = iv_data.get("ivp")
    if ivp is not None:
        regime = "cheap (buy premium)" if ivp < 50 else "elevated (premium is expensive)"
        parts.append(f"IV at {ivp:.0f}th percentile — {regime}.")

    pct_peak = gex_data.get("pct_of_peak")
    if pct_peak is not None and pct_peak < 70:
        parts.append(
            f"GEX at {pct_peak:.0f}% of day peak — "
            "gamma support reduced, directional move easier."
        )

    if dealer_oclock:
        parts.append("⚠ DEALER O’CLOCK active — charm flows dominate near expiry.")

    return " ".join(parts)
