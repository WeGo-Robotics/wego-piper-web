import type { ReactNode } from 'react'
import { useT } from '../i18n'

/**
 * 창에서 고르는 칸 — select 를 없앤 자리(추론의 체크포인트, 학습의 Fine-tune).
 *
 * ⚠ **입력칸처럼 보여야 한다.** 고른 것의 글자만 두었더니 "처음부터 학습" 이 안내인지 값인지,
 *   눌러야 하는지 아닌지가 안 읽혔다. 다른 입력칸과 같은 테두리 상자로 두고, 상자 전체를
 *   누르면 창이 열린다. 비었으면 회색 안내문(입력칸의 placeholder 와 같은 뜻)이다.
 */
export default function PickerField({ empty, placeholder, onOpen, onClear, clearTitle, children }: {
  /** 아무것도 안 골랐나 — 그러면 `placeholder` 를 회색으로 */
  empty: boolean
  placeholder: string
  onOpen: () => void
  /** 있으면 고른 것을 비우는 ✕ 를 그린다 (고르지 않아도 되는 칸) */
  onClear?: () => void
  clearTitle?: string
  children?: ReactNode
}) {
  const t = useT()
  return (
    <div role="button" tabIndex={0} onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen() } }}
      className="flex w-full cursor-pointer items-center gap-2 rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5
                 hover:border-neutral-500 focus:border-blue-500 focus:outline-none">
      {empty
        ? <span className="flex-1 truncate py-0.5 text-sm text-neutral-500">{placeholder}</span>
        : <div className="min-w-0 flex-1">{children}</div>}
      {!empty && onClear && (
        <button type="button" title={clearTitle}
          onClick={(e) => { e.stopPropagation(); onClear() }}
          className="shrink-0 rounded px-1.5 text-neutral-500 hover:bg-neutral-700 hover:text-white">✕</button>
      )}
      <span className="shrink-0 text-xs text-neutral-400">{t('선택하기')} ▾</span>
    </div>
  )
}
