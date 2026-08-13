import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 지원하는 정책 목록 — 백엔드 `core/policies.py` 하나에서 온다.
 *
 * 이전에는 `TrainingPage.POLICY_TYPES`, `InferencePage` 의 `<option>` 목록,
 * `RTC_POLICIES` 가 각각 손으로 적혀 있었고 셋 다 달랐다. 실제로 `sac` 은
 * 학습 화면에서 고를 수 있는데 추론 시작에서 죽었다.
 */

export type PolicyInfo = {
  type: string
  label: string
  train: boolean
  infer: boolean
  rtc: boolean
  /** 언어 지시(task)를 받는가. ACT 처럼 안 받는 정책엔 입력을 띄우지 않는다. */
  language: boolean
  encoder_probe: boolean
  /** 처음부터 학습이 무의미한 정책의 권장 시작점 (없으면 빈 문자열). */
  policy_base: string
}

export function usePolicies() {
  const [policies, setPolicies] = useState<PolicyInfo[]>([])

  useEffect(() => {
    api.get<PolicyInfo[]>('/policies').then(setPolicies).catch(() => {})
  }, [])

  /** flow-matching 정책인가 (RTC 가이던스 파라미터를 노출할지) */
  const isRtc = useCallback(
    (type: string) => policies.some((p) => p.type === type && p.rtc),
    [policies],
  )

  /** 언어 지시(task)를 받는 정책인가.
   *
   * ⚠ 목록이 비어 있는 동안(로딩 중)에는 **true 로 본다.** 받는 정책인데 입력을
   * 감췄다가 뒤늦게 띄우면 사용자가 이미 시작 버튼을 눌렀을 수 있다 —
   * 안 쓰이는 입력이 잠깐 보이는 쪽이 낫다.
   */
  const takesLanguage = useCallback(
    (type: string) =>
      policies.length === 0 || policies.some((p) => p.type === type && p.language),
    [policies],
  )

  /** 권장 베이스 체크포인트 — 프론트가 목록을 또 만들면 백엔드와 갈라진다. */
  const policyBase = useCallback(
    (type: string) => policies.find((p) => p.type === type)?.policy_base || '',
    [policies],
  )

  return {
    takesLanguage,
    policies,
    policyBase,
    trainable: policies.filter((p) => p.train),
    inferable: policies.filter((p) => p.infer),
    isRtc,
  }
}
