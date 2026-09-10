import { useState, type CSSProperties } from 'react'

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

/**
 * 2열 배치 — **최소 폭은 지키고, 넘으면 비율대로 늘고, 내용에 안 밀린다.**
 *
 * ⚠ 수집·학습·추론의 2열이 **시시각각** 폭이 바뀌었다. `grid-cols-[1fr_2fr]` 같은
 *   임의값은 그냥 `1fr 2fr` 이 되는데, fr 트랙의 최소값은 `auto`(=min-content)라
 *   열 안의 **쪼갤 수 없는 내용**이 트랙을 민다. 실제 추론 페이지에서 쟀다(1920px):
 *
 *       기본                         541 : 1083
 *       왼쪽 열에 카메라 행 하나      1226 :  706   ← 비율 역전 + 가로 넘침
 *       오른쪽 열에 긴 로그 한 줄      195 : 2268   ← 설정 열이 짜부라짐
 *
 *   카메라 행은 `flex-1` 셀렉트라 가장 긴 옵션 폭이 그대로 min-content 가 되고,
 *   로그는 실행 중에만 흘러서 "가만있다가 갑자기" 바뀐다.
 *
 * `minmax(최소px, Nfr)` 는 최소값이 **고정**이라 내용이 트랙에 못 끼어들고, 여유가
 * 있으면 fr 비율로 는다. 두 최소의 합보다 창이 좁으면 쌓지 않고 `minWidth` 가
 * `<main>` 을 가로로 스크롤시킨다 — 좁은 화면에서 한 열로 접으면 둘을 견주려고
 * 나란히 둔 뜻이 사라진다.
 */
export function twoColumns(
  ratio: [number, number], min: [number, number], gap = 24,
): { className: string; style: CSSProperties; 'data-two-col': string } {
  return {
    // `[&>*]:min-w-0` — 열 상자가 제 트랙보다 넓어져 옆 열을 덮지 않게
    className: 'grid items-start [&>*]:min-w-0',
    style: {
      gridTemplateColumns: `minmax(${min[0]}px, ${ratio[0]}fr) minmax(${min[1]}px, ${ratio[1]}fr)`,
      columnGap: gap, rowGap: gap,
      minWidth: min[0] + min[1] + gap,
    },
    'data-two-col': `${ratio[0]}:${ratio[1]}`,
  }
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
