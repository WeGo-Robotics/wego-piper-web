import { useEffect, useState, useCallback, useRef } from 'react'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import type { Model } from '../types/models'
import ParamSlider from '../components/ParamSlider'
import LogViewer from '../components/LogViewer'
import EvalPanel from '../components/EvalPanel'
import TelemetryPanel, { type TelemetryData } from '../components/TelemetryPanel'
import ManualControlPanel from '../components/ManualControlPanel'
import VizPanel from '../components/VizPanel'

function CameraPreview({ cameraNames }: { cameraNames: string[] }) {
  const [ts, setTs] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTs(Date.now()), 500)
    return () => clearInterval(id)
  }, [])
  if (cameraNames.length === 0) return null
  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
      <span className="text-sm font-semibold">카메라</span>
      <div className="grid grid-cols-3 gap-3">
        {cameraNames.map((name) => (
          <div key={name}>
            <img
              src={`/api/inference/camera/${name}?t=${ts}`}
              alt={name}
              className="rounded border border-neutral-700 w-full aspect-[4/3] object-cover bg-neutral-900"
              onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.3' }}
              onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
            />
            <span className="text-xs text-neutral-400">{name}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

type ProcessState = 'idle' | 'starting' | 'running' | 'stopping' | 'error'
type ReadyArm = { iface: string; role: string; config: Record<string, unknown> }
type ReadyCam = { id: string; name: string; config: { width: number | null; height: number | null; fps: number | null } }
type ValidationResult = { valid: boolean; errors: string[]; warnings: string[] }

const PIPER_JOINTS = 7
// flow-matching 정책: RTC(real-time chunking) 가이던스 파라미터 사용
const RTC_POLICIES = ['smolvla', 'pi0', 'pi05']

export default function InferencePage() {
  const [readyFollowers, setReadyFollowers] = useState<ReadyArm[]>([])
  const [selectedFollower, setSelectedFollower] = useState<string>('')
  const [robotMode, setRobotMode] = useState<'single' | 'bimanual'>('single')
  const [leftFollower, setLeftFollower] = useState<string>('')
  const [rightFollower, setRightFollower] = useState<string>('')
  const [readyCameras, setReadyCameras] = useState<ReadyCam[]>([])
  const [models, setModels] = useState<Model[]>([])
  const [selectedModel, setSelectedModel] = useState<string>('')
  const [cameraMapping, setCameraMapping] = useState<Record<string, string>>({})
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [cliArgs, setCliArgs] = useState<string>('')
  const [cliEdited, setCliEdited] = useState(false)
  const [inferenceMode, setInferenceMode] = useState<'local' | 'server'>('local')
  const [serverAddress, setServerAddress] = useState('127.0.0.1:8088')
  const [aggregateFn, setAggregateFn] = useState('weighted_average')
  const [offsetCorrection, setOffsetCorrection] = useState(false)
  const [smoothing, setSmoothing] = useState('none')
  const [smoothingWindow, setSmoothingWindow] = useState(5)
  const [policyType, setPolicyType] = useState('smolvla')
  const [actionsPerChunk, setActionsPerChunk] = useState(100)
  const [processState, setProcessState] = useState<ProcessState>('idle')
  const [telemetry, setTelemetry] = useState<TelemetryData | null>(null)
  const [paused, setPaused] = useState(false)
  const [activeCameras, setActiveCameras] = useState<string[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const [lastLogFile, setLastLogFile] = useState<string | null>(null)
  const MAX_LOGS = 500
  const [taskText, setTaskText] = useState(() => localStorage.getItem('piper_task') || 'do the task')
  const [params, setParams] = useState({
    fps: 20, max_velocity: 180, max_gripper_velocity: 300,
    lowpass_alpha: 0.5, max_jerk: 0, interpolation_steps: 0, use_chunk_size: 0,
    max_guidance_weight: 10.0, execution_horizon: 10,
    temporal_ensemble_coeff: 0.01, n_action_steps: 50,
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
      } else if (msg.type === 'log_saved') {
        const d = msg.data as { csv_path: string; steps: number }
        const filename = d.csv_path.split('/').pop() ?? ''
        setLastLogFile(filename)
        setLogs((prev) => [...prev, `[INFO] CSV 로그 저장 완료: ${d.csv_path} (${d.steps} steps)`])
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
      api.get<{ state: string; cameras?: string[] }>('/inference/status').then((s) => {
        setProcessState(s.state as ProcessState)
        if (s.cameras && s.cameras.length > 0) setActiveCameras(s.cameras)
      }).catch(() => {}),
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
  // 파라미터 패널/정책 표시 기준: 선택 모델의 policy_type 우선, 없으면 수동 선택값
  const activePolicy = selectedModelData?.policy_type ?? policyType

  // 선택한 체크포인트의 policy_type을 정책 선택값에 자동 반영 (서버 모드 드롭다운 기본값)
  useEffect(() => {
    const pt = selectedModelData?.policy_type
    if (pt) setPolicyType(pt)
  }, [selectedModelData?.policy_type])

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
            robot_port: selectedFollower, camera_mapping: cameraMapping,
            params: { ...params, task: taskText },
            inference_mode: inferenceMode, server_address: serverAddress,
            policy_type: policyType, actions_per_chunk: actionsPerChunk,
            aggregate_fn: aggregateFn, offset_correction: offsetCorrection,
            smoothing, smoothing_window: smoothingWindow,
          }).then((r) => { if (!cliEdited) setCliArgs(r.command) }).catch(() => {})
        }
      }
    }).catch(() => {})
  }, [selectedModel, selectedFollower, cameraMapping, inferenceMode, serverAddress, policyType, actionsPerChunk, aggregateFn, offsetCorrection, smoothing, smoothingWindow])

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
    try {
      const model = models.find((m) => m.id === selectedModel)
      if (!model) return
      // start API를 직접 호출 (subprocess 리스트로 안전하게 전달)
      await api.post('/models/inference/start', {
        checkpoint_path: model.path,
        robot_type: 'piper_follower',
        robot_port: robotMode === 'single' ? selectedFollower : leftFollower,
        robot_ports: robotMode === 'bimanual' ? [leftFollower, rightFollower] : [],
        camera_mapping: cameraMapping,
        params: { ...params, task: taskText },
        inference_mode: inferenceMode,
        server_address: serverAddress,
        policy_type: policyType,
        actions_per_chunk: actionsPerChunk,
        aggregate_fn: aggregateFn,
        offset_correction: offsetCorrection,
        smoothing,
        smoothing_window: smoothingWindow,
      })
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
        inference_mode: inferenceMode, server_address: serverAddress,
        policy_type: policyType, actions_per_chunk: actionsPerChunk,
        aggregate_fn: aggregateFn, offset_correction: offsetCorrection,
        smoothing, smoothing_window: smoothingWindow,
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

  const isRunning = processState === 'running' || processState === 'starting' || processState === 'stopping'
  const followerReady = robotMode === 'bimanual'
    ? (!!leftFollower && !!rightFollower && leftFollower !== rightFollower)
    : !!selectedFollower
  const canStart = followerReady && !!selectedModel && !isRunning && validation?.valid === true

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
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold">로봇 (Follower)</h3>
                {readyFollowers.length >= 2 && (
                  <div className="flex gap-2 text-xs">
                    <label className="flex items-center gap-1 cursor-pointer">
                      <input type="radio" name="robotMode" value="single" checked={robotMode === 'single'}
                        onChange={() => setRobotMode('single')} className="accent-blue-500" />
                      단일
                    </label>
                    <label className="flex items-center gap-1 cursor-pointer">
                      <input type="radio" name="robotMode" value="bimanual" checked={robotMode === 'bimanual'}
                        onChange={() => setRobotMode('bimanual')} className="accent-blue-500" />
                      양팔
                    </label>
                  </div>
                )}
              </div>
              {readyFollowers.length > 0 ? (
                robotMode === 'bimanual' ? (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-neutral-400 w-10">왼팔</span>
                      <select value={leftFollower} onChange={(e) => setLeftFollower(e.target.value)}
                        className="flex-1 px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                        <option value="">선택...</option>
                        {readyFollowers.map((a) => (
                          <option key={a.iface} value={a.iface} disabled={a.iface === rightFollower}>
                            {a.iface}{a.iface === rightFollower ? ' (오른팔)' : ''}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-neutral-400 w-10">오른팔</span>
                      <select value={rightFollower} onChange={(e) => setRightFollower(e.target.value)}
                        className="flex-1 px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                        <option value="">선택...</option>
                        {readyFollowers.map((a) => (
                          <option key={a.iface} value={a.iface} disabled={a.iface === leftFollower}>
                            {a.iface}{a.iface === leftFollower ? ' (왼팔)' : ''}
                          </option>
                        ))}
                      </select>
                    </div>
                    {leftFollower && rightFollower && leftFollower === rightFollower && (
                      <p className="text-xs text-red-400">왼팔과 오른팔은 다른 로봇이어야 합니다</p>
                    )}
                  </div>
                ) : (
                  <select value={selectedFollower} onChange={(e) => setSelectedFollower(e.target.value)}
                    className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500">
                    <option value="">팔 선택...</option>
                    {readyFollowers.map((a) => <option key={a.iface} value={a.iface}>{a.iface}</option>)}
                  </select>
                )
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
                      <span className="w-24 text-neutral-300 font-medium">{cam.name}{cam.model_name && cam.model_name !== cam.name ? <span className="text-neutral-500 text-[10px] ml-1">({cam.model_name})</span> : ''}</span>
                      <span className="text-neutral-500 w-20">{cam.width && cam.height ? `${cam.width}x${cam.height}` : ''}</span>
                      <select value={cameraMapping[cam.name] ?? ''} onChange={(e) => setCameraMapping((prev) => ({ ...prev, [cam.name]: e.target.value }))}
                        className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500">
                        <option value="">카메라 선택...</option>
                        {readyCameras.map((c) => {
                          const usedBy = Object.entries(cameraMapping).find(([k, v]) => v === c.id && k !== cam.name)
                          return <option key={c.id} value={c.id} disabled={!!usedBy}>{c.id} ({c.name}){usedBy ? ` — ${usedBy[0]}에서 사용 중` : ''}</option>
                        })}
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

            {/* 추론 모드 */}
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
              <h3 className="text-sm font-semibold">추론 모드</h3>
              <div className="flex gap-4 text-sm">
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input type="radio" name="mode" value="local" checked={inferenceMode === 'local'}
                    onChange={() => { setInferenceMode('local'); setCliEdited(false) }}
                    className="accent-blue-500" />
                  로컬 (wrapper)
                </label>
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input type="radio" name="mode" value="server" checked={inferenceMode === 'server'}
                    onChange={() => { setInferenceMode('server'); setCliEdited(false) }}
                    className="accent-blue-500" />
                  서버 (gRPC)
                </label>
              </div>
              {inferenceMode === 'server' && (
                <div className="space-y-2">
                  <input type="text" value={serverAddress}
                    onChange={(e) => { setServerAddress(e.target.value); setCliEdited(false) }}
                    placeholder="서버 주소 (예: 127.0.0.1:8088)"
                    className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500" />
                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-neutral-400 w-20">Policy</span>
                    <select value={policyType} onChange={(e) => { setPolicyType(e.target.value); setCliEdited(false) }}
                      className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100">
                      <option value="smolvla">SmolVLA</option>
                      <option value="act">ACT</option>
                      <option value="diffusion">Diffusion</option>
                      <option value="pi0">PI0</option>
                      <option value="pi05">PI0.5</option>
                      <option value="pi0_fast">PI0-FAST</option>
                      <option value="vqbet">VQ-BeT</option>
                      <option value="tdmpc">TD-MPC</option>
                    </select>
                  </div>
                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-neutral-400 w-20">Chunk Size</span>
                    <input type="number" value={actionsPerChunk} min={10} max={200}
                      onChange={(e) => { setActionsPerChunk(Number(e.target.value)); setCliEdited(false) }}
                      className="w-20 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 text-center" />
                    <span className="text-neutral-500">actions/chunk</span>
                  </div>
                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-neutral-400 w-20">Aggregate</span>
                    <select value={aggregateFn} onChange={(e) => { setAggregateFn(e.target.value); setCliEdited(false) }}
                      className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100">
                      <option value="weighted_average">weighted_average (0.3/0.7)</option>
                      <option value="average">average (0.5/0.5)</option>
                      <option value="conservative">conservative (0.7/0.3)</option>
                      <option value="latest_only">latest_only (새 액션만)</option>
                    </select>
                  </div>
                  <label className="flex items-center gap-2 text-xs cursor-pointer">
                    <input type="checkbox" checked={offsetCorrection}
                      onChange={(e) => { setOffsetCorrection(e.target.checked); setCliEdited(false) }}
                      className="accent-blue-500" />
                    <span className="text-neutral-400">오프셋 보정 (chunk 간 점프 제거)</span>
                  </label>
                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-neutral-400 w-20">Smoothing</span>
                    <select value={smoothing} onChange={(e) => { setSmoothing(e.target.value); setCliEdited(false) }}
                      className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100">
                      <option value="none">none (필터 없음)</option>
                      <option value="moving_avg">이동평균</option>
                      <option value="exponential">지수이동평균 (EMA)</option>
                    </select>
                    {smoothing !== 'none' && (
                      <input type="number" value={smoothingWindow} min={3} max={20}
                        onChange={(e) => { setSmoothingWindow(Number(e.target.value)); setCliEdited(false) }}
                        className="w-14 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 text-center" />
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* Task (시작 전 설정) */}
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
              <h3 className="text-sm font-semibold">Task</h3>
              <input type="text" value={taskText}
                onChange={(e) => { setTaskText(e.target.value); localStorage.setItem('piper_task', e.target.value) }}
                placeholder="언어 명령어 입력..."
                className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500" />
            </div>

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
              {lastLogFile && (
                <a href={`/api/logs/download/${lastLogFile}`}
                  download className="inline-block mt-2 text-xs text-blue-400 hover:text-blue-300 underline">
                  CSV 로그 다운로드: {lastLogFile}
                </a>
              )}
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
        <span>로봇: <span className="text-blue-400">{robotMode === 'bimanual' ? `${leftFollower} / ${rightFollower}` : selectedFollower}</span>{robotMode === 'bimanual' && <span className="text-amber-400 ml-1">(양팔)</span>}</span>
        <span>모델: <span className="text-blue-400">{selectedModel}</span></span>
        <span>카메라: <span className="text-blue-400">{Object.values(cameraMapping).join(', ')}</span></span>
      </div>

      {/* 카메라 프리뷰 */}
      <CameraPreview cameraNames={reqs?.required_cameras.map((c) => c.name) ?? (activeCameras.length > 0 ? activeCameras : Object.keys(cameraMapping))} />
      {/* 모델 시각화 */}
      <VizPanel cameraCount={reqs?.required_cameras.length ?? 1} />

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-4 gap-6">
        {/* 1열: Task + 실행 속도 */}
        <div className="space-y-4">
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <h3 className="text-sm font-semibold">Task</h3>
            <input type="text" value={taskText}
              onChange={(e) => {
                setTaskText(e.target.value)
                localStorage.setItem('piper_task', e.target.value)
                clearTimeout(debounceRef.current)
                debounceRef.current = setTimeout(() => {
                  api.post('/params', { params: { task: e.target.value } }).catch(() => {})
                }, 500)
              }}
              placeholder="언어 명령어 입력..."
              className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500" />
          </div>
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-4">
            <h3 className="text-sm font-semibold">실행 속도</h3>
            <ParamSlider label="FPS" value={params.fps ?? 30} min={1} max={60} step={1}
              onChange={(v) => handleParamChange('fps', v)} />
            <ParamSlider label="관절 속도 (deg/s)" value={params.max_velocity ?? 180} min={0} max={500} step={10}
              onChange={(v) => handleParamChange('max_velocity', v)} />
            <div className="space-y-1">
              <div className="flex justify-between text-xs">
                <span className="text-neutral-400">관절 속도 제한 (%)</span>
                <span className="text-neutral-100">{Math.round(((params.max_velocity ?? 180) / 500) * 100)}%</span>
              </div>
              <input type="range" value={Math.round(((params.max_velocity ?? 180) / 500) * 100)}
                min={0} max={100} step={5}
                onChange={(e) => handleParamChange('max_velocity', Math.round(Number(e.target.value) * 500 / 100))}
                className="w-full accent-blue-500" />
            </div>
            <ParamSlider label="그리퍼 속도 (%/s)" value={params.max_gripper_velocity ?? 100} min={0} max={500} step={10}
              onChange={(v) => handleParamChange('max_gripper_velocity', v)} />
            <div className="space-y-1">
              <div className="flex justify-between text-xs">
                <span className="text-neutral-400">그리퍼 속도 제한 (%)</span>
                <span className="text-neutral-100">{Math.round(((params.max_gripper_velocity ?? 100) / 500) * 100)}%</span>
              </div>
              <input type="range" value={Math.round(((params.max_gripper_velocity ?? 100) / 500) * 100)}
                min={0} max={100} step={5}
                onChange={(e) => handleParamChange('max_gripper_velocity', Math.round(Number(e.target.value) * 500 / 100))}
                className="w-full accent-blue-500" />
            </div>
          </div>
        </div>

        {/* 2열: 진동 감소 + 정책 파라미터 */}
        <div className="space-y-4">
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-4">
            <h3 className="text-sm font-semibold">진동 감소</h3>
            <ParamSlider label="저역통과 필터 α (1.0=OFF)" value={params.lowpass_alpha ?? 0.5} min={0.05} max={1} step={0.05}
              onChange={(v) => handleParamChange('lowpass_alpha', v)} />
            <ParamSlider label="Jerk 제한 (deg/s², 0=OFF)" value={params.max_jerk ?? 0} min={0} max={5000} step={100}
              onChange={(v) => handleParamChange('max_jerk', v)} />
            <ParamSlider label="보간 스텝 (0=OFF)" value={params.interpolation_steps ?? 0} min={0} max={10} step={1}
              onChange={(v) => handleParamChange('interpolation_steps', v)} />
            <ParamSlider label="액션 청크 크기 (0=전부)" value={params.use_chunk_size ?? 0} min={0} max={200} step={5}
              onChange={(v) => handleParamChange('use_chunk_size', v)} />
          </div>
          {RTC_POLICIES.includes(activePolicy) && (
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-4">
              <h3 className="text-sm font-semibold">RTC 파라미터</h3>
              <ParamSlider label="max_guidance_weight" value={params.max_guidance_weight} min={0} max={50} step={0.5}
                onChange={(v) => handleParamChange('max_guidance_weight', v)} />
              <ParamSlider label="execution_horizon" value={params.execution_horizon} min={1} max={100} step={1}
                onChange={(v) => handleParamChange('execution_horizon', v)} />
            </div>
          )}
          {activePolicy === 'act' && (
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-4">
              <h3 className="text-sm font-semibold">ACT 파라미터</h3>
              <ParamSlider label="temporal_ensemble_coeff" value={params.temporal_ensemble_coeff} min={0} max={1} step={0.001}
                onChange={(v) => handleParamChange('temporal_ensemble_coeff', v)} />
              <ParamSlider label="n_action_steps" value={params.n_action_steps} min={1} max={100} step={1}
                onChange={(v) => handleParamChange('n_action_steps', v)} />
            </div>
          )}
        </div>

        {/* 3열: 텔레메트리 + 수동/평가 */}
        <div className="space-y-4">
          <TelemetryPanel
            data={telemetry}
            targetFps={20}
            cameraNames={[]}
          />
          <ManualControlPanel
            currentJoints={telemetry?.joints ?? []}
            disabled={!paused}
          />
          <EvalPanel checkpoint={selectedModel} />
        </div>

        {/* 4열: 로그 */}
        <div className="space-y-4">
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <h3 className="text-sm font-semibold mb-2">로그</h3>
            <LogViewer logs={logs} onClear={() => setLogs([])} />
            {lastLogFile && !isRunning && (
              <a href={`/api/logs/download/${lastLogFile}`}
                download className="inline-block mt-2 text-xs text-blue-400 hover:text-blue-300 underline">
                CSV 로그 다운로드: {lastLogFile}
              </a>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
