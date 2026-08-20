/**
 * YOLO bbox 라벨러 — feature/yolo-training.md 2단계.
 *
 * 좌표는 처음부터 끝까지 **정규화(0~1)** 다. YOLO txt 가 정규화라서
 * 화면 크기와 무관하게 % 배치로 그리면 픽셀 변환이 아예 없다.
 *
 * 조작: 빈 곳 드래그 = 새 박스 · 클릭 = 선택 · 선택 후 드래그 = 이동 ·
 * 우하단 핸들 = 크기 · Del = 삭제 · 1~9 = 클래스 · ←→ = 이미지 이동.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'

export type LabelBox = { cls: number; cx: number; cy: number; w: number; h: number }

/** 클래스 색 — 트랙 표시가 아니라 구분용이라 순환해도 된다 */
export const CLASS_COLORS = [
  '#3b82f6', '#22c55e', '#f59e0b', '#a855f7', '#ef4444', '#22d3ee', '#eab308', '#ec4899',
]

type Props = {
  dataset: string
  classes: string[]
  files: string[]                 // 표시 순서의 이미지 파일명들
  index: number                   // 현재 이미지 인덱스
  onNavigate: (index: number) => void
  onSaved: (file: string, labeled: boolean) => void
  onError: (msg: string) => void
}

type Drag =
  | { kind: 'draw'; x0: number; y0: number; x1: number; y1: number }
  | { kind: 'move'; box: number; dx: number; dy: number }
  | { kind: 'resize'; box: number }

const clamp01 = (v: number) => Math.min(1, Math.max(0, v))

export default function YoloLabeler({ dataset, classes, files, index, onNavigate, onSaved, onError }: Props) {
  const file = files[index]
  const [boxes, setBoxes] = useState<LabelBox[] | null>(null)
  const [loaded, setLoaded] = useState(false)   // 라벨 GET 완료 전 조작 금지
  const [selected, setSelected] = useState<number | null>(null)
  const [cls, setCls] = useState(0)             // 새 박스에 붙는 클래스
  const [drag, setDrag] = useState<Drag | null>(null)
  const [savedAt, setSavedAt] = useState(0)     // 저장 표시등

  const areaRef = useRef<HTMLDivElement>(null)
  const dirtyRef = useRef(false)
  const boxesRef = useRef<LabelBox[] | null>(null)
  boxesRef.current = boxes
  const fileRef = useRef(file)

  // ── 라벨 로드 (이미지 전환마다) ──
  useEffect(() => {
    if (!file) return
    setLoaded(false)
    setSelected(null)
    fileRef.current = file
    api.get<{ boxes: LabelBox[] | null }>(`/yolo/datasets/${dataset}/labels/${file}`)
      .then((r) => { setBoxes(r.boxes); dirtyRef.current = false; setLoaded(true) })
      .catch((e) => onError(e instanceof Error ? e.message : '라벨 로드 실패'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataset, file])

  // ── 저장 (디바운스 + 전환·언마운트 시 즉시) ──
  const save = useCallback(async (f: string, b: LabelBox[] | null) => {
    if (b == null) return
    try {
      await api.put(`/yolo/datasets/${dataset}/labels/${f}`, { boxes: b })
      dirtyRef.current = false
      setSavedAt(Date.now())
      onSaved(f, true)
    } catch (e) {
      onError(e instanceof Error ? e.message : '라벨 저장 실패')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataset])

  useEffect(() => {
    if (!dirtyRef.current) return
    const t = setTimeout(() => { if (dirtyRef.current) void save(fileRef.current, boxesRef.current) }, 600)
    return () => clearTimeout(t)
  }, [boxes, save])

  useEffect(() => () => {   // 언마운트 — 마지막 변경을 흘리지 않는다
    if (dirtyRef.current) void save(fileRef.current, boxesRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const mutate = (next: LabelBox[]) => { dirtyRef.current = true; setBoxes(next) }

  const navigate = (to: number) => {
    if (to < 0 || to >= files.length) return
    if (dirtyRef.current) void save(fileRef.current, boxesRef.current)
    onNavigate(to)
  }

  // ── 마우스 → 정규화 좌표 ──
  const norm = (e: { clientX: number; clientY: number }) => {
    const r = areaRef.current!.getBoundingClientRect()
    return { x: clamp01((e.clientX - r.left) / r.width), y: clamp01((e.clientY - r.top) / r.height) }
  }

  const beginDraw = (e: React.MouseEvent) => {
    if (!loaded || e.button !== 0) return
    const { x, y } = norm(e)
    setSelected(null)
    setDrag({ kind: 'draw', x0: x, y0: y, x1: x, y1: y })
  }

  const beginMove = (e: React.MouseEvent, i: number) => {
    if (!loaded || e.button !== 0 || !boxes) return
    e.stopPropagation()
    setSelected(i)
    const { x, y } = norm(e)
    setDrag({ kind: 'move', box: i, dx: x - boxes[i].cx, dy: y - boxes[i].cy })
  }

  const beginResize = (e: React.MouseEvent, i: number) => {
    if (!loaded || e.button !== 0) return
    e.stopPropagation()
    setSelected(i)
    setDrag({ kind: 'resize', box: i })
  }

  useEffect(() => {
    if (!drag) return
    const onMove = (e: MouseEvent) => {
      const { x, y } = norm(e)
      if (drag.kind === 'draw') {
        setDrag({ ...drag, x1: x, y1: y })
      } else if (drag.kind === 'move') {
        const b = boxesRef.current![drag.box]
        // 박스가 화면 밖으로 안 나가게 중심을 반폭 안쪽으로 클램프
        const cx = Math.min(1 - b.w / 2, Math.max(b.w / 2, x - drag.dx))
        const cy = Math.min(1 - b.h / 2, Math.max(b.h / 2, y - drag.dy))
        mutate(boxesRef.current!.map((bb, j) => (j === drag.box ? { ...bb, cx, cy } : bb)))
      } else {
        const b = boxesRef.current![drag.box]
        const left = b.cx - b.w / 2, top = b.cy - b.h / 2
        const w = Math.max(0.005, x - left), h = Math.max(0.005, y - top)
        mutate(boxesRef.current!.map((bb, j) =>
          j === drag.box ? { ...bb, cx: clamp01(left + w / 2), cy: clamp01(top + h / 2), w: Math.min(w, 1), h: Math.min(h, 1) } : bb))
      }
    }
    const onUp = () => {
      if (drag.kind === 'draw') {
        const w = Math.abs(drag.x1 - drag.x0), h = Math.abs(drag.y1 - drag.y0)
        if (w > 0.008 && h > 0.008) {
          const nb: LabelBox = {
            cls, cx: (drag.x0 + drag.x1) / 2, cy: (drag.y0 + drag.y1) / 2, w, h,
          }
          const next = [...(boxesRef.current ?? []), nb]
          mutate(next)
          setSelected(next.length - 1)
        }
      }
      setDrag(null)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drag, cls])

  // ── 키보드 ──
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
      if (e.key === 'ArrowLeft') { e.preventDefault(); navigate(index - 1) }
      else if (e.key === 'ArrowRight') { e.preventDefault(); navigate(index + 1) }
      else if ((e.key === 'Delete' || e.key === 'Backspace') && selected != null && boxes) {
        mutate(boxes.filter((_, j) => j !== selected)); setSelected(null)
      } else if (e.key === 'Escape') setSelected(null)
      else if (/^[1-9]$/.test(e.key)) {
        const c = Number(e.key) - 1
        if (c >= classes.length) return
        setCls(c)
        if (selected != null && boxes) mutate(boxes.map((b, j) => (j === selected ? { ...b, cls: c } : b)))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, selected, boxes, classes.length, files.length])

  if (!file) return <div className="text-sm text-neutral-500 py-10 text-center">이미지가 없습니다</div>

  // 그리는 중인 임시 박스
  const temp = drag?.kind === 'draw' ? {
    left: Math.min(drag.x0, drag.x1), top: Math.min(drag.y0, drag.y1),
    w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0),
  } : null

  return (
    <div className="space-y-3">
      {/* 클래스 팔레트 + 상태 */}
      <div className="flex items-center gap-2 flex-wrap text-sm">
        {classes.map((c, i) => (
          <button key={c} onClick={() => {
            setCls(i)
            if (selected != null && boxes) mutate(boxes.map((b, j) => (j === selected ? { ...b, cls: i } : b)))
          }}
            className={`px-2 py-0.5 rounded border text-xs ${
              cls === i ? 'border-white text-white' : 'border-neutral-700 text-neutral-400 hover:text-white'}`}
            style={{ backgroundColor: `${CLASS_COLORS[i % CLASS_COLORS.length]}33` }}>
            {i + 1}. {c}
          </button>
        ))}
        <span className="ml-auto text-xs text-neutral-500">
          {index + 1}/{files.length}
          {boxes == null ? ' · 미라벨' : ` · 박스 ${boxes.length}개`}
          {savedAt > 0 && dirtyRef.current === false && boxes != null && ' · 저장됨'}
        </span>
      </div>

      {/* 캔버스 — 이미지 + % 배치 박스 오버레이 */}
      <div ref={areaRef} onMouseDown={beginDraw}
        className="relative select-none cursor-crosshair rounded border border-neutral-700 bg-black overflow-hidden">
        <img src={`/api/yolo/datasets/${dataset}/images/${file}`} alt={file}
          className="w-full block pointer-events-none" draggable={false} />
        {(boxes ?? []).map((b, i) => {
          const color = CLASS_COLORS[b.cls % CLASS_COLORS.length]
          const sel = selected === i
          return (
            <div key={i} onMouseDown={(e) => beginMove(e, i)}
              className="absolute cursor-move"
              style={{
                left: `${(b.cx - b.w / 2) * 100}%`, top: `${(b.cy - b.h / 2) * 100}%`,
                width: `${b.w * 100}%`, height: `${b.h * 100}%`,
                border: `2px solid ${color}`,
                boxShadow: sel ? `0 0 0 1px white` : undefined,
                backgroundColor: sel ? `${color}22` : 'transparent',
              }}>
              <span className="absolute -top-5 left-0 px-1 text-[10px] rounded whitespace-nowrap"
                style={{ backgroundColor: color, color: '#000' }}>
                {classes[b.cls] ?? b.cls}
              </span>
              {sel && (
                <div onMouseDown={(e) => beginResize(e, i)}
                  className="absolute -right-1.5 -bottom-1.5 w-3 h-3 bg-white rounded-sm cursor-nwse-resize" />
              )}
            </div>
          )
        })}
        {temp && (
          <div className="absolute border-2 border-dashed border-white/70 pointer-events-none"
            style={{ left: `${temp.left * 100}%`, top: `${temp.top * 100}%`, width: `${temp.w * 100}%`, height: `${temp.h * 100}%` }} />
        )}
      </div>

      {/* 동작 줄 */}
      <div className="flex items-center gap-2 flex-wrap text-xs text-neutral-500">
        <button onClick={() => navigate(index - 1)} disabled={index === 0}
          className="px-2 py-1 rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-300 disabled:opacity-40">← 이전</button>
        <button onClick={() => navigate(index + 1)} disabled={index >= files.length - 1}
          className="px-2 py-1 rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-300 disabled:opacity-40">다음 →</button>
        {boxes == null && (
          <button onClick={() => mutate([])}
            className="px-2 py-1 rounded bg-neutral-800 hover:bg-green-700 text-neutral-300 hover:text-white"
            title="박스 0개로 저장 — 이 장면에 대상 물체가 없음을 사람이 확인함">
            배경으로 저장
          </button>
        )}
        {boxes != null && (
          <button onClick={async () => {
            try {
              await api.delete(`/yolo/datasets/${dataset}/labels/${file}`)
              dirtyRef.current = false
              setBoxes(null); setSelected(null)
              onSaved(file, false)
            } catch (e) { onError(e instanceof Error ? e.message : '해제 실패') }
          }}
            className="px-2 py-1 rounded bg-neutral-800 hover:bg-red-700 text-neutral-300 hover:text-white">
            미라벨로
          </button>
        )}
        <span className="ml-auto">드래그=박스 · Del=삭제 · 1~9=클래스 · ←→=이동</span>
      </div>
    </div>
  )
}
