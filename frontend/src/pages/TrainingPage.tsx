import { useEffect, useState, useCallback, useRef } from 'react'
import RepoIdInput from '../components/RepoIdInput'
import SpecFields from '../components/SpecFields'
import { usePolicyUi, specDefaults, activeWarnings } from '../hooks/usePolicyUi'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'
import { dlActive } from '../hooks/useHubDownload'
import TrainWhereForm, { type RentPick } from '../components/TrainWhereForm'
import CloudRentProgress from '../components/CloudRentProgress'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import { LOCAL_JOB_ID, type JobRecord, type ProcessState } from '../types/ws'
import { useActivity, isStateMessage } from '../hooks/useActivity'
import { usePolicies } from '../hooks/usePolicies'
import LayoutToggle, { useLayout, twoColumns } from '../components/LayoutToggle'
import PresetBar from '../components/PresetBar'
import LogViewer from '../components/LogViewer'
import TrainingMetrics, { type MetricsData, type HistoryData } from '../components/TrainingMetrics'
import WeightTableModal from '../components/WeightTableModal'
import CheckpointLabel from '../components/CheckpointLabel'
import PickerField from '../components/PickerField'
import PolicyPickerModal from '../components/PolicyPickerModal'
import type { Dataset as DatasetInfo, Model } from '../types/models'
import DatasetPickerModal from '../components/DatasetPickerModal'
import { useT, fmt } from '../i18n'

// `/datasets` 응답 그대로 — feature 안쪽 모양만 이 화면이 읽는 만큼 좁힌다
type Dataset = DatasetInfo & { features: Record<string, { shape?: number[]; dtype?: string; names?: string[] }> }
type Checkpoint = { name: string; step: number; size_kb: number; path: string }

// 정책 목록은 백엔드 core/policies.py 하나에서 온다 (usePolicies)
// 이전에는 여기 `sac` 이 있었는데 추론 시작에서 ValueError 로 죽었다.
const OPTIMIZER_TYPES = ['adam', 'adamw', 'sgd']

// 정책별 **권장 시작점** — Hub 에 올라온 사전학습 정책 체크포인트.
//
// 이게 없으면 "처음부터 학습"밖에 못 고르는데, VLA 계열은 그러면 액션 전문가를
// 맨바닥에서 올리는 셈이라 사실상 못 쓴다. 목록이 비었을 때 무엇을 받아야 하는지
// 알려주고 바로 받을 수 있게 한다.
// 처음부터 학습할 때 **가중치가 어디서 오는지** — 정책마다 다르다.
// ACT 는 자체 기본값이 ImageNet 이라 받을 게 없고, VLA 는 베이스 체크포인트가 필요하다.

// 정책별 학습 옵션 스키마 (단일 소스)
//  - defaults: 정책 선택 시 공통 옵션(batch/steps/optimizer)에 적용할 권장값
//  - fields: 노출할 --policy.<key> config 필드 (LeRobot config 클래스에서 확인한 기본값)
// VLA(SmolVLA/Pi0/Pi05)는 추론 런타임 파라미터가 아닌 학습 config만 노출한다.
// `arch: true` = 모델 구조를 정하는 값. 체크포인트에서 이어 학습(pretrained)하면
// 구조가 이미 고정이라 바꿀 수 없다. 나머지는 **학습 방식** 이라 파인튜닝에도 유효하다
export default function TrainingPage() {
  const t = useT()
  const { confirm: askConfirm, notify } = useSystemMessage()
  // 설정 — localStorage에서 복원
  const _saved = (() => { try { return JSON.parse(localStorage.getItem('piper_train_settings') || '{}') } catch { return {} } })()
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [models, setModels] = useState<Model[]>([])
  const [selectedDataset, setSelectedDataset] = useState(_saved.selectedDataset || '')
  const [policyType, setPolicyType] = useState(_saved.policyType || 'act')
  const [pretrainedPath, setPretrainedPath] = useState(_saved.pretrainedPath || '')
  const [policyRepoId, setPolicyRepoId] = useState(_saved.policyRepoId || '')
  const [outputDir, setOutputDir] = useState(_saved.outputDir || '')
  // 가중치 제목·설명 — 학습 폴더(run 루트)의 piper_notes.json 사이드카(제목 = name).
  // LeRobot 이 폴더를 정한 뒤 백엔드가 쓴다 — Output Dir 을 비워도(자동) 남는다.
  const [title, setTitle] = useState(_saved.title || '')
  const [description, setDescription] = useState(_saved.description || '')
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
  // UI 에 입력란이 없으니 항상 0(=체크포인트/데이터셋 차원 그대로). 예전엔 localStorage 에
  // 남은 옛 값(단일 팔 7)을 되살려 백엔드가 두 팔 체크포인트 config.json 을 7로 덮어썼다.
  const stateDim = 0
  const actionDim = 0
  const [renameMap, setRenameMap] = useState(_saved.renameMap || '')
  // 저장값을 스키마 기본값 위에 덮어쓴다. 스키마에 필드가 추가되면 저장값에 없던 키가
  // 그대로 누락되어(=CLI 인자 미생성) LeRobot 기본값으로 학습되는 것을 막기 위함.
  // 스펙이 오기 전에는 저장된 값만 갖고 시작한다 — 기본값은 아래 effect 가
  // 스펙에서 채운다. 예전엔 여기서 하드코딩 테이블을 읽었다.
  const [policyParams, setPolicyParams] = useState<Record<string, number | boolean>>(
    { ...(_saved.policyParams ?? {}) }
  )

  // 정책 화면 스펙 — `policies/<type>.yaml` 이 정본이다.
  const policyUi = usePolicyUi(policyType)

  // ⚠ 스펙이 도착하면 **저장된 값이 없는 키만** 채운다. 통째로 덮으면 사용자가
  // 방금 만진 값이 되돌아가고, 아무것도 안 채우면 새 정책의 필드가 빈 칸이 된다.
  const appliedSpecFor = useRef('')
  //   첫 스펙: 저장된 값을 기본값 위에 얹는다 (새로고침해도 만지던 값이 남는다)
  //   정책 변경: **통째로 갈아끼운다** — 남겨두면 ACT 의 `use_vae` 가 SmolVLA
  //   실행 인자에 실려 "왜 이런 설정이 들어갔지"가 된다
  const restored = useRef<Record<string, number | boolean>>(_saved.policyParams ?? {})
  useEffect(() => {
    if (!policyUi.type || appliedSpecFor.current === policyUi.type) return
    const first = appliedSpecFor.current === ''
    appliedSpecFor.current = policyUi.type
    const d = policyUi.train.defaults as { batch_size?: number; steps?: number; optimizer_type?: string }
    if (!first) {
      if (d.batch_size) setBatchSize(d.batch_size)
      if (d.steps) setSteps(d.steps)
      if (d.optimizer_type) setOptimizerType(d.optimizer_type)
    }
    const defaults = specDefaults(policyUi.train.fields)
    setPolicyParams(first ? { ...defaults, ...restored.current } : defaults)
  }, [policyUi])

  const [amp, setAmp] = useState<string>(_saved.amp ?? 'bf16')  // 혼합정밀도: off | bf16 | fp16
  const [cliArgs, setCliArgs] = useState('')
  const [cliEdited, setCliEdited] = useState(false)
  const [weightTable, setWeightTable] = useState(false)
  const [datasetPicker, setDatasetPicker] = useState(false)
  const [policyPicker, setPolicyPicker] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [confirmConfig, setConfirmConfig] = useState<string | null>(null)

  // 설정값 변경 시 localStorage에 저장
  useEffect(() => {
    localStorage.setItem('piper_train_settings', JSON.stringify({
      selectedDataset, policyType, pretrainedPath, policyRepoId, outputDir, title, description,
      batchSize, steps, logFreq, saveFreq, numWorkers, seed, device,
      optimizerType, learningRate, wandbEnable, wandbProject, resume, usePolicyPreset,
      stateDim, actionDim, renameMap, policyParams, amp,
    }))
  }, [selectedDataset, policyType, pretrainedPath, policyRepoId, outputDir, title, description, batchSize, steps, logFreq, saveFreq, numWorkers, seed, device, optimizerType, learningRate, wandbEnable, wandbProject, resume, usePolicyPreset, stateDim, actionDim, renameMap, policyParams, amp])

  // 실행 상태
  const [trainState, setTrainState] = useState<ProcessState>('idle')
  // 학습 job 목록 — 로컬도 job 이다(`local`). 원격이 붙으면 여기 함께 뜬다.
  const [jobs, setJobs] = useState<JobRecord[]>([])
  /** 이번 학습이 **다른 기계**에서 도나. 서버가 러너를 실시간으로 답한다. */
  const [remoteRun, setRemoteRun] = useState(false)
  // ⚠ **어디서 돌릴지.** 학습 설정은 아래 폼 그대로 쓰고, 여기서는 기계만 정한다 —
  //   폼을 두 벌 만들면 반드시 어긋난다(RENT 탭이 그랬다, §12-17).
  const [where, setWhere] = useState<'here' | 'rent'>('here')
  const [rentPick, setRentPick] = useState<RentPick | null>(null)
  const [runner, setRunner] = useState('')
  // 지금 화면이 보고 있는 job. WS 메시지를 이걸로 걸러야 job 끼리 안 섞인다.
  const [viewJobId, setViewJobId] = useState<string>(LOCAL_JOB_ID)
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
  const { trainable, policyBase } = usePolicies()

  const { connected } = useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (isStateMessage(msg.type)) refreshActivity()
      if (msg.type === 'job_list') {
        setJobs(msg.data as JobRecord[])
        return
      }
      // 학습 메시지는 **보고 있는 job 것만** 반영한다. 이게 없으면 원격 job 이
      // 붙는 순간 두 job 이 서로의 상태·로그를 덮어쓴다 (cloud-training.md §1-(2)).
      if (msg.type === 'train_state' || msg.type === 'train_metrics' || msg.type === 'train_log') {
        if ((msg as { job_id?: string }).job_id !== viewJobId) return
      }
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
    }, [refreshActivity, viewJobId]),
  })

  // 초기 데이터 로드
  useEffect(() => {
    api.get<Dataset[]>('/datasets').then(setDatasets).catch(() => {})
    api.get<Model[]>('/models').then(setModels).catch(() => {})
    // ⚠ `remote` 도 같이 받는다 — 이 학습이 **어느 기계에서 도는지**는 화면 어디에도
    //   안 나와 있었다. 원격이냐에 인터프리터·HF 토큰·회수 가드가 전부 달려 있는데,
    //   러너가 조용히 로컬로 떨어지면(설정한 원격이 잠깐 안 닿으면 그렇게 된다) 화면은
    //   똑같아 보이면서 이 기계의 GPU 를 먹는다. 그게 가장 비싼 실패다.
    api.get<{ state: string; remote?: boolean; runner?: string }>('/training/status')
      .then((s) => {
        setTrainState(s.state as ProcessState); setRemoteRun(!!s.remote)
        // ⚠ 라벨을 화면이 지어내지 않는다 — "로컬" 이라 적어 두고 사내 서버에서
        //   도는 것이 제일 나쁘다. 러너 이름은 서버가 안다.
        setRunner(s.runner ?? '')
      })
      .catch(() => {})
    api.get<{ jobs: JobRecord[] }>('/training/jobs').then((r) => setJobs(r.jobs)).catch(() => {})
  }, [])

  // 보고 있는 job 이 바뀌면 그 job 의 과거 로그를 채운다.
  // WS 는 신규분만 보내므로(6시간 학습은 수만 줄이라 전부 밀면 브라우저가 죽는다),
  // 전환 직후 화면이 비지 않게 링버퍼에서 읽어온다.
  useEffect(() => {
    let alive = true
    api.get<{ lines: string[]; dropped: number }>(
      `/training/jobs/${encodeURIComponent(viewJobId)}/logs?limit=${MAX_LOGS}`
    ).then((r) => {
      if (!alive) return
      const head = r.dropped > 0 ? [t('… 이전 {dropped}줄 생략 (버퍼 한도)', { dropped: r.dropped })] : []
      setLogs([...head, ...r.lines])
    }).catch(() => {})
    return () => { alive = false }
  }, [viewJobId, t])

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

  // 히스토리 — **열자마자 한 번**, 그리고 학습 중에는 주기적으로
  const histPollRef = useRef<ReturnType<typeof setInterval>>(undefined)
  const fetchHistory = useCallback(
    () => { void api.get<HistoryData>('/training/metrics').then(setHistory).catch(() => {}) }, [])

  // ⚠ **상태를 기다리지 않는다.** 예전에는 `trainState === 'running'` 일 때만
  //   가져왔는데, 화면을 열면 그 값이 아직 `running` 이 아니라 **한 번도 안
  //   가져왔다.** 학습 도중에 들어가면 그래프가 한참 비어 있었고, 학습이 끝난
  //   뒤에 열면 영영 비어 있었다 — 서버에는 곡선이 그대로 있는데도.
  useEffect(() => { fetchHistory() }, [fetchHistory])

  useEffect(() => {
    if (trainState === 'running') {
      histPollRef.current = setInterval(fetchHistory, 5000)
    } else {
      clearInterval(histPollRef.current)
      // 막 끝났으면 마지막 점까지 한 번 더 받는다
      fetchHistory()
    }
    return () => clearInterval(histPollRef.current)
  }, [trainState, fetchHistory])

  // 학습 중 패널 배치 — 값은 localStorage 에 남는다
  const { layout, switchLayout } = useLayout('training')
  const isRunning = trainState === 'running' || trainState === 'starting' || trainState === 'stopping'

  // 권장 베이스 체크포인트 받기 — 목록이 비었을 때 유일한 탈출구다
  const [fetchingBase, setFetchingBase] = useState(false)
  const handleFetchBase = async (repoId: string) => {
    setFetchingBase(true)
    try {
      await api.post('/hub/download', { repo_id: repoId, repo_type: 'model' })
      // 다운로드는 백그라운드다 — 완료를 기다렸다가 목록을 새로 읽는다.
      // ⚠ **끝 판정은 서버가 주는 값으로 한다.** 예전에는 `'running'`·`'started'` 가
      //   아니면 끝이라고 봤는데 서버가 주는 것은 `downloading`·`completed` 라,
      //   **첫 폴링(2초)에서 바로 빠져나왔다** — 기다리는 척만 했다.
      let last: { status?: string; missing?: string[] } = {}
      for (let i = 0; i < 300; i++) {
        await new Promise((r) => setTimeout(r, 2000))
        last = await api
          .get<{ status?: string; missing?: string[] }>(
            `/hub/download/status?repo_id=${encodeURIComponent(repoId)}`)
          .catch(() => ({}))
        if (!dlActive(last.status as never)) break
      }
      // ⚠ 받다 만 것을 조용히 넘기지 않는다 — 목록에는 뜨는데 쓸 때 깨진다.
      if (last.status === 'incomplete') {
        notify({ level: 'error', source: t('학습'),
                 text: t('{repoId}: 파일 {length}개가 빠졌습니다 — 저장소 페이지에서 다시 받으세요', { repoId, length: last.missing?.length ?? 0 }) })
      } else if (last.status === 'error') {
        notify({ level: 'error', source: t('학습'), text: t('{repoId} 다운로드 실패', { repoId }) })
      }
      setModels(await api.get<Model[]>('/models'))
    } catch { /* 실패 사유는 Hub 페이지에서 확인한다 */ }
    finally { setFetchingBase(false) }
  }

  // Fine-tune 모델의 제목·설명 (사이드카). 둘 다 비어 있으면 가져올 것이 없다.
  const baseModel = models.find((m) => m.path === pretrainedPath)
  // 워밍스타트 — 고른 체크포인트가 **다른 정책**(smolvla → SmolVLA-S)이면 그 타입. 학습 시작이 type 을
  // 바꾼 사본으로 연다(backend core/base_align.py). 안 그러면 LeRobot 이 바닐라로 학습한다.
  const warmFrom = policyUi.warm_start_from ?? []
  const warmStartFrom = baseModel?.policy_type && baseModel.policy_type !== policyType
    && warmFrom.includes(baseModel.policy_type) ? baseModel.policy_type : ''
  const baseNotes = baseModel?.notes?.name || baseModel?.notes?.description ? baseModel.notes : undefined
  const importBaseNotes = async () => {
    if (!baseModel || !baseNotes) return
    // 적어 둔 것을 말없이 덮지 않는다. ⚠ window.confirm 금지 — heartbeat 를 막는다(askConfirm 은 논블로킹).
    if ((title || description)
      && !await askConfirm(t('입력해 둔 제목·설명을 "{id}" 모델의 것으로 바꿉니다. 계속할까요?', { id: baseModel.id }))) return
    setTitle(baseNotes.name ?? '')
    setDescription(baseNotes.description ?? '')
  }

  // 정책 변경 시 권장 기본값 + config 필드 리셋
  const handlePolicyChange = (next: string) => {
    setPolicyType(next)
    // 고른 체크포인트가 새 정책과 안 맞으면 비운다 — 남겨두면 화면에는 안 보이는데
    // CLI 인자에는 실려서 "왜 다른 모델로 학습되지"가 된다.
    if (pretrainedPath) {
      const cur = models.find((m) => m.path === pretrainedPath)
      if (cur && cur.policy_type && cur.policy_type !== next) setPretrainedPath('')
    }
    // 기본값은 스펙이 온 뒤 아래 effect 가 채운다 — 여기서 못 하는 이유는
    // `usePolicyUi` 가 비동기라 이 시점엔 아직 이전 정책의 스펙이기 때문이다.
    setCliEdited(false)
  }

  const setPolicyParam = (key: string, value: number | boolean) => {
    setPolicyParams((p) => ({ ...p, [key]: value }))
    setCliEdited(false)
  }

  // 파인튜닝 후보는 **같은 정책의 체크포인트**와, 이 정책이 이어 받을 수 있는 정책(`warm_start_from`)의 것이다.
  // 예전에는 models 디렉토리의 모든 것을 나열해서, smolvla 의 비전-언어 백본
  // (`SmolVLM2-500M-Video-Instruct`) 처럼 정책이 아닌 모델까지 떴다 — 고르면 학습이 깨진다.
  const finetuneCandidates = models.filter(
    (m) => m.is_policy !== false
      && (!m.policy_type || m.policy_type === policyType || warmFrom.includes(m.policy_type))
  )

  // 체크포인트에서 이어 학습하면 모델 구조는 이미 고정 — `arch` 값만 가린다.
  // 학습 방식 스위치(freeze_vision_encoder 등)는 파인튜닝에서도 유효하다.
  const policyFields = policyUi.train.fields.filter((f) => !(pretrainedPath && f.arch))
  const warnings = activeWarnings(policyUi.train.warnings, policyParams)

  const trainParams = () => ({
    dataset_repo_id: selectedDataset, policy_type: policyType,
    pretrained_path: pretrainedPath, policy_repo_id: policyRepoId, output_dir: outputDir,
    title, description,
    batch_size: batchSize, steps, log_freq: logFreq, save_freq: saveFreq,
    num_workers: numWorkers, seed, device, optimizer_type: optimizerType,
    learning_rate: learningRate, wandb_enable: wandbEnable, wandb_project: wandbProject, resume,
    use_policy_training_preset: usePolicyPreset, state_dim: stateDim, action_dim: actionDim, rename_map: renameMap,
    policy_params: policyParams, amp,
  })

  const handlePreConfirm = async () => {
    // CLI 미리보기를 config 확인용으로 표시
    try {
      // ⚠ **클라우드면 다른 인터프리터다.** 임대 기계는 `/opt/venv/bin/python` 이고
      //   이 기계는 conda 경로다 — 같은 미리보기를 쓰면 확인 창이 **안 도는 명령**을
      //   보여 준다. 서버가 실제 경로로 만들어 주는 쪽을 쓴다.
      const r = where === 'rent'
        ? await api.post<{ command: string; args: string[] }>('/cloud/rent/preview', trainParams())
        : await api.post<{ command: string; args: string[] }>('/training/preview', trainParams())
      const configLines = [
        t('=== 학습 설정 확인 ==='),
        '',
        t('데이터셋: {selectedDataset}', { selectedDataset: String(selectedDataset) }),
        t('정책: {policyType}', { policyType: String(policyType) }),
        `Fine-tune: ${pretrainedPath || t('(처음부터)')}`,
        ...(warmStartFrom ? [t('워밍스타트: {from} → {to} (가중치는 그대로, 새 층만 무작위)', { from: warmStartFrom, to: policyType })] : []),
        `Repo ID: ${policyRepoId || t('(없음)')}`,
        `Output: ${outputDir || t('(자동)')}`,
        '',
        `Batch: ${batchSize}  Steps: ${steps}  Save: ${saveFreq}`,
        `Device: ${device}  AMP: ${amp}`,
        `Workers: ${numWorkers}  Seed: ${seed}`,
        `Resume: ${resume}  Preset: ${usePolicyPreset}  State: ${stateDim || 'auto'}  Action: ${actionDim || 'auto'}`,
        '',
        `Rename map: ${renameMap || t('(없음)')}`,
        '',
        t('=== CLI 명령어 ==='),
        r.command,
      ].join('\n')
      setConfirmConfig(configLines)
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('미리보기 실패')
      setLogs((prev) => [...prev, `[ERROR] ${msg}`])
    }
  }

  const handleStart = async () => {
    setConfirmConfig(null)
    try {
      if (where === 'rent') {
        if (!rentPick) throw new Error(t('학습을 걸 기계를 고르세요 — 클라우드 GPU 페이지에서 먼저 빌려야 합니다'))
        // ⚠ **학습 설정은 이 페이지의 것 그대로 보낸다.** 서버는 `RentRequest` 가
        //   `TrainStartRequest` 를 상속하므로 하나도 안 흘린다(§12-18).
        // ⚠ **빌리지 않는다** — 이미 있는 기계에 얹는다. 빌리기는 클라우드 페이지다.
        await api.post('/cloud/train-on', { ...trainParams(), ...rentPick }, { timeoutMs: 120_000 })
        return
      }
      if (cliEdited) {
        await api.post('/training/start-custom', {
          args: cliArgs.split(/\s+/).filter(Boolean), total_steps: steps, output_dir: outputDir,
          // 이어 학습 명령엔 `--policy.type` 이 없다 — 워밍스타트인지 서버가 알 수 있게 같이 보낸다
          policy_type: policyType, policy_params: policyParams,
        })
      } else {
        await api.post('/training/start', trainParams())
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('알 수 없는 오류')
      setLogs((prev) => [...prev, t('[ERROR] 학습 시작 실패: {msg}', { msg })])
    }
  }

  const handleStop = async () => { await api.post('/training/stop') }

  const trainBlockedBy = blockedBy('training')
  const canStart = !!selectedDataset && !isRunning && !isBlocked('training')

  return (
    <div className="space-y-6">
      {/* ⚠ 빌린 기계의 단계·비용·회수. 임대가 아니면 스스로 아무것도 안 그린다 —
          `/cloud/rent` 에 도는 job 이 없으면 `null` 을 돌려준다. */}
      <CloudRentProgress />
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold">{t('학습')}</h1>
          {/* ⚠ 원격이면 **말해 준다.** 이게 없으면 임대 GPU 에서 도는 학습과 이 기계에서
              도는 학습이 화면상 똑같다 — 러너가 조용히 로컬로 떨어졌을 때 알 길이 없다. */}
          {remoteRun && (
            <span className="rounded bg-blue-900/60 px-2 py-0.5 text-xs text-blue-200"
              title={t('이 학습은 다른 기계(SSH)에서 돕니다 — 이 기계의 GPU 는 쓰지 않습니다')}>
              {t('원격')}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {/* job 선택 — 로컬도 job 이다. 원격이 붙으면 같은 자리에 함께 뜬다.
              2개 이상일 때만 보여준다 (지금은 항상 로컬 1개라 화면이 조용하다). */}
          {jobs.length > 1 && (
            <select
              value={viewJobId}
              onChange={(e) => { setViewJobId(e.target.value); setLogs([]) }}
              className="px-2 py-1 text-xs rounded bg-neutral-900 border border-neutral-700 text-neutral-100">
              {jobs.map((j) => (
                <option key={j.job_id} value={j.job_id}>
                  {j.job_id === LOCAL_JOB_ID ? t('로컬') : `${j.runner}:${j.job_id}`} ({j.state})
                </option>
              ))}
            </select>
          )}
          <span className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
          {isRunning && (
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400">{trainState}</span>
          )}
          {trainBlockedBy && (
            <span className="px-2 py-0.5 rounded text-xs bg-amber-500/20 text-amber-400">{t('{trainBlockedBy} 실행 중 — 학습을 시작할 수 없습니다', { trainBlockedBy })}</span>
          )}
        </div>
      </div>

      {!isRunning ? (
        <>
          {/* 설정은 두 열로 나눠 담고, CLI 와 로그는 가로를 다 쓴다.
              예전에는 2:1 세로 분할이라 로그가 좁고 길게 눌려 한 줄이 자꾸 접혔다 —
              LeRobot 로그는 경로와 JSON 이 길어서 가로가 중요하다. */}
          {/* 프리셋 — 얇은 바라 2열 셀에 넣으면 어색하다. 위에 전체폭으로 둔다.
              학습 파라미터는 기기와 무관하므로 shared */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <PresetBar domain="training" scope="shared" policyType={policyType}
              values={presetValues} onApply={applyPreset} disabled={isRunning} />
          </div>

          <div {...twoColumns([1, 1], [420, 420])}>
          {/* ⚠ 열을 **따로 흘린다.** 카드들을 그리드에 직접 담으면 행 단위로
              묶여, 짧은 카드 밑에 옆 칸 높이만큼 빈 공간이 생긴다 — 데이터셋
              카드 밑이 데이터셋 정보 높이만큼 비어 보였던 그 공백이다.
              좁은 창에서도 쌓지 않는다 — 최소 폭 아래로는 가로로 스크롤한다(`twoColumns`). */}
          <div className="space-y-6">
          {/* 데이터셋 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <h3 className="text-sm font-semibold">{t('데이터셋')}</h3>
            {/* 정책이 요구하는 feature 가 없는 데이터셋은 빼고 보여준다 — 스펙(`train.requires_features`)이
                정한다. 조건을 여기 다시 적지 않는다. 이미 고른 것이 조건에 안 맞으면 목록엔 남기되 경고한다. */}
            {(() => {
              const req = policyUi.train.requires_features
              const eligible = (d: Dataset) => req.every((k) => d.features && k in d.features)
              const shown = datasets.filter((d) => eligible(d) || d.id === selectedDataset)
              const hidden = datasets.length - shown.length
              const selectedOk = !selectedDataset || datasets.find((d) => d.id === selectedDataset) == null
                || eligible(datasets.find((d) => d.id === selectedDataset)!)
              const ds = datasets.find((d) => d.id === selectedDataset)
              return (
                <>
                  {/* ⚠ select 를 두지 않는다 — 에피소드 페이지와 같은 [선택하기] 창(표 | 썸네일)에서 고른다.
                      창에는 위에서 거른 목록(`shown`)만 넘긴다 — 정책이 못 받는 데이터셋은 거기서도 안 보인다. */}
                  <PickerField empty={!selectedDataset} placeholder={t('선택된 데이터셋 없음 — 눌러서 고르세요')}
                    onOpen={() => setDatasetPicker(true)}>
                    {ds ? (
                      <>
                        <p className="truncate text-sm font-medium text-neutral-100">{ds.notes?.name || ds.id}</p>
                        <p className="truncate text-[11px] text-neutral-400">
                          {[ds.notes?.name && ds.id, t('{n}개 에피소드', { n: ds.total_episodes }),
                            `${fmt.number(ds.total_frames)} frames`, ds.baked && t('구운 사본'), ds.delta && `Δ ${t('델타')}`]
                            .filter(Boolean).join(' · ')}
                        </p>
                      </>
                    ) : (
                      <>
                        <p className="truncate font-mono text-xs text-neutral-200">{selectedDataset}</p>
                        {/* 목록을 읽기 전에는 경고하지 않는다 — 열자마자 잠깐 "없다" 가 뜨면 거짓말이다 */}
                        {datasets.length > 0 && (
                          <p className="text-[11px] text-amber-400">{t('목록에 없는 데이터셋입니다 — 지워졌거나 이름이 바뀌었을 수 있습니다')}</p>
                        )}
                      </>
                    )}
                  </PickerField>
                  {datasetPicker && (
                    <DatasetPickerModal datasets={shown} value={selectedDataset}
                      onPick={(id) => { setSelectedDataset(id); setCliEdited(false) }}
                      onClose={() => setDatasetPicker(false)}
                      onRenamed={() => { api.get<Dataset[]>('/datasets').then(setDatasets).catch(() => {}) }} />
                  )}
                  {req.length > 0 && (
                    <p className="text-xs text-neutral-500">
                      {t.rich('이 정책은 {code} 가 있는 데이터셋만 받습니다{hidden}. 에피소드 화면의 [ACT-S·SmolVLA-S용 굽기]로 만듭니다.', {
                        code: <code>{req.join(', ')}</code>,
                        hidden: hidden > 0 && t(' — {hidden}개 숨김', { hidden }),
                      })}
                    </p>
                  )}
                  {!selectedOk && (
                    <p className="text-xs text-amber-400">{t.rich('⚠ 선택한 데이터셋에 {code} 가 없습니다 — 이대로 시작하면 첫 배치에서 죽습니다.', { code: <code>{req.join(', ')}</code> })}</p>
                  )}
                </>
              )
            })()}
          </div>

          {/* 정책 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <h3 className="text-sm font-semibold">{t('정책')}</h3>
            {/* ⚠ select 를 두지 않는다 — 이름(`ACT-S`)만으로는 무엇이 다른지, 어떤 데이터셋이 필요한지
                고른 뒤에야 알았다. 창에서 정책마다 설명과 조건을 보고 고른다 (내용은 정책 스펙). */}
            {(() => {
              const cur = trainable.find((p) => p.type === policyType)
              return (
                <PickerField empty={false} placeholder="" onOpen={() => setPolicyPicker(true)}>
                  <p className="truncate text-sm font-medium text-neutral-100">{cur?.label ?? policyType}</p>
                  {cur?.description && (
                    <p className="truncate text-[11px] text-neutral-400" title={cur.description}>{cur.description}</p>
                  )}
                </PickerField>
              )
            })()}
            {policyPicker && (
              <PolicyPickerModal policies={trainable} value={policyType}
                onPick={(type) => { if (type !== policyType) handlePolicyChange(type) }}
                onClose={() => setPolicyPicker(false)} />
            )}
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <label className="text-xs text-neutral-400">{t('Fine-tune 모델 (선택)')}</label>
              </div>
              {/* ⚠ select 를 두지 않는다 — 추론 칸과 같다. 고른 것의 이름만 보이고 [선택하기] 창(표)에서
                  고른다. ⚠ 예전 select 는 저장된 경로가 목록에 없으면 "처음부터 학습" 으로 **보이면서**
                  그 경로로 학습을 걸었다 — 이제는 경로를 그대로 보이고 목록에 없다고 말한다. */}
              <PickerField empty={!pretrainedPath} placeholder={t('선택 안 함 — 처음부터 학습')}
                onOpen={() => setWeightTable(true)}
                onClear={() => { setPretrainedPath(''); setCliEdited(false) }} clearTitle={t('처음부터 학습으로 되돌립니다')}>
                {baseModel ? <CheckpointLabel model={baseModel} models={models} /> : (
                  <>
                    <p className="truncate font-mono text-xs text-neutral-200" title={pretrainedPath}>{pretrainedPath}</p>
                    {/* 목록을 읽기 전에는 경고하지 않는다 — 열자마자 잠깐 "없다" 가 뜨면 거짓말이다 */}
                    {models.length > 0 && (
                      <p className="text-[11px] text-amber-400">{t('목록에 없는 경로입니다 — 지워졌거나 이 기계의 것이 아닐 수 있습니다')}</p>
                    )}
                  </>
                )}
              </PickerField>
              {!pretrainedPath && (
                <p className="text-[10px] text-neutral-500">{t('고르지 않아도 됩니다 — 이어 학습할 때만 체크포인트를 고르세요')}</p>
              )}
              {weightTable && (
                <WeightTableModal title={t('이어 학습할 모델 고르기')} value={baseModel?.id}
                  onClose={() => setWeightTable(false)}
                  onPick={(c) => { setPretrainedPath(c.path); setCliEdited(false) }}
                  include={(r) => r.is_policy}
                  // 후보 규칙은 위 `finetuneCandidates` 와 같다 — 같은 정책이거나 이어 받을 수 있는 정책
                  blockedReason={(r) => !r.policy_type || r.policy_type === policyType || warmFrom.includes(r.policy_type)
                    ? null : t('{policy} 정책이라 {to} 로 이어 학습할 수 없습니다', { policy: r.policy_type, to: policyType })} />
              )}
              {/* SmolVLA 의 `load_vlm_weights` 는 LeRobot 기본값이 **false** 다.
                  false 면 `SmolVLMForConditionalGeneration(config=...)` 로 VLM 을
                  **랜덤 초기화**한다 — 사전학습 가중치를 안 쓴다는 뜻이다.
                  이 저장소가 예전에 실제로 당한 문제라(커밋 ce768bc) 눈에 보이게 경고한다. */}
              {/* ⚠ **조건을 여기 다시 적지 않는다.** 예전엔 `policyType === 'smolvla' &&
                  policyParams.load_vlm_weights === false` 가 박혀 있었고, 아래 파라미터
                  카드에 같은 경고가 또 있었다 — 둘이 갈리면 한쪽만 뜬다.
                  스펙이 `error` 를 냈고 처음부터 학습이면 여기에 크게 띄운다.
                  **무엇을 경고할지는 스펙, 어디에 띄울지는 화면**이다. */}
              {!pretrainedPath && warnings.some((w) => w.level === 'error') && (
                <p className="mt-1 px-2 py-1.5 rounded bg-red-500/10 border border-red-500/30 text-[10px] text-red-300">
                  {t.rich('⚠ 이 상태로 {b}하면 사실상 학습이 되지 않습니다 —', { b: <b>{t('처음부터 학습')}</b> })}{' '}{warnings.filter((w) => w.level === 'error').map((w) => w.text).join(' / ')}{t('. 아래 파라미터에서 고치거나 체크포인트를 골라주세요.')}
                </p>
              )}
              {warmStartFrom && (
                <p className="mt-1 px-2 py-1.5 rounded bg-sky-500/10 border border-sky-500/30 text-[10px] text-sky-300">
                  {t('{from} 체크포인트를 {to} 로 엽니다 — 가중치는 그대로 쓰고, {to} 가 더한 층(stage 헤드)만 무작위로 시작합니다', { from: warmStartFrom, to: policyType })}
                </p>
              )}
              {!pretrainedPath && policyUi.scratch_note && (
                <p className="text-[10px] text-neutral-500 mt-1">
                  {t('처음부터 학습 — {scratch_note}', { scratch_note: policyUi.scratch_note })}
                </p>
              )}
              {finetuneCandidates.length === 0 && (
                <div className="mt-1 space-y-1">
                  <p className="text-[10px] text-neutral-500">
                    {t.rich('로컬에 {policyType} 체크포인트가 없습니다.{length}', {
                      policyType,
                      length: models.length > 0 && t(' (다른 정책 모델 {length}개는 이어서 학습할 수 없어 숨겼습니다)', { length: models.length }),
                    })}
                  </p>
                  {policyBase(policyType) && (
                    <div className="flex items-center gap-2 px-2 py-1.5 rounded bg-amber-500/10 border border-amber-500/30">
                      <span className="text-[10px] text-amber-300 flex-1">
                        {t.rich('권장 시작점: {span} — 없이 처음부터 학습하면 액션 전문가를 맨바닥에서 올립니다', {
                          span: <span className="font-mono">{policyBase(policyType)}</span>,
                        })}
                      </span>
                      <button onClick={() => handleFetchBase(policyBase(policyType))}
                        disabled={fetchingBase}
                        className="shrink-0 px-2 py-0.5 text-[10px] rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
                        {fetchingBase ? t('받는 중…') : t('Hub 에서 받기')}
                      </button>
                    </div>
                  )}
                </div>
              )}
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
                      {t('카메라:')}{' '}{r.required_cameras.map((c) => (
                        <span key={c.name} className="inline-block mr-2">
                          <span className="text-neutral-200">{c.name}</span>
                          {c.model_name && c.model_name !== c.name && <span className="text-neutral-600 text-[10px]">({c.model_name})</span>}
                        </span>
                      ))}
                    </div>
                  )}
                  {/* ⚠ base 와 데이터셋의 길이가 다르면 말해 준다. 아래 데이터셋 정보의 경고는
                      데이터셋의 state 와 action 을 서로 비교할 뿐이라 이 경우를 못 본다 —
                      SmolVLA 는 길이가 달라도 학습이 에러 없이 끝나고, 어긋난 체크포인트로
                      추론에서야 알게 된다 (실기 2026-10-14). */}
                  {(() => {
                    const feats = datasets.find((d) => d.id === selectedDataset)?.features
                    const dsState = feats?.['observation.state']?.shape?.[0]
                    const dsAction = feats?.['action']?.shape?.[0]
                    if (!dsState || !dsAction) return null
                    if (dsState === r.state_dim && dsAction === r.action_dim) return null
                    // 입력을 max_*_dim 으로 패딩하는 정책(smolvla·pi0 계열)이면 가중치가 길이에
                    // 묶이지 않는다 — 백엔드가 시작 때 사본으로 맞춘다 (core/base_align.py).
                    const pads = typeof m.config?.max_state_dim === 'number'
                      && typeof m.config?.max_action_dim === 'number'
                    const vars = { baseState: r.state_dim, baseAction: r.action_dim, dsState, dsAction }
                    return pads ? (
                      <p className="text-amber-400">
                        {t('base 모델은 state {baseState} / action {baseAction} 이고 데이터셋은 state {dsState} / action {dsAction} 입니다. 이 정책은 입력을 패딩하므로 학습을 시작할 때 base 의 길이를 데이터셋에 맞춘 사본을 씁니다 (원본 base 는 그대로).', vars)}
                      </p>
                    ) : (
                      <p className="text-red-400">
                        {t('base 모델은 state {baseState} / action {baseAction} 이고 데이터셋은 state {dsState} / action {dsAction} 입니다. 이 정책은 가중치가 길이에 묶여 있어 이어 학습이 시작 직후 실패합니다 — 처음부터 학습하거나 길이가 같은 base 를 고르세요.', vars)}
                      </p>
                    )
                  })()}
                  <div className="text-[10px] text-neutral-600 truncate">{m.path}</div>
                </div>
              )
            })()}
            {/* 가중치 제목·설명 — 학습이 폴더를 정하면 그 폴더의 piper_notes.json 사이드카로 남는다.
                Fine-tune 모델을 골랐으면 그 모델의 제목·설명을 가져올 수 있다 — 자동으로
                채우지 않는다(이어 학습한 결과는 다른 가중치라 같은 이름을 물려받으면 헷갈린다). */}
            <div className="space-y-1">
              <div className="flex items-center justify-between gap-2">
                <label className="text-xs text-neutral-400">{t('가중치 제목 (선택)')}</label>
                {baseNotes && (
                  <button type="button" onClick={importBaseNotes}
                    title={t('{id} 의 제목·설명을 아래 칸에 채웁니다', { id: baseModel?.id ?? '' })}
                    className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white transition-colors">
                    {t('Fine-tune 모델에서 가져오기')}
                  </button>
                )}
              </div>
              <input type="text" value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={t('사람이 읽는 이름 — 모델 목록에 경로 대신 보입니다')}
                className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
              <label className="block text-xs text-neutral-400">{t('가중치 설명 (선택)')}</label>
              <textarea value={description} rows={2}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t('어떤 데이터·조건·의도로 학습했는지 — 학습 폴더에 남고, 모델 페이지에서 고칠 수 있습니다')}
                className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
            </div>
            <div className="space-y-1">
              <label className="text-xs text-neutral-400">{t('Policy Repo ID (Hub 업로드용)')}</label>
              {/* ⚠ 비워 두면 `--policy.push_to_hub=false` 가 붙어 로컬에만 남는다.
                  "올리지 않음" 을 명시적인 선택지로 둔다 — 빈 칸을 그냥 두는 것과
                  고르는 것은 다르다. */}
              <RepoIdInput value={policyRepoId} allowEmpty
                onChange={(v) => { setPolicyRepoId(v); setCliEdited(false) }} />
              {policyRepoId && (() => {
                const existing = models.find((m) => m.id.includes(policyRepoId.split('/').pop() || ''))
                if (!existing) return null
                return (
                  <p className="text-xs text-amber-400 mt-1">
                    {t('이미 로컬에 존재합니다: {id}', { id: existing.id })}
                    <button onClick={async () => {
                      if (!await askConfirm(t('"{id}" 모델을 삭제하시겠습니까?\n경로: {path}', { id: existing.id, path: existing.path }))) return
                      await api.delete(`/models/${existing.id}`)
                      api.get<Model[]>('/models').then(setModels).catch(() => {})
                    }} className="ml-2 text-red-400 hover:text-red-300 underline">{t('삭제')}</button>
                  </p>
                )
              })()}
            </div>
          </div>

          {/* 정책별 파라미터.
              예전에는 pretrained 가 있으면 **패널 전체를 숨겼다.** 아키텍처 값이
              체크포인트에 고정되는 건 맞지만, `freeze_vision_encoder` 같은 **학습 스위치**
              까지 같이 사라져서 파인튜닝에서 가장 중요한 손잡이를 못 만졌다.
              이제 `arch` 값만 가린다. */}
          {policyFields.length > 0 && (
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
              <h3 className="text-sm font-semibold">{t.rich('{policyType} 파라미터', { policyType: policyType.toUpperCase() })}</h3>
              {pretrainedPath && (
                <p className="text-[10px] text-neutral-500">
                  {t('체크포인트에서 이어 학습 중 — 모델 구조 값(chunk_size 등)은 체크포인트에 고정돼 숨겼습니다.')}
                </p>
              )}
              <SpecFields fields={policyFields} values={policyParams} onChange={setPolicyParam} />
              {/* 정책과 무관한 규칙이라 스펙이 아니라 여기 남는다 — 두 키가 다
                  있을 때만 뜻이 있고, 어느 정책이든 같은 관계다. */}
              {typeof policyParams.n_action_steps === 'number' && typeof policyParams.chunk_size === 'number'
                && policyParams.n_action_steps > policyParams.chunk_size && (
                <span className="text-yellow-500 text-[10px]">{t('⚠ n_action_steps는 chunk_size 이하여야 합니다')}</span>
              )}
              {/* 정책별 경고는 스펙이 준다 — 예전엔 `policyType === 'smolvla'` 가
                  화면에 박혀 있었다. 조건도 문구도 `policies/<type>.yaml` 에 있다. */}
              {warnings.map((w, i) => (
                <span key={i} className={`block text-[10px] ${
                  w.level === 'error' ? 'text-red-400'
                  : w.level === 'warn' ? 'text-yellow-500' : 'text-neutral-400'}`}>
                  ⚠ {w.text}
                </span>
              ))}
            </div>
          )}
          </div>

          <div className="space-y-6">
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
                <h3 className="text-sm font-semibold">{t('데이터셋 정보')}</h3>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div>
                    <span className="text-neutral-400">{t('관절 (state):')}</span>
                    <span className="ml-1 text-neutral-100">{t('{stateShape}개', { stateShape: stateShape?.[0] ?? '?' })}</span>
                    {stateNames && <span className="text-neutral-500 ml-1">({stateNames.join(', ')})</span>}
                  </div>
                  <div>
                    <span className="text-neutral-400">{t('액션:')}</span>
                    <span className="ml-1 text-neutral-100">{t('{actionShape}차원', { actionShape: actionShape?.[0] ?? '?' })}</span>
                  </div>
                </div>
                {stateShape && actionShape && stateShape[0] !== actionShape[0] && (
                  <p className="text-xs text-amber-400">{t('state({stateShape})와 action({actionShape}) 차원이 다릅니다. 베이스 모델의 기본 state_dim이 다를 수 있으니 CLI에서 확인하세요.', { stateShape: stateShape[0], actionShape: actionShape[0] })}</p>
                )}
                {cameras.length > 0 && (
                  <div className="text-xs">
                    <span className="text-neutral-400">{t('카메라 ({length}대):', { length: cameras.length })}</span>
                    <span className="ml-1 text-neutral-100">
                      {cameras.map((c) => c.replace('observation.images.', '')).join(', ')}
                    </span>
                  </div>
                )}
                <div className="text-xs pt-2 space-y-2">
                  <div className="flex items-center justify-between">
                    <label className="text-neutral-400 font-medium">{t('카메라 이름 매핑')}</label>
                    {renameMap && pretrainedPath && <span className="text-[10px] text-green-400">{t('자동 감지됨')}</span>}
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
                              <th className="text-left py-1 pr-2">{t('데이터셋 카메라')}</th>
                              <th className="text-center py-1 px-1">→</th>
                              <th className="text-left py-1 pl-2">{t('모델 카메라')}</th>
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
                          className="text-[10px] text-blue-400 hover:text-blue-300">{t('+ 매핑 추가')}</button>
                      </>
                    )
                  })()}
                  <textarea value={renameMap} readOnly rows={2}
                    className="w-full px-2 py-1 rounded bg-neutral-950 border border-neutral-800 text-[10px] font-mono text-neutral-500 resize-none" />
                </div>
              </div>
            )
          })()}

          {/* 기본 파라미터 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <h3 className="text-sm font-semibold">{t('파라미터')}</h3>
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
              <label className="text-xs text-neutral-400">{t('혼합정밀도 (AMP)')}</label>
              <select value={amp} onChange={(e) => { setAmp(e.target.value); setCliEdited(false) }}
                className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                <option value="bf16">{t('bf16 (권장 · RTX 5090)')}</option>
                <option value="fp16">fp16</option>
                <option value="off">off (fp32)</option>
              </select>
              <p className="text-[10px] text-neutral-500 mt-0.5">
                {t('bf16: 속도↑·VRAM↓ (5090 권장). VRAM 여유 시 Batch를 32~48로 키우세요.')}
              </p>
            </div>

            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input type="checkbox" checked={resume} onChange={(e) => { setResume(e.target.checked); setCliEdited(false) }}
                className="accent-blue-500" />
              <span className="text-neutral-400">{t('체크포인트에서 재개 (Resume)')}</span>
            </label>
            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input type="checkbox" checked={usePolicyPreset} onChange={(e) => { setUsePolicyPreset(e.target.checked); setCliEdited(false) }}
                className="accent-blue-500" />
              <span className="text-neutral-400">{t('정책 학습 프리셋 사용 (use_policy_training_preset)')}</span>
              {!usePolicyPreset && <span className="text-yellow-500 text-[10px]">{t('OFF — config.json 값 그대로 사용')}</span>}
            </label>
          </div>

          {/* 고급 설정 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <button onClick={() => setShowAdvanced(!showAdvanced)}
              className="text-sm font-semibold text-neutral-300 hover:text-white">
              {t('{showAdvanced} 고급 설정', { showAdvanced: showAdvanced ? '▾' : '▸' })}
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
                  <label className="text-xs text-neutral-400">{t('Learning Rate (0=기본)')}</label>
                  <input type="number" value={learningRate} step={0.0001} min={0}
                    onChange={(e) => { setLearningRate(Number(e.target.value)); setCliEdited(false) }}
                    className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                </div>
                <div>
                  <label className="text-xs text-neutral-400">Workers</label>
                  <input type="number" value={numWorkers} min={0} max={32}
                    onChange={(e) => { setNumWorkers(Number(e.target.value)); setCliEdited(false) }}
                    className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                  <p className="text-[10px] text-neutral-500 mt-0.5">{t('data_s > updt_s면 ↑ (데이터 로딩 병목)')}</p>
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
                  <label className="text-xs text-neutral-400">{t('Output Dir (비우면 자동)')}</label>
                  <input type="text" value={outputDir}
                    onChange={(e) => { setOutputDir(e.target.value); setCliEdited(false) }}
                    className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                </div>
                <div className="col-span-2">
                  <label className="flex items-center gap-2 text-xs cursor-pointer">
                    <input type="checkbox" checked={wandbEnable} onChange={(e) => { setWandbEnable(e.target.checked); setCliEdited(false) }}
                      className="accent-blue-500" />
                    <span className="text-neutral-400">{t('WandB 로깅')}</span>
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
          </div>

          </div>

          {/* CLI 미리보기 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold">{t('CLI 명령어')}</h3>
              {cliEdited && (
                <button onClick={() => setCliEdited(false)} className="text-xs text-blue-400 hover:underline">{t('초기화')}</button>
              )}
            </div>
            <textarea value={cliArgs}
              onChange={(e) => { setCliArgs(e.target.value); setCliEdited(true) }}
              rows={4}
              className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-xs font-mono text-neutral-100 focus:outline-none focus:border-blue-500 resize-y" />
            {amp !== 'off' && (
              <p className="text-[10px] font-mono text-neutral-500">
                env: ACCELERATE_MIXED_PRECISION={amp}
                <span className="ml-1 not-italic text-neutral-600">{t('(CLI 직접 편집 시 미적용)')}</span>
              </p>
            )}
            {/* ⚠ **시작 버튼 바로 위다.** 위쪽 폼 어딘가에 두면 "어디서 도는지" 를
                모른 채 누르게 된다 — 임대는 그 한 번이 곧 돈이다. */}
            <TrainWhereForm
              where={where} onWhere={setWhere} onPick={setRentPick}
              runner={runner}
              // ⚠ CLI 를 직접 고친 경우는 임대로 못 보낸다 — `/cloud/rent` 는 인자를
              //   스스로 조립한다. 막고 **왜인지 말한다.**
              disabledReason={cliEdited ? t('CLI 를 직접 고친 학습은 아직 빌려서 못 돌립니다') : undefined}
            />

            <button onClick={cliEdited ? handleStart : handlePreConfirm}
              disabled={!canStart || (where === 'rent' && !rentPick)}
              className="w-full px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium">
              {where === 'rent' ? t('고른 기계에서 학습 시작')
                : cliEdited ? t('학습 시작 (CLI 직접)') : t('설정 확인 후 시작')}
            </button>
          </div>

          {/* 설정 확인 모달 */}
          {confirmConfig && (
            <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setConfirmConfig(null)}>
              <div className="bg-neutral-800 border border-neutral-600 rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
                <h3 className="text-lg font-bold mb-3">{t('학습 설정 확인')}</h3>
                <pre className="flex-1 overflow-auto text-xs font-mono bg-neutral-900 p-4 rounded border border-neutral-700 text-neutral-200 whitespace-pre-wrap mb-4">
                  {confirmConfig}
                </pre>
                <div className="flex gap-3 justify-end">
                  <button onClick={() => setConfirmConfig(null)}
                    className="px-4 py-2 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 text-sm">{t('취소')}</button>
                  <button onClick={handleStart}
                    className="px-6 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium">{t('학습 시작')}</button>
                </div>
              </div>
            </div>
          )}

          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <h3 className="text-sm font-semibold mb-2">{t('로그')}</h3>
            <LogViewer logs={logs} onClear={() => setLogs([])} />
          </div>
        </>
      ) : (
        <>
          {/* 학습 중 배치 — 그래프는 넓을수록 읽기 좋고, 로그는 가로가 중요하다.
              어느 쪽을 넓게 볼지는 화면 크기와 그때 보고 싶은 것이 정한다. */}
          <LayoutToggle layout={layout} onChange={switchLayout} />

          <div {...(layout === 'row'
            ? twoColumns([3, 2], [480, 360], 16)
            : { className: 'space-y-4' })}>
          <div className="space-y-4">
          <TrainingMetrics metrics={metrics} history={history} />

          {/* 체크포인트 */}
          {checkpoints.length > 0 && (
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
              <h3 className="text-sm font-semibold">{t('체크포인트')}</h3>
              <div className="space-y-1 max-h-40 overflow-y-auto">
                {checkpoints.map((ck) => (
                  <div key={ck.name} className="flex items-center justify-between text-xs px-2 py-1 rounded bg-neutral-900">
                    <span className="font-mono">step {fmt.number(ck.step)}</span>
                    <span className="text-neutral-400">{ck.size_kb.toFixed(0)} KB</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <button onClick={handleStop}
            className="w-full px-4 py-2 rounded bg-red-600 hover:bg-red-500 text-white text-sm font-medium">
            {t('학습 정지')}
          </button>
          </div>

          {/* ⚠ 로그는 **좌우 분할 안에 있어도 된다** — 여기서는 그게 요점이다.
              가로 배치는 그래프와 로그를 나란히 보려는 것이고, 좁으면 세로로
              돌리면 된다. 설정 화면의 로그와는 사정이 다르다
              (`test_page_layout.py` 는 설정 쪽을 잠근다). */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <h3 className="text-sm font-semibold mb-2">{t('로그')}</h3>
            <LogViewer logs={logs} onClear={() => setLogs([])} />
          </div>
          </div>
        </>
      )}
    </div>
  )
}
