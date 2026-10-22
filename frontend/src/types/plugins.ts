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

export type PluginManifest = {
  name: string
  version: string
  title: string
  description: string
  entry: string
  python: 'system' | 'venv' | 'venv-isolated'
  cameras: PluginCameraSlot[]
  config: PluginConfigField[]
  permissions: string[]
  resources: { gpu: boolean; heartbeat: boolean; memory?: string | null; cpu?: string | null }
  /** false | 저장소 경로(`/detector`) | `ui/` | `server` — §8-2 */
  ui: boolean | string
  outputs: { previews: string[]; detections: string[]; state: { key: string; label?: string; unit?: string }[] }
  commands: string[]
  mission: boolean
}

export type PluginScopeDesc = { scope: string; rules: string[][] }

export type PluginConfigValues = Record<string, number | boolean | string>

export type PluginStatus = {
  name: string
  state: string
  pid: number | null
  started_at: number | null
  run_id: string | null
  config: PluginConfigValues | null
  cameras: Record<string, string> | null
  /** 플러그인이 버스에 올린 상태 KV (`phase`·fps·…). 없으면 아직 안 올렸거나 죽었다 */
  kv: Record<string, unknown> | null
  log_tail?: string[]
}

export type PluginInfo = PluginStatus & {
  dir: string
  bundled: boolean
  error: string | null
  manifest?: PluginManifest
  scopes?: PluginScopeDesc[]
  saved: { config: PluginConfigValues; cameras: Record<string, string> }
}

export type PluginRun = { id: string; plugin: string; bytes: number; mtime: number; live: boolean }

export const isRunning = (state: string | undefined) => state === 'running' || state === 'starting' || state === 'stopping'
