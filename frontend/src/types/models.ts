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
  id: string
  path: string
  policy_type: string
  config: Record<string, unknown>
  requirements: ModelRequirements
  size_bytes: number
  modified: string
}

export type ModelDetail = Model & {
  files: { path: string; size_bytes: number }[]
}

export type Dataset = {
  id: string
  path: string
  total_episodes: number
  total_frames: number
  fps: number | null
  features: Record<string, unknown>
  size_bytes: number
  modified: string
  /** ACT-Aux 용으로 구운 사본이면 그 정보 (backend dataset_scanner.baked_info). 원본이면 null/undefined. */
  baked?: BakedInfo | null
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
