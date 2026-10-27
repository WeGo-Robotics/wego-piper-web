import { useEffect, useState } from 'react'
import { api } from '../services/api'
import type { WeightCheckpoint, WeightRun } from '../types/models'
import WeightTable from './WeightTable'
import { useT } from '../i18n'

/**
 * 가중치 표를 창으로 띄워 고른다 — 추론의 체크포인트, 학습의 Fine-tune 모델.
 *
 * ⚠ **React 창이다.** `window.confirm` 류는 이벤트 루프를 막아 E-stop heartbeat 가
 *   끊기고, 2초 뒤 돌던 추론이 SIGKILL 된다. 추론 중에 열어도 안전해야 한다.
 * ⚠ **Esc 로 닫지 않는다.** Esc 는 전역 E-stop 이다(EStopButton) — 창을 닫으려던
 *   손이 로봇을 세운다. ✕ 와 바깥 클릭으로 닫는다.
 */
type Props = {
  title: string
  value?: string
  onPick: (ckpt: WeightCheckpoint, row: WeightRun) => void
  onClose: () => void
  /** 아예 안 보일 행 (정책이 아닌 모델 등) */
  include?: (row: WeightRun) => boolean
  blockedReason?: (row: WeightRun) => string | null
}

export default function WeightTableModal({ title, value, onPick, onClose, include, blockedReason }: Props) {
  const t = useT()
  const [rows, setRows] = useState<WeightRun[] | null>(null)
  const [error, setError] = useState('')

  const load = () => {
    api.get<WeightRun[]>('/models/runs')
      .then((r) => { setRows(r); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : t('목록을 읽지 못했습니다')))
  }
  useEffect(load, [])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="flex max-h-[88vh] w-full max-w-7xl flex-col rounded-lg border border-neutral-700 bg-neutral-900"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 border-b border-neutral-800 px-4 py-3">
          <h3 className="text-sm font-semibold text-neutral-100">{title}</h3>
          <span className="text-xs text-neutral-500">{t('행을 누르면 마지막 체크포인트, 칩을 누르면 그 체크포인트를 고릅니다')}</span>
          <button type="button" onClick={onClose} title={t('닫기')}
            className="ml-auto rounded px-2 py-0.5 text-neutral-400 hover:bg-neutral-800 hover:text-white">✕</button>
        </div>
        <div className="min-h-0 overflow-y-auto p-4">
          {error ? <p className="text-sm text-red-300">{error}</p>
            : rows == null ? <p className="text-sm text-neutral-400">{t('스캔 중...')}</p>
            : <WeightTable rows={include ? rows.filter(include) : rows} value={value}
                blockedReason={blockedReason} onRenamed={load}
                onPick={(c, r) => { onPick(c, r); onClose() }} />}
        </div>
      </div>
    </div>
  )
}
