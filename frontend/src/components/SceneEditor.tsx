/**
 * 장면 편집기 — 복제 도장으로 물체를 지운다 (feature/weight-analyzer.md §5).
 *
 * Alt+클릭(또는 [베낄 곳 찍기])으로 원본 위치를 정하고, 지울 곳을 칠하면 그만큼 떨어진 곳의
 * 배경이 덮인다. 옆 자리 배경으로 물체를 덮어 "처음 보는 장면" 을 만든다.
 *
 * ⚠ **한 번 칠하는 동안은 시작 순간의 그림에서 베낀다.** 칠하는 캔버스에서 바로 베끼면 방금
 *   칠한 자국을 다시 베껴 무늬가 번진다(포토샵의 "정렬" 도장도 같은 문제를 다르게 푼다).
 * ⚠ 바탕 이미지는 게이트웨이(같은 출처)에서 온다 — 캔버스가 오염되지 않아 PNG 로 내보낼 수 있다.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useT } from '../i18n'

type Pt = { x: number; y: number }
const MAX_UNDO = 20

export default function SceneEditor({ src, onChange }: {
  src: string
  /** 편집한 PNG data URL. 원래대로 돌리면 null */
  onChange: (dataUrl: string | null) => void
}) {
  const t = useT()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const overlayRef = useRef<HTMLCanvasElement>(null)
  const snapshot = useRef<HTMLCanvasElement | null>(null)
  const undo = useRef<ImageData[]>([])
  const stroke = useRef<{ offset: Pt; last: Pt } | null>(null)
  const [source, setSource] = useState<Pt | null>(null)
  const [picking, setPicking] = useState(false)
  const [radius, setRadius] = useState(14)
  const [edited, setEdited] = useState(false)
  const [undoCount, setUndoCount] = useState(0)
  // 부모가 렌더마다 새 함수를 줘도 바탕을 다시 읽지 않게 — 바탕은 src 가 바뀔 때만 다시 읽는다
  const onChangeRef = useRef(onChange)
  useEffect(() => { onChangeRef.current = onChange })

  const load = useCallback(() => {
    const img = new Image()
    img.onload = () => {
      const c = canvasRef.current, o = overlayRef.current
      if (!c || !o) return
      c.width = o.width = img.naturalWidth
      c.height = o.height = img.naturalHeight
      c.getContext('2d')!.drawImage(img, 0, 0)
      undo.current = []
      setUndoCount(0)
      setEdited(false)
      onChangeRef.current(null)
    }
    img.src = src
  }, [src])

  useEffect(() => { load() }, [load])

  const toImage = (e: React.PointerEvent): Pt => {
    const c = canvasRef.current!
    const r = c.getBoundingClientRect()
    return { x: ((e.clientX - r.left) * c.width) / r.width, y: ((e.clientY - r.top) * c.height) / r.height }
  }

  const drawOverlay = (cursor: Pt | null) => {
    const o = overlayRef.current
    if (!o) return
    const g = o.getContext('2d')!
    g.clearRect(0, 0, o.width, o.height)
    g.lineWidth = Math.max(1, o.width / 400)
    if (cursor) {
      g.strokeStyle = 'rgba(255,255,255,0.9)'
      g.beginPath(); g.arc(cursor.x, cursor.y, radius, 0, Math.PI * 2); g.stroke()
    }
    // 베낄 곳 — 칠하는 중이면 붓을 따라 움직인다
    const s = stroke.current && cursor
      ? { x: cursor.x + stroke.current.offset.x, y: cursor.y + stroke.current.offset.y }
      : source
    if (s) {
      g.strokeStyle = 'rgba(250,204,21,0.95)'
      g.beginPath(); g.arc(s.x, s.y, radius, 0, Math.PI * 2); g.stroke()
      g.beginPath(); g.moveTo(s.x - radius, s.y); g.lineTo(s.x + radius, s.y)
      g.moveTo(s.x, s.y - radius); g.lineTo(s.x, s.y + radius); g.stroke()
    }
  }

  const stamp = (p: Pt) => {
    const c = canvasRef.current!, snap = snapshot.current, st = stroke.current
    if (!snap || !st) return
    const g = c.getContext('2d')!
    g.save()
    g.beginPath(); g.arc(p.x, p.y, radius, 0, Math.PI * 2); g.clip()
    // 목적지 p 에 원본 p+offset 의 화소를 놓는다
    g.drawImage(snap, -st.offset.x, -st.offset.y)
    g.restore()
  }

  const onDown = (e: React.PointerEvent) => {
    const p = toImage(e)
    if (e.altKey || picking) {
      setSource(p)
      setPicking(false)
      drawOverlay(p)
      return
    }
    if (!source) return
    const c = canvasRef.current!
    const g = c.getContext('2d')!
    undo.current = [...undo.current.slice(-(MAX_UNDO - 1)), g.getImageData(0, 0, c.width, c.height)]
    setUndoCount(undo.current.length)
    const snap = document.createElement('canvas')
    snap.width = c.width; snap.height = c.height
    snap.getContext('2d')!.drawImage(c, 0, 0)
    snapshot.current = snap
    stroke.current = { offset: { x: source.x - p.x, y: source.y - p.y }, last: p }
    ;(e.target as Element).setPointerCapture(e.pointerId)
    stamp(p)
    drawOverlay(p)
  }

  const onMove = (e: React.PointerEvent) => {
    const p = toImage(e)
    const st = stroke.current
    if (st) {
      // 빠르게 그어도 끊기지 않게 지난 점과의 사이를 채운다
      const d = Math.hypot(p.x - st.last.x, p.y - st.last.y)
      const steps = Math.max(1, Math.ceil(d / (radius / 3)))
      for (let i = 1; i <= steps; i++) {
        stamp({ x: st.last.x + ((p.x - st.last.x) * i) / steps, y: st.last.y + ((p.y - st.last.y) * i) / steps })
      }
      st.last = p
    }
    drawOverlay(p)
  }

  const onUp = () => {
    const st = stroke.current
    if (!st) return
    // 다음 획은 지금 놓인 붓 자리 기준으로 같은 간격을 유지한다
    setSource({ x: st.last.x + st.offset.x, y: st.last.y + st.offset.y })
    stroke.current = null
    snapshot.current = null
    setEdited(true)
    onChange(canvasRef.current!.toDataURL('image/png'))
  }

  const onUndo = () => {
    const last = undo.current.pop()
    if (!last) return
    setUndoCount(undo.current.length)
    const c = canvasRef.current!
    c.getContext('2d')!.putImageData(last, 0, 0)
    const left = undo.current.length > 0
    setEdited(left)
    onChange(left ? c.toDataURL('image/png') : null)
  }

  const btn = 'rounded border border-neutral-600 px-2 py-0.5 text-xs text-neutral-300 hover:bg-neutral-700 disabled:opacity-40'

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-neutral-400">
        <button onClick={() => setPicking(true)} className={`${btn} ${picking ? 'bg-amber-600/30 border-amber-500' : ''}`}>
          {t('베낄 곳 찍기')}
        </button>
        <span>{t('붓')}</span>
        <input type="range" min={3} max={60} value={radius} onChange={(e) => setRadius(Number(e.target.value))} className="w-28 accent-blue-500" />
        <span className="tabular-nums">{radius}px</span>
        <button onClick={onUndo} disabled={undoCount === 0} className={btn}>{t('되돌리기')}</button>
        <button onClick={load} disabled={!edited} className={btn}>{t('원래대로')}</button>
        <span className="ml-auto">
          {source
            ? t('지울 곳을 칠하면 노란 원 자리의 배경이 덮인다')
            : t('Alt+클릭(또는 [베낄 곳 찍기])으로 덮을 배경을 먼저 찍는다')}
        </span>
      </div>
      <div className="relative">
        <canvas ref={canvasRef} className="block w-full rounded border border-neutral-700" />
        <canvas
          ref={overlayRef}
          className={`absolute inset-0 h-full w-full ${picking ? 'cursor-crosshair' : source ? 'cursor-none' : 'cursor-not-allowed'}`}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerLeave={() => { if (!stroke.current) drawOverlay(null) }}
        />
      </div>
    </div>
  )
}
