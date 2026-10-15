import { useEffect, useRef } from 'react'
import type { LiveFrames } from '../hooks/useCameraLive'

/**
 * 한 카메라의 상시 화면. 프레임은 `useLiveFrames` 가 `<img>` 의 `src` 에 직접 건다.
 *
 * ⚠ **`src` 속성을 주지 않는다.** 주면 부모가 다시 그려질 때(3초 폴링마다) React 가 그 값으로
 *   되돌려 쓴다 — 프레임이 한 장짜리 `/preview` 로 깜빡이며 바뀐다.
 */
export default function LiveCameraFrame({ id, alt, attach, className = '' }: {
  id: string
  alt: string
  attach: LiveFrames['attach']
  className?: string
}) {
  const ref = useRef<HTMLImageElement | null>(null)

  useEffect(() => {
    const el = ref.current
    return el ? attach(id, el) : undefined
  }, [id, attach])

  // `alt` 를 비워 두는 이유: `src` 가 없는 `<img>` 는 첫 프레임이 오기 전까지 alt 글자를 그린다.
  return <img ref={ref} alt="" aria-label={alt} draggable={false} className={className} />
}
