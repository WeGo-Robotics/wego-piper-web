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

export default function CamerasPage() {
  const [loading, setLoading] = useState(true)
  const [cams, setCams] = useState<CamInfo[]>([])
  const [scanning, setScanning] = useState(false)
  const [connectingId, setConnectingId] = useState<string | null>(null)
  const [expandedCam, setExpandedCam] = useState<string | null>(null)
  const [previewTs, setPreviewTs] = useState(0)

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

  // 프리뷰 자동 갱신 (등록된 = 백그라운드 캡처 중인 카메라만)
  useEffect(() => {
    if (!cams.some((c) => c.ready)) return
    const interval = setInterval(() => setPreviewTs(Date.now()), 1000)
    return () => clearInterval(interval)
  }, [cams])

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
                      <button onClick={() => setExpandedCam(isExpanded ? null : cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300">설정</button>
                      <button onClick={() => handleRegister(cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white">등록</button>
                      <button onClick={() => handleDisconnect(cam.id)}
                        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">해제</button>
                    </div>
                  </div>
                  {isExpanded && (
                    <div className="border-t border-neutral-700 p-3 space-y-2 bg-neutral-900/50">
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
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 등록됨 */}
      <div className="space-y-2">
        <h2 className="text-sm font-semibold text-neutral-400">사용 가능</h2>
        {ready.length === 0 ? (
          <p className="text-xs text-neutral-500">등록된 카메라가 없습니다</p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {ready.map((cam) => (
              <div key={cam.id} className="rounded-lg border border-green-500/30 bg-green-500/5 overflow-hidden hover:border-green-500/60 transition-colors">
                <img
                  src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs}`}
                  alt={cam.name}
                  className="w-full aspect-[4/3] object-cover bg-neutral-900"
                  onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                  onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
                />
                <div className="p-3 space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="text-green-400 text-sm">✓</span>
                    <span className="font-mono text-sm">{cam.id}</span>
                  </div>
                  <div className="text-xs text-neutral-400">
                    {cam.name}
                    {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                    {cam.config.fps && <span className="ml-2">{cam.config.fps}fps</span>}
                  </div>
                  <button onClick={() => handleUnregister(cam.id)}
                    className="w-full mt-1 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white transition-colors">등록해제</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {cams.length === 0 && (
        <p className="text-center text-neutral-500 text-sm py-8">
          "스캔"을 눌러 카메라를 검색하세요
        </p>
      )}
    </div>
  )
}
