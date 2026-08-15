/**
 * 에피소드 뷰어 — 재생·신호·페이즈를 한 화면에서 본다 (feature/episode-editor.md 2단계).
 *
 * 이 단계는 **읽기 전용**이다: 프레임 재생 + 신호 그래프 + 페이즈 트랙 + ⚠ 정렬.
 * 구간 편집(드래그/분할/병합)은 3단계, 삭제·task 이관은 4단계.
 *
 * 페이즈 사이드카는 선택적 입력이다 — 없으면 트랙 자리에 [분석] 버튼만 보이고
 * 재생은 그대로 동작한다. 분류기(`python -m piper_phase`)가 만든 사이드카도
 * 같은 파일이라 똑같이 보인다.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../services/api'
import { useSystemMessage } from '../components/SystemMessages'
import PlotlyChart from '../components/PlotlyChart'
import type { Dataset, DatasetDetail } from '../types/models'

// ── 페이즈 사이드카 형태 (backend/app/routers/phase.py 응답) ──

type PhaseEpisode = {
  segments: [number, number, number][] // [start, end(포함), code]
  cycles: number
  frames: number
  reviewed: boolean
  note: string
}
type PhaseLabels = {
  phases: string[]
  params: Record<string, number>
  episodes: Record<string, PhaseEpisode>
}
type PhaseOutlier = { episode: number; cycles: number; reasons: string[] }
type PhaseSummary = {
  episodes: number
  cycle_distribution: Record<string, number>
  median_cycles: number
  outliers: PhaseOutlier[]
}
type Signals = { frames: number; speed: number[]; gripper_gap: number[]; phase: number[] }

/** 페이즈 코드(0~6) 색 — 트랙·칩·범례가 같은 배열을 쓴다. */
const PHASE_COLORS = ['#525252', '#3b82f6', '#22d3ee', '#f59e0b', '#22c55e', '#a855f7', '#404040']

const PREFETCH_AHEAD = 12

type EpisodeRow = {
  index: number
  length: number
  cycles: number | null
  reviewed: boolean
  reasons: string[] // 비어 있으면 정상
}

export default function EpisodesPage() {
  const { notify } = useSystemMessage()
  const notifyError = (text: string) => notify({ level: 'error', text, source: '에피소드' })

  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [dsId, setDsId] = useState<string>('')
  const [detail, setDetail] = useState<DatasetDetail | null>(null)
  const [labels, setLabels] = useState<PhaseLabels | null>(null)
  const [summary, setSummary] = useState<PhaseSummary | null>(null)
  const [ep, setEp] = useState<number | null>(null)
  const [signals, setSignals] = useState<Signals | null>(null)
  const [frame, setFrame] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [cacheMissing, setCacheMissing] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)

  useEffect(() => {
    api.get<Dataset[]>('/datasets').then(setDatasets).catch(() => {})
  }, [])

  const loadPhase = useCallback(async (id: string) => {
    // 사이드카는 선택적 — 404 는 "아직 분석 안 됨"이지 오류가 아니다
    setLabels(await api.get<PhaseLabels>(`/phase/${id}/labels`).catch(() => null))
    setSummary(await api.get<PhaseSummary>(`/phase/${id}/summary`).catch(() => null))
  }, [])

  const selectDataset = useCallback(async (id: string) => {
    setDsId(id)
    setEp(null)
    setSignals(null)
    setPlaying(false)
    setCacheMissing(false)
    if (!id) { setDetail(null); setLabels(null); setSummary(null); return }
    try {
      setDetail(await api.get<DatasetDetail>(`/datasets/${id}`))
    } catch { notifyError(`데이터셋을 열 수 없습니다: ${id}`); return }
    await loadPhase(id)
  }, [loadPhase]) // eslint-disable-line react-hooks/exhaustive-deps

  const selectEpisode = useCallback(async (id: string, index: number) => {
    setEp(index)
    setFrame(0)
    setPlaying(false)
    setCacheMissing(false)
    setSignals(await api.get<Signals>(`/phase/${id}/signals/${index}`).catch(() => null))
  }, [])

  // ── 파생 상태 ──

  const cams = useMemo(
    () => Object.keys(detail?.features ?? {})
      .filter((k) => k.startsWith('observation.images.'))
      .map((k) => k.slice('observation.images.'.length)),
    [detail],
  )

  const episodes = useMemo<EpisodeRow[]>(() => {
    if (!detail) return []
    const flagged = new Map((summary?.outliers ?? []).map((o) => [o.episode, o.reasons]))
    const rows = detail.episodes.map((e, i) => {
      const index = (e.episode_index as number) ?? i
      const labeled = labels?.episodes[String(index)]
      return {
        index,
        length: (e.length as number) ?? labeled?.frames ?? 0,
        cycles: labeled?.cycles ?? null,
        reviewed: labeled?.reviewed ?? false,
        reasons: flagged.get(index) ?? [],
      }
    })
    // ⚠ 먼저 (사유 많은 순) — 50개를 다 볼 필요 없이 이상한 것부터 본다
    return rows.sort((a, b) => b.reasons.length - a.reasons.length || a.index - b.index)
  }, [detail, labels, summary])

  const current = ep != null ? episodes.find((r) => r.index === ep) : undefined
  const labeledEp = ep != null ? labels?.episodes[String(ep)] : undefined
  const totalFrames = labeledEp?.frames ?? current?.length ?? 0
  const fps = detail?.fps ?? 15

  const currentSegment = useMemo(
    () => labeledEp?.segments.find(([s, e]) => s <= frame && frame <= e),
    [labeledEp, frame],
  )

  const frameUrl = useCallback(
    (cam: string, f: number) => `/api/datasets/${dsId}/episodes/${ep}/frames/${cam}/${f}`,
    [dsId, ep],
  )

  // ── 재생 (rAF, fps 기준) ──

  const frameRef = useRef(frame)
  frameRef.current = frame

  useEffect(() => {
    if (!playing || totalFrames === 0) return
    let raf = 0
    let last = performance.now()
    const stepMs = 1000 / fps
    const tick = (now: number) => {
      const steps = Math.floor((now - last) / stepMs)
      if (steps > 0) {
        last += steps * stepMs
        const next = Math.min(frameRef.current + steps, totalFrames - 1)
        setFrame(next)
        if (next >= totalFrames - 1) { setPlaying(false); return }
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing, fps, totalFrames])

  // 앞 프레임 프리페치 — 서버가 immutable 캐시 헤더를 주므로 재생이 끊기지 않는다
  useEffect(() => {
    if (ep == null || !dsId) return
    for (const cam of cams) {
      for (let i = 1; i <= PREFETCH_AHEAD; i++) {
        if (frame + i >= totalFrames) break
        new Image().src = frameUrl(cam, frame + i)
      }
    }
  }, [frame, ep, dsId, cams, totalFrames, frameUrl])

  // ── 단축키 (01-phase-annotation §4.3 의 뷰어 부분집합) ──

  const stepFrame = useCallback(
    (d: number) => setFrame((f) => Math.max(0, Math.min(totalFrames - 1, f + d))),
    [totalFrames],
  )
  const moveEpisode = useCallback((d: number) => {
    if (ep == null || episodes.length === 0) return
    const pos = episodes.findIndex((r) => r.index === ep)
    const next = episodes[pos + d]
    if (next) void selectEpisode(dsId, next.index)
  }, [ep, episodes, dsId, selectEpisode])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT') return
      if (t.tagName === 'INPUT' && (t as HTMLInputElement).type === 'text') return
      if (e.code === 'Space') { e.preventDefault(); if (totalFrames > 0) setPlaying((p) => !p) }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); stepFrame(e.shiftKey ? -10 : -1) }
      else if (e.key === 'ArrowRight') { e.preventDefault(); stepFrame(e.shiftKey ? 10 : 1) }
      else if (e.key === 'j' || e.key === 'J') moveEpisode(-1)
      else if (e.key === 'k' || e.key === 'K') moveEpisode(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [stepFrame, moveEpisode, totalFrames])

  // ── 동작 ──

  const handleAnalyze = async () => {
    if (!dsId) return
    setAnalyzing(true)
    try {
      await api.post(`/phase/${dsId}/analyze`, {})
      await loadPhase(dsId)
      if (ep != null) setSignals(await api.get<Signals>(`/phase/${dsId}/signals/${ep}`).catch(() => null))
      notify({ level: 'info', text: '페이즈 분석 완료', source: '에피소드' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '분석 실패')
    } finally { setAnalyzing(false) }
  }

  const handleCreateCache = async () => {
    try {
      await api.post(`/datasets/${dsId}/decode-cache`, { format: 'jpeg', max_dim: 320 })
      notify({ level: 'info', text: '디코딩 캐시 생성 시작 — 완료 후 에피소드를 다시 선택하세요', source: '에피소드' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '캐시 생성 실패')
    }
  }

  // ── 렌더 ──

  const distText = summary
    ? Object.entries(summary.cycle_distribution).map(([c, n]) => `${c}사이클×${n}`).join(' ')
    : null

  return (
    <div className="flex gap-4 items-start">
      {/* 에피소드 리스트 */}
      <aside className="w-72 shrink-0 space-y-3">
        <select
          value={dsId}
          onChange={(e) => void selectDataset(e.target.value)}
          className="w-full rounded bg-neutral-800 border border-neutral-700 px-2 py-1.5 text-sm"
        >
          <option value="">데이터셋 선택…</option>
          {datasets.map((d) => (
            <option key={d.id} value={d.id}>{d.id} ({d.total_episodes})</option>
          ))}
        </select>

        {detail && (
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 text-xs space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-neutral-400">
                {distText ?? '페이즈 분석 안 됨'}
                {summary && ` (중앙값 ${summary.median_cycles})`}
              </span>
              <button
                onClick={() => void handleAnalyze()}
                disabled={analyzing}
                className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white disabled:opacity-50"
              >
                {analyzing ? '분석 중…' : labels ? '재분석' : '분석'}
              </button>
            </div>
            {summary && summary.outliers.length > 0 && (
              <div className="text-amber-400">⚠ 이상 에피소드 {summary.outliers.length}개 — 위로 정렬됨</div>
            )}
          </div>
        )}

        {episodes.length > 0 && (
          <ul className="rounded-lg border border-neutral-700 bg-neutral-800 divide-y divide-neutral-700/60 max-h-[70vh] overflow-y-auto">
            {episodes.map((r) => (
              <li key={r.index}>
                <button
                  onClick={() => void selectEpisode(dsId, r.index)}
                  className={`w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 hover:bg-neutral-700/50 ${
                    ep === r.index ? 'bg-neutral-700' : ''
                  }`}
                >
                  <span className="font-mono">#{r.index}</span>
                  <span className="text-xs text-neutral-500">{r.length}f</span>
                  {r.cycles != null && (
                    <span className="text-xs text-neutral-400">{r.cycles}사이클</span>
                  )}
                  <span className="ml-auto flex items-center gap-1">
                    {r.reasons.length > 0 && (
                      <span className="text-amber-400" title={r.reasons.join(', ')}>⚠</span>
                    )}
                    {r.reviewed && <span className="text-green-500">✔</span>}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      {/* 뷰어 */}
      <main className="flex-1 min-w-0 space-y-3">
        {ep == null ? (
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-8 text-center text-neutral-500 text-sm">
            {dsId ? '왼쪽에서 에피소드를 선택하세요' : '데이터셋을 선택하세요'}
            <div className="mt-2 text-xs">Space 재생 · ←→ 프레임 (Shift=10) · J/K 에피소드 이동</div>
          </div>
        ) : (
          <>
            {/* 카메라 프레임 */}
            <div className="flex gap-3 flex-wrap">
              {cams.map((cam) => (
                <figure key={cam} className="flex-1 min-w-[240px] max-w-[520px]">
                  <img
                    src={frameUrl(cam, frame)}
                    alt={cam}
                    className="w-full rounded border border-neutral-700 bg-black"
                    onError={() => setCacheMissing(true)}
                    onLoad={() => setCacheMissing(false)}
                  />
                  <figcaption className="text-xs text-neutral-500 mt-1">{cam}</figcaption>
                </figure>
              ))}
            </div>

            {cacheMissing && (
              <div className="rounded-lg border border-amber-700/60 bg-amber-950/40 p-3 text-sm flex items-center justify-between">
                <span>디코딩 캐시가 없습니다 — 생성해야 프레임이 보입니다 (JPEG·긴변 320px, 수 분 소요)</span>
                <button
                  onClick={() => void handleCreateCache()}
                  className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-amber-600 text-neutral-300 hover:text-white"
                >
                  캐시 생성
                </button>
              </div>
            )}

            {/* 컨트롤 */}
            <div className="flex items-center gap-3 text-sm">
              <button
                onClick={() => totalFrames > 0 && setPlaying((p) => !p)}
                className="px-3 py-1 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white"
              >
                {playing ? '⏸ 정지' : '▶ 재생'}
              </button>
              <span className="font-mono text-neutral-400">
                {frame + 1}/{totalFrames} · {(frame / fps).toFixed(1)}s
              </span>
              {currentSegment && labels && (
                <span
                  className="px-2 py-0.5 rounded text-xs font-medium text-black"
                  style={{ backgroundColor: PHASE_COLORS[currentSegment[2]] }}
                >
                  {labels.phases[currentSegment[2]]}
                </span>
              )}
              {labeledEp?.reviewed && <span className="text-green-500 text-xs">✔ 검토됨</span>}
            </div>

            <input
              type="range"
              min={0}
              max={Math.max(0, totalFrames - 1)}
              value={frame}
              onChange={(e) => { setPlaying(false); setFrame(Number(e.target.value)) }}
              className="w-full"
            />

            {/* 페이즈 트랙 — 구간 클릭 = 그 시작으로 이동 (편집은 3단계) */}
            {labeledEp ? (
              <div>
                <div className="relative h-6 rounded overflow-hidden flex border border-neutral-700">
                  {labeledEp.segments.map(([s, e, code], i) => (
                    <button
                      key={i}
                      title={`${labels?.phases[code]} ${s}–${e}`}
                      onClick={() => { setPlaying(false); setFrame(s) }}
                      style={{
                        width: `${((e - s + 1) / totalFrames) * 100}%`,
                        backgroundColor: PHASE_COLORS[code],
                      }}
                    />
                  ))}
                  <div
                    className="absolute top-0 bottom-0 w-px bg-yellow-400 pointer-events-none"
                    style={{ left: `${(frame / Math.max(1, totalFrames - 1)) * 100}%` }}
                  />
                </div>
                <div className="flex gap-3 mt-1 text-[10px] text-neutral-500 flex-wrap">
                  {labels?.phases.map((name, code) => (
                    <span key={name} className="flex items-center gap-1">
                      <span className="inline-block w-2 h-2 rounded-sm" style={{ backgroundColor: PHASE_COLORS[code] }} />
                      {name}
                    </span>
                  ))}
                </div>
              </div>
            ) : (
              <div className="rounded border border-neutral-700 bg-neutral-800 p-3 text-xs text-neutral-500">
                페이즈 분석이 없습니다 — [분석] 을 실행하면 구간 트랙과 신호 그래프가 보입니다
              </div>
            )}

            {/* 신호 그래프 — 재생헤드(markerX) 공유 */}
            {signals && (
              <div className="space-y-2">
                <PlotlyChart
                  x={Array.from({ length: signals.frames }, (_, i) => i)}
                  series={[{ label: '관절 속도 (deg/s)', color: '#60a5fa', data: signals.speed }]}
                  markerX={frame}
                  height={160}
                  uirevision={`${dsId}/${ep}/speed`}
                />
                <PlotlyChart
                  x={Array.from({ length: signals.frames }, (_, i) => i)}
                  series={[{ label: '그리퍼 지령-실측 갭', color: '#f472b6', data: signals.gripper_gap }]}
                  markerX={frame}
                  height={160}
                  uirevision={`${dsId}/${ep}/gap`}
                />
              </div>
            )}
          </>
        )}
      </main>
    </div>
  )
}
