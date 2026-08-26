/**
 * YOLO 데모 — 시연용 뷰. 어노테이트 프리뷰를 크게 띄우고 검출 라벨을 얹는다.
 *
 * 운영·설정은 비전·판단 페이지 몫이고, 여기는 보여주는 화면이다. 그래도 시연 현장에서
 * 페이지를 오가지 않도록 최소한의 시작/정지(세그먼트 전체 선택 기본)는 넣는다.
 * 프리뷰는 yolod 기본 fps(5)에 맞춰 200ms 로 갱신, 검출 텍스트는 500ms 폴링.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'

type DetObject = { label: string; conf: number; bbox: number[]; center: number[] }
type DetPayload = {
  ts: number
  cam: string
  frame_seq: number
  size: number[]
  objects: DetObject[]
  text: string
  /** ultralytics 추론 단계 소요(ms) — 구버전 yolod 페이로드에는 없다 */
  infer_ms?: number | null
  speed_ms?: { preprocess?: number; inference?: number; postprocess?: number }
  /** 카메라별 검출 횟수 카운터 — fps 는 이걸로 잰다 (frame_seq 는 카메라 발행률) */
  det_seq?: number
}
type YoloMeta = {
  model: string
  device: string
  conf: number
  fps: number
  imgsz?: number
  /** alias→세그먼트 매핑 — "이 장면 캡처"가 원본 세그먼트로 돌아가는 길 */
  cams?: Record<string, string>
  task?: string
  classes?: number
  layers?: number
  params?: number
  gflops?: number
}
type YoloStatus = { state: string; pid: number | null; cams: string[]; model: YoloMeta | null }
type YoloModel = {
  file: string
  family: string
  label: string
  params_m?: number
  size_mb?: number
  downloaded?: boolean
  /** 학습 곁 JSON 에서 (커스텀 가중치만) */
  map50?: number | null
  classes_n?: number | null
  trained_on?: string | null
}

/** 서버 카탈로그(/vision/models)가 정본 — 못 받았을 때(구버전 게이트웨이 등)만 쓰는 폴백. */
const FALLBACK_MODELS: YoloModel[] = [
  { file: 'yolo11n.pt', family: 'YOLO11', label: 'nano' },
  { file: 'yolo11s.pt', family: 'YOLO11', label: 'small' },
  { file: 'yolo11m.pt', family: 'YOLO11', label: 'medium' },
  { file: 'yolo11l.pt', family: 'YOLO11', label: 'large' },
  { file: 'yolo11x.pt', family: 'YOLO11', label: 'xlarge' },
  { file: 'yolov8n.pt', family: 'YOLOv8', label: 'nano' },
  { file: 'yolov8s.pt', family: 'YOLOv8', label: 'small' },
  { file: 'yolov8m.pt', family: 'YOLOv8', label: 'medium' },
  { file: 'yolov8l.pt', family: 'YOLOv8', label: 'large' },
  { file: 'yolov8x.pt', family: 'YOLOv8', label: 'xlarge' },
  { file: 'yolov5nu.pt', family: 'YOLOv5u', label: 'nano' },
  { file: 'yolov5su.pt', family: 'YOLOv5u', label: 'small' },
  { file: 'yolov5mu.pt', family: 'YOLOv5u', label: 'medium' },
]

const ALIASES = ['top', 'hand', 'side', 'extra']

/** rs_335122271186_color → rs_…1186_color */
const shortId = (s: string) => (s.length > 24 ? `${s.slice(0, 6)}…${s.slice(-10)}` : s)

export default function YoloDemoPage() {
  const [status, setStatus] = useState<YoloStatus | null>(null)
  const [detections, setDetections] = useState<Record<string, DetPayload>>({})
  const [segments, setSegments] = useState<string[]>([])
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [models, setModels] = useState<YoloModel[]>(FALLBACK_MODELS)
  // ?model=<file> 로 열면 그 가중치가 선택된 채 시작 (학습 완료 → 데모 단축 경로)
  const urlModel = useRef(new URLSearchParams(window.location.search).get('model'))
  const [model, setModel] = useState(() => urlModel.current ?? 'yolo11n.pt')
  // 카탈로그가 오면 기본값을 **로컬에 있는 가중치**로 한 번 맞춘다.
  // 안 맞추면 시작을 누르는 순간 100MB 를 받느라 멈춘 것처럼 보인다.
  const defaultFixed = useRef(false)
  const [fps, setFps] = useState(5)
  const [imgsz, setImgsz] = useState(640)
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [imgTick, setImgTick] = useState(0)

  const running = status?.state === 'running' || status?.state === 'starting'
  // yolod 가 버스에 발행하는 자기소개 — 돌고 있는데 없으면 아직 모델 로드 중
  const meta = status?.model ?? null

  // ── YOLO 학습 데이터셋으로 캡처 (feature/yolo-training.md 데모 훅) ──
  const [capDatasets, setCapDatasets] = useState<string[]>([])
  const [capTarget, setCapTarget] = useState('')
  const [capCount, setCapCount] = useState<Record<string, number>>({})

  useEffect(() => {
    api.get<{ datasets: { name: string }[] }>('/yolo/datasets')
      .then((r) => {
        setCapDatasets(r.datasets.map((d) => d.name))
        setCapTarget((cur) => cur || (r.datasets[0]?.name ?? ''))
      })
      .catch(() => {})
  }, [])

  const captureScene = async (alias: string) => {
    const seg = meta?.cams?.[alias]
    if (!capTarget || !seg) return
    try {
      await api.post(`/yolo/datasets/${capTarget}/capture`, { cam: seg })
      setCapCount((c) => ({ ...c, [alias]: (c[alias] ?? 0) + 1 }))
    } catch (e) {
      setError(e instanceof Error ? e.message : '캡처 실패')
    }
  }

  // 검출 fps — 검출 카운터 증분 / 생산자 타임스탬프 증분.
  // ⚠ frame_seq(카메라 발행 카운터)를 브라우저 폴 간격으로 나누면 안 된다:
  // 값은 검출률이 아니라 카메라 발행률(~30)이 되고, 검출(200ms)·폴(500ms)
  // 격자가 어긋나며 맥놀이로 숫자가 널뛴다. 둘 다 생산자 쪽 값이면 폴 타이밍과 무관하다.
  const fpsRef = useRef<Record<string, { det: number; ts: number; fps: number }>>({})

  const poll = useCallback(async () => {
    try {
      setStatus(await api.get<YoloStatus>('/vision/status'))
      const det = await api.get<Record<string, DetPayload>>('/vision/detections')
      for (const [name, d] of Object.entries(det)) {
        if (d.det_seq == null) continue // 구버전 yolod — fps 표시 생략
        const prev = fpsRef.current[name]
        if (!prev || d.det_seq < prev.det) {
          fpsRef.current[name] = { det: d.det_seq, ts: d.ts, fps: 0 } // 최초 또는 yolod 재시작
        } else if (d.det_seq > prev.det && d.ts > prev.ts) {
          const inst = (d.det_seq - prev.det) / (d.ts - prev.ts)
          // ts 는 프레임 캡처 시각이라 ±1프레임 양자화가 남는다 — 가볍게 EMA
          const fps = prev.fps > 0 ? prev.fps * 0.6 + inst * 0.4 : inst
          fpsRef.current[name] = { det: d.det_seq, ts: d.ts, fps }
        }
      }
      setDetections(det)
      setError(null)
    } catch {
      /* 게이트웨이 순단은 다음 폴에 회복 */
    }
  }, [])

  useEffect(() => {
    void poll()
    const detTimer = setInterval(() => void poll(), 500)
    return () => clearInterval(detTimer)
  }, [poll])

  // 프리뷰 폴링 주기는 실제 검출 fps 를 따라간다 (66ms 하한 — 그보다 촘촘한 건 낭비)
  const previewMs = running && meta?.fps ? Math.max(66, Math.min(1000, 1000 / meta.fps)) : 200
  useEffect(() => {
    const imgTimer = setInterval(() => setImgTick((t) => t + 1), previewMs)
    return () => clearInterval(imgTimer)
  }, [previewMs])

  // 안 돌고 있을 때만 세그먼트·모델 목록이 필요하다 (시작 UI)
  useEffect(() => {
    if (running) return
    // 자동 선택하지 않는다 — depth/infrared 세그먼트까지 딸려 들어간다.
    // 사용자가 썸네일을 보고 고른다.
    api.get<{ segments: string[] }>('/vision/segments')
      .then((r) => setSegments(r.segments))
      .catch(() => {})
    api.get<{ models: YoloModel[] }>('/vision/models')
      .then((r) => { setModels(r.models); pickLocalDefault(r.models) })
      .catch(() => {})
  }, [running])

  /**
   * 목록이 오면 기본 선택을 **이미 받아둔 가중치**로 옮긴다.
   *
   * ⚠ 목록에서 안 받은 것을 빼지는 않는다. 그러면 새 기기에서 목록이 통째로
   *   비고 첫 모델을 받을 길이 사라진다 — 안 받은 것은 `(다운로드 필요)` 로
   *   그대로 보인다. 여기서 바꾸는 것은 **기본값뿐**이다.
   *
   * 한 번만 한다. 그리고 `?model=` 로 지정해 왔거나(학습 직후 단축 경로)
   * 사용자가 이미 고른 뒤면 건드리지 않는다 — 고른 것을 되돌리는 화면이
   * 제일 나쁘다.
   */
  const pickLocalDefault = (list: YoloModel[]) => {
    if (defaultFixed.current || urlModel.current) return
    defaultFixed.current = true
    setModel((cur) => {
      if (list.some((m) => m.file === cur && m.downloaded === true)) return cur
      // 카탈로그 순서를 그대로 따른다 (작은 것 → 큰 것, 표준 → 커스텀)
      return list.find((m) => m.downloaded === true)?.file ?? cur
    })
  }

  const handleStart = async () => {
    const chosen = segments.filter((s) => picked.has(s))
    if (chosen.length === 0) { setError('구독할 카메라 세그먼트가 없습니다'); return }
    setBusy(true)
    setError(null)
    try {
      const cams = Object.fromEntries(chosen.map((s, i) => [ALIASES[i] ?? `cam${i}`, s]))
      await api.post('/vision/start', { cams, model, conf: 0.25, fps, imgsz })
    } catch (e) {
      setError(e instanceof Error ? e.message : '검출기 시작 실패')
    } finally { setBusy(false) }
  }

  const refreshModels = () =>
    api.get<{ models: YoloModel[] }>('/vision/models')
      .then((r) => { setModels(r.models); return r.models })

  const handleUpload = async (f: File) => {
    setUploading(true)
    setError(null)
    try {
      // raw 바디 PUT — api 헬퍼는 JSON 전용이라 직접 fetch
      const res = await fetch(`/api/vision/models/${encodeURIComponent(f.name)}`, {
        method: 'PUT', body: f,
      })
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`
        try { detail = (await res.json()).detail ?? detail } catch { /* 비 JSON 에러 */ }
        throw new Error(detail)
      }
      await refreshModels()
      setModel(f.name) // 방금 올린 걸 바로 선택
    } catch (e) {
      setError(e instanceof Error ? e.message : '업로드 실패')
    } finally { setUploading(false) }
  }

  const handleDeleteModel = async () => {
    try {
      await api.delete(`/vision/models/${encodeURIComponent(model)}`)
      const list = await refreshModels()
      // ⚠ 예전에는 'yolo11n.pt' 를 박아 넣었다 — 그 파일이 이 기기에 없으면
      //   삭제한 뒤 선택이 **받아야 하는 모델**로 옮겨 앉는다. 있는 것 중에서 고른다.
      setModel(list?.find((m) => m.downloaded === true)?.file ?? 'yolo11n.pt')
    } catch (e) {
      setError(e instanceof Error ? e.message : '삭제 실패')
    }
  }

  const handleStop = async () => {
    setBusy(true)
    try { await api.post('/vision/stop') } catch (e) {
      setError(e instanceof Error ? e.message : '정지 실패')
    } finally { setBusy(false) }
  }

  const entries = Object.entries(detections)
  const totalObjects = entries.reduce((n, [, d]) => n + d.objects.length, 0)

  // 헤더 요약 — 카메라별 값(추론 ms, 검출 fps)의 평균
  const inferVals = entries.map(([, d]) => d.infer_ms).filter((v): v is number => v != null)
  const avgInferMs = inferVals.length
    ? inferVals.reduce((a, b) => a + b, 0) / inferVals.length : null
  const fpsVals = entries.map(([name]) => fpsRef.current[name]?.fps ?? 0).filter((v) => v > 0)
  const avgFps = fpsVals.length ? fpsVals.reduce((a, b) => a + b, 0) / fpsVals.length : null

  const metaParts = meta && [
    meta.model,
    meta.device,
    `conf ${meta.conf}`,
    `${meta.fps} fps`,
    meta.imgsz != null && `입력 ${meta.imgsz}px`,
    meta.task,
    meta.classes != null && `${meta.classes} 클래스`,
    meta.params != null && `${(meta.params / 1e6).toFixed(1)}M 파라미터`,
    meta.gflops != null && `${meta.gflops} GFLOPs`,
  ].filter(Boolean) as string[]

  return (
    <div className="space-y-4">
      {/* ── 헤더 ── */}
      <header className="flex items-center gap-4 flex-wrap">
        <h1 className="text-xl font-bold tracking-tight">검출 데모</h1>
        <span className={`text-xs px-2 py-0.5 rounded-full ${
          running ? 'bg-green-900/70 text-green-300' : 'bg-neutral-800 text-neutral-500'}`}>
          {status?.state ?? '…'}
        </span>
        {running && (
          <span className="text-sm text-neutral-400">
            객체 <b className="text-2xl text-white tabular-nums align-middle">{totalObjects}</b>
          </span>
        )}
        {running && avgInferMs != null && (
          <span className="text-sm text-neutral-400">
            추론 <b className="text-lg text-white tabular-nums align-middle">{avgInferMs.toFixed(1)}</b> ms
          </span>
        )}
        {running && avgFps != null && (
          <span className="text-sm text-neutral-400">
            <b className="text-lg text-white tabular-nums align-middle">{avgFps.toFixed(1)}</b> fps
          </span>
        )}
        {/* 모델 세부 정보 — yolod 자기소개. 돌고 있는데 아직 없으면 로드 중 */}
        {running && (metaParts ? (
          <span className="text-xs text-neutral-500 font-mono">{metaParts.join(' · ')}</span>
        ) : (
          <span className="text-xs text-neutral-600 animate-pulse">모델 로드 중…</span>
        ))}
        <div className="ml-auto flex items-center gap-3">
          {error && <span className="text-xs text-red-400">{error}</span>}
          {running && capDatasets.length > 0 && (
            <label className="text-xs text-neutral-500">캡처 →
              <select value={capTarget} onChange={(e) => setCapTarget(e.target.value)}
                className="ml-1 rounded bg-neutral-900 border border-neutral-700 px-1.5 py-0.5 text-neutral-300">
                {capDatasets.map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
            </label>
          )}
          {running && (
            <button onClick={() => void handleStop()} disabled={busy}
              className="px-4 py-1.5 text-sm rounded bg-neutral-800 hover:bg-red-700 text-neutral-300 hover:text-white disabled:opacity-50">
              정지
            </button>
          )}
        </div>
      </header>

      {/* ── 본문 ── */}
      {!running && entries.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-4 py-24 text-neutral-500">
          <div className="text-5xl">👁</div>
          <div className="text-sm">검출기가 꺼져 있습니다 — 구독할 세그먼트를 고르고 시작하세요</div>
          {segments.length === 0 ? (
            <div className="text-xs text-neutral-600">살아 있는 카메라 세그먼트가 없습니다 (카메라 페이지에서 연결)</div>
          ) : (
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3 max-w-3xl">
              {segments.map((s) => {
                const on = picked.has(s)
                return (
                  <button key={s} type="button"
                    onClick={() => {
                      const next = new Set(picked)
                      if (on) next.delete(s); else next.add(s)
                      setPicked(next)
                    }}
                    className={`relative rounded-lg overflow-hidden border text-left transition-opacity ${
                      on ? 'border-green-500 ring-1 ring-green-500'
                        : 'border-neutral-700 opacity-60 hover:opacity-100'}`}>
                    {/* 스냅샷은 1초마다 갱신 (imgTick 200ms 의 1/5) */}
                    <img
                      src={`/api/vision/segments/${encodeURIComponent(s)}/snapshot?t=${Math.floor(imgTick / 5)}`}
                      alt={s}
                      className="w-full aspect-video object-cover bg-black"
                      onError={(e) => { e.currentTarget.style.opacity = '0.25' }}
                      onLoad={(e) => { e.currentTarget.style.opacity = '1' }}
                    />
                    <div className="px-2 py-1 text-xs font-mono text-neutral-300 bg-neutral-900 truncate">
                      {shortId(s)}
                    </div>
                    {on && (
                      <span className="absolute top-1.5 right-1.5 w-5 h-5 flex items-center justify-center rounded-full bg-green-600 text-white text-xs">
                        ✓
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          )}
          <div className="flex items-center gap-5 flex-wrap justify-center">
          <label className="flex items-center gap-2 text-sm text-neutral-400">
            모델
            <select value={model} onChange={(e) => setModel(e.target.value)}
              className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-neutral-200">
              {[...new Set(models.map((m) => m.family))].map((fam) => (
                <optgroup key={fam} label={fam}>
                  {models.filter((m) => m.family === fam).map((m) => (
                    <option key={m.file} value={m.file}>
                      {m.file} — {m.label}
                      {m.params_m != null ? ` · ${m.params_m}M` : ''}
                      {m.trained_on ? ` · ${m.trained_on}` : ''}
                      {m.classes_n != null ? ` · ${m.classes_n}클래스` : ''}
                      {m.map50 != null ? ` · mAP50 ${m.map50}` : ''}
                      {m.downloaded === false
                        ? ` (다운로드 필요${m.size_mb != null ? `, ${m.size_mb}MB` : ''})` : ''}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-neutral-400">
            FPS
            <select value={fps} onChange={(e) => setFps(Number(e.target.value))}
              className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-neutral-200">
              {[1, 2, 5, 10, 15, 20, 30].map((v) => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-neutral-400">
            해상도
            <select value={imgsz} onChange={(e) => setImgsz(Number(e.target.value))}
              className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-neutral-200">
              {[320, 480, 640, 960, 1280].map((v) => (
                <option key={v} value={v}>{v}px</option>
              ))}
            </select>
          </label>
          <input ref={fileRef} type="file" accept=".pt" className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0]
              e.target.value = '' // 같은 파일 재선택도 change 가 뜨게
              if (f) void handleUpload(f)
            }} />
          <button type="button" onClick={() => fileRef.current?.click()} disabled={uploading}
            className="px-3 py-1 text-sm rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-300 hover:text-white disabled:opacity-50">
            {uploading ? '업로드 중…' : '＋ 가중치 업로드 (.pt)'}
          </button>
          {models.find((m) => m.file === model)?.family === '커스텀' && (
            <button type="button" onClick={() => void handleDeleteModel()}
              className="text-sm text-neutral-500 hover:text-red-400">
              삭제
            </button>
          )}
          </div>
          {models.find((m) => m.file === model)?.downloaded === false && (
            <div className="text-xs text-neutral-600">
              이 모델은 처음이라 시작 시 자동 다운로드된다 — 로드가 그만큼 늦는다
            </div>
          )}
          <button onClick={() => void handleStart()} disabled={busy || picked.size === 0}
            className="mt-2 px-10 py-2.5 text-base font-semibold rounded-lg bg-green-700 hover:bg-green-600 text-white disabled:opacity-40">
            {busy ? '시작 중…' : picked.size === 0 ? '카메라를 선택하세요' : `▶ 시작 (${picked.size}대)`}
          </button>
        </div>
      ) : (
        <main className={`grid gap-4 content-start ${
          entries.length <= 1 ? 'grid-cols-1 max-w-5xl mx-auto w-full' : 'grid-cols-1 lg:grid-cols-2'}`}>
          {entries.map(([name, d]) => {
            const fps = fpsRef.current[name]?.fps ?? 0
            return (
              <div key={name} className="relative rounded-xl overflow-hidden border border-neutral-800 bg-black">
                <img
                  src={`/api/vision/preview/${name}?t=${imgTick}`}
                  alt={name}
                  className="w-full block"
                  onError={(e) => { e.currentTarget.style.opacity = '0.25' }}
                  onLoad={(e) => { e.currentTarget.style.opacity = '1' }}
                />
                {/* 카메라 이름 + fps */}
                <div className="absolute top-2 left-2 flex items-center gap-2">
                  <span className="px-2 py-0.5 rounded bg-black/60 backdrop-blur text-sm font-semibold">{name}</span>
                  {fps > 0 && (
                    <span className="px-1.5 py-0.5 rounded bg-black/60 backdrop-blur text-xs text-neutral-400 tabular-nums">
                      {fps.toFixed(1)} fps
                    </span>
                  )}
                  {d.infer_ms != null && (
                    <span className="px-1.5 py-0.5 rounded bg-black/60 backdrop-blur text-xs text-neutral-400 tabular-nums">
                      추론 {d.infer_ms.toFixed(1)}ms
                    </span>
                  )}
                </div>
                {/* 이 장면을 학습 데이터셋으로 — 오검출을 본 그 순간이 하드 케이스다 */}
                {capTarget && meta?.cams?.[name] && (
                  <div className="absolute top-2 right-2 flex items-center gap-1.5">
                    {capCount[name] != null && (
                      <span className="px-1.5 py-0.5 rounded bg-black/60 text-xs text-green-400">+{capCount[name]}</span>
                    )}
                    <button onClick={() => void captureScene(name)}
                      title={`원본 프레임을 ${capTarget} 데이터셋으로 캡처`}
                      className="px-2 py-0.5 rounded bg-black/60 backdrop-blur text-sm text-neutral-300 hover:bg-green-700 hover:text-white">
                      📸
                    </button>
                  </div>
                )}
                {/* 검출 라벨 칩 */}
                <div className="absolute bottom-2 left-2 right-2 flex flex-wrap gap-1.5">
                  {d.objects.map((o, i) => (
                    <span key={i}
                      className="px-2 py-0.5 rounded-full bg-black/70 backdrop-blur text-sm border border-neutral-700">
                      {o.label} <span className="text-neutral-500 text-xs tabular-nums">{(o.conf * 100).toFixed(0)}%</span>
                    </span>
                  ))}
                  {d.objects.length === 0 && (
                    <span className="px-2 py-0.5 rounded-full bg-black/50 text-xs text-neutral-500">검출 없음</span>
                  )}
                </div>
              </div>
            )
          })}
          {running && entries.length === 0 && (
            <div className="text-sm text-neutral-500 p-8 text-center">검출 대기 중… (모델 로드에 수 초)</div>
          )}
        </main>
      )}
    </div>
  )
}
