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
  /** 학습 때 카메라 (backend services/train_cameras.py) — config 의 입력 크기와 다르다 */
  train_cameras?: TrainCameras
}

/** 학습(run)의 이름. 학습 폼의 제목(run 사이드카)이 먼저, 없으면 체크포인트에 붙인 이름
 *  (모델 페이지 편집기). 이름이 없으면 빈 문자열 — 화면은 경로로 대신한다. */
export function runName(list: Model[]): string {
  return list[0]?.run_notes?.name || list.find((m) => m.notes?.name)?.notes?.name || ''
}

/** 가중치 표의 체크포인트 한 칸. `last` 링크와 그 스텝 폴더는 한 칸이다 (backend `scan_runs`). */
export type WeightCheckpoint = {
  /** 고르는 id — 스텝 쪽. `last` 만 있는 학습이면 `…/last` */
  id: string
  /** 이 칸이 가리키는 id 전부 (`…/020000`, `…/last`) — 예전에 고른 값을 알아볼 때 */
  ids: string[]
  checkpoint: string | null
  step: number | null
  /** 학습의 마지막 체크포인트 (`last` 가 가리킨다) */
  final: boolean
  path: string
  /** 체크포인트에 붙은 이름 — run 이름을 물려받았으면 그것과 같다 */
  name: string
  size_bytes: number
}

/** 가중치 표의 한 행 — 학습 하나, 또는 학습 산출물이 아닌 모델 하나 (`GET /models/runs`). */
export type WeightRun = {
  key: string
  kind: 'run' | 'model'
  run: string | null
  /** 보이는 이름 — run 사이드카, 없으면 체크포인트에 붙인 이름 */
  name: string
  description: string
  /** 표에서 고치는 사이드카 그대로 (run 이면 run 루트, 모델이면 그 모델) */
  notes: SidecarNotes | Record<string, never>
  dataset: string | null
  policy_type: string
  is_policy: boolean
  action_space?: Model['action_space']
  created: string | null
  modified: string
  steps: number | null
  batch_size: number | null
  lr: number | null
  /** 이어 학습의 출발점. 처음부터면 null. 로컬 학습이면 `run`·`id` 가 있다 */
  base: { run: string | null; id: string | null; checkpoint: string | null; label: string } | null
  final_loss: number | null
  train_cameras?: TrainCameras
  size_bytes: number
  path: string
  checkpoints: WeightCheckpoint[]
}

/** 학습 때 카메라 한 대. 크기는 데이터셋 비디오, fps·기종·캡처 크기는 녹화 사이드카에서 온다. */
export type TrainCamera = {
  width: number
  height: number
  fps?: number
  /** 기종 — 카메라 페이지의 장치명(`D435I Color`) */
  model?: string
  /** 장치를 연 크기. 발행 크기와 다르면 넓게 받아 줄인 것이다(화각이 다르다) */
  capture_width?: number
  capture_height?: number
  id?: string
  stream?: string
}

export type TrainCameras = {
  /** 키는 데이터셋 쪽 이름(top/hand) — 추론 매핑의 키와 같다 */
  cameras: Record<string, TrainCamera>
  dataset?: string | null
  /** marker: 체크포인트의 표식 · dataset: 학습 데이터셋에서 읽음 · none: 모름 */
  source: 'marker' | 'dataset' | 'none'
}

/** 추론 검증의 카메라 대조 한 줄 (`/inference/validate` 의 `cameras`). 판정은 백엔드 한 곳이다. */
export type CameraCheck = {
  train: TrainCamera | null
  live: { width?: number; height?: number; model?: string } | null
  /** ok · size: 비율은 같고 크기만 다름(실행됨) · aspect: 비율이 다름(시작 안 됨) · unknown: 학습 쪽 모름 · no_live */
  status: 'ok' | 'size' | 'aspect' | 'unknown' | 'no_live'
  model_differs: boolean
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
  /** 어디서 왔나 — `lerobot`: 이 기계의 데이터셋 폴더, `hub`: 허브 캐시에서 받은 것 */
  source?: 'lerobot' | 'hub' | null
  /** ACT-S·SmolVLA-S 용으로 구운 사본이면 그 정보 (backend dataset_scanner.baked_info). 원본이면 null/undefined. */
  baked?: BakedInfo | null
  /** 액션을 관절 이동 변화량으로 바꾼 사본이면 그 근거 (backend dataset_scanner.delta_info). 원본이면 null. */
  delta?: DeltaInfo | null
  /** 시뮬로 모았으면 **어느 세계에서** 모았는지 (meta/piper_scene.json). 실기면 없다. */
  scene?: { id: string; name: string; objects: number } | null
  /** 시나리오 녹화 결과 요약 (meta/piper_scenario_log.jsonl). 시나리오 없이 찍었으면 없다.
   *  `pending` 이 미결 실패 — 학습에 넣을지 아직 안 정한 것 (feature/scenario-designer.md §8-3). */
  scenario?: { bound: number; failed: number; pending: number; excluded: number } | null
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
  /** 영상 주소에 붙이는 판 번호 — 편집·녹화로 mp4 가 바뀌면 달라진다 (dataset_scanner.get_dataset) */
  rev: number
}

export type DiskUsage = {
  datasets_bytes: number
  models_bytes: number
  total_gb: number
  warning: boolean
  threshold_gb: number
}
