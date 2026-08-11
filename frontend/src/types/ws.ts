/**
 * WebSocket 메시지 계약 — 백엔드 `app/core/ws_messages.py` 의 짝.
 *
 * 이전에는 `WsMessage.type` 이 그냥 `string` 이고 `data` 가 `unknown` 이라,
 * `msg.type === 'train_metrics'` 를 `'train_metric'` 으로 오타 내도
 * **빌드가 통과하고 런타임에 조용히 아무 일도 안 일어났다.**
 * 백엔드에서 타입 이름을 바꿔도 마찬가지 — 화면이 그냥 안 갱신됐다.
 *
 * 판별 유니언으로 두면 `if (msg.type === 'train_metrics')` 안에서 `msg.data` 가
 * 자동으로 좁혀져 캐스트가 사라지고, 오타는 컴파일 에러가 된다.
 */

import type { TelemetryData } from '../components/TelemetryPanel'

/** 프로세스 수명주기 상태 — 백엔드 `ProcessManager.ProcessState` 와 같은 집합. */
export const PROCESS_STATES = ['idle', 'starting', 'running', 'stopping', 'error'] as const
export type ProcessState = (typeof PROCESS_STATES)[number]

/** 실행 중으로 볼 상태. `stopping` 도 포함한다 — SIGTERM 후에도 프로세스는 살아 있다. */
export const isBusy = (s: ProcessState): boolean => s !== 'idle' && s !== 'error'

export type TrainMetricsData = {
  step: number
  loss?: number
  lr?: number
  grad_norm?: number
  [key: string]: number | undefined
}

export type RecordStatusData = {
  state?: string
  current_episode: number
  total_episodes: number
  phase: string
  progress: number
}

export type LogSavedData = {
  csv_path: string
  steps: number
  debug_dir?: string | null
}

export type WsMessage =
  // 추론
  | { type: 'log'; data: string }
  | { type: 'state'; data: ProcessState }
  | { type: 'telemetry'; data: TelemetryData & { paused?: boolean } }
  | { type: 'log_saved'; data: LogSavedData }
  // 학습
  | { type: 'train_log'; data: string }
  | { type: 'train_state'; data: ProcessState }
  | { type: 'train_metrics'; data: TrainMetricsData }
  // 녹화
  | { type: 'record_log'; data: string }
  | { type: 'record_state'; data: ProcessState }
  | { type: 'record_status'; data: RecordStatusData }
  // 정책 서버
  | { type: 'ps_log'; data: string }
  | { type: 'ps_state'; data: ProcessState }
  // Hub 업로드
  | { type: 'upload_log'; data: string }
  | { type: 'upload_state'; data: ProcessState }
  // 연결 유지
  | { type: 'pong'; data?: undefined }

export type WsMessageType = WsMessage['type']

/** 활동 상태를 바꿀 수 있는 메시지인가 — `/api/activity` 재조회 시점 판단용. */
export function isStateMessage(type: WsMessageType): boolean {
  return type === 'state' || type.endsWith('_state')
}
