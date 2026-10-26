export type RequiredCamera = {
  name: string
  model_name?: string
  channels: number
  height: number | null
  width: number | null
}

export type ModelRequirements = {
  required_cameras: RequiredCamera[]
  state_dim: number
  action_dim: number
}

export type Model = {
  notes?: SidecarNotes
  id: string
  /** 학습 단위 (`2026-09-01/10-11-22_act`). 학습 산출물이 아니면 없다 — HF 허브
   *  모델은 묶이지 않는다. 백엔드가 정한다: `id` 를 화면에서 쪼개면
   *  `PekingU/rtdetr_v2_r18vd` 까지 "PekingU 학습" 으로 묶인다. */
  run?: string
  /** 체크포인트 이름 (`060000`, `last`) */
  checkpoint?: string
  /** 정렬용 숫자. `last` 는 없다 */
  step?: number | null
  path: string
  policy_type: string
  is_policy?: boolean
  config: Record<string, unknown>
  requirements: ModelRequirements
  size_bytes: number
  modified: string
  created?: string
  // 학습(run) 루트 사이드카 — 학습 폼의 제목·설명. 체크포인트마다 물려받는다.
  run_notes?: SidecarNotes
  run_created?: string
  /** 무엇을 내는가 (backend services/action_space.py). `delta_v1` 이면 추론이 절대 위치로 되돌린다,
   *  `unknown` 은 델타로 학습한 듯한데 데이터셋이 없어 확인 못 함 — 추론이 막힌다. */
  action_space?: 'absolute' | 'delta_v1' | 'unknown'
}

export type ModelDetail = Model & {
  files: { path: string; size_bytes: number }[]
}

/** 이름·설명 사이드카 (meta/piper_notes.json — LeRobot 구조에 이 자리가 없다). */
export type SidecarNotes = { name: string; description: string; updated_at: string }

export type Dataset = {
  id: string
  path: string
  notes?: SidecarNotes
  total_episodes: number
  total_frames: number
  fps: number | null
  features: Record<string, unknown>
  size_bytes: number
  modified: string
  created?: string
  /** ACT-S·SmolVLA-S 용으로 구운 사본이면 그 정보 (backend dataset_scanner.baked_info). 원본이면 null/undefined. */
  baked?: BakedInfo | null
  /** 액션을 관절 이동 변화량으로 바꾼 사본이면 그 근거 (backend dataset_scanner.delta_info). 원본이면 null. */
  delta?: DeltaInfo | null
  /** 시뮬로 모았으면 **어느 세계에서** 모았는지 (meta/piper_scene.json). 실기면 없다. */
  scene?: { id: string; name: string; objects: number } | null
}

export type DeltaInfo = {
  source: string | null
  delta_names: string[]
  /** 절대값 그대로인 채널 (그리퍼) */
  absolute_names: string[]
  /** 스텝당 상한 — 데모에서 나온 가장 큰 한 스텝 */
  clamp: Record<string, number | null>
  /** 추종 지연 ε 평균 (학습 기준 a[t−1] 과 추론 기준 s[t] 의 차이) */
  lag: Record<string, number>
}

export type BakedInfo = {
  source: string | null
  /** 원본 사이드카가 bake 뒤에 바뀌었다 — 재굽기 필요 */
  stale: boolean
  source_missing: boolean
  stage_names: string[]
  class_counts?: Record<string, number>
}

export type DatasetDetail = Dataset & {
  episodes: Record<string, unknown>[]
  tasks: Record<string, unknown>[]
}

export type DiskUsage = {
  datasets_bytes: number
  models_bytes: number
  total_gb: number
  warning: boolean
  threshold_gb: number
}
