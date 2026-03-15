import { useQuery } from '@tanstack/react-query'
import { useDashboardStore, POLL } from '../store/dashboardStore'
import { fetchStrikes, fetchTermStructure } from '../api/screener'

export function useStrikes(topN = 20) {
  const { underlying, tradeDate, selectedSnapTime, snapMode } = useDashboardStore()
  return useQuery({
    queryKey: ['strikes', tradeDate, selectedSnapTime, underlying, topN],
    queryFn: () => fetchStrikes(tradeDate, selectedSnapTime, underlying, topN),
    // Issue-R14: use LIVE interval when in LIVE mode so screener data stays
    // current.  Previously only POLL.SLOW (30s) was used regardless of mode,
    // causing stale strike data during active trading.
    refetchInterval: snapMode === 'LIVE' ? POLL.LIVE : POLL.SLOW,
    staleTime: 0,
  })
}

export function useTermStructure() {
  const { underlying, tradeDate, selectedSnapTime } = useDashboardStore()
  return useQuery({
    queryKey: ['termstructure', tradeDate, selectedSnapTime, underlying],
    queryFn: () => fetchTermStructure(tradeDate, selectedSnapTime, underlying),
    refetchInterval: POLL.SLOW,
    staleTime: 0,
  })
}
