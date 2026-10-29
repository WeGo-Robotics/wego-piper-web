import { useState } from 'react'
import { useT } from '../../i18n'
import BoardView, { zoneTint } from './BoardView'
import type { BoardSpec, Outside, RobotSide, Zone } from './types'
import { cellName, clipOutside, edgeLength, parseCell, withOutsideBox, withSideBox } from './types'

/**
 * 작업판 편집 — 크기와 칸 역할(놓을 칸 · 금지 · 구역).
 *
 * 구역(상자·지그 자리)은 격자 **안**이면 칸으로 칠하고, **바깥**이면 변에 붙는 직사각형(변·칸 범위·
 * 깊이·띄움)으로 잡는다(`services/scenario/board.py`). 바깥 구역은 칠하기 도구에 안 뜬다 — 칸이 없다.
 */

type Props = { board: BoardSpec; onChange: (b: BoardSpec) => void }

const field = 'px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100'
const input = `w-full ${field}`

function clip(b: BoardSpec): BoardSpec {
  const inside = (c: string) => { const [x, y] = parseCell(c); return x < b.cols && y < b.rows }
  return clipOutside({
    ...b,
    blocked: b.blocked.filter(inside),
    zones: b.zones.map(z => ({ ...z, cells: z.cells.filter(inside) })),
  })
}

/** 바깥 구역 자리 — 변, 그 변의 몇 번째 칸부터 몇 번째까지, 깊이·띄움 */
function OutsideFields({ board, zone, onChange }: { board: BoardSpec; zone: Zone; onChange: (o: Outside) => void }) {
  const t = useT()
  const o = zone.outside!
  const n = edgeLength(board, o.side)
  const name = (i: number) => (o.side === 'top' || o.side === 'bottom') ? cellName(i, 0)[0] : String(i + 1)
  const sel = 'px-1 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-100'
  const num = `${sel} w-14`
  return (
    <span className="flex items-center gap-1 shrink-0 text-neutral-400">
      <select value={o.side} className={sel} onChange={e => {
        const side = e.target.value as RobotSide
        const len = edgeLength(board, side)
        onChange({ ...o, side, start: Math.min(o.start, len - 1), end: Math.min(o.end, len - 1) })
      }}>
        {ROBOT_SIDES.map(s => <option key={s} value={s}>{t('바깥 {side}', { side: sideName(t, s) })}</option>)}
      </select>
      <select value={o.start} className={sel}
        onChange={e => { const v = Number(e.target.value); onChange({ ...o, start: v, end: Math.max(v, o.end) }) }}>
        {Array.from({ length: n }, (_, i) => <option key={i} value={i}>{name(i)}</option>)}
      </select>
      ~
      <select value={o.end} className={sel}
        onChange={e => { const v = Number(e.target.value); onChange({ ...o, end: v, start: Math.min(v, o.start) }) }}>
        {Array.from({ length: n }, (_, i) => <option key={i} value={i}>{name(i)}</option>)}
      </select>
      <span title={t('격자에서 바깥쪽으로 상자 자리의 깊이')}>{t('깊이')}</span>
      <input type="number" min={10} max={500} value={o.depth_mm} className={num}
        onChange={e => onChange({ ...o, depth_mm: Math.max(10, Math.min(500, Number(e.target.value) || 10)) })} />
      <span title={t('격자 가장자리와 상자 자리 사이')}>{t('띄움')}</span>
      <input type="number" min={0} max={300} value={o.gap_mm} className={num}
        onChange={e => onChange({ ...o, gap_mm: Math.max(0, Math.min(300, Number(e.target.value) || 0)) })} />
      mm
    </span>
  )
}

export const ROBOT_SIDES: RobotSide[] = ['top', 'bottom', 'left', 'right']

export function sideName(t: (s: string) => string, side: RobotSide): string {
  return { top: t('위쪽'), bottom: t('아래쪽'), left: t('왼쪽'), right: t('오른쪽') }[side]
}

export default function BoardEditor({ board, onChange }: Props) {
  const t = useT()
  const [tool, setTool] = useState<string>('work')
  const [boxWhere, setBoxWhere] = useState<'in' | 'out'>('in')
  const robotSide = board.robot_side ?? 'top'

  const setSize = (patch: Partial<BoardSpec>) => onChange(clip({ ...board, ...patch }))

  const paint = (cell: string) => {
    const blocked = board.blocked.filter(c => c !== cell)
    const zones = board.zones.map(z => ({ ...z, cells: z.cells.filter(c => c !== cell) }))
    if (tool === 'blocked') blocked.push(cell)
    if (tool.startsWith('zone:')) {
      const z = zones.find(z => z.id === tool.slice(5))
      if (z) z.cells.push(cell)
    }
    onChange({ ...board, blocked, zones })
  }

  const addZone = () => {
    let n = board.zones.length + 1
    while (board.zones.some(z => z.id === `zone${n}`)) n++
    const id = `zone${n}`
    onChange({ ...board, zones: [...board.zones, { id, label: t('구역 {n}', { n }), cells: [] }] })
    setTool(`zone:${id}`)
  }

  const tools: [string, string, string][] = [
    ['work', t('놓을 칸'), '#404040'],
    ['blocked', t('금지'), '#262626'],
    ...board.zones.filter(z => !z.outside)
      .map(z => [`zone:${z.id}`, z.label || z.id, zoneTint(board, z.id)] as [string, string, string]),
  ]

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-4 gap-2">
        <label className="text-xs text-neutral-400">{t('열')}
          <input type="number" min={1} max={26} value={board.cols} className={input}
            onChange={e => setSize({ cols: Math.max(1, Math.min(26, Number(e.target.value) || 1)) })} />
        </label>
        <label className="text-xs text-neutral-400">{t('행')}
          <input type="number" min={1} max={50} value={board.rows} className={input}
            onChange={e => setSize({ rows: Math.max(1, Math.min(50, Number(e.target.value) || 1)) })} />
        </label>
        <label className="text-xs text-neutral-400">{t('칸 크기 (mm)')}
          <input type="number" min={20} max={300} value={board.cell_mm} className={input}
            onChange={e => setSize({ cell_mm: Number(e.target.value) || 80 })} />
        </label>
        <label className="text-xs text-neutral-400" title={t('판의 로봇 쪽 가장자리에서 로봇 베이스 중심까지 — "가까운 순" 규칙이 이걸로 잽니다')}>
          {t('로봇까지 (mm)')}
          <input type="number" min={0} value={board.robot_gap_mm} className={input}
            onChange={e => setSize({ robot_gap_mm: Math.max(0, Number(e.target.value) || 0) })} />
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-neutral-400">{t('로봇 쪽')}</span>
        <div className="flex overflow-hidden rounded border border-neutral-700">
          {ROBOT_SIDES.map(s => (
            <button key={s} onClick={() => onChange({ ...board, robot_side: s })}
              className={`px-2.5 py-1 ${robotSide === s ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white'}`}>
              {sideName(t, s)}
            </button>
          ))}
        </div>
        <span className="text-neutral-400 ml-2">{t('상자')}</span>
        <div className="flex overflow-hidden rounded border border-neutral-700">
          {([['in', t('격자 안')], ['out', t('격자 바깥')]] as const).map(([k, label]) => (
            <button key={k} onClick={() => setBoxWhere(k)}
              className={`px-2.5 py-1 ${boxWhere === k ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white'}`}>
              {label}
            </button>
          ))}
        </div>
        {ROBOT_SIDES.filter(s => s !== robotSide).map(s => (
          <button key={s}
            title={boxWhere === 'in' ? t('그 변의 가운데 칸을 상자로, 나머지 칸을 금지로 칠합니다')
              : t('그 변 바깥 가운데에 상자 자리를 붙입니다 — 물체 칸은 그대로입니다')}
            onClick={() => {
              const label = t('{side} 상자', { side: sideName(t, s) })
              if (boxWhere === 'out') { onChange(withOutsideBox(board, s, label)); return }
              const next = withSideBox(board, s, label)
              onChange(next)
              setTool(`zone:${next.zones[next.zones.length - 1].id}`)
            }}
            className="px-2 py-1 rounded border border-dashed border-neutral-600 text-neutral-400 hover:text-white">
            {t('+ {side} 변', { side: sideName(t, s) })}
          </button>
        ))}
      </div>
      <p className="text-xs text-neutral-500">
        {t('격자 {w} × {h} mm · 칸 이름은 그림 기준(A1 = 왼쪽 위) · 칸을 눌러 아래 도구로 칠합니다', {
          w: board.cols * board.cell_mm, h: board.rows * board.cell_mm })}
      </p>

      <div className="flex flex-wrap items-center gap-1.5">
        {tools.map(([k, label, color]) => (
          <button key={k} onClick={() => setTool(k)}
            className={`flex items-center gap-1.5 px-2 py-1 rounded text-xs border ${tool === k
              ? 'border-blue-400 text-white' : 'border-neutral-700 text-neutral-400 hover:text-white'}`}>
            <span className="w-3 h-3 rounded-sm" style={{ background: color }} />
            {label}
          </button>
        ))}
        <button onClick={addZone}
          className="px-2 py-1 rounded text-xs border border-dashed border-neutral-600 text-neutral-400 hover:text-white">
          {t('+ 구역')}
        </button>
      </div>

      <div className="max-w-xl">
        <BoardView board={board} onCellClick={paint} />
      </div>

      {board.zones.length > 0 && (
        <div className="space-y-1">
          {board.zones.map(z => (
            <div key={z.id} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="w-3 h-3 rounded-sm shrink-0" style={{ background: zoneTint(board, z.id) }} />
              <code className="text-neutral-500 w-16 shrink-0">{z.id}</code>
              <input value={z.label} className={`${field} py-0.5 flex-1 min-w-[8rem]`} placeholder={t('구역 이름')}
                onChange={e => onChange({ ...board, zones: board.zones.map(zz => zz.id === z.id ? { ...zz, label: e.target.value } : zz) })} />
              {z.outside
                ? <OutsideFields board={board} zone={z}
                    onChange={o => onChange({ ...board, zones: board.zones.map(zz => zz.id === z.id ? { ...zz, outside: o } : zz) })} />
                : <span className="text-neutral-500 shrink-0">{t('{n}칸', { n: z.cells.length })}</span>}
              <button onClick={() => { onChange({ ...board, zones: board.zones.filter(zz => zz.id !== z.id) }); setTool('work') }}
                className="text-neutral-500 hover:text-red-400 shrink-0">{t('삭제')}</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
