import { useEffect, useState, useCallback } from 'react'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import type { ProcessState } from '../types/ws'
import { useActivity, isStateMessage } from '../hooks/useActivity'
import LogViewer from '../components/LogViewer'
import RecordPreview from '../components/RecordPreview'
import { camOptionText, type ReadyCam } from '../types/camera'

type ReadyArm = { iface: string; role: string }
type RecordStatusData = { state?: string; current_episode: number; total_episodes: number; phase: string; progress: number }

const VCODECS = ['auto', 'libsvtav1', 'h264', 'hevc', 'h264_nvenc', 'libx264']

export default function RecordingPage() {
  const [followers, setFollowers] = useState<ReadyArm[]>([])
  const [leaders, setLeaders] = useState<ReadyArm[]>([])
  const [cameras, setCameras] = useState<ReadyCam[]>([])

  // 설정
  // 모든 설정값을 localStorage에서 복원
  const _saved = (() => { try { return JSON.parse(localStorage.getItem('piper_record_settings') || '{}') } catch { return {} } })()
  const [followerPort, setFollowerPort] = useState(_saved.followerPort || '')
  const [leaderPort, setLeaderPort] = useState(_saved.leaderPort || '')
  const [cameraMapping, setCameraMapping] = useState<Record<string, string>>(_saved.cameraMapping || {})
  const [camWidth, setCamWidth] = useState(_saved.camWidth ?? 480)
  const [camHeight, setCamHeight] = useState(_saved.camHeight ?? 360)
  const [repoId, setRepoId] = useState(_saved.repoId || '')
  // 네임스페이스를 제안하려고 HF 로그인 사용자를 읽는다 (없으면 안내만)
  const [hfUser, setHfUser] = useState('')
  const [singleTask, setSingleTask] = useState(_saved.singleTask || '')
  const [numEpisodes, setNumEpisodes] = useState(_saved.numEpisodes ?? 50)
  const [fps, setFps] = useState(_saved.fps ?? 15)
  const [episodeTime, setEpisodeTime] = useState(_saved.episodeTime ?? 60)
  const [resetTime, setResetTime] = useState(_saved.resetTime ?? 60)
  const [streamingEncoding, setStreamingEncoding] = useState(_saved.streamingEncoding ?? true)
  const [vcodec, setVcodec] = useState(_saved.vcodec || 'auto')
  const [encoderThreads, setEncoderThreads] = useState(_saved.encoderThreads ?? 4)
  const [encoderQueue, setEncoderQueue] = useState(_saved.encoderQueue ?? 100)
  const [pushToHub, setPushToHub] = useState(_saved.pushToHub ?? true)
  const [webPreview, setWebPreview] = useState(_saved.webPreview ?? true)
  const [resume, setResume] = useState(false)
  const [cliArgs, setCliArgs] = useState('')
  const [cliEdited, setCliEdited] = useState(false)
  const [datasetExists, setDatasetExists] = useState<{ exists: boolean; size_mb: number } | null>(null)
  const [checkTrigger, setCheckTrigger] = useState(0)

  // 실행 상태
  const [recordState, setRecordState] = useState<ProcessState>('idle')
  const [status, setStatus] = useState<RecordStatusData | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const MAX_LOGS = 500

  const isRunning = recordState === 'running' || recordState === 'starting' || recordState === 'stopping'

  // 배타 규칙은 백엔드 exclusivity.py 한 곳에만 있다
  const { isBlocked, blockedBy, refresh: refreshActivity } = useActivity()

  const { connected } = useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (isStateMessage(msg.type)) refreshActivity()
      if (msg.type === 'record_state') {
        setRecordState(msg.data as ProcessState)
        // 녹화 종료 시 데이터셋 존재 재확인 트리거
        if (msg.data === 'idle' || msg.data === 'error') {
          setCheckTrigger(n => n + 1)
        }
      }
      else if (msg.type === 'record_status') setStatus(msg.data as RecordStatusData)
      else if (msg.type === 'record_log') setLogs((prev) => {
        const next = [...prev, msg.data as string]
        return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next
      })
    }, [refreshActivity]),
  })

  useEffect(() => {
    api.get<ReadyArm[]>('/robots/ready').then((all) => {
      setFollowers(all.filter(a => a.role === 'follower'))
      setLeaders(all.filter(a => a.role === 'leader'))
    }).catch(() => {})
    api.get<ReadyCam[]>('/cameras/ready').then(setCameras).catch(() => {})
    api.get<{ username: string }>('/hub/whoami').then((r) => setHfUser(r.username || '')).catch(() => {})
    api.get<RecordStatusData>('/recording/status').then((s) => setRecordState(s.state as ProcessState)).catch(() => {})
  }, [])

  // 설정값 변경 시 localStorage에 통합 저장
  useEffect(() => {
    localStorage.setItem('piper_record_settings', JSON.stringify({
      followerPort, leaderPort, cameraMapping, camWidth, camHeight,
      repoId, singleTask, numEpisodes, fps, episodeTime, resetTime,
      streamingEncoding, vcodec, encoderThreads, encoderQueue, pushToHub, webPreview,
    }))
  }, [followerPort, leaderPort, cameraMapping, camWidth, camHeight, repoId, singleTask, numEpisodes, fps, episodeTime, resetTime, streamingEncoding, vcodec, encoderThreads, encoderQueue, pushToHub, webPreview])

  // 데이터셋 존재 여부 확인
  useEffect(() => {
    if (!repoId || repoId.split('/').length < 2) { setDatasetExists(null); return }
    api.get<{ exists: boolean; size_mb: number }>(`/recording/check-dataset/${repoId}`)
      .then(setDatasetExists).catch(() => setDatasetExists(null))
  }, [repoId, checkTrigger])

  const handleDeleteDataset = async () => {
    if (!repoId || !confirm(`"${repoId}" 데이터셋을 삭제하시겠습니까? 복구할 수 없습니다.`)) return
    try {
      await api.delete(`/recording/delete-dataset/${repoId}`)
      setDatasetExists({ exists: false, size_mb: 0 })
      setLogs(prev => [...prev, `[INFO] 데이터셋 ${repoId} 삭제 완료`])
    } catch { setLogs(prev => [...prev, `[ERROR] 데이터셋 삭제 실패`]) }
  }

  // 카메라 매핑 JSON 빌드 (preview/start 공용)
  // 카메라 JSON 조립은 **백엔드가 한다** (`services/camera_config.py`).
  // 여기서 조립하면 백엔드 설정(`camera_transport`)을 몰라 녹화만 옛 경로를 탄다.
  // 매핑과 해상도만 보낸다.

  // CLI 미리보기
  useEffect(() => {
    if (cliEdited || !repoId) return
    api.post<{ command: string }>('/recording/preview', {
      robot_port: followerPort,
      camera_mapping: cameraMapping,
      camera_width: camWidth, camera_height: camHeight, camera_fps: fps,
      teleop_port: leaderPort,
      repo_id: repoId, single_task: singleTask,
      num_episodes: numEpisodes, fps, episode_time_s: episodeTime, reset_time_s: resetTime,
      streaming_encoding: streamingEncoding, vcodec, encoder_threads: encoderThreads, encoder_queue_maxsize: encoderQueue, push_to_hub: pushToHub, resume,
    }).then(r => setCliArgs(r.command)).catch(() => {})
  }, [followerPort, leaderPort, cameraMapping, repoId, singleTask, numEpisodes, fps, episodeTime, resetTime, streamingEncoding, vcodec, pushToHub, resume, cliEdited])

  const recordBlockedBy = blockedBy('recording')
  // LeRobot 은 `repo_id.split("/")` 를 2개로 언패킹한다. 슬래시가 없으면 녹화 시작
  // 직후 ValueError 로 죽는데, 그땐 이미 팔·카메라를 다 잡은 뒤라 원인을 알기 어렵다.
  // 백엔드 `hf_layout.repo_id_error` 와 같은 규칙을 여기서도 미리 알려준다.
  const repoIdError = (() => {
    const v = repoId.trim()
    if (!v) return null                                   // 비어 있으면 그냥 시작 불가
    if (!v.includes('/')) return `'네임스페이스/이름' 형식이어야 합니다 — 예: ${hfUser || 'my-org'}/${v}`
    if (v.split('/').length > 2) return '슬래시는 하나만 쓸 수 있습니다'
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/.test(v))
      return '영문·숫자와 -_. 만 쓸 수 있고 각 부분은 영문·숫자로 시작해야 합니다'
    return null
  })()

  const canStart = !!followerPort && !!leaderPort && !!repoId && !repoIdError && !!singleTask && !isRunning && !isBlocked('recording')

  const handleStart = async () => {
    try {
      if (cliEdited) {
        // custom 미지원 — preview에서 빌드된 args 사용
      }
      await api.post('/recording/start', {
        robot_port: followerPort,
        camera_mapping: cameraMapping,
        camera_width: camWidth, camera_height: camHeight, camera_fps: fps,
        teleop_port: leaderPort,
        repo_id: repoId, single_task: singleTask,
        num_episodes: numEpisodes, fps, episode_time_s: episodeTime, reset_time_s: resetTime,
        streaming_encoding: streamingEncoding, vcodec, encoder_threads: encoderThreads, encoder_queue_maxsize: encoderQueue, push_to_hub: pushToHub, resume,
        web_preview: webPreview,
      })
    } catch (e) {
      const msg = e instanceof Error ? e.message : '알 수 없는 오류'
      setLogs(prev => [...prev, `[ERROR] ${msg}`])
    }
  }

  // 정지는 즉시 끝나지 않는다 — `escape` 를 받은 LeRobot 이 현재 에피소드를
  // 데이터셋에 쓰고 비디오를 인코딩한 뒤에야 종료한다(60초 에피소드면 수 초).
  // 버튼이 멈춘 것처럼 보이지 않게 진행 중임을 알린다.
  const [stopping, setStopping] = useState(false)
  // 녹화 중 task 변경 — 에피소드 경계에서만 반영된다
  const [taskDraft, setTaskDraft] = useState('')
  const [liveTask, setLiveTask] = useState('')
  const [taskMsg, setTaskMsg] = useState('')
  const handleTaskChange = async () => {
    const t = taskDraft.trim()
    if (!t) return
    try {
      await api.post('/recording/task', { task: t })
      setLiveTask(t)
      setTaskDraft('')
      setTaskMsg(`다음 에피소드부터 "${t}" 로 기록됩니다`)
    } catch (e) {
      setTaskMsg(`변경 실패: ${(e as Error).message}`)
    }
  }
  const handleStop = async () => {
    setStopping(true)
    try { await api.post('/recording/stop') }
    catch { /* 상태는 WS 로 온다 */ }
    finally { setStopping(false) }
  }
  const handleSkip = () => api.post('/recording/skip')
  const handleRerecord = () => api.post('/recording/rerecord')

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">데이터 수집</h1>
        <div className="flex items-center gap-3">
          <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
          {isRunning && (
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400">{recordState}</span>
          )}
        </div>
      </div>

      {!isRunning ? (
        <>
          {/* 설정 화면 — 로봇·데이터셋을 한 줄에 나란히 두고,
              로그와 CLI 는 가로를 다 쓴다. 예전에는 2:1 세로 분할이라
              로그가 좁고 길게 눌려 한 줄이 자꾸 접혔다. */}
          <div className="grid gap-6 grid-cols-1 lg:grid-cols-2 items-start">
          {/* 로봇 선택 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <h3 className="text-sm font-semibold">로봇</h3>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-neutral-400">Follower</label>
                <select value={followerPort} onChange={e => { setFollowerPort(e.target.value); setCliEdited(false) }}
                  className="w-full px-2 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                  <option value="">선택...</option>
                  {followers.map(a => <option key={a.iface} value={a.iface}>{a.iface}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-neutral-400">Leader (텔레오퍼레이션)</label>
                <select value={leaderPort} onChange={e => { setLeaderPort(e.target.value); setCliEdited(false) }}
                  className="w-full px-2 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                  <option value="">선택...</option>
                  {leaders.map(a => <option key={a.iface} value={a.iface}>{a.iface}</option>)}
                </select>
              </div>
            </div>
            {/* 카메라 */}
            {cameras.length > 0 && (
              <div className="space-y-1 pt-2">
                <div className="flex items-center justify-between">
                  <label className="text-xs text-neutral-400">카메라 매핑</label>
                  <div className="flex gap-1">
                    {([
                      { label: 'top', preset: { top: '' } },
                      { label: 'top+hand', preset: { top: '', hand: '' } },
                      { label: 'top+wrist', preset: { top: '', wrist: '' } },
                      { label: 'top+left+right', preset: { top: '', left_hand: '', right_hand: '' } },
                      { label: 'top+wrist+side', preset: { top: '', wrist: '', side: '' } },
                      { label: 'front+side', preset: { front: '', side: '' } },
                    ] as { label: string; preset: Record<string, string> }[]).map(({ label, preset }) => (
                      <button key={label} onClick={() => { setCameraMapping(preset); setCliEdited(false) }}
                        className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-700 text-neutral-400 hover:text-white hover:bg-neutral-600">
                        {label}
                      </button>
                    ))}
                    <button onClick={() => {
                      const name = `cam${Object.keys(cameraMapping).length}`
                      setCameraMapping(prev => ({ ...prev, [name]: '' }))
                      setCliEdited(false)
                    }} className="text-[10px] px-1.5 py-0.5 rounded bg-blue-600/30 text-blue-400 hover:bg-blue-600 hover:text-white">+ 추가</button>
                  </div>
                </div>
                {Object.entries(cameraMapping).map(([name, devId], idx) => (
                  <div key={idx} className="flex items-center gap-1.5 text-xs">
                    <input type="text" value={name}
                      onChange={e => {
                        const newName = e.target.value
                        setCameraMapping(prev => {
                          const entries = Object.entries(prev).map(([k, v], i) => i === idx ? [newName, v] : [k, v])
                          return Object.fromEntries(entries)
                        })
                        setCliEdited(false)
                      }}
                      className="w-16 px-1.5 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 font-mono" />
                    <select value={devId} onChange={e => { setCameraMapping(prev => ({ ...prev, [name]: e.target.value })); setCliEdited(false) }}
                      className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100">
                      <option value="">없음</option>
                      {cameras.map(c => {
                        const usedBy = Object.entries(cameraMapping).find(([k, v]) => v === c.id && k !== name)
                        return <option key={c.id} value={c.id} disabled={!!usedBy}>{camOptionText(c)}{usedBy ? ` — ${usedBy[0]}` : ''}</option>
                      })}
                    </select>
                    <button onClick={() => {
                      setCameraMapping(prev => {
                        const entries = Object.entries(prev).filter((_, i) => i !== idx)
                        return Object.fromEntries(entries)
                      })
                      setCliEdited(false)
                    }} className="text-red-400 hover:text-red-300 px-1">✕</button>
                  </div>
                ))}
                {/* 카메라 해상도/FPS */}
                {Object.keys(cameraMapping).length > 0 && (
                  <div className="flex gap-2 pt-1">
                    {[
                      { label: 'W', val: camWidth, set: setCamWidth },
                      { label: 'H', val: camHeight, set: setCamHeight },
                    ].map(({ label, val, set }) => (
                      <div key={label} className="flex items-center gap-1 text-xs">
                        <span className="text-neutral-500">{label}</span>
                        <input type="number" value={val}
                          onChange={e => { set(Number(e.target.value)); setCliEdited(false) }}
                          className="w-14 px-1 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 text-center" />
                      </div>
                    ))}
                    <div className="flex gap-1 ml-auto">
                      {[
                        { label: '480p', w: 640, h: 480 },
                        { label: '360p', w: 480, h: 360 },
                        { label: '240p', w: 320, h: 240 },
                      ].map(({ label, w, h }) => (
                        <button key={label} onClick={() => { setCamWidth(w); setCamHeight(h); setCliEdited(false) }}
                          className={`px-1.5 py-0.5 text-[10px] rounded ${camWidth === w ? 'bg-blue-600 text-white' : 'bg-neutral-700 text-neutral-400'}`}>
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* 데이터셋 설정 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <h3 className="text-sm font-semibold">데이터셋</h3>
            <div>
              <label className="text-xs text-neutral-400">Repo ID (예: wego-hansu/piper_demo)</label>
              <input type="text" value={repoId} onChange={e => { setRepoId(e.target.value); setCliEdited(false) }}
                className={`w-full px-3 py-1.5 rounded bg-neutral-900 border text-sm text-neutral-100 ${repoIdError ? 'border-red-500' : 'border-neutral-700'}`} />
              {repoIdError && (
                <div className="flex items-center justify-between gap-2 mt-1 px-2 py-1 rounded bg-red-500/10 border border-red-500/30 text-xs">
                  <span className="text-red-300">{repoIdError}</span>
                  {!repoId.includes('/') && (
                    <button onClick={() => { setRepoId(`${hfUser || 'my-org'}/${repoId.trim()}`); setCliEdited(false) }}
                      className="shrink-0 px-2 py-0.5 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white text-[10px]">
                      {hfUser || 'my-org'}/ 붙이기
                    </button>
                  )}
                </div>
              )}
              {datasetExists?.exists && (
                <div className="flex items-center justify-between mt-1 px-2 py-1 rounded bg-amber-500/10 border border-amber-500/30 text-xs">
                  <span className="text-amber-300">기존 데이터셋 존재 ({datasetExists.size_mb} MB) — 이어서 녹화됩니다</span>
                  <button onClick={handleDeleteDataset}
                    className="px-2 py-0.5 rounded bg-red-600 hover:bg-red-500 text-white text-[10px]">
                    삭제
                  </button>
                </div>
              )}
            </div>
            <div>
              <label className="text-xs text-neutral-400">Task 설명</label>
              <input type="text" value={singleTask}
                onChange={e => { setSingleTask(e.target.value); setCliEdited(false) }}
                placeholder="Pick the cube and put in the box"
                className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
            </div>
            <div className="grid grid-cols-2 gap-2">
              {[
                { label: '에피소드 수', val: numEpisodes, set: setNumEpisodes, min: 1, max: 1000 },
                { label: 'FPS', val: fps, set: setFps, min: 10, max: 60 },
                { label: '에피소드 시간(s)', val: episodeTime, set: setEpisodeTime, min: 5, max: 300 },
                { label: '리셋 시간(s)', val: resetTime, set: setResetTime, min: 5, max: 300 },
              ].map(({ label, val, set, min, max }) => (
                <div key={label}>
                  <label className="text-xs text-neutral-400">{label}</label>
                  <input type="number" value={val} min={min} max={max}
                    onChange={e => { set(Number(e.target.value)); setCliEdited(false) }}
                    className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
                </div>
              ))}
            </div>
            <div className="grid grid-cols-3 gap-2 pt-1">
              <div>
                <label className="text-xs text-neutral-400">Codec</label>
                <select value={vcodec} onChange={e => { setVcodec(e.target.value); setCliEdited(false) }}
                  className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
                  {VCODECS.map(v => <option key={v} value={v}>{v}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-neutral-400">Enc Threads</label>
                <input type="number" value={encoderThreads} min={1} max={16}
                  onChange={e => { setEncoderThreads(Number(e.target.value)); setCliEdited(false) }}
                  className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
              </div>
              <div>
                <label className="text-xs text-neutral-400">Enc Queue</label>
                <input type="number" value={encoderQueue} min={30} max={500}
                  onChange={e => { setEncoderQueue(Number(e.target.value)); setCliEdited(false) }}
                  className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                  <input type="checkbox" checked={streamingEncoding} onChange={e => { setStreamingEncoding(e.target.checked); setCliEdited(false) }} className="accent-blue-500" />
                  <span className="text-neutral-400">스트리밍 인코딩</span>
                </label>
                <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                  <input type="checkbox" checked={pushToHub} onChange={e => { setPushToHub(e.target.checked); setCliEdited(false) }} className="accent-blue-500" />
                  <span className="text-neutral-400">Hub 업로드</span>
                </label>
                <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                  <input type="checkbox" checked={resume} onChange={e => { setResume(e.target.checked); setCliEdited(false) }} className="accent-blue-500" />
                  <span className="text-neutral-400">이어서 녹화 (Resume)</span>
                </label>
                <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                  <input type="checkbox" checked={webPreview} onChange={e => setWebPreview(e.target.checked)} className="accent-blue-500" />
                  <span className="text-neutral-400">녹화 중 웹 미리보기</span>
                </label>
              </div>
            </div>
          </div>

          </div>

          {/* CLI 미리보기 */}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold">CLI 명령어</h3>
              {cliEdited && <button onClick={() => setCliEdited(false)} className="text-xs text-blue-400 hover:underline">초기화</button>}
            </div>
            <textarea value={cliArgs} onChange={e => { setCliArgs(e.target.value); setCliEdited(true) }}
              rows={4}
              className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-xs font-mono text-neutral-100 resize-y" />
            {recordBlockedBy && (
              <p className="text-xs text-amber-400">{recordBlockedBy} 실행 중 — 먼저 중지하세요</p>
            )}
            <button onClick={handleStart} disabled={!canStart}
              className="w-full px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium">
              녹화 시작
            </button>
          </div>

          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <h3 className="text-sm font-semibold mb-2">로그</h3>
            <LogViewer logs={logs} onClear={() => setLogs([])} height="h-64" />
          </div>
        </>
      ) : (
        <>
          {/* 녹화 중 — 미리보기가 주인공이라 좌측을 넓게 준다 */}
          <div className="grid gap-6 grid-cols-1 lg:grid-cols-[2fr_1fr] items-start">
            <div className="space-y-4">
              {webPreview && <RecordPreview />}
            </div>
            <div className="space-y-4">
              {/* 녹화 중: 상태 + 제어 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
                <h3 className="text-sm font-semibold">녹화 진행</h3>

                {status && (
                  <>
                    <div className="flex justify-between text-xs text-neutral-400">
                      <span>에피소드 {status.current_episode} / {status.total_episodes}</span>
                      <span>{Math.round(status.progress * 100)}%</span>
                    </div>
                    <div className="w-full h-2 bg-neutral-700 rounded overflow-hidden">
                      <div className="h-full bg-blue-500 rounded transition-all" style={{ width: `${status.progress * 100}%` }} />
                    </div>

                    <div className="flex items-center gap-2">
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                        status.phase === 'recording' ? 'bg-red-500/20 text-red-400' :
                        status.phase === 'resetting' ? 'bg-amber-500/20 text-amber-400' :
                        status.phase === 'saving' ? 'bg-blue-500/20 text-blue-400' :
                        'bg-neutral-700 text-neutral-400'
                      }`}>
                        {status.phase === 'recording' && '녹화 중'}
                        {status.phase === 'resetting' && '리셋 대기'}
                        {status.phase === 'saving' && '저장 중'}
                        {status.phase === 'done' && '완료'}
                        {status.phase === 'idle' && '대기'}
                      </span>
                    </div>
                  </>
                )}
              </div>

              {/* 에피소드 제어 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
                <h3 className="text-sm font-semibold">에피소드 제어</h3>

                {/* 리셋 대기 중: 준비 완료 버튼 강조 */}
                {status?.phase === 'resetting' && (
                  <button onClick={handleSkip}
                    className="w-full px-4 py-3 rounded bg-green-600 hover:bg-green-500 text-white text-sm font-semibold animate-pulse">
                    준비 완료 — 다음 에피소드 시작
                  </button>
                )}

                <div className="grid grid-cols-3 gap-2">
                  {/* ⚠ "건너뛰기"가 아니다 — LeRobot 은 이 신호로 루프를 빠져나온 뒤
                      `dataset.save_episode()` 로 떨어진다. **에피소드는 저장된다.**
                      버리는 것은 재녹화뿐이다. 단계에 따라 뜻이 갈려 라벨도 나눈다. */}
                  <button onClick={handleSkip}
                    title={status?.phase === 'resetting'
                      ? '리셋을 마쳤으니 다음 에피소드를 지금 시작한다'
                      : '지정 시간을 다 안 채우고 이번 에피소드를 여기서 마감해 저장한다'}
                    className="px-3 py-2 rounded bg-neutral-700 hover:bg-neutral-600 text-sm text-neutral-200">
                    {status?.phase === 'resetting' ? '준비 완료 →' : '저장하고 다음 →'}
                  </button>
                  <button onClick={handleRerecord}
                    className="px-3 py-2 rounded bg-amber-600 hover:bg-amber-500 text-sm text-white">
                    ← 재녹화
                  </button>
                  <button onClick={handleStop} disabled={stopping}
                    className="px-3 py-2 rounded bg-red-600 hover:bg-red-500 text-sm text-white disabled:opacity-60">
                    {stopping ? '정지 중… (에피소드 저장)' : '정지 (ESC)'}
                  </button>
                </div>
                <p className="text-[10px] text-neutral-500">
                  <b>저장하고 다음</b>: 지정 시간을 다 안 채우고 이번 에피소드를 마감해 <b>저장</b> · <b>재녹화</b>: 이번 에피소드를 <b>폐기</b>하고 다시 · <b>정지</b>: 이번 에피소드까지 저장하고 녹화 종료
                </p>
              </div>

              {/* 녹화 중 Task 메모 (녹화 후 일괄 변경용) */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <h3 className="text-sm font-semibold">Task 변경</h3>
                <div className="text-xs text-neutral-400">
                  현재: <span className="text-neutral-100">{liveTask || singleTask}</span>
                </div>
                <div className="flex gap-1.5">
                  <input type="text" value={taskDraft} onChange={e => setTaskDraft(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') handleTaskChange() }}
                    placeholder="다음 에피소드부터 쓸 task"
                    className="flex-1 px-2 py-1 text-xs rounded bg-neutral-900 border border-neutral-700 text-neutral-100 placeholder:text-neutral-600" />
                  <button onClick={handleTaskChange} disabled={!taskDraft.trim()}
                    className="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-40">적용</button>
                </div>
                {taskMsg && <p className="text-[10px] text-amber-300">{taskMsg}</p>}
                <p className="text-[10px] text-neutral-500">
                  ⚠ <b>다음 에피소드부터</b> 적용됩니다. LeRobot 은 에피소드 시작 시점의 task 를
                  그 에피소드의 모든 프레임에 찍기 때문에, 진행 중인 에피소드는 바뀌지 않습니다.
                  이미 녹화된 에피소드는 데이터셋 페이지에서 고칠 수 있습니다.
                </p>
              </div>
            </div>
          </div>

          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <h3 className="text-sm font-semibold mb-2">로그</h3>
            <LogViewer logs={logs} onClear={() => setLogs([])} />
          </div>
        </>
      )}
    </div>
  )
}
