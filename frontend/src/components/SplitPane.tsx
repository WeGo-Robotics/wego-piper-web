import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 좌우 분할 — 가운데 선을 끌어 폭을 바꾼다.
 *
 * ⚠ **두 칸일 때만 적용한다.** 나누는 폭은 인라인 스타일인데, 칸이 하나로 접히는
 * 좁은 화면에서도 그 스타일이 남으면 반응형이 깨진다. 그래서 브레이크포인트를
 * CSS 가 아니라 `matchMedia` 로 봐서, 한 칸일 때는 스타일을 아예 안 건다 —
 * 같은 조건을 CSS 와 JS 가 따로 판단하다 어긋난 전례가 이 화면에 있다.
 */

const MIN_PCT = 20        // 한쪽이 이보다 좁아지면 내용이 읽히지 않는다
const MAX_PCT = 80

export function useMediaQuery(query: string): boolean {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = () => setMatch(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [query])
  return match
}

export function useSplit(key: string, initial = 50) {
  const storageKey = `${key}-split`
  const [pct, setPct] = useState<number>(() => {
    const v = Number(localStorage.getItem(storageKey))
    return Number.isFinite(v) && v >= MIN_PCT && v <= MAX_PCT ? v : initial
  })
  // ⚠ 저장은 **놓을 때만** 한다. 끄는 내내 쓰면 프레임마다 localStorage 를 친다.
  const commit = useCallback((v: number) => {
    localStorage.setItem(storageKey, String(Math.round(v)))
  }, [storageKey])
  return { pct, setPct, commit, reset: () => { setPct(initial); commit(initial) } }
}

export default function SplitHandle({
  onCommit, onReset, containerRef,
}: {
  onCommit: (pct: number) => void
  onReset: () => void
  containerRef: React.RefObject<HTMLDivElement | null>
}) {
  const last = useRef(50)

  /**
   * ⚠ **끄는 동안 React 상태를 건드리지 않는다.**
   *
   * 폭을 상태로 두면 포인터 이벤트마다 페이지가 다시 그려지고, 그 안의 그래프
   * 열 몇 개가 매 프레임 재생성된다 — 무겁고, 그리는 도중 폭이 0 으로 잡히면
   * 빈 채로 굳는다. 대신 CSS 변수만 직접 쓰고, 상태는 **놓을 때** 한 번 맞춘다.
   */
  const move = (e: React.PointerEvent) => {
    const el = containerRef.current
    const box = el?.getBoundingClientRect()
    if (!el || !box || box.width === 0) return
    const pct = Math.min(MAX_PCT, Math.max(MIN_PCT, ((e.clientX - box.left) / box.width) * 100))
    last.current = pct
    el.style.setProperty('--split', `${pct}%`)
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      title="끌어서 폭 조절 · 더블클릭으로 반반"
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId)
        // 끄는 동안 텍스트가 잡히면 커서가 I-빔이 되고 선택이 번진다
        document.body.style.userSelect = 'none'
      }}
      onPointerMove={(e) => { if (e.currentTarget.hasPointerCapture(e.pointerId)) move(e) }}
      onPointerUp={(e) => {
        e.currentTarget.releasePointerCapture(e.pointerId)
        document.body.style.userSelect = ''
        onCommit(last.current)   // 여기서 한 번만 상태로 옮긴다
      }}
      onDoubleClick={onReset}
      className="group relative w-2 shrink-0 cursor-col-resize self-stretch"
    >
      <div className="absolute inset-y-0 left-1/2 -translate-x-1/2 w-px bg-neutral-700
                      group-hover:bg-blue-500 transition-colors" />
    </div>
  )
}
