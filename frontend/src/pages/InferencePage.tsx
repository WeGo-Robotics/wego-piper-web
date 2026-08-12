import { useEffect, useState, useCallback, useRef, type ReactNode } from 'react'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import type { ProcessState } from '../types/ws'
import { useActivity, isStateMessage } from '../hooks/useActivity'
import { usePolicies } from '../hooks/usePolicies'
import { useParamSpec } from '../hooks/useParamSpec'
import type { Model } from '../types/models'
import ParamSlider from '../components/ParamSlider'
import PresetBar from '../components/PresetBar'
import LogViewer from '../components/LogViewer'
import EvalPanel from '../components/EvalPanel'
import TelemetryPanel, { type TelemetryData } from '../components/TelemetryPanel'
import ManualControlPanel from '../components/ManualControlPanel'
import { camOptionText, type ReadyCam } from '../types/camera'

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

// 접을 수 있는 카드 (제목 클릭으로 토글, 접힘 상태는 localStorage에 저장)
function CollapsibleCard({ title, storageKey, defaultOpen = true, children }: {
  title: string
  storageKey: string
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(() => {
    const v = localStorage.getItem(storageKey)
    return v === null ? defaultOpen : v === '1'
  })
  const toggle = () => setOpen((o) => {
    localStorage.setItem(storageKey, o ? '0' : '1')
    return !o
  })
  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800">
      <button onClick={toggle}
        className="w-full flex items-center justify-between px-4 py-3 text-sm font-semibold text-left hover:bg-neutral-700/40 rounded-lg">
        <span>{title}</span>
        <span className="text-neutral-400 text-xs">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="px-4 pb-4 space-y-4">{children}</div>}
    </div>
  )
}

type ReadyArm = { iface: string; role: string; config: Record<string, unknown> }
type ValidationResult = { valid: boolean; errors: string[]; warnings: string[]; robot_joints?: number }

// 관절 수와 정책 목록은 백엔드에서 온다 —
// robot_joints 는 /inference/validate 응답, 정책 목록은 usePolicies()

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
  const [debugMode, setDebugMode] = useState(false)
  const [processState, setProcessState] = useState<ProcessState>('idle')
  const [telemetry, setTelemetry] = useState<TelemetryData | null>(null)
  const [paused, setPaused] = useState(false)
  const [showResetConfirm, setShowResetConfirm] = useState(false)
  const [activeCameras, setActiveCameras] = useState<string[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const [lastLogFile, setLastLogFile] = useState<string | null>(null)
  const MAX_LOGS = 500
  const [taskText, setTaskText] = useState(() => localStorage.getItem('piper_task') || 'do the task')
  const [params, setParams] = useState(() => {
    // 이전 실행에서 맞춘 파라미터(진동 감소/이동속도 등)를 그대로 복원.
    // 새로 추가된 파라미터는 defaults가 채워지도록 병합.
    const defaults = {
      fps: 20, max_velocity: 180, max_gripper_velocity: 300,
      lowpass_alpha: 0.5, max_jerk: 0, interpolation_steps: 0, use_chunk_size: 0,
      refill_threshold_pct: 20, gripper_bypass_filter: true,
      max_guidance_weight: 10.0, execution_horizon: 10,
      temporal_ensemble_coeff: 0.01,
      // n_action_steps 는 로컬 모드에서 use_chunk_size 와 같은 변수를 가리켰다 →
      // "받는 액션 청크 크기" 슬라이더 하나로 통합했다.
    }
    try {
      const saved = JSON.parse(localStorage.getItem('piper_inference_params') || '{}') as Partial<typeof defaults>
      return { ...defaults, ...saved }
    } catch {
      return defaults
    }
  })
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined)

  // 배타 규칙은 백엔드 exclusivity.py 한 곳에만 있다
  const { isBlocked, blockedBy, refresh: refreshActivity } = useActivity()
  // 정책 목록·RTC 여부는 백엔드 core/policies.py 한 곳에서 온다
  const { inferable, isRtc } = usePolicies()
  const [activePreset, setActivePreset] = useState('')
  // 파라미터 범위·기본값은 백엔드 core/inference_params.py 에서 온다
  const { rangeOf, defaults: specDefaults } = useParamSpec()

  // 스펙이 도착하면 아직 없는 파라미터를 서버 기본값으로 보충한다
  // (프론트에 기본값을 또 적지 않기 위해)
  useEffect(() => {
    if (!Object.keys(specDefaults).length) return
    setParams((prev) => {
      const merged = { ...specDefaults, ...prev }
      return JSON.stringify(merged) === JSON.stringify(prev) ? prev : merged
    })
  }, [specDefaults])

  const { connected } = useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (isStateMessage(msg.type)) refreshActivity()
      if (msg.type === 'state') {
        setProcessState(msg.data as ProcessState)
        if (msg.data !== 'running') setTelemetry(null)
      } else if (msg.type === 'telemetry') {
        const td = msg.data as TelemetryData & { paused?: boolean }
        setTelemetry(td)
        if (td.paused !== undefined) setPaused(td.paused)
      } else if (msg.type === 'log_saved') {
        const d = msg.data as { csv_path: string; steps: number; debug_dir?: string | null }
        const filename = d.csv_path.split('/').pop() ?? ''
        setLastLogFile(filename)
        setLogs((prev) => {
          const next = [...prev, `[INFO] CSV 로그 저장 완료: ${d.csv_path} (${d.steps} steps)`]
          if (d.debug_dir) next.push(`[INFO] 디버그 기록 저장됨: ${d.debug_dir} → "디버그" 탭에서 확인`)
          return next
        })
      } else if (msg.type === 'log') setLogs((prev) => {
        const next = [...prev, msg.data as string]
        return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next
      })
    }, [refreshActivity]),
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

  // 파라미터 변경 시 localStorage에 저장 → 다음 실행에서 그대로 복원
  useEffect(() => {
    localStorage.setItem('piper_inference_params', JSON.stringify(params))
  }, [params])

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
            checkpoint_path: model.path,
            robot_port: selectedFollower, camera_mapping: cameraMapping,
            params: { ...params, task: taskText },
            inference_mode: inferenceMode, server_address: serverAddress,
            policy_type: policyType, actions_per_chunk: actionsPerChunk,
            aggregate_fn: aggregateFn, offset_correction: offsetCorrection,
            smoothing, smoothing_window: smoothingWindow, debug_mode: debugMode,
          }).then((r) => { if (!cliEdited) setCliArgs(r.command) }).catch(() => {})
        }
      }
    }).catch(() => {})
  }, [selectedModel, selectedFollower, cameraMapping, inferenceMode, serverAddress, policyType, actionsPerChunk, aggregateFn, offsetCorrection, smoothing, smoothingWindow, debugMode])

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
        debug_mode: debugMode,
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
        checkpoint_path: model.path, params,
        inference_mode: inferenceMode, server_address: serverAddress,
        policy_type: policyType, actions_per_chunk: actionsPerChunk,
        aggregate_fn: aggregateFn, offset_correction: offsetCorrection,
        smoothing, smoothing_window: smoothingWindow,
      }).then((r) => setCliArgs(r.command)).catch(() => {})
    }
  }
  const handleStop = async () => { await api.post('/models/inference/stop') }
  const handleReset = async () => {
    // window.confirm은 브라우저 이벤트 루프를 블로킹해 E-stop heartbeat(500ms)가 멈추고,
    // 2초 타임아웃에 걸려 추론이 강제 종료된다. 논블로킹 React 모달로 확인받는다.
    setShowResetConfirm(false)
    await api.post('/params/reset').catch(() => {})
    setPaused(false)
  }
  const handleParamChange = (key: string, value: number | boolean) => {
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
  const inferBlockedBy = blockedBy('inference')
  const canStart = followerReady && !!selectedModel && !isRunning && validation?.valid === true && !isBlocked('inference')

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
                  {selectedFollower && validation?.robot_joints != null && (
                    reqs.state_dim === validation.robot_joints
                      ? <span className="text-green-400">✓</span>
                      : <span className="text-red-400">✗ (follower: {validation.robot_joints})</span>
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
                          return <option key={c.id} value={c.id} disabled={!!usedBy}>{camOptionText(c)}{usedBy ? ` — ${usedBy[0]}에서 사용 중` : ''}</option>
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
                      {inferable.map((p) => (
                        <option key={p.type} value={p.type}>{p.label}</option>
                      ))}
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
              <label className="flex items-center gap-2 text-xs cursor-pointer">
                <input type="checkbox" checked={debugMode}
                  onChange={(e) => { setDebugMode(e.target.checked); setCliEdited(false) }}
                  className="accent-blue-500" />
                <span className="text-neutral-400">디버그 모드 (주고받은 모든 데이터를 별도 폴더에 기록)</span>
              </label>
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
              {inferBlockedBy && (
                <p className="text-[10px] text-amber-400">{inferBlockedBy} 실행 중 — 먼저 중지하세요</p>
              )}
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
      {/* 원위치+리셋 확인 모달 (window.confirm은 heartbeat를 막아 E-stop을 유발하므로 사용 금지) */}
      {showResetConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
          onClick={() => setShowResetConfirm(false)}>
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-6 max-w-sm mx-4 space-y-4"
            onClick={(e) => e.stopPropagation()}>
            <h3 className="text-lg font-semibold">원위치 + 리셋</h3>
            <p className="text-sm text-neutral-300">
              로봇을 원위치로 되돌리고 액션 버퍼를 비운 뒤 추론을 새로 시작합니다.
              팔이 크게 움직입니다. 진행할까요?
            </p>
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowResetConfirm(false)}
                className="px-4 py-1.5 rounded bg-neutral-700 hover:bg-neutral-600 text-white text-sm font-medium">
                취소
              </button>
              <button onClick={handleReset}
                className="px-4 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium">
                원위치+리셋
              </button>
            </div>
          </div>
        </div>
      )}
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
          <button onClick={() => setShowResetConfirm(true)}
            className="px-4 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium">
            원위치+리셋
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

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
        {/* 1열: Task + 실행 속도 */}
        <div className="space-y-4">
          <CollapsibleCard title="Task" storageKey="piper_fold_task">
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
          </CollapsibleCard>
          {/* 프리셋 — 속도·필터는 팔마다 다르므로 device scope */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <PresetBar domain="inference" scope="device" policyType={activePolicy}
              values={() => params} onApply={(v) => setParams((prev) => ({ ...prev, ...v }))}
              defaults={specDefaults} onSelect={setActivePreset} />
          </div>
          <CollapsibleCard title="실행 속도" storageKey="piper_fold_speed">
            <ParamSlider label="FPS" value={params.fps ?? 20} {...rangeOf('fps', { min: 1, max: 60, step: 1 })}
              onChange={(v) => handleParamChange('fps', v)} />
            <ParamSlider label="관절 속도 (deg/s)" value={params.max_velocity ?? 180} {...rangeOf('max_velocity', { min: 0, max: 500, step: 10 })}
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
            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input type="checkbox" checked={params.gripper_bypass_filter ?? true}
                onChange={(e) => handleParamChange('gripper_bypass_filter', e.target.checked)}
                className="accent-blue-500" />
              <span className="text-neutral-300">그리퍼 속도 제한/필터 미적용 (원본 그대로)</span>
            </label>
            <div className={params.gripper_bypass_filter ? 'opacity-40 pointer-events-none space-y-4' : 'space-y-4'}>
              <ParamSlider label="그리퍼 속도 (%/s)" value={params.max_gripper_velocity ?? 300} {...rangeOf('max_gripper_velocity', { min: 0, max: 500, step: 10 })}
                onChange={(v) => handleParamChange('max_gripper_velocity', v)} />
              <div className="space-y-1">
                <div className="flex justify-between text-xs">
                  <span className="text-neutral-400">그리퍼 속도 제한 (%)</span>
                  <span className="text-neutral-100">{Math.round(((params.max_gripper_velocity ?? 300) / 500) * 100)}%</span>
                </div>
                <input type="range" value={Math.round(((params.max_gripper_velocity ?? 300) / 500) * 100)}
                  min={0} max={100} step={5}
                  onChange={(e) => handleParamChange('max_gripper_velocity', Math.round(Number(e.target.value) * 500 / 100))}
                  className="w-full accent-blue-500" />
              </div>
            </div>
          </CollapsibleCard>
        </div>

        {/* 2열: 진동 감소 + 정책 파라미터 */}
        <div className="space-y-4">
          <CollapsibleCard title="진동 감소" storageKey="piper_fold_smooth">
            <ParamSlider label="저역통과 필터 α (1.0=OFF)" value={params.lowpass_alpha ?? 0.5} {...rangeOf('lowpass_alpha', { min: 0.05, max: 1, step: 0.05 })}
              onChange={(v) => handleParamChange('lowpass_alpha', v)} />
            <ParamSlider label="Jerk 제한 (deg/s², 0=OFF)" value={params.max_jerk ?? 0} {...rangeOf('max_jerk', { min: 0, max: 5000, step: 100 })}
              onChange={(v) => handleParamChange('max_jerk', v)} />
            <ParamSlider label="보간 스텝 (0=OFF)" value={params.interpolation_steps ?? 0} {...rangeOf('interpolation_steps', { min: 0, max: 10, step: 1 })}
              onChange={(v) => handleParamChange('interpolation_steps', v)} />
            <ParamSlider label="받는 액션 청크 크기 (0=모델 전체)" value={params.use_chunk_size ?? 0} {...rangeOf('use_chunk_size', { min: 0, max: 200, step: 5 })}
              onChange={(v) => handleParamChange('use_chunk_size', v)} />
            <ParamSlider label="재추론 트리거 (큐 잔량 ≤ %, 0=소진 시)" value={params.refill_threshold_pct ?? 20} {...rangeOf('refill_threshold_pct', { min: 0, max: 100, step: 5 })}
              onChange={(v) => handleParamChange('refill_threshold_pct', v)} />
          </CollapsibleCard>
          {isRtc(activePolicy) && (
            <CollapsibleCard title="RTC 파라미터" storageKey="piper_fold_rtc" defaultOpen={false}>
              <ParamSlider label="max_guidance_weight" value={params.max_guidance_weight} {...rangeOf('max_guidance_weight', { min: 0, max: 50, step: 0.5 })}
                onChange={(v) => handleParamChange('max_guidance_weight', v)} />
              <ParamSlider label="execution_horizon" value={params.execution_horizon} {...rangeOf('execution_horizon', { min: 1, max: 100, step: 1 })}
                onChange={(v) => handleParamChange('execution_horizon', v)} />
            </CollapsibleCard>
          )}
          {activePolicy === 'act' && (
            <CollapsibleCard title="ACT 파라미터" storageKey="piper_fold_act">
              <ParamSlider label="temporal_ensemble_coeff" value={params.temporal_ensemble_coeff} {...rangeOf('temporal_ensemble_coeff', { min: 0, max: 1, step: 0.001 })}
                onChange={(v) => handleParamChange('temporal_ensemble_coeff', v)} />
              <p className="text-[10px] text-neutral-500">
                액션 스텝 수는 위 &ldquo;받는 액션 청크 크기&rdquo;로 통합되었습니다.
              </p>
            </CollapsibleCard>
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
          <EvalPanel checkpoint={selectedModel} preset={activePreset} params={params} />
        </div>
      </div>

      {/* 로그 (하단 전체폭) */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
        <h3 className="text-sm font-semibold mb-2">로그</h3>
        <LogViewer logs={logs} onClear={() => setLogs([])} height="h-[32rem]" />
        {lastLogFile && !isRunning && (
          <a href={`/api/logs/download/${lastLogFile}`}
            download className="inline-block mt-2 text-xs text-blue-400 hover:text-blue-300 underline">
            CSV 로그 다운로드: {lastLogFile}
          </a>
        )}
      </div>
    </div>
  )
}
