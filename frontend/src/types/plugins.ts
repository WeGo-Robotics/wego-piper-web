/**
 * 플러그인 — 백엔드 `services/plugins.py` 의 `PluginInfo.to_dict()` + `status()` 의 짝
 * (feature/plugins.md §4, §8-1). 매니페스트 모양의 정본은 SDK `piper_plugin/manifest.py`.
 */

export type PluginConfigField = {
  key: string
  kind: 'number' | 'bool' | 'text' | 'select' | 'model' | 'dataset' | 'camera'
  default?: number | boolean | string | null
  min?: number | null
  max?: number | null
  step?: number | null
  label?: string
  options?: string[]
  multiline?: boolean
}

export type PluginCameraSlot = { key: string; label?: string; required?: boolean }
/** 상태를 읽을 팔 — 값은 CAN 인터페이스. 읽기 전용 (§7-5) */
export type PluginArmSlot = { key: string; label?: string; required?: boolean }
/** 다른 플러그인의 출력을 받는 자리 — 검출 이름 또는 토픽 (§7-3) */
export type PluginInputSlot = { key: string; kind: 'detections' | 'topic'; source?: string; label?: string; required?: boolean }

export type PluginManifest = {
  name: string
  version: string
  title: string
  description: string
  entry: string
  python: 'system' | 'venv' | 'venv-isolated'
  cameras: PluginCameraSlot[]
  arms?: PluginArmSlot[]
  inputs: PluginInputSlot[]
  config: PluginConfigField[]
  permissions: string[]
  resources: { gpu: boolean; heartbeat: boolean; memory?: string | null; cpu?: string | null }
  /** false | 저장소 경로(`/detector`) | `ui/` | `server` — §8-2 */
  ui: boolean | string
  outputs: { previews: string[]; detections: string[]; topics: string[]; state: { key: string; label?: string; unit?: string }[] }
  commands: string[]
  mission: boolean
  /** 같은 폴더를 여러 인스턴스(`name:label`)로 — 검출기·카메라별 감시 (§7-4) */
  multi_instance?: boolean
}

export type PluginScopeDesc = { scope: string; rules: string[][] }

export type PluginConfigValues = Record<string, number | boolean | string>

export type PluginStatus = {
  /** 인스턴스 id — 기본 인스턴스는 폴더 이름, 추가 인스턴스는 `name:label` */
  name: string
  plugin?: string
  instance?: string | null
  state: string
  pid: number | null
  started_at: number | null
  run_id: string | null
  config: PluginConfigValues | null
  cameras: Record<string, string> | null
  inputs?: Record<string, string> | null
  arms?: Record<string, string> | null
  /** 입력마다 소스가 지금 살아 있나 — 막지 않고 말만 한다 */
  inputs_live?: Record<string, boolean>
  /** 플러그인이 버스에 올린 상태 KV (`phase`·fps·…). 없으면 아직 안 올렸거나 죽었다 */
  kv: Record<string, unknown> | null
  log_tail?: string[]
  /** 유닛 소유자인가 — 자원 제한(`resources.memory`·`cpu`)이 실제로 걸리는 것은 유닛뿐이다 */
  limits_applied?: boolean
}

/** `GET /api/plugins/{name}/deps` — `python: venv | venv-isolated` 의 의존 상태 */
export type PluginDeps = {
  python: 'system' | 'venv' | 'venv-isolated'
  venv_ready: boolean
  requirements: boolean
  installing: boolean
  log_tail: string[]
}

export type PluginInfo = PluginStatus & {
  dir: string
  bundled: boolean
  error: string | null
  manifest?: PluginManifest
  scopes?: PluginScopeDesc[]
  saved: { config: PluginConfigValues; cameras: Record<string, string>; inputs?: Record<string, string>; arms?: Record<string, string> }
  /** 업데이트 설치의 결과 — 새로 깐 것이면 없다 */
  updated?: { from: string | null; to: string; deps_reset: boolean }
  /** 기본 인스턴스가 첫 줄 */
  instances?: PluginStatus[]
}

/** `GET /api/plugins/topics` — 살아 있는 소스 */
export type PluginSources = {
  detections: Record<string, { publisher?: string | null; ts?: number }>
  topics: Record<string, { publisher?: string | null; ts?: number; seq?: number }>
  /** 상태를 발행 중인 팔 — `{iface: {role, side, age_s}}` */
  arms?: Record<string, { role?: string | null; side?: string | null; age_s?: number | null }>
}

export type PluginTopic = { topic: string; publisher: string; seq: number; ts: number; data: unknown }

export type PluginRun = { id: string; plugin: string; bytes: number; mtime: number; live: boolean }

export const isRunning = (state: string | undefined) => state === 'running' || state === 'starting' || state === 'stopping'
