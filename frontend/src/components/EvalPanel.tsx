import { useEffect, useState } from 'react'
import { api } from '../services/api'

type EvalStats = {
  total: number
  successes: number
  rate: number
  recent: {
    timestamp: number
    success: boolean
    checkpoint: string
    memo: string
    preset?: string
  }[]
  by_preset: { preset: string; total: number; successes: number; rate: number }[]
}

type Props = {
  checkpoint: string
  /** 어떤 설정으로 돌린 추론인지 — 없으면 "성공률 70%" 의 근거를 알 수 없다 */
  preset?: string
  params?: Record<string, unknown>
}

export default function EvalPanel({ checkpoint, preset = '', params }: Props) {
  const [stats, setStats] = useState<EvalStats | null>(null)
  const [memo, setMemo] = useState('')

  const fetchStats = () => {
    api.get<EvalStats>('/eval/stats').then(setStats).catch(() => {})
  }

  useEffect(() => {
    fetchStats()
  }, [])

  const logResult = async (success: boolean) => {
    await api.post('/eval/log', { success, checkpoint, memo, preset, params: params ?? {} })
    setMemo('')
    fetchStats()
  }

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
      <h3 className="text-sm font-semibold">평가</h3>
      {preset && (
        <p className="text-[10px] text-neutral-500">기록될 프리셋: {preset}</p>
      )}

      {/* 성공/실패 버튼 */}
      <div className="flex gap-2">
        <button
          onClick={() => logResult(true)}
          className="flex-1 py-2 rounded bg-green-600 hover:bg-green-500 text-white text-sm font-medium"
        >
          성공
        </button>
        <button
          onClick={() => logResult(false)}
          className="flex-1 py-2 rounded bg-red-600 hover:bg-red-500 text-white text-sm font-medium"
        >
          실패
        </button>
      </div>
      <input
        type="text"
        value={memo}
        onChange={(e) => setMemo(e.target.value)}
        placeholder="메모 (선택)"
        className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
      />

      {/* 통계 */}
      {stats && (
        <>
          <div className="flex gap-4 text-sm">
            <span className="text-neutral-400">
              전체: <span className="text-neutral-100">{stats.total}</span>
            </span>
            <span className="text-neutral-400">
              성공:{' '}
              <span className="text-green-400">{stats.successes}</span>
            </span>
            <span className="text-neutral-400">
              성공률:{' '}
              <span className="text-blue-400">
                {(stats.rate * 100).toFixed(1)}%
              </span>
            </span>
          </div>

          {/* 프리셋별 성공률 — "어느 설정이 잘 됐나" */}

          {stats && stats.by_preset?.length > 0 && (

            <div className="space-y-1">

              <span className="text-xs text-neutral-400">프리셋별</span>

              {stats.by_preset.map((p) => (

                <div key={p.preset} className="flex justify-between text-[11px]">

                  <span className="text-neutral-300">{p.preset}</span>

                  <span className={p.rate >= 0.7 ? 'text-green-400' : 'text-amber-400'}>

                    {Math.round(p.rate * 100)}% ({p.successes}/{p.total})

                  </span>

                </div>

              ))}

            </div>

          )}


          {/* 최근 기록 */}
          {stats.recent.length > 0 && (
            <div className="max-h-32 overflow-auto text-xs space-y-1">
              {stats.recent.map((r, i) => (
                <div
                  key={i}
                  className="flex gap-2 items-center text-neutral-400"
                >
                  <span
                    className={
                      r.success ? 'text-green-400' : 'text-red-400'
                    }
                  >
                    {r.success ? 'O' : 'X'}
                  </span>
                  <span>{new Date(r.timestamp * 1000).toLocaleTimeString()}</span>
                  {r.memo && <span className="truncate">{r.memo}</span>}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
