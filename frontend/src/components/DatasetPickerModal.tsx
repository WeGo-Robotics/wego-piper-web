import type { Dataset } from '../types/models'
import DatasetBrowser from './DatasetBrowser'
import { useT } from '../i18n'

/**
 * 데이터셋 고르는 창 — 에피소드 페이지의 [선택하기]. 목록은 `DatasetBrowser`(표 | 썸네일).
 *
 * ⚠ **React 창이다** — `window.confirm` 류는 E-stop heartbeat 를 막는다.
 * ⚠ **Esc 로 닫지 않는다** — Esc 는 전역 E-stop 이다(EStopButton). ✕ 와 바깥 클릭으로 닫는다.
 */
type Props = {
  datasets: Dataset[]
  value: string
  onPick: (id: string) => void
  onClose: () => void
  /** 이름을 고친 뒤 — 부모가 목록을 다시 읽는다 */
  onRenamed?: () => void
}

export default function DatasetPickerModal({ datasets, value, onPick, onClose, onRenamed }: Props) {
  const t = useT()
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="flex max-h-[88vh] w-full max-w-6xl flex-col rounded-lg border border-neutral-700 bg-neutral-900"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 border-b border-neutral-800 px-4 py-3">
          <h3 className="text-sm font-semibold text-neutral-100">{t('데이터셋 선택')}</h3>
          <button type="button" onClick={onClose} title={t('닫기')}
            className="ml-auto rounded px-2 py-0.5 text-neutral-400 hover:bg-neutral-800 hover:text-white">✕</button>
        </div>
        <div className="min-h-0 overflow-y-auto p-4">
          <DatasetBrowser datasets={datasets} value={value} onRenamed={onRenamed}
            onOpen={(d) => { onPick(d.id); onClose() }} />
        </div>
      </div>
    </div>
  )
}
