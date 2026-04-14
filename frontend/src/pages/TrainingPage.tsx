import { useEffect, useState, useCallback, useRef } from 'react'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import LogViewer from '../components/LogViewer'
import TrainingMetrics, { type MetricsData, type HistoryData } from '../components/TrainingMetrics'

type ProcessState = 'idle' | 'starting' | 'running' | 'stopping' | 'error'
type Dataset = { id: string; path: string; total_episodes: number; total_frames: number; fps: number | null; features: Record<string, { shape?: number[]; dtype?: string; names?: string[] }> }
type Model = { id: string; path: string }
type Checkpoint = { name: string; step: number; size_kb: number; path: string }

const POLICY_TYPES = ['act', 'diffusion', 'smolvla', 'pi0', 'pi05', 'vqbet', 'tdmpc', 'sac']
const OPTIMIZER_TYPES = ['adam', 'adamw', 'sgd']

export default function TrainingPage() {
  // 설정 — localStorage에서 복원
  const _saved = (() => { try { return JSON.parse(localStorage.getItem('piper_train_settings') || '{}') } catch { return {} } })()
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [models, setModels] = useState<Model[]>([])
  const [selectedDataset, setSelectedDataset] = useState(_saved.selectedDataset || '')
  const [policyType, setPolicyType] = useState(_saved.policyType || 'act')
  const [pretrainedPath, setPretrainedPath] = useState(_saved.pretrainedPath || '')
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
  const [stateDim] = useState(_saved.stateDim ?? 0)
  const [actionDim] = useState(_saved.actionDim ?? 0)
  const [renameMap, setRenameMap] = useState(_saved.renameMap || '')
  const [cliArgs, setCliArgs] = useState('')
  const [cliEdited, setCliEdited] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)

  // 설정값 변경 시 localStorage에 저장
  useEffect(() => {
    localStorage.setItem('piper_train_settings', JSON.stringify({
      selectedDataset, policyType, pretrainedPath, outputDir,
      batchSize, steps, logFreq, saveFreq, numWorkers, seed, device,
      optimizerType, learningRate, wandbEnable, wandbProject, resume,
      stateDim, actionDim, renameMap,
    }))
  }, [selectedDataset, policyType, pretrainedPath, outputDir, batchSize, steps, logFreq, saveFreq, numWorkers, seed, device, optimizerType, learningRate, wandbEnable, wandbProject, resume, stateDim, actionDim, renameMap])

  // 실행 상태
  const [trainState, setTrainState] = useState<ProcessState>('idle')
  const [metrics, setMetrics] = useState<MetricsData | null>(null)
  const [history, setHistory] = useState<HistoryData>({ steps: [], losses: [], grad_norms: [], lrs: [] })
  const [logs, setLogs] = useState<string[]>([])
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([])
  const [inferenceRunning, setInferenceRunning] = useState(false)
  const MAX_LOGS = 500

  const { connected } = useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
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
      } else if (msg.type === 'state') {
        // 추론 상태 감시 (GPU 경합 표시용)
        setInferenceRunning((msg.data as string) === 'running')
      }
    }, []),
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
      pretrained_path: pretrainedPath, output_dir: outputDir,
      batch_size: batchSize, steps, log_freq: logFreq, save_freq: saveFreq,
      num_workers: numWorkers, seed, device, optimizer_type: optimizerType,
      learning_rate: learningRate, wandb_enable: wandbEnable, wandb_project: wandbProject, resume,
      state_dim: stateDim, action_dim: actionDim, rename_map: renameMap,
    }).then((r) => setCliArgs(r.command)).catch(() => {})
  }, [selectedDataset, policyType, pretrainedPath, outputDir, batchSize, steps, logFreq, saveFreq, numWorkers, seed, device, optimizerType, learningRate, wandbEnable, wandbProject, resume, stateDim, actionDim, cliEdited])

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

  const handleStart = async () => {
    try {
      if (cliEdited) {
        await api.post('/training/start-custom', {
          args: cliArgs.split(/\s+/).filter(Boolean), total_steps: steps, output_dir: outputDir,
        })
      } else {
        await api.post('/training/start', {
          dataset_repo_id: selectedDataset, policy_type: policyType,
          pretrained_path: pretrainedPath, output_dir: outputDir,
          batch_size: batchSize, steps, log_freq: logFreq, save_freq: saveFreq,
          num_workers: numWorkers, seed, device, optimizer_type: optimizerType,
          learning_rate: learningRate, wandb_enable: wandbEnable, wandb_project: wandbProject, resume,
          state_dim: stateDim, action_dim: actionDim,
        })
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : '알 수 없는 오류'
      setLogs((prev) => [...prev, `[ERROR] 학습 시작 실패: ${msg}`])
    }
  }

  const handleStop = async () => { await api.post('/training/stop') }

  const canStart = !!selectedDataset && !isRunning && !inferenceRunning

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">학습</h1>
        <div className="flex items-center gap-3">
          <span className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
          {isRunning && (
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400">{trainState}</span>
          )}
          {inferenceRunning && (
            <span className="px-2 py-0.5 rounded text-xs bg-amber-500/20 text-amber-400">추론 실행 중 (GPU 사용)</span>
          )}
        </div>
      </div>

      <div className={`grid gap-6 ${isRunning ? 'grid-cols-1 lg:grid-cols-[1fr_2fr]' : 'grid-cols-1 lg:grid-cols-[2fr_1fr]'}`}>
        {/* 좌측: 설정 또는 메트릭 */}
        <div className="space-y-4">
          {!isRunning ? (
            <>
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
                    <div className="text-xs pt-1">
                      <label className="text-neutral-400">카메라 이름 매핑 (rename_map)</label>
                      <textarea value={renameMap}
                        onChange={(e) => { setRenameMap(e.target.value); setCliEdited(false) }}
                        rows={3}
                        placeholder='{"observation.images.side": "observation.images.camera1", ...}'
                        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs font-mono text-neutral-100 placeholder:text-neutral-600 resize-y" />
                      <p className="text-neutral-500 text-[10px] mt-0.5">
                        {renameMap && pretrainedPath ? 'pretrained 모델에서 자동 감지됨 (편집 가능)' : '데이터셋 카메라 이름 → 모델 카메라 이름'}
                      </p>
                    </div>
                    {cameras.length > 0 && (
                      <div className="text-xs">
                        <span className="text-neutral-400">카메라 ({cameras.length}대):</span>
                        <span className="ml-1 text-neutral-100">
                          {cameras.map((c) => c.replace('observation.images.', '')).join(', ')}
                        </span>
                      </div>
                    )}
                  </div>
                )
              })()}

              {/* 정책 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <h3 className="text-sm font-semibold">정책</h3>
                <select value={policyType} onChange={(e) => { setPolicyType(e.target.value); setCliEdited(false) }}
                  className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                  {POLICY_TYPES.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
                <div className="space-y-1">
                  <label className="text-xs text-neutral-400">Fine-tune 모델 (선택)</label>
                  <select value={pretrainedPath} onChange={(e) => { setPretrainedPath(e.target.value); setCliEdited(false) }}
                    className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                    <option value="">처음부터 학습</option>
                    {models.map((m) => <option key={m.id} value={m.path}>{m.id}</option>)}
                  </select>
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

                <label className="flex items-center gap-2 text-xs cursor-pointer">
                  <input type="checkbox" checked={resume} onChange={(e) => { setResume(e.target.checked); setCliEdited(false) }}
                    className="accent-blue-500" />
                  <span className="text-neutral-400">체크포인트에서 재개 (Resume)</span>
                </label>
              </div>

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
                <button onClick={handleStart} disabled={!canStart}
                  className="w-full px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium">
                  학습 시작
                </button>
              </div>
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
