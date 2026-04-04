import { useEffect, useState, useCallback, useRef } from 'react'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import type { Model } from '../types/models'
import ParamSlider from '../components/ParamSlider'
import LogViewer from '../components/LogViewer'
import EvalPanel from '../components/EvalPanel'
import TelemetryPanel, { type TelemetryData } from '../components/TelemetryPanel'
import ManualControlPanel from '../components/ManualControlPanel'

type ProcessState = 'idle' | 'starting' | 'running' | 'stopping' | 'error'
type ReadyArm = { iface: string; role: string; config: Record<string, unknown> }
type ReadyCam = { id: string; name: string; config: { width: number | null; height: number | null; fps: number | null } }
type ValidationResult = { valid: boolean; errors: string[]; warnings: string[] }

const PIPER_JOINTS = 7

export default function InferencePage() {
  const [readyFollowers, setReadyFollowers] = useState<ReadyArm[]>([])
  const [selectedFollower, setSelectedFollower] = useState<string>('')
  const [readyCameras, setReadyCameras] = useState<ReadyCam[]>([])
  const [models, setModels] = useState<Model[]>([])
  const [selectedModel, setSelectedModel] = useState<string>('')
  const [cameraMapping, setCameraMapping] = useState<Record<string, string>>({})
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [cliArgs, setCliArgs] = useState<string>('')
  const [cliEdited, setCliEdited] = useState(false)
  const [processState, setProcessState] = useState<ProcessState>('idle')
  const [telemetry, setTelemetry] = useState<TelemetryData | null>(null)
  const [paused, setPaused] = useState(false)
  const [logs, setLogs] = useState<string[]>([])
  const MAX_LOGS = 500
  const [params, setParams] = useState({
    max_guidance_weight: 10.0, execution_horizon: 10,
    temporal_ensemble_coeff: 0.01, n_action_steps: 1,
  })
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined)

  const { connected } = useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (msg.type === 'state') {
        setProcessState(msg.data as ProcessState)
        if (msg.data !== 'running') setTelemetry(null)
      } else if (msg.type === 'telemetry') {
        const td = msg.data as TelemetryData & { paused?: boolean }
        setTelemetry(td)
        if (td.paused !== undefined) setPaused(td.paused)
      } else if (msg.type === 'log') setLogs((prev) => {
        const next = [...prev, msg.data as string]
        return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next
      })
    }, []),
  })

  useEffect(() => {
    Promise.all([
      api.get<ReadyArm[]>('/robots/ready').then((all) => {
        const followers = all.filter((a) => a.role === 'follower')
        setReadyFollowers(followers)
        if (followers.length === 1) setSelectedFollower(followers[0].iface)
      }).catch(() => {}),
      api.get<ReadyCam[]>('/cameras/ready').then(setReadyCameras).catch(() => {}),
      api.get<Model[]>('/models').then(setModels).catch(() => {}),
      api.get<{ state: string }>('/inference/status').then((s) => setProcessState(s.state as ProcessState)).catch(() => {}),
    ]).then(() => {
      api.get<{ follower_iface: string; model_id: string; camera_mapping: Record<string, string> }>('/inference/selection')
        .then((sel) => {
          if (sel.follower_iface) setSelectedFollower(sel.follower_iface)
          if (sel.model_id) setSelectedModel(sel.model_id)
          if (sel.camera_mapping && Object.keys(sel.camera_mapping).length > 0) setCameraMapping(sel.camera_mapping)
        }).catch(() => {})
    })
  }, [])

  const selectedModelData = models.find((m) => m.id === selectedModel)
  const reqs = selectedModelData?.requirements

  useEffect(() => {
    if (!reqs) { setCameraMapping({}); setValidation(null); return }
    setCameraMapping((prev) => {
      const next: Record<string, string> = {}
      for (const cam of reqs.required_cameras) { next[cam.name] = prev[cam.name] ?? '' }
      return next
    })
  }, [selectedModel])

  useEffect(() => {
    if (!selectedModel || !selectedFollower || !reqs) { setValidation(null); return }
    const allMapped = reqs.required_cameras.every((c) => cameraMapping[c.name])
    if (!allMapped) { setValidation(null); return }
    api.post<ValidationResult>('/inference/validate', {
      model_id: selectedModel, follower_iface: selectedFollower, camera_mapping: cameraMapping,
    }).then((v) => {
      setValidation(v)
      if (v.valid) {
        const model = models.find((m) => m.id === selectedModel)
        if (model) {
          api.post<{ command: string }>('/models/inference/preview', {
            checkpoint_path: model.path, robot_type: 'piper_follower',
            robot_port: selectedFollower, camera_mapping: cameraMapping, params,
          }).then((r) => { if (!cliEdited) setCliArgs(r.command) }).catch(() => {})
        }
      }
    }).catch(() => {})
  }, [selectedModel, selectedFollower, cameraMapping])

  const saveSelectionRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => {
    clearTimeout(saveSelectionRef.current)
    saveSelectionRef.current = setTimeout(() => {
      api.post('/inference/selection', {
        follower_iface: selectedFollower, model_id: selectedModel, camera_mapping: cameraMapping,
      }).catch(() => {})
    }, 500)
  }, [selectedFollower, selectedModel, cameraMapping])

  const handleStart = async () => {
    if (!cliArgs.trim()) return
    try {
      const args: string[] = []
      let current = '', inBrace = 0, inQuote = false
      for (const ch of cliArgs.trim()) {
        if (ch === '"' || ch === "'") { inQuote = !inQuote; current += ch; continue }
        if (!inQuote && ch === '{') { inBrace++; current += ch; continue }
        if (!inQuote && ch === '}') { inBrace--; current += ch; continue }
        if (!inQuote && inBrace === 0 && /\s/.test(ch)) { if (current) { args.push(current); current = '' }; continue }
        current += ch
      }
      if (current) args.push(current)
      await api.post('/models/inference/start-custom', { args })
    } catch (e) {
      const msg = e instanceof Error ? e.message : '알 수 없는 오류'
      setLogs((prev) => [...prev, `[ERROR] 추론 시작 실패: ${msg}`])
    }
  }
  const handleResetCli = () => {
    setCliEdited(false)
    const model = models.find((m) => m.id === selectedModel)
    if (model) {
      api.post<{ command: string }>('/models/inference/preview', {
        checkpoint_path: model.path, robot_type: 'piper_follower', params,
      }).then((r) => setCliArgs(r.command)).catch(() => {})
    }
  }
  const handleStop = async () => { await api.post('/models/inference/stop') }
  const handleParamChange = (key: string, value: number) => {
    setParams((prev) => ({ ...prev, [key]: value }))
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      api.post('/params', { params: { [key]: value } }).catch(() => {})
    }, 200)
  }

  const isRunning = processState === 'running' || processState === 'starting'
  const canStart = !!selectedFollower && !!selectedModel && !isRunning && validation?.valid === true

  // ── 정지 상태 UI ──
  if (!isRunning) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">추론</h1>
          <div className="flex items-center gap-3">
            <span className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
            <span className="text-xs text-neutral-400">{connected ? '연결됨' : '연결 끊김'}</span>
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
              processState === 'error' ? 'bg-red-500/20 text-red-400' : 'bg-neutral-700 text-neutral-400'
            }`}>{processState}</span>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-[1fr_2fr] gap-6">
          {/* 좌측: 설정 */}
          <div className="space-y-4">
            {/* Follower */}
            <div className={`rounded-lg border p-4 space-y-2 ${readyFollowers.length > 0 ? 'border-neutral-700 bg-neutral-800' : 'border-amber-500/50 bg-amber-500/10'}`}>
              <h3 className="text-sm font-semibold">로봇 (Follower)</h3>
              {readyFollowers.length > 0 ? (
                <select value={selectedFollower} onChange={(e) => setSelectedFollower(e.target.value)}
                  className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500">
                  <option value="">팔 선택...</option>
                  {readyFollowers.map((a) => <option key={a.iface} value={a.iface}>{a.iface}</option>)}
                </select>
              ) : (
                <div className="text-sm text-amber-300">
                  <p>등록된 follower가 없습니다</p>
                  <a href="/robots" className="text-blue-400 underline text-xs">로봇 페이지에서 등록하기</a>
                </div>
              )}
            </div>

            {/* 모델 */}
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
              <h3 className="text-sm font-semibold">체크포인트</h3>
              <select value={selectedModel} onChange={(e) => setSelectedModel(e.target.value)}
                className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500">
                <option value="">모델 선택...</option>
                {models.map((m) => <option key={m.id} value={m.id}>{m.id} ({m.policy_type})</option>)}
              </select>
            </div>

            {/* 모델 요구사항 + 카메라 매핑 */}
            {reqs && (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
                <h3 className="text-sm font-semibold">모델 요구사항</h3>
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-neutral-400">관절:</span>
                  <span>{reqs.state_dim}개</span>
                  {selectedFollower && (reqs.state_dim === PIPER_JOINTS
                    ? <span className="text-green-400">✓</span>
                    : <span className="text-red-400">✗ (follower: {PIPER_JOINTS})</span>
                  )}
                </div>
                <div className="space-y-2">
                  <span className="text-xs text-neutral-400">카메라 ({reqs.required_cameras.length}대):</span>
                  {reqs.required_cameras.map((cam) => (
                    <div key={cam.name} className="flex items-center gap-2 text-xs">
                      <span className="w-16 text-neutral-300 font-medium">{cam.name}</span>
                      <span className="text-neutral-500 w-20">{cam.width && cam.height ? `${cam.width}x${cam.height}` : ''}</span>
                      <select value={cameraMapping[cam.name] ?? ''} onChange={(e) => setCameraMapping((prev) => ({ ...prev, [cam.name]: e.target.value }))}
                        className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500">
                        <option value="">카메라 선택...</option>
                        {readyCameras.map((c) => <option key={c.id} value={c.id}>{c.id} ({c.name})</option>)}
                      </select>
                      {cameraMapping[cam.name] ? <span className="text-green-400">✓</span> : <span className="text-neutral-500">-</span>}
                    </div>
                  ))}
                </div>
                {validation && (
                  <div className="space-y-1">
                    {validation.errors.map((e, i) => <p key={i} className="text-xs text-red-400">✗ {e}</p>)}
                    {validation.warnings.map((w, i) => <p key={i} className="text-xs text-amber-400">⚠ {w}</p>)}
                    {validation.valid && <p className="text-xs text-green-400">✓ 추론 준비 완료</p>}
                  </div>
                )}
              </div>
            )}

            {/* CLI */}
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold">CLI 인자</h3>
                {cliEdited && <button onClick={handleResetCli} className="text-[10px] text-neutral-400 hover:text-blue-400">초기화</button>}
              </div>
              <textarea value={cliArgs} onChange={(e) => { setCliArgs(e.target.value); setCliEdited(true) }}
                rows={4} placeholder="모델과 카메라를 매핑하면 CLI 명령어가 자동 생성됩니다"
                className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-xs font-mono text-neutral-100 focus:outline-none focus:border-blue-500 resize-y" />
              {cliEdited && <p className="text-[10px] text-amber-400">직접 편집됨</p>}
              <button onClick={handleStart} disabled={!canStart || !cliArgs.trim()}
                className="w-full py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium disabled:opacity-50">
                추론 시작
              </button>
            </div>
          </div>

          {/* 우측: 로그 */}
          <div className="space-y-4">
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
              <h3 className="text-sm font-semibold mb-2">로그</h3>
              <LogViewer logs={logs} onClear={() => setLogs([])} />
            </div>
          </div>
        </div>
      </div>
    )
  }

  // ── 추론 중 UI ──
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">추론</h1>
        <div className="flex items-center gap-3">
          <span className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
          <span className="text-xs text-neutral-400">{connected ? '연결됨' : '연결 끊김'}</span>
          <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400">
            {processState}
          </span>
          {/* 일시정지/재개 + 정지 */}
          <button onClick={async () => {
            if (paused) {
              await api.post('/params/resume')
              setPaused(false)
            } else {
              await api.post('/params/pause')
              setPaused(true)
            }
          }}
            className={`px-4 py-1.5 rounded text-sm font-medium ${paused ? 'bg-green-600 hover:bg-green-500 text-white' : 'bg-amber-600 hover:bg-amber-500 text-white'}`}>
            {paused ? '추론 재개' : '일시정지'}
          </button>
          <button onClick={handleStop}
            className="px-4 py-1.5 rounded bg-red-600 hover:bg-red-500 text-white text-sm font-medium">
            정지
          </button>
        </div>
      </div>

      {/* 요약 바 */}
      <div className={`flex items-center gap-4 text-xs rounded-lg border px-4 py-2 ${paused ? 'border-amber-500/50 bg-amber-500/10 text-amber-300' : 'border-neutral-700 bg-neutral-800 text-neutral-400'}`}>
        {paused && <span className="font-semibold">⏸ 일시정지</span>}
        <span>로봇: <span className="text-blue-400">{selectedFollower}</span></span>
        <span>모델: <span className="text-blue-400">{selectedModel}</span></span>
        <span>카메라: <span className="text-blue-400">{Object.values(cameraMapping).join(', ')}</span></span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_2fr] gap-6">
        {/* 좌측: 실시간 제어 */}
        <div className="space-y-4">
          {/* Task */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <h3 className="text-sm font-semibold">Task</h3>
            <input type="text" defaultValue="do the task"
              onChange={(e) => {
                clearTimeout(debounceRef.current)
                debounceRef.current = setTimeout(() => {
                  api.post('/params', { params: { task: e.target.value } }).catch(() => {})
                }, 500)
              }}
              placeholder="언어 명령어 입력..."
              className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500" />
          </div>

          {/* 파라미터 슬라이더 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-4">
            <h3 className="text-sm font-semibold">RTC 파라미터</h3>
            <ParamSlider label="max_guidance_weight" value={params.max_guidance_weight} min={0} max={50} step={0.5}
              onChange={(v) => handleParamChange('max_guidance_weight', v)} />
            <ParamSlider label="execution_horizon" value={params.execution_horizon} min={1} max={100} step={1}
              onChange={(v) => handleParamChange('execution_horizon', v)} />
            <h3 className="text-sm font-semibold pt-2">ACT 파라미터</h3>
            <ParamSlider label="temporal_ensemble_coeff" value={params.temporal_ensemble_coeff} min={0} max={1} step={0.001}
              onChange={(v) => handleParamChange('temporal_ensemble_coeff', v)} />
            <ParamSlider label="n_action_steps" value={params.n_action_steps} min={1} max={100} step={1}
              onChange={(v) => handleParamChange('n_action_steps', v)} />
          </div>

          {/* 수동 조작 */}
          <ManualControlPanel
            currentJoints={telemetry?.joints ?? []}
            disabled={!paused}
          />

          {/* 평가 */}
          <EvalPanel checkpoint={selectedModel} />
        </div>

        {/* 우측: 텔레메트리 + 로그 */}
        <div className="space-y-4">
          <TelemetryPanel
            data={telemetry}
            targetFps={30}
            cameraNames={reqs?.required_cameras.map((c) => c.name) ?? []}
          />
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <h3 className="text-sm font-semibold mb-2">로그</h3>
            <LogViewer logs={logs} onClear={() => setLogs([])} />
          </div>
        </div>
      </div>
    </div>
  )
}
