import { useCallback, useEffect, useRef, useState } from 'react'

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
 *
 * ## ⚠ 프리뷰는 **200ms 마다 다시 로딩된다**
 *
 * 설정 모달은 `src` 를 계속 갈아끼워 화면을 갱신한다. 그 순간 `naturalWidth` 가
 * 0 이 되므로, 렌더할 때마다 이미지에서 크기를 읽으면 **상자가 5분의 1초마다
 * 사라진다.** 그래서 크기를 state 에 담아두고 **0 은 무시**한다 — 마지막으로
 * 제대로 읽은 값을 계속 쓴다.
 *
 * ## ⚠ 휠 리스너는 한 번만 붙인다
 *
 * `roi` 나 `onChange` 를 의존성에 넣으면 휠을 굴리는 내내 리스너가 떼였다 붙는다.
 * 그 틈에 들어온 이벤트는 **같은 옛 값에서 다시 계산**해 크기가 초기화된 것처럼
 * 보인다. 최신 값은 ref 로 읽는다.
 */

export type Roi = { cx: number; cy: number; size: number }

/** 백엔드가 최소 100픽셀을 요구한다(10x10). 그보다 작으면 잡음만 잰다. */
export const MIN_SIZE = 32
const STEP = 1.12

type Geo = { ox: number; oy: number; w: number; h: number; nw: number; nh: number }

/** 컨테이너 안에서 이미지가 **실제로 그려진** 사각형. `object-contain` 여백을 뺀다. */
function readGeo(img: HTMLImageElement): Geo | null {
  const nw = img.naturalWidth
  const nh = img.naturalHeight
  const r = img.getBoundingClientRect()
  // 재로딩 중이면 0 이다. **덮어쓰지 않는다** — 그러면 상자가 깜빡인다.
  if (!nw || !nh || !r.width || !r.height) return null
  const scale = Math.min(r.width / nw, r.height / nh)
  const w = nw * scale
  const h = nh * scale
  return { ox: (r.width - w) / 2, oy: (r.height - h) / 2, w, h, nw, nh }
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
  const [geo, setGeo] = useState<Geo | null>(null)
  const dragging = useRef(false)
  const surfaceRef = useRef<HTMLDivElement | null>(null)
  // 리스너를 다시 붙이지 않고 최신 값을 읽는 통로
  const roiRef = useRef(roi)
  const onChangeRef = useRef(onChange)
  roiRef.current = roi
  onChangeRef.current = onChange

  // 그려진 사각형 추적 — 로드·리사이즈 때만 갱신하고 **0 은 버린다**
  useEffect(() => {
    const img = imgRef.current
    if (!img) return
    const update = () => {
      const g = readGeo(img)
      if (g) setGeo(g)
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(img)
    img.addEventListener('load', update)
    window.addEventListener('resize', update)
    return () => {
      ro.disconnect()
      img.removeEventListener('load', update)
      window.removeEventListener('resize', update)
    }
  }, [imgRef])

  const moveTo = useCallback((e: { clientX: number; clientY: number }) => {
    const img = imgRef.current
    const cur = roiRef.current
    if (!img || !cur) return
    const g = readGeo(img)
    if (!g) return
    const r = img.getBoundingClientRect()
    onChangeRef.current({
      ...cur,
      cx: ((e.clientX - r.left - g.ox) / g.w) * g.nw,
      cy: ((e.clientY - r.top - g.oy) / g.h) * g.nh,
    })
  }, [imgRef])

  // ⚠ 네이티브 리스너다. React 의 `onWheel` 은 루트에 passive 로 붙어
  //   `preventDefault()` 가 안 먹는다 — 모달이 같이 스크롤된다.
  //   그리고 **마우스를 받는 이 층**에 붙여야 한다. 이미지는 이 층에 덮여 있고
  //   둘은 형제라 wheel 이 거기까지 안 간다.
  //   의존성은 비운다 — 굴리는 내내 떼였다 붙으면 크기가 초기화된 것처럼 보인다.
  useEffect(() => {
    const surface = surfaceRef.current
    if (!surface) return
    const onWheel = (e: WheelEvent) => {
      const cur = roiRef.current
      if (!cur) return
      e.preventDefault()
      const img = imgRef.current
      const nw = img?.naturalWidth || 0
      const nh = img?.naturalHeight || 0
      // 재로딩 중이라 크기를 모르면 상한만 빼고 계산한다 — 멈추는 것보다 낫다
      const cap = nw && nh ? Math.min(nw, nh) : Number.POSITIVE_INFINITY
      const factor = e.deltaY < 0 ? STEP : 1 / STEP
      onChangeRef.current({
        ...cur, size: Math.max(MIN_SIZE, Math.min(cur.size * factor, cap)),
      })
    }
    surface.addEventListener('wheel', onWheel, { passive: false })
    return () => surface.removeEventListener('wheel', onWheel)
  }, [imgRef])

  useEffect(() => {
    const up = () => { dragging.current = false }
    window.addEventListener('pointerup', up)
    return () => window.removeEventListener('pointerup', up)
  }, [])

  if (!roi || !geo) return null

  const [bx, by, bw] = toBox(roi, geo.nw, geo.nh)
  const s = geo.w / geo.nw
  const box = {
    left: geo.ox + bx * s, top: geo.oy + by * s,
    width: bw * s, height: bw * s,
  }

  // ⚠ 바깥을 어둡게 하는 데 **거대한 그림자를 쓰지 않는다.** `9999px` 확산은
  //   프리뷰를 넘어 페이지 전체를 덮는다(실기에서 그랬다). 네 조각으로 상자
  //   둘레만 덮으면 이 컨테이너 밖으로 샐 수가 없다.
  const dim = 'pointer-events-none absolute bg-black/45'

  return (
    <>
      <div className={dim} style={{ left: 0, top: 0, right: 0, height: box.top }} />
      <div className={dim} style={{ left: 0, top: box.top + box.height, right: 0, bottom: 0 }} />
      <div className={dim} style={{ left: 0, top: box.top, width: box.left, height: box.height }} />
      <div className={dim} style={{ left: box.left + box.width, top: box.top, right: 0, height: box.height }} />

      <div
        ref={surfaceRef}
        className="absolute inset-0 cursor-crosshair touch-none"
        onPointerDown={(e) => { dragging.current = true; moveTo(e) }}
        onPointerMove={(e) => { if (dragging.current) moveTo(e) }}
      />
      <div className="pointer-events-none absolute border-2 border-amber-400" style={box}>
        {/* 상자 **안쪽**에 붙인다. 위에 두면 상자가 꼭대기에 있을 때 잘린다. */}
        <span className="absolute left-0 top-0 whitespace-nowrap rounded-br bg-amber-400
                         px-1 text-[10px] font-medium text-black tabular-nums">
          {Math.round(bw)}px{hint ? ` · ${hint}` : ''}
        </span>
      </div>
    </>
  )
}
