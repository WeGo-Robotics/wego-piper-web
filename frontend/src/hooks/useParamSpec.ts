import { useEffect, useMemo, useState } from 'react'
import { api } from '../services/api'

/**
 * 추론 파라미터 스펙 — 백엔드 `core/inference_params.py` 하나에서 온다.
 *
 * 이전에는 기본값·범위·라벨이 프론트에 손으로 적혀 있어서 백엔드 클램프 범위와
 * 어긋났다 (`max_velocity` 가 프론트 500 / 백엔드 1000).
 * 이제 범위는 서버가 말하고, 프론트는 그걸 렌더만 한다.
 *
 * 스펙이 도착하기 전에도 화면은 떠야 하므로 `fallback` 을 받는다 —
 * 다만 그 값이 정본은 아니다.
 */

export type ParamSpec = {
  key: string
  label: string
  kind: 'number' | 'bool'
  default: number | boolean | null
  min: number | null
  max: number | null
  step: number | null
  group: string
  policies: string[]
  help: string
}

type SpecResponse = {
  params: ParamSpec[]
  defaults: Record<string, number | boolean>
}

export function useParamSpec() {
  const [spec, setSpec] = useState<ParamSpec[]>([])
  const [defaults, setDefaults] = useState<Record<string, number | boolean>>({})

  useEffect(() => {
    api.get<SpecResponse>('/params/spec')
      .then((r) => { setSpec(r.params); setDefaults(r.defaults) })
      .catch(() => {})
  }, [])

  const byKey = useMemo(
    () => Object.fromEntries(spec.map((p) => [p.key, p])) as Record<string, ParamSpec>,
    [spec],
  )

  /** 슬라이더 props — 스펙이 아직 없으면 fallback 을 쓴다. */
  const rangeOf = (
    key: string,
    fallback: { min: number; max: number; step: number },
  ) => {
    const s = byKey[key]
    return {
      min: s?.min ?? fallback.min,
      max: s?.max ?? fallback.max,
      step: s?.step ?? fallback.step,
    }
  }

  /** 이 정책에서 노출할 파라미터인가. `policies` 가 비면 전부 노출. */
  const isVisible = (key: string, policyType: string) => {
    const s = byKey[key]
    if (!s || s.policies.length === 0) return true
    return s.policies.includes(policyType)
  }

  return { spec, defaults, byKey, rangeOf, isVisible, loaded: spec.length > 0 }
}
