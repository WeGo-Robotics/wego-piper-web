/**
 * 검사 결과 그래프 — 관절끼리 **겹쳐 그린다.**
 *
 * ⚠ 이 검사의 전제는 "여섯이 같은 모션을 했으니 서로가 서로의 대조군" 이다.
 *   그러면 그래프도 겹쳐야 한다 — 나란히 놓으면 눈이 축을 오가며 비교해야 하고,
 *   그게 바로 표만 봐서는 안 보이던 차이다.
 *
 * 색은 dataviz 팔레트의 다크 카테고리 슬롯 1~6 (표면 #262626 에서 검증).
 */

const SLOTS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300']
const GRID = '#404040'
const INK = '#a3a3a3'

/** SVG 점 상한. 1361행을 그대로 그리면 path 가 쓸데없이 무겁다. */
const MAX_POINTS = 300

export type Row = Record<string, number | boolean | null>

const fmt = (v: number) =>
  Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3)

function thin<T>(rows: T[]): T[] {
  if (rows.length <= MAX_POINTS) return rows
  const step = rows.length / MAX_POINTS
  return Array.from({ length: MAX_POINTS }, (_, i) => rows[Math.floor(i * step)])
}

export default function DiagChart({ rows, joints, field, unit, title, zeroLine }: {
  rows: Row[]; joints: string[]; field: string; unit: string; title: string
  zeroLine?: boolean
}) {
  const pts = thin(rows)
  const width = 560
  const height = 150
  const pad = { l: 52, r: 84, t: 8, b: 18 }
  const w = width - pad.l - pad.r
  const h = height - pad.t - pad.b

  const series = joints.map((j, i) => ({
    joint: j, color: SLOTS[i % SLOTS.length],
    data: pts.map((r) => {
      const v = r[`${j}_${field}`]
      return typeof v === 'number' ? v : null
    }),
  })).filter((s) => s.data.some((v) => v != null))

  if (!series.length || pts.length < 2) return null

  const all = series.flatMap((s) => s.data).filter((v): v is number => v != null)
  let lo = Math.min(...all)
  let hi = Math.max(...all)
  if (zeroLine) { lo = Math.min(lo, 0); hi = Math.max(hi, 0) }
  const span = hi - lo || 1
  lo -= span * 0.08; hi += span * 0.08

  const x = (i: number) => pad.l + (i / (pts.length - 1)) * w
  const y = (v: number) => pad.t + h - ((v - lo) / (hi - lo)) * h
  const path = (d: (number | null)[]) => {
    let out = ''; let pen = false
    d.forEach((v, i) => {
      if (v == null) { pen = false; return }
      out += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`
      pen = true
    })
    return out
  }
  const tSpan = (rows[rows.length - 1]?.t_s as number) ?? 0

  return (
    <figure className="m-0">
      <figcaption className="mb-1 text-[10px] text-neutral-400">
        {title} <span className="text-neutral-600">({unit})</span>
      </figcaption>
      <svg width={width} height={height} role="img" aria-label={title}>
        {[hi, lo].map((v, i) => (
          <g key={i}>
            <line x1={pad.l} x2={pad.l + w} y1={pad.t + h * i} y2={pad.t + h * i}
                  stroke={GRID} strokeWidth={1} />
            <text x={pad.l - 4} y={pad.t + h * i + 3} textAnchor="end"
                  fill={INK} fontSize={9}>{fmt(v)}</text>
          </g>
        ))}
        {zeroLine && lo < 0 && hi > 0 && (
          <line x1={pad.l} x2={pad.l + w} y1={y(0)} y2={y(0)}
                stroke={GRID} strokeWidth={1} />
        )}
        <text x={pad.l} y={height - 5} fill={INK} fontSize={9}>0s</text>
        <text x={pad.l + w} y={height - 5} textAnchor="end" fill={INK} fontSize={9}>
          {tSpan.toFixed(0)}s
        </text>

        {series.map((s) => (
          <path key={s.joint} d={path(s.data)} fill="none" stroke={s.color}
                strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        ))}

        {/* ⚠ 2계열 이상이면 범례는 늘 있어야 한다 — 색만으로 정체를 나르면 안 된다.
            선 끝에 직접 붙여 눈이 범례와 그래프를 오가지 않게 한다. */}
        {series.map((s, i) => (
          <g key={`l-${s.joint}`}>
            <line x1={pad.l + w + 6} x2={pad.l + w + 16}
                  y1={pad.t + 10 + i * 12} y2={pad.t + 10 + i * 12}
                  stroke={s.color} strokeWidth={2} />
            <text x={pad.l + w + 20} y={pad.t + 13 + i * 12} fill={INK} fontSize={9}>
              {s.joint.replace('joint', 'J')}
            </text>
          </g>
        ))}
      </svg>
    </figure>
  )
}
