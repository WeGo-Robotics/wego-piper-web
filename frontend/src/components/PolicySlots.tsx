import { useState } from 'react'
import { useT } from '../i18n'
import type { Model } from '../types/models'
import type { ExtraPolicy, SlotProblems } from '../types/policySlots'
import CheckpointLabel from './CheckpointLabel'
import PickerField from './PickerField'
import WeightTableModal from './WeightTableModal'

/**
 * 한 추론에 정책을 여럿 올린다 (feature/multi-policy.md) — 준비 화면의 고르기(`ExtraPoliciesEditor`)와
 * 실행 중 바꾸기(`PolicyBar`). 판정·요청은 types/policySlots.ts.
 *
 * 정책이 하나면 둘 다 거의 안 보인다: 준비 화면엔 [+ 함께 올릴 정책] 한 줄, 실행 화면엔 아무것도 —
 * 하나만 돌리는 추론의 화면과 시작 요청은 예전과 같다.
 */

const inputClass = 'px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs font-mono text-neutral-100 focus:outline-none focus:border-blue-500'

export function ExtraPoliciesEditor({ firstName, onFirstName, extras, onChange, models, first, problems }: {
  firstName: string
  onFirstName: (name: string) => void
  extras: ExtraPolicy[]
  onChange: (extras: ExtraPolicy[]) => void
  models: Model[]
  /** 첫 정책(위에서 고른 체크포인트) — 종류·액션 길이·카메라의 기준 */
  first: Model | undefined
  problems: SlotProblems
}) {
  const t = useT()
  const [picking, setPicking] = useState<number | null>(null)
  const set = (i: number, patch: Partial<ExtraPolicy>) =>
    onChange(extras.map((x, j) => (j === i ? { ...x, ...patch } : x)))
  const add = () => {
    const taken = new Set([firstName, ...extras.map((x) => x.name)])
    let n = extras.length + 2
    while (taken.has(`policy_${n}`)) n++
    onChange([...extras, { name: `policy_${n}`, model_id: '' }])
  }
  return (
    <div className="space-y-2 border-t border-neutral-700 pt-3">
      {extras.length > 0 && (<>
        <div className="flex items-center gap-2 text-xs">
          <span className="w-28 shrink-0 text-neutral-400">{t('위 정책의 이름')}</span>
          <input value={firstName} onChange={(e) => onFirstName(e.target.value.trim())} className={`w-32 ${inputClass}`} />
        </div>
        {problems.first && <p className="pl-[7.5rem] text-[11px] text-red-400">✗ {problems.first}</p>}
        <h4 className="text-xs font-semibold text-neutral-300">{t('함께 올릴 정책')}</h4>
      </>)}
      {extras.map((x, i) => {
        const m = models.find((mm) => mm.id === x.model_id)
        return (
          <div key={i} className="space-y-1">
            <div className="flex items-center gap-2">
              <input value={x.name} onChange={(e) => set(i, { name: e.target.value.trim() })}
                className={`w-28 shrink-0 ${inputClass}`} />
              <PickerField empty={!x.model_id} placeholder={t('체크포인트 고르기')} onOpen={() => setPicking(i)}>
                {m ? <CheckpointLabel model={m} models={models} />
                  : <p className="truncate font-mono text-xs text-neutral-200" title={x.model_id}>{x.model_id}</p>}
              </PickerField>
              <button type="button" title={t('빼기')} onClick={() => onChange(extras.filter((_, j) => j !== i))}
                className="shrink-0 rounded px-1.5 text-neutral-500 hover:bg-neutral-700 hover:text-white">✕</button>
            </div>
            {problems.rows[i] && <p className="pl-[7.5rem] text-[11px] text-red-400">✗ {problems.rows[i]}</p>}
          </div>
        )
      })}
      {problems.mode && <p className="text-[11px] text-red-400">✗ {problems.mode}</p>}
      <button type="button" onClick={add} className="text-xs text-blue-400 hover:text-blue-300">
        {t('+ 함께 올릴 정책')}
      </button>
      {extras.length > 0 && (
        <p className="text-[10px] text-neutral-500">
          {t('같은 로봇·카메라로 학습한 같은 종류의 정책을 한 추론에 올려 두고, 실행 중에 팔을 세우지 않은 채 바꿔 씁니다. 플러그인은 이 이름으로 바꿉니다.')}
        </p>
      )}
      {picking !== null && (
        <WeightTableModal title={t('함께 올릴 체크포인트 고르기')} value={extras[picking]?.model_id}
          onClose={() => setPicking(null)}
          onPick={(c) => set(picking, { model_id: c.id })}
          include={(r) => r.is_policy}
          // 시작이 거절할 것을 미리 흐린다 — 판정은 시작 API 가 다시 한다
          blockedReason={(r) => first && r.policy_type !== first.policy_type
            ? t('첫 정책({type})과 종류가 다릅니다', { type: first.policy_type })
            : r.action_space === 'unknown'
              ? t('델타로 학습한 것으로 보이는데 학습 데이터셋이 없어 확인할 수 없습니다 — 시작하지 않습니다') : null} />
      )}
    </div>
  )
}

/**
 * 실행 중 — 올린 정책 칩과 유지. ● 는 wrapper 텔레메트리가 바뀌었다고 알린 뒤에 옮긴다(요청을 보냈다고
 * 옮기지 않는다). 그 사이 고른 칩은 깜박인다 — 도는 추론이 끝나야 갈아끼운다.
 */
export function PolicyBar({ policies, active, holding, pending, onSwitch, onHold }: {
  policies: string[]
  active: string | null
  holding: boolean
  pending: string | null
  onSwitch: (name: string) => void
  onHold: (on: boolean) => void
}) {
  const t = useT()
  return (
    <div className={`flex flex-wrap items-center gap-2 rounded-lg border px-4 py-2 text-xs ${
      holding ? 'border-amber-500/50 bg-amber-500/10' : 'border-neutral-700 bg-neutral-800'}`}>
      <span className="text-neutral-400">{t('정책')}</span>
      {policies.map((n) => {
        const on = n === active
        const waiting = n === pending && !on
        return (
          <button key={n} type="button" onClick={() => onSwitch(n)} disabled={waiting}
            title={on ? t('지금 정책 — 누르면 처음부터 다시 돕니다')
              : waiting ? t('도는 추론이 끝나면 바뀝니다')
                : t('이 정책으로 바꿉니다 — 팔은 지금 자리에서 이어갑니다')}
            className={`rounded-full px-3 py-1 font-mono ${on ? 'bg-blue-600 text-white'
              : waiting ? 'animate-pulse border border-blue-500 text-blue-300'
                : 'border border-neutral-600 text-neutral-300 hover:border-neutral-400'}`}>
            {on && '● '}{n}
          </button>
        )
      })}
      <button type="button" onClick={() => onHold(!holding)}
        title={t('새 추론 없이 마지막 액션을 계속 보냅니다 — 일시정지와 달리 쥔 그리퍼의 지령도 그대로 보냅니다')}
        className={`ml-2 rounded px-3 py-1 font-medium ${holding ? 'bg-amber-600 text-white hover:bg-amber-500'
          : 'border border-neutral-600 text-neutral-300 hover:border-neutral-400'}`}>
        {holding ? t('유지 풀기') : t('유지')}
      </button>
      {holding && <span className="font-semibold text-amber-300">{t('⏸ 유지 중 — 마지막 액션을 계속 보냅니다')}</span>}
    </div>
  )
}
