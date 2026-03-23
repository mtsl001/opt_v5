"""Trade quality score — advisory composite, range 0–100.

Three components:
  C1: Strike quality  (S_score / 120, capped at 1.0)           — max 35
  C2: Gate adequacy   (gate_score / GATE_MAX_SCORE)             — max 35
  C3: Confidence      (confidence / 100)                        — max 30

Grades: A ≥ 80 | B ≥ 65 | C ≥ 50 | D < 50

S_score normalisation:
  Screener formula: (weighted sum of 7 factors) × 10
  Max possible = (W_DELTA×0.50 + W_EFF_RATIO + W_LIQUIDITY + W_IV
                  + W_GAMMA + W_VEGA + W_MOMENTUM×3.0) × 10 = 180
  Practical 99th-pct for screened options ≈ 120 → used as normaliser.
"""
from optdash.config import settings

# Issue-R17: derive normaliser dynamically from config weights so tuning
# any W_* in .env automatically updates quality grade normalisation.
# Practical 99th-pct is ~80% of theoretical max for well-screened options.
_SSCORE_MAX = (
    settings.W_DELTA * 0.50  # delta capped at SCREENER_MAX_DELTA (0.50)
    + settings.W_EFF_RATIO
    + settings.W_LIQUIDITY
    + settings.W_IV
    + settings.W_GAMMA
    + settings.W_VEGA
    + settings.W_MOMENTUM * 3.0
) * 10
_SSCORE_NORM = _SSCORE_MAX * 0.80  # practical 99th-pct range


def compute_quality_score(strike: dict, gate_score: int, confidence: int) -> dict:
    # C1: S_score normalised against practical 99th-pct range (0–120 → 0–1.0)
    sscore_norm = min(1.0, (strike.get("s_score") or 0) / _SSCORE_NORM)
    c1 = sscore_norm * 35

    # C2: Gate adequacy — guard against misconfigured GATE_MAX_SCORE=0
    gate_max = settings.GATE_MAX_SCORE or 10
    c2 = min(35, (gate_score / gate_max) * 35)

    # C3: Confidence adequacy
    c3 = min(30, (confidence / 100) * 30)

    quality = int(c1 + c2 + c3)
    grade = (
        "A" if quality >= 80 else
        "B" if quality >= 65 else
        "C" if quality >= 50 else
        "D"
    )
    return {"quality_score": quality, "grade": grade}
