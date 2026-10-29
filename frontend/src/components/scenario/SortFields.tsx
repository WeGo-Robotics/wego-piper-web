import { useEffect } from 'react'
import { useT } from '../../i18n'
import type { TFn } from '../../i18n'
import type { BoardSpec, CatalogItem, Common, Design, TemplateSpec } from './types'
import { withSideBox } from './types'
import { sideName } from './BoardEditor'

/**
 * 분류 템플릿의 화면 (feature/scenario-designer.md §5).
 *
 * 값의 모양(기본값·선택지)은 `services/scenario/templates/sort.yaml` 이 정하고, 문구는 여기
 * `t()` 가 갖는다 — YAML 문자열은 번역 추출 대상이 아니다.
 */

export type SortRule = { obj: string; dest: string; task: string }

export type TemplateFieldsProps = {
  spec: TemplateSpec
  fields: Record<string, unknown>
  setFields: (patch: Record<string, unknown>) => void
  common: Common
  setCommon: (patch: Partial<Common>) => void
  board: BoardSpec
  catalog: CatalogItem[]
  /** 고른 정책이 지시문을 보나 — 정책을 안 골랐으면 null */
  language: boolean | null
  policyLabel: string
}

const field = 'px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100'
const input = `w-full ${field}`
// 색이 겹치면 어느 쪽이 이길지 모른다(w-full 대 w-16 처럼) — 테두리·글자색을 바꾼 바탕을 따로 둔다
const missingField = 'w-full px-2 py-1 rounded bg-neutral-900 border border-red-500 text-sm text-red-300'

export function sortDefaults(t: TFn, spec: TemplateSpec): Pick<Design, 'board' | 'catalog' | 'fields'> {
  const fields: Record<string, unknown> = {}
  for (const [k, f] of Object.entries(spec.fields)) fields[k] = f.default
  // task 문구는 **데이터**다 — 화면 언어가 아니라 정책이 읽기 좋은 말로. 기본은 영어
  // (SmolVLA·π0 의 VLM 이 영어로 학습됐다). 바꾸려면 사람이 고친다.
  fields.rules = [
    { obj: 'red', dest: 'bin_left', task: 'put the red apple in the left box' },
    { obj: 'yellow', dest: 'bin_right', task: 'put the yellow apple in the right box' },
    { obj: 'small', dest: 'bin_top', task: 'put the small apple in the top box' },
  ] satisfies SortRule[]
  fields.all_task = 'sort the apples into the boxes'
  // 로봇은 아래, 상자는 나머지 세 변 — 가운데 5×4 = 20칸이 물체 자리다
  let board: BoardSpec = { cols: 7, rows: 5, cell_mm: 80, robot_gap_mm: 100, robot_side: 'bottom', blocked: [], zones: [] }
  for (const side of ['left', 'top', 'right'] as const) {
    board = withSideBox(board, side, t('{side} 상자', { side: sideName(t, side) }))
  }
  return {
    board,
    catalog: [
      { id: 'red', label: t('빨간 사과'), color: '#d62728', size_mm: 75, count: 3 },
      { id: 'yellow', label: t('노란 사과'), color: '#e6c229', size_mm: 75, count: 3 },
      { id: 'small', label: t('작은 사과'), color: '#f97316', size_mm: 55, count: 2 },
    ],
    fields,
  }
}

function Choice({ name, value, current, onPick, title, desc, disabled = false }: {
  name: string; value: string; current: string; onPick: (v: string) => void
  title: string; desc?: string; disabled?: boolean
}) {
  return (
    <label className={`flex gap-2 items-start text-sm ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}>
      <input type="radio" name={name} checked={current === value} disabled={disabled}
        onChange={() => onPick(value)} className="mt-1 accent-blue-500" />
      <span>
        <span className="text-neutral-200">{title}</span>
        {desc && <span className="block text-xs text-neutral-500">{desc}</span>}
      </span>
    </label>
  )
}

export default function SortFields({
  fields, setFields, common, setCommon, board, catalog, language, policyLabel,
}: TemplateFieldsProps) {
  const t = useT()
  const unit = common.episode_unit || 'one'
  const style = String(fields.round_style ?? 'chain')
  const targetBy = String(fields.target_by ?? 'instruction')
  const rules = (fields.rules as SortRule[] | undefined) ?? []
  const noLanguage = language === false

  // 정책이 지시문을 안 보면 지시문 모드를 쓸 수 없다 — 백엔드도 거절한다(같은 장면·다른 목표 = 모순 데이터)
  useEffect(() => {
    if (noLanguage && unit === 'one' && targetBy === 'instruction') {
      setFields({ target_by: 'rule', round_style: 'chain' })
    }
  }, [noLanguage, unit, targetBy, setFields])

  const ruleOf = (obj: string): SortRule =>
    rules.find(r => r.obj === obj) ?? { obj, dest: board.zones[0]?.id ?? '', task: '' }

  // 규칙은 물체 목록을 따라간다 — 물체를 더하거나 id 를 바꾸면 규칙 줄도 같이
  const ids = catalog.map(i => i.id).join('\n')
  const ruleIds = rules.map(r => r.obj).join('\n')
  useEffect(() => {
    if (ids !== ruleIds) setFields({ rules: catalog.map(i => ruleOf(i.id)) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids, ruleIds])
  const destMissing = (r: SortRule) => !board.zones.some(z => z.id === r.dest)
  const anyMissing = catalog.some(i => destMissing(ruleOf(i.id)))
  const setRule = (obj: string, patch: Partial<SortRule>) => {
    const next = catalog.map(i => (i.id === obj ? { ...ruleOf(obj), ...patch } : ruleOf(i.id)))
    setFields({ rules: next })
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <h4 className="text-xs font-semibold text-neutral-400">{t('에피소드 하나에 담는 것')}</h4>
        <Choice name="sortUnit" value="one" current={unit} onPick={v => setCommon({ episode_unit: v })}
          title={t('하나씩 (기본)')}
          desc={t('물체 n개를 놓고 하나씩 담는다 — 담고 나면 그 자체가 다음 배치라 사이에 리셋이 없다')} />
        <Choice name="sortUnit" value="all" current={unit} onPick={v => setCommon({ episode_unit: v })}
          title={t('여러 개 연속 분류')}
          desc={t('라운드 전체를 순서 규칙대로 분류하는 긴 에피소드 하나')} />
      </div>

      {unit === 'one' && (
        <div className="space-y-1.5">
          <h4 className="text-xs font-semibold text-neutral-400">{t('목표 고르기')}</h4>
          <Choice name="sortTarget" value="instruction" current={targetBy} disabled={noLanguage}
            onPick={v => setFields({ target_by: v })}
            title={t('지시문')} desc={t('어느 것을 집을지 task 문구가 정한다 — 지시문을 보는 정책 전용')} />
          <Choice name="sortTarget" value="rule" current={targetBy}
            onPick={v => setFields({ target_by: v, round_style: 'chain' })}
            title={t('순서 규칙')} desc={t('장면만으로 정해진다 — 시연자가 매번 같은 규칙을 지켜야 한다')} />
          {noLanguage && (
            <p className="text-xs text-amber-400">
              {t('{policy} 는 task 문구를 보지 않습니다 — 같은 장면에서 지시만 바꾸면 모순 데이터가 되므로 순서 규칙만 됩니다', { policy: policyLabel })}
            </p>
          )}
        </div>
      )}

      {(unit === 'all' || targetBy === 'rule') && (
        <label className="block text-xs text-neutral-400">{t('순서 규칙')}
          <select value={String(fields.order_rule ?? 'type_priority')} className={input}
            onChange={e => setFields({ order_rule: e.target.value })}>
            <option value="type_priority">{t('물체 목록 순서대로 (같은 종류는 가까운 순)')}</option>
            <option value="nearest">
              {(board.robot_side ?? 'top') === 'top' || board.robot_side === 'bottom'
                ? t('로봇에서 가까운 순 (같으면 왼쪽부터)') : t('로봇에서 가까운 순 (같으면 위쪽부터)')}
            </option>
          </select>
        </label>
      )}

      {unit === 'one' && targetBy === 'instruction' && (
        <div className="space-y-1.5">
          <h4 className="text-xs font-semibold text-neutral-400">{t('라운드 모양')}</h4>
          <Choice name="sortStyle" value="chain" current={style} onPick={v => setFields({ round_style: v })}
            title={t('연쇄')} desc={t('담은 것은 상자에 남고 다음 목표로 — 라운드 크기만큼 에피소드')} />
          <Choice name="sortStyle" value="restore" current={style} onPick={v => setFields({ round_style: v })}
            title={t('같은 배치에서 목표만 바꾸기')}
            desc={t('담은 것을 되돌려 놓고 같은 배치에서 다른 것을 집는다 — 모델이 목표 입력을 무시하지 못하게')} />
          {style === 'restore' && (
            <label className="block text-xs text-neutral-400 pl-6">{t('배치 하나에서 바꿀 목표 수')}
              <input type="number" min={1} max={10} value={Number(fields.targets_per_layout ?? 3)} className={`${field} block w-24`}
                onChange={e => setFields({ targets_per_layout: Math.max(1, Math.min(10, Number(e.target.value) || 1)) })} />
            </label>
          )}
        </div>
      )}

      {common.composition !== 'fixed' && (
        <label className="block text-xs text-neutral-400">{t('라운드에 놓는 물체 수 (쉼표로 여럿 — 번갈아 쓴다)')}
          <input value={String(fields.round_sizes ?? '')} className={input} placeholder="3,5"
            onChange={e => setFields({ round_sizes: e.target.value })} />
        </label>
      )}

      {unit === 'all' && (
        <label className="block text-xs text-neutral-400">{t('task 문구 (데이터 — 화면 언어와 무관)')}
          <input value={String(fields.all_task ?? '')} className={input}
            onChange={e => setFields({ all_task: e.target.value })} />
        </label>
      )}

      <div className="space-y-1.5">
        <h4 className="text-xs font-semibold text-neutral-400">{t('분류 규칙')}</h4>
        {board.zones.length === 0 && (
          <p className="text-xs text-amber-400">{t('작업판에 구역(상자 자리)이 없습니다 — 작업판에서 구역을 먼저 만드세요')}</p>
        )}
        {board.zones.length > 0 && anyMissing && (
          <p className="text-xs text-red-400">{t('지운 상자를 가리키는 규칙이 있습니다 — 빨간 칸에서 담을 상자를 다시 고르세요')}</p>
        )}
        {catalog.map(item => {
          const r = ruleOf(item.id)
          return (
            <div key={item.id} className="grid grid-cols-[8rem_9rem_1fr] gap-1.5 items-center">
              <span className="flex items-center gap-1.5 text-sm text-neutral-200 truncate">
                <span className="w-3 h-3 rounded-full shrink-0" style={{ background: item.color }} />
                {item.label}
              </span>
              {/* ⚠ 지운 구역을 가리키는 규칙을 첫 항목으로 보여 주면 안 된다 — 컨트롤드 select 는 값이 목록에
                  없으면 첫 항목을 그려서, 화면엔 "→ 특" 인데 실제 값은 지운 bin_left 였다(실기 2026-10-29) */}
              <select value={destMissing(r) ? '' : r.dest}
                className={destMissing(r) ? missingField : input}
                onChange={e => setRule(item.id, { dest: e.target.value })}>
                {destMissing(r) && <option value="" disabled>{t('담을 상자를 고르세요')}</option>}
                {board.zones.map(z => <option key={z.id} value={z.id}>→ {z.label || z.id}</option>)}
              </select>
              {unit === 'one' ? (
                <input value={r.task} className={input} placeholder={t('task 문구 (데이터 — 화면 언어와 무관)')}
                  onChange={e => setRule(item.id, { task: e.target.value })} />
              ) : <span />}
            </div>
          )
        })}
      </div>
    </div>
  )
}
