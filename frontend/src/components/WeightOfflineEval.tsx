/**
 * 오프라인 평가 (W2) + 편집 장면의 반응 — feature/weight-analyzer.md §4·§5.
 *
 * 위: 체크포인트를 데이터셋 에피소드에 돌린 결과 — 페이즈별·관절별 오차, 에피소드별 그리퍼 시점,
 *     에피소드 그래프(녹화 대 예측, 뒤에 페이즈 띠).
 * 아래: 학습 프레임을 복제 도장으로 고쳐 원본과 같은 관절 상태로 넣고, 두 계획(말단 궤적)을 겹친다.
 *     정답 행동이 없어도 된다 — 배경을 지웠으면 같아야, 목표를 지웠으면 달라야 정상이다.
 */
import { useEffect, useMemo, useState } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import type { Data, Layout, PlotMouseEvent } from 'plotly.js'
import { api } from '../services/api'
import { useT } from '../i18n'
import { PHASE_COLORS } from '../config/phases'
import type { Dataset, DatasetDetail } from '../types/models'
import type { EpisodeSeries, JobInfo, JobStatus, Reaction, RunDetail, RunItem, Summary } from './weightTypes'
import { sampleEpisodes, stepLabel } from './weightTypes'
import type { JobRequest } from './useWevalJob'
import type { Sample } from './WeightCheckpoints'
import SceneEditor from './SceneEditor'

const Plot = createPlotlyComponent(Plotly)
const BG = '#171717'
const SERIES = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']
// 두 계획이 이만큼 이상 갈라지면 "반응했다" 로 읽는다 — 잠정값, 실기 사례로 다시 잡는다
const REACT_MM = 15
const SUMMARY_H = 220
const SERIES_H = 300
const REACT_H = 380

const axis = { gridcolor: '#2a2a2a', zerolinecolor: '#444', color: '#888' }

export default function WeightOfflineEval({ run, setRun, checkpoint, setCheckpoint, resultKey, setResultKey, sample, job, version, onStart }: {
  run: string
  setRun: (run: string) => void
  checkpoint: string
  setCheckpoint: (id: string) => void
  resultKey: string | null
  setResultKey: (key: string | null) => void
  sample: Sample
  job: JobStatus
  version: number
  onStart: (req: JobRequest) => void
}) {
  const t = useT()
  const [runs, setRuns] = useState<RunItem[]>([])
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [dataset, setDataset] = useState('')
  const [results, setResults] = useState<JobInfo[]>([])
  // 결과는 **어느 키의 것인지와 함께** 둔다. 키가 바뀌면 옛 결과가 저절로 안 쓰인다 —
  // effect 첫 줄에서 비우면 렌더가 한 번 더 돈다(React 컴파일러 규칙)
  const [summaryOf, setSummaryOf] = useState<{ key: string; data: Summary } | null>(null)
  const [episode, setEpisode] = useState<number | null>(null)
  const [seriesOf, setSeriesOf] = useState<{ id: string; data: EpisodeSeries } | null>(null)
  const summary = summaryOf && summaryOf.key === resultKey ? summaryOf.data : null
  const series = seriesOf && seriesOf.id === `${resultKey}:${episode}` ? seriesOf.data : null
  const [dims, setDims] = useState<number[]>([])
  // 에피소드 그래프에서 고른 프레임 → 장면 편집 반응의 프레임
  const [pickedFrame, setPickedFrame] = useState(0)

  useEffect(() => {
    api.get<RunItem[]>('/weval/runs', { timeoutMs: 60_000 }).then((r) => {
      setRuns(r)
      if (!run && r.length) setRun(r[0].run)
    }).catch(() => {})
    api.get<Dataset[]>('/datasets').then(setDatasets).catch(() => {})
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!run) return
    api.get<RunDetail>(`/weval/run?run=${encodeURIComponent(run)}`, { timeoutMs: 60_000 }).then((d) => {
      setDetail(d)
      if (!d.checkpoints.some((c) => c.id === checkpoint)) {
        const last = [...d.checkpoints].reverse().find((c) => c.step != null) ?? d.checkpoints[0]
        if (last) setCheckpoint(last.id)
      }
      if (d.dataset) setDataset((cur) => cur || d.dataset!)
    }).catch(() => setDetail(null))
  }, [run, version]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!checkpoint) return
    api.get<JobInfo[]>(`/weval/results?checkpoint=${encodeURIComponent(checkpoint)}`)
      .then((r) => {
        setResults(r)
        const done = r.filter((j) => j.state === 'done')
        if (!resultKey || !done.some((j) => j.key === resultKey)) setResultKey(done[0]?.key ?? null)
      }).catch(() => setResults([]))
  }, [checkpoint, version]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!resultKey) return
    api.get<Summary>(`/weval/results/${resultKey}`).then((s) => {
      setSummaryOf({ key: resultKey, data: s })
      setEpisode(s.by_episode[0]?.episode ?? null)
      // 기본으로 볼 차원: 그리퍼 전부 + 가장 많이 틀린 관절 둘
      const grips = s.dims.map((d, i) => (d.kind === 'gripper' ? i : -1)).filter((i) => i >= 0)
      const worst = s.dims.map((d, i) => ({ i, e: d.kind === 'joint' ? s.per_dim[i] : -1 }))
        .sort((a, b) => b.e - a.e).slice(0, 2).map((x) => x.i)
      setDims([...worst, ...grips])
    }).catch(() => {})
  }, [resultKey])

  useEffect(() => {
    if (!resultKey || episode == null) return
    api.get<EpisodeSeries>(`/weval/results/${resultKey}/episodes/${episode}`)
      .then((d) => setSeriesOf({ id: `${resultKey}:${episode}`, data: d })).catch(() => {})
  }, [resultKey, episode])

  const runEval = async () => {
    if (!checkpoint || !dataset) return
    try {
      const d = await api.get<DatasetDetail>(`/datasets/${dataset}`)
      const all = d.episodes.map((e, i) => Number((e as { episode_index?: number }).episode_index ?? i))
      onStart({ checkpoint, dataset, episodes: sampleEpisodes(all, sample.n), stride: sample.stride })
    } catch { /* 목록에 있는 데이터셋이라 실패할 일은 드물다 */ }
  }

  // ── 요약 그래프: 페이즈별 | 관절별 ──
  const { sumData, sumLayout } = useMemo(() => {
    if (!summary) return { sumData: [] as Data[], sumLayout: {} as Partial<Layout> }
    const ph = summary.by_phase.filter((p) => p.joint_deg != null)
    const jointIdx = summary.dims.map((d, i) => (d.kind === 'joint' ? i : -1)).filter((i) => i >= 0)
    return {
      sumData: [
        { type: 'bar', x: ph.map((p) => p.name), y: ph.map((p) => p.joint_deg), xaxis: 'x', yaxis: 'y',
          marker: { color: ph.map((p) => PHASE_COLORS[p.phase] ?? '#525252') },
          text: ph.map((p) => `n=${p.n}`), hovertemplate: '%{x}: %{y:.2f}°<br>%{text}<extra></extra>' },
        { type: 'bar', x: jointIdx.map((i) => summary.dims[i].name.replace('.pos', '')), y: jointIdx.map((i) => summary.per_dim[i]),
          xaxis: 'x2', yaxis: 'y2', marker: { color: '#3987e5' }, hovertemplate: '%{x}: %{y:.2f}°<extra></extra>' },
      ] as Data[],
      sumLayout: {
        height: SUMMARY_H, margin: { l: 44, r: 8, t: 24, b: 56 }, paper_bgcolor: BG, plot_bgcolor: BG,
        font: { color: '#999', size: 10 }, showlegend: false, bargap: 0.25,
        grid: { rows: 1, columns: 2, pattern: 'independent', xgap: 0.12 },
        xaxis: { ...axis }, yaxis: { ...axis, title: { text: t('관절 오차 (°)') }, rangemode: 'tozero' },
        xaxis2: { ...axis, tickangle: -40 }, yaxis2: { ...axis, rangemode: 'tozero' },
        annotations: [
          { text: t('페이즈별'), x: 0, y: 1.12, xref: 'x domain', yref: 'y domain', showarrow: false, xanchor: 'left', font: { color: '#a3a3a3', size: 11 } },
          { text: t('관절별'), x: 0, y: 1.12, xref: 'x2 domain', yref: 'y2 domain', showarrow: false, xanchor: 'left', font: { color: '#a3a3a3', size: 11 } },
        ],
      } as Partial<Layout>,
    }
  }, [summary, t])

  // ── 에피소드 그래프: 녹화(실선) 대 예측(점선), 뒤에 페이즈 띠 ──
  const { serData, serLayout } = useMemo(() => {
    if (!series) return { serData: [] as Data[], serLayout: {} as Partial<Layout> }
    const traces: Data[] = []
    dims.forEach((i, k) => {
      const c = SERIES[k % SERIES.length]
      const name = series.names[i]?.replace('.pos', '') ?? String(i)
      traces.push({ type: 'scatter', mode: 'lines', x: series.frames, y: series.true[i], name: `${name} ${t('녹화')}`,
        line: { color: c, width: 2 }, hovertemplate: `${name} ${t('녹화')}: %{y:.1f}<extra></extra>` } as Data)
      traces.push({ type: 'scatter', mode: 'lines+markers', x: series.frames, y: series.pred[i], name: `${name} ${t('예측')}`,
        line: { color: c, width: 1.5, dash: 'dot' }, marker: { size: 4 }, hovertemplate: `${name} ${t('예측')}: %{y:.1f}<extra></extra>` } as Data)
    })
    // 페이즈가 바뀌는 곳마다 띠 하나
    const shapes: Partial<Layout>['shapes'] = []
    let s = 0
    for (let i = 1; i <= series.frames.length; i++) {
      if (i === series.frames.length || series.phase[i] !== series.phase[s]) {
        const code = series.phase[s]
        if (code >= 0) {
          shapes.push({ type: 'rect', xref: 'x', yref: 'paper', x0: series.frames[s], x1: series.frames[Math.min(i, series.frames.length - 1)],
            y0: 0, y1: 1, fillcolor: PHASE_COLORS[code] ?? '#525252', opacity: 0.12, line: { width: 0 }, layer: 'below' })
        }
        s = i
      }
    }
    return {
      serData: traces,
      serLayout: {
        height: SERIES_H, margin: { l: 44, r: 8, t: 8, b: 32 }, paper_bgcolor: BG, plot_bgcolor: BG,
        font: { color: '#999', size: 10 }, legend: { orientation: 'h', y: 1.12, x: 0, font: { size: 10 } },
        hovermode: 'x unified', xaxis: { ...axis, title: { text: t('프레임') } }, yaxis: { ...axis },
        shapes, uirevision: `${resultKey}-${episode}`,
      } as Partial<Layout>,
    }
  }, [series, dims, resultKey, episode, t])

  const ckpts = detail?.checkpoints ?? []
  const running = job.state === 'running'
  const o = summary?.overall
  const select = 'rounded bg-neutral-800 border border-neutral-700 px-2 py-1 text-sm'

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <select value={run} onChange={(e) => setRun(e.target.value)} className={`${select} min-w-[18rem]`}>
          {runs.map((r) => <option key={r.run} value={r.run}>{r.run} · {r.policy_type}</option>)}
        </select>
        <select value={checkpoint} onChange={(e) => setCheckpoint(e.target.value)} className={select}>
          {ckpts.map((c) => <option key={c.id} value={c.id}>{stepLabel(c)}</option>)}
        </select>
        <span className="text-neutral-500">{t('데이터')}</span>
        <select value={dataset} onChange={(e) => setDataset(e.target.value)} className={`${select} min-w-[16rem]`}>
          {datasets.filter((d) => !d.baked).map((d) => (
            <option key={d.id} value={d.id}>{d.id}{d.id === detail?.dataset ? t(' (학습 데이터)') : ''}</option>
          ))}
        </select>
        <button onClick={() => void runEval()} disabled={running || !checkpoint || !dataset}
                className="rounded bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-40">
          {t('평가 실행')}
        </button>
        <span className="text-xs text-neutral-500">{t('표본 {n}에피소드 · {stride}프레임 간격 (체크포인트 탭에서 바꾼다)', { n: sample.n, stride: sample.stride })}</span>
      </div>

      {running && job.job && (
        <div className="text-xs text-neutral-400">
          {t('평가 중')} {job.job.checkpoint} · {job.progress ? `${job.progress.done}/${job.progress.total}` : '…'}
        </div>
      )}

      {results.length > 0 && (
        <div className="flex flex-wrap gap-1.5 text-xs">
          {results.map((r) => (
            <button key={r.key} onClick={() => setResultKey(r.key)} disabled={r.state !== 'done'}
                    className={`rounded border px-2 py-1 ${resultKey === r.key ? 'border-blue-500 text-white' : 'border-neutral-700 text-neutral-400 hover:text-white'} disabled:opacity-40`}>
              {r.dataset.split('/').pop()} · {t('{n}에피소드', { n: r.episodes.length })} · ÷{r.stride}
              {r.summary?.joint_deg != null && ` · ${r.summary.joint_deg.toFixed(2)}°`}
              {r.state !== 'done' && ` · ${r.state}`}
            </button>
          ))}
        </div>
      )}

      {summary && o && (
        <>
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-neutral-300">
            <span>{t('관절 오차 {v}°', { v: o.joint_deg?.toFixed(2) ?? '—' })}</span>
            <span className="text-neutral-400">{t('청크 오차 {v}°', { v: o.chunk_joint_deg?.toFixed(2) ?? '—' })}</span>
            <span className="text-neutral-400">{t('그리퍼 오차 {v}', { v: o.gripper?.toFixed(1) ?? '—' })}</span>
            <span className={o.grip_missed ? 'text-amber-400' : 'text-neutral-400'}>
              {t('그리퍼 시점 ±{s}초 · 닫힘 {c}번 중 놓침 {m}', {
                s: o.grip_mean_abs_s?.toFixed(2) ?? '—', c: o.grip_closes ?? 0, m: o.grip_missed ?? 0,
              })}
            </span>
            <span className="text-neutral-500">{t('{n}프레임 · {s}초', { n: summary.meta.frames, s: summary.meta.elapsed_s })}</span>
          </div>

          <div className="rounded-lg border border-neutral-700 bg-neutral-900 px-1 pt-1">
            <Plot data={sumData} layout={sumLayout} config={{ displaylogo: false, responsive: true, displayModeBar: false }}
                  style={{ width: '100%', height: SUMMARY_H }} useResizeHandler />
          </div>

          <div className="flex flex-col xl:flex-row gap-3 items-start">
            <div className="w-full xl:w-80 shrink-0 rounded-lg border border-neutral-700 bg-neutral-800 overflow-auto max-h-[22rem]">
              <table className="w-full text-xs tabular-nums [&_td]:px-2 [&_th]:px-2">
                <thead className="text-neutral-500 sticky top-0 bg-neutral-800">
                  <tr className="text-left">
                    <th className="font-normal py-1">ep</th>
                    <th className="font-normal text-right">{t('관절 오차')}</th>
                    <th className="font-normal text-right" title={t('예측 닫힘 − 녹화 닫힘 (초). 음수 = 일찍 닫는다')}>{t('그리퍼 Δ')}</th>
                  </tr>
                </thead>
                <tbody className="text-neutral-300">
                  {summary.by_episode.map((e) => {
                    const ds = e.grip.flatMap((g) => g.deltas_s)
                    const miss = e.grip.reduce((a, g) => a + g.missed, 0)
                    return (
                      <tr key={e.episode} onClick={() => setEpisode(e.episode)}
                          className={`cursor-pointer border-t border-neutral-700/60 hover:bg-neutral-700/40 ${episode === e.episode ? 'bg-neutral-700' : ''}`}>
                        <td className="py-1 font-mono">#{e.episode}</td>
                        <td className="text-right">{e.joint_deg?.toFixed(2)}°</td>
                        <td className="text-right">
                          {ds.length ? ds.map((d) => `${d >= 0 ? '+' : ''}${d.toFixed(2)}`).join(' ') : '—'}
                          {miss > 0 && <span className="text-amber-400"> {t('놓침 {n}', { n: miss })}</span>}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <div className="w-full flex-1 min-w-0 rounded-lg border border-neutral-700 bg-neutral-900 px-1 pt-1">
              <div className="flex flex-wrap gap-1 px-2 pt-1 text-xs">
                {summary.dims.map((d, i) => (
                  <button key={d.name} onClick={() => setDims((cur) => (cur.includes(i) ? cur.filter((x) => x !== i) : [...cur, i]))}
                          className={`rounded px-1.5 py-0.5 ${dims.includes(i) ? 'bg-neutral-700 text-white' : 'text-neutral-500 hover:text-white'}`}>
                    {d.name.replace('.pos', '')}
                  </button>
                ))}
              </div>
              {series ? (
                <Plot data={serData} layout={serLayout} config={{ displaylogo: false, responsive: true }}
                      style={{ width: '100%', height: SERIES_H }} useResizeHandler
                      onClick={(ev: Readonly<PlotMouseEvent>) => {
                        const x = ev.points?.[0]?.x
                        if (typeof x === 'number') setPickedFrame(x)
                      }} />
              ) : (
                <div className="p-6 text-center text-xs text-neutral-500">{t('읽는 중…')}</div>
              )}
              <div className="px-2 pb-2 text-xs text-neutral-500">
                {t('실선 = 녹화, 점선 = 예측(그 프레임의 첫 동작). 뒤의 색 띠가 페이즈다. 그래프를 누르면 그 프레임이 아래 장면 편집으로 간다')}
              </div>
            </div>
          </div>
        </>
      )}

      {!summary && results.length === 0 && checkpoint && (
        <div className="rounded-lg border border-dashed border-neutral-700 p-8 text-center text-sm text-neutral-500">
          {t('이 체크포인트는 아직 평가하지 않았다 — [평가 실행]')}
        </div>
      )}

      <ReactionPanel
        checkpoint={checkpoint}
        dataset={dataset}
        episode={episode}
        frame={pickedFrame}
        setFrame={setPickedFrame}
        fromChart={series != null}
      />
    </div>
  )
}

/** 장면 편집 반응 — 고친 한 프레임을 원본과 같은 관절 상태로 넣어 두 계획을 견준다. */
function ReactionPanel({ checkpoint, dataset, episode, frame, setFrame, fromChart }: {
  checkpoint: string
  dataset: string
  episode: number | null
  frame: number
  setFrame: (f: number) => void
  fromChart: boolean
}) {
  const t = useT()
  const [ep, setEp] = useState<number>(episode ?? 0)
  const [frames, setFrames] = useState<{ sid: string; cameras: string[]; task: string } | null>(null)
  const [cam, setCam] = useState('')
  const [edits, setEdits] = useState<Record<string, string | null>>({})
  const [intent, setIntent] = useState<'background' | 'target'>('background')
  const [busy, setBusy] = useState<'' | 'frames' | 'react'>('')
  const [result, setResult] = useState<Reaction | null>(null)
  const [error, setError] = useState('')

  useEffect(() => { if (episode != null) setEp(episode) }, [episode])

  const loadFrames = async () => {
    setBusy('frames'); setError(''); setResult(null); setEdits({})
    try {
      const r = await api.post<{ sid: string; cameras: string[]; task: string }>(
        '/weval/frames', { dataset, episode: ep, frame }, { timeoutMs: 300_000 })
      setFrames(r)
      setCam(r.cameras[0] ?? '')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy('') }
  }

  const react = async () => {
    setBusy('react'); setError('')
    try {
      const body = { checkpoint, dataset, episode: ep, frame,
        edits: Object.fromEntries(Object.entries(edits).filter(([, v]) => v)) }
      setResult(await api.post<Reaction>('/weval/reaction', body, { timeoutMs: 300_000 }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy('') }
  }

  const editedCams = Object.entries(edits).filter(([, v]) => v).map(([k]) => k)
  const arms = result ? Object.keys(result.original_tips) : []

  const { data, layout } = useMemo(() => {
    if (!result) return { data: [] as Data[], layout: {} as Partial<Layout> }
    const traces: Data[] = []
    const n = arms.length || 1
    const l: Record<string, unknown> = {
      height: REACT_H, margin: { l: 0, r: 0, t: 24, b: 0 }, paper_bgcolor: BG, font: { color: '#999', size: 10 },
      legend: { orientation: 'h', x: 0, y: 1, yanchor: 'bottom', font: { size: 10 } }, uirevision: 'react',
      annotations: n > 1 ? arms.map((a, i) => ({ text: a, showarrow: false, xref: 'paper', yref: 'paper',
        x: (i + 0.5) / n, y: 1, yanchor: 'top', font: { size: 12, color: '#d4d4d4' } })) : [],
    }
    arms.forEach((a, i) => {
      const scene = i === 0 ? 'scene' : `scene${i + 1}`
      const line = (pts: number[][], color: string, name: string, show: boolean) => ({
        type: 'scatter3d', scene, mode: 'lines', name, showlegend: show, legendgroup: name,
        x: pts.map((p) => p[0]), y: pts.map((p) => p[1]), z: pts.map((p) => p[2]),
        line: { color, width: 6 }, hovertemplate: `${name}<br>x %{x:.0f} y %{y:.0f} z %{z:.0f} mm<extra></extra>`,
      } as Data)
      const o = result.original_tips[a]
      traces.push(line(o, '#fafafa', t('원본 계획'), i === 0))
      traces.push({ type: 'scatter3d', scene, mode: 'markers', showlegend: false, hoverinfo: 'skip',
        x: [o[0][0]], y: [o[0][1]], z: [o[0][2]], marker: { size: 5, color: '#737373', symbol: 'diamond' } } as Data)
      if (result.edited_tips?.[a]) traces.push(line(result.edited_tips[a], '#d95926', t('편집 계획'), i === 0))
      l[scene] = {
        domain: { x: [i / n + (i ? 0.01 : 0), (i + 1) / n - (i < n - 1 ? 0.01 : 0)], y: [0, n > 1 ? 0.94 : 1] },
        aspectmode: 'data', bgcolor: BG,
        xaxis: { ...axis, title: { text: 'x (mm)' }, backgroundcolor: BG }, yaxis: { ...axis, title: { text: 'y (mm)' }, backgroundcolor: BG },
        zaxis: { ...axis, title: { text: 'z (mm)' }, backgroundcolor: BG }, camera: { eye: { x: -1.4, y: -1.6, z: 1.0 } },
      }
    })
    return { data: traces, layout: l as Partial<Layout> }
  }, [result, arms.join(','), t]) // eslint-disable-line react-hooks/exhaustive-deps

  const verdict = (mm: number) => (intent === 'background'
    ? (mm >= REACT_MM
      ? { ok: false, text: t('⚠ 배경·방해물만 지웠는데 계획이 {mm}mm 달라졌다 — 배경에 기대고 있다', { mm: mm.toFixed(0) }) }
      : { ok: true, text: t('✔ 배경이 바뀌어도 계획이 그대로다({mm}mm)', { mm: mm.toFixed(0) }) })
    : (mm < REACT_MM
      ? { ok: false, text: t('⚠ 목표를 지웠는데 계획이 거의 같다({mm}mm) — 화면을 안 보고 궤적을 외웠다', { mm: mm.toFixed(0) }) }
      : { ok: true, text: t('✔ 목표가 사라지자 계획이 바뀌었다({mm}mm)', { mm: mm.toFixed(0) }) }))

  const input = 'w-20 rounded bg-neutral-900 border border-neutral-700 px-1 py-0.5 text-right'
  const seg = (on: boolean) => `px-2 py-0.5 rounded text-xs ${on ? 'bg-neutral-700 text-white' : 'text-neutral-500 hover:text-white'}`

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-neutral-300">{t('장면 편집 반응')}</span>
        <span className="text-xs text-neutral-500">{t('학습 프레임의 물체를 옆 배경으로 지우고, 같은 관절 상태에서 계획이 어떻게 바뀌나 본다')}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-neutral-400">
        {t('에피소드')}
        <input type="number" min={0} value={ep} onChange={(e) => setEp(Number(e.target.value))} className={input} />
        {t('프레임')}
        <input type="number" min={0} value={frame} onChange={(e) => setFrame(Number(e.target.value))} className={input} />
        {fromChart && <span className="text-neutral-500">{t('(위 그래프를 눌러도 된다)')}</span>}
        <button onClick={() => void loadFrames()} disabled={!dataset || busy !== ''}
                className="rounded border border-neutral-600 px-2 py-0.5 text-neutral-300 hover:bg-neutral-700 disabled:opacity-40">
          {busy === 'frames' ? t('불러오는 중…') : t('프레임 불러오기')}
        </button>
      </div>

      {frames && (
        <>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <div className="flex gap-0.5 rounded bg-neutral-900 p-0.5">
              {frames.cameras.map((c) => (
                <button key={c} className={seg(cam === c)} onClick={() => setCam(c)}>
                  {c}{edits[c] ? ' ✎' : ''}
                </button>
              ))}
            </div>
            <span className="text-neutral-500">{t('카메라마다 따로 고친다. 고친 카메라만 바뀌고 나머지는 원본 그대로 들어간다')}</span>
          </div>
          {cam && (
            <div className="max-w-3xl">
              <SceneEditor key={`${frames.sid}-${cam}`} src={`/api/weval/frames/${frames.sid}/${cam}.png`}
                           onChange={(url) => setEdits((e) => ({ ...e, [cam]: url }))} />
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-neutral-400">{t('무엇을 지웠나')}</span>
            <div className="flex gap-0.5 rounded bg-neutral-900 p-0.5">
              <button className={seg(intent === 'background')} onClick={() => setIntent('background')}>{t('배경·방해물')}</button>
              <button className={seg(intent === 'target')} onClick={() => setIntent('target')}>{t('목표 물체')}</button>
            </div>
            <button onClick={() => void react()} disabled={!checkpoint || busy !== ''}
                    className="rounded bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-40">
              {busy === 'react' ? t('계산 중…') : editedCams.length ? t('반응 보기 ({cams})', { cams: editedCams.join(', ') }) : t('원본 계획만 보기')}
            </button>
          </div>
        </>
      )}

      {error && <div className="text-xs text-red-400">{error}</div>}

      {result && (
        <div className="space-y-2">
          {result.tip_distance && (
            <div className="space-y-1 text-xs">
              {Object.entries(result.tip_distance).map(([arm, d]) => {
                const v = verdict(d.mean_mm)
                return (
                  <div key={arm} className="flex flex-wrap gap-x-4">
                    <span className="text-neutral-300">{arm}</span>
                    <span className="text-neutral-400 tabular-nums">
                      {t('평균 {mean}mm · 최대 {max}mm · 끝 {end}mm', { mean: d.mean_mm, max: d.max_mm, end: d.end_mm })}
                    </span>
                    <span className={v.ok ? 'text-emerald-400' : 'text-amber-400'}>{v.text}</span>
                  </div>
                )
              })}
              {result.device === 'cpu' && <div className="text-neutral-500">{t('학습·추론이 GPU 를 쓰고 있어 CPU 로 계산했다')}</div>}
            </div>
          )}
          <div className="rounded-lg border border-neutral-700 bg-neutral-900">
            <Plot data={data} layout={layout} config={{ displaylogo: false, responsive: true }}
                  style={{ width: '100%', height: REACT_H }} useResizeHandler />
          </div>
          <div className="text-xs text-neutral-500">
            {t('선은 정책이 그 순간 세운 계획(청크 전체)을 말단 위치로 바꾼 것이다. 회색 마름모가 지금 위치')}
          </div>
        </div>
      )}
    </div>
  )
}
