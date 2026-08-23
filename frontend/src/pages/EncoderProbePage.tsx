import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'
import { usePolicies } from '../hooks/usePolicies'
import { usePolicyUi } from '../hooks/usePolicyUi'
import { type ReadyCam } from '../types/camera'

// ⚠ 정책 이름을 유니언으로 박지 않는다 — 모델이 늘 때마다 여기도 늘어난다.
// 어떤 정책이 프로브 되는지는 `policies/*.yaml` (capabilities.encoder_probe) 이 정한다.
type PolicyType = string
type ViewKind = 'input' | 'pca' | 'sim' | 'kmeans'

type EncoderModel = { id: string; path: string; policy_type: PolicyType; modified?: string; cameras: string[] }

type Meta = {
  policy_type: PolicyType
  tap: string
  checkpoint: string
  image_key: string
  image_keys: string[]
  grid_h: number; grid_w: number; dim: number; n_patches: number
  model_w: number; model_h: number; orig_w: number; orig_h: number
  valid_row0: number; valid_col0: number
  patch_px_model: number; patch_px_orig_x: number; patch_px_orig_y: number
  encoder_source: string
  encoder_stats: { random_init?: boolean; pretrained_backbone_weights?: string | null; q_proj_std?: number }
  feature_stats?: { norm_median: number; norm_max: number; norm_outlier_ratio: number }
  elapsed_ms: number; total_ms: number; device: string
}

type Slot = { sid: string; meta: Meta }
// ImageData 는 ArrayBuffer 로 뒷받침된 버퍼만 받는다 (SharedArrayBuffer 불가)
type PixelBuffer = Uint8ClampedArray<ArrayBuffer>
type Overlay = { data: PixelBuffer; info: string } | null

// 슬롯 안의 이미지 한 장. `src` 는 data URL — 카메라 캡처도 미리 받아 두므로
// 설정(체크포인트·추출 지점)을 바꾼 뒤 다시 인코딩할 수 있다.
type SlotImage = { id: number; name: string; src: string; result: Slot | null; error: string }
// 카드 하나 = 이미지 묶음. 라벨(A, B, …)은 만들 때 비어 있는 글자 중 첫 번째를 받고
// 이후 고정이다 — 가운데 카드를 지워도 나머지 이름이 밀리지 않는다.
type SlotEntry = { id: number; label: string; images: SlotImage[]; busy: boolean; error: string }

function nextLabel(entries: SlotEntry[]): string {
  const used = new Set(entries.map((e) => e.label))
  for (let i = 0; i < 26; i++) {
    const l = String.fromCharCode(65 + i)
    if (!used.has(l)) return l
  }
  return `#${entries.length + 1}`
}

// 순차(magnitude) 램프 — 단일 색상(blue) light→dark. 사진 위에 얹으므로 알파를 같이 올려
// 낮은 값이 원본으로 물러나게 한다.
const SEQ_STOPS: [number, number, number][] = [[205, 226, 251], [134, 182, 239], [57, 135, 229]]
// 범주형 고정 순서 (순환 금지). 인접 색 식별은 아래 클러스터 칩(격리)으로 보조한다.
const CATEGORICAL = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']
const MAX_K = CATEGORICAL.length

function seqColor(t: number): [number, number, number] {
  const x = Math.max(0, Math.min(1, t)) * (SEQ_STOPS.length - 1)
  const i = Math.min(SEQ_STOPS.length - 2, Math.floor(x))
  const f = x - i
  const a = SEQ_STOPS[i], b = SEQ_STOPS[i + 1]
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f]
}

function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

/** 패치 하나라도 패딩에 걸치면 내용이 없으므로 오버레이에서 제외한다. */
function isValidPatch(meta: Meta, index: number): boolean {
  const row = Math.floor(index / meta.grid_w)
  const col = index % meta.grid_w
  return row >= meta.valid_row0 && col >= meta.valid_col0
}

// ── 캔버스 ────────────────────────────────────────────────────────────────────

function ProbeCanvas({ slot, overlay, alpha, showGrid, smooth, selected, onPick }: {
  slot: Slot
  overlay: Overlay
  alpha: number
  showGrid: boolean
  smooth: boolean
  selected: number | null
  onPick: (patch: number) => void
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const [loaded, setLoaded] = useState(false)
  const { meta } = slot

  useEffect(() => {
    setLoaded(false)
    const img = new Image()
    img.onload = () => { imgRef.current = img; setLoaded(true) }
    img.src = `/api/encoder/${slot.sid}/input.jpg`
    return () => { img.onload = null }
  }, [slot.sid])

  useEffect(() => {
    const canvas = canvasRef.current
    const img = imgRef.current
    if (!canvas || !img || !loaded) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    canvas.width = meta.model_w
    canvas.height = meta.model_h
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height)

    if (overlay) {
      const off = document.createElement('canvas')
      off.width = meta.grid_w
      off.height = meta.grid_h
      const offCtx = off.getContext('2d')
      // ⚠ `ImageData` 는 길이가 `w*h*4` 와 다르면 **RangeError 를 던진다.**
      // 렌더 중 예외라 화면이 통째로 하얘진다 — 실제로 겪었다: 추출지점을 바꿔
      // 다시 인코딩하면 격자 크기가 달라지는데, 그 사이 오버레이는 옛 크기다.
      // 아래 effect 가 슬롯이 바뀔 때 오버레이를 비우지만, 비동기라 한 프레임
      // 어긋날 수 있다. 그때는 **안 그린다** — 오버레이 없는 그림이 흰 화면보다 낫다.
      const fits = overlay.data.length === meta.grid_w * meta.grid_h * 4
      if (offCtx && fits) {
        offCtx.putImageData(new ImageData(overlay.data, meta.grid_w, meta.grid_h), 0, 0)
        ctx.save()
        ctx.imageSmoothingEnabled = smooth
        ctx.globalAlpha = alpha
        ctx.drawImage(off, 0, 0, canvas.width, canvas.height)
        ctx.restore()
      }
    }

    const cw = canvas.width / meta.grid_w
    const ch = canvas.height / meta.grid_h

    if (showGrid) {
      ctx.save()
      ctx.strokeStyle = 'rgba(255,255,255,0.28)'
      ctx.lineWidth = 1
      for (let c = 1; c < meta.grid_w; c++) {
        ctx.beginPath(); ctx.moveTo(c * cw, 0); ctx.lineTo(c * cw, canvas.height); ctx.stroke()
      }
      for (let r = 1; r < meta.grid_h; r++) {
        ctx.beginPath(); ctx.moveTo(0, r * ch); ctx.lineTo(canvas.width, r * ch); ctx.stroke()
      }
      ctx.restore()
    }

    // 패딩 경계 — 이 위/왼쪽 패치는 내용이 없다
    if (meta.valid_row0 > 0 || meta.valid_col0 > 0) {
      ctx.save()
      ctx.strokeStyle = '#e66767'
      ctx.lineWidth = 2
      ctx.setLineDash([8, 6])
      if (meta.valid_row0 > 0) {
        ctx.beginPath(); ctx.moveTo(0, meta.valid_row0 * ch); ctx.lineTo(canvas.width, meta.valid_row0 * ch); ctx.stroke()
      }
      if (meta.valid_col0 > 0) {
        ctx.beginPath(); ctx.moveTo(meta.valid_col0 * cw, 0); ctx.lineTo(meta.valid_col0 * cw, canvas.height); ctx.stroke()
      }
      ctx.restore()
    }

    if (selected != null && selected >= 0) {
      const row = Math.floor(selected / meta.grid_w)
      const col = selected % meta.grid_w
      ctx.save()
      ctx.strokeStyle = '#ffffff'
      ctx.lineWidth = 2
      ctx.strokeRect(col * cw, row * ch, cw, ch)
      ctx.restore()
    }
  }, [loaded, overlay, alpha, showGrid, smooth, selected, meta])

  const handleClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const col = Math.floor(((e.clientX - rect.left) / rect.width) * meta.grid_w)
    const row = Math.floor(((e.clientY - rect.top) / rect.height) * meta.grid_h)
    if (col < 0 || row < 0 || col >= meta.grid_w || row >= meta.grid_h) return
    onPick(row * meta.grid_w + col)
  }, [meta, onPick])

  return (
    <canvas ref={canvasRef} onClick={handleClick}
      className="w-full h-auto rounded border border-neutral-700 bg-neutral-900 cursor-crosshair" />
  )
}

// ── 슬롯 카드 ─────────────────────────────────────────────────────────────────

/** 인코딩 결과가 지금 설정과 다른 모델/탭에서 나온 것인가. 섞어 비교하면 서버가 400 을 낸다. */
function isStale(img: SlotImage, cur: { policyType: string; checkpoint: string; tap: string; imageKey: string }): boolean {
  const m = img.result?.meta
  if (!m) return false
  return m.policy_type !== cur.policyType || (m.checkpoint || '') !== cur.checkpoint
    || m.tap !== cur.tap || (cur.imageKey !== '' && m.image_key !== cur.imageKey)
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

// 슬롯 하나가 카드 하나: 소스(캡처/파일 **추가**) → 이미지 격자 → 일괄 인코딩.
// 뷰 종류·불투명도·k 같은 공통 설정은 페이지가 쥐고 props 로 내려온다.
function SlotCard({ entry, cameras, blocked, removable, pendingCount, overlays, view, alpha, showGrid, smooth, selected, stale,
  onAddImages, onRemoveImage, onEncode, onRemove, onPick }: {
  entry: SlotEntry
  cameras: ReadyCam[]
  blocked: boolean  // 다른 슬롯이 인코딩 중 — 서버는 한 번에 하나만 처리한다
  removable: boolean
  pendingCount: number  // 아직 인코딩 안 됐거나 설정이 바뀐 이미지 수
  overlays: Record<string, Overlay>
  view: ViewKind
  alpha: number
  showGrid: boolean
  smooth: boolean
  selected: { sid: string; patch: number } | null
  stale: (img: SlotImage) => boolean
  onAddImages: (images: { name: string; src: string }[]) => void
  onRemoveImage: (imageId: number) => void
  onEncode: () => void
  onRemove: () => void
  onPick: (sid: string, patch: number) => void
}) {
  const { label, images, busy, error } = entry
  const [cameraId, setCameraId] = useState('')
  const [capError, setCapError] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!cameraId && cameras.length) setCameraId(cameras[0].id)
  }, [cameras, cameraId])

  const pickFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    const items = await Promise.all(Array.from(files).map(async (f) => ({ name: f.name, src: await blobToDataUrl(f) })))
    onAddImages(items)
  }

  // 캡처는 **지금** 프레임을 받아 목록에 넣는다. 인코딩 시점에 서버가 찍으면 여러 장이
  // 같은 순간의 프레임이 되어 "조명·위치를 바꿔 가며" 모으는 쓰임이 사라진다.
  const capture = async () => {
    setCapError('')
    try {
      const res = await fetch(`/api/cameras/${cameraId}/preview`)
      if (!res.ok) throw new Error(`카메라 프레임을 가져올 수 없습니다 (${res.status})`)
      const src = await blobToDataUrl(await res.blob())
      const cam = cameras.find((c) => c.id === cameraId)
      const stamp = new Date().toTimeString().slice(0, 8)
      onAddImages([{ name: `${cam?.name || cameraId} ${stamp}`, src }])
    } catch (err) {
      setCapError(err instanceof Error ? err.message : '캡처 실패')
    }
  }

  const encodedCount = images.filter((i) => i.result).length
  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold text-neutral-200">슬롯 {label}</span>
        <span className="text-[10px] text-neutral-500">{images.length}장{encodedCount > 0 && ` · 인코딩 ${encodedCount}`}</span>
        {busy && <span className="text-[10px] text-blue-400">인코딩 중…</span>}
        {!busy && blocked && <span className="text-[10px] text-neutral-500">대기 중</span>}
        <span className="flex-1" />
        {removable && (
          <button onClick={onRemove} disabled={busy} title="이 슬롯 제거 (이미지 포함)"
            className="px-2 py-0.5 rounded text-xs text-neutral-400 hover:text-red-300 hover:bg-neutral-700 disabled:opacity-40">
            ✕
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select value={cameraId} onChange={(e) => setCameraId(e.target.value)}
          className="px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100 max-w-[9rem]">
          {cameras.length === 0 && <option value="">카메라 없음</option>}
          {cameras.map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
        </select>
        <button onClick={capture} disabled={busy || !cameraId}
          className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-40 text-xs">
          캡처 추가
        </button>
        <button onClick={() => fileRef.current?.click()} disabled={busy}
          className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-40 text-xs">
          파일 추가
        </button>
        <input ref={fileRef} type="file" accept="image/*" multiple className="hidden"
          onChange={(e) => { void pickFiles(e.target.files); e.target.value = '' }} />
        <span className="flex-1" />
        <button onClick={onEncode} disabled={busy || blocked || pendingCount === 0}
          title={pendingCount === 0 ? '인코딩할 이미지가 없습니다' : '모델을 한 번만 로드해 한꺼번에 처리합니다'}
          className="px-3 py-1 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-xs font-medium">
          인코딩 ({pendingCount}장)
        </button>
      </div>
      {(error || capError) && <p className="text-[11px] text-red-400">{error || capError}</p>}

      {images.length === 0 ? (
        <p className="text-xs text-neutral-600">
          이미지가 없습니다. 카메라를 캡처하거나 파일을 여러 장 골라 넣은 뒤 한꺼번에 인코딩하세요.
        </p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
          {images.map((img) => {
            const m = img.result?.meta
            const sid = img.result?.sid
            const isStaleImg = stale(img)
            return (
              <div key={img.id} className="space-y-1">
                <div className="flex items-center gap-1.5">
                  <span className="text-[11px] text-neutral-300 truncate" title={img.name}>{img.name}</span>
                  {!img.result && !img.error && <span className="text-[10px] text-yellow-500 shrink-0">미인코딩</span>}
                  {isStaleImg && <span className="text-[10px] text-yellow-500 shrink-0">설정 변경됨</span>}
                  <span className="flex-1" />
                  <button onClick={() => onRemoveImage(img.id)} disabled={busy} title="이미지 제거"
                    className="px-1 rounded text-[11px] text-neutral-500 hover:text-red-300 hover:bg-neutral-700 disabled:opacity-40">
                    ✕
                  </button>
                </div>
                {img.result && sid ? (
                  <ProbeCanvas slot={img.result} overlay={overlays[sid] ?? null} alpha={alpha}
                    showGrid={showGrid} smooth={smooth}
                    selected={selected?.sid === sid ? selected.patch : null}
                    onPick={(patch) => onPick(sid, patch)} />
                ) : (
                  <img src={img.src} alt={img.name}
                    className={`w-full h-auto rounded border border-neutral-700 bg-neutral-900 ${img.error ? 'opacity-60' : 'opacity-80'}`} />
                )}
                {img.error && <div className="text-[10px] text-red-400">{img.error}</div>}
                {m && (
                  <div className="text-[10px] text-neutral-500 leading-relaxed">
                    <div>
                      격자 {m.grid_h}×{m.grid_w} · {m.dim}d · 패치 1개 ≈ 원본 {m.patch_px_orig_x}×{m.patch_px_orig_y}px
                      {m.valid_row0 > 0 && <span className="text-red-400"> · 상단 {m.valid_row0}행은 패딩</span>}
                    </div>
                    <div>
                      {m.encoder_source === 'base' ? '베이스 가중치' : '체크포인트 가중치'} · {m.device} · {m.elapsed_ms}ms
                      {m.feature_stats && <span> · norm 아웃라이어 {m.feature_stats.norm_outlier_ratio}배</span>}
                    </div>
                    {sid && overlays[sid]?.info && <div className="text-neutral-400">{overlays[sid]?.info}</div>}
                    {view === 'sim' && selected?.sid === sid && <div className="text-neutral-400">기준 패치 (클릭해서 옮길 수 있음)</div>}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── 페이지 ────────────────────────────────────────────────────────────────────

type BatchResult = { sid: string; meta: Meta } | { error: string }
type PatchMatrix = {
  patch: number; sids: string[]; valid: boolean[]
  matrix: (number | null)[][]; mean: number | null; min: number | null
}

export default function EncoderProbePage() {
  const [models, setModels] = useState<EncoderModel[]>([])
  const [cameras, setCameras] = useState<ReadyCam[]>([])
  const [policyType, setPolicyType] = useState<PolicyType>('smolvla')
  const [checkpoint, setCheckpoint] = useState('')
  const [imageKey, setImageKey] = useState('')
  const [tap, setTap] = useState('')

  // 정책 화면 스펙 — 프로브 가능 목록·베이스 문구·추출 지점·설명이 전부 여기서 온다.
  const { policies } = usePolicies()
  const probePolicies = policies.filter((p) => p.encoder_probe)
  const probe = usePolicyUi(policyType).encoder_probe

  // ⚠ 정책을 바꾸면 tap 을 **그 정책의 기본값으로** 갈아끼운다. 남겨두면
  // SmolVLA 의 `siglip` 이 ACT 요청에 실려 나가고, wrapper 가 되돌리긴 하지만
  // 화면에 뜬 값과 실제로 쓴 값이 달라진다.
  useEffect(() => {
    if (probe.taps.length === 0) { setTap(''); return }
    if (!probe.taps.some((t) => t.key === tap)) {
      setTap((probe.taps.find((t) => t.default) ?? probe.taps[0]).key)
    }
  }, [probe])
  const [device, setDevice] = useState('')

  // 처음엔 A 하나. "+ 슬롯 추가" 로 아래에 붙는다.
  const nextId = useRef(1)
  const [entries, setEntries] = useState<SlotEntry[]>([{ id: 0, label: 'A', images: [], busy: false, error: '' }])
  const patchEntry = useCallback((id: number, patch: Partial<SlotEntry>) => {
    setEntries((es) => es.map((e) => (e.id === id ? { ...e, ...patch } : e)))
  }, [])

  const [view, setView] = useState<ViewKind>('input')
  const [alpha, setAlpha] = useState(0.75)
  const [showGrid, setShowGrid] = useState(true)
  const [smooth, setSmooth] = useState(false)
  const [k, setK] = useState(4)
  const [isolate, setIsolate] = useState<number | null>(null)
  const [selected, setSelected] = useState<{ sid: string; patch: number } | null>(null)

  const [overlays, setOverlays] = useState<Record<string, Overlay>>({})
  // 같은 패치 위치의 이미지 쌍별 코사인 — 클릭 유사도 뷰에서 표로 보여 준다
  const [matrix, setMatrix] = useState<PatchMatrix | null>(null)
  const [note, setNote] = useState('')
  const [maxSessions, setMaxSessions] = useState(48)

  useEffect(() => {
    api.get<EncoderModel[]>('/encoder/models').then(setModels).catch(() => setModels([]))
    api.get<ReadyCam[]>('/cameras/ready').then(setCameras).catch(() => setCameras([]))
  }, [])

  const candidates = models.filter((m) => m.policy_type === policyType)

  // 정책을 바꾸면 체크포인트/카메라 키 선택을 초기화하고 **결과만** 비운다 — 이미지는 남겨
  // 새 정책으로 바로 다시 인코딩할 수 있게 한다.
  useEffect(() => {
    setCheckpoint('')
    setImageKey('')
    setEntries((es) => es.map((e) => ({
      ...e, error: '', images: e.images.map((i) => ({ ...i, result: null, error: '' })),
    })))
    setSelected(null)
    setOverlays({})
  }, [policyType])

  const cur = { policyType, checkpoint, tap, imageKey }
  const stale = (img: SlotImage) => isStale(img, cur)

  const addSlot = () => {
    setEntries((es) => [...es, { id: nextId.current++, label: nextLabel(es), images: [], busy: false, error: '' }])
  }
  // 서버 세션은 같이 지운다 — 상한(maxSessions)을 안 지운 세션이 차지하지 않게.
  const dropSessions = (images: SlotImage[]) => {
    for (const img of images) {
      if (img.result) api.delete(`/encoder/${img.result.sid}`).catch(() => {})
    }
  }
  const removeSlot = (id: number) => {
    const entry = entries.find((e) => e.id === id)
    if (!entry || entries.length <= 1) return
    dropSessions(entry.images)
    if (selected && entry.images.some((i) => i.result?.sid === selected.sid)) setSelected(null)
    setEntries((es) => es.filter((e) => e.id !== id))
  }
  const addImages = (slotId: number, items: { name: string; src: string }[]) => {
    setEntries((es) => es.map((e) => e.id === slotId
      ? { ...e, images: [...e.images, ...items.map((it) => ({ id: nextId.current++, ...it, result: null, error: '' }))] }
      : e))
  }
  const removeImage = (slotId: number, imageId: number) => {
    const img = entries.find((e) => e.id === slotId)?.images.find((i) => i.id === imageId)
    if (!img) return
    dropSessions([img])
    if (selected && img.result?.sid === selected.sid) setSelected(null)
    setEntries((es) => es.map((e) => e.id === slotId ? { ...e, images: e.images.filter((i) => i.id !== imageId) } : e))
  }

  // 슬롯의 미인코딩·설정 변경 이미지를 **한 요청**으로 보낸다 — 모델 로드는 한 번.
  const encodeSlot = useCallback(async (slotId: number) => {
    const entry = entries.find((e) => e.id === slotId)
    if (!entry) return
    const targets = entry.images.filter((i) => !i.result || isStale(i, cur))
    if (targets.length === 0) return
    patchEntry(slotId, { busy: true, error: '' })
    try {
      const res = await api.post<{ results: BatchResult[]; device_note: string; max_sessions: number }>(
        '/encoder/encode_batch', {
          policy_type: policyType,
          checkpoint_path: checkpoint,
          image_key: imageKey,
          tap,
          device,
          images: targets.map((t) => t.src),
        })
      // 갈아끼워지는 옛 결과의 서버 세션은 버린다
      dropSessions(targets)
      const byId = new Map(targets.map((t, i) => [t.id, res.results[i]]))
      setEntries((es) => es.map((e) => e.id !== slotId ? e : {
        ...e,
        images: e.images.map((img) => {
          const r = byId.get(img.id)
          if (!r) return img
          return 'error' in r
            ? { ...img, result: null, error: r.error }
            : { ...img, result: { sid: r.sid, meta: r.meta }, error: '' }
        }),
      }))
      setNote(res.device_note || '')
      setMaxSessions(res.max_sessions)
      const first = res.results.find((r): r is { sid: string; meta: Meta } => 'sid' in r)
      if (!imageKey && first?.meta.image_key) setImageKey(first.meta.image_key)
    } catch (err) {
      patchEntry(slotId, { error: err instanceof Error ? err.message : '인코딩 실패' })
    } finally {
      patchEntry(slotId, { busy: false })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entries, policyType, checkpoint, imageKey, tap, device, patchEntry])

  // 인코딩된 이미지 전부(슬롯 경계 없이). 기준(ref)은 첫 번째 — PCA 기저와 유사도
  // 질의를 하나로 맞춰야 여러 장의 색이 같은 의미를 갖는다.
  const filled = entries.flatMap((e) => e.images.filter((i) => i.result)).map((i) => i.result!)
  const sidKey = filled.map((s) => s.sid).join('|')

  // 뷰/선택/파라미터가 바뀌면 모든 이미지의 오버레이를 다시 만든다.
  useEffect(() => {
    let alive = true
    const refSid = filled[0]?.sid || ''

    async function build(slot: Slot): Promise<[string, Overlay]> {
      if (view === 'input') return [slot.sid, null]
      const meta = slot.meta
      const n = meta.n_patches
      const data: PixelBuffer = new Uint8ClampedArray(new ArrayBuffer(n * 4))

      if (view === 'pca') {
        const res = await api.get<{ rgb: number[][]; explained: number[] }>(
          `/encoder/${slot.sid}/pca?ref=${refSid}`)
        for (let i = 0; i < n; i++) {
          if (!isValidPatch(meta, i)) continue
          const [r, g, b] = res.rgb[i]
          data.set([r, g, b, 255], i * 4)
        }
        const total = res.explained.reduce((a, b) => a + b, 0)
        return [slot.sid, { data, info: `상위 3개 주성분이 분산의 ${(total * 100).toFixed(1)}% 설명` }]
      }

      if (view === 'sim') {
        if (!selected) return [slot.sid, null]
        const res = await api.get<{
          values: number[]; min: number; max: number; mean: number
          locality?: number; locality_baseline?: number; locality_ratio?: number
        }>(`/encoder/${slot.sid}/similarity?patch=${selected.patch}&ref=${selected.sid}`)
        const span = res.max - res.min || 1
        for (let i = 0; i < n; i++) {
          if (!isValidPatch(meta, i)) continue
          const t = Math.max(0, Math.min(1, (res.values[i] - res.min) / span))
          const [r, g, b] = seqColor(t)
          data.set([r, g, b, t * 255], i * 4)
        }
        const loc = res.locality_ratio != null
          ? ` · 국소성 ${res.locality_ratio.toFixed(2)} (상위20이 평균 ${res.locality}패치, 무작위면 ${res.locality_baseline})`
          : ''
        return [slot.sid, { data, info: `코사인 ${res.min.toFixed(2)}~${res.max.toFixed(2)} · 평균 ${res.mean.toFixed(2)}${loc}` }]
      }

      const res = await api.get<{ labels: number[]; k: number }>(`/encoder/${slot.sid}/kmeans?k=${k}`)
      for (let i = 0; i < n; i++) {
        const label = res.labels[i]
        if (label < 0 || !isValidPatch(meta, i)) continue
        if (isolate != null && label !== isolate) continue
        const [r, g, b] = hexToRgb(CATEGORICAL[label % CATEGORICAL.length])
        data.set([r, g, b, 255], i * 4)
      }
      return [slot.sid, { data, info: `${res.k}개 클러스터` }]
    }

    // 슬롯이 바뀌었으면 **먼저 버린다.** 새 격자에 옛 오버레이를 물리면
    // `ImageData` 가 던지고, 그건 렌더 중이라 화면이 하얘진다.
    setOverlays({})

    // 한 장의 실패(만료된 세션, 차원 불일치)가 나머지 오버레이를 지우지 않게 장별로 잡는다
    Promise.all(filled.map((s) => build(s).catch((): [string, Overlay] => [s.sid, null])))
      .then((pairs) => { if (alive) setOverlays(Object.fromEntries(pairs)) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, sidKey, selected, k, isolate])

  // 기준 패치가 정해지면 모든 이미지의 **그 자리** 특징끼리 한 번에 비교한다.
  useEffect(() => {
    if (view !== 'sim' || !selected || filled.length === 0) { setMatrix(null); return }
    let alive = true
    const sids = filled.map((s) => s.sid)
    api.get<PatchMatrix>(`/encoder/patch_matrix?patch=${selected.patch}&sids=${sids.join(',')}`)
      .then((m) => { if (alive) setMatrix(m) })
      .catch(() => { if (alive) setMatrix(null) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, sidKey, selected?.patch])

  // 표 머리글용 짧은 이름: 슬롯 글자 + 슬롯 안 순번 (A1, A2, B1 …)
  const imageTag = new Map<string, { tag: string; name: string }>()
  for (const e of entries) {
    e.images.forEach((img, i) => {
      if (img.result) imageTag.set(img.result.sid, { tag: `${e.label}${i + 1}`, name: img.name })
    })
  }

  const anySlot = filled[0] ?? null
  const anyBusy = entries.some((e) => e.busy)  // 서버가 한 번에 하나만 처리 — 모든 버튼을 함께 잠근다
  const randomEncoder = Boolean(anySlot?.meta.encoder_stats?.random_init)

  const VIEWS: { key: ViewKind; label: string; hint: string }[] = [
    { key: 'input', label: '입력 + 격자', hint: '모델이 실제로 보는 이미지와 패치 격자' },
    { key: 'sim', label: '클릭 유사도', hint: '패치를 클릭하면 모든 이미지에서 그 특징과의 코사인 유사도' },
    { key: 'pca', label: 'PCA → RGB', hint: '패치 특징을 3차원으로 줄여 색으로 표시 (기저는 첫 이미지 기준)' },
    { key: 'kmeans', label: 'k-means', hint: '패치를 군집화해 장면이 어떻게 갈리는지 확인' },
  ]

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">이미지 엔코더 프로브</h1>
        <p className="text-xs text-neutral-400">
          로봇을 움직이지 않고 이미지를 엔코더에만 통과시켜, 여러 장면에서 같은 영역이 비슷한 특징으로 잡히는지 확인합니다.
        </p>
      </div>

      {randomEncoder && (
        <div className="rounded-lg border border-red-600 bg-red-950/40 p-3 text-xs text-red-200">
          ⚠ 이 체크포인트의 비전 인코더는 <b>랜덤 초기화 상태</b>입니다 (사전학습 가중치가 로드되지 않음).
          학습 시 <code>load_vlm_weights=true</code>가 필요합니다. 아래 결과는 학습된 특징이 아닙니다.
        </div>
      )}

      {/* 모델 설정 */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-3">
            {probePolicies.map((p) => (
              <label key={p.type} className="flex items-center gap-1.5 text-xs cursor-pointer">
                <input type="radio" name="probePolicy" value={p.type} checked={policyType === p.type}
                  onChange={() => setPolicyType(p.type)} className="accent-blue-500" />
                <span className="text-neutral-300">{p.label}</span>
              </label>
            ))}
          </div>

          <div className="flex items-center gap-1.5">
            <span className="text-xs text-neutral-400">체크포인트</span>
            <select value={checkpoint} onChange={(e) => setCheckpoint(e.target.value)}
              className="px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100 max-w-[22rem]">
              {/* 두 정책 다 **학습 전 시작점**을 볼 수 있어야 비교가 성립한다.
                  무엇이 그 시작점인지는 정책마다 다르므로 스펙이 문구를 준다. */}
              {probe.base_label && <option value="">{probe.base_label}</option>}
              {candidates.map((m) => <option key={m.path} value={m.path}>{m.id}</option>)}
            </select>
          </div>

          {/* 뽑을 지점이 둘 이상일 때만 고르게 한다 — ACT 는 하나뿐이라 안 뜬다.
              예전엔 `policyType === 'smolvla'` 로 그 사실을 화면이 알고 있었다. */}
          {probe.taps.length > 1 && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-neutral-400">추출 지점</span>
              <select value={tap} onChange={(e) => setTap(e.target.value)}
                className="px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100">
                {probe.taps.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
            </div>
          )}

          {probe.image_key_select && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-neutral-400">카메라 키</span>
              <select value={imageKey} onChange={(e) => setImageKey(e.target.value)}
                className="px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100">
                <option value="">자동</option>
                {(anySlot?.meta.image_keys || []).map((key) => <option key={key} value={key}>{key}</option>)}
              </select>
            </div>
          )}

          <div className="flex items-center gap-1.5">
            <span className="text-xs text-neutral-400">디바이스</span>
            <select value={device} onChange={(e) => setDevice(e.target.value)}
              className="px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100">
              <option value="">자동</option>
              <option value="cuda">CUDA</option>
              <option value="cpu">CPU</option>
            </select>
          </div>
        </div>

        {/* 문구도 조건도 스펙이 준다. 예전엔 정책 이름 두 개가 여기 박혀 있었다. */}
        {probe.note && <p className="text-[11px] text-neutral-500 whitespace-pre-line">{probe.note.trim()}</p>}
        {note && <p className="text-[11px] text-yellow-500">{note}</p>}
        {filled.length > maxSessions && (
          <p className="text-[11px] text-yellow-500">
            인코딩된 이미지가 서버 상한({maxSessions}장)을 넘었습니다 — 오래된 것부터 서버에서 지워져 오버레이가 비게 됩니다. 안 쓰는 이미지를 제거하세요.
          </p>
        )}
      </div>

      {anyBusy && (
        <div className="rounded border border-blue-700 bg-blue-950/40 px-3 py-2 text-xs text-blue-200">
          인코딩 중… 모델 로드에 <b>4초 안팎</b>, 그 뒤 장당 1초 미만이 더 걸립니다.
          컨테이너·서버를 막 켠 뒤 첫 실행은 20~30초까지 걸릴 수 있습니다. 창을 닫지 말고 기다려 주세요.
        </div>
      )}

      {/* 뷰 — 아래 모든 슬롯의 모든 이미지에 공통으로 적용 */}
      {anySlot && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {VIEWS.map((v) => (
              <button key={v.key} onClick={() => setView(v.key)} title={v.hint}
                className={`px-2.5 py-1 text-xs rounded ${view === v.key ? 'bg-blue-600 text-white' : 'bg-neutral-700 text-neutral-300 hover:bg-neutral-600'}`}>
                {v.label}
              </button>
            ))}
            <span className="text-[11px] text-neutral-500 ml-1">
              {VIEWS.find((v) => v.key === view)?.hint}
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-4 text-xs">
            <label className="flex items-center gap-1.5">
              <span className="text-neutral-400">불투명도</span>
              <input type="range" min={0} max={1} step={0.05} value={alpha}
                onChange={(e) => setAlpha(Number(e.target.value))} className="accent-blue-500 w-28" />
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input type="checkbox" checked={showGrid} onChange={(e) => setShowGrid(e.target.checked)}
                className="accent-blue-500" />
              <span className="text-neutral-400">패치 격자</span>
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input type="checkbox" checked={smooth} onChange={(e) => setSmooth(e.target.checked)}
                className="accent-blue-500" />
              <span className="text-neutral-400">보간</span>
            </label>
            {view === 'kmeans' && (
              <label className="flex items-center gap-1.5">
                <span className="text-neutral-400">k</span>
                <input type="number" min={2} max={MAX_K} value={k}
                  onChange={(e) => { setK(Math.max(2, Math.min(MAX_K, Number(e.target.value)))); setIsolate(null) }}
                  className="w-14 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100" />
              </label>
            )}
          </div>

          {/* 범례 */}
          {view === 'sim' && (
            <div className="flex items-center gap-2 max-w-xs">
              <span className="text-[10px] text-neutral-500">낮음</span>
              <div className="flex-1 h-2 rounded"
                style={{ background: `linear-gradient(to right, rgb(${SEQ_STOPS[0].join(',')}), rgb(${SEQ_STOPS[1].join(',')}), rgb(${SEQ_STOPS[2].join(',')}))` }} />
              <span className="text-[10px] text-neutral-500">높음</span>
              {!selected && <span className="text-[11px] text-yellow-500 ml-2">아무 이미지에서나 패치를 클릭하세요 — 모든 이미지에 유사도가 그려집니다</span>}
            </div>
          )}
          {view === 'sim' && selected && matrix && matrix.sids.length > 1 && (() => {
            const gw = filled.find((s) => s.sid === selected.sid)?.meta.grid_w ?? 0
            const row = gw ? Math.floor(selected.patch / gw) : 0
            const col = gw ? selected.patch % gw : 0
            return (
              <div className="space-y-1.5">
                <div className="text-[11px] text-neutral-400">
                  패치 <b className="text-neutral-200">({row}, {col})</b> 자리의 특징 벡터가 이미지끼리 얼마나 같은가 (코사인)
                  {matrix.mean != null && (
                    <span className="ml-2">· 평균 <b className="text-neutral-200">{matrix.mean.toFixed(2)}</b> · 최소 <b className="text-neutral-200">{matrix.min?.toFixed(2)}</b></span>
                  )}
                </div>
                <div className="overflow-x-auto">
                  <table className="text-[11px] border-collapse">
                    <thead>
                      <tr>
                        <th className="px-1.5 py-1 text-left text-neutral-500 font-normal" />
                        {matrix.sids.map((sid) => {
                          const t = imageTag.get(sid)
                          return (
                            <th key={sid} title={t?.name} className={`px-1.5 py-1 font-semibold ${sid === selected.sid ? 'text-white' : 'text-neutral-300'}`}>
                              {t?.tag ?? '?'}
                            </th>
                          )
                        })}
                      </tr>
                    </thead>
                    <tbody>
                      {matrix.sids.map((rsid, i) => {
                        const rt = imageTag.get(rsid)
                        return (
                          <tr key={rsid}>
                            <th title={rt?.name} className={`px-1.5 py-1 text-left font-semibold whitespace-nowrap ${rsid === selected.sid ? 'text-white' : 'text-neutral-300'}`}>
                              {rt?.tag ?? '?'} <span className="font-normal text-neutral-500 max-w-[10rem] inline-block truncate align-bottom">{rt?.name}</span>
                            </th>
                            {matrix.sids.map((csid, j) => {
                              const v = matrix.matrix[i]?.[j]
                              if (v == null) {
                                return <td key={csid} className="px-1.5 py-1 text-center text-neutral-600 border border-neutral-700/60" title={matrix.valid[i] && matrix.valid[j] ? '' : '이 자리가 격자 밖·패딩이거나 차원이 다르거나 세션이 만료됨'}>—</td>
                              }
                              const t = Math.max(0, Math.min(1, v))
                              const [r, g, b] = seqColor(t)
                              const diag = i === j
                              return (
                                <td key={csid} className={`px-1.5 py-1 text-center tabular-nums border border-neutral-700/60 ${diag ? 'text-neutral-500' : 'text-neutral-900 font-medium'}`}
                                  style={diag ? undefined : { background: `rgba(${r | 0},${g | 0},${b | 0},${0.25 + 0.75 * t})` }}>
                                  {v.toFixed(2)}
                                </td>
                              )
                            })}
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
                <div className="text-[10px] text-neutral-500">
                  1.00 = 같은 방향. 같은 물체가 같은 자리에 있는 이미지끼리는 높고, 다른 물체·빈 배경으로 바뀐 이미지는 낮아야 그 자리 특징이 내용을 따라간다는 뜻입니다. 전부 높으면(0.9↑) 위치만 보고 내용은 못 보는 것.
                </div>
              </div>
            )
          })()}
          {view === 'kmeans' && (
            <div className="flex flex-wrap items-center gap-1.5">
              {Array.from({ length: k }, (_, i) => (
                <button key={i} onClick={() => setIsolate(isolate === i ? null : i)}
                  className={`flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] border ${isolate === i ? 'border-white text-white' : 'border-neutral-600 text-neutral-400'}`}>
                  <span className="w-2.5 h-2.5 rounded-sm inline-block" style={{ background: CATEGORICAL[i] }} />
                  {i}
                </button>
              ))}
              <span className="text-[10px] text-neutral-500 ml-1">
                칩을 누르면 해당 클러스터만 표시 (색만으로 구분하지 마세요)
              </span>
            </div>
          )}

          <p className="text-[11px] text-neutral-500 leading-relaxed">
            판정 기준: 물체를 클릭했을 때 <b>국소성</b>이 낮을수록(0.3 이하면 양호, 0.7 이상이면 장면 전체가
            비슷하게 보인다는 뜻) 그 물체를 구분한다는 뜻입니다. 다른 이미지에서는 같은 물체 자리에 높은 값이
            몰리고 나머지는 낮아야 그 영역이 비슷한 특징으로 뽑힌다는 뜻입니다. 조명/위치를 바꾼 여러 장에서
            같은 물체가 같은 색(PCA)·같은 클러스터로 남는지도 함께 보세요. 대상이 패치 1~2개보다 작으면 엔코더가
            잡을 수 없습니다. PCA가 얼룩덜룩하면 norm 아웃라이어가 큰 것으로, SigLIP 계열에서는 정상입니다 —
            판정은 국소성으로 하세요.
          </p>
        </div>
      )}

      {/* 슬롯 카드 — 세로로 쌓이고, 아래 버튼으로 추가 */}
      {entries.map((entry) => (
        <SlotCard key={entry.id} entry={entry} cameras={cameras}
          blocked={anyBusy && !entry.busy} removable={entries.length > 1}
          pendingCount={entry.images.filter((i) => !i.result || stale(i)).length}
          overlays={overlays} view={view} alpha={alpha} showGrid={showGrid} smooth={smooth}
          selected={selected} stale={stale}
          onAddImages={(items) => addImages(entry.id, items)}
          onRemoveImage={(imageId) => removeImage(entry.id, imageId)}
          onEncode={() => { void encodeSlot(entry.id) }}
          onRemove={() => removeSlot(entry.id)}
          onPick={(sid, patch) => { setView('sim'); setSelected({ sid, patch }) }} />
      ))}

      <button onClick={addSlot}
        className="w-full rounded-lg border border-dashed border-neutral-600 hover:border-neutral-400 hover:bg-neutral-800/60 py-2.5 text-xs text-neutral-400 hover:text-neutral-200">
        + 슬롯 추가 (다른 조건의 장면 묶음을 같은 기준으로 비교)
      </button>
    </div>
  )
}
