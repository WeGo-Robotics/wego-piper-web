import { useMemo, useState } from 'react'
import { useT } from '../../i18n'
import type { TFn } from '../../i18n'
import BoardView from './BoardView'
import type { Hint, Plan, PlanEpisode, PlanWarning } from './types'

/**
 * 생성 직후 보여 줄 것 (feature/scenario-designer.md §7-5) — 종류별 목표 칸 히트맵·총량·변형·
 * 교차표·예상 시간·경고, 그리고 시도 목록과 미리보기.
 */

export function variantLabel(t: TFn, v: string): string {
  const m: Record<string, string> = {
    normal: t('정상'),
    recover_missed_grasp: t('회복: 빗나간 파지'),
    recover_dropped: t('회복: 떨어뜨림'),
    perturb: t('교란: 접근 중 옮기기'),
    empty_target: t('빈손: 지시한 것이 판에 없음'),
    wrong_bin_fix: t('잘못 담긴 것 바로잡기'),
    hard_layout: t('어려운 배치 (붙여 놓기)'),
  }
  return m[v] ?? v
}

function hintText(t: TFn, h: Hint, name: (id: string) => string, zone: (id: string) => string): string {
  switch (h.kind) {
    case 'missed_grasp':
      return t('시작 전: 그리퍼를 {obj} 옆 2~3cm 빗나간 자리에 두고 시작', { obj: name(h.obj) })
    case 'dropped':
      return t('시작 전: {obj} 를 {cell} 에 떨어진 것처럼 두고, 빈 그리퍼를 상자 쪽 경로 중간에 두고 시작', { obj: name(h.obj), cell: h.cell ?? '' })
    case 'perturb':
      return t('접근 중: {obj} 를 {to} 로 옮기기', { obj: name(h.obj), to: h.to ?? '' })
    case 'empty_target':
      return t('{obj} 가 판에 없습니다 — 원위치에서 기다리기', { obj: name(h.obj) })
    case 'wrong_bin_fix':
      return t('시작 전: {obj} 를 {from} 에서 {to} 로 옮겨 두기', { obj: name(h.obj), from: zone(h.from ?? ''), to: zone(h.to ?? '') })
  }
}

function warningText(t: TFn, w: PlanWarning, variantName: (v: string) => string): string {
  switch (w.code) {
    case 'combos_short':
      return t('칸 × 종류 조합 {combos}개보다 목표가 적습니다 ({targets}회) — 모든 조합을 못 채웁니다. 칸을 줄이거나 회수를 늘리세요',
        { combos: Number(w.combos), targets: Number(w.targets) })
    case 'uneven_types':
      return t('종류별 목표 수가 고르지 않습니다 — 실물 개수가 모자랄 수 있습니다')
    case 'small_round':
      return t('블록 끝에서 라운드가 작아졌습니다 ({size}개) — 블록 크기를 라운드 크기의 배수로 맞추면 없어집니다', { size: Number(w.size) })
    case 'variant_short':
      return t('{variant} 를 {missing}개 못 넣었습니다 — 넣을 자리가 없습니다', { variant: variantName(String(w.variant)), missing: Number(w.missing) })
    default:
      return w.code
  }
}

function duration(t: TFn, s: number): string {
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return h ? t('{h}시간 {m}분', { h, m }) : t('{m}분', { m })
}

export default function PlanSummary({ plan }: { plan: Plan }) {
  const t = useT()
  const s = plan.summary
  const [sel, setSel] = useState<PlanEpisode | null>(plan.episodes[0] ?? null)
  const [filter, setFilter] = useState('')
  const name = (id: string) => plan.catalog.find(i => i.id === id)?.label ?? id
  const zone = (id: string) => plan.board.zones.find(z => z.id === id)?.label || id
  const vName = (v: string) => variantLabel(t, v)
  const heatMax = Math.max(1, ...Object.values(s.target_counts).flatMap(c => Object.values(c)))
  const shown = useMemo(
    () => plan.episodes.filter(e => !filter || e.variant === filter),
    [plan.episodes, filter],
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        <span>{t('시도 {n}개', { n: s.episodes })}</span>
        <span>{t('라운드 {n}개', { n: s.rounds })}</span>
        <span>{t('블록 {n}개', { n: s.blocks })}</span>
        <span className="text-neutral-400">{t('예상 {time} (리셋 {n}번 포함)', { time: duration(t, s.est_s), n: s.resets })}</span>
        <span className="text-neutral-400">
          {t('라운드 크기')} {Object.entries(s.round_sizes).map(([k, v]) => `${k}×${v}`).join(' · ')}
        </span>
      </div>

      {s.warnings.length > 0 && (
        <ul className="space-y-1">
          {s.warnings.map((w, i) => (
            <li key={i} className="text-xs text-amber-400">⚠ {warningText(t, w, vName)}</li>
          ))}
        </ul>
      )}

      <div>
        <h4 className="text-xs font-semibold text-neutral-400 mb-1">
          {t('종류별 목표 칸 — 숫자는 그 칸에서 집는 횟수, 편차는 가장 많은 칸 − 가장 적은 칸')}
        </h4>
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))' }}>
          {plan.catalog.map(item => (
            <div key={item.id} className="rounded border border-neutral-700 p-2 space-y-1">
              <div className="flex items-center gap-1.5 text-xs">
                <span className="w-3 h-3 rounded-full" style={{ background: item.color }} />
                <span className="text-neutral-200">{item.label}</span>
                <span className="ml-auto text-neutral-500">
                  {t('{n}회 · 편차 {d} · 블록 편차 {b}', { n: s.type_totals[item.id] ?? 0, d: s.spread[item.id] ?? 0, b: s.block_spread[item.id] ?? 0 })}
                </span>
              </div>
              <BoardView board={plan.board} counts={s.target_counts[item.id]} heatColor={item.color}
                heatMax={heatMax} compact />
            </div>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5 text-xs">
        {Object.entries(s.variant_counts).map(([v, n]) => (
          <button key={v} onClick={() => setFilter(filter === v ? '' : v)}
            className={`px-2 py-0.5 rounded border ${filter === v ? 'border-blue-400 text-white' : 'border-neutral-700 text-neutral-300'}`}>
            {vName(v)} {n}
          </button>
        ))}
        {s.variants_off.length > 0 && (
          <span className="text-neutral-500 self-center">
            {t('이 설정에서 꺼진 변형: {list}', { list: s.variants_off.map(vName).join(', ') })}
          </span>
        )}
      </div>

      {Object.entries(s.crosstab).map(([factor, tab]) => (
        <table key={factor} className="text-xs border-collapse">
          <thead>
            <tr>
              <th className="px-2 py-0.5 text-left text-neutral-400">{factor}</th>
              {plan.catalog.map(i => <th key={i.id} className="px-2 py-0.5 text-neutral-400">{i.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {Object.entries(tab).map(([level, row]) => (
              <tr key={level}>
                <td className="px-2 py-0.5 text-neutral-300">{level}</td>
                {plan.catalog.map(i => <td key={i.id} className="px-2 py-0.5 text-center">{row[i.id] ?? 0}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      ))}

      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
        <div className="max-h-96 overflow-y-auto rounded border border-neutral-700">
          <table className="w-full text-xs">
            <tbody>
              {shown.map(ep => (
                <tr key={ep.id} onClick={() => setSel(ep)}
                  className={`cursor-pointer border-b border-neutral-800 ${sel?.id === ep.id ? 'bg-neutral-700' : 'hover:bg-neutral-800'}`}>
                  <td className="px-2 py-1 font-mono text-neutral-500 whitespace-nowrap">{ep.id}</td>
                  <td className="px-2 py-1">
                    {ep.picks.map((p, i) => (
                      <span key={i} className="whitespace-nowrap mr-2">
                        {name(p.obj)} {p.cell ?? (p.from_zone ? zone(p.from_zone) : '—')}
                        {p.dest ? ` → ${zone(p.dest)}` : ''}
                      </span>
                    ))}
                  </td>
                  <td className="px-2 py-1 text-neutral-400 whitespace-nowrap">
                    {ep.variant !== 'normal' && vName(ep.variant)}{ep.hard && ` · ${t('붙여 놓기')}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {sel && (
          <div className="space-y-2">
            <div className="text-xs text-neutral-400">
              <span className="font-mono">{sel.id}</span>
              {sel.reset && <span className="ml-2 text-sky-400">{t('새로 늘어놓기')}</span>}
              {Object.entries(sel.nuisance).map(([k, v]) => <span key={k} className="ml-2">{k}: {v}</span>)}
            </div>
            <BoardView board={plan.board} catalog={plan.catalog} layout={sel.layout} picks={sel.picks}
              inZones={sel.in_zones}
              perturbTo={sel.hint?.kind === 'perturb' && sel.hint.cell && sel.hint.to
                ? { from: sel.hint.cell, to: sel.hint.to } : undefined} />
            <p className="text-sm text-neutral-200 break-words">“{sel.task}”</p>
            {sel.hint && <p className="text-xs text-amber-300">{hintText(t, sel.hint, name, zone)}</p>}
          </div>
        )}
      </div>
    </div>
  )
}
