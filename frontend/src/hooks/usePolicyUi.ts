import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 정책별 화면 스펙 — `policies/<type>.yaml` 이 정본이다.
 *
 * 이전에는 이 내용이 `TrainingPage.tsx` 의 `POLICY_TRAIN_SCHEMAS`(400줄)에 있었고,
 * 백엔드 목록과 갈라져서 `pi0_fast`·`tdmpc`·`vqbet` 은 **골라도 화면이 안 바뀌었다.**
 * 같은 사실을 프론트도 읽을 수 있는 곳에 두면 그런 분기가 생길 자리가 없다.
 */

export type SpecField = {
  key: string
  label: string
  kind: 'number' | 'bool'
  default: number | boolean
  min: number | null
  max: number | null
  step: number | null
  /** 모델 구조를 정하는 값 — 체크포인트에서 이어 학습하면 이미 고정이라 가린다. */
  arch: boolean
  /** LeRobot 기본값과 일부러 다를 때 그 이유. 비어 있으면 상류 그대로. */
  override_reason: string
}

type Cond = { field: string; is: unknown }

export type SpecWarning = {
  when: Cond
  and: Cond | null
  level: 'info' | 'warn' | 'error'
  text: string
}

export type PolicyUi = {
  type: string
  /** 베이스 없이 처음부터 학습할 때의 안내. 없으면 빈 문자열. */
  scratch_note: string
  train: { defaults: Record<string, unknown>; fields: SpecField[]; warnings: SpecWarning[] }
  encoder_probe: { base_label: string; taps: { key: string; label: string; default: boolean }[]; note: string }
}

const EMPTY: PolicyUi = {
  type: '',
  scratch_note: '',
  train: { defaults: {}, fields: [], warnings: [] },
  encoder_probe: { base_label: '', taps: [], note: '' },
}

export function usePolicyUi(policyType: string) {
  const [ui, setUi] = useState<PolicyUi>(EMPTY)

  useEffect(() => {
    if (!policyType) { setUi(EMPTY); return }
    let alive = true
    api.get<PolicyUi>(`/policies/${policyType}/ui`)
      // ⚠ 못 받아도 **빈 스펙**으로 둔다. 스펙은 편의 계층이지 안전 계층이 아니다 —
      // 파라미터 패널만 비고 학습 시작은 그대로 된다. 값 검증의 정본은 백엔드다.
      .then((d) => { if (alive) setUi(d) })
      .catch(() => { if (alive) setUi(EMPTY) })
    return () => { alive = false }
  }, [policyType])

  return ui
}

/** 스펙 기본값으로 채운 값 객체. `override` 가 있으면 그 값이 이미 반영돼 있다. */
export function specDefaults(fields: SpecField[]): Record<string, number | boolean> {
  return Object.fromEntries(fields.map((f) => [f.key, f.default]))
}

/** 조건 하나. 문법이 빈약한 것은 의도다 — YAML 안에 프로그램을 만들지 않는다. */
function matches(c: Cond | null, values: Record<string, unknown>): boolean {
  if (!c) return true
  return values[c.field] === c.is
}

export function activeWarnings(
  warnings: SpecWarning[],
  values: Record<string, unknown>,
): SpecWarning[] {
  return warnings.filter((w) => matches(w.when, values) && matches(w.and, values))
}
