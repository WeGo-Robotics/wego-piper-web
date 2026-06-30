import { useEffect, useState, useCallback } from 'react'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import LogViewer from '../components/LogViewer'
import RecordPreview from '../components/RecordPreview'

type ProcessState = 'idle' | 'starting' | 'running' | 'stopping' | 'error'
type ReadyArm = { iface: string; role: string }
type ReadyCam = { id: string; name: string }
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

  const { connected } = useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
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
    }, []),
  })

  useEffect(() => {
    api.get<ReadyArm[]>('/robots/ready').then((all) => {
      setFollowers(all.filter(a => a.role === 'follower'))
      setLeaders(all.filter(a => a.role === 'leader'))
    }).catch(() => {})
    api.get<ReadyCam[]>('/cameras/ready').then(setCameras).catch(() => {})
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
  const buildCameraConfig = () => {
    const camJson: Record<string, unknown> = {}
    for (const [name, id] of Object.entries(cameraMapping)) {
      if (!id) continue  // 빈 매핑 스킵
      if (id.startsWith('rs:')) {
        // RealSense: 'rs:<serial>:<stream>' → intelrealsense 설정. OpenCV V4L2
        // 백엔드는 rs: 경로를 못 열기 때문에 별도 카메라 타입으로 빌드해야 한다.
        const serial = id.split(':')[1]
        camJson[name] = { type: 'intelrealsense', serial_number_or_name: serial, width: camWidth, height: camHeight, fps }
      } else {
        camJson[name] = { type: 'opencv', index_or_path: id, width: camWidth, height: camHeight, fps, backend: 200 }
      }
    }
    return camJson
  }

  // CLI 미리보기
  useEffect(() => {
    if (cliEdited || !repoId) return
    api.post<{ command: string }>('/recording/preview', {
      robot_type: 'piper_follower', robot_port: followerPort,
      robot_cameras: buildCameraConfig(),
      teleop_type: 'piper_leader', teleop_port: leaderPort,
      repo_id: repoId, single_task: singleTask,
      num_episodes: numEpisodes, fps, episode_time_s: episodeTime, reset_time_s: resetTime,
      streaming_encoding: streamingEncoding, vcodec, encoder_threads: encoderThreads, encoder_queue_maxsize: encoderQueue, push_to_hub: pushToHub, resume,
    }).then(r => setCliArgs(r.command)).catch(() => {})
  }, [followerPort, leaderPort, cameraMapping, repoId, singleTask, numEpisodes, fps, episodeTime, resetTime, streamingEncoding, vcodec, pushToHub, resume, cliEdited])

  const canStart = !!followerPort && !!leaderPort && !!repoId && !!singleTask && !isRunning

  const handleStart = async () => {
    try {
      if (cliEdited) {
        // custom 미지원 — preview에서 빌드된 args 사용
      }
      await api.post('/recording/start', {
        robot_type: 'piper_follower', robot_port: followerPort,
        robot_cameras: buildCameraConfig(),
        teleop_type: 'piper_leader', teleop_port: leaderPort,
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

  const handleStop = () => api.post('/recording/stop')
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

      <div className={`grid gap-6 ${isRunning ? 'grid-cols-1 lg:grid-cols-[1fr_2fr]' : 'grid-cols-1 lg:grid-cols-[2fr_1fr]'}`}>
        <div className="space-y-4">
          {!isRunning ? (
            <>
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
                            return <option key={c.id} value={c.id} disabled={!!usedBy}>{c.id} ({c.name}){usedBy ? ` — ${usedBy[0]}` : ''}</option>
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
                    className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
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

              {/* CLI 미리보기 */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold">CLI 명령어</h3>
                  {cliEdited && <button onClick={() => setCliEdited(false)} className="text-xs text-blue-400 hover:underline">초기화</button>}
                </div>
                <textarea value={cliArgs} onChange={e => { setCliArgs(e.target.value); setCliEdited(true) }}
                  rows={4}
                  className="w-full px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-xs font-mono text-neutral-100 resize-y" />
                <button onClick={handleStart} disabled={!canStart}
                  className="w-full px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium">
                  녹화 시작
                </button>
              </div>
            </>
          ) : (
            <>
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
                  <button onClick={handleSkip}
                    className="px-3 py-2 rounded bg-neutral-700 hover:bg-neutral-600 text-sm text-neutral-200">
                    건너뛰기 →
                  </button>
                  <button onClick={handleRerecord}
                    className="px-3 py-2 rounded bg-amber-600 hover:bg-amber-500 text-sm text-white">
                    ← 재녹화
                  </button>
                  <button onClick={handleStop}
                    className="px-3 py-2 rounded bg-red-600 hover:bg-red-500 text-sm text-white">
                    정지 (ESC)
                  </button>
                </div>
                <p className="text-[10px] text-neutral-500">
                  건너뛰기: 현재 에피소드 건너뛰고 다음으로 | 재녹화: 현재 에피소드 폐기 후 다시 | 정지: 녹화 종료
                </p>
              </div>

              {/* 녹화 중 Task 메모 (녹화 후 일괄 변경용) */}
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
                <h3 className="text-sm font-semibold">에피소드별 Task 메모</h3>
                <p className="text-[10px] text-neutral-500">녹화 완료 후 데이터셋 페이지에서 에피소드별 task를 변경할 수 있습니다.</p>
                <div className="text-xs text-neutral-400">
                  현재 Task: <span className="text-neutral-100">{singleTask}</span>
                </div>
              </div>
            </>
          )}
        </div>

        {/* 우측: 카메라 미리보기 + 로그 */}
        <div className="space-y-4">
          {isRunning && webPreview && <RecordPreview />}
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <h3 className="text-sm font-semibold mb-2">로그</h3>
            <LogViewer logs={logs} onClear={() => setLogs([])} />
          </div>
        </div>
      </div>
    </div>
  )
}
