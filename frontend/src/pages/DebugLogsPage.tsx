import { useEffect, useMemo, useRef, useState } from 'react'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'
import PlotlyChart, { type Series } from '../components/PlotlyChart'
import { twoColumns } from '../components/LayoutToggle'

type RunSummary = {
  id: string
  mode: string
  meta: Record<string, unknown>
  cameras: string[]
  counts: { observations: number; inference: number; control: number }
  size_kb: number
  modified: string
}
type ObsRec = { seq: number; t: number; task: string; state: Record<string, number>; images: Record<string, string> }
type InfRec = { seq: number; t: number; inference_ms: number | null; action_chunk: number[][] }
type ControlRec = { step: number; t: number; target: Record<string, number> | null }
type RecordsResp<T> = { records: T[]; total: number; truncated: boolean }
type SimResp = {
  motors: string[]
  t: number[]
  steps: number
  series: Record<string, { target: (number | null)[]; actual: (number | null)[]; filtered_recorded: (number | null)[]; filtered_sim: (number | null)[] }>
}
type FilterParams = {
  lowpass_alpha: number
  max_velocity: number
  max_gripper_velocity: number
  max_jerk: number
  interpolation_steps: number
  fps: number
}

const DEFAULT_PARAMS: FilterParams = {
  lowpass_alpha: 0.5, max_velocity: 180, max_gripper_velocity: 300, max_jerk: 0, interpolation_steps: 0, fps: 20,
}

export default function DebugLogsPage() {
  const { confirm: askConfirm } = useSystemMessage()
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<RunSummary | null>(null)

  const [obs, setObs] = useState<ObsRec[]>([])
  const [inf, setInf] = useState<InfRec[]>([])
  const [control, setControl] = useState<ControlRec[]>([])
  const [truncated, setTruncated] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)

  const [scrub, setScrub] = useState(0)
  const [motor, setMotor] = useState('')
  const [params, setParams] = useState<FilterParams>(DEFAULT_PARAMS)
  const [sim, setSim] = useState<SimResp | null>(null)
  const [simBusy, setSimBusy] = useState(false)

  const fetchRuns = () => {
    setLoading(true)
    api.get<RunSummary[]>('/debug/runs')
      .then(setRuns)
      .catch(() => setRuns([]))
      .finally(() => setLoading(false))
  }
  useEffect(() => { fetchRuns() }, [])

  const motors = useMemo(() => {
    const mn = detail?.meta?.motor_names as string[] | undefined
    if (mn && mn.length) return mn
    const t = control.find((c) => c.target)?.target
    return t ? Object.keys(t) : []
  }, [detail, control])

  // 런 선택 → 레코드 로드
  useEffect(() => {
    if (!selected) return
    setDetailLoading(true)
    setSim(null); setScrub(0); setParams(DEFAULT_PARAMS)
    Promise.all([
      api.get<RunSummary>(`/debug/runs/${selected}`),
      api.get<RecordsResp<ObsRec>>(`/debug/runs/${selected}/records/observations`),
      api.get<RecordsResp<InfRec>>(`/debug/runs/${selected}/records/inference`),
      api.get<RecordsResp<ControlRec>>(`/debug/runs/${selected}/records/control`),
    ]).then(([d, o, i, c]) => {
      setDetail(d)
      setObs(o.records); setInf(i.records); setControl(c.records)
      setTruncated(o.truncated || i.truncated || c.truncated)
      const fps = (d.meta?.fps as number) || 20
      setParams((p) => ({ ...p, fps }))
    }).catch(() => {
      setDetail(null); setObs([]); setInf([]); setControl([]); setTruncated(false)
    }).finally(() => setDetailLoading(false))
  }, [selected])

  useEffect(() => { if (motors.length && !motors.includes(motor)) setMotor(motors[0]) }, [motors, motor])

  // 필터 시뮬레이션 (디바운스)
  const simTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => {
    if (!selected || control.length === 0) return
    clearTimeout(simTimer.current)
    simTimer.current = setTimeout(() => {
      setSimBusy(true)
      api.post<SimResp>(`/debug/runs/${selected}/simulate`, params)
        .then(setSim)
        .catch(() => setSim(null))
        .finally(() => setSimBusy(false))
    }, 300)
    return () => clearTimeout(simTimer.current)
  }, [selected, control, params])

  const handleDelete = async (id: string) => {
    if (!await askConfirm(`디버그 런 ${id} 을(를) 삭제하시겠습니까?`)) return
    try {
      await api.delete(`/debug/runs/${id}`)
      if (selected === id) { setSelected(null); setDetail(null) }
      fetchRuns()
    } catch { /* ignore */ }
  }

  const curObs = obs[scrub]
  const t0 = control[0]?.t ?? 0
  const markerX = curObs ? curObs.t - t0 : null
  const curInf = curObs ? (inf.find((r) => r.seq === curObs.seq) ?? inf[scrub]) : undefined
  const motorIdx = motors.indexOf(motor)

  // 배열 그래프 series (시뮬 결과 기반)
  const arraySeries: Series[] = useMemo(() => {
    if (!sim || !sim.series[motor]) return []
    const s = sim.series[motor]
    return [
      { label: 'target (raw)', color: '#6b7fff', data: s.target },
      { label: 'actual', color: '#ff6432', data: s.actual, dash: true },
      { label: 'filtered (녹화)', color: '#32c832', data: s.filtered_recorded },
      { label: 'filtered (시뮬)', color: '#eab308', data: s.filtered_sim },
    ]
  }, [sim, motor])

  const chunkSeries: Series[] = useMemo(() => {
    if (!curInf || motorIdx < 0) return []
    const data = curInf.action_chunk.map((row) => (row && motorIdx < row.length ? row[motorIdx] : null))
    return [{ label: `예측 궤적 (${motor})`, color: '#22d3ee', data }]
  }, [curInf, motorIdx, motor])
  const chunkX = useMemo(() => (curInf ? curInf.action_chunk.map((_, i) => i) : []), [curInf])

  return (
    // 앱 셸 안의 보통 페이지다 — 예전엔 자기 헤더("Piper Studio"·메인으로)를 가진 새 창이었다.
    // "로그"가 아니라 추론 런의 분석 도구라 로그 페이지(/logs)와 이름이 겹치지 않게 한다.
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold">추론 분석</h1>
          <p className="mt-1 text-xs text-neutral-400">추론에서 "디버그 모드"를 켜고 돌린 런 — 프레임·관절 궤적·액션 청크·필터 시뮬레이션</p>
        </div>
        <button onClick={fetchRuns} className="shrink-0 px-3 py-1.5 text-sm rounded bg-neutral-700 hover:bg-neutral-600 transition-colors">
          새로고침
        </button>
      </div>

      {/* 2열 — 최소 폭 고정(수집·학습·추론과 같은 헬퍼). 우측이 `1fr` 이면 한 번 그려진
          Plotly SVG 의 픽셀 폭과 긴 mono 모델 경로가 min-content 가 되어 열을 밀고, 넘친
          쪽(버튼·셀렉트·그래프 오른쪽)이 잘려 사라진다. */}
      <div {...twoColumns([1, 4], [200, 520], 16)}>
        {/* 좌: 런 목록 (컴팩트 사이드바) */}
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 overflow-hidden self-start divide-y divide-neutral-700/50">
          {loading ? (
            <p className="text-neutral-400 text-xs p-3">로딩 중...</p>
          ) : runs.length === 0 ? (
            <p className="text-neutral-400 text-xs p-3">디버그 런이 없습니다. 추론 시 "디버그 모드"를 켜세요.</p>
          ) : (
            runs.map((r) => (
              <div key={r.id}
                onClick={() => setSelected(r.id)}
                className={`px-2.5 py-2 cursor-pointer ${selected === r.id ? 'bg-blue-600/20' : 'hover:bg-neutral-700/30'}`}>
                <div className="flex items-center justify-between gap-1">
                  <span className="font-mono text-xs truncate">{r.id}</span>
                  <span className="text-[10px] text-neutral-500 shrink-0">{r.counts.control}st</span>
                </div>
                <div className="flex items-center justify-between text-[10px] text-neutral-500 mt-0.5">
                  <span className="truncate">
                    {new Date(r.modified).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                    {' · '}{r.size_kb > 1024 ? `${(r.size_kb / 1024).toFixed(1)}MB` : `${r.size_kb.toFixed(0)}KB`}
                  </span>
                  <span className="flex gap-1.5 shrink-0">
                    <a href={`/api/debug/runs/${r.id}/download`} download onClick={(e) => e.stopPropagation()}
                      className="text-blue-400 hover:text-blue-300">zip</a>
                    <button onClick={(e) => { e.stopPropagation(); handleDelete(r.id) }}
                      className="text-red-400 hover:text-red-300">삭제</button>
                  </span>
                </div>
              </div>
            ))
          )}
        </div>

        {/* 우: 상세 */}
        <div className="space-y-4">
          {!selected ? (
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-8 text-center text-neutral-500 text-sm">
              왼쪽에서 런을 선택하세요.
            </div>
          ) : detailLoading ? (
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-8 text-center text-neutral-400 text-sm">로딩 중...</div>
          ) : (
            <>
              {/* 메타 헤더 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                  <h3 className="text-sm font-semibold font-mono break-all">{selected}</h3>
                  <span className="text-xs text-neutral-400">{detail?.mode} · obs {detail?.counts.observations} / inf {detail?.counts.inference} / ctrl {detail?.counts.control}</span>
                </div>
                {/* 모델 경로는 공백 없는 긴 mono 한 덩어리 — 못 쪼개면 카드 밖으로 나간다 */}
                <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-neutral-400">
                  <span className="min-w-0 break-all">model: <span className="text-neutral-300 font-mono">{String(detail?.meta?.policy_path || detail?.meta?.pretrained_path || '?')}</span></span>
                  <span>fps: {String(detail?.meta?.fps ?? '?')}</span>
                  <span>cameras: {detail?.cameras.join(', ') || '-'}</span>
                </div>
                {truncated && <p className="text-[11px] text-amber-400">레코드가 많아 일부만 로드되었습니다 (표시가 잘릴 수 있음).</p>}
              </div>

              {/* 프레임 스크러버 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold">프레임 스크러버</h3>
                  <span className="text-xs text-neutral-400 font-mono">
                    {obs.length ? `frame ${scrub + 1}/${obs.length} · seq ${curObs?.seq ?? '-'}` : 'no frames'}
                  </span>
                </div>
                {obs.length > 0 ? (
                  <>
                    <div className="flex items-center gap-2">
                      <button onClick={() => setScrub((s) => Math.max(0, s - 1))} className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600">◀</button>
                      <input type="range" min={0} max={obs.length - 1} value={scrub}
                        onChange={(e) => setScrub(Number(e.target.value))} className="flex-1 accent-blue-500" />
                      <button onClick={() => setScrub((s) => Math.min(obs.length - 1, s + 1))} className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600">▶</button>
                    </div>
                    <div className="flex flex-wrap gap-3">
                      {curObs && Object.keys(curObs.images).map((cam) => (
                        <div key={cam} className="space-y-1">
                          <div className="text-[10px] text-neutral-500">{cam}</div>
                          <img src={`/api/debug/runs/${selected}/images/${cam}/${curObs.seq}`} alt={cam}
                            className="h-40 rounded border border-neutral-700 bg-neutral-900 object-contain"
                            onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                            onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }} />
                        </div>
                      ))}
                      <div className="flex-1 min-w-[240px] space-y-2">
                        <div className="text-[10px] text-neutral-500">
                          state (관절) · inference {curInf?.inference_ms != null ? `${curInf.inference_ms}ms` : '-'}
                        </div>
                        <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px] font-mono">
                          {curObs && motors.map((m) => (
                            <div key={m} className="flex justify-between">
                              <span className="text-neutral-500 truncate">{m}</span>
                              <span className="text-neutral-300">{curObs.state?.[m]?.toFixed(2) ?? '-'}</span>
                            </div>
                          ))}
                        </div>
                        {/* ⚠ **자리를 미리 잡는다** (에피소드 페이지와 같다, 0f90f4e). 프레임을 넘기다
                            추론 기록이 없는 프레임을 만나면 청크가 비는데, 그때 그래프를 **떼면** 칸이
                            사라져 옆 이미지 행이 다시 흐르고, 다음 프레임에 새 Plotly 가 흔들리는 폭에서
                            그려진다 — 폭 0 을 한 번 잡으면 빈 채로 굳는다. 칸은 늘 두고 안에서만 바뀐다. */}
                        <div className="pt-1" style={{ minHeight: 150 }}>
                          {chunkX.length > 1
                            ? <PlotlyChart x={chunkX} series={chunkSeries} height={150}
                                uirevision={`chunk-${selected}-${curObs?.seq}`} />
                            : <p className="text-[10px] text-neutral-600">이 프레임엔 추론 기록이 없습니다</p>}
                        </div>
                        <p className="text-[10px] text-neutral-500">현 시점 정책 raw action chunk (미래 예측 스텝)</p>
                      </div>
                    </div>
                  </>
                ) : (
                  <p className="text-xs text-neutral-500">이 런에는 관측 프레임이 없습니다.</p>
                )}
              </div>

              {/* 배열 그래프 + 필터 시뮬레이션 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold">배열 그래프 · 필터 시뮬레이션</h3>
                  <div className="flex items-center gap-2">
                    {simBusy && <span className="text-[10px] text-neutral-500">계산 중...</span>}
                    <select value={motor} onChange={(e) => setMotor(e.target.value)}
                      className="px-2 py-1 text-xs rounded bg-neutral-900 border border-neutral-700 text-neutral-100">
                      {motors.map((m) => <option key={m} value={m}>{m}</option>)}
                    </select>
                  </div>
                </div>

                {/* 런을 바꾸면 `sim` 이 null 이 되어 그래프가 떨어지고 카드가 한 줄로 접혔다가
                    결과가 오면 다시 펴진다 — 그 사이 아래 슬라이더가 위아래로 튄다. 칸은 최종
                    높이로 늘 둔다. */}
                <div style={{ minHeight: 280 }}>
                  {sim && sim.series[motor] ? (
                    <PlotlyChart x={sim.t} series={arraySeries} markerX={markerX} height={280}
                      uirevision={`arr-${selected}-${motor}`} yTitle={motor} />
                  ) : (
                    <p className="text-xs text-neutral-500">{control.length ? '시뮬레이션 준비 중...' : 'control 데이터가 없습니다.'}</p>
                  )}
                </div>

                {/* 필터 파라미터 */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 pt-1">
                  <FilterSlider label="lowpass_alpha" value={params.lowpass_alpha} min={0.05} max={1} step={0.05}
                    onChange={(v) => setParams((p) => ({ ...p, lowpass_alpha: v }))} hint="1=OFF, 낮을수록 강함" />
                  <FilterSlider label="max_velocity (deg/s)" value={params.max_velocity} min={0} max={1000} step={10}
                    onChange={(v) => setParams((p) => ({ ...p, max_velocity: v }))} hint="0=무제한" />
                  <FilterSlider label="max_gripper_velocity" value={params.max_gripper_velocity} min={0} max={500} step={10}
                    onChange={(v) => setParams((p) => ({ ...p, max_gripper_velocity: v }))} hint="0=무제한" />
                  <FilterSlider label="max_jerk (deg/s²)" value={params.max_jerk} min={0} max={5000} step={50}
                    onChange={(v) => setParams((p) => ({ ...p, max_jerk: v }))} hint="0=무제한" />
                  <FilterSlider label="interpolation_steps" value={params.interpolation_steps} min={0} max={10} step={1}
                    onChange={(v) => setParams((p) => ({ ...p, interpolation_steps: v }))} hint="0=OFF (근사)" />
                  <FilterSlider label="fps" value={params.fps} min={1} max={60} step={1}
                    onChange={(v) => setParams((p) => ({ ...p, fps: v }))} hint="속도/jerk 계산 기준" />
                </div>
                <div className="flex items-center justify-between pt-1">
                  <p className="text-[11px] text-neutral-500">
                    녹화된 raw <span className="text-[#6b7fff]">target</span>을 실제 필터코드로 재생 → <span className="text-[#eab308]">filtered(시뮬)</span>.
                    velocity/jerk/lowpass 충실, interpolation·서버 smoothing은 근사.
                  </p>
                  <button onClick={() => setParams((p) => ({ ...DEFAULT_PARAMS, fps: p.fps }))}
                    className="text-xs text-neutral-400 hover:text-white underline">초기화</button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function FilterSlider({ label, value, min, max, step, onChange, hint }: {
  label: string; value: number; min: number; max: number; step: number; onChange: (v: number) => void; hint?: string
}) {
  return (
    <div className="space-y-0.5">
      <div className="flex items-center justify-between text-xs">
        <span className="text-neutral-400">{label}</span>
        <span className="font-mono text-neutral-200">{value}</span>
      </div>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))} className="w-full accent-blue-500" />
      {hint && <div className="text-[10px] text-neutral-600">{hint}</div>}
    </div>
  )
}
