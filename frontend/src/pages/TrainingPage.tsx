import { useEffect, useState, useCallback, useRef } from 'react'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import type { ProcessState } from '../types/ws'
import { useActivity, isStateMessage } from '../hooks/useActivity'
import { usePolicies } from '../hooks/usePolicies'
import PresetBar from '../components/PresetBar'
import LogViewer from '../components/LogViewer'
import TrainingMetrics, { type MetricsData, type HistoryData } from '../components/TrainingMetrics'

type Dataset = { id: string; path: string; total_episodes: number; total_frames: number; fps: number | null; features: Record<string, { shape?: number[]; dtype?: string; names?: string[] }> }
type Model = { id: string; path: string; policy_type?: string; config?: Record<string, unknown>; requirements?: { required_cameras: { name: string; model_name?: string }[]; state_dim: number; action_dim: number } }
type Checkpoint = { name: string; step: number; size_kb: number; path: string }

// 정책 목록은 백엔드 core/policies.py 하나에서 온다 (usePolicies)
// 이전에는 여기 `sac` 이 있었는데 추론 시작에서 ValueError 로 죽었다.
const OPTIMIZER_TYPES = ['adam', 'adamw', 'sgd']

// 정책별 학습 옵션 스키마 (단일 소스)
//  - defaults: 정책 선택 시 공통 옵션(batch/steps/optimizer)에 적용할 권장값
//  - fields: 노출할 --policy.<key> config 필드 (LeRobot config 클래스에서 확인한 기본값)
// VLA(SmolVLA/Pi0/Pi05)는 추론 런타임 파라미터가 아닌 학습 config만 노출한다.
type PolicyField = { key: string; label: string; type: 'number' | 'bool'; default: number | boolean; min?: number; max?: number; step?: number }
type PolicySchema = { defaults: { batchSize: number; steps: number; optimizerType: string }; fields: PolicyField[] }

const PI_FIELDS: PolicyField[] = [
  { key: 'chunk_size', label: 'chunk_size', type: 'number', default: 50, min: 1, max: 200, step: 1 },
  { key: 'n_action_steps', label: 'n_action_steps', type: 'number', default: 50, min: 1, max: 200, step: 1 },
  { key: 'freeze_vision_encoder', label: 'freeze_vision_encoder', type: 'bool', default: false },
  { key: 'train_expert_only', label: 'train_expert_only', type: 'bool', default: false },
  { key: 'num_inference_steps', label: 'num_inference_steps', type: 'number', default: 10, min: 1, max: 100, step: 1 },
  { key: 'gradient_checkpointing', label: 'gradient_checkpointing', type: 'bool', default: false },
]

const POLICY_TRAIN_SCHEMAS: Record<string, PolicySchema> = {
  act: {
    defaults: { batchSize: 8, steps: 100000, optimizerType: 'adam' },
    fields: [
      { key: 'chunk_size', label: 'chunk_size', type: 'number', default: 100, min: 1, max: 200, step: 1 },
      { key: 'n_action_steps', label: 'n_action_steps', type: 'number', default: 100, min: 1, max: 200, step: 1 },
      { key: 'n_obs_steps', label: 'n_obs_steps', type: 'number', default: 1, min: 1, max: 10, step: 1 },
      { key: 'dim_model', label: 'dim_model', type: 'number', default: 512, min: 64, max: 2048, step: 64 },
      { key: 'use_vae', label: 'use_vae', type: 'bool', default: true },
    ],
  },
  diffusion: {
    defaults: { batchSize: 64, steps: 200000, optimizerType: 'adam' },
    fields: [
      { key: 'horizon', label: 'horizon', type: 'number', default: 16, min: 1, max: 128, step: 1 },
      { key: 'n_obs_steps', label: 'n_obs_steps', type: 'number', default: 2, min: 1, max: 10, step: 1 },
      { key: 'n_action_steps', label: 'n_action_steps', type: 'number', default: 8, min: 1, max: 128, step: 1 },
      { key: 'num_train_timesteps', label: 'num_train_timesteps', type: 'number', default: 100, min: 1, max: 1000, step: 1 },
    ],
  },
  smolvla: {
    defaults: { batchSize: 64, steps: 200000, optimizerType: 'adamw' },
    fields: [
      { key: 'chunk_size', label: 'chunk_size', type: 'number', default: 50, min: 1, max: 200, step: 1 },
      { key: 'n_action_steps', label: 'n_action_steps', type: 'number', default: 50, min: 1, max: 200, step: 1 },
      { key: 'freeze_vision_encoder', label: 'freeze_vision_encoder', type: 'bool', default: true },
      { key: 'train_expert_only', label: 'train_expert_only', type: 'bool', default: true },
      // LeRobot 기본값은 false — 처음부터 학습할 때 false면 VLM(SigLIP 비전 인코더 포함)이
      // 랜덤 초기화된다. freeze_vision_encoder=true와 겹치면 학습조차 되지 않으므로 기본 true.
      // (pretrained_path로 파인튜닝할 땐 체크포인트에서 가중치가 오므로 이 패널 자체가 숨겨진다)
      { key: 'load_vlm_weights', label: 'load_vlm_weights', type: 'bool', default: true },
    ],
  },
  pi0: { defaults: { batchSize: 32, steps: 30000, optimizerType: 'adamw' }, fields: PI_FIELDS },
  pi05: { defaults: { batchSize: 32, steps: 30000, optimizerType: 'adamw' }, fields: PI_FIELDS },
}

// 정책의 config 필드 기본값 객체 생성
function policyConfigDefaults(policy: string): Record<string, number | boolean> {
  const schema = POLICY_TRAIN_SCHEMAS[policy]
  if (!schema) return {}
  return Object.fromEntries(schema.fields.map((f) => [f.key, f.default]))
}

export default function TrainingPage() {
  // 설정 — localStorage에서 복원
  const _saved = (() => { try { return JSON.parse(localStorage.getItem('piper_train_settings') || '{}') } catch { return {} } })()
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [models, setModels] = useState<Model[]>([])
  const [selectedDataset, setSelectedDataset] = useState(_saved.selectedDataset || '')
  const [policyType, setPolicyType] = useState(_saved.policyType || 'act')
  const [pretrainedPath, setPretrainedPath] = useState(_saved.pretrainedPath || '')
  const [policyRepoId, setPolicyRepoId] = useState(_saved.policyRepoId || '')
  const [outputDir, setOutputDir] = useState(_saved.outputDir || '')
  const [batchSize, setBatchSize] = useState(_saved.batchSize ?? 8)
  const [steps, setSteps] = useState(_saved.steps ?? 100000)
  const [logFreq, setLogFreq] = useState(_saved.logFreq ?? 200)
  const [saveFreq, setSaveFreq] = useState(_saved.saveFreq ?? 20000)
  const [numWorkers, setNumWorkers] = useState(_saved.numWorkers ?? 4)
  const [seed, setSeed] = useState(_saved.seed ?? 1000)
  const [device, setDevice] = useState(_saved.device || 'cuda')
  const [optimizerType, setOptimizerType] = useState(_saved.optimizerType || 'adam')
  const [learningRate, setLearningRate] = useState(_saved.learningRate ?? 0)
  const [wandbEnable, setWandbEnable] = useState(_saved.wandbEnable ?? false)
  const [wandbProject, setWandbProject] = useState(_saved.wandbProject || '')
  const [resume, setResume] = useState(_saved.resume ?? false)
  const [usePolicyPreset, setUsePolicyPreset] = useState(_saved.usePolicyPreset ?? true)
  const [stateDim] = useState(_saved.stateDim ?? 0)
  const [actionDim] = useState(_saved.actionDim ?? 0)
  const [renameMap, setRenameMap] = useState(_saved.renameMap || '')
  // 저장값을 스키마 기본값 위에 덮어쓴다. 스키마에 필드가 추가되면 저장값에 없던 키가
  // 그대로 누락되어(=CLI 인자 미생성) LeRobot 기본값으로 학습되는 것을 막기 위함.
  const [policyParams, setPolicyParams] = useState<Record<string, number | boolean>>(
    { ...policyConfigDefaults(_saved.policyType || 'act'), ...(_saved.policyParams ?? {}) }
  )
  const [amp, setAmp] = useState<string>(_saved.amp ?? 'bf16')  // 혼합정밀도: off | bf16 | fp16
  const [cliArgs, setCliArgs] = useState('')
  const [cliEdited, setCliEdited] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [confirmConfig, setConfirmConfig] = useState<string | null>(null)

  // 설정값 변경 시 localStorage에 저장
  useEffect(() => {
    localStorage.setItem('piper_train_settings', JSON.stringify({
      selectedDataset, policyType, pretrainedPath, policyRepoId, outputDir,
      batchSize, steps, logFreq, saveFreq, numWorkers, seed, device,
      optimizerType, learningRate, wandbEnable, wandbProject, resume, usePolicyPreset,
      stateDim, actionDim, renameMap, policyParams, amp,
    }))
  }, [selectedDataset, policyType, pretrainedPath, policyRepoId, outputDir, batchSize, steps, logFreq, saveFreq, numWorkers, seed, device, optimizerType, learningRate, wandbEnable, wandbProject, resume, usePolicyPreset, stateDim, actionDim, renameMap, policyParams, amp])

  // 실행 상태
  const [trainState, setTrainState] = useState<ProcessState>('idle')
  const [metrics, setMetrics] = useState<MetricsData | null>(null)
  const [history, setHistory] = useState<HistoryData>({ steps: [], losses: [], grad_norms: [], lrs: [] })
  const [logs, setLogs] = useState<string[]>([])
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([])
  const MAX_LOGS = 500

  // 프리셋에 담는 것은 "튜닝" 만 — dataset/output_dir 같은 실행 대상은 제외한다.
  // 키 이름은 백엔드 TrainStartRequest 필드명을 그대로 쓴다.
  const presetValues = () => ({
    policy_type: policyType, batch_size: batchSize, steps, log_freq: logFreq,
    save_freq: saveFreq, num_workers: numWorkers, seed, device,
    optimizer_type: optimizerType, learning_rate: learningRate,
    wandb_enable: wandbEnable, wandb_project: wandbProject,
    use_policy_training_preset: usePolicyPreset, policy_params: policyParams, amp,
  })
  const applyPreset = (v: Record<string, unknown>) => {
    if (v.policy_type !== undefined) setPolicyType(v.policy_type as string)
    if (v.batch_size !== undefined) setBatchSize(v.batch_size as number)
    if (v.steps !== undefined) setSteps(v.steps as number)
    if (v.log_freq !== undefined) setLogFreq(v.log_freq as number)
    if (v.save_freq !== undefined) setSaveFreq(v.save_freq as number)
    if (v.num_workers !== undefined) setNumWorkers(v.num_workers as number)
    if (v.seed !== undefined) setSeed(v.seed as number)
    if (v.device !== undefined) setDevice(v.device as string)
    if (v.optimizer_type !== undefined) setOptimizerType(v.optimizer_type as string)
    if (v.learning_rate !== undefined) setLearningRate(v.learning_rate as number)
    if (v.wandb_enable !== undefined) setWandbEnable(v.wandb_enable as boolean)
    if (v.wandb_project !== undefined) setWandbProject(v.wandb_project as string)
    if (v.use_policy_training_preset !== undefined) setUsePolicyPreset(v.use_policy_training_preset as boolean)
    if (v.policy_params !== undefined) setPolicyParams(v.policy_params as Record<string, number | boolean>)
    if (v.amp !== undefined) setAmp(v.amp as string)
    setCliEdited(false)
  }

  // 배타 규칙은 백엔드 exclusivity.py 한 곳에만 있다
  const { isBlocked, blockedBy, refresh: refreshActivity } = useActivity()
  // 정책 목록도 백엔드 core/policies.py 한 곳에서 온다
  const { trainable } = usePolicies()

  const { connected } = useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (isStateMessage(msg.type)) refreshActivity()
      if (msg.type === 'train_state') {
        setTrainState(msg.data as ProcessState)
      } else if (msg.type === 'train_metrics') {
        const d = msg.data as MetricsData
        setMetrics(d)
      } else if (msg.type === 'train_log') {
        setLogs((prev) => {
          const next = [...prev, msg.data as string]
          return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next
        })
      }
    }, [refreshActivity]),
  })

  // 초기 데이터 로드
  useEffect(() => {
    api.get<Dataset[]>('/datasets').then(setDatasets).catch(() => {})
    api.get<Model[]>('/models').then(setModels).catch(() => {})
    api.get<{ state: string }>('/training/status').then((s) => setTrainState(s.state as ProcessState)).catch(() => {})
  }, [])

  // pretrained 모델 변경 시 rename_map 자동 로드
  useEffect(() => {
    if (!pretrainedPath) { setRenameMap(''); return }
    api.post<{ rename_map: Record<string, string> }>('/training/rename-map', { pretrained_path: pretrainedPath })
      .then((r) => {
        if (r.rename_map && Object.keys(r.rename_map).length > 0) {
          setRenameMap(JSON.stringify(r.rename_map, null, 2))
        }
      }).catch(() => {})
  }, [pretrainedPath])

  // CLI 미리보기
  useEffect(() => {
    if (cliEdited || !selectedDataset) return
    api.post<{ command: string }>('/training/preview', {
      dataset_repo_id: selectedDataset, policy_type: policyType,
      pretrained_path: pretrainedPath, policy_repo_id: policyRepoId, output_dir: outputDir,
      batch_size: batchSize, steps, log_freq: logFreq, save_freq: saveFreq,
      num_workers: numWorkers, seed, device, optimizer_type: optimizerType,
      learning_rate: learningRate, wandb_enable: wandbEnable, wandb_project: wandbProject, resume,
      use_policy_training_preset: usePolicyPreset, state_dim: stateDim, action_dim: actionDim, rename_map: renameMap,
      policy_params: policyParams, amp,
    }).then((r) => setCliArgs(r.command)).catch(() => {})
  }, [selectedDataset, policyType, pretrainedPath, policyRepoId, outputDir, batchSize, steps, logFreq, saveFreq, numWorkers, seed, device, optimizerType, learningRate, wandbEnable, wandbProject, resume, usePolicyPreset, stateDim, actionDim, renameMap, policyParams, amp, cliEdited])

  // 체크포인트 폴링 (학습 중)
  const ckptPollRef = useRef<ReturnType<typeof setInterval>>(undefined)
  useEffect(() => {
    if (trainState === 'running') {
      const poll = () => api.get<Checkpoint[]>('/training/checkpoints').then(setCheckpoints).catch(() => {})
      poll()
      ckptPollRef.current = setInterval(poll, 10000)
    } else {
      clearInterval(ckptPollRef.current)
    }
    return () => clearInterval(ckptPollRef.current)
  }, [trainState])

  // 히스토리 폴링 (학습 중)
  const histPollRef = useRef<ReturnType<typeof setInterval>>(undefined)
  useEffect(() => {
    if (trainState === 'running') {
      const poll = () => api.get<HistoryData>('/training/metrics').then(setHistory).catch(() => {})
      poll()
      histPollRef.current = setInterval(poll, 5000)
    } else {
      clearInterval(histPollRef.current)
    }
    return () => clearInterval(histPollRef.current)
  }, [trainState])

  const isRunning = trainState === 'running' || trainState === 'starting' || trainState === 'stopping'

  // 정책 변경 시 권장 기본값 + config 필드 리셋
  const handlePolicyChange = (next: string) => {
    setPolicyType(next)
    const schema = POLICY_TRAIN_SCHEMAS[next]
    if (schema) {
      setBatchSize(schema.defaults.batchSize)
      setSteps(schema.defaults.steps)
      setOptimizerType(schema.defaults.optimizerType)
      setPolicyParams(policyConfigDefaults(next))
    }
    setCliEdited(false)
  }

  const setPolicyParam = (key: string, value: number | boolean) => {
    setPolicyParams((p) => ({ ...p, [key]: value }))
    setCliEdited(false)
  }

  const policySchema = POLICY_TRAIN_SCHEMAS[policyType]

  const trainParams = () => ({
    dataset_repo_id: selectedDataset, policy_type: policyType,
    pretrained_path: pretrainedPath, policy_repo_id: policyRepoId, output_dir: outputDir,
    batch_size: batchSize, steps, log_freq: logFreq, save_freq: saveFreq,
    num_workers: numWorkers, seed, device, optimizer_type: optimizerType,
    learning_rate: learningRate, wandb_enable: wandbEnable, wandb_project: wandbProject, resume,
    use_policy_training_preset: usePolicyPreset, state_dim: stateDim, action_dim: actionDim, rename_map: renameMap,
    policy_params: policyParams, amp,
  })

  const handlePreConfirm = async () => {
    // CLI 미리보기를 config 확인용으로 표시
    try {
      const r = await api.post<{ command: string; args: string[] }>('/training/preview', trainParams())
      const configLines = [
        '=== 학습 설정 확인 ===',
        '',
        `데이터셋: ${selectedDataset}`,
        `정책: ${policyType}`,
        `Fine-tune: ${pretrainedPath || '(처음부터)'}`,
        `Repo ID: ${policyRepoId || '(없음)'}`,
        `Output: ${outputDir || '(자동)'}`,
        '',
        `Batch: ${batchSize}  Steps: ${steps}  Save: ${saveFreq}`,
        `Device: ${device}  AMP: ${amp}`,
        `Workers: ${numWorkers}  Seed: ${seed}`,
        `Resume: ${resume}  Preset: ${usePolicyPreset}  State: ${stateDim || 'auto'}  Action: ${actionDim || 'auto'}`,
        '',
        `Rename map: ${renameMap || '(없음)'}`,
        '',
        '=== CLI 명령어 ===',
        r.command,
      ].join('\n')
      setConfirmConfig(configLines)
    } catch (e) {
      const msg = e instanceof Error ? e.message : '미리보기 실패'
      setLogs((prev) => [...prev, `[ERROR] ${msg}`])
    }
  }

  const handleStart = async () => {
    setConfirmConfig(null)
    try {
      if (cliEdited) {
        await api.post('/training/start-custom', {
          args: cliArgs.split(/\s+/).filter(Boolean), total_steps: steps, output_dir: outputDir,
        })
      } else {
        await api.post('/training/start', trainParams())
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : '알 수 없는 오류'
      setLogs((prev) => [...prev, `[ERROR] 학습 시작 실패: ${msg}`])
    }
  }

  const handleStop = async () => { await api.post('/training/stop') }

  const trainBlockedBy = blockedBy('training')
  const canStart = !!selectedDataset && !isRunning && !isBlocked('training')

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">학습</h1>
        <div className="flex items-center gap-3">
          <span className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
          {isRunning && (
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400">{trainState}</span>
          )}
          {trainBlockedBy && (
            <span className="px-2 py-0.5 rounded text-xs bg-amber-500/20 text-amber-400">{trainBlockedBy} 실행 중 — 학습을 시작할 수 없습니다</span>
          )}
        </div>
      </div>

      <div className={`grid gap-6 ${isRunning ? 'grid-cols-1 lg:grid-cols-[1fr_2fr]' : 'grid-cols-1 lg:grid-cols-[2fr_1fr]'}`}>
        {/* 좌측: 설정 또는 메트릭 */}
        <div className="space-y-4">
          {!isRunning ? (
            <>
              {/* 프리셋 — 학습 파라미터는 기기와 무관하므로 shared */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
                <PresetBar domain="training" scope="shared" policyType={policyType}
                  values={presetValues} onApply={applyPreset} disabled={isRunning} />
              </div>
              {/* 데이터셋 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <h3 className="text-sm font-semibold">데이터셋</h3>
                <select value={selectedDataset} onChange={(e) => { setSelectedDataset(e.target.value); setCliEdited(false) }}
                  className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                  <option value="">선택...</option>
                  {datasets.map((d) => (
                    <option key={d.id} value={d.id}>{d.id} ({d.total_episodes} ep, {d.total_frames} frames)</option>
                  ))}
                </select>
              </div>

              {/* 데이터셋 정보 */}
              {selectedDataset && (() => {
                const ds = datasets.find((d) => d.id === selectedDataset)
                if (!ds?.features) return null
                const stateShape = ds.features['observation.state']?.shape
                const actionShape = ds.features['action']?.shape
                const stateNames = ds.features['observation.state']?.names
                const cameras = Object.keys(ds.features).filter((k) => k.startsWith('observation.images.'))
                return (
                  <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                    <h3 className="text-sm font-semibold">데이터셋 정보</h3>
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      <div>
                        <span className="text-neutral-400">관절 (state):</span>
                        <span className="ml-1 text-neutral-100">{stateShape?.[0] ?? '?'}개</span>
                        {stateNames && <span className="text-neutral-500 ml-1">({stateNames.join(', ')})</span>}
                      </div>
                      <div>
                        <span className="text-neutral-400">액션:</span>
                        <span className="ml-1 text-neutral-100">{actionShape?.[0] ?? '?'}차원</span>
                      </div>
                    </div>
                    {stateShape && actionShape && stateShape[0] !== actionShape[0] && (
                      <p className="text-xs text-amber-400">state({stateShape[0]})와 action({actionShape[0]}) 차원이 다릅니다. 베이스 모델의 기본 state_dim이 다를 수 있으니 CLI에서 확인하세요.</p>
                    )}
                    {cameras.length > 0 && (
                      <div className="text-xs">
                        <span className="text-neutral-400">카메라 ({cameras.length}대):</span>
                        <span className="ml-1 text-neutral-100">
                          {cameras.map((c) => c.replace('observation.images.', '')).join(', ')}
                        </span>
                      </div>
                    )}
                    <div className="text-xs pt-2 space-y-2">
                      <div className="flex items-center justify-between">
                        <label className="text-neutral-400 font-medium">카메라 이름 매핑</label>
                        {renameMap && pretrainedPath && <span className="text-[10px] text-green-400">자동 감지됨</span>}
                      </div>
                      {(() => {
                        let entries: [string, string][] = []
                        try { entries = Object.entries(JSON.parse(renameMap || '{}')) } catch {}
                        const updateEntries = (newEntries: [string, string][]) => {
                          const obj: Record<string, string> = {}
                          newEntries.forEach(([k, v]) => { if (k) obj[k] = v })
                          setRenameMap(Object.keys(obj).length > 0 ? JSON.stringify(obj, null, 2) : '')
                          setCliEdited(false)
                        }
                        return (
                          <>
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="text-neutral-500">
                                  <th className="text-left py-1 pr-2">데이터셋 카메라</th>
                                  <th className="text-center py-1 px-1">→</th>
                                  <th className="text-left py-1 pl-2">모델 카메라</th>
                                  <th className="w-8"></th>
                                </tr>
                              </thead>
                              <tbody>
                                {entries.map(([src, dst], idx) => (
                                  <tr key={idx}>
                                    <td className="pr-1 py-0.5">
                                      <input type="text" value={src.replace('observation.images.', '')}
                                        onChange={(e) => {
                                          const newEntries = [...entries]
                                          newEntries[idx] = [`observation.images.${e.target.value}`, dst]
                                          updateEntries(newEntries)
                                        }}
                                        className="w-full px-1.5 py-0.5 rounded bg-neutral-800 border border-neutral-700 text-neutral-100" />
                                    </td>
                                    <td className="text-center text-neutral-600">→</td>
                                    <td className="pl-1 py-0.5">
                                      <input type="text" value={dst.replace('observation.images.', '')}
                                        onChange={(e) => {
                                          const newEntries = [...entries]
                                          newEntries[idx] = [src, `observation.images.${e.target.value}`]
                                          updateEntries(newEntries)
                                        }}
                                        className="w-full px-1.5 py-0.5 rounded bg-neutral-800 border border-neutral-700 text-neutral-100" />
                                    </td>
                                    <td>
                                      <button onClick={() => updateEntries(entries.filter((_, i) => i !== idx))}
                                        className="text-neutral-600 hover:text-red-400 px-1">x</button>
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                            <button onClick={() => updateEntries([...entries, ['observation.images.', 'observation.images.camera' + (entries.length + 1)]])}
                              className="text-[10px] text-blue-400 hover:text-blue-300">+ 매핑 추가</button>
                          </>
                        )
                      })()}
                      <textarea value={renameMap} readOnly rows={2}
                        className="w-full px-2 py-1 rounded bg-neutral-950 border border-neutral-800 text-[10px] font-mono text-neutral-500 resize-none" />
                    </div>
                  </div>
                )
              })()}

              {/* 정책 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <h3 className="text-sm font-semibold">정책</h3>
                <select value={policyType} onChange={(e) => handlePolicyChange(e.target.value)}
                  className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                  {trainable.map((p) => <option key={p.type} value={p.type}>{p.label}</option>)}
                </select>
                <div className="space-y-1">
                  <label className="text-xs text-neutral-400">Fine-tune 모델 (선택)</label>
                  <select value={pretrainedPath} onChange={(e) => { setPretrainedPath(e.target.value); setCliEdited(false) }}
                    className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                    <option value="">처음부터 학습</option>
                    {models.map((m) => <option key={m.id} value={m.path}>{m.id}</option>)}
                  </select>
                </div>
                {pretrainedPath && (() => {
                  const m = models.find((mm) => mm.path === pretrainedPath)
                  if (!m?.requirements) return null
                  const r = m.requirements
                  return (
                    <div className="rounded border border-neutral-700 bg-neutral-900 p-2 space-y-1 text-xs">
                      <div className="flex items-center gap-2 text-neutral-300">
                        <span className="font-medium">{m.policy_type ?? '?'}</span>
                        <span className="text-neutral-500">|</span>
                        <span>state: {r.state_dim}</span>
                        <span className="text-neutral-500">|</span>
                        <span>action: {r.action_dim}</span>
                      </div>
                      {r.required_cameras.length > 0 && (
                        <div className="text-neutral-400">
                          카메라: {r.required_cameras.map((c) => (
                            <span key={c.name} className="inline-block mr-2">
                              <span className="text-neutral-200">{c.name}</span>
                              {c.model_name && c.model_name !== c.name && <span className="text-neutral-600 text-[10px]">({c.model_name})</span>}
                            </span>
                          ))}
                        </div>
                      )}
                      <div className="text-[10px] text-neutral-600 truncate">{m.path}</div>
                    </div>
                  )
                })()}
                <div className="space-y-1">
                  <label className="text-xs text-neutral-400">Policy Repo ID (Hub 업로드용)</label>
                  <input type="text" value={policyRepoId}
                    onChange={(e) => { setPolicyRepoId(e.target.value); setCliEdited(false) }}
                    placeholder="예: wego-hansu/piper_tws_v2"
                    className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 placeholder:text-neutral-600" />
                  {policyRepoId && (() => {
                    const existing = models.find((m) => m.id.includes(policyRepoId.split('/').pop() || ''))
                    if (!existing) return null
                    return (
                      <p className="text-xs text-amber-400 mt-1">
                        이미 로컬에 존재합니다: {existing.id}
                        <button onClick={async () => {
                          if (!confirm(`"${existing.id}" 모델을 삭제하시겠습니까?\n경로: ${existing.path}`)) return
                          await api.delete(`/models/${existing.id}`)
                          api.get<Model[]>('/models').then(setModels).catch(() => {})
                        }} className="ml-2 text-red-400 hover:text-red-300 underline">삭제</button>
                      </p>
                    )
                  })()}
                </div>
              </div>

              {/* 기본 파라미터 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <h3 className="text-sm font-semibold">파라미터</h3>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    { label: 'Batch Size', value: batchSize, set: setBatchSize, min: 1, max: 256 },
                    { label: 'Steps', value: steps, set: setSteps, min: 1000, max: 10000000, step: 10000 },
                    { label: 'Log Freq', value: logFreq, set: setLogFreq, min: 10, max: 10000 },
                    { label: 'Save Freq', value: saveFreq, set: setSaveFreq, min: 1000, max: 100000 },
                  ].map(({ label, value, set, min, max, step }) => (
                    <div key={label}>
                      <label className="text-xs text-neutral-400">{label}</label>
                      <input type="number" value={value} min={min} max={max} step={step || 1}
                        onChange={(e) => { set(Number(e.target.value)); setCliEdited(false) }}
                        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                    </div>
                  ))}
                </div>

                {/* 혼합정밀도(AMP): ACCELERATE_MIXED_PRECISION env로 적용 (--policy.use_amp는 학습 미사용). 속도 최대 레버 */}
                <div>
                  <label className="text-xs text-neutral-400">혼합정밀도 (AMP)</label>
                  <select value={amp} onChange={(e) => { setAmp(e.target.value); setCliEdited(false) }}
                    className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                    <option value="bf16">bf16 (권장 · RTX 5090)</option>
                    <option value="fp16">fp16</option>
                    <option value="off">off (fp32)</option>
                  </select>
                  <p className="text-[10px] text-neutral-500 mt-0.5">
                    bf16: 속도↑·VRAM↓ (5090 권장). VRAM 여유 시 Batch를 32~48로 키우세요.
                  </p>
                </div>

                <label className="flex items-center gap-2 text-xs cursor-pointer">
                  <input type="checkbox" checked={resume} onChange={(e) => { setResume(e.target.checked); setCliEdited(false) }}
                    className="accent-blue-500" />
                  <span className="text-neutral-400">체크포인트에서 재개 (Resume)</span>
                </label>
                <label className="flex items-center gap-2 text-xs cursor-pointer">
                  <input type="checkbox" checked={usePolicyPreset} onChange={(e) => { setUsePolicyPreset(e.target.checked); setCliEdited(false) }}
                    className="accent-blue-500" />
                  <span className="text-neutral-400">정책 학습 프리셋 사용 (use_policy_training_preset)</span>
                  {!usePolicyPreset && <span className="text-yellow-500 text-[10px]">OFF — config.json 값 그대로 사용</span>}
                </label>
              </div>

              {/* 정책별 config 파라미터 (from-scratch 학습 시에만; pretrained면 아키텍처 고정) */}
              {policySchema && policySchema.fields.length > 0 && !pretrainedPath && (
                <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                  <h3 className="text-sm font-semibold">{policyType.toUpperCase()} 파라미터</h3>
                  <div className="grid grid-cols-2 gap-2">
                    {policySchema.fields.map((f) => (
                      <div key={f.key} className={f.type === 'bool' ? 'col-span-2' : ''}>
                        {f.type === 'bool' ? (
                          <label className="flex items-center gap-2 text-xs cursor-pointer">
                            <input type="checkbox" checked={Boolean(policyParams[f.key] ?? f.default)}
                              onChange={(e) => setPolicyParam(f.key, e.target.checked)}
                              className="accent-blue-500" />
                            <span className="text-neutral-400">{f.label}</span>
                          </label>
                        ) : (
                          <>
                            <label className="text-xs text-neutral-400">{f.label}</label>
                            <input type="number" value={Number(policyParams[f.key] ?? f.default)}
                              min={f.min} max={f.max} step={f.step || 1}
                              onChange={(e) => setPolicyParam(f.key, Number(e.target.value))}
                              className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                          </>
                        )}
                      </div>
                    ))}
                  </div>
                  {typeof policyParams.n_action_steps === 'number' && typeof policyParams.chunk_size === 'number'
                    && policyParams.n_action_steps > policyParams.chunk_size && (
                    <span className="text-yellow-500 text-[10px]">⚠ n_action_steps는 chunk_size 이하여야 합니다</span>
                  )}
                  {policyType === 'smolvla' && !policyParams.load_vlm_weights && (
                    <span className="block text-red-400 text-[10px]">
                      ⚠ load_vlm_weights=false — VLM이 랜덤 초기화됩니다
                      {policyParams.freeze_vision_encoder
                        ? ' (freeze_vision_encoder=true와 겹쳐 비전 인코더가 학습조차 되지 않습니다)'
                        : ''}
                    </span>
                  )}
                </div>
              )}

              {/* 고급 설정 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <button onClick={() => setShowAdvanced(!showAdvanced)}
                  className="text-sm font-semibold text-neutral-300 hover:text-white">
                  {showAdvanced ? '▾' : '▸'} 고급 설정
                </button>
                {showAdvanced && (
                  <div className="grid grid-cols-2 gap-2 pt-2">
                    <div>
                      <label className="text-xs text-neutral-400">Optimizer</label>
                      <select value={optimizerType} onChange={(e) => { setOptimizerType(e.target.value); setCliEdited(false) }}
                        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                        {OPTIMIZER_TYPES.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className="text-xs text-neutral-400">Learning Rate (0=기본)</label>
                      <input type="number" value={learningRate} step={0.0001} min={0}
                        onChange={(e) => { setLearningRate(Number(e.target.value)); setCliEdited(false) }}
                        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                    </div>
                    <div>
                      <label className="text-xs text-neutral-400">Workers</label>
                      <input type="number" value={numWorkers} min={0} max={32}
                        onChange={(e) => { setNumWorkers(Number(e.target.value)); setCliEdited(false) }}
                        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                      <p className="text-[10px] text-neutral-500 mt-0.5">data_s &gt; updt_s면 ↑ (데이터 로딩 병목)</p>
                    </div>
                    <div>
                      <label className="text-xs text-neutral-400">Seed</label>
                      <input type="number" value={seed}
                        onChange={(e) => { setSeed(Number(e.target.value)); setCliEdited(false) }}
                        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                    </div>
                    <div>
                      <label className="text-xs text-neutral-400">Device</label>
                      <select value={device} onChange={(e) => { setDevice(e.target.value); setCliEdited(false) }}
                        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                        <option value="cuda">CUDA (GPU)</option>
                        <option value="cpu">CPU</option>
                      </select>
                    </div>
                    <div>
                      <label className="text-xs text-neutral-400">Output Dir (비우면 자동)</label>
                      <input type="text" value={outputDir}
                        onChange={(e) => { setOutputDir(e.target.value); setCliEdited(false) }}
                        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                    </div>
                    <div className="col-span-2">
                      <label className="flex items-center gap-2 text-xs cursor-pointer">
                        <input type="checkbox" checked={wandbEnable} onChange={(e) => { setWandbEnable(e.target.checked); setCliEdited(false) }}
                          className="accent-blue-500" />
                        <span className="text-neutral-400">WandB 로깅</span>
                      </label>
                      {wandbEnable && (
                        <input type="text" value={wandbProject} placeholder="WandB project name"
                          onChange={(e) => { setWandbProject(e.target.value); setCliEdited(false) }}
                          className="mt-1 w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                      )}
                    </div>
                  </div>
                )}
              </div>

              {/* CLI 미리보기 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold">CLI 명령어</h3>
                  {cliEdited && (
                    <button onClick={() => setCliEdited(false)} className="text-xs text-blue-400 hover:underline">초기화</button>
                  )}
                </div>
                <textarea value={cliArgs}
                  onChange={(e) => { setCliArgs(e.target.value); setCliEdited(true) }}
                  rows={4}
                  className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-xs font-mono text-neutral-100 focus:outline-none focus:border-blue-500 resize-y" />
                {amp !== 'off' && (
                  <p className="text-[10px] font-mono text-neutral-500">
                    env: ACCELERATE_MIXED_PRECISION={amp}
                    <span className="ml-1 not-italic text-neutral-600">(CLI 직접 편집 시 미적용)</span>
                  </p>
                )}
                <button onClick={cliEdited ? handleStart : handlePreConfirm} disabled={!canStart}
                  className="w-full px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium">
                  {cliEdited ? '학습 시작 (CLI 직접)' : '설정 확인 후 시작'}
                </button>
              </div>

              {/* 설정 확인 모달 */}
              {confirmConfig && (
                <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setConfirmConfig(null)}>
                  <div className="bg-neutral-800 border border-neutral-600 rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
                    <h3 className="text-lg font-bold mb-3">학습 설정 확인</h3>
                    <pre className="flex-1 overflow-auto text-xs font-mono bg-neutral-900 p-4 rounded border border-neutral-700 text-neutral-200 whitespace-pre-wrap mb-4">
                      {confirmConfig}
                    </pre>
                    <div className="flex gap-3 justify-end">
                      <button onClick={() => setConfirmConfig(null)}
                        className="px-4 py-2 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 text-sm">취소</button>
                      <button onClick={handleStart}
                        className="px-6 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium">학습 시작</button>
                    </div>
                  </div>
                </div>
              )}
            </>
          ) : (
            <>
              {/* 실행 중: 메트릭 + 체크포인트 */}
              <TrainingMetrics metrics={metrics} history={history} />

              {/* 체크포인트 */}
              {checkpoints.length > 0 && (
                <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                  <h3 className="text-sm font-semibold">체크포인트</h3>
                  <div className="space-y-1 max-h-40 overflow-y-auto">
                    {checkpoints.map((ck) => (
                      <div key={ck.name} className="flex items-center justify-between text-xs px-2 py-1 rounded bg-neutral-900">
                        <span className="font-mono">step {ck.step.toLocaleString()}</span>
                        <span className="text-neutral-400">{ck.size_kb.toFixed(0)} KB</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <button onClick={handleStop}
                className="w-full px-4 py-2 rounded bg-red-600 hover:bg-red-500 text-white text-sm font-medium">
                학습 정지
              </button>
            </>
          )}
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
