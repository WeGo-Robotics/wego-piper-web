/**
 * 가중치 분석(추론 분석 화면의 체크포인트·오프라인 평가 탭) 공용 타입 — feature/weight-analyzer.md.
 * 백엔드 `app/services/weight_eval.py` 의 응답 모양을 그대로 옮긴다.
 */

export type RunItem = {
  run: string
  policy_type: string | null
  created: string | null
  checkpoints: number
  dataset: string | null
  /** 학습 폼 제목, 없으면 체크포인트에 붙인 이름. 없으면 빈 문자열 */
  name?: string
}

export type Overall = {
  n: number
  joint_deg: number | null
  gripper: number | null
  chunk_joint_deg: number | null
  grip_closes?: number
  grip_missed?: number
  grip_mean_abs_s?: number | null
  grip_mean_s?: number | null
}

export type OfflineRef = Overall & { key: string; dataset: string; episodes: number[]; stride: number }

export type CkptRow = {
  id: string
  checkpoint: string | null
  step: number | null
  real: { n: number; success: number } | null
  offline: OfflineRef[]
}

export type RunDetail = {
  run: string
  policy_type: string | null
  dataset: string | null
  dataset_episodes: number[] | null
  steps: number | null
  history: { steps: number[]; losses: number[] } | null
  checkpoints: CkptRow[]
}

export type JobInfo = {
  key: string
  checkpoint: string
  dataset: string
  episodes: number[]
  stride: number
  started: number
  state?: string
  summary?: Overall
}

export type JobStatus = {
  state: 'idle' | 'running' | 'done' | 'error' | 'stopped' | 'missing'
  job?: JobInfo
  progress?: { done: number; total: number }
  error?: string | null
}

export type GripTiming = { dim: number; arm: string | null; closes: number; missed: number; deltas_s: number[] }

export type Summary = {
  key: string
  job: JobInfo
  meta: { fps: number; chunk_size: number; elapsed_s: number; policy_type: string; frames: number }
  dims: { name: string; kind: 'joint' | 'gripper' | 'other'; arm: string | null; joint: number | null }[]
  per_dim: number[]
  overall: Overall
  by_phase: (Overall & { phase: number; name: string })[]
  by_episode: (Overall & { episode: number; grip: GripTiming[] })[]
}

export type EpisodeSeries = {
  episode: number
  frames: number[]
  phase: number[]
  names: string[]
  pred: number[][]
  true: number[][]
  fps: number
}

export type Reaction = {
  checkpoint: string
  dataset: string
  episode: number
  frame: number
  device: string
  edited_cameras: string[]
  names: string[]
  original: number[][]
  original_tips: Record<string, number[][]>
  edited?: number[][]
  edited_tips?: Record<string, number[][]>
  tip_distance?: Record<string, { mean_mm: number; max_mm: number; end_mm: number }>
}

/** `/weval/frames` — 에피소드 × 프레임마다 폴더 하나(`dir`), 그 안에 카메라별 PNG */
export type FramesResponse = {
  sid: string
  cameras: string[]
  task: string
  items: { episode: number; frame: number; dir: string | null; error?: string }[]
}

/** 평가 표본 — 체크포인트끼리 견주려면 **같은** 에피소드여야 한다. 고르게 N개를 뽑는다. */
export function sampleEpisodes(all: number[], n: number): number[] {
  const sorted = [...all].sort((a, b) => a - b)
  if (n <= 0 || n >= sorted.length) return sorted
  const out = new Set<number>()
  for (let i = 0; i < n; i++) out.add(sorted[Math.round((i * (sorted.length - 1)) / (n - 1 || 1))])
  return [...out]
}

export const stepLabel = (r: { step: number | null; checkpoint: string | null }) =>
  r.step == null ? (r.checkpoint ?? '?') : r.step >= 1000 ? `${r.step / 1000}k` : String(r.step)
