import type { TFn } from '../i18n'
import type { Model } from './models'

/**
 * 한 추론에 정책을 여럿 올린다 (feature/multi-policy.md) — 준비 화면의 판정과 시작 요청.
 * 화면은 components/PolicySlots.tsx.
 */

export type ExtraPolicy = { name: string; model_id: string }
export const DEFAULT_POLICY_NAME = 'main'
// backend routers/models._policy_name_ok · wrapper/policy_slots.NAME_RE 와 같은 규칙
const NAME_RE = /^[A-Za-z0-9_-]{1,40}$/

export type SlotProblems = { first: string | null; rows: (string | null)[]; mode: string | null }

/**
 * 시작 전에 막을 것. 판정은 시작 API 가 장치를 건드리기 전에 다시 한다 — 여기는 미리 보여 주기만.
 * 목록을 읽기 전에는 "목록에 없다" 를 말하지 않는다(열자마자 잠깐 뜨면 거짓말이다).
 */
export function slotProblems(t: TFn, firstName: string, extras: ExtraPolicy[], models: Model[],
  first: Model | undefined, mode: 'local' | 'server'): SlotProblems {
  if (extras.length === 0) return { first: null, rows: [], mode: null }
  const counts = new Map<string, number>()
  for (const n of [firstName, ...extras.map((x) => x.name)]) counts.set(n, (counts.get(n) ?? 0) + 1)
  const nameProblem = (n: string) => !NAME_RE.test(n) ? t('이름은 영숫자·_·- 로 40자까지입니다')
    : (counts.get(n) ?? 0) > 1 ? t('이름이 겹칩니다') : null
  const firstCams = new Set(first?.requirements.required_cameras.map((c) => c.name) ?? [])
  const rows = extras.map((x) => {
    const own = nameProblem(x.name)
    if (own) return own
    if (!x.model_id) return t('체크포인트를 고르세요')
    const m = models.find((mm) => mm.id === x.model_id)
    if (!m) return models.length ? t('목록에 없는 경로입니다 — 지워졌거나 이 기계의 것이 아닐 수 있습니다') : null
    if (!first) return null
    if (m.policy_type !== first.policy_type) return t('첫 정책({type})과 종류가 다릅니다', { type: first.policy_type })
    const length = m.requirements.action_dim
    const firstLength = first.requirements.action_dim
    if (length && firstLength && length !== firstLength)
      return t('액션 길이({length})가 첫 정책({first_length})과 다릅니다', { length, first_length: firstLength })
    const missing = m.requirements.required_cameras.map((c) => c.name).filter((c) => !firstCams.has(c))
    if (missing.length) return t('첫 정책에 없는 카메라를 씁니다: {cams}', { cams: missing.join(', ') })
    if (m.action_space === 'unknown')
      return t('델타로 학습한 것으로 보이는데 학습 데이터셋이 없어 확인할 수 없습니다 — 시작하지 않습니다')
    return null
  })
  return {
    first: nameProblem(firstName),
    rows,
    mode: mode === 'server' ? t('정책 여럿은 로컬 추론에서만 올릴 수 있습니다 — 정책 서버는 하나만 돌립니다') : null,
  }
}

export const blocksStart = (p: SlotProblems) => !!(p.first || p.mode || p.rows.some(Boolean))

/** 시작·미리보기 요청에 실을 것 — 정책이 하나면 예전 요청 그대로(이름도 안 싣는다) */
export function slotRequest(firstName: string, extras: ExtraPolicy[], models: Model[]) {
  if (extras.length === 0) return {}
  return {
    policy_name: firstName,
    extra_policies: extras.flatMap((x) => {
      const m = models.find((mm) => mm.id === x.model_id)
      return m ? [{ name: x.name, checkpoint_path: m.path }] : []
    }),
  }
}
