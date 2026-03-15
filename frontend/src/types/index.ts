// ── Market ───────────────────────────────────────────────────────────────────
export interface SpotData {
  snap_time: string
  spot: number
  day_open: number
  day_high: number
  day_low: number
  change_pct: number
}

export interface GEXRow {
  snap_time: string
  gex_all_B: number
  gex_near_B: number
  gex_far_B: number
  pct_of_peak: number
  // Issue-R2: added POSITIVE_DECLINING — backend GEXRegime enum has 3 variants.
  regime: 'POSITIVE_CHOP' | 'POSITIVE_DECLINING' | 'NEGATIVE_TREND'
}

export interface CoCRow {
  snap_time: string
  fut_price: number
  spot: number
  coc: number
  v_coc_15m: number
  signal: 'VELOCITY_BULL' | 'VELOCITY_BEAR' | 'DISCOUNT' | 'NORMAL'
}

// ── Environment Gate ─────────────────────────────────────────────────────

export type GateVerdict = 'GO' | 'WAIT' | 'NO_GO'

export interface GateCondition {
  met: boolean
  value: number | string
  points: number
  note?: string
}

export interface EnvironmentScore {
  score: number
  max_score: number
  verdict: GateVerdict
  conditions: Record<string, GateCondition>
  session: string
}

// ── Strike Screener ─────────────────────────────────────────────────────────
// Issue-R3: removed phantom `rho` (not in API/BQ feed, excluded from screener SQL).
// Added `expiry_tier` which backend screener.py actually returns.
export interface StrikeRow {
  expiry_date: string
  expiry_tier: string
  dte: number
  option_type: 'CE' | 'PE'
  strike_price: number
  ltp: number
  iv: number
  delta: number
  theta: number
  gamma: number
  vega: number
  moneyness_pct: number
  eff_ratio: number
  s_score: number
  liquidity_cr: number
  stars: 1 | 2 | 3 | 4
}

// ── PCR ─────────────────────────────────────────────────────────────────────
export interface PCRRow {
  snap_time: string
  pcr_vol: number
  pcr_oi: number
  pcr_divergence: number
  smoothed_obi: number
  signal: 'RETAIL_PANIC_PUTS' | 'DIVERGENCE_BUILDING' | 'RETAIL_PANIC_CALLS' | 'BALANCED'
}

// ── Volume Velocity ─────────────────────────────────────────────────────────
export interface VolumeVelocityRow {
  snap_time: string
  vol_total: number
  baseline_vol: number
  volume_ratio: number
  signal: 'SPIKE' | 'NORMAL'
}

// ── IV Term Structure ────────────────────────────────────────────────────────
export type TermStructureShape = 'CONTANGO' | 'FLAT' | 'BACKWARDATION'
export interface TermStructureRow {
  expiry_date: string
  dte: number
  expiry_tier: string
  atm_iv: number
  avg_theta: number
}
export interface TermStructureResponse {
  series: TermStructureRow[]
  shape: TermStructureShape
  near_iv: number
  far_iv: number
}

// ── VEX / CEX ───────────────────────────────────────────────────────────────
export interface VexCexRow {
  snap_time: string
  vex_total_M: number
  vex_ce_M: number
  vex_pe_M: number
  cex_total_M: number
  spot: number
  vex_signal: string
  cex_signal: string
  dealer_oclock: boolean
  interpretation: string
}
export interface VexCexResponse {
  series: VexCexRow[]
  current: VexCexRow | null
  dealer_oclock: boolean
  interpretation: string
}

// ── Alerts ───────────────────────────────────────────────────────────────────
export type AlertSeverity = 'HIGH' | 'MEDIUM' | 'LOW'
export interface Alert {
  time: string
  type: string
  severity: AlertSeverity
  direction: 'CE' | 'PE' | null
  headline: string
  message: string
}

// ── AI Trade Card ──────────────────────────────────────────────────────────
export interface TradeCard {
  id: number
  trade_date: string
  snap_time: string
  underlying: string
  option_type: 'CE' | 'PE'
  strike_price: number
  expiry_date: string
  dte: number
  entry_premium: number
  sl_price: number
  target_price: number
  confidence: number
  gate_score: number
  gate_verdict: GateVerdict
  s_score: number
  quality_grade: 'A' | 'B' | 'C' | 'D'
  narrative: string
  status: string
}

export interface PositionLive {
  id: number
  underlying: string
  option_type: 'CE' | 'PE'
  strike_price: number
  entry_premium: number
  current_ltp: number | null
  pnl_abs: number | null
  pnl_pct: number | null
  sl_adjusted: number | null
  theta_sl_status: string | null
  gate_score: number | null
  gate_verdict: GateVerdict | null
}
