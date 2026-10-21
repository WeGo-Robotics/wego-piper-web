/**
 * POS분석의 커버리지 — 파지 자리·방향이 **얼마나 퍼져 있나** (feature/episode-analyzer.md §6 A1).
 *
 * 무작위로 놓았다고 믿은 시연이 파지 순간으로 보면 몇 덩어리에 몰려 있는 일이 있다.
 * 그러면 정책은 자리를 외우는 것만으로 손실이 낮게 나오고, 자리가 바뀌면 못 집는다.
 * 여기서 세 가지를 잰다 — 모두 지금 보이는(필터 적용된) 파지 기준, **팔마다 따로**:
 *
 * - 작업면 칸: 위에서 본 말단 위치를 정사각 격자로 나눠 채운 칸 수. 격자는 같은 팔의
 *   보이는 파지 전체를 덮는 범위라 데이터셋끼리 같은 자로 견준다.
 * - 자리 덩어리: 가까운 점끼리 이은 묶음 중 **작게 뭉친 것**(아래 LUMP_*). 대부분이
 *   덩어리 몇 개에 들어가면 경고한다.
 * - 방향 구간: 손가락 여닫는 축의 수평 방향(0~180°)을 5° 로 나눠 채운 구간 수.
 */
import { useMemo, useState } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import type { Data, Layout } from 'plotly.js'
import { useT } from '../i18n'

const Plot = createPlotlyComponent(Plotly)

export type CoverageEvent = {
  ds: string
  arm: string | null
  tip: number[]
  /** 손가락 여닫는 축의 수평 방향 (도, 0~180). 축이 거의 수직이면 null */
  yaw: number | null
}

// 덩어리 = 이웃 거리 LINK 안으로 이어지는 점들 중, 크기가 충분하고 반경이 작은 것.
// ⚠ **반경 조건이 없으면 촘촘한 데이터가 덩어리 하나로 읽힌다.** 고르게 퍼져 있어도
//   점이 많으면 이웃 간격이 2cm 아래로 내려가 전부 한 사슬로 이어진다.
// 실측(2026-10-21): 2cm 에서 min_cube 가 큐브 자리 3곳, singlearm_apple_4point 가 4곳,
// bolt_two1 이 10곳으로 갈렸고 two_arm_bolt_2 는 덩어리가 없었다. 3cm 면 3cm 간격으로
// 놓인 min_cube 의 큐브 셋이 하나로 붙는다.
const LUMP_LINK_MM = 20
const LUMP_MAX_RADIUS_MM = 25
// 덩어리 몇 개에 이만큼 이상 들어가면 "몰려 있다"
const LUMP_WARN_SHARE = 0.8
const MIN_FOR_WARN = 10
const YAW_BIN = 5
const CELL_CHOICES = [20, 30, 50]
const BG = '#171717'

/** 이웃 거리 link 로 이은 묶음(합집합-찾기) 중 덩어리 조건을 만족하는 것들의 크기. */
function lumps(points: number[][]): number[] {
  const n = points.length
  const parent = points.map((_, i) => i)
  const find = (i: number): number => {
    while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i] }
    return i
  }
  const link2 = LUMP_LINK_MM ** 2
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const d = (points[i][0] - points[j][0]) ** 2 + (points[i][1] - points[j][1]) ** 2
        + (points[i][2] - points[j][2]) ** 2
      if (d < link2) parent[find(i)] = find(j)
    }
  }
  const groups = new Map<number, number[][]>()
  points.forEach((p, i) => {
    const r = find(i)
    if (!groups.has(r)) groups.set(r, [])
    groups.get(r)!.push(p)
  })
  const minSize = Math.max(3, Math.ceil(0.05 * n))
  const out: number[] = []
  for (const g of groups.values()) {
    if (g.length < minSize) continue
    const c = [0, 1, 2].map((k) => g.reduce((a, p) => a + p[k], 0) / g.length)
    const rms = Math.sqrt(g.reduce((a, p) => a + (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 + (p[2] - c[2]) ** 2, 0) / g.length)
    if (rms <= LUMP_MAX_RADIUS_MM) out.push(g.length)
  }
  return out.sort((a, b) => b - a)
}

const armOf = (e: CoverageEvent) => e.arm ?? 'single'
const std = (xs: number[]) => {
  const m = xs.reduce((a, b) => a + b, 0) / xs.length
  return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / xs.length)
}

type Grid = { x0: number; y0: number; cols: number; rows: number; cell: number }

function gridFor(evs: CoverageEvent[], cell: number): Grid {
  const xs = evs.map((e) => e.tip[0])
  const ys = evs.map((e) => e.tip[1])
  const x0 = Math.floor(Math.min(...xs) / cell) * cell
  const y0 = Math.floor(Math.min(...ys) / cell) * cell
  return {
    x0, y0, cell,
    cols: Math.max(1, Math.ceil((Math.max(...xs) - x0) / cell + 1e-9)),
    rows: Math.max(1, Math.ceil((Math.max(...ys) - y0) / cell + 1e-9)),
  }
}

function measure(evs: CoverageEvent[], grid: Grid) {
  const cells = new Set(evs.map((e) => {
    const ix = Math.min(grid.cols - 1, Math.floor((e.tip[0] - grid.x0) / grid.cell))
    const iy = Math.min(grid.rows - 1, Math.floor((e.tip[1] - grid.y0) / grid.cell))
    return `${ix},${iy}`
  }))
  const yaws = evs.map((e) => e.yaw).filter((y): y is number => y != null)
  const lump = lumps(evs.map((e) => e.tip))
  return {
    n: evs.length,
    sx: std(evs.map((e) => e.tip[0])),
    sy: std(evs.map((e) => e.tip[1])),
    cells: cells.size,
    lumpCount: lump.length,
    lumpShare: lump.reduce((a, b) => a + b, 0) / evs.length,
    yawBins: new Set(yaws.map((y) => Math.min(180 / YAW_BIN - 1, Math.floor(y / YAW_BIN)))).size,
    yawNone: evs.length - yaws.length,
  }
}

export default function PosCoverage({ events, arms, armName, dsColor, dsName }: {
  events: CoverageEvent[]
  /** 패널 순서와 같은 팔 목록 ('single' | 'left' | 'right' …) */
  arms: string[]
  armName: (arm: string) => string
  dsColor: (ds: string) => string
  dsName: (ds: string) => string
}) {
  const t = useT()
  const [cell, setCell] = useState(() => {
    const v = Number(localStorage.getItem('pos-cell-mm'))
    return CELL_CHOICES.includes(v) ? v : 30
  })
  const pickCell = (v: number) => {
    setCell(v)
    try { localStorage.setItem('pos-cell-mm', String(v)) } catch { /* 사생활 모드 */ }
  }
  const split = arms.length > 1

  const rows = useMemo(() => arms.flatMap((a) => {
    const evs = events.filter((e) => armOf(e) === a)
    if (evs.length === 0) return []
    const grid = gridFor(evs, cell)
    const dss = [...new Set(evs.map((e) => e.ds))]
    const per = dss.map((ds) => ({ key: `${a}|${ds}`, arm: a, ds, grid, m: measure(evs.filter((e) => e.ds === ds), grid) }))
    // 여럿을 함께 학습할 거면 **합친 것**의 커버리지가 실제로 정책이 보는 것이다
    return dss.length > 1
      ? [...per, { key: `${a}|*`, arm: a, ds: null as string | null, grid, m: measure(evs, grid) }]
      : per
  }), [events, arms, cell])

  // ⚠ **그래프 div 에 높이를 직접 준다** — 레이아웃 height 만으로는 창 크기가 한 번 바뀌면 무너진다.
  //   `responsive` 가 리사이즈 때 레이아웃 height 를 지우고 안쪽 상자를 부모의 100% 로 바꾸는데,
  //   높이 없는 div 의 100% 는 0 이다. 그러면 카드는 표만 남고 그림은 아래 카드 위로 넘쳐
  //   그려졌다(실측: 480px → 0px). 에피소드 뷰어 그래프가 자리를 미리 잡는 것과 같은 병이다.
  const height = 40 + 220 * arms.length
  const { data, layout } = useMemo(() => {
    const traces: Data[] = []
    const l: Record<string, unknown> = {
      height,
      margin: { l: 48, r: 12, t: 28, b: 36 },
      paper_bgcolor: BG, plot_bgcolor: BG,
      font: { color: '#999', size: 10 },
      showlegend: false,
      barmode: 'stack',
      bargap: 0.05,
      grid: { rows: arms.length, columns: 2, pattern: 'independent', xgap: 0.12, ygap: 0.25 },
      annotations: [] as unknown[],
      uirevision: 'pos-coverage',
    }
    arms.forEach((a, i) => {
      const evs = events.filter((e) => armOf(e) === a)
      if (evs.length === 0) return
      const g = gridFor(evs, cell)
      const hx = i === 0 ? '' : String(2 * i + 1)
      const bx = String(2 * i + 2)
      traces.push({
        type: 'histogram2d', xaxis: `x${hx}`, yaxis: `y${hx}`,
        x: evs.map((e) => e.tip[0]), y: evs.map((e) => e.tip[1]),
        xbins: { start: g.x0, end: g.x0 + g.cols * cell, size: cell },
        ybins: { start: g.y0, end: g.y0 + g.rows * cell, size: cell },
        // 빈 칸은 바탕 그대로 — 어디가 비었는지가 이 그림의 요점이다
        colorscale: [[0, 'rgba(0,0,0,0)'], [0.0001, '#1d4f91'], [1, '#a9cdf6']],
        zmin: 0, showscale: false,
        hovertemplate: `x %{x} · y %{y} mm<br>${t('파지 {n}', { n: '%{z}' })}<extra></extra>`,
      } as unknown as Data)
      // 눈금 숫자는 100mm 마다, 칸 경계는 보조 격자선으로 — 칸마다 숫자를 달면 2cm 에서 읽을 수 없다
      const cellGrid = (start: number) => ({ tick0: start, dtick: cell, showgrid: true, gridcolor: '#333' })
      l[`xaxis${hx}`] = {
        range: [g.x0, g.x0 + g.cols * cell], tick0: 0, dtick: 100, showgrid: false, minor: cellGrid(g.x0),
        // ⚠ 1:1 을 맞출 때 x 도 **칸을 줄여서** 맞춘다. 기본(range)이면 x 가 범위를 늘려
        //   데이터 밖 빈 땅이 그림의 반을 차지한다 — 빈 칸을 보는 그림에서 가짜 빈 칸이다.
        constrain: 'domain',
        zeroline: false, color: '#888',
        title: { text: t('x (mm, 칸 {cell})', { cell }), standoff: 4 },
      }
      l[`yaxis${hx}`] = {
        range: [g.y0, g.y0 + g.rows * cell], tick0: 0, dtick: 100, showgrid: false, minor: cellGrid(g.y0),
        zeroline: false, color: '#888',
        // 위에서 본 그림이니 x·y 비율을 1:1 로 — 칸이 정사각이어야 빈 곳 크기를 눈으로 잰다
        scaleanchor: `x${hx}`, constrain: 'domain', title: { text: 'y (mm)', standoff: 4 },
      }
      const centers = Array.from({ length: 180 / YAW_BIN }, (_, k) => k * YAW_BIN + YAW_BIN / 2)
      for (const ds of [...new Set(evs.map((e) => e.ds))]) {
        const counts = centers.map(() => 0)
        for (const e of evs) {
          if (e.ds !== ds || e.yaw == null) continue
          counts[Math.min(counts.length - 1, Math.floor(e.yaw / YAW_BIN))]++
        }
        traces.push({
          type: 'bar', xaxis: `x${bx}`, yaxis: `y${bx}`, x: centers, y: counts, width: YAW_BIN * 0.9,
          marker: { color: dsColor(ds) }, name: dsName(ds),
          hovertemplate: `${dsName(ds)}<br>%{x}° ± ${YAW_BIN / 2}: %{y}<extra></extra>`,
        } as Data)
      }
      l[`xaxis${bx}`] = {
        range: [0, 180], dtick: 30, color: '#888', gridcolor: '#2a2a2a', zeroline: false,
        title: { text: t('방향 (°) — 손가락 여닫는 축, 0°≡180°'), standoff: 4 },
      }
      l[`yaxis${bx}`] = { color: '#888', gridcolor: '#2a2a2a', zeroline: false, rangemode: 'tozero' }
      if (split) {
        ;(l.annotations as unknown[]).push({
          text: armName(a), showarrow: false, xref: 'paper', yref: `y${hx} domain`,
          x: 0, y: 1, xanchor: 'left', yanchor: 'bottom', font: { size: 12, color: '#d4d4d4' },
        })
      }
    })
    return { data: traces, layout: l as Partial<Layout> }
  }, [events, arms, cell, split, armName, dsColor, dsName, t, height])

  if (events.length === 0) return null
  const seg = (on: boolean) =>
    `px-2 py-0.5 rounded text-xs transition-colors ${on ? 'bg-neutral-700 text-white' : 'text-neutral-500 hover:text-white'}`

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-900 px-3 pt-2 pb-1 space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="text-neutral-400">{t('커버리지 — 파지 자리와 방향이 얼마나 퍼져 있나')}</span>
        <span className="ml-auto text-neutral-500">{t('칸 크기')}</span>
        <div className="flex gap-0.5 rounded bg-neutral-800 p-0.5">
          {CELL_CHOICES.map((c) => (
            <button key={c} className={seg(cell === c)} onClick={() => pickCell(c)}>{c / 10}cm</button>
          ))}
        </div>
      </div>

      {/* 좁은 창에서 칸이 줄바꿈되면 이름과 팔이 붙어 읽힌다 — 줄바꿈 대신 가로 스크롤 */}
      <div className="overflow-x-auto">
      <table className="w-full text-xs tabular-nums whitespace-nowrap [&_td]:px-2 [&_th]:px-2">
        <thead className="text-neutral-500">
          <tr className="text-left">
            <th className="font-normal py-0.5">{t('데이터셋')}</th>
            {split && <th className="font-normal">{t('팔')}</th>}
            <th className="font-normal text-right">{t('파지')}</th>
            <th className="font-normal text-right" title={t('말단 위치의 표준편차')}>σ x · y (mm)</th>
            <th className="font-normal text-right" title={t('위에서 본 작업면을 정사각 칸으로 나눠 파지가 하나라도 있는 칸 수 / 같은 팔 파지 전체를 덮는 칸 수')}>{t('작업면 칸')}</th>
            <th className="font-normal text-right" title={t('2cm 안으로 이어지고 반경 2.5cm 안에 뭉친 묶음 — 그 수와 거기 들어간 파지 비율')}>{t('자리 덩어리')}</th>
            <th className="font-normal text-right" title={t('손가락 여닫는 축의 수평 방향을 5° 로 나눠 채운 구간 수 / 36')}>{t('방향 5° 구간')}</th>
            <th />
          </tr>
        </thead>
        <tbody className="text-neutral-300">
          {rows.map(({ key, arm, ds, grid, m }) => {
            const warn = m.n >= MIN_FOR_WARN && m.lumpCount > 0 && m.lumpShare >= LUMP_WARN_SHARE
            return (
              <tr key={key} className={`border-t border-neutral-800 ${ds == null ? 'text-neutral-100' : ''}`}>
                <td className="py-0.5">
                  {ds == null
                    ? <span className="text-neutral-400">{t('합친 것')}</span>
                    : (
                      <span className="flex items-center gap-1.5" title={ds}>
                        <span className="h-2 w-2 rounded-full shrink-0" style={{ background: dsColor(ds) }} />
                        <span>{dsName(ds)}</span>
                      </span>
                    )}
                </td>
                {split && <td className="text-neutral-400">{armName(arm)}</td>}
                <td className="text-right">{m.n}</td>
                <td className="text-right">{m.sx.toFixed(0)} · {m.sy.toFixed(0)}</td>
                <td className="text-right">{m.cells} / {grid.cols * grid.rows}</td>
                <td className="text-right">
                  {m.lumpCount === 0 ? '—' : t('{k}곳 · {pct}%', { k: m.lumpCount, pct: Math.round(m.lumpShare * 100) })}
                </td>
                <td className="text-right">
                  {m.yawBins} / 36
                  {m.yawNone > 0 && (
                    <span className="text-neutral-500" title={t('여닫는 축이 거의 수직이라 방향을 안 매긴 파지')}>
                      {' '}({t('수직 {n}', { n: m.yawNone })})
                    </span>
                  )}
                </td>
                <td className="pl-2 text-amber-400 whitespace-normal min-w-[16rem]">
                  {warn && t('⚠ 자리 {k}곳에 {pct}% 몰림 — 자리를 외워도 손실이 낮게 나온다', {
                    k: m.lumpCount, pct: Math.round(m.lumpShare * 100),
                  })}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      </div>

      <Plot
        data={data}
        layout={layout}
        config={{ displaylogo: false, responsive: true, displayModeBar: false }}
        style={{ width: '100%', height }}
        useResizeHandler
      />
    </div>
  )
}
