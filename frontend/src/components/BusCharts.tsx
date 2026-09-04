/**
 * 버스별 작은 차트 두 개 — 트래픽(선)과 오류(막대).
 *
 * ⚠ **누적값은 그리지 않는다.** 카운터는 단조증가라 선이 언제나 우상향이고
 *   아무것도 안 말한다. 그릴 값은 **초당 증가량**이다 — 그래야 "지금 이 버스가
 *   바쁜가 / 오류가 나고 있나" 가 보인다.
 *
 * 색은 dataviz 팔레트의 다크 슬롯을 우리 표면(#262626)에 대고 검증한 것이다:
 * 파랑 #3987e5 · 주황 #d95926 — 명도대비·CVD 분리 전부 통과.
 */

export type Point = { t: number; rx: number; tx: number }

const RX = '#3987e5'   // 카테고리 슬롯 1
const TX = '#d95926'   // 카테고리 슬롯 2
const ERR = '#fab219'  // 상태: warning (표면 대비 8.25:1)
const GRID = '#404040'
const INK = '#a3a3a3'

const fmt = (v: number) =>
  v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v >= 10 ? v.toFixed(0) : v.toFixed(1)

// ── 트래픽 (선) ─────────────────────────────────────────────────────────────

export function TrafficChart({ points, width = 300, height = 92 }: {
  points: Point[]; width?: number; height?: number
}) {
  const pad = { l: 34, r: 8, t: 8, b: 14 }
  const w = width - pad.l - pad.r
  const h = height - pad.t - pad.b

  if (points.length < 2) {
    return (
      <div className="flex items-center justify-center rounded bg-neutral-900/60 text-xs text-neutral-600"
           style={{ width, height }}>
        표본이 모이면 그려집니다 — 자동 새로고침을 켜세요
      </div>
    )
  }

  // ⚠ **두 계열을 한 축에 둔다.** 같은 단위(패킷/초)이므로 축을 둘로 쪼개면
  //   크기 비교가 거짓이 된다. TX 가 바닥에 붙어 보이는 것은 사실이 그래서다 —
  //   그 대신 최신값을 숫자로 직접 붙여 작은 계열도 읽히게 한다.
  const max = Math.max(1, ...points.flatMap((p) => [p.rx, p.tx]))
  const x = (i: number) => pad.l + (i / (points.length - 1)) * w
  const y = (v: number) => pad.t + h - (v / max) * h
  const path = (key: 'rx' | 'tx') =>
    points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ')

  const last = points[points.length - 1]
  return (
    <svg width={width} height={height} role="img"
         aria-label={`초당 패킷 — RX ${fmt(last.rx)}, TX ${fmt(last.tx)}`}>
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line x1={pad.l} x2={pad.l + w} y1={pad.t + h * f} y2={pad.t + h * f}
                stroke={GRID} strokeWidth={1} />
          <text x={pad.l - 4} y={pad.t + h * f + 3} textAnchor="end"
                fill={INK} fontSize={9}>{fmt(max * (1 - f))}</text>
        </g>
      ))}
      <path d={path('rx')} fill="none" stroke={RX} strokeWidth={2}
            strokeLinejoin="round" strokeLinecap="round" />
      <path d={path('tx')} fill="none" stroke={TX} strokeWidth={2}
            strokeLinejoin="round" strokeLinecap="round" />
      <title>{`RX ${fmt(last.rx)}/s · TX ${fmt(last.tx)}/s`}</title>
    </svg>
  )
}

// ── 오류 (막대) ─────────────────────────────────────────────────────────────

export function ErrorChart({ counters, labels, width = 300, height = 92 }: {
  counters: Record<string, number>; labels: Record<string, string>
  width?: number; height?: number
}) {
  const entries = Object.entries(counters)
  const pad = { l: 34, r: 8, t: 8, b: 22 }
  const w = width - pad.l - pad.r
  const h = height - pad.t - pad.b
  const max = Math.max(1, ...entries.map(([, v]) => v))
  // 2px 간격 — 막대끼리 붙으면 경계가 값으로 읽힌다
  const slot = w / entries.length
  const bw = Math.max(4, slot - 2)

  return (
    <svg width={width} height={height} role="img" aria-label="오류 카운터">
      <line x1={pad.l} x2={pad.l + w} y1={pad.t + h} y2={pad.t + h}
            stroke={GRID} strokeWidth={1} />
      <text x={pad.l - 4} y={pad.t + 7} textAnchor="end" fill={INK} fontSize={9}>
        {fmt(max)}
      </text>
      {entries.map(([k, v], i) => {
        const bh = (v / max) * h
        const bx = pad.l + i * slot + (slot - bw) / 2
        return (
          <g key={k}>
            {/* 값이 0 이어도 자리를 남긴다 — 빈칸과 0 은 다르다 */}
            <rect x={bx} y={pad.t + h - Math.max(bh, v ? 2 : 0)} width={bw}
                  height={Math.max(bh, v ? 2 : 0)} rx={2}
                  fill={v ? ERR : 'transparent'} />
            <text x={bx + bw / 2} y={height - 12} textAnchor="middle"
                  fill={INK} fontSize={8}>{(labels[k] ?? k).slice(0, 4)}</text>
            <text x={bx + bw / 2} y={height - 3} textAnchor="middle"
                  fill={v ? '#e5e5e5' : '#525252'} fontSize={8}>{v}</text>
            <title>{`${labels[k] ?? k}: ${v}`}</title>
          </g>
        )
      })}
    </svg>
  )
}
