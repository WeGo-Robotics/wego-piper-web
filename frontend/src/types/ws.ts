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

/** 학습 job — 로컬(`job_id: 'local'`)과 원격이 같은 모양이다. */
export type JobRecord = {
  job_id: string
  runner: string
  state: ProcessState
  output_dir: string
  total_steps: number
  metrics: TrainMetricsData | Record<string, never>
  created_at: string
  updated_at: string
  provider: string
  instance_id: string
}

/** 로컬 학습의 job_id. 백엔드 `piper_bus.contract.LOCAL_JOB_ID` 와 같아야 한다. */
export const LOCAL_JOB_ID = 'local'

/** 장치가 사라졌다/돌아왔다. 문구는 **백엔드가 만든다** — 화면이 문장을 조립하면
 *  한쪽만 고쳐져 어긋난다 (`usb_warning` 과 같은 규칙). */
export type DeviceAlert = {
  kind: 'robot' | 'camera'
  id: string
  name: string
  /** `device_gone` = 그 장치의 USB / `daemon_down` = 데몬이 내려갔다 /
   *  `all_gone` = 한꺼번에 전부 (데몬 또는 USB 컨트롤러 — 확인법이 다르다).
   *  안 가르면 데몬이 죽었을 때 USB 를 확인하러 가게 만든다. */
  reason: 'device_gone' | 'daemon_down' | 'all_gone' | 'stalled'
  text: string
}

export type DeviceAlertData = { alerts: DeviceAlert[]; added: DeviceAlert[]; cleared: DeviceAlert[] }

export type WsMessage =
  // 추론
  | { type: 'log'; data: string }
  | { type: 'state'; data: ProcessState }
  | { type: 'telemetry'; data: TelemetryData & { paused?: boolean } }
  | { type: 'log_saved'; data: LogSavedData }
  // 학습 — `job_id` 로 어느 job 것인지 밝힌다. 없으면 클라우드 job 2개가 서로를 덮어쓴다.
  | { type: 'train_log'; job_id: string; data: string }
  | { type: 'train_state'; job_id: string; data: ProcessState }
  | { type: 'train_metrics'; job_id: string; data: TrainMetricsData }
  | { type: 'job_list'; data: JobRecord[] }
  // 녹화
  | { type: 'record_log'; data: string }
  | { type: 'record_state'; data: ProcessState }
  | { type: 'record_status'; data: RecordStatusData }
  // 정책 서버
  | { type: 'ps_log'; data: string }
  | { type: 'ps_state'; data: ProcessState }
  // 에피소드 오케스트레이터 (스텝 전이·회차 완료)
  | { type: 'orchestrator'; data: Record<string, unknown> }
  // Hub 업로드
  | { type: 'upload_log'; data: string }
  | { type: 'upload_state'; data: ProcessState }
  // 장치 사라짐 (CAN·카메라). **전이에서만** 온다 — 현재 목록은 /api/devices/alerts
  | { type: 'device_alert'; data: DeviceAlertData }
  // 연결 유지
  | { type: 'pong'; data?: undefined }
  // ⚠ **클라이언트가 보내는** 유일한 메시지 (나머지는 전부 서버 → 화면).
  //
  //   E-stop heartbeat 는 원래 `POST /api/estop/heartbeat` 였는데, HTTP/1.1 은
  //   오리진당 연결이 6개뿐이라 **카메라 프리뷰와 같은 줄에 서 있었다.**
  //   실측: 타이머는 정시(500ms)에 만들었고 유실도 없었는데 서버가 본 도착
  //   간격이 2.35초였다 — 만들어진 뒤 나가기 전에 대기한 것이다. 그 2.35초가
  //   녹화를 죽였다.
  //
  //   WS 는 그 6개와 **다른 풀**을 쓴다. 안전 신호가 화면 갱신 트래픽에
  //   밀리지 않게 하는 것이 요점이다.
  | {
      type: 'heartbeat'
      gap?: number
      hidden?: boolean
      seq?: number
      rtt?: number
      rttSeq?: number
      via?: 'ws' | 'http'
    }

export type WsMessageType = WsMessage['type']

/** 활동 상태를 바꿀 수 있는 메시지인가 — `/api/activity` 재조회 시점 판단용. */
export function isStateMessage(type: WsMessageType): boolean {
  return type === 'state' || type.endsWith('_state')
}
