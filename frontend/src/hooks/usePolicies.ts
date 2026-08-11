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
  encoder_probe: boolean
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

  return {
    policies,
    trainable: policies.filter((p) => p.train),
    inferable: policies.filter((p) => p.infer),
    isRtc,
  }
}
