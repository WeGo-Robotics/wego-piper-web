/**
 * POS분석 — 파지 순간의 자세를 여러 데이터셋에 걸쳐 3D 로 본다 (feature/episode-analyzer.md).
 *
 * 에피소드 화면의 탭이다. 뷰어가 "한 에피소드를 자세히" 라면 여기는 "전부를 모아서" 다.
 * 점 하나 = 파지 한 번(GRASP 구간의 시작 프레임 — 정의는 `piper_phase.grasps`).
 * 점을 고르면 그 순간의 팔이 스틱으로 그려지고, [뷰어에서 열기] 로 그 프레임에 간다.
 *
 * 3D 는 이미 있는 Plotly 의 scatter3d 다 — 스틱은 FK 마디를 잇는 선이라 메시가 필요 없다.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import type { Data, Layout, PlotMouseEvent } from 'plotly.js'
import { api } from '../services/api'
import type { Dataset } from '../types/models'
import { msg, useT } from '../i18n'
import { useSystemMessage } from './SystemMessages'
import PosCoverage from './PosCoverage'
import PosApproach from './PosApproach'

const Plot = createPlotlyComponent(Plotly)

type GraspEvent = {
  episode: number
  cycle: number
  frame: number
  t: number
  outcome: 'hold' | 'empty'
  /** 양팔이면 'left' / 'right', 단일 팔이면 null */
  arm: string | null
  /** sidecar = 페이즈 라벨(사람이 고친 것 포함) · fsm = 양팔의 나머지 팔을 같은 규칙으로 즉석 분석 */
  source: 'sidecar' | 'fsm'
  reviewed: boolean
  /** J1~J6 (도) */
  joints: number[]
  /** 닫기를 마친 뒤의 그리퍼 — 물었으면 물체 폭, 빈손이면 0 근처 */
  gripper: number
  /** 그리퍼 손가락 뿌리 가운데, 팔 base 기준 mm */
  tip: number[]
  /** 손가락 여닫는 축의 수평 방향 (도, 0~180). 축이 거의 수직이면 null */
  yaw: number | null
  /** 스틱 마디 6개 (base → … → tip), mm */
  stick: number[][]
  /** 닫기 직전 0.5초의 말단 최고 속도 (mm/s) */
  approach_speed: number | null
  /** 닫는 동안(GRASP 구간)의 말단 최고 속도 (mm/s) — 튀어나가며 집는 버릇이 여기서 보인다 */
  closing_speed: number | null
  /** 닫기 0.5초 전부터 닫기가 끝날 때까지의 말단 경로 (mm) */
  trail: number[][]
  /** trail 안에서 파지 순간(닫기 시작)의 위치 */
  trail_at: number
}
type GraspsResponse =
  | { labeled: true; fps: number; episodes: number; reviewed: number; stick: string[]; events: GraspEvent[] }
  | { labeled: false; events: [] }
type Load =
  | { state: 'loading' }
  | { state: 'ok'; res: Extract<GraspsResponse, { labeled: true }> }
  | { state: 'none' }
  | { state: 'error'; error: string }
type Ev = GraspEvent & { ds: string; slot: number; key: string; order: number }
type ColorBy = 'dataset' | 'cycle' | 'outcome' | 'order'
type Pick = { id: string; slot: number }

// ── 색 ──
// 범주 8색은 어두운 면(#171717) 기준으로 검증한 고정 순서다 — **데이터셋을 고른 순서대로
// 칸을 받고, 빼도 남은 것의 색은 안 바뀐다**(색은 데이터셋을 따라가지 순위를 따라가지 않는다).
// 산점도에서는 앞 세 칸까지만 모든 쌍이 구분되므로, 넷째부터는 범례·툴팁이 같이 일한다.
const SERIES = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']
const OUTCOME_COLOR = { hold: '#3987e5', empty: '#e66767' } as const
const ORDER_SCALE: [number, string][] = [[0, '#1d4f91'], [1, '#a9cdf6']]
// 스틱 마디 — 이웃한 마디끼리 구분되게 검증한 순서. 색만으로 가르지 않게 관절 이름도 찍는다.
const STICK_COLORS = ['#c98500', '#d55181', '#9085e9', '#199e70', '#e66767']
const STICK_PARTS = [msg('베이스'), msg('상완'), msg('전완'), msg('손목'), msg('그리퍼')]
// 그리퍼는 닫기를 마친 뒤의 값이다 — 물었으면 물체 폭, 빈손이면 0 근처 (piper_phase.grasps)
// 접근·닫는 중 속도는 파지 전후의 말단 최고 속도다 (§6 A2, piper_phase.grasps "접근 동작").
type Channel = { label: string; unit: string; get: (e: GraspEvent) => number | null }
const CHANNELS: Channel[] = [
  ...[0, 1, 2, 3, 4, 5].map((i) => ({ label: `J${i + 1}`, unit: '°', get: (e: GraspEvent) => e.joints[i] })),
  { label: msg('그리퍼(닫은 뒤)'), unit: '', get: (e) => e.gripper },
  { label: msg('접근 속도'), unit: 'mm/s', get: (e) => e.approach_speed },
  { label: msg('닫는 중 속도'), unit: 'mm/s', get: (e) => e.closing_speed },
]
const nums = (xs: (number | null)[]) => xs.filter((x): x is number => x != null)
// 팔 패널 순서. 그 밖의 이름(arm2 …)은 뒤에 붙는다.
const ARM_ORDER = ['single', 'left', 'right']

const BG = '#171717'
const STORE_KEY = 'pos-datasets'

function loadPicks(): Pick[] {
  try {
    const v = JSON.parse(localStorage.getItem(STORE_KEY) ?? '[]')
    return Array.isArray(v) ? v.filter((p) => typeof p?.id === 'string' && Number.isInteger(p?.slot)) : []
  } catch { return [] }
}

const armOf = (e: GraspEvent) => e.arm ?? 'single'
/** 팔 패널 i 의 Plotly 장면 이름 */
const sceneId = (i: number) => (i === 0 ? 'scene' : `scene${i + 1}`)
const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length
const std = (xs: number[]) => {
  const m = mean(xs)
  return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / xs.length)
}
const short = (id: string) => id.slice(id.lastIndexOf('/') + 1)
/** 띠 그래프의 가로 흩뿌림. 키에서 정해야 다시 그려도 점이 안 튄다. */
const jitter = (key: string) => {
  let h = 0
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) | 0
  return ((h >>> 0) % 1000) / 1000 * 0.6 - 0.3
}

export default function PosAnalysis({ datasets, onOpen }: {
  datasets: Dataset[]
  onOpen: (dataset: string, episode: number, frame: number) => void
}) {
  const t = useT()
  const { notify } = useSystemMessage()

  const [picks, setPicks] = useState<Pick[]>(loadPicks)
  const [loads, setLoads] = useState<Record<string, Load>>({})
  const [analyzing, setAnalyzing] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [colorByUser, setColorByUser] = useState<ColorBy | null>(null)
  const [showHold, setShowHold] = useState(true)
  const [showEmpty, setShowEmpty] = useState(true)
  const [reviewedOnly, setReviewedOnly] = useState(false)
  const [hiddenArms, setHiddenArms] = useState<string[]>([])
  const [hiddenCycles, setHiddenCycles] = useState<number[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  // 모든 파지의 접근 궤적 — 많으면 그림이 덮이므로 기본은 고른 것만
  const [showTrails, setShowTrails] = useState(false)

  useEffect(() => {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(picks)) } catch { /* 사생활 모드 */ }
  }, [picks])

  // ⚠ 구운 사본(`_stage`)은 원본과 **같은 프레임**이다. 둘 다 고르면 점이 그대로 두 벌
  //   겹쳐 분포가 두 배로 진해진다 — 목록에서 뺀다.
  const choices = useMemo(() => datasets.filter((d) => !d.baked), [datasets])
  const shown = useMemo(
    () => choices.filter((d) => d.id.toLowerCase().includes(query.trim().toLowerCase())),
    [choices, query],
  )

  // 지워진 데이터셋을 기억에서 뺀다 (목록을 받은 뒤에만 — 빈 목록이면 전부 날아간다)
  useEffect(() => {
    if (choices.length === 0) return
    setPicks((ps) => {
      const keep = ps.filter((p) => choices.some((d) => d.id === p.id))
      return keep.length === ps.length ? ps : keep
    })
  }, [choices])

  const load = useCallback((id: string) => {
    setLoads((m) => ({ ...m, [id]: { state: 'loading' } }))
    api.get<GraspsResponse>(`/phase/${id}/grasps`, { timeoutMs: 120_000 })
      .then((r) => setLoads((m) => ({ ...m, [id]: r.labeled ? { state: 'ok', res: r } : { state: 'none' } })))
      .catch((e) => setLoads((m) => ({
        ...m, [id]: { state: 'error', error: e instanceof Error ? e.message : String(e) },
      })))
  }, [])

  useEffect(() => {
    for (const p of picks) if (!loads[p.id]) load(p.id)
  }, [picks, loads, load])

  const toggle = (id: string) => setPicks((ps) => {
    if (ps.some((p) => p.id === id)) return ps.filter((p) => p.id !== id)
    // 빈 칸 중 가장 앞 — 뺐다 다시 넣어도 다른 데이터셋의 색을 빼앗지 않는다
    let slot = 0
    while (ps.some((p) => p.slot === slot)) slot++
    return [...ps, { id, slot }]
  })

  const analyze = async (id: string) => {
    setAnalyzing((a) => [...a, id])
    try {
      await api.post(`/phase/${id}/analyze`, {}, { timeoutMs: 600_000 })
      load(id)
    } catch (e) {
      notify({ level: 'error', text: e instanceof Error ? e.message : t('분석 실패'), source: t('POS분석') })
    } finally {
      setAnalyzing((a) => a.filter((x) => x !== id))
    }
  }

  // ── 파생 ──

  const all = useMemo<Ev[]>(() => picks.flatMap((p) => {
    const l = loads[p.id]
    if (l?.state !== 'ok') return []
    const last = Math.max(1, ...l.res.events.map((e) => e.episode))
    return l.res.events.map((e) => ({
      // 팔이 키에 들어가야 한다 — 양팔은 같은 에피소드·같은 번째 파지가 팔마다 하나씩 있다
      ...e, ds: p.id, slot: p.slot, key: `${p.id}|${e.episode}|${e.arm ?? 'single'}|${e.cycle}`,
      order: e.episode / last,
    }))
  }), [picks, loads])

  const stickLabels = useMemo(() => {
    for (const p of picks) {
      const l = loads[p.id]
      if (l?.state === 'ok') return l.res.stick
    }
    return ['base', 'J1·J2', 'J3', 'J4·J5', 'J6', 'grip']
  }, [picks, loads])

  const arms = useMemo(() => [...new Set(all.map(armOf))].sort(), [all])
  const cycles = useMemo(() => [...new Set(all.map((e) => e.cycle))].sort((a, b) => a - b), [all])

  const visible = useMemo(() => all.filter((e) =>
    (e.outcome === 'hold' ? showHold : showEmpty)
    && (!reviewedOnly || e.reviewed)
    && !hiddenArms.includes(armOf(e))
    && !hiddenCycles.includes(e.cycle),
  ), [all, showHold, showEmpty, reviewedOnly, hiddenArms, hiddenCycles])

  const loadedCount = picks.filter((p) => loads[p.id]?.state === 'ok').length
  const failed = picks.filter((p) => loads[p.id]?.state === 'error')
  const unlabeled = picks.filter((p) => loads[p.id]?.state === 'none')
  const noGrasps = picks.filter((p) => {
    const l = loads[p.id]
    return l?.state === 'ok' && l.res.events.length === 0
  })
  const errorOf = (id: string) => {
    const l = loads[id]
    return l?.state === 'error' ? l.error : ''
  }
  const colorBy: ColorBy = colorByUser
    ?? (loadedCount > 1 ? 'dataset'
      : all.some((e) => e.outcome === 'empty') ? 'outcome'
      : cycles.length > 1 ? 'cycle' : 'order')

  const armName = useCallback((a: string) =>
    a === 'single' ? t('단일 팔') : a === 'left' ? t('왼팔') : a === 'right' ? t('오른팔') : a, [t])
  const dsColor = useCallback(
    (ds: string) => SERIES[(picks.find((p) => p.id === ds)?.slot ?? 0) % SERIES.length], [picks])

  const groupOf = useCallback((e: Ev): [string, string, string] => {
    if (colorBy === 'dataset') return [String(e.slot).padStart(3, '0'), short(e.ds), SERIES[e.slot % SERIES.length]]
    if (colorBy === 'cycle') {
      return [String(e.cycle).padStart(3, '0'), t('{cycle}번째 파지', { cycle: e.cycle }),
        SERIES[(e.cycle - 1) % SERIES.length]]
    }
    return [e.outcome, e.outcome === 'hold' ? t('물음') : t('빈손'), OUTCOME_COLOR[e.outcome]]
  }, [colorBy, t])

  const hover = useCallback((e: Ev) =>
    `${short(e.ds)} · ep ${e.episode} · ${t('{cycle}번째 파지', { cycle: e.cycle })}`
    + `${e.outcome === 'empty' ? ` · ${t('빈손')}` : ''}${e.arm ? ` · ${armName(e.arm)}` : ''}`
    + `<br>x ${e.tip[0].toFixed(0)} · y ${e.tip[1].toFixed(0)} · z ${e.tip[2].toFixed(0)} mm`,
  [t, armName])

  // ⚠ **팔마다 따로 그리고 따로 잰다.** 좌표가 팔마다 자기 base 기준이고 두 팔 사이
  //   배치는 어디에도 없다. 한 공간에 겹치면 왼팔이 오른쪽까지 뻗은 것처럼 보이고,
  //   J1 분포는 좌우가 거울상이라 섞으면 있지도 않은 두 봉우리가 생긴다.
  const panelArms = useMemo(() => {
    const present = [...new Set(visible.map(armOf))]
    const rank = (a: string) => (ARM_ORDER.includes(a) ? ARM_ORDER.indexOf(a) : ARM_ORDER.length)
    return present.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
  }, [visible])

  const statsByArm = useMemo(() => {
    const out = new Map<string, {
      chMean: number[]; chStd: number[]; tipStd: number[]; medoid: Ev
    }>()
    for (const a of panelArms) {
      const evs = visible.filter((e) => armOf(e) === a)
      const chMean = CHANNELS.map((ch) => { const v = nums(evs.map(ch.get)); return v.length ? mean(v) : 0 })
      const chStd = CHANNELS.map((ch) => { const v = nums(evs.map(ch.get)); return v.length ? std(v) : 0 })
      const tipStd = [0, 1, 2].map((i) => std(evs.map((e) => e.tip[i])))
      // 중앙 자세 = 관절 공간에서 평균에 가장 가까운 **실제** 파지. 평균 관절값으로 FK 를
      // 돌리면 아무도 취한 적 없는 자세가 나올 수 있다 (J1 이 갈라진 두 덩어리의 가운데 등).
      let medoid = evs[0]
      let best = Infinity
      for (const e of evs) {
        let d = 0
        for (let c = 0; c < 6; c++) if (chStd[c] > 0) d += ((e.joints[c] - chMean[c]) / chStd[c]) ** 2
        if (d < best) { best = d; medoid = e }
      }
      out.set(a, { chMean, chStd, tipStd, medoid })
    }
    return out
  }, [visible, panelArms])

  const totals = useMemo(() => ({
    episodes: new Set(visible.map((e) => `${e.ds}|${e.episode}`)).size,
    empty: visible.filter((e) => e.outcome === 'empty').length,
  }), [visible])

  const sel = visible.find((e) => e.key === selected) ?? null
  const selStats = sel ? statsByArm.get(armOf(sel)) ?? null : null
  const split = panelArms.length > 1

  // ── 3D ──

  const sceneData = useMemo<Data[]>(() => {
    const out: Data[] = []
    if (visible.length === 0) return out
    const sym = (e: Ev) => (e.outcome === 'empty' ? 'x' : 'circle')
    const legendShown = new Set<string>()

    panelArms.forEach((a, i) => {
      const scene = sceneId(i)
      const evs = visible.filter((e) => armOf(e) === a)
      const st = statsByArm.get(a)

      // 바닥(z=0) — 점과 base 를 함께 덮는다. 높이를 읽을 기준면이다.
      const xs = [0, ...evs.map((e) => e.tip[0])]
      const ys = [0, ...evs.map((e) => e.tip[1])]
      const pad = 60
      const [x0, x1, y0, y1] = [Math.min(...xs) - pad, Math.max(...xs) + pad, Math.min(...ys) - pad, Math.max(...ys) + pad]
      out.push({
        type: 'mesh3d', scene, x: [x0, x1, x1, x0], y: [y0, y0, y1, y1], z: [0, 0, 0, 0],
        i: [0, 0], j: [1, 2], k: [2, 3], color: '#525252', opacity: 0.18,
        hoverinfo: 'skip', showlegend: false,
      } as unknown as Data) // 타입 정의가 i/j/k 를 TypedArray 로만 받는다 — 런타임은 배열도 된다
      out.push({
        type: 'scatter3d', scene, mode: 'text+markers', x: [0], y: [0], z: [0], text: ['base'],
        textposition: 'bottom center', textfont: { color: '#737373', size: 10 },
        marker: { symbol: 'diamond', size: 5, color: '#737373' }, hoverinfo: 'skip', showlegend: false,
      } as Data)

      const groups = new Map<string, { name: string; color: string; evs: Ev[] }>()
      if (colorBy === 'order') groups.set('', { name: t('수집 순서'), color: '', evs })
      else {
        for (const e of evs) {
          const [k, name, color] = groupOf(e)
          if (!groups.has(k)) groups.set(k, { name, color, evs: [] })
          groups.get(k)!.evs.push(e)
        }
      }
      for (const [k, g] of [...groups].sort(([x], [y]) => x.localeCompare(y))) {
        // 범례는 묶음마다 한 번 — 같은 데이터셋이 두 팔 패널에 다 있어도 한 줄이고,
        // 누르면 두 패널에서 같이 꺼진다(legendgroup).
        const first = !legendShown.has(k)
        legendShown.add(k)
        out.push({
          type: 'scatter3d', scene, mode: 'markers', name: g.name, legendgroup: k,
          showlegend: colorBy !== 'order' && first,
          x: g.evs.map((e) => e.tip[0]), y: g.evs.map((e) => e.tip[1]), z: g.evs.map((e) => e.tip[2]),
          customdata: g.evs.map((e) => e.key),
          text: g.evs.map(hover), hovertemplate: '%{text}<extra></extra>',
          marker: colorBy === 'order'
            ? {
              size: 4, color: g.evs.map((e) => e.order), colorscale: ORDER_SCALE, cmin: 0, cmax: 1,
              symbol: g.evs.map(sym), opacity: 0.85, showscale: i === 0,
              colorbar: { title: { text: t('수집 순서') }, tickvals: [0, 1], ticktext: [t('처음'), t('끝')], thickness: 10, len: 0.4 },
            }
            : { size: 4, color: g.color, symbol: g.evs.map(sym), opacity: 0.85 },
        } as Data)
      }

      // 접근 궤적(닫기 0.5초 전 → 닫기 끝). 끊김은 null — 파지마다 따로 그은 선이다
      const trailLine = (list: number[][][], color: string, width: number, opacity: number) => {
        const xs: (number | null)[] = [], ys: (number | null)[] = [], zs: (number | null)[] = []
        for (const tr of list) {
          if (tr.length < 2) continue
          for (const p of tr) { xs.push(p[0]); ys.push(p[1]); zs.push(p[2]) }
          xs.push(null); ys.push(null); zs.push(null)
        }
        if (xs.length === 0) return
        out.push({
          type: 'scatter3d', scene, mode: 'lines', hoverinfo: 'skip', showlegend: false, connectgaps: false,
          x: xs, y: ys, z: zs, line: { color, width }, opacity,
        } as Data)
      }
      if (showTrails) trailLine(evs.map((e) => e.trail), '#a3a3a3', 2, 0.55)
      const mineTrail = sel && armOf(sel) === a ? sel : null
      if (mineTrail) {
        // 들어오는 길은 흰색, 닫는 동안 움직인 길은 빨강 — 튀어나가며 집으면 빨간 꼬리가 길다
        trailLine([mineTrail.trail.slice(0, mineTrail.trail_at + 1)], '#fafafa', 5, 1)
        trailLine([mineTrail.trail.slice(mineTrail.trail_at)], '#e66767', 6, 1)
      }

      const stick = (e: Ev, faint: boolean) => {
        const s = e.stick
        for (let j = 0; j < s.length - 1; j++) {
          out.push({
            type: 'scatter3d', scene, mode: 'lines', hoverinfo: 'skip', showlegend: false,
            x: [s[j][0], s[j + 1][0]], y: [s[j][1], s[j + 1][1]], z: [s[j][2], s[j + 1][2]],
            line: { color: faint ? '#737373' : STICK_COLORS[j % STICK_COLORS.length], width: faint ? 4 : 9 },
            opacity: faint ? 0.6 : 1,
          } as Data)
        }
        if (faint) return
        const joints = s.slice(1, -1)
        out.push({
          type: 'scatter3d', scene, mode: 'text+markers', hoverinfo: 'skip', showlegend: false,
          x: joints.map((p) => p[0]), y: joints.map((p) => p[1]), z: joints.map((p) => p[2]),
          text: stickLabels.slice(1, -1), textposition: 'middle right',
          textfont: { color: '#e5e5e5', size: 11 }, marker: { size: 3, color: '#e5e5e5' },
        } as Data)
      }
      // 고른 것이 이 팔이면 그것을 색으로, 중앙 자세는 회색 참고선으로 둔다
      const mine = sel && armOf(sel) === a ? sel : null
      if (st) {
        if (mine && mine.key !== st.medoid.key) stick(st.medoid, true)
        stick(mine ?? st.medoid, false)
      }
      if (mine) {
        out.push({
          type: 'scatter3d', scene, mode: 'markers', hoverinfo: 'skip', showlegend: false,
          x: [mine.tip[0]], y: [mine.tip[1]], z: [mine.tip[2]],
          marker: { size: 9, symbol: 'circle-open', color: '#fafafa', line: { width: 2, color: '#fafafa' } },
        } as Data)
      }
    })
    return out
  }, [visible, panelArms, statsByArm, colorBy, groupOf, hover, sel, stickLabels, showTrails, t])

  const sceneLayout = useMemo<Partial<Layout>>(() => {
    const axis = (title: string) => ({
      title: { text: title }, gridcolor: '#2a2a2a', zerolinecolor: '#444', color: '#888',
      backgroundcolor: BG, showbackground: false,
    })
    const n = panelArms.length || 1
    const zoom = n === 1 ? 1 : n === 2 ? 1.45 : 1.8
    const l: Record<string, unknown> = {
      autosize: true,
      margin: { l: 0, r: 0, t: 26, b: 0 },
      paper_bgcolor: BG,
      font: { color: '#999', size: 10 },
      // 범례는 그림 위 여백에 — 패널 제목과 겹치지 않게
      legend: { orientation: 'h', x: 0, y: 1, yanchor: 'bottom', font: { size: 10 }, bgcolor: 'rgba(0,0,0,0)' },
      // 데이터가 바뀌어도(필터·선택) 카메라를 그대로 둔다. 패널 수가 바뀌면 새로 잡는다 —
      // 좁아진 패널에 넓은 패널의 카메라를 물려주면 팔이 잘린다.
      uirevision: `pos-${n}`,
      annotations: n > 1
        ? panelArms.map((a, i) => ({
          text: `${armName(a)} · ${t('자기 base 기준')}`, showarrow: false,
          xref: 'paper', yref: 'paper', x: (i + 0.5) / n, y: 1, yanchor: 'top',
          font: { size: 12, color: '#d4d4d4' },
        }))
        : [],
    }
    for (let i = 0; i < n; i++) {
      l[sceneId(i)] = {
        domain: { x: [i / n + (i ? 0.01 : 0), (i + 1) / n - (i < n - 1 ? 0.01 : 0)], y: [0, n > 1 ? 0.95 : 1] },
        // ⚠ 축 비율을 데이터 그대로 — 아니면 z(수 cm)가 x·y(수십 cm)만큼 부풀어 높이 차가 과장된다
        aspectmode: 'data',
        bgcolor: BG,
        xaxis: axis('x (mm)'), yaxis: axis('y (mm)'), zaxis: axis('z (mm)'),
        // 패널이 좁으면 그만큼 물러선다 — 같은 거리면 base 와 팔 아래쪽이 화면 밖으로 나간다
        camera: { eye: { x: -1.3 * zoom, y: -1.5 * zoom, z: 0.9 * zoom } },
        uirevision: `pos-${n}`,
      }
    }
    return l as Partial<Layout>
  }, [panelArms, t]) // eslint-disable-line react-hooks/exhaustive-deps

  // 패널이 여럿이면 **같은 방향에서** 봐야 비교가 된다 — 하나를 돌리면 나머지도 따라 돈다
  const sceneGd = useRef<HTMLElement | null>(null)
  const syncing = useRef(false)
  const onSceneRelayout = (ev: Readonly<Record<string, unknown>>) => {
    const gd = sceneGd.current
    if (!gd || syncing.current || panelArms.length < 2) return
    const key = Object.keys(ev).find((k) => /^scene\d*\.camera$/.test(k))
    if (!key) return
    const camera = ev[key]
    const update: Record<string, unknown> = {}
    panelArms.forEach((_, i) => {
      const k = `${sceneId(i)}.camera`
      if (k !== key) update[k] = camera
    })
    syncing.current = true
    Promise.resolve((Plotly as { relayout: (g: HTMLElement, u: object) => Promise<unknown> }).relayout(gd, update))
      .catch(() => { /* 다음 회전에 다시 맞춘다 */ })
      .finally(() => { syncing.current = false })
  }

  // ── 관절별 분포 (띠) ──

  // 관절 칸마다 팔별로 한 열씩 — 왼팔·오른팔 J1 은 거울상이라 한 열에 섞으면 안 된다
  const stripData = useMemo<Data[]>(() => {
    const out: Data[] = []
    if (visible.length === 0) return out
    const col = (e: Ev) => panelArms.indexOf(armOf(e))
    CHANNELS.forEach(({ label, get: val }, c) => {
      const ax = c === 0 ? '' : String(c + 1)
      panelArms.forEach((a, i) => {
        out.push({
          type: 'box', y: nums(visible.filter((e) => armOf(e) === a).map(val)), x0: i, width: 0.9, boxpoints: false,
          xaxis: `x${ax}`, yaxis: `y${ax}`,
          fillcolor: 'rgba(0,0,0,0)', line: { color: '#525252', width: 1 }, hoverinfo: 'skip', showlegend: false,
        } as Data)
      })
      out.push({
        type: 'scatter', mode: 'markers', xaxis: `x${ax}`, yaxis: `y${ax}`, showlegend: false,
        x: visible.map((e) => col(e) + jitter(e.key)), y: visible.map(val),
        customdata: visible.map((e) => e.key),
        text: visible.map(hover), hovertemplate: `%{text}<br>${t(label)}: %{y:.1f}<extra></extra>`,
        marker: colorBy === 'order'
          ? { size: 5, color: visible.map((e) => e.order), colorscale: ORDER_SCALE, cmin: 0, cmax: 1,
              symbol: visible.map((e) => (e.outcome === 'empty' ? 'x' : 'circle')), opacity: 0.8 }
          : { size: 5, color: visible.map((e) => groupOf(e)[2]),
              symbol: visible.map((e) => (e.outcome === 'empty' ? 'x' : 'circle')), opacity: 0.8 },
      } as Data)
      if (sel && val(sel) != null) {
        out.push({
          type: 'scatter', mode: 'markers', xaxis: `x${ax}`, yaxis: `y${ax}`, hoverinfo: 'skip', showlegend: false,
          x: [col(sel) + jitter(sel.key)], y: [val(sel)],
          marker: { size: 12, color: 'rgba(0,0,0,0)', line: { color: '#fafafa', width: 2 } },
        } as Data)
      }
    })
    return out
  }, [visible, panelArms, colorBy, groupOf, sel, hover, t])

  // 높이는 레이아웃과 그래프 div 둘 다에 — div 에 없으면 창 리사이즈 뒤 0 으로 무너진다 (PosCoverage 주석)
  const stripHeight = panelArms.length > 1 ? 250 : 230
  const stripLayout = useMemo<Partial<Layout>>(() => {
    const n = panelArms.length || 1
    const l: Record<string, unknown> = {
      height: stripHeight,
      margin: { l: 34, r: 8, t: 8, b: n > 1 ? 44 : 30 },
      paper_bgcolor: BG, plot_bgcolor: BG,
      font: { color: '#999', size: 10 },
      showlegend: false,
      hovermode: 'closest',
      grid: { rows: 1, columns: CHANNELS.length, pattern: 'independent', xgap: 0.3 },
      uirevision: 'pos-strip',
    }
    CHANNELS.forEach(({ label, unit }, c) => {
      const ax = c === 0 ? '' : String(c + 1)
      l[`xaxis${ax}`] = {
        range: [-0.6, n - 0.4], showgrid: false, zeroline: false, fixedrange: true,
        // 열이 둘 이상이면 어느 팔인지 열 아래에 적는다
        showticklabels: n > 1, tickvals: panelArms.map((_, i) => i), ticktext: panelArms.map(armName),
        title: { text: unit ? `${t(label)} (${unit})` : t(label), standoff: 4 },
      }
      l[`yaxis${ax}`] = { gridcolor: '#2a2a2a', zerolinecolor: '#444', color: '#888' }
    })
    return l as Partial<Layout>
  }, [panelArms, t, stripHeight]) // eslint-disable-line react-hooks/exhaustive-deps

  const onPick = (ev: Readonly<PlotMouseEvent>) => {
    const k = ev.points?.[0]?.customdata
    if (typeof k === 'string') setSelected((cur) => (cur === k ? null : k))
  }

  // Plotly 는 창 크기만 듣는다 — 왼쪽 목록이 접히거나 패널 폭이 바뀌면 직접 맞춘다
  const sceneRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = sceneRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    let raf = 0
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        const gd = el.querySelector<HTMLElement>('.js-plotly-plot')
        if (gd && el.clientWidth > 0) {
          try { (Plotly as { Plots: { resize: (g: HTMLElement) => void } }).Plots.resize(gd) } catch { /* 다음 리사이즈에 맞춘다 */ }
        }
      })
    })
    ro.observe(el)
    return () => { cancelAnimationFrame(raf); ro.disconnect() }
  }, [])

  // ── 렌더 ──

  const seg = (on: boolean) =>
    `px-2 py-0.5 rounded text-xs transition-colors ${
      on ? 'bg-neutral-700 text-white' : 'text-neutral-500 hover:text-white'}`
  const chip = (on: boolean) =>
    `px-2 py-0.5 rounded border text-xs ${
      on ? 'border-neutral-500 text-neutral-200' : 'border-neutral-700 text-neutral-600 line-through'}`
  const nHold = all.filter((e) => e.outcome === 'hold').length
  const nEmpty = all.length - nHold

  return (
    <div className="flex gap-4 items-start">
      <aside className="w-72 shrink-0 space-y-3">
        {/* 데이터셋 — 여러 개 */}
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 text-xs space-y-2">
          <div className="flex items-center justify-between text-neutral-400">
            <span>{t('데이터셋 ({n}개 선택)', { n: picks.length })}</span>
            {picks.length > 0 && (
              <button onClick={() => setPicks([])} className="text-neutral-500 hover:text-white">
                {t('모두 해제')}
              </button>
            )}
          </div>
          {choices.length > 8 && (
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('이름으로 찾기')}
              className="w-full rounded bg-neutral-900 border border-neutral-700 px-2 py-1"
            />
          )}
          <ul className="max-h-[40vh] overflow-y-auto divide-y divide-neutral-700/60 -mx-1">
            {shown.map((d) => {
              const p = picks.find((x) => x.id === d.id)
              const l = loads[d.id]
              return (
                <li key={d.id} className="flex items-center gap-2 px-1 py-1">
                  <input type="checkbox" checked={!!p} onChange={() => toggle(d.id)} className="shrink-0" />
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-full"
                    style={{ background: p ? SERIES[p.slot % SERIES.length] : 'transparent' }}
                  />
                  <button onClick={() => toggle(d.id)} className="min-w-0 flex-1 truncate text-left text-neutral-300" title={d.id}>
                    {d.id}
                  </button>
                  {p && l?.state === 'loading' && <span className="text-neutral-500">…</span>}
                  {p && l?.state === 'ok' && (
                    <span className="text-neutral-500">{t('파지 {n}', { n: l.res.events.length })}</span>
                  )}
                  {p && l?.state === 'error' && <span className="text-red-400" title={l.error}>{t('⚠ 오류')}</span>}
                  {p && l?.state === 'none' && (
                    <button
                      onClick={() => void analyze(d.id)}
                      disabled={analyzing.includes(d.id)}
                      title={t('페이즈 분석 전 — 분석해야 파지 구간이 생긴다')}
                      className="px-1.5 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white disabled:opacity-50"
                    >
                      {analyzing.includes(d.id) ? t('분석 중…') : t('분석')}
                    </button>
                  )}
                </li>
              )
            })}
          </ul>
        </div>

        {/* 보기 */}
        {all.length > 0 && (
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 text-xs space-y-3">
            <div className="space-y-1">
              <div className="text-neutral-500">{t('색')}</div>
              <div className="flex flex-wrap gap-0.5">
                {([
                  ['dataset', t('데이터셋')], ['cycle', t('몇 번째 파지')],
                  ['outcome', t('물음/빈손')], ['order', t('수집 순서')],
                ] as [ColorBy, string][]).map(([k, label]) => (
                  <button key={k} className={seg(colorBy === k)} onClick={() => setColorByUser(k)}>{label}</button>
                ))}
              </div>
            </div>
            <div className="space-y-1">
              <div className="text-neutral-500">{t('포함')}</div>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={showHold} onChange={(e) => setShowHold(e.target.checked)} />
                {t('물음 ●')} <span className="text-neutral-500">{nHold}</span>
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={showEmpty} onChange={(e) => setShowEmpty(e.target.checked)} />
                {t('빈손 ✕')} <span className="text-neutral-500">{nEmpty}</span>
              </label>
              <label className="flex items-center gap-2" title={t('닫기 0.5초 전부터 닫기가 끝날 때까지 말단이 지나간 길. 고른 파지는 늘 보인다')}>
                <input type="checkbox" checked={showTrails} onChange={(e) => setShowTrails(e.target.checked)} />
                {t('접근 궤적 — 모든 파지')}
              </label>
              <label className="flex items-center gap-2" title={t('사람이 뷰어에서 확인(✔)한 에피소드의 라벨만')}>
                <input type="checkbox" checked={reviewedOnly} onChange={(e) => setReviewedOnly(e.target.checked)} />
                {t('검토한 라벨만')}
              </label>
            </div>
            {arms.length > 1 && (
              <div className="space-y-1">
                <div className="text-neutral-500">{t('팔')}</div>
                <div className="flex flex-wrap gap-1">
                  {arms.map((a) => (
                    <button key={a} className={chip(!hiddenArms.includes(a))}
                            onClick={() => setHiddenArms((h) => h.includes(a) ? h.filter((x) => x !== a) : [...h, a])}>
                      {armName(a)}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {cycles.length > 1 && (
              <div className="space-y-1">
                <div className="text-neutral-500">{t('몇 번째 파지')}</div>
                <div className="flex flex-wrap gap-1">
                  {cycles.map((c) => (
                    <button key={c} className={chip(!hiddenCycles.includes(c))}
                            onClick={() => setHiddenCycles((h) => h.includes(c) ? h.filter((x) => x !== c) : [...h, c])}>
                      {c}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </aside>

      <main className="flex-1 min-w-0 space-y-3">
        {picks.length === 0 ? (
          <div className="rounded-lg border border-dashed border-neutral-700 p-10 text-center text-sm text-neutral-500">
            {t('왼쪽에서 데이터셋을 고르세요 — 여러 개를 고르면 한 공간에 겹쳐 본다')}
          </div>
        ) : (
          <>
            {/* ⚠ **못 받은 것과 없는 것을 한 문장으로 뭉개지 않는다.** 예전에는 셋을 다
                "파지 구간이 없습니다 — 분석을 먼저 하세요" 로 보였는데, 실제로는 게이트웨이가
                옛 코드라 404 였다 — 분석을 몇 번 다시 해도 안 바뀌는 안내였다. */}
            {(failed.length > 0 || noGrasps.length > 0) && (
              <div className="rounded-lg border border-amber-500/30 bg-neutral-800 p-3 text-xs space-y-1">
                {failed.map((p) => (
                  <div key={p.id} className="text-red-400">
                    {t('{id}: 파지 목록을 받지 못했습니다 — {error}', { id: p.id, error: errorOf(p.id) })}
                  </div>
                ))}
                {noGrasps.map((p) => (
                  <div key={p.id} className="text-amber-400">
                    {t('{id}: 분석은 됐지만 GRASP 구간이 0개입니다 — 뷰어의 파라미터(hold_gap 등)를 조정해 재분석하세요', { id: p.id })}
                  </div>
                ))}
                {failed.length > 0 && (
                  <button
                    onClick={() => failed.forEach((p) => load(p.id))}
                    className="mt-1 rounded border border-neutral-600 px-2 py-0.5 text-neutral-300 hover:bg-neutral-700"
                  >
                    {t('다시 시도')}
                  </button>
                )}
              </div>
            )}
            {all.length === 0 ? (
              <div className="rounded-lg border border-dashed border-neutral-700 p-10 text-center text-sm text-neutral-500">
                {picks.some((p) => loads[p.id]?.state === 'loading')
                  ? t('읽는 중…')
                  : unlabeled.length > 0
                    ? t('분석 전인 데이터셋은 왼쪽 목록의 [분석] 을 누르세요 — 페이즈 분석이 파지 구간을 만든다')
                    : t('그릴 파지가 없습니다')}
              </div>
            ) : (
          <>
            {/* 요약 */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-neutral-400">
              <span>{t('파지 {n}건', { n: visible.length })}</span>
              <span>{t('에피소드 {n}', { n: totals.episodes })}</span>
              <span className={totals.empty ? 'text-red-400' : ''}>{t('빈손 {n}', { n: totals.empty })}</span>
              {/* 퍼짐은 팔마다 — 좌표계가 다른 점들의 표준편차는 아무것도 말해주지 않는다 */}
              {panelArms.map((a) => {
                const st = statsByArm.get(a)
                if (!st) return null
                return (
                  <span key={a} title={t('말단(그리퍼 손가락 뿌리) 위치의 표준편차')}>
                    {split && <span className="text-neutral-500">{armName(a)} </span>}
                    {t('퍼짐 σ x {x} · y {y} · z {z} mm', {
                      x: st.tipStd[0].toFixed(0), y: st.tipStd[1].toFixed(0), z: st.tipStd[2].toFixed(0),
                    })}
                  </span>
                )
              })}
            </div>

            <div className="flex flex-col xl:flex-row gap-3 items-start">
              <div className="w-full flex-1 min-w-0 rounded-lg border border-neutral-700 bg-neutral-900 overflow-hidden">
                <div ref={sceneRef} className="h-[60vh] min-h-[420px]">
                  <Plot
                    data={sceneData}
                    layout={sceneLayout}
                    config={{ displaylogo: false, responsive: true }}
                    style={{ width: '100%', height: '100%' }}
                    useResizeHandler
                    onClick={onPick}
                    onInitialized={(_, gd) => { sceneGd.current = gd as HTMLElement }}
                    onUpdate={(_, gd) => { sceneGd.current = gd as HTMLElement }}
                    onRelayout={onSceneRelayout}
                  />
                </div>
                {/* 스틱 범례 — 마디 색은 데이터 범례와 따로 둔다 (켜고 끄는 대상이 아니다) */}
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-neutral-800 px-3 py-1.5 text-xs text-neutral-400">
                  <span className="text-neutral-500">
                    {sel ? t('고른 자세') : t('중앙 자세')}
                  </span>
                  {STICK_PARTS.map((part, i) => (
                    <span key={part} className="flex items-center gap-1">
                      <span className="inline-block h-1 w-4 rounded" style={{ background: STICK_COLORS[i] }} />
                      {t(part)}
                    </span>
                  ))}
                  {sel && (
                    <span className="flex items-center gap-1 text-neutral-500">
                      <span className="inline-block h-0.5 w-4 rounded bg-neutral-500" />
                      {t('중앙 자세')}
                    </span>
                  )}
                  {split && (
                    <span className="ml-auto text-neutral-500">
                      {t('팔마다 패널이 따로다 — 두 팔 사이 배치는 기록에 없다. 하나를 돌리면 같이 돈다')}
                    </span>
                  )}
                </div>
              </div>

              {/* 고른 파지 */}
              <div className="w-full xl:w-72 shrink-0 rounded-lg border border-neutral-700 bg-neutral-800 p-3 text-xs space-y-2">
                {sel && selStats ? (
                  <>
                    <div className="truncate text-neutral-500" title={sel.ds}>{sel.ds}</div>
                    <div className="flex items-center gap-2 text-sm text-neutral-200">
                      <span className="font-mono">ep {sel.episode}</span>
                      <span>{t('{cycle}번째 파지', { cycle: sel.cycle })}</span>
                      {sel.outcome === 'empty'
                        ? <span className="text-red-400">{t('✕ 빈손')}</span>
                        : <span className="text-neutral-400">{t('● 물음')}</span>}
                      {sel.reviewed && <span className="text-green-500" title={t('검토한 라벨')}>✔</span>}
                    </div>
                    <div className="text-neutral-500">
                      {t('프레임 {frame} ({sec}초)', { frame: sel.frame, sec: sel.t.toFixed(1) })}
                      {sel.arm && ` · ${armName(sel.arm)}`}
                    </div>
                    {sel.source === 'fsm' && (
                      <div className="rounded border border-neutral-700 px-2 py-1 text-neutral-400">
                        {t('이 팔은 페이즈 라벨이 없어(라벨은 에피소드당 한 팔) 같은 규칙으로 즉석 분석한 파지다 — 사람 검토 없음')}
                      </div>
                    )}
                    <table className="w-full tabular-nums">
                      <tbody>
                        {CHANNELS.map(({ label, unit, get }, c) => {
                          const v = get(sel)
                          const z = v != null && selStats.chStd[c] > 0 ? (v - selStats.chMean[c]) / selStats.chStd[c] : 0
                          return (
                            <tr key={label}>
                              <td className="text-neutral-500">{t(label)}</td>
                              <td className="text-right text-neutral-200">
                                {v == null ? '—' : `${v.toFixed(1)}${unit === '°' ? '°' : unit ? ` ${unit}` : ''}`}
                              </td>
                              <td className={`text-right ${Math.abs(z) >= 2 ? 'text-amber-400' : 'text-neutral-500'}`}
                                  title={t('지금 보이는 같은 팔 파지들의 평균에서 표준편차 몇 배 떨어졌나')}>
                                {z >= 0 ? '+' : ''}{z.toFixed(1)}σ
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                    <div className="text-neutral-400 tabular-nums">
                      {t('말단')} x {sel.tip[0].toFixed(0)} · y {sel.tip[1].toFixed(0)} · z {sel.tip[2].toFixed(0)} mm
                    </div>
                    <button
                      onClick={() => onOpen(sel.ds, sel.episode, sel.frame)}
                      className="w-full rounded bg-blue-600 px-2 py-1.5 text-white hover:bg-blue-500"
                    >
                      {t('뷰어에서 열기 →')}
                    </button>
                  </>
                ) : (
                  <div className="space-y-1 text-neutral-500">
                    <div>{t('점을 누르면 그 파지 순간의 팔 자세가 스틱으로 그려진다.')}</div>
                    {panelArms.length > 0 && (
                      <div className="space-y-0.5">
                        <div>{t('지금 그려진 스틱은 중앙 자세 — 관절값이 평균에 가장 가까운 파지')}</div>
                        {panelArms.map((a) => {
                          const m = statsByArm.get(a)?.medoid
                          return m && (
                            <div key={a} className="text-neutral-600">
                              {split && `${armName(a)}: `}{short(m.ds)} · ep {m.episode}
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            <PosCoverage events={visible} arms={panelArms} armName={armName} dsColor={dsColor} dsName={short} />

            <PosApproach
              events={visible} arms={panelArms} armName={armName} dsColor={dsColor} dsName={short}
              selectedKey={sel?.key ?? null} onSelect={setSelected} onOpen={onOpen}
            />

            {/* 관절별 분포 — 3D 에는 세 축만 들어간다. 일곱 채널 전부를 여기서 본다 */}
            <div className="rounded-lg border border-neutral-700 bg-neutral-900 px-1 pt-1">
              <div className="px-2 pt-1 text-xs text-neutral-500">{t('관절별 분포 — 파지 순간')}</div>
              <Plot
                data={stripData}
                layout={stripLayout}
                config={{ displaylogo: false, responsive: true, displayModeBar: false }}
                style={{ width: '100%', height: stripHeight }}
                useResizeHandler
                onClick={onPick}
              />
            </div>
          </>
            )}
          </>
        )}
      </main>
    </div>
  )
}
