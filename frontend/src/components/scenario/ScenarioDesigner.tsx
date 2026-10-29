import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ComponentType } from 'react'
import { api } from '../../services/api'
import { useT } from '../../i18n'
import type { TFn } from '../../i18n'
import { usePolicies } from '../../hooks/usePolicies'
import PresetBar from '../PresetBar'
import BoardEditor from './BoardEditor'
import CatalogEditor from './CatalogEditor'
import PlanSummary, { variantLabel } from './PlanSummary'
import PrintPanel from './PrintPanel'
import SortFields, { sortDefaults } from './SortFields'
import type { TemplateFieldsProps } from './SortFields'
import type { Common, Design, Nuisance, Plan, TemplateSpec } from './types'

/**
 * 시나리오 설계기 (feature/scenario-designer.md) — 템플릿을 고르고, 작업판·물체·규칙·변형을
 * 채우고, 칸·종류·순서가 고른 시도 목록을 만들고, 작업판을 찍는다.
 *
 * 계획은 저장하지 않는다 — 같은 설정·시드면 같은 계획이 나온다. 저장하는 것은 **설정**(프리셋
 * `scenario`)이고, 계획은 녹화 때 데이터셋 사이드카에 실린다(2단계).
 */

type TemplateUi = {
  label: (t: TFn) => string
  description: (t: TFn) => string
  defaults: (t: TFn, spec: TemplateSpec) => Pick<Design, 'board' | 'catalog' | 'fields'>
  Fields: ComponentType<TemplateFieldsProps>
}

// 템플릿 화면 — 템플릿 YAML(`services/scenario/templates/`)과 짝. 화면이 없는 템플릿은 목록에만 뜬다.
const TEMPLATE_UI: Record<string, TemplateUi> = {
  sort: {
    label: t => t('분류'),
    description: t => t('물체를 속성(색·크기)에 따라 정해진 상자에 담는다'),
    defaults: sortDefaults,
    Fields: SortFields,
  },
}

const COMMON_DEFAULTS: Omit<Common, 'episode_unit'> = {
  episodes: 120, block_size: 20, composition: 'equal', fixed_composition: {}, min_spacing: 1,
  episode_s: 20, reset_s: 15, nuisance: [], policy_type: '', variants: {},
}

// 마지막 설계 — 수집 화면의 폼(`piper_record_settings`)과 같은 자리(브라우저)에 둔다. 이름 붙여 공유할
// 것은 프리셋으로, 이건 "새로고침해도 하던 것 그대로" 를 위한 것이다.
const STORE_KEY = 'piper_scenario_design'

function loadStored(): unknown {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || 'null') } catch { return null }
}

/** 저장된 설계(프리셋·마지막 값)를 지금 템플릿 기본값 위에 얹는다 — 예전 모양에 없던 값은 기본값으로.
 *  템플릿을 못 찾으면 null. */
function mergeDesign(t: TFn, specs: TemplateSpec[], raw: unknown): Design | null {
  const d = raw as Partial<Design> | null
  const s = d && specs.find(x => x.type === d.template)
  if (!d || !s || !TEMPLATE_UI[s.type]) return null
  const base = makeDesign(t, s)
  return {
    ...base, ...d,
    board: d.board ? { ...base.board, ...d.board } : base.board,
    catalog: Array.isArray(d.catalog) && d.catalog.length ? d.catalog : base.catalog,
    common: { ...base.common, ...d.common },
    fields: { ...base.fields, ...d.fields },
  }
}

function makeDesign(t: TFn, spec: TemplateSpec): Design {
  return {
    template: spec.type,
    seed: 1,
    ...TEMPLATE_UI[spec.type].defaults(t, spec),
    common: { ...COMMON_DEFAULTS, episode_unit: spec.episode_units[0] },
  }
}

const card = 'rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3'
const field = 'px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100'
const input = `w-full ${field}`

function NuisanceEditor({ items, onChange }: { items: Nuisance[]; onChange: (n: Nuisance[]) => void }) {
  const t = useT()
  return (
    <div className="space-y-1.5">
      {items.map((n, i) => (
        <div key={i} className="grid grid-cols-[8rem_1fr_2rem] gap-1.5">
          <input value={n.name} placeholder={t('이름 (예: 시연자)')} className={input}
            onChange={e => onChange(items.map((x, k) => k === i ? { ...x, name: e.target.value } : x))} />
          <input value={n.levels.join(', ')} placeholder={t('값 (쉼표로 — 예: 민수, 지영)')} className={input}
            onChange={e => onChange(items.map((x, k) => k === i
              ? { ...x, levels: e.target.value.split(',').map(s => s.trim()) } : x))} />
          <button onClick={() => onChange(items.filter((_, k) => k !== i))}
            className="text-neutral-500 hover:text-red-400 text-xs">✕</button>
        </div>
      ))}
      {items.length < 4 && (
        <button onClick={() => onChange([...items, { name: '', levels: [] }])}
          className="px-2 py-1 rounded text-xs border border-dashed border-neutral-600 text-neutral-400 hover:text-white">
          {t('+ 성가신 변수')}
        </button>
      )}
    </div>
  )
}

export default function ScenarioDesigner() {
  const t = useT()
  const { policies } = usePolicies()
  const [specs, setSpecs] = useState<TemplateSpec[]>([])
  const [design, setDesign] = useState<Design | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [planKey, setPlanKey] = useState('')
  const [presetName, setPresetName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.get<TemplateSpec[]>('/scenario/templates')
      .then(list => {
        setSpecs(list)
        const first = list.find(s => TEMPLATE_UI[s.type])
        setDesign(d => d ?? mergeDesign(t, list, loadStored()) ?? (first ? makeDesign(t, first) : null))
      })
      .catch(e => setErr((e as Error).message))
    // 처음 한 번만 — 기본 설계의 이름(t)은 그때 언어로 정해지고 이후엔 데이터다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!design) return
    try { localStorage.setItem(STORE_KEY, JSON.stringify(design)) } catch { /* 사생활 모드 — 이번 세션엔 그대로 쓴다 */ }
  }, [design])

  // [처음 값으로] 는 두 번 눌러야 한다 — 기억해 둔 설계를 한 번에 날리지 않게. window.confirm 은 쓰지 않는다
  // (E-stop heartbeat 를 막는다)
  const [confirmReset, setConfirmReset] = useState(false)
  useEffect(() => {
    if (!confirmReset) return
    const id = setTimeout(() => setConfirmReset(false), 3000)
    return () => clearTimeout(id)
  }, [confirmReset])

  const spec = specs.find(s => s.type === design?.template)
  const ui = design ? TEMPLATE_UI[design.template] : undefined

  const setFields = useCallback((patch: Record<string, unknown>) =>
    setDesign(d => d && { ...d, fields: { ...d.fields, ...patch } }), [])
  const setCommon = useCallback((patch: Partial<Common>) =>
    setDesign(d => d && { ...d, common: { ...d.common, ...patch } }), [])

  const policy = policies.find(p => p.type === design?.common.policy_type)
  const language = policy ? policy.language : null

  const values = useMemo(() => {
    if (!design) return null
    const ids = new Set(design.catalog.map(i => i.id))
    const fixed = Object.fromEntries(
      Object.entries(design.common.fixed_composition).filter(([k, v]) => ids.has(k) && v > 0))
    return {
      ...design.fields,
      ...design.common,
      fixed_composition: fixed,
      nuisance: design.common.nuisance.map(n => ({ name: n.name, levels: n.levels.filter(Boolean) })),
    }
  }, [design])
  const key = JSON.stringify([design?.template, design?.seed, design?.board, design?.catalog, values])

  const generate = async () => {
    if (!design || !values) return
    setBusy(true)
    setErr('')
    try {
      const p = await api.post<Plan>('/scenario/generate', {
        template: design.template, seed: design.seed, board: design.board, catalog: design.catalog, values,
      }, { timeoutMs: 60_000 })
      setPlan(p)
      setPlanKey(key)
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (!design || !spec || !ui) {
    return (
      <div className={card}>
        <p className="text-sm text-neutral-400">{err || t('템플릿을 불러오는 중...')}</p>
      </div>
    )
  }

  const varValues: Record<string, string> = Object.fromEntries(
    Object.entries({ ...design.fields, episode_unit: design.common.episode_unit }).map(([k, v]) => [k, String(v)]))
  const Fields = ui.Fields

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex overflow-hidden rounded-lg border border-neutral-700 text-sm">
          {specs.map(s => {
            const u = TEMPLATE_UI[s.type]
            return (
              <button key={s.type} disabled={!u}
                onClick={() => { if (u && s.type !== design.template) { setDesign(makeDesign(t, s)); setPlan(null) } }}
                title={u ? u.description(t) : t('이 템플릿의 화면이 아직 없습니다')}
                className={`px-4 py-1.5 ${design.template === s.type
                  ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white disabled:opacity-40'}`}>
                {u ? u.label(t) : s.type}
              </button>
            )
          })}
        </div>
        <span className="text-xs text-neutral-500">{ui.description(t)}</span>
        <button
          onClick={() => {
            if (!confirmReset) { setConfirmReset(true); return }
            setConfirmReset(false)
            setDesign(makeDesign(t, spec))
            setPlan(null)
          }}
          title={t('마지막 설계는 이 브라우저에 기억됩니다 — 이름 붙여 남기려면 프리셋으로 저장하세요')}
          className={`px-2 py-1 rounded text-xs border ${confirmReset
            ? 'border-red-500 text-red-300' : 'border-neutral-700 text-neutral-400 hover:text-white'}`}>
          {confirmReset ? t('한 번 더 누르면 지금 설계를 지웁니다') : t('처음 값으로')}
        </button>
        <div className="ml-auto">
          <PresetBar domain="scenario" scope="shared" values={() => design as unknown as Record<string, unknown>}
            onSelect={setPresetName}
            onApply={v => {
              const d = mergeDesign(t, specs, v)
              if (!d) { setErr(t('이 설정의 템플릿을 찾을 수 없습니다: {name}', { name: String(v.template) })); return }
              setDesign(d)
              setPlan(null)
            }} />
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <div className={card}>
          <h3 className="text-sm font-semibold">{t('작업판')}</h3>
          <BoardEditor board={design.board} onChange={board => setDesign({ ...design, board })} />
        </div>
        <div className="space-y-4">
          <div className={card}>
            <h3 className="text-sm font-semibold">{t('물체')}</h3>
            <p className="text-xs text-neutral-500">{t('위에 있을수록 "물체 목록 순서" 규칙에서 먼저 집습니다. 실물 개수는 라운드 하나에 한 번에 놓을 수 있는 최대치입니다.')}</p>
            <CatalogEditor items={design.catalog} onChange={catalog => setDesign({ ...design, catalog })} />
          </div>
          <div className={card}>
            <h3 className="text-sm font-semibold">{t('{template} 설정', { template: ui.label(t) })}</h3>
            <Fields spec={spec} fields={design.fields} setFields={setFields} common={design.common}
              setCommon={setCommon} board={design.board} catalog={design.catalog} language={language}
              policyLabel={policy?.label ?? ''} />
          </div>
        </div>
      </div>

      <div className={card}>
        <h3 className="text-sm font-semibold">{t('계획')}</h3>
        <div className="grid gap-3 grid-cols-2 md:grid-cols-4">
          <label className="text-xs text-neutral-400">{t('시도 수 (에피소드)')}
            <input type="number" min={1} max={5000} value={design.common.episodes} className={input}
              onChange={e => setCommon({ episodes: Math.max(1, Number(e.target.value) || 1) })} />
          </label>
          <label className="text-xs text-neutral-400" title={t('블록마다 따로 균형을 맞춥니다 — 중간에 멈춰도 그때까지가 고릅니다')}>
            {t('블록 크기')}
            <input type="number" min={1} max={500} value={design.common.block_size} className={input}
              onChange={e => setCommon({ block_size: Math.max(1, Number(e.target.value) || 1) })} />
          </label>
          <label className="text-xs text-neutral-400">{t('시드')}
            <div className="flex gap-1">
              <input type="number" value={design.seed} className={input}
                onChange={e => setDesign({ ...design, seed: Number(e.target.value) || 0 })} />
              <button onClick={() => setDesign({ ...design, seed: Math.floor(Math.random() * 100000) })}
                title={t('다른 시드')} className="px-2 rounded bg-neutral-700 hover:bg-neutral-600">🎲</button>
            </div>
          </label>
          <label className="text-xs text-neutral-400">{t('쓸 정책')}
            <select value={design.common.policy_type} className={input}
              onChange={e => setCommon({ policy_type: e.target.value })}>
              <option value="">{t('정하지 않음')}</option>
              {policies.map(p => (
                <option key={p.type} value={p.type}>
                  {p.label}{p.language ? '' : ` — ${t('지시문 안 봄')}`}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-neutral-400">{t('최소 간격')}
            <select value={design.common.min_spacing} className={input}
              onChange={e => setCommon({ min_spacing: Number(e.target.value) })}>
              <option value={1}>{t('이웃 칸 허용')}</option>
              <option value={2}>{t('한 칸 띄움')}</option>
              <option value={3}>{t('두 칸 띄움')}</option>
            </select>
          </label>
          <label className="text-xs text-neutral-400">{t('에피소드 시간 (초)')}
            <input type="number" min={1} value={design.common.episode_s} className={input}
              onChange={e => setCommon({ episode_s: Math.max(1, Number(e.target.value) || 1) })} />
          </label>
          <label className="text-xs text-neutral-400">{t('리셋 시간 (초)')}
            <input type="number" min={0} value={design.common.reset_s} className={input}
              onChange={e => setCommon({ reset_s: Math.max(0, Number(e.target.value) || 0) })} />
          </label>
        </div>

        <div className="space-y-1.5">
          <h4 className="text-xs font-semibold text-neutral-400">{t('종류 총량 맞추기')}</h4>
          <div className="flex flex-wrap gap-4 text-sm">
            {([['equal', t('종류 총량 같게 (기본)')], ['proportional', t('실물 비율대로')], ['fixed', t('라운드 구성 고정')]] as const)
              .map(([k, label]) => (
                <label key={k} className="flex items-center gap-1.5 cursor-pointer">
                  <input type="radio" name="composition" checked={design.common.composition === k}
                    onChange={() => setCommon({ composition: k })} className="accent-blue-500" />
                  {label}
                </label>
              ))}
          </div>
          {design.common.composition === 'fixed' && (
            <div className="flex flex-wrap gap-3">
              {design.catalog.map(i => (
                <label key={i.id} className="flex items-center gap-1.5 text-xs text-neutral-300">
                  <span className="w-3 h-3 rounded-full" style={{ background: i.color }} />
                  {i.label}
                  <input type="number" min={0} max={i.count} value={design.common.fixed_composition[i.id] ?? 0}
                    className={`${field} w-16 shrink-0`}
                    onChange={e => setCommon({ fixed_composition: {
                      ...design.common.fixed_composition,
                      [i.id]: Math.max(0, Math.min(i.count, Number(e.target.value) || 0)) } })} />
                </label>
              ))}
            </div>
          )}
        </div>

        <div className="space-y-1.5">
          <h4 className="text-xs font-semibold text-neutral-400">{t('실패·변형 비율')}</h4>
          <div className="grid gap-x-4 gap-y-1 md:grid-cols-2">
            {Object.entries(spec.variants).map(([k, v]) => {
              const on = Object.entries(v.requires).every(([rk, rv]) => varValues[rk] === rv)
              const ratio = design.common.variants[k] ?? v.default
              return (
                <label key={k} className={`flex items-center gap-2 text-sm ${on ? '' : 'opacity-40'}`}
                  title={on ? '' : t('이 설정에서는 쓸 수 없습니다')}>
                  <input type="number" min={0} max={50} step={1} value={Math.round(ratio * 100)} disabled={!on}
                    className={`${field} w-16 shrink-0`}
                    onChange={e => setCommon({ variants: {
                      ...design.common.variants,
                      [k]: Math.max(0, Math.min(50, Number(e.target.value) || 0)) / 100 } })} />
                  <span className="text-neutral-400">%</span>
                  <span className="text-neutral-200">{variantLabel(t, k)}</span>
                  {v.per === 'round' && <span className="text-xs text-neutral-500">{t('(라운드마다)')}</span>}
                </label>
              )
            })}
          </div>
          <p className="text-xs text-neutral-500">
            {t('회복 시연은 "빗나간 뒤부터" 찍습니다 — 일부러 빗나가는 앞부분까지 녹화하면 정책이 빗나가는 동작도 배웁니다.')}
          </p>
        </div>

        <div className="space-y-1.5">
          <h4 className="text-xs font-semibold text-neutral-400"
            title={t('조명·바닥천·시연자처럼 목표와 상관없어야 하는 것 — 목표 종류와 교차로 맞춥니다')}>
            {t('성가신 변수 (선택)')}
          </h4>
          <NuisanceEditor items={design.common.nuisance} onChange={nuisance => setCommon({ nuisance })} />
        </div>

        <div className="flex items-center gap-3">
          <button onClick={generate} disabled={busy}
            className="px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-sm text-white disabled:opacity-50">
            {busy ? t('만드는 중...') : t('계획 만들기')}
          </button>
          {plan && planKey !== key && (
            <span className="text-xs text-amber-400">{t('설정이 바뀌었습니다 — 다시 만드세요')}</span>
          )}
          {err && <span className="text-xs text-red-400">{err}</span>}
        </div>
      </div>

      {plan && (
        <div className={card}>
          <h3 className="text-sm font-semibold">{t('계획 결과')}</h3>
          <PlanSummary plan={plan} />
        </div>
      )}

      <div className={card}>
        <h3 className="text-sm font-semibold">{t('작업판 인쇄')}</h3>
        <PrintPanel board={design.board} title={presetName} />
      </div>
    </div>
  )
}
