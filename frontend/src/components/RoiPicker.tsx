import { useCallback, useEffect, useRef } from 'react'

/**
 * 프리뷰 위에서 사각형 영역을 고른다 — 클릭·드래그로 옮기고 **휠로 크기**를 바꾼다
 * (feature/gray-card-calibration.md §6).
 *
 * ## 좌표는 프레임 기준이다
 *
 * 화면에 보이는 크기가 아니라 **원본 프레임 픽셀**로 주고받는다. 백엔드가 자르는
 * 것은 프레임이고, 화면 크기는 창을 줄이면 바뀌기 때문이다.
 *
 * ⚠ **`object-contain` 은 레터박스를 만든다.** 848x480 프레임을 4:3 상자에 넣으면
 * 위아래에 빈 띠가 생기는데, 요소 좌표를 그대로 비율로 쓰면 그 띠만큼 어긋난다 —
 * 상자가 손끝에서 미끄러진다. `naturalWidth/Height` 로 실제 그려진 사각형을 먼저
 * 구하고 그 안에서 환산한다.
 */

export type Roi = { cx: number; cy: number; size: number }

/** 백엔드가 최소 100픽셀을 요구한다(10x10). 그보다 작으면 잡음만 잰다. */
export const MIN_SIZE = 32
const STEP = 1.12

/** 요소 안에서 이미지가 **실제로 그려진** 사각형. `object-contain` 이 남긴 여백을 뺀다. */
function drawnRect(img: HTMLImageElement) {
  const r = img.getBoundingClientRect()
  const nw = img.naturalWidth || 1
  const nh = img.naturalHeight || 1
  const scale = Math.min(r.width / nw, r.height / nh)
  const w = nw * scale
  const h = nh * scale
  return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, w, h, nw, nh }
}

export function centerRoi(nw: number, nh: number, frac = 0.3): Roi {
  return { cx: nw / 2, cy: nh / 2, size: Math.round(Math.min(nw, nh) * frac) }
}

/** 프레임 좌표 → 백엔드가 받는 `(x, y, w, h)`. 프레임 밖으로 안 나간다. */
export function toBox(roi: Roi, nw: number, nh: number): [number, number, number, number] {
  const size = Math.max(MIN_SIZE, Math.min(roi.size, nw, nh))
  const half = size / 2
  const cx = Math.min(Math.max(roi.cx, half), nw - half)
  const cy = Math.min(Math.max(roi.cy, half), nh - half)
  return [Math.round(cx - half), Math.round(cy - half), Math.round(size), Math.round(size)]
}

export default function RoiPicker({
  imgRef, roi, onChange, hint,
}: {
  imgRef: React.RefObject<HTMLImageElement | null>
  roi: Roi | null
  onChange: (roi: Roi) => void
  hint?: string
}) {
  const dragging = useRef(false)
  // ⚠ 휠 리스너는 **여기** 붙는다. 이미지에 붙이면 안 먹는다 — 이 층이 이미지를
  //   덮고 있고 둘은 형제라, wheel 이 이미지로 흘러가지 않는다. 실기에서 그랬다.
  const surfaceRef = useRef<HTMLDivElement | null>(null)

  const toFrame = useCallback((clientX: number, clientY: number): Roi | null => {
    const img = imgRef.current
    if (!img || !roi) return null
    const d = drawnRect(img)
    if (d.w <= 0 || d.h <= 0) return null
    return {
      ...roi,
      cx: ((clientX - d.left) / d.w) * d.nw,
      cy: ((clientY - d.top) / d.h) * d.nh,
    }
  }, [imgRef, roi])

  const moveTo = useCallback((e: { clientX: number; clientY: number }) => {
    const next = toFrame(e.clientX, e.clientY)
    if (next) onChange(next)
  }, [toFrame, onChange])

  // ⚠ **네이티브 리스너로 단다.** React 의 `onWheel` 은 루트에 passive 로 붙어
  //   `preventDefault()` 가 안 먹는다 — 그러면 상자를 키우는 동안 설정 모달이
  //   같이 스크롤된다.
  useEffect(() => {
    const surface = surfaceRef.current
    if (!surface) return
    const onWheel = (e: WheelEvent) => {
      if (!roi) return
      e.preventDefault()
      const img = imgRef.current
      const nw = img?.naturalWidth || 1
      const nh = img?.naturalHeight || 1
      const factor = e.deltaY < 0 ? STEP : 1 / STEP
      onChange({ ...roi, size: Math.max(MIN_SIZE, Math.min(roi.size * factor, nw, nh)) })
    }
    surface.addEventListener('wheel', onWheel, { passive: false })
    return () => surface.removeEventListener('wheel', onWheel)
  }, [imgRef, roi, onChange])

  useEffect(() => {
    const up = () => { dragging.current = false }
    window.addEventListener('pointerup', up)
    return () => window.removeEventListener('pointerup', up)
  }, [])

  const img = imgRef.current
  if (!roi || !img || !img.naturalWidth) return null

  const d = drawnRect(img)
  const parent = img.parentElement?.getBoundingClientRect()
  if (!parent) return null
  const [bx, by, bw] = toBox(roi, d.nw, d.nh)
  const s = d.w / d.nw
  const style = {
    left: d.left - parent.left + bx * s,
    top: d.top - parent.top + by * s,
    width: bw * s,
    height: bw * s,
  }

  // ⚠ 바깥을 어둡게 하는 데 **거대한 그림자를 쓰지 않는다.** `9999px` 짜리 확산은
  //   프리뷰 상자를 넘어 페이지 전체를 덮는다(실기에서 그랬다). 네 조각으로
  //   상자 둘레만 덮으면 이 컨테이너 밖으로 샐 수가 없다.
  const dim = 'pointer-events-none absolute bg-black/45'
  const { left, top, width, height } = style

  return (
    <>
      <div className={dim} style={{ left: 0, top: 0, right: 0, height: top }} />
      <div className={dim} style={{ left: 0, top: top + height, right: 0, bottom: 0 }} />
      <div className={dim} style={{ left: 0, top, width: left, height }} />
      <div className={dim} style={{ left: left + width, top, right: 0, height }} />

      {/* 마우스를 받는 층. 이미지 위에 겹쳐 두고 드래그로 옮긴다.
          휠 리스너도 여기 붙는다 — 위 주석 참고. */}
      <div
        ref={surfaceRef}
        className="absolute inset-0 cursor-crosshair touch-none"
        onPointerDown={(e) => { dragging.current = true; moveTo(e) }}
        onPointerMove={(e) => { if (dragging.current) moveTo(e) }}
      />
      <div className="pointer-events-none absolute border-2 border-amber-400" style={style}>
        {/* 상자 **안쪽**에 붙인다. 위에 두면 상자가 화면 꼭대기에 있을 때 잘린다. */}
        <span className="absolute left-0 top-0 whitespace-nowrap rounded-br bg-amber-400
                         px-1 text-[10px] font-medium text-black tabular-nums">
          {Math.round(bw)}px{hint ? ` · ${hint}` : ''}
        </span>
      </div>
    </>
  )
}
