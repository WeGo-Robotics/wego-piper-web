/** 시나리오 설계기 — 백엔드 `services/scenario` 와 맞춘 모양 (feature/scenario-designer.md). */

export type RobotSide = 'top' | 'bottom' | 'left' | 'right'

/** 격자 바깥 구역 — `side` 변의 `start`~`end` 번째 칸(0 부터, 끝 포함) 맞은편, `gap_mm` 띄워 `depth_mm` 깊이 */
export type Outside = { side: RobotSide; start: number; end: number; depth_mm: number; gap_mm: number }

/** 구역(상자 자리) — 격자 안이면 칸의 묶음(`cells`), 바깥이면 `outside` (feature/scenario-designer.md §3-1) */
export type Zone = { id: string; label: string; cells: string[]; outside?: Outside }

export type BoardSpec = {
  cols: number
  rows: number
  cell_mm: number
  blocked: string[]
  zones: Zone[]
  /** 판의 로봇 쪽 가장자리에서 로봇 베이스 중심까지 */
  robot_gap_mm: number
  /** 로봇이 있는 변 — 없으면(예전에 저장한 설정) 위 */
  robot_side?: RobotSide
}

export type CatalogItem = { id: string; label: string; color: string; size_mm: number; count: number }

export type Nuisance = { name: string; levels: string[] }

export type Composition = 'equal' | 'proportional' | 'fixed'

/** 모든 템플릿이 같이 쓰는 값 — 백엔드 `plan.Common` */
export type Common = {
  episodes: number
  block_size: number
  episode_unit: string
  composition: Composition
  fixed_composition: Record<string, number>
  min_spacing: number
  episode_s: number
  reset_s: number
  nuisance: Nuisance[]
  policy_type: string
  variants: Record<string, number>
}

export type TemplateField = {
  kind: 'number' | 'bool' | 'text' | 'select' | 'data'
  default: unknown
  options: string[]
  min: number | null
  max: number | null
}

export type TemplateVariant = { default: number; per: 'episode' | 'round'; requires: Record<string, string> }

export type TemplateSpec = {
  spec_version: number
  type: string
  generator: string
  episode_units: string[]
  fields: Record<string, TemplateField>
  variants: Record<string, TemplateVariant>
}

/** 설계 하나 — 저장한 설정(프리셋 `scenario`)의 values 가 이 모양이다 */
export type Design = {
  template: string
  seed: number
  board: BoardSpec
  catalog: CatalogItem[]
  common: Common
  fields: Record<string, unknown>
}

export type Placed = { obj: string; cell: string }

export type Pick = { obj: string; cell: string | null; dest: string | null; from_zone?: string }

export type Hint = {
  kind: 'missed_grasp' | 'dropped' | 'perturb' | 'empty_target' | 'wrong_bin_fix'
  obj: string
  cell?: string
  to?: string
  from?: string
}

export type PlanEpisode = {
  id: string
  block: number
  round: number
  step: number
  layout: Placed[]
  in_zones: { obj: string; zone: string }[]
  picks: Pick[]
  task: string
  variant: string
  hint: Hint | null
  reset: boolean
  hard: boolean
  nuisance: Record<string, string>
}

export type PlanWarning = { code: string } & Record<string, unknown>

export type PlanSummary = {
  episodes: number
  rounds: number
  blocks: number
  work_cells: string[]
  target_counts: Record<string, Record<string, number>>
  placed_counts: Record<string, Record<string, number>>
  type_totals: Record<string, number>
  spread: Record<string, number>
  block_spread: Record<string, number>
  variant_counts: Record<string, number>
  variants_off: string[]
  round_sizes: Record<string, number>
  crosstab: Record<string, Record<string, Record<string, number>>>
  resets: number
  est_s: number
  warnings: PlanWarning[]
}

export type Plan = {
  version: number
  template: string
  seed: number
  board: BoardSpec
  catalog: CatalogItem[]
  values: Record<string, unknown>
  episodes: PlanEpisode[]
  summary: PlanSummary
}

const COLS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

export function cellName(col: number, row: number): string {
  return `${COLS[col]}${row + 1}`
}

export function parseCell(name: string): [number, number] {
  return [COLS.indexOf(name[0]), Number(name.slice(1)) - 1]
}

export function allCells(b: BoardSpec): string[] {
  const out: string[] = []
  for (let r = 0; r < b.rows; r++) for (let c = 0; c < b.cols; c++) out.push(cellName(c, r))
  return out
}

export function workCells(b: BoardSpec): string[] {
  const off = new Set([...b.blocked, ...b.zones.flatMap(z => z.cells)])
  return allCells(b).filter(c => !off.has(c))
}

/** 한 변의 칸 — 그림 순서(왼쪽→오른쪽, 위→아래) */
export function edgeCells(b: BoardSpec, side: RobotSide): string[] {
  if (side === 'top' || side === 'bottom') {
    const r = side === 'top' ? 0 : b.rows - 1
    return Array.from({ length: b.cols }, (_, c) => cellName(c, r))
  }
  const c = side === 'left' ? 0 : b.cols - 1
  return Array.from({ length: b.rows }, (_, r) => cellName(c, r))
}

/** 변의 길이(칸 수) */
export function edgeLength(b: BoardSpec, side: RobotSide): number {
  return side === 'top' || side === 'bottom' ? b.cols : b.rows
}

/** 변 가운데 칸 범위 — 변 길이가 짝수면 2칸, 홀수면 3칸 */
function centerSpan(n: number): [number, number] {
  const span = n < 3 ? n : n % 2 ? 3 : 2
  const start = Math.floor((n - span) / 2)
  return [start, start + span - 1]
}

function freeZoneId(b: BoardSpec, side: RobotSide): string {
  let id = `bin_${side}`
  for (let n = 2; b.zones.some(z => z.id === id); n++) id = `bin_${side}${n}`
  return id
}

/** 바깥 구역의 직사각형 (x, y, 폭, 높이) mm — 백엔드 `Board.zone_rect` 와 같은 계산 */
export function zoneRect(b: BoardSpec, z: Zone): [number, number, number, number] | null {
  const o = z.outside
  if (!o) return null
  const W = b.cols * b.cell_mm
  const H = b.rows * b.cell_mm
  const a0 = o.start * b.cell_mm
  const along = (o.end - o.start + 1) * b.cell_mm
  if (o.side === 'top') return [a0, -o.gap_mm - o.depth_mm, along, o.depth_mm]
  if (o.side === 'bottom') return [a0, H + o.gap_mm, along, o.depth_mm]
  if (o.side === 'left') return [-o.gap_mm - o.depth_mm, a0, o.depth_mm, along]
  return [W + o.gap_mm, a0, o.depth_mm, along]
}

/** 격자 + 바깥 구역을 덮는 사각형 [x0, y0, x1, y1] mm */
export function boardFrame(b: BoardSpec): [number, number, number, number] {
  let [x0, y0, x1, y1] = [0, 0, b.cols * b.cell_mm, b.rows * b.cell_mm]
  for (const z of b.zones) {
    const r = zoneRect(b, z)
    if (!r) continue
    x0 = Math.min(x0, r[0]); y0 = Math.min(y0, r[1])
    x1 = Math.max(x1, r[0] + r[2]); y1 = Math.max(y1, r[1] + r[3])
  }
  return [x0, y0, x1, y1]
}

/** 격자 바깥, `side` 변 가운데에 상자를 붙인다. 물체 칸은 그대로다. */
export function withOutsideBox(b: BoardSpec, side: RobotSide, label: string): BoardSpec {
  const [start, end] = centerSpan(edgeLength(b, side))
  const outside: Outside = { side, start, end, depth_mm: Math.round(b.cell_mm * 1.5), gap_mm: 10 }
  return { ...b, zones: [...b.zones, { id: freeZoneId(b, side), label, cells: [], outside }] }
}

/** 바깥 구역들의 칸 범위를 판 크기 안으로 — 열·행을 줄였을 때 */
export function clipOutside(b: BoardSpec): BoardSpec {
  return {
    ...b,
    zones: b.zones.map(z => {
      if (!z.outside) return z
      const n = edgeLength(b, z.outside.side)
      const end = Math.min(z.outside.end, n - 1)
      return { ...z, outside: { ...z.outside, start: Math.min(z.outside.start, end), end } }
    }),
  }
}

/**
 * 변 하나를 상자 줄로 — 가운데 칸(변 길이가 짝수면 2칸, 홀수면 3칸)을 상자 구역으로, 그 줄의
 * 나머지 칸은 금지로. 다른 구역에 속한 칸은 그 구역에 둔다(모서리를 두 상자 줄이 같이 쓸 때).
 */
export function withSideBox(b: BoardSpec, side: RobotSide, label: string): BoardSpec {
  const edge = edgeCells(b, side)
  const [start, end] = centerSpan(edge.length)
  const box = edge.slice(start, end + 1)
  const rest = edge.filter(c => !box.includes(c))
  const id = freeZoneId(b, side)
  const zones = b.zones.map(z => ({ ...z, cells: z.cells.filter(c => !box.includes(c)) }))
  const inZone = new Set(zones.flatMap(z => z.cells))
  const blocked = [...b.blocked.filter(c => !box.includes(c)),
    ...rest.filter(c => !inZone.has(c) && !b.blocked.includes(c))]
  return { ...b, blocked, zones: [...zones, { id, label, cells: box }] }
}
