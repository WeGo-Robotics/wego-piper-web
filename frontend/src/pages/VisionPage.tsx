/**
 * 비전·판단 테스트 — 분리수거 파이프라인(YOLO→LLM→VLA)의 앞 두 칸을 화면에서 굴린다.
 *
 * 위: yolod 제어(세그먼트 구독·시작/정지) + 카메라별 어노테이트 프리뷰·검출 목록.
 * 아래: LLM 판단 — 검출 텍스트(버스의 `text` 그대로)를 규칙과 함께 judge 로 보내
 * 슬롯(target/destination/reason)을 받는다. 프로바이더·모델은 호출별 선택.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'
import { useSystemMessage } from '../components/SystemMessages'

type DetObject = { label: string; conf: number; bbox: number[]; center: number[] }
type DetPayload = {
  ts: number
  cam: string
  frame_seq: number
  size: number[]
  objects: DetObject[]
  text: string
}
type YoloStatus = { state: string; pid: number | null; cams: string[] }
type JudgeResult = {
  slots: { target: string; destination: string; reason: string }
  ms: number
  provider: string
  model: string
}

type OrchHistoryItem = {
  episode: number
  slots?: { target: string; destination: string } | null
  task?: string | null
  outcome?: string | null
  judge_error?: string | null
}
type OrchStatus = {
  state: string
  episode: number
  max_episodes: number
  step: string
  history: OrchHistoryItem[]
}

type CamRow = { alias: string; camId: string }

/** rs_335122271186_color → rs_…1186_color (선택 UI 가독용) */
const shortId = (s: string) => (s.length > 24 ? `${s.slice(0, 6)}…${s.slice(-10)}` : s)

export default function VisionPage() {
  const { notify } = useSystemMessage()
  const notifyError = (text: string) => notify({ level: 'error', text, source: '비전·판단' })

  // 패널 배치 — col: 세로 스택(패널이 넓다), row: 가로 3열(한눈에 나란히)
  const [layout, setLayout] = useState<'col' | 'row'>(
    () => (localStorage.getItem('vision-layout') === 'row' ? 'row' : 'col'))
  const switchLayout = (l: 'col' | 'row') => {
    setLayout(l)
    localStorage.setItem('vision-layout', l)
  }

  // ── yolod ──
  const [segments, setSegments] = useState<string[]>([])
  const [status, setStatus] = useState<YoloStatus | null>(null)
  const [detections, setDetections] = useState<Record<string, DetPayload>>({})
  const [camRows, setCamRows] = useState<CamRow[]>([])
  const [model, setModel] = useState('yolo11n.pt')
  const [conf, setConf] = useState(0.25)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0) // 프리뷰 이미지 캐시 버스터

  const running = status?.state === 'running' || status?.state === 'starting'

  const [orch, setOrch] = useState<OrchStatus | null>(null)

  const poll = useCallback(async () => {
    try {
      setStatus(await api.get<YoloStatus>('/vision/status'))
      setDetections(await api.get<Record<string, DetPayload>>('/vision/detections'))
      setOrch(await api.get<OrchStatus>('/orchestrator/status'))
      setTick((t) => t + 1)
    } catch {
      /* 게이트웨이 순단은 다음 폴에 회복 */
    }
  }, [])

  useEffect(() => {
    api.get<{ segments: string[] }>('/vision/segments').then((r) => setSegments(r.segments)).catch(() => {})
    void poll()
    const timer = setInterval(() => void poll(), 1000)
    return () => clearInterval(timer)
  }, [poll])

  const addCam = (camId: string) => {
    if (!camId || camRows.some((r) => r.camId === camId)) return
    // rs_..._color 가 여럿이면 top/hand 순으로 별칭 제안
    const suggested = ['top', 'hand', 'side', 'extra'][camRows.length] ?? `cam${camRows.length}`
    setCamRows([...camRows, { alias: suggested, camId }])
  }

  const handleStart = async () => {
    if (camRows.length === 0) { notifyError('구독할 카메라를 추가하세요'); return }
    setBusy(true)
    try {
      const cams = Object.fromEntries(camRows.map((r) => [r.alias.trim() || r.camId, r.camId]))
      await api.post('/vision/start', { cams, model, conf })
      notify({ level: 'info', text: 'yolod 시작 (모델 로드에 수 초)', source: '비전·판단' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'yolod 시작 실패')
    } finally { setBusy(false) }
  }

  const handleStop = async () => {
    setBusy(true)
    try { await api.post('/vision/stop') } catch (e) {
      notifyError(e instanceof Error ? e.message : '정지 실패')
    } finally { setBusy(false) }
  }

  // ── LLM 판단 ──
  const [rules, setRules] = useState('')
  const [showRules, setShowRules] = useState(false)
  const [input, setInput] = useState('')
  const [provider, setProvider] = useState('')
  const [llmModel, setLlmModel] = useState('')
  const [judging, setJudging] = useState(false)
  const [result, setResult] = useState<JudgeResult | null>(null)
  const [judgeError, setJudgeError] = useState<string | null>(null)

  useEffect(() => {
    api.get<{ rules: string; provider: string; model: string }>('/vision/judge/defaults')
      .then((r) => { setRules(r.rules); setProvider(r.provider); setLlmModel(r.model) })
      .catch(() => {})
  }, [])

  const fillFromDetections = () => {
    const lines = Object.values(detections).map((d) => d.text)
    if (lines.length === 0) { notifyError('살아 있는 검출이 없습니다 — yolod 를 켜세요'); return }
    setInput(lines.join('\n'))
  }

  // ── 분리수거 루프 ──
  const [maxEpisodes, setMaxEpisodes] = useState(10)
  const [waitS, setWaitS] = useState(40)
  const [dryRun, setDryRun] = useState(true)
  const [orchBusy, setOrchBusy] = useState(false)
  const orchRunning = orch?.state === 'running' || orch?.state === 'stopping'

  const handleOrchStart = async () => {
    setOrchBusy(true)
    try {
      await api.post('/orchestrator/start', {
        max_episodes: maxEpisodes, wait_s: waitS, dry_run: dryRun,
        rules, provider, model: llmModel,
      })
      notify({ level: 'info', text: `분리수거 루프 시작 (${maxEpisodes}회, ${dryRun ? '드라이런' : '실기'})`, source: '비전·판단' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '루프 시작 실패')
    } finally { setOrchBusy(false) }
  }

  const handleOrchStop = async () => {
    setOrchBusy(true)
    try { await api.post('/orchestrator/stop') } catch (e) {
      notifyError(e instanceof Error ? e.message : '정지 실패')
    } finally { setOrchBusy(false) }
  }

  const handleJudge = async () => {
    if (!input.trim()) { notifyError('입력(검출 텍스트)이 비어 있습니다'); return }
    setJudging(true)
    setResult(null)
    setJudgeError(null)
    try {
      setResult(await api.post<JudgeResult>('/vision/judge', {
        user: input, system: rules, provider, model: llmModel,
      }))
    } catch (e) {
      setJudgeError(e instanceof Error ? e.message : '판단 실패')
    } finally { setJudging(false) }
  }

  return (
    <div className="space-y-3">
      {/* 배치 토글 — 값은 localStorage 에 남는다 */}
      <div className="flex justify-end">
        <div className="flex rounded overflow-hidden border border-neutral-700 text-xs">
          {([['col', '세로'], ['row', '가로']] as const).map(([l, label]) => (
            <button key={l} onClick={() => switchLayout(l)}
              className={`px-2.5 py-1 ${layout === l
                ? 'bg-neutral-600 text-white' : 'bg-neutral-800 text-neutral-500 hover:text-neutral-300'}`}>
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className={layout === 'row' ? 'grid grid-cols-1 xl:grid-cols-3 gap-4 items-start' : 'space-y-4'}>
      {/* ── YOLO ── */}
      <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold">YOLO 검출</h2>
          <span className={`text-xs px-2 py-0.5 rounded ${running ? 'bg-green-900 text-green-300' : 'bg-neutral-700 text-neutral-400'}`}>
            {status?.state ?? '…'}{status?.pid ? ` · pid ${status.pid}` : ''}
          </span>
          {running ? (
            <button onClick={() => void handleStop()} disabled={busy}
              className="ml-auto px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white disabled:opacity-50">
              정지
            </button>
          ) : (
            <button onClick={() => void handleStart()} disabled={busy}
              className="ml-auto px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-green-600 text-neutral-300 hover:text-white disabled:opacity-50">
              시작
            </button>
          )}
        </div>

        {!running && (
          <div className="space-y-2 text-sm">
            <div className="flex items-center gap-2 flex-wrap">
              <select
                value=""
                onChange={(e) => addCam(e.target.value)}
                className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1"
              >
                <option value="">카메라 세그먼트 추가…</option>
                {segments.filter((s) => !camRows.some((r) => r.camId === s)).map((s) => (
                  <option key={s} value={s}>{shortId(s)}</option>
                ))}
              </select>
              <label className="text-neutral-400">모델
                <input value={model} onChange={(e) => setModel(e.target.value)}
                  className="ml-1 w-28 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
              </label>
              <label className="text-neutral-400">conf
                <input type="number" step="0.05" min="0" max="1" value={conf}
                  onChange={(e) => setConf(Number(e.target.value))}
                  className="ml-1 w-20 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
              </label>
            </div>
            {camRows.map((r, i) => (
              <div key={r.camId} className="flex items-center gap-2">
                <input value={r.alias}
                  onChange={(e) => setCamRows(camRows.map((x, j) => (j === i ? { ...x, alias: e.target.value } : x)))}
                  className="w-24 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
                <span className="text-neutral-500 font-mono text-xs">{r.camId}</span>
                <button onClick={() => setCamRows(camRows.filter((_, j) => j !== i))}
                  className="text-neutral-500 hover:text-red-400">✕</button>
              </div>
            ))}
          </div>
        )}

        {/* 검출 라이브 뷰 — 세로 배치(넓은 패널)에서는 목록을 사진 옆에,
            높이를 사진과 같게 고정해 검출 수가 변해도 화면이 안 출렁인다 */}
        <div className={layout === 'col' ? 'space-y-3' : 'flex gap-4 flex-wrap'}>
          {Object.entries(detections).map(([name, d]) => {
            const table = d.objects.length > 0 && (
              <table className="w-full text-xs text-neutral-300">
                <tbody>
                  {d.objects.map((o, i) => (
                    <tr key={i} className="border-t border-neutral-700/50">
                      <td className="py-0.5">{o.label}</td>
                      <td className="text-neutral-500">{o.conf.toFixed(2)}</td>
                      <td className="text-neutral-500 font-mono">({o.center[0]}, {o.center[1]})</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
            const img = (
              <img
                src={`/api/vision/preview/${name}?t=${tick}`}
                alt={name}
                className="w-full rounded border border-neutral-700 bg-black"
                onError={(e) => { e.currentTarget.style.opacity = '0.3' }}
                onLoad={(e) => { e.currentTarget.style.opacity = '1' }}
              />
            )
            if (layout === 'col') {
              return (
                <div key={name} className="flex gap-3 max-w-4xl">
                  <div className="w-[380px] max-w-[55%] shrink-0 self-start">{img}</div>
                  {/* 사진이 행 높이를 정한다 — 목록은 absolute 로 그 높이에 갇혀 스크롤 */}
                  <div className="relative flex-1 min-w-0 self-stretch">
                    <div className="absolute inset-0 overflow-y-auto rounded border border-neutral-700/60 bg-neutral-900/40 p-2 space-y-1">
                      <div className="text-xs text-neutral-400 font-mono">{d.text}</div>
                      {table}
                    </div>
                  </div>
                </div>
              )
            }
            return (
              <div key={name} className="w-[380px] max-w-full space-y-1">
                {img}
                <div className="text-xs text-neutral-400 font-mono">{d.text}</div>
                {table}
              </div>
            )
          })}
          {running && Object.keys(detections).length === 0 && (
            <div className="text-sm text-neutral-500">검출 대기 중… (모델 로드 수 초)</div>
          )}
        </div>
      </section>

      {/* ── LLM 판단 ── */}
      <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">LLM 판단</h2>
          <select value={provider} onChange={(e) => setProvider(e.target.value)}
            className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-sm">
            <option value="openai_compat">로컬 (Ollama/vLLM)</option>
            <option value="anthropic">Claude API</option>
          </select>
          <input value={llmModel} onChange={(e) => setLlmModel(e.target.value)}
            placeholder="모델"
            className="w-40 rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-sm" />
          <button onClick={() => setShowRules((s) => !s)}
            className="text-sm text-neutral-400 hover:text-white">
            규칙 {showRules ? '▴' : '▾'}
          </button>
        </div>

        {showRules && (
          <textarea value={rules} onChange={(e) => setRules(e.target.value)} rows={8}
            className="w-full rounded bg-neutral-900 border border-neutral-700 p-2 text-sm font-mono" />
        )}

        <div className="flex gap-2">
          <textarea value={input} onChange={(e) => setInput(e.target.value)} rows={3}
            placeholder="검출 텍스트 — [현재 검출 채우기] 또는 직접 입력"
            className="flex-1 rounded bg-neutral-900 border border-neutral-700 p-2 text-sm font-mono" />
          <div className="flex flex-col gap-2">
            <button onClick={fillFromDetections}
              className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 hover:text-white whitespace-nowrap">
              현재 검출 채우기
            </button>
            <button onClick={() => void handleJudge()} disabled={judging}
              className="px-3 py-1 text-sm rounded bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-50 whitespace-nowrap">
              {judging ? '판단 중…' : '판단 실행'}
            </button>
          </div>
        </div>

        {result && (
          <div className="rounded border border-green-800 bg-green-950/40 p-3 text-sm space-y-1">
            <div className="flex gap-4 items-center flex-wrap">
              <span>🎯 <b>{result.slots.target}</b> → 🗑 <b>{result.slots.destination}</b></span>
              <span className="text-neutral-400 text-xs">
                {result.ms}ms · {result.provider} · {result.model}
              </span>
            </div>
            <div className="text-neutral-300">{result.slots.reason}</div>
          </div>
        )}
        {judgeError && (
          <div className="rounded border border-red-800 bg-red-950/40 p-3 text-sm text-red-300">
            {judgeError}
          </div>
        )}
      </section>

      {/* ── 분리수거 루프 (오케스트레이터 1단계) ── */}
      <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">분리수거 루프</h2>
          <span className={`text-xs px-2 py-0.5 rounded ${orchRunning ? 'bg-green-900 text-green-300' : 'bg-neutral-700 text-neutral-400'}`}>
            {orch?.state ?? '…'}
            {orchRunning && ` · ${orch?.episode}/${orch?.max_episodes} · ${orch?.step || '—'}`}
          </span>
          {orchRunning ? (
            <button onClick={() => void handleOrchStop()} disabled={orchBusy}
              className="ml-auto px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white disabled:opacity-50">
              정지
            </button>
          ) : (
            <button onClick={() => void handleOrchStart()} disabled={orchBusy}
              className="ml-auto px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-green-600 text-neutral-300 hover:text-white disabled:opacity-50">
              시작
            </button>
          )}
        </div>

        {!orchRunning && (
          <div className="flex items-center gap-4 text-sm flex-wrap">
            <label className="text-neutral-400">회차
              <input type="number" min={1} max={200} value={maxEpisodes}
                onChange={(e) => setMaxEpisodes(Number(e.target.value))}
                className="ml-1 w-16 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
            </label>
            <label className="text-neutral-400">실행 대기(s)
              <input type="number" min={1} value={waitS}
                onChange={(e) => setWaitS(Number(e.target.value))}
                className="ml-1 w-16 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
            </label>
            <label className="text-neutral-400 flex items-center gap-1">
              <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
              드라이런 (task/reset 미전송 — 로봇 없이 루프 확인)
            </label>
            <span className="text-xs text-neutral-600">
              검출→판단→task→대기→리셋. 판단은 위 LLM 설정(프로바이더·모델·규칙)을 쓴다
            </span>
          </div>
        )}

        {orch && orch.history.length > 0 && (
          <ul className="text-sm divide-y divide-neutral-700/50">
            {[...orch.history].reverse().slice(0, 8).map((h) => (
              <li key={h.episode} className="py-1 flex items-center gap-3 flex-wrap">
                <span className="font-mono text-neutral-500">#{h.episode}</span>
                {h.slots ? (
                  <span>🎯 {h.slots.target} → 🗑 {h.slots.destination}</span>
                ) : (
                  <span className="text-neutral-500">판단 없음</span>
                )}
                <span className={`text-xs px-1.5 rounded ${
                  h.outcome === 'timeout_done' ? 'bg-green-900 text-green-300'
                  : h.outcome === 'judge_failed' ? 'bg-red-900 text-red-300'
                  : 'bg-neutral-700 text-neutral-400'}`}>
                  {h.outcome}
                </span>
                {h.task && <span className="text-xs text-neutral-500 font-mono truncate">{h.task}</span>}
                {h.judge_error && <span className="text-xs text-red-400">{h.judge_error}</span>}
              </li>
            ))}
          </ul>
        )}
      </section>
      </div>
    </div>
  )
}
