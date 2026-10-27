/**
 * 체크포인트 개요 (W1) — 한 학습 실행의 체크포인트를 step 축 하나에 (feature/weight-analyzer.md §3).
 *
 * 학습 손실 · 오프라인 오차 · 실기 성공을 **같은 x(step), 다른 칸**에 둔다 — 단위가 달라 한 축에
 * 겹치면 읽을 수 없다. 오프라인 지표는 진단이고 판정은 실기다: 오프라인 1위와 실기 1위가 다르면
 * 그 사실을 화면이 먼저 말한다 (알약 조제 로봇 사례에서 두 번 있었다).
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import type { Data, Layout } from 'plotly.js'
import { api } from '../services/api'
import { useT } from '../i18n'
import type { DatasetDetail } from '../types/models'
import type { CkptRow, JobStatus, OfflineRef, RunDetail, RunItem } from './weightTypes'
import { sampleEpisodes, stepLabel } from './weightTypes'
import type { JobRequest } from './useWevalJob'

const Plot = createPlotlyComponent(Plotly)
const BG = '#171717'
const CHART_H = 330

export type Sample = { n: number; stride: number }

export default function WeightCheckpoints({ run, setRun, sample, setSample, job, version, onStart, onEnqueue, onStop, onOpen }: {
  run: string
  setRun: (run: string) => void
  sample: Sample
  setSample: (s: Sample) => void
  job: JobStatus
  version: number
  onStart: (req: JobRequest) => void
  onEnqueue: (reqs: JobRequest[]) => void
  onStop: () => void
  onOpen: (checkpoint: string, key: string | null) => void
}) {
  const t = useT()
  const [runs, setRuns] = useState<RunItem[]>([])
  const [detail, setDetail] = useState<RunDetail | null>(null)
  // 어느 데이터셋의 에피소드인지 함께 둔다 — 실행을 바꾸면 키가 달라져 옛 값이 저절로 안 쓰인다
  const [dsEps, setDsEps] = useState<{ ds: string; eps: number[] | null } | null>(null)

  useEffect(() => {
    api.get<RunItem[]>('/weval/runs', { timeoutMs: 60_000 }).then((r) => {
      setRuns(r)
      if (!run && r.length) setRun(r[0].run)
    }).catch(() => {})
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!run) return
    api.get<RunDetail>(`/weval/run?run=${encodeURIComponent(run)}`, { timeoutMs: 60_000 })
      .then(setDetail).catch(() => setDetail(null))
  }, [run, version])

  // 학습 데이터셋의 에피소드 — 표본을 거기서 고른다. 다른 기계에서 학습했으면 여기 없을 수 있다
  const trainDs = detail?.dataset ?? null
  useEffect(() => {
    if (!trainDs) return
    api.get<DatasetDetail>(`/datasets/${trainDs}`)
      .then((d) => setDsEps({ ds: trainDs, eps: d.episodes.map((e, i) => Number((e as { episode_index?: number }).episode_index ?? i)) }))
      .catch(() => setDsEps({ ds: trainDs, eps: null }))
  }, [trainDs])
  const episodes = dsEps?.ds === trainDs ? dsEps.eps : null
  const dsMissing = dsEps?.ds === trainDs && dsEps.eps == null

  const pick = useMemo(() => (episodes ? sampleEpisodes(episodes, sample.n) : []), [episodes, sample.n])
  const sameSample = useCallback((o: OfflineRef) =>
    o.dataset === trainDs && o.stride === sample.stride
    && o.episodes.length === pick.length && o.episodes.every((e, i) => e === pick[i]),  // 둘 다 오름차순
  [trainDs, sample.stride, pick])
  // 표에 보일 오프라인 결과 — 지금 표본과 같은 것을 먼저, 없으면 학습 데이터의 아무것
  const offlineOf = useCallback((r: CkptRow) =>
    r.offline.find(sameSample) ?? r.offline.find((o) => o.dataset === trainDs) ?? null,
  [sameSample, trainDs])

  const rows = useMemo(() => detail?.checkpoints ?? [], [detail])
  const lossAt = (step: number | null) => {
    const h = detail?.history
    if (!h || step == null || !h.steps.length) return null
    let best = 0
    for (let i = 0; i < h.steps.length; i++) if (Math.abs(h.steps[i] - step) < Math.abs(h.steps[best] - step)) best = i
    return Math.abs(h.steps[best] - step) <= 1000 ? h.losses[best] : null
  }
  const rate = (r: CkptRow) => (r.real && r.real.n ? r.real.success / r.real.n : null)

  const bestOffline = useMemo(() => {
    let b: CkptRow | null = null
    for (const r of rows) {
      const o = offlineOf(r)
      if (o?.joint_deg == null) continue
      const bo = b ? offlineOf(b) : null
      if (!b || (bo?.joint_deg ?? Infinity) > o.joint_deg) b = r
    }
    return b
  }, [rows, offlineOf])
  const bestReal = useMemo(() => {
    let b: CkptRow | null = null
    for (const r of rows) {
      if (!r.real || r.real.n < 3) continue
      if (!b || (rate(r) ?? 0) > (rate(b) ?? 0)) b = r
    }
    return b
  }, [rows])

  const request = (r: CkptRow): JobRequest | null =>
    detail?.dataset && pick.length ? { checkpoint: r.id, dataset: detail.dataset, episodes: pick, stride: sample.stride } : null
  const missing = rows.filter((r) => !r.offline.some(sameSample))
  const running = job.state === 'running'

  const { data, layout } = useMemo(() => {
    const stepped = rows.filter((r) => r.step != null)
    const xs = stepped.map((r) => r.step as number)
    const traces: Data[] = []
    const h = detail?.history
    // 손실 곡선이 없는 학습이면 그 칸을 아예 안 그린다 — 빈 칸이 그래프의 3분의 1 을 먹는다
    const hasLoss = !!(h && h.steps.length)
    const [yOff, yReal] = hasLoss ? ['y2', 'y3'] : ['y', 'y2']
    if (h && hasLoss) {
      traces.push({ type: 'scatter', mode: 'lines', x: h.steps, y: h.losses, xaxis: 'x', yaxis: 'y',
        line: { color: '#a3a3a3', width: 1.5 }, name: t('학습 손실'), hovertemplate: 'step %{x}<br>loss %{y:.4f}<extra></extra>' } as Data)
    }
    traces.push({ type: 'scatter', mode: 'lines+markers', x: xs, y: stepped.map((r) => offlineOf(r)?.joint_deg ?? null),
      xaxis: 'x', yaxis: yOff, connectgaps: true, line: { color: '#3987e5', width: 2 }, marker: { size: 8 },
      name: t('오프라인 관절 오차'), hovertemplate: `step %{x}<br>%{y:.2f}°<extra></extra>` } as Data)
    traces.push({ type: 'scatter', mode: 'text+markers', x: xs, y: stepped.map((r) => { const v = rate(r); return v == null ? null : v * 100 }),
      text: stepped.map((r) => (r.real ? `${r.real.success}/${r.real.n}` : '')), textposition: 'top center',
      xaxis: 'x', yaxis: yReal, marker: { size: 10, color: '#199e70' }, textfont: { color: '#a3a3a3', size: 10 },
      name: t('실기 성공'), hovertemplate: `step %{x}<br>%{y:.0f}%<extra></extra>` } as Data)
    const axis = { gridcolor: '#2a2a2a', zerolinecolor: '#444', color: '#888' }
    return {
      data: traces,
      layout: {
        height: CHART_H, margin: { l: 56, r: 12, t: 8, b: 32 }, paper_bgcolor: BG, plot_bgcolor: BG,
        font: { color: '#999', size: 10 }, showlegend: false, hovermode: 'closest',
        grid: { rows: hasLoss ? 3 : 2, columns: 1, pattern: 'coupled', roworder: 'top to bottom', ygap: 0.25 },
        xaxis: { ...axis, title: { text: 'step' } },
        ...(hasLoss ? { yaxis: { ...axis, title: { text: t('손실') }, rangemode: 'tozero' } } : {}),
        [hasLoss ? 'yaxis2' : 'yaxis']: { ...axis, title: { text: t('오차 (°)') }, rangemode: 'tozero' },
        [hasLoss ? 'yaxis3' : 'yaxis2']: { ...axis, title: { text: t('실기 %') }, range: [0, 110] },
        uirevision: run,
      } as Partial<Layout>,
    }
  }, [rows, detail, offlineOf, run, t])

  const runInfo = runs.find((r) => r.run === run)
  const input = 'w-14 rounded bg-neutral-900 border border-neutral-700 px-1 py-0.5 text-right'

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
        <select value={run} onChange={(e) => setRun(e.target.value)}
                className="min-w-[22rem] rounded bg-neutral-800 border border-neutral-700 px-2 py-1">
          {runs.map((r) => (
            <option key={r.run} value={r.run}>{r.name ? `${r.name} · ` : ''}{r.run} · {r.policy_type} · {r.dataset ?? '?'} ({r.checkpoints})</option>
          ))}
        </select>
        {runInfo && detail && (
          <span className="text-xs text-neutral-500">
            {t('학습 데이터 {dataset}', { dataset: detail.dataset ?? '?' })}
            {detail.dataset_episodes ? t(' · 에피소드 {n}개만', { n: detail.dataset_episodes.length }) : t(' · 전체 에피소드')}
          </span>
        )}
        <span className="ml-auto flex items-center gap-2 text-xs text-neutral-400"
              title={t('체크포인트끼리 견주려면 같은 에피소드여야 한다 — 학습 데이터에서 고르게 뽑는다')}>
          {t('표본')}
          <input type="number" min={1} max={200} value={sample.n} className={input}
                 onChange={(e) => setSample({ ...sample, n: Math.max(1, Number(e.target.value) || 1) })} />
          {t('에피소드 ·')}
          <input type="number" min={1} max={30} value={sample.stride} className={input}
                 onChange={(e) => setSample({ ...sample, stride: Math.max(1, Number(e.target.value) || 1) })} />
          {t('프레임 간격')}
        </span>
      </div>

      {dsMissing && (
        <div className="rounded border border-amber-500/30 bg-neutral-800 px-3 py-2 text-xs text-amber-400">
          {t('학습 데이터셋 {dataset} 이 이 기계에 없어 오프라인 평가를 못 한다 — 오프라인 평가 탭에서 다른 데이터셋을 고를 수 있다', { dataset: detail?.dataset ?? '?' })}
        </div>
      )}

      {running && job.job && (
        <div className="flex items-center gap-3 rounded border border-neutral-700 bg-neutral-800 px-3 py-2 text-xs">
          <span className="text-neutral-300">{t('평가 중')} {job.job.checkpoint}</span>
          <div className="h-1.5 flex-1 rounded bg-neutral-700 overflow-hidden">
            <div className="h-full bg-blue-500" style={{ width: `${job.progress ? (100 * job.progress.done) / Math.max(1, job.progress.total) : 0}%` }} />
          </div>
          <span className="tabular-nums text-neutral-400">{job.progress ? `${job.progress.done}/${job.progress.total}` : '…'}</span>
          <button onClick={onStop} className="rounded border border-neutral-600 px-2 py-0.5 text-neutral-300 hover:bg-neutral-700">{t('멈춤')}</button>
        </div>
      )}

      <div className="rounded-lg border border-neutral-700 bg-neutral-900 px-1 pt-1">
        <Plot data={data} layout={layout} config={{ displaylogo: false, responsive: true, displayModeBar: false }}
              style={{ width: '100%', height: CHART_H }} useResizeHandler />
        {!detail?.history && (
          <div className="px-3 pb-2 text-xs text-neutral-500">
            {t('이 학습은 손실 곡선이 남아 있지 않다 — 곡선은 이번 버전부터 학습 폴더에 저장된다')}
          </div>
        )}
      </div>

      {bestOffline && bestReal && bestOffline.id !== bestReal.id && (
        <div className="rounded border border-amber-500/30 bg-neutral-800 px-3 py-2 text-xs text-amber-400">
          {t('⚠ 오프라인 최고({a})와 실기 최고({b})가 다르다 — 오프라인 오차는 진단이고 판정은 실기다', {
            a: stepLabel(bestOffline), b: stepLabel(bestReal),
          })}
        </div>
      )}

      <div className="rounded-lg border border-neutral-700 bg-neutral-800 overflow-x-auto">
        <table className="w-full text-xs tabular-nums whitespace-nowrap [&_td]:px-3 [&_th]:px-3">
          <thead className="text-neutral-500">
            <tr className="text-left">
              <th className="font-normal py-1.5">{t('체크포인트')}</th>
              <th className="font-normal text-right">{t('손실')}</th>
              <th className="font-normal text-right" title={t('예측 첫 동작과 녹화 행동의 관절 평균 오차')}>{t('관절 오차')}</th>
              <th className="font-normal text-right" title={t('청크 전체(앞으로의 계획)와 녹화의 관절 평균 오차')}>{t('청크 오차')}</th>
              <th className="font-normal text-right" title={t('녹화에서 그리퍼가 닫히는 순간과 예측이 닫는 순간의 평균 차이 · 놓친 닫힘')}>{t('그리퍼 시점')}</th>
              <th className="font-normal text-right">{t('실기 성공')}</th>
              <th className="font-normal text-right">
                {missing.length > 0 && !dsMissing && (
                  <button
                    disabled={running || !pick.length}
                    onClick={() => onEnqueue(missing.map(request).filter((x): x is JobRequest => x != null))}
                    className="rounded bg-blue-600 px-2 py-0.5 text-white hover:bg-blue-500 disabled:opacity-40"
                  >
                    {t('빠진 {n}개 모두 평가', { n: missing.length })}
                  </button>
                )}
              </th>
            </tr>
          </thead>
          <tbody className="text-neutral-300">
            {rows.map((r) => {
              const o = offlineOf(r)
              const loss = lossAt(r.step)
              const req = request(r)
              return (
                <tr key={r.id} className="border-t border-neutral-700/60">
                  <td className="py-1.5">
                    <span className="font-mono">{stepLabel(r)}</span>
                    {bestOffline?.id === r.id && <span className="ml-2 text-blue-400">{t('오프라인 최고')}</span>}
                    {bestReal?.id === r.id && <span className="ml-2 text-emerald-400">{t('실기 최고')}</span>}
                  </td>
                  <td className="text-right">{loss == null ? '—' : loss.toFixed(4)}</td>
                  <td className="text-right">{o?.joint_deg == null ? '—' : `${o.joint_deg.toFixed(2)}°`}
                    {o && !sameSample(o) && <span className="text-neutral-500" title={t('지금 표본과 다른 설정으로 잰 값')}> *</span>}
                  </td>
                  <td className="text-right">{o?.chunk_joint_deg == null ? '—' : `${o.chunk_joint_deg.toFixed(2)}°`}</td>
                  <td className="text-right">
                    {o?.grip_mean_abs_s == null ? '—' : `±${o.grip_mean_abs_s.toFixed(2)}s`}
                    {o?.grip_missed ? <span className="text-amber-400"> {t('놓침 {n}', { n: o.grip_missed })}</span> : null}
                  </td>
                  <td className="text-right">
                    {r.real ? `${r.real.success}/${r.real.n} (${Math.round((100 * r.real.success) / r.real.n)}%)` : '—'}
                  </td>
                  <td className="text-right space-x-2">
                    {!r.offline.some(sameSample) && req && (
                      <button disabled={running} onClick={() => onStart(req)}
                              className="rounded border border-neutral-600 px-2 py-0.5 hover:bg-neutral-700 disabled:opacity-40">
                        {t('평가')}
                      </button>
                    )}
                    <button onClick={() => onOpen(r.id, o?.key ?? null)} className="text-neutral-400 hover:text-white">
                      {t('자세히 →')}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="text-xs text-neutral-500">
        {t('오프라인 오차는 녹화된 정답 상태에서 매번 다시 시작하는 open-loop 값이다 — 정책이 스스로 실수를 바로잡는 실기와 다르다. 진단에 쓰고, 고르는 것은 실기로')}
      </div>
    </div>
  )
}
