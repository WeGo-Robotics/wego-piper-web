import type { BoardSpec, CatalogItem, Pick, Placed } from './types'
import { allCells, boardFrame, parseCell, zoneRect } from './types'

/**
 * 작업판 그림 — 설계 화면·히트맵·시도 미리보기가 같이 쓴다.
 *
 * ⚠ **칸 이름은 그림 기준이다** — A1 이 그림의 왼쪽 위, 로봇이 어느 변에 있든. 인쇄물·백엔드
 *   (`services/scenario/board.py`)와 같은 그림이라야 "B3" 가 화면과 종이에서 같은 칸이다. 로봇은
 *   고른 변(`robot_side`) 바깥에 회색 막대로 그린다. SVG 좌표는 mm 그대로 쓴다.
 *
 * 격자 바깥 구역(`zone.outside`)은 격자 둘레에 직사각형으로 그리고, 그림은 그것까지 덮도록 넓힌다
 * — 로봇 막대는 그 바깥에.
 */

type Props = {
  board: BoardSpec
  catalog?: CatalogItem[]
  /** 판 위 물체 */
  layout?: Placed[]
  /** 이번에 집을 것 — 굵은 테두리 */
  picks?: Pick[]
  /** 구역 안에 들어 있는 것 — 구역 위에 작게 */
  inZones?: { obj: string; zone: string }[]
  /** 칸별 숫자(히트맵). `heatMax` 대비 진하기 */
  counts?: Record<string, number>
  heatColor?: string
  heatMax?: number
  /** 칸 위에 덧칠 (설계 편집기의 도구 미리보기 등) */
  onCellClick?: (cell: string) => void
  /** 접근 중 옮길 칸 — 점선 화살표 */
  perturbTo?: { from: string; to: string }
  compact?: boolean
  className?: string
}

const ZONE_TINTS = ['#2563eb', '#16a34a', '#d97706', '#9333ea', '#db2777', '#0891b2']

export function zoneTint(board: BoardSpec, zoneId: string): string {
  const i = board.zones.findIndex(z => z.id === zoneId)
  return ZONE_TINTS[(i < 0 ? 0 : i) % ZONE_TINTS.length]
}

export default function BoardView({
  board, catalog = [], layout = [], picks = [], inZones = [], counts, heatColor = '#3b82f6', heatMax,
  onCellClick, perturbTo, compact = false, className = '',
}: Props) {
  const cell = board.cell_mm
  const W = board.cols * cell
  const H = board.rows * cell
  const side = board.robot_side ?? 'top'
  const pad = compact ? 4 : 14
  const robotPad = compact ? 10 : 22
  const m = {
    top: side === 'top' ? robotPad : pad, bottom: side === 'bottom' ? robotPad : pad,
    left: side === 'left' ? robotPad : pad, right: side === 'right' ? robotPad : pad,
  }
  const [fx0, fy0, fx1, fy1] = boardFrame(board)
  const robot = side === 'top' ? { x: W / 2 - cell * 0.4, y: fy0 - m.top + 2, w: cell * 0.8, h: m.top - 6 }
    : side === 'bottom' ? { x: W / 2 - cell * 0.4, y: fy1 + 4, w: cell * 0.8, h: m.bottom - 6 }
    : side === 'left' ? { x: fx0 - m.left + 2, y: H / 2 - cell * 0.4, w: m.left - 6, h: cell * 0.8 }
    : { x: fx1 + 4, y: H / 2 - cell * 0.4, w: m.right - 6, h: cell * 0.8 }
  const zoneOf = new Map<string, string>()
  board.zones.forEach(z => z.cells.forEach(c => zoneOf.set(c, z.id)))
  const blocked = new Set(board.blocked)
  const color = (obj: string) => catalog.find(i => i.id === obj)?.color ?? '#888'
  const size = (obj: string) => catalog.find(i => i.id === obj)?.size_mm ?? cell * 0.7
  const max = heatMax ?? Math.max(1, ...Object.values(counts ?? {}))
  const pickCells = new Set(picks.map(p => p.cell).filter(Boolean) as string[])
  const center = (name: string): [number, number] => {
    const [c, r] = parseCell(name)
    return [(c + 0.5) * cell, (r + 0.5) * cell]
  }

  return (
    <svg viewBox={`${fx0 - m.left} ${fy0 - m.top} ${fx1 - fx0 + m.left + m.right} ${fy1 - fy0 + m.top + m.bottom}`}
      className={className}
      style={{ width: '100%', height: 'auto', display: 'block' }}>
      {/* 로봇 쪽 */}
      <rect x={robot.x} y={robot.y} width={robot.w} height={robot.h} rx={3} fill="#525252" />
      {allCells(board).map(name => {
        const [c, r] = parseCell(name)
        const zid = zoneOf.get(name)
        const n = counts?.[name]
        const fill = zid ? zoneTint(board, zid)
          : blocked.has(name) ? '#262626'
          : n !== undefined ? heatColor : '#171717'
        const opacity = zid ? 0.25 : n !== undefined && !blocked.has(name) ? 0.08 + 0.85 * (n / max) : 1
        return (
          <g key={name} onClick={onCellClick ? () => onCellClick(name) : undefined}
            style={onCellClick ? { cursor: 'pointer' } : undefined}>
            <rect x={c * cell} y={r * cell} width={cell} height={cell} fill={fill} fillOpacity={opacity}
              stroke="#404040" strokeWidth={compact ? 0.8 : 1.2} />
            {blocked.has(name) && (
              <path d={`M${c * cell + 8} ${r * cell + 8} L${(c + 1) * cell - 8} ${(r + 1) * cell - 8}
                M${(c + 1) * cell - 8} ${r * cell + 8} L${c * cell + 8} ${(r + 1) * cell - 8}`}
                stroke="#525252" strokeWidth={2} />
            )}
            {!compact && !zid && !blocked.has(name) && n === undefined && (
              <text x={c * cell + 4} y={r * cell + 12} fontSize={10} fill="#737373">{name}</text>
            )}
            {n !== undefined && !zid && !blocked.has(name) && (
              <text x={(c + 0.5) * cell} y={(r + 0.5) * cell + 6} fontSize={compact ? 22 : 18}
                textAnchor="middle" fill="#f5f5f5">{n}</text>
            )}
          </g>
        )
      })}
      {board.zones.map(z => {
        const rect = zoneRect(board, z)
        const pts = z.cells.map(center)
        if (!rect && !pts.length) return null
        const zx = rect ? rect[0] + rect[2] / 2 : pts.reduce((a, p) => a + p[0], 0) / pts.length
        const zy = rect ? rect[1] + rect[3] / 2 : pts.reduce((a, p) => a + p[1], 0) / pts.length
        const inside = inZones.filter(p => p.zone === z.id)
        return (
          <g key={z.id} pointerEvents="none">
            {rect && (
              <rect x={rect[0]} y={rect[1]} width={rect[2]} height={rect[3]} rx={4}
                fill={zoneTint(board, z.id)} fillOpacity={0.25} stroke={zoneTint(board, z.id)}
                strokeWidth={compact ? 1.5 : 2} strokeDasharray="6 4" />
            )}
            <text x={zx} y={zy - (inside.length ? 8 : 0)} fontSize={compact ? 16 : 13} textAnchor="middle"
              fill="#e5e5e5">{z.label || z.id}</text>
            {inside.map((p, i) => (
              <circle key={i} cx={zx - (inside.length - 1) * 7 + i * 14} cy={zy + 10} r={6}
                fill={color(p.obj)} stroke="#000" strokeWidth={1} />
            ))}
          </g>
        )
      })}
      {layout.map((p, i) => {
        const [x, y] = center(p.cell)
        const r = Math.min(cell * 0.46, size(p.obj) / 2)
        const target = pickCells.has(p.cell)
        return (
          <circle key={i} cx={x} cy={y} r={r} fill={color(p.obj)} pointerEvents="none"
            stroke={target ? '#ffffff' : '#000'} strokeWidth={target ? 5 : 1.5} />
        )
      })}
      {perturbTo && (() => {
        const [x1, y1] = center(perturbTo.from)
        const [x2, y2] = center(perturbTo.to)
        return (
          <g pointerEvents="none">
            <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#fbbf24" strokeWidth={3} strokeDasharray="8 6" />
            <circle cx={x2} cy={y2} r={cell * 0.3} fill="none" stroke="#fbbf24" strokeWidth={3}
              strokeDasharray="8 6" />
          </g>
        )
      })()}
    </svg>
  )
}
