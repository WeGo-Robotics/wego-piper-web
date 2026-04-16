import { useEffect, useState } from 'react'
import { api } from '../services/api'

function Spinner({ className = '' }: { className?: string }) {
  return (
    <svg className={`animate-spin h-4 w-4 ${className}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

type CamInfo = {
  id: string; name: string; cam_type: string
  connected: boolean; ready: boolean; has_preview: boolean
  config: { width: number | null; height: number | null; fps: number | null; color_mode: string; rotation: number; fourcc: string | null }
}

type CamControl = {
  cid: number; name: string; label: string; type: number
  min: number; max: number; step: number; default: number; value: number
}

export default function CamerasPage() {
  const [loading, setLoading] = useState(true)
  const [cams, setCams] = useState<CamInfo[]>([])
  const [scanning, setScanning] = useState(false)
  const [connectingId, setConnectingId] = useState<string | null>(null)
  const [expandedCam, setExpandedCam] = useState<string | null>(null)
  const [previewTs, setPreviewTs] = useState(0)
  const [liveIds, setLiveIds] = useState<Set<string>>(new Set())
  const [controlsCam, setControlsCam] = useState<string | null>(null)
  const [controls, setControls] = useState<CamControl[]>([])

  useEffect(() => {
    api.get<{ cameras: CamInfo[] }>('/cameras/current')
      .then((r) => {
        if (r.cameras) {
          setCams(r.cameras)
          if (r.cameras.some((c) => c.connected)) setPreviewTs(Date.now())
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // 프리뷰 자동 갱신 (실시간 보기 중인 카메라)
  useEffect(() => {
    if (liveIds.size === 0) return
    const interval = setInterval(() => setPreviewTs(Date.now()), 200)
    return () => clearInterval(interval)
  }, [liveIds])

  // 1단계: 스캔 (auto_connect=true → 병렬 연결 + 프리뷰)
  const handleScan = async () => {
    setScanning(true)
    try {
      const result = await api.get<CamInfo[]>('/cameras/scan?auto_connect=true')
      setCams(result)
      setPreviewTs(Date.now())
    } catch {}
    setScanning(false)
  }

  const handleDisconnect = async (id: string) => {
    await api.post('/cameras/disconnect', { id })
    setCams((prev) => prev.map((c) => c.id === id ? { ...c, connected: false, ready: false } : c))
  }

  // 설정
  const handleConfig = async (id: string, cfg: Record<string, unknown>) => {
    const updated = await api.post<CamInfo>('/cameras/config', { id, config: cfg })
    setCams((prev) => prev.map((c) => (c.id === id ? updated : c)))
  }

  // 등록 (자동 connect 포함)
  const handleRegister = async (id: string) => {
    setConnectingId(id)
    try {
      const updated = await api.post<CamInfo>('/cameras/register', { id })
      setCams((prev) => prev.map((c) => (c.id === id ? updated : c)))
    } catch { alert('등록 실패') }
    finally { setConnectingId(null) }
  }
  const handleUnregister = async (id: string) => {
    await api.post('/cameras/unregister', { id })
    setCams((prev) => prev.map((c) => (c.id === id ? { ...c, ready: false } : c)))
  }

  const toggleControls = async (camId: string) => {
    if (controlsCam === camId) {
      setControlsCam(null)
      setControls([])
      return
    }
    try {
      const data = await api.get<CamControl[]>(`/cameras/${encodeURIComponent(camId)}/controls`)
      setControls(data)
      setControlsCam(camId)
    } catch { setControls([]); setControlsCam(camId) }
  }

  const handleResetControls = async (camId: string) => {
    try {
      const data = await api.post<CamControl[]>('/cameras/controls/reset', { id: camId })
      setControls(data)
    } catch {}
  }

  const handleControl = async (camId: string, name: string, value: number) => {
    // 즉시 UI 반영
    setControls((prev) => prev.map((c) => c.name === name ? { ...c, value } : c))
    try {
      await api.post('/cameras/control', { id: camId, name, value })
    } catch {}
  }

  const unconnected = cams.filter((c) => !c.connected)
  const connected = cams.filter((c) => c.connected && !c.ready)
  const ready = cams.filter((c) => c.ready)

  if (loading) {
    return <div className="flex items-center justify-center h-64 gap-2 text-neutral-400"><Spinner /> 로딩 중...</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">카메라</h1>
        <button onClick={handleScan} disabled={scanning}
          className="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
          {scanning ? <><Spinner className="inline" /> 스캔 중...</> : '스캔'}
        </button>
      </div>

      {/* 등록됨 (최상단) */}
      {ready.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-green-400">사용 가능 ({ready.length})</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {ready.map((cam) => {
              const isLive = liveIds.has(cam.id)
              return (
                <div key={cam.id} className="rounded-lg border border-green-500/30 bg-green-500/5 overflow-hidden hover:border-green-500/60 transition-colors">
                  <img
                    src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${isLive ? previewTs : previewTs}`}
                    alt={cam.name}
                    className="w-full aspect-[4/3] object-cover bg-neutral-900"
                    onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                    onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
                  />
                  <div className="p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="text-green-400 text-sm">✓</span>
                      <span className="font-mono text-sm">{cam.id}</span>
                    </div>
                    <div className="text-xs text-neutral-400">
                      {cam.name}
                      {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                      {cam.config.fps && <span className="ml-2">{cam.config.fps}fps</span>}
                    </div>
                    <div className="flex gap-1.5">
                      <button onClick={async () => {
                        try {
                          await api.post('/cameras/probe', { id: cam.id })
                          setPreviewTs(Date.now())
                        } catch {}
                      }}
                        className="flex-1 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white transition-colors">
                        업데이트
                      </button>
                      <button onClick={async () => {
                        if (isLive) {
                          setLiveIds((prev) => { const next = new Set(prev); next.delete(cam.id); return next })
                          await api.post('/cameras/disconnect', { id: cam.id })
                          setCams((prev) => prev.map((c) => c.id === cam.id ? { ...c, connected: false } : c))
                        } else {
                          // 연결 후 실시간 보기 시작
                          try {
                            const updated = await api.post<CamInfo>('/cameras/connect', { id: cam.id })
                            setCams((prev) => prev.map((c) => c.id === cam.id ? updated : c))
                            setLiveIds((prev) => new Set(prev).add(cam.id))
                          } catch { alert('카메라 연결 실패') }
                        }
                      }}
                        className={`flex-1 py-1 text-xs rounded transition-colors ${isLive ? 'bg-red-600 hover:bg-red-500 text-white' : 'bg-neutral-700 hover:bg-green-600 text-neutral-300 hover:text-white'}`}>
                        {isLive ? '중단' : '실시간 보기'}
                      </button>
                      <button onClick={() => toggleControls(cam.id)}
                        className={`flex-1 py-1 text-xs rounded transition-colors ${controlsCam === cam.id ? 'bg-yellow-600 text-white' : 'bg-neutral-700 hover:bg-yellow-600 text-neutral-300 hover:text-white'}`}>
                        {controlsCam === cam.id ? '설정 닫기' : '설정'}
                      </button>
                      <button onClick={() => handleUnregister(cam.id)}
                        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white transition-colors">
                        해제
                      </button>
                    </div>
                    {controlsCam === cam.id && (
                      <div className="border-t border-neutral-700 pt-2 mt-2 space-y-1.5">
                        {controls.length > 0 && (
                          <div className="flex justify-end">
                            <button onClick={() => handleResetControls(cam.id)}
                              className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-orange-600 text-neutral-400 hover:text-white transition-colors">
                              초기화
                            </button>
                          </div>
                        )}
                        {controls.length === 0 ? (
                          <p className="text-xs text-neutral-500">지원되는 컨트롤이 없습니다</p>
                        ) : controls.map((ctrl) => (
                          <div key={ctrl.name} className="flex items-center gap-2 text-xs">
                            <span className="text-neutral-400 w-24 shrink-0 truncate" title={ctrl.label}>{ctrl.label}</span>
                            {ctrl.type === 2 ? (
                              <input type="checkbox" checked={ctrl.value !== 0}
                                onChange={(e) => handleControl(cam.id, ctrl.name, e.target.checked ? 1 : 0)}
                                className="accent-blue-500" />
                            ) : ctrl.type === 3 ? (
                              <select value={ctrl.value}
                                onChange={(e) => handleControl(cam.id, ctrl.name, Number(e.target.value))}
                                className="flex-1 px-1 py-0.5 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 text-xs">
                                {Array.from({ length: ctrl.max - ctrl.min + 1 }, (_, i) => ctrl.min + i).map((v) => (
                                  <option key={v} value={v}>{v}</option>
                                ))}
                              </select>
                            ) : (
                              <input type="range" min={ctrl.min} max={ctrl.max} step={ctrl.step || 1}
                                value={ctrl.value}
                                onChange={(e) => handleControl(cam.id, ctrl.name, Number(e.target.value))}
                                className="flex-1 h-1 accent-blue-500" />
                            )}
                            <span className="text-neutral-300 w-12 text-right font-mono">{ctrl.value}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 미연결 카메라 */}
      {unconnected.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-neutral-400">스캔 완료</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {unconnected.map((cam) => (
              <div key={cam.id}
                className="rounded-lg border border-neutral-700 bg-neutral-800 overflow-hidden hover:border-blue-500 hover:bg-blue-500/5 transition-colors group"
              >
                {cam.has_preview ? (
                  <img src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs}`} alt={cam.name}
                    className="w-full aspect-[4/3] object-cover bg-neutral-900" />
                ) : (
                  <div className="w-full aspect-[4/3] bg-neutral-900 flex items-center justify-center text-neutral-600 text-xs">
                    프리뷰 없음
                  </div>
                )}
                <div className="p-3 space-y-2">
                  <div className="flex items-center gap-2">
                    <span className="inline-block w-2 h-2 rounded-full bg-neutral-500" />
                    <span className="font-mono text-sm">{cam.id}</span>
                  </div>
                  <p className="text-xs text-neutral-400">{cam.name}
                    {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                  </p>
                  <button onClick={() => handleRegister(cam.id)} disabled={connectingId === cam.id}
                    className="w-full py-1.5 text-xs rounded bg-neutral-700 group-hover:bg-green-600 text-neutral-300 group-hover:text-white transition-colors disabled:opacity-50 flex items-center justify-center gap-1">
                    {connectingId === cam.id ? <><Spinner /> 등록 중...</> : '등록'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 연결됨 (미등록) */}
      {connected.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-neutral-400">연결됨 (미등록)</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {connected.map((cam) => {
              const isExpanded = expandedCam === cam.id
              return (
                <div key={cam.id} className="rounded-lg border border-blue-500/30 bg-blue-500/5 overflow-hidden hover:border-blue-500/60 transition-colors">
                  {/* 프리뷰 */}
                  <img
                    src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs}`}
                    alt={cam.name}
                    className="w-full aspect-[4/3] object-cover bg-neutral-900"
                    onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                    onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
                  />
                  <div className="p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
                      <span className="font-mono text-sm">{cam.id}</span>
                    </div>
                    <div className="text-xs text-neutral-400">
                      {cam.name}
                      {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                      {cam.config.fps && <span className="ml-2">{cam.config.fps}fps</span>}
                    </div>
                    <div className="flex gap-1.5">
                      <button onClick={() => {
                        setExpandedCam(isExpanded ? null : cam.id)
                        if (!isExpanded) toggleControls(cam.id)
                        else { setControlsCam(null); setControls([]) }
                      }}
                        className={`flex-1 py-1 text-xs rounded transition-colors ${isExpanded ? 'bg-yellow-600 text-white' : 'bg-neutral-700 hover:bg-neutral-600 text-neutral-300'}`}>설정</button>
                      <button onClick={() => handleRegister(cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white">등록</button>
                      <button onClick={() => handleDisconnect(cam.id)}
                        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">해제</button>
                    </div>
                  </div>
                  {isExpanded && (
                    <div className="border-t border-neutral-700 p-3 space-y-3 bg-neutral-900/50">
                      <div className="grid grid-cols-2 gap-2 text-xs">
                        <div className="flex items-center gap-2">
                          <span className="text-neutral-400 w-14">Width</span>
                          <input type="number" value={cam.config.width ?? ''} placeholder="auto"
                            onChange={(e) => handleConfig(cam.id, { width: e.target.value ? Number(e.target.value) : null })}
                            className="flex-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-neutral-400 w-14">Height</span>
                          <input type="number" value={cam.config.height ?? ''} placeholder="auto"
                            onChange={(e) => handleConfig(cam.id, { height: e.target.value ? Number(e.target.value) : null })}
                            className="flex-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-neutral-400 w-14">FPS</span>
                          <input type="number" value={cam.config.fps ?? ''} placeholder="auto"
                            onChange={(e) => handleConfig(cam.id, { fps: e.target.value ? Number(e.target.value) : null })}
                            className="flex-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-neutral-400 w-14">Rot</span>
                          <select value={cam.config.rotation}
                            onChange={(e) => handleConfig(cam.id, { rotation: Number(e.target.value) })}
                            className="flex-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100">
                            <option value={0}>0°</option><option value={90}>90°</option>
                            <option value={180}>180°</option><option value={270}>270°</option>
                          </select>
                        </div>
                      </div>
                      {controlsCam === cam.id && controls.length > 0 && (
                        <div className="border-t border-neutral-700 pt-2 space-y-1.5">
                          <div className="flex items-center justify-between">
                            <span className="text-[10px] text-neutral-500">이미지 조정</span>
                            <button onClick={() => handleResetControls(cam.id)}
                              className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-orange-600 text-neutral-400 hover:text-white transition-colors">
                              초기화
                            </button>
                          </div>
                          {controls.map((ctrl) => (
                            <div key={ctrl.name} className="flex items-center gap-2 text-xs">
                              <span className="text-neutral-400 w-24 shrink-0 truncate" title={ctrl.label}>{ctrl.label}</span>
                              {ctrl.type === 2 ? (
                                <input type="checkbox" checked={ctrl.value !== 0}
                                  onChange={(e) => handleControl(cam.id, ctrl.name, e.target.checked ? 1 : 0)}
                                  className="accent-blue-500" />
                              ) : ctrl.type === 3 ? (
                                <select value={ctrl.value}
                                  onChange={(e) => handleControl(cam.id, ctrl.name, Number(e.target.value))}
                                  className="flex-1 px-1 py-0.5 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 text-xs">
                                  {Array.from({ length: ctrl.max - ctrl.min + 1 }, (_, i) => ctrl.min + i).map((v) => (
                                    <option key={v} value={v}>{v}</option>
                                  ))}
                                </select>
                              ) : (
                                <input type="range" min={ctrl.min} max={ctrl.max} step={ctrl.step || 1}
                                  value={ctrl.value}
                                  onChange={(e) => handleControl(cam.id, ctrl.name, Number(e.target.value))}
                                  className="flex-1 h-1 accent-blue-500" />
                              )}
                              <span className="text-neutral-300 w-12 text-right font-mono">{ctrl.value}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {cams.length === 0 && (
        <p className="text-center text-neutral-500 text-sm py-8">
          "스캔"을 눌러 카메라를 검색하세요
        </p>
      )}
    </div>
  )
}
