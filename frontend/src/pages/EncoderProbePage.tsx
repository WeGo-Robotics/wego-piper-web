import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'
import { type ReadyCam } from '../types/camera'

type PolicyType = 'smolvla' | 'act'
type SlotKey = 'A' | 'B'
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

// ── 슬롯 ──────────────────────────────────────────────────────────────────────

function SlotPanel({ label, slot, cameras, busy, blocked, error, onCapture, onUpload }: {
  label: SlotKey
  slot: Slot | null
  cameras: ReadyCam[]
  busy: boolean
  blocked: boolean  // 다른 슬롯이 인코딩 중 — 서버는 한 번에 하나만 처리한다
  error: string
  onCapture: (cameraId: string) => void
  onUpload: (dataUrl: string) => void
}) {
  const [cameraId, setCameraId] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!cameraId && cameras.length) setCameraId(cameras[0].id)
  }, [cameras, cameraId])

  const pickFile = (file: File | undefined) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => onUpload(String(reader.result))
    reader.readAsDataURL(file)
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-neutral-300">슬롯 {label}</span>
        {slot && <span className="text-[10px] text-neutral-500">{slot.meta.grid_h}×{slot.meta.grid_w} · {slot.meta.dim}d</span>}
        {busy && <span className="text-[10px] text-blue-400">인코딩 중…</span>}
        {!busy && blocked && <span className="text-[10px] text-neutral-500">대기 중</span>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <select value={cameraId} onChange={(e) => setCameraId(e.target.value)}
          className="px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100 max-w-[9rem]">
          {cameras.length === 0 && <option value="">카메라 없음</option>}
          {cameras.map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
        </select>
        <button onClick={() => onCapture(cameraId)} disabled={busy || blocked || !cameraId}
          className="px-2 py-1 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-xs">
          캡처 후 인코딩
        </button>
        <button onClick={() => fileRef.current?.click()} disabled={busy || blocked}
          className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-40 text-xs">
          파일 선택
        </button>
        <input ref={fileRef} type="file" accept="image/*" className="hidden"
          onChange={(e) => { pickFile(e.target.files?.[0]); e.target.value = '' }} />
      </div>
      {error && <p className="text-[11px] text-red-400">{error}</p>}
    </div>
  )
}

// ── 페이지 ────────────────────────────────────────────────────────────────────

export default function EncoderProbePage() {
  const [models, setModels] = useState<EncoderModel[]>([])
  const [cameras, setCameras] = useState<ReadyCam[]>([])
  const [policyType, setPolicyType] = useState<PolicyType>('smolvla')
  const [checkpoint, setCheckpoint] = useState('')
  const [imageKey, setImageKey] = useState('')
  const [tap, setTap] = useState<'siglip' | 'connector'>('siglip')
  const [device, setDevice] = useState('')

  const [slots, setSlots] = useState<Record<SlotKey, Slot | null>>({ A: null, B: null })
  const [busy, setBusy] = useState<Record<SlotKey, boolean>>({ A: false, B: false })
  const [errors, setErrors] = useState<Record<SlotKey, string>>({ A: '', B: '' })

  const [view, setView] = useState<ViewKind>('input')
  const [alpha, setAlpha] = useState(0.75)
  const [showGrid, setShowGrid] = useState(true)
  const [smooth, setSmooth] = useState(false)
  const [k, setK] = useState(4)
  const [isolate, setIsolate] = useState<number | null>(null)
  const [selected, setSelected] = useState<{ slot: SlotKey; patch: number } | null>(null)

  const [overlays, setOverlays] = useState<Record<SlotKey, Overlay>>({ A: null, B: null })
  const [note, setNote] = useState('')

  useEffect(() => {
    api.get<EncoderModel[]>('/encoder/models').then(setModels).catch(() => setModels([]))
    api.get<ReadyCam[]>('/cameras/ready').then(setCameras).catch(() => setCameras([]))
  }, [])

  const candidates = models.filter((m) => m.policy_type === policyType)

  // 정책을 바꾸면 체크포인트/카메라 키 선택을 초기화한다
  useEffect(() => {
    setCheckpoint('')
    setImageKey('')
    setSlots({ A: null, B: null })
    setSelected(null)
    setOverlays({ A: null, B: null })
  }, [policyType])

  const encode = useCallback(async (key: SlotKey, payload: Record<string, unknown>) => {
    setBusy((b) => ({ ...b, [key]: true }))
    setErrors((e) => ({ ...e, [key]: '' }))
    try {
      const res = await api.post<{ sid: string; meta: Meta; device_note: string }>('/encoder/encode', {
        policy_type: policyType,
        checkpoint_path: checkpoint,
        image_key: imageKey,
        tap,
        device,
        ...payload,
      })
      setSlots((s) => ({ ...s, [key]: { sid: res.sid, meta: res.meta } }))
      setNote(res.device_note || '')
      if (!imageKey && res.meta.image_key) setImageKey(res.meta.image_key)
    } catch (err) {
      setErrors((e) => ({ ...e, [key]: err instanceof Error ? err.message : '인코딩 실패' }))
    } finally {
      setBusy((b) => ({ ...b, [key]: false }))
    }
  }, [policyType, checkpoint, imageKey, tap, device])

  // 뷰/선택/파라미터가 바뀌면 두 슬롯의 오버레이를 다시 만든다.
  // PCA와 유사도는 A를 기준(ref)으로 삼아야 두 이미지의 색이 같은 의미를 갖는다.
  useEffect(() => {
    let alive = true
    const refSid = slots.A?.sid || slots.B?.sid || ''

    async function build(key: SlotKey): Promise<Overlay> {
      const slot = slots[key]
      if (!slot || view === 'input') return null
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
        return { data, info: `상위 3개 주성분이 분산의 ${(total * 100).toFixed(1)}% 설명` }
      }

      if (view === 'sim') {
        if (!selected) return null
        const ref = slots[selected.slot]
        if (!ref) return null
        const res = await api.get<{
          values: number[]; min: number; max: number; mean: number
          locality?: number; locality_baseline?: number; locality_ratio?: number
        }>(`/encoder/${slot.sid}/similarity?patch=${selected.patch}&ref=${ref.sid}`)
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
        return { data, info: `코사인 ${res.min.toFixed(2)}~${res.max.toFixed(2)}${loc}` }
      }

      const res = await api.get<{ labels: number[]; k: number }>(`/encoder/${slot.sid}/kmeans?k=${k}`)
      for (let i = 0; i < n; i++) {
        const label = res.labels[i]
        if (label < 0 || !isValidPatch(meta, i)) continue
        if (isolate != null && label !== isolate) continue
        const [r, g, b] = hexToRgb(CATEGORICAL[label % CATEGORICAL.length])
        data.set([r, g, b, 255], i * 4)
      }
      return { data, info: `${res.k}개 클러스터` }
    }

    // 슬롯이 바뀌었으면 **먼저 버린다.** 새 격자에 옛 오버레이를 물리면
    // `ImageData` 가 던지고, 그건 렌더 중이라 화면이 하얘진다.
    setOverlays({ A: null, B: null })

    Promise.all([build('A'), build('B')])
      .then(([a, b]) => { if (alive) setOverlays({ A: a, B: b }) })
      .catch(() => { if (alive) setOverlays({ A: null, B: null }) })
    return () => { alive = false }
  }, [view, slots, selected, k, isolate])

  const anySlot = slots.A || slots.B
  const anyBusy = busy.A || busy.B  // 서버가 한 번에 하나만 처리 — 양쪽 버튼을 함께 잠근다
  const randomEncoder = Boolean(anySlot?.meta.encoder_stats?.random_init)

  const VIEWS: { key: ViewKind; label: string; hint: string }[] = [
    { key: 'input', label: '입력 + 격자', hint: '모델이 실제로 보는 이미지와 패치 격자' },
    { key: 'sim', label: '클릭 유사도', hint: '패치를 클릭하면 그 특징과의 코사인 유사도' },
    { key: 'pca', label: 'PCA → RGB', hint: '패치 특징을 3차원으로 줄여 색으로 표시' },
    { key: 'kmeans', label: 'k-means', hint: '패치를 군집화해 장면이 어떻게 갈리는지 확인' },
  ]

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">이미지 엔코더 프로브</h1>
        <p className="text-xs text-neutral-400">
          로봇을 움직이지 않고 이미지 한 장을 엔코더에만 통과시켜, 지금 장면에서 특징이 제대로 잡히는지 확인합니다.
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
            {(['smolvla', 'act'] as PolicyType[]).map((p) => (
              <label key={p} className="flex items-center gap-1.5 text-xs cursor-pointer">
                <input type="radio" name="probePolicy" value={p} checked={policyType === p}
                  onChange={() => setPolicyType(p)} className="accent-blue-500" />
                <span className="text-neutral-300">{p.toUpperCase()}</span>
              </label>
            ))}
          </div>

          <div className="flex items-center gap-1.5">
            <span className="text-xs text-neutral-400">체크포인트</span>
            <select value={checkpoint} onChange={(e) => setCheckpoint(e.target.value)}
              className="px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100 max-w-[22rem]">
              {policyType === 'smolvla' && <option value="">베이스 SigLIP (체크포인트 없이)</option>}
              {policyType === 'act' && !checkpoint && <option value="">선택하세요</option>}
              {candidates.map((m) => <option key={m.path} value={m.path}>{m.id}</option>)}
            </select>
          </div>

          {policyType === 'smolvla' && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-neutral-400">추출 지점</span>
              <select value={tap} onChange={(e) => setTap(e.target.value as 'siglip' | 'connector')}
                className="px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100">
                <option value="siglip">비전 타워 (32×32)</option>
                <option value="connector">VLM 입력 토큰 (8×8)</option>
              </select>
            </div>
          )}

          {policyType === 'act' && (
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

        {policyType === 'smolvla' && !checkpoint && (
          <p className="text-[11px] text-neutral-500">
            SmolVLA는 <code>freeze_vision_encoder=true</code>면 학습 중 비전 인코더가 바뀌지 않습니다.
            체크포인트 없이 베이스만으로도 "이 장면에서 SigLIP이 쓸 만한가"를 미리 볼 수 있습니다.
          </p>
        )}
        {policyType === 'act' && (
          <p className="text-[11px] text-neutral-500">
            ACT의 ResNet 백본은 정책과 함께 학습되므로 체크포인트마다 다릅니다 — 사후 진단용입니다.
          </p>
        )}
        {note && <p className="text-[11px] text-yellow-500">{note}</p>}
      </div>

      {/* 소스 */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        {anyBusy && (
          <div className="rounded border border-blue-700 bg-blue-950/40 px-3 py-2 text-xs text-blue-200">
            인코딩 중… 요청마다 모델을 새로 로드하므로 <b>4초 안팎</b> 걸립니다.
            컨테이너·서버를 막 켠 뒤 첫 실행은 20~30초까지 걸릴 수 있습니다. 창을 닫지 말고 기다려 주세요.
          </div>
        )}
        <div className="grid md:grid-cols-2 gap-4">
          {(['A', 'B'] as SlotKey[]).map((key) => (
            <SlotPanel key={key} label={key} slot={slots[key]} cameras={cameras}
              busy={busy[key]} blocked={anyBusy && !busy[key]} error={errors[key]}
              onCapture={(cameraId) => encode(key, { source: 'camera', camera_id: cameraId })}
              onUpload={(dataUrl) => encode(key, { source: 'upload', image_b64: dataUrl })} />
          ))}
        </div>
      </div>

      {/* 뷰 */}
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
              {!selected && <span className="text-[11px] text-yellow-500 ml-2">이미지에서 패치를 클릭하세요</span>}
            </div>
          )}
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

          <div className="grid md:grid-cols-2 gap-4">
            {(['A', 'B'] as SlotKey[]).map((key) => {
              const slot = slots[key]
              if (!slot) return <div key={key} className="text-xs text-neutral-600">슬롯 {key} 비어 있음</div>
              const m = slot.meta
              return (
                <div key={key} className="space-y-1.5">
                  <ProbeCanvas slot={slot} overlay={overlays[key]} alpha={alpha}
                    showGrid={showGrid} smooth={smooth}
                    selected={selected?.slot === key ? selected.patch : null}
                    onPick={(patch) => { setView('sim'); setSelected({ slot: key, patch }) }} />
                  <div className="text-[10px] text-neutral-500 leading-relaxed">
                    <div>
                      슬롯 {key} · 격자 {m.grid_h}×{m.grid_w} · 패치 1개 ≈ 원본 {m.patch_px_orig_x}×{m.patch_px_orig_y}px
                      {m.valid_row0 > 0 && <span className="text-red-400"> · 상단 {m.valid_row0}행은 패딩</span>}
                    </div>
                    <div>
                      {m.encoder_source === 'base' ? '베이스 가중치' : '체크포인트 가중치'} · {m.device} · {m.elapsed_ms}ms
                      {m.feature_stats && <span> · norm 아웃라이어 {m.feature_stats.norm_outlier_ratio}배</span>}
                    </div>
                    {overlays[key]?.info && <div className="text-neutral-400">{overlays[key]?.info}</div>}
                  </div>
                </div>
              )
            })}
          </div>

          <p className="text-[11px] text-neutral-500 leading-relaxed">
            판정 기준: 물체를 클릭했을 때 <b>국소성</b>이 낮을수록(0.3 이하면 양호, 0.7 이상이면 장면 전체가
            비슷하게 보인다는 뜻) 그 물체를 구분한다는 뜻입니다. 조명/위치를 바꾼 두 장에서 같은 물체가 같은
            색(PCA)·같은 클러스터로 남는지도 함께 보세요. 대상이 패치 1~2개보다 작으면 엔코더가 잡을 수 없습니다.
            PCA가 얼룩덜룩하면 norm 아웃라이어가 큰 것으로, SigLIP 계열에서는 정상입니다 — 판정은 국소성으로 하세요.
          </p>
        </div>
      )}
    </div>
  )
}
