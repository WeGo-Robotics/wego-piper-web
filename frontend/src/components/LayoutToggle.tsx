import { useState } from 'react'

/**
 * 세로/가로 배치 토글.
 *
 * 같은 토글이 비전 페이지에 먼저 있었고 학습 페이지에도 필요해졌다. 복사하면
 * 저장 키 규칙이나 라벨이 한쪽만 바뀐다 — 그런 갈라짐이 이 저장소에서 반복해서
 * 났으므로(관절 순서, 페이지 목록) 처음부터 한 벌로 둔다.
 *
 * ⚠ 값은 `localStorage` 에 남는다. 배치는 **화면 크기와 취향**이 정하는 것이라
 * 매번 다시 고르게 하면 성가시다.
 */

export type Layout = 'col' | 'row'

export function useLayout(key: string, initial: Layout = 'col') {
  const storageKey = `${key}-layout`
  const [layout, set] = useState<Layout>(
    () => (localStorage.getItem(storageKey) as Layout) || initial)
  const switchLayout = (l: Layout) => {
    localStorage.setItem(storageKey, l)
    set(l)
  }
  return { layout, switchLayout }
}

export default function LayoutToggle({
  layout, onChange,
}: {
  layout: Layout
  onChange: (l: Layout) => void
}) {
  return (
    <div className="flex justify-end">
      <div className="flex rounded overflow-hidden border border-neutral-700 text-xs">
        {([['col', '세로'], ['row', '가로']] as const).map(([l, label]) => (
          <button key={l} onClick={() => onChange(l)}
            className={`px-2.5 py-1 ${layout === l
              ? 'bg-neutral-600 text-white' : 'bg-neutral-800 text-neutral-500 hover:text-neutral-300'}`}>
            {label}
          </button>
        ))}
      </div>
    </div>
  )
}
