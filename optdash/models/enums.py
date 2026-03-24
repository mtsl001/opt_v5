"""All enumerations used across the OptDash codebase."""
from enum import Enum


class Direction(str, Enum):
    CE      = "CE"
    PE      = "PE"
    NEUTRAL = "NEUTRAL"


class GateVerdict(str, Enum):
    GO    = "GO"
    WAIT  = "WAIT"
    NO_GO = "NO_GO"


class MarketSession(str, Enum):
    OPENING_TURBULENCE = "OPENING_TURBULENCE"  # 09:15 – 09:30
    OPENING         = "OPENING"          # 09:15 – 10:15
    MIDMORNING      = "MIDMORNING"        # 10:15 – 11:30
    MIDDAY_CHOP     = "MIDDAY_CHOP"       # 11:30 – 13:00
    AFTERNOON       = "AFTERNOON"         # 13:00 – 14:30
    CLOSING_CRUSH   = "CLOSING_CRUSH"     # 14:30 – 15:30


class TradeStatus(str, Enum):
    GENERATED = "GENERATED"   # AI issued recommendation
    ACCEPTED  = "ACCEPTED"    # Trader accepted — position is live
    REJECTED  = "REJECTED"    # Trader manually rejected
    EXPIRED   = "EXPIRED"     # Not actioned within expiry window
    CLOSED    = "CLOSED"      # Position closed (any reason)


class ExitReason(str, Enum):
    TARGET_HIT        = "TARGET_HIT"
    SL_HIT            = "SL_HIT"
    # Fix-P1-10: TRAILING_STOP_HIT added as a distinct exit reason.
    # Previously tracker.py emitted SL_HIT as a proxy for trailing-stop exits,
    # conflating two different exit types and corrupting per-reason outcome
    # analysis in the learning engine (e.g. "pure SL_HIT" vs trailing stop).
    # tracker.py must now emit ExitReason.TRAILING_STOP_HIT when the trail fires.
    TRAILING_STOP_HIT = "TRAILING_STOP_HIT"
    THETA_SL_HIT      = "THETA_SL_HIT"
    GATE_NO_GO        = "GATE_NO_GO"
    IV_CRUSH          = "IV_CRUSH"
    MANUAL_EXIT       = "MANUAL_EXIT"
    EOD_FORCE         = "EOD_FORCE"


class RejectionReason(str, Enum):
    MANUAL_OVERRIDE   = "MANUAL_OVERRIDE"
    LOW_CONFIDENCE    = "LOW_CONFIDENCE"
    BAD_TIMING        = "BAD_TIMING"
    RISK_LIMIT        = "RISK_LIMIT"
    NEWS_EVENT        = "NEWS_EVENT"
    OTHER             = "OTHER"


class ShadowOutcome(str, Enum):
    CLEAN_MISS  = "CLEAN_MISS"    # Would have won ≥30% — costly rejection
    GOOD_SKIP   = "GOOD_SKIP"     # Would have lost ≥20% — correct rejection
    RISKY_MISS  = "RISKY_MISS"    # Mixed outcome
    BREAK_EVEN  = "BREAK_EVEN"    # |PnL| < 5%


class IVCrushSeverity(str, Enum):
    NONE = "NONE"
    LOW  = "LOW"
    HIGH = "HIGH"


class AlertType(str, Enum):
    COC_VELOCITY       = "COC_VELOCITY"
    GEX_DECLINE        = "GEX_DECLINE"
    PCR_DIVERGENCE     = "PCR_DIVERGENCE"
    OBI_SHIFT          = "OBI_SHIFT"
    VOLUME_SPIKE       = "VOLUME_SPIKE"
    GATE_CHANGE        = "GATE_CHANGE"
    # Fix A-1: these types were constructed as raw strings in alerts.py,
    # bypassing _make_alert() and breaking enum-based deduplication.
    HIGH_CONVICTION_BEAR = "HIGH_CONVICTION_BEAR"
    BELOW_ZGL            = "BELOW_ZGL"
    APPROACHING_ZGL      = "APPROACHING_ZGL"


class AlertSeverity(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


class TermStructureShape(str, Enum):
    CONTANGO      = "CONTANGO"
    FLAT          = "FLAT"
    BACKWARDATION = "BACKWARDATION"


class GEXRegime(str, Enum):
    POSITIVE_CHOP      = "POSITIVE_CHOP"       # GEX > 0 and above 70% of day peak
    POSITIVE_DECLINING = "POSITIVE_DECLINING"   # GEX > 0 but below 70% of day peak
    NEGATIVE_TREND     = "NEGATIVE_TREND"       # GEX < 0 (net dealer short gamma)


class VexSignal(str, Enum):
    VEX_BULLISH = "VEX_BULLISH"
    VEX_BEARISH = "VEX_BEARISH"
    NEUTRAL     = "NEUTRAL"


class CexSignal(str, Enum):
    STRONG_CHARM_BID = "STRONG_CHARM_BID"
    CHARM_BID        = "CHARM_BID"
    CHARM_PRESSURE   = "CHARM_PRESSURE"
    NEUTRAL          = "NEUTRAL"
