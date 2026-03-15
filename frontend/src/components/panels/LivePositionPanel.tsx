import { useState } from 'react'
import { useLivePosition, useCloseTrade } from '../../hooks/useAI'
import { useDashboardStore } from '../../store/dashboardStore'
import type { PositionLive } from '../../types'
import clsx from 'clsx'

export default function LivePositionPanel() {
  const { data, isLoading } = useLivePosition()
  const close = useCloseTrade()
  const { selectedSnapTime } = useDashboardStore()
  const [exitPrice, setExitPrice] = useState('')

  if (isLoading) {
    return (
      <div className="panel h-full">
        <div className="panel-title">Live Position</div>
        <div className="text-muted text-xs">… loading</div>
      </div>
    )
  }

  if (!data || (data as Record<string, unknown>).status === 'NO_POSITION') {
    return (
      <div className="panel h-full">
        <div className="panel-title">Live Position</div>
        <div className="text-muted text-xs mt-4 text-center">No open position</div>
      </div>
    )
  }

  // Issue-R5: typed cast instead of `as any` — all property accesses are
  // now compile-time checked against the PositionLive interface.
  const pos = data as PositionLive
  const pnlPct = pos.pnl_pct ?? 0
  const pnlClass = pnlPct >= 0 ? 'val-bull' : 'val-bear'
  const isCall = pos.option_type === 'CE'
  const typeClass = isCall ? 'text-bull-light' : 'text-bear-light'

  return (
    <div className="panel h-full">
      <div className="panel-title flex justify-between">
        <span>Live Position</span>
        {pos.gate_verdict && (
          <span className={clsx({
            'badge-go': pos.gate_verdict === 'GO',
            'badge-wait': pos.gate_verdict === 'WAIT',
            'badge-nogo': pos.gate_verdict === 'NO_GO',
          })}>{pos.gate_verdict}</span>
        )}
      </div>

      <div className="flex gap-4 mb-3">
        <div>
          <span className={clsx('text-base font-mono font-semibold', typeClass)}>
            {pos.strike_price.toLocaleString()} {pos.option_type}
          </span>
          <div className="text-muted text-xs mt-0.5">Entry: {pos.entry_premium?.toFixed(1)}</div>
        </div>
        <div className="ml-auto text-right">
          <div className={clsx('text-lg font-mono font-semibold', pnlClass)}>
            {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(1)}%
          </div>
          <div className="text-xs text-muted">
            ₹{pos.pnl_abs?.toFixed(0)} abs
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs mb-3">
        <div>
          <div className="text-muted">Current LTP</div>
          <div className="val-neutral">{pos.current_ltp?.toFixed(1) ?? '—'}</div>
        </div>
        <div>
          <div className="text-muted">SL (adjusted)</div>
          <div className="val-bear">{pos.sl_adjusted?.toFixed(1) ?? '—'}</div>
        </div>
      </div>

      {/* Manual close */}
      <div className="flex gap-2 mt-auto">
        <input
          type="number"
          placeholder="Exit price"
          value={exitPrice}
          onChange={e => setExitPrice(e.target.value)}
          className="flex-1 bg-bg-panel border border-border-dim rounded px-2 py-1 text-xs text-gray-300"
        />
        <button
          className="btn-danger"
          disabled={close.isPending || !exitPrice}
          onClick={() =>
            close.mutate({
              tradeId: pos.id,
              exitPrice: parseFloat(exitPrice),
              snapTime: selectedSnapTime,
            })
          }
        >
          {close.isPending ? 'Closing…' : 'Close'}
        </button>
      </div>
    </div>
  )
}
