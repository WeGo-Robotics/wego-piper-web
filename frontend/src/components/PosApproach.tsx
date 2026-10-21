/**
 * POS분석의 접근 동작 — 파지 직전·닫는 동안 말단이 **얼마나 빨리 움직였나** (feature/episode-analyzer.md §6 A2).
 *
 * 보통은 멈춰 서서 닫는다. "잘 안 잡힐 것 같으면 앞으로 튀어나가며 집는" 시연 버릇은 정책이
 * 그대로 배워, 실기에서 팔이 접근하다 툭 튀는 동작으로 나온다. 그런 파지를 골라 보여준다.
 *
 * 튀는 파지 = 같은 데이터셋·같은 팔 안에서 **중앙값 + 3×MAD** 를 넘고 FLOOR 이상인 것.
 * 데이터셋마다 묶는 이유: 작업·조작자마다 평소 속도가 다르다(실측 접근 중앙값 17~56mm/s).
 * FLOOR 는 거의 멈춘 데이터에서 MAD 가 0 에 가까워 2~3mm/s 차이로 걸리는 것을 막는다.
 *
 * ⚠ **빈손 파지는 따로 표시한다.** 놓친 걸 알고 팔을 이미 옮기는 동안 그리퍼가 계속 닫히면
 *   "닫으며 움직임" 으로 크게 걸린다(실측 yeonwonju 371·360·338mm/s 가 그랬다). 튀어나가며
 *   **잡는** 버릇과는 다른 이야기라, 같은 줄에 섞여도 읽는 사람이 가를 수 있어야 한다.
 */
import { useMemo, useState } from 'react'
import { useT } from '../i18n'

export type ApproachEvent = {
  key: string
  ds: string
  arm: string | null
  episode: number
  cycle: number
  frame: number
  outcome: 'hold' | 'empty'
  approach_speed: number | null
  closing_speed: number | null
}

const FLOOR_MM_S = 40
const MAD_K = 3
const SHOW_FIRST = 10

const armOf = (e: ApproachEvent) => e.arm ?? 'single'
const median = (v: number[]) => {
  const s = [...v].sort((a, b) => a - b)
  const m = s.length >> 1
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}
const pct = (v: number[], p: number) => {
  const s = [...v].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.floor(p * s.length))]
}
function limit(v: number[]) {
  if (v.length === 0) return { med: 0, p90: 0, thr: Infinity }
  const med = median(v)
  const mad = median(v.map((x) => Math.abs(x - med))) * 1.4826
  return { med, p90: pct(v, 0.9), thr: Math.max(med + MAD_K * mad, FLOOR_MM_S) }
}

type Flag = { e: ApproachEvent; kind: 'closing' | 'approach'; value: number; ratio: number }

export default function PosApproach({ events, arms, armName, dsName, dsColor, selectedKey, onSelect, onOpen }: {
  events: ApproachEvent[]
  arms: string[]
  armName: (arm: string) => string
  dsName: (ds: string) => string
  dsColor: (ds: string) => string
  selectedKey: string | null
  onSelect: (key: string) => void
  onOpen: (ds: string, episode: number, frame: number) => void
}) {
  const t = useT()
  const [showAll, setShowAll] = useState(false)
  const split = arms.length > 1

  const { groups, flags } = useMemo(() => {
    const groups: {
      key: string; ds: string; arm: string; n: number
      ap: ReturnType<typeof limit>; cl: ReturnType<typeof limit>; flagged: number
    }[] = []
    const flags: Flag[] = []
    for (const a of arms) {
      const evA = events.filter((e) => armOf(e) === a)
      for (const ds of [...new Set(evA.map((e) => e.ds))]) {
        const evs = evA.filter((e) => e.ds === ds)
        const ap = limit(evs.map((e) => e.approach_speed).filter((x): x is number => x != null))
        const cl = limit(evs.map((e) => e.closing_speed).filter((x): x is number => x != null))
        let flagged = 0
        for (const e of evs) {
          // 한 파지는 한 번만 — 닫으며 튄 것이 먼저다(시연 버릇이 그쪽에서 보인다)
          if (e.closing_speed != null && e.closing_speed > cl.thr) {
            flags.push({ e, kind: 'closing', value: e.closing_speed, ratio: e.closing_speed / cl.thr }); flagged++
          } else if (e.approach_speed != null && e.approach_speed > ap.thr) {
            flags.push({ e, kind: 'approach', value: e.approach_speed, ratio: e.approach_speed / ap.thr }); flagged++
          }
        }
        groups.push({ key: `${a}|${ds}`, ds, arm: a, n: evs.length, ap, cl, flagged })
      }
    }
    flags.sort((x, y) => y.ratio - x.ratio)
    return { groups, flags }
  }, [events, arms])

  if (events.length === 0) return null
  const shown = showAll ? flags : flags.slice(0, SHOW_FIRST)

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 space-y-2 text-xs">
      <div className="text-neutral-400"
           title={t('같은 데이터셋·같은 팔 안에서 중앙값 + 3×MAD 를 넘고 40mm/s 이상이면 튀는 파지로 본다')}>
        {t('접근 동작 — 닫기 직전 0.5초와 닫는 동안의 말단 최고 속도')}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full tabular-nums whitespace-nowrap [&_td]:px-2 [&_th]:px-2">
          <thead className="text-neutral-500">
            <tr className="text-left">
              <th className="font-normal py-0.5">{t('데이터셋')}</th>
              {split && <th className="font-normal">{t('팔')}</th>}
              <th className="font-normal text-right">{t('파지')}</th>
              <th className="font-normal text-right">{t('접근 속도 중앙 · p90 (mm/s)')}</th>
              <th className="font-normal text-right">{t('닫는 중 속도 중앙 · p90 (mm/s)')}</th>
              <th className="font-normal text-right">{t('튀는 파지')}</th>
            </tr>
          </thead>
          <tbody className="text-neutral-300">
            {groups.map((g) => (
              <tr key={g.key} className="border-t border-neutral-800">
                <td className="py-0.5">
                  <span className="flex items-center gap-1.5" title={g.ds}>
                    <span className="h-2 w-2 rounded-full shrink-0" style={{ background: dsColor(g.ds) }} />
                    {dsName(g.ds)}
                  </span>
                </td>
                {split && <td className="text-neutral-400">{armName(g.arm)}</td>}
                <td className="text-right">{g.n}</td>
                <td className="text-right">{g.ap.med.toFixed(0)} · {g.ap.p90.toFixed(0)}</td>
                <td className="text-right">{g.cl.med.toFixed(0)} · {g.cl.p90.toFixed(0)}</td>
                <td className={`text-right ${g.flagged ? 'text-amber-400' : ''}`}>{g.flagged || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {flags.length > 0 && (
        <div className="space-y-0.5">
          <div className="text-neutral-500">{t('튀는 파지 — 많이 벗어난 순. 누르면 3D 에 그 접근 궤적이 그려진다')}</div>
          {shown.map(({ e, kind, value }) => (
            <div
              key={e.key}
              onClick={() => onSelect(e.key)}
              className={`flex cursor-pointer items-center gap-2 rounded px-2 py-0.5 hover:bg-neutral-800 ${
                selectedKey === e.key ? 'bg-neutral-800 ring-1 ring-neutral-600' : ''}`}
            >
              <span className="h-2 w-2 rounded-full shrink-0" style={{ background: dsColor(e.ds) }} />
              <span className="text-neutral-400 truncate max-w-[14rem]" title={e.ds}>{dsName(e.ds)}</span>
              {e.arm && <span className="text-neutral-500">{armName(e.arm)}</span>}
              <span className="font-mono text-neutral-300">ep {e.episode}</span>
              <span className="text-neutral-500">{t('{cycle}번째 파지', { cycle: e.cycle })}</span>
              {e.outcome === 'empty' && (
                <span className="text-neutral-400" title={t('놓친 뒤 팔을 옮기는 동안 그리퍼가 닫히고 있었을 수 있다 — 튀어나가며 잡은 것과 다르다')}>
                  {t('✕ 빈손')}
                </span>
              )}
              <span className={kind === 'closing' ? 'text-red-400' : 'text-amber-400'}>
                {kind === 'closing'
                  ? t('닫으며 움직임 {v}mm/s', { v: value.toFixed(0) })
                  : t('빠르게 들어옴 {v}mm/s', { v: value.toFixed(0) })}
              </span>
              <button
                onClick={(ev) => { ev.stopPropagation(); onOpen(e.ds, e.episode, e.frame) }}
                className="ml-auto text-neutral-500 hover:text-white"
              >
                {t('뷰어 →')}
              </button>
            </div>
          ))}
          {flags.length > SHOW_FIRST && (
            <button onClick={() => setShowAll((v) => !v)} className="px-2 text-neutral-500 hover:text-white">
              {showAll ? t('접기') : t('{n}개 더 보기', { n: flags.length - SHOW_FIRST })}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
