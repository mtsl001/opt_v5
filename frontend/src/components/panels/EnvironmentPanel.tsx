import { useEnvironment } from '../../hooks/useMarket'
import clsx from 'clsx'

// Issue-10: labels must match the exact condition keys returned by
// get_environment_score() in environment.py (C1–C10).  The previous
// labels were completely wrong — none matched the backend keys, causing
// the panel to fall back to displaying raw keys like "gex_declining".
const CONDITION_LABELS: Record<string, string> = {
  gex_declining: 'GEX Declining',       // C1: GEX % of peak ≤ threshold
  vcoc_signal: 'V_CoC Signal',        // C2: V_CoC velocity spike
  fut_bs_ratio: 'Futures Flow',        // C3: Futures OBI directional
  pcr_divergence: 'PCR Divergence',      // C4: Put-Call divergence
  ivp_cheap: 'IV Cheap',            // C5: IVP < 50th percentile
  obi_negative: 'ATM OBI',             // C6: ATM order book imbalance
  term_structure_ok: 'Term Structure',      // C7: Not in backwardation
  session_ok: 'Session',             // C8: Not midday chop
  vex_aligned: 'VEX Aligned ★★',     // C9: VEX mechanical alignment (2 pts)
  not_charm_distortion: "No Dealer O'Clock",   // C10: Charm distortion guard
}

export default function EnvironmentPanel() {
  const { data: env, isLoading } = useEnvironment()

  if (isLoading || !env) {
    return (
      <div className="panel h-full">
        <div className="panel-title">Environment Gate</div>
        <div className="text-muted text-xs">… loading</div>
      </div>
    )
  }

  const verdictClass =
    env.verdict === 'GO' ? 'badge-go' :
      env.verdict === 'WAIT' ? 'badge-wait' : 'badge-nogo'

  return (
    <div className="panel h-full">
      <div className="panel-title flex justify-between">
        <span>Environment Gate</span>
        <span className={verdictClass}>{env.verdict}</span>
      </div>

      {/* Score bar */}
      <div className="flex items-center gap-2 mb-3">
        <div className="flex-1 bg-bg-panel rounded-full h-2">
          <div
            className={clsx('h-2 rounded-full transition-all', {
              'bg-bull-light': env.verdict === 'GO',
              'bg-accent': env.verdict === 'WAIT',
              'bg-bear-light': env.verdict === 'NO_GO',
            })}
            style={{ width: `${(env.score / env.max_score) * 100}%` }}
          />
        </div>
        <span className="font-mono text-xs text-gray-300">{env.score}/{env.max_score}</span>
      </div>

      {/* Conditions grid */}
      <div className="grid grid-cols-2 gap-x-3 gap-y-1">
        {Object.entries(env.conditions).map(([key, cond]) => (
          <div key={key} className="flex items-center gap-1.5 text-xs">
            <span className={cond.met ? 'text-bull-light' : 'text-bear-light'}>
              {cond.met ? '✓' : '✗'}
            </span>
            <span className="text-muted">{CONDITION_LABELS[key] ?? key}</span>
            <span className="ml-auto text-gray-400 font-mono">
              {typeof cond.value === 'number' ? cond.value.toFixed(1) : cond.value}
            </span>
          </div>
        ))}
      </div>

      <div className="mt-2 text-xs text-muted">
        Session: <span className="text-gray-300">{env.session}</span>
      </div>
    </div>
  )
}
