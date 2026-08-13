import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * USB 2.0 경고 배지.
 *
 * 왜 문구를 프론트에서 안 만드나: 임계값(USB3 = 5000Mbps)과 판정을 백엔드가
 * 갖고 있다. 화면이 그걸 다시 적으면 한쪽만 고쳐져 어긋난다 —
 * `camera_manager._usb_warning` 이 정본이다.
 */
function UsbWarning({ reason }: { reason: string }) {
  return (
    <span
      title={reason}
      className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-500"
    >
      USB 2.0
    </span>
  )
}

function Spinner({ className = '' }: { className?: string }) {
  return (
    <svg className={`animate-spin h-4 w-4 ${className}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

type CamInfo = {
  id: string; name: string; usb_port?: string; cam_type: string
  /** 백엔드가 판정해서 준다 — 화면이 임계값을 따로 적으면 한쪽만 고쳐져 어긋난다. */
  usb_warning?: string | null
  /** 사람이 붙인 별칭("탑뷰"). LeRobot 카메라 키와는 별개 — 화면 표시용이다. */
  label?: string
  /** `label || name`. 각 화면이 따로 계산하지 않도록 백엔드가 준다. */
  display_name?: string
  connected: boolean; ready: boolean; has_preview: boolean
  config: { width: number | null; height: number | null; fps: number | null; color_mode: string; rotation: number; fourcc: string | null }
}

type CamControl = {
  cid: number; name: string; label: string; type: number
  min: number; max: number; step: number; default: number; value: number
  inactive?: boolean; readonly?: boolean
}

export default function CamerasPage() {
  const [loading, setLoading] = useState(true)
  const [cams, setCams] = useState<CamInfo[]>([])
  const [scanning, setScanning] = useState(false)
  const [connectingId, setConnectingId] = useState<string | null>(null)
  // 미등록 카드에서 입력 중인 별칭 (등록 버튼을 누를 때 함께 보낸다)
  const [labelDraft, setLabelDraft] = useState<Record<string, string>>({})
  // 설정은 **모달로 띄운다.** 카드 안에서 펼치면 그리드 행 높이가 늘어나
  // 같은 줄의 다른 카드까지 함께 커진다.
  const [settingsCam, setSettingsCam] = useState<string | null>(null)
  // 프리뷰 캐시버스팅 타임스탬프 — 카메라별로 따로 관리해야 한 카메라 동작이
  // 다른 타일까지 새로고침시키지 않는다.
  const [previewTs, setPreviewTs] = useState<Record<string, number>>({})
  const [liveIds, setLiveIds] = useState<Set<string>>(new Set())
  const [controlsCam, setControlsCam] = useState<string | null>(null)
  const [controls, setControls] = useState<CamControl[]>([])
  // 디바이스 작업(초기화/업데이트)이 진행 중인 카메라 — 같은 디바이스에 동시 요청을
  // 막는다 (RealSense 등에서 컨트롤 질의와 스트림 probe 가 충돌하면 멈춤).
  const [busyId, setBusyId] = useState<string | null>(null)

  // 지정한 카메라들의 프리뷰만 새로고침 (캐시버스팅 타임스탬프 갱신)
  const bumpPreview = (ids: string[]) => {
    const now = Date.now()
    setPreviewTs((prev) => {
      const next = { ...prev }
      for (const id of ids) next[id] = now
      return next
    })
  }

  useEffect(() => {
    api.get<{ cameras: CamInfo[] }>('/cameras/current')
      .then((r) => {
        if (r.cameras) {
          setCams(r.cameras)
          bumpPreview(r.cameras.filter((c) => c.connected).map((c) => c.id))
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // 프리뷰 자동 갱신 (실시간 보기 중인 카메라)
  useEffect(() => {
    if (liveIds.size === 0) return
    const interval = setInterval(() => bumpPreview([...liveIds]), 200)
    return () => clearInterval(interval)
  }, [liveIds])

  // 모달은 Esc 로도 닫힌다
  useEffect(() => {
    if (!settingsCam) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeSettings() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [settingsCam])

  // 설정 모달이 열려 있는 동안 프리뷰를 갱신한다 — 밝기·노출·회전을 만지면서
  // 결과를 못 보면 설정 자체가 의미가 없다.
  //
  // `liveIds` 에 넣지 않는 이유: 그건 카드의 "실시간 보기/중단" 버튼 상태를 겸해서,
  // 모달을 열었다고 카드가 "중단"으로 바뀌면 사용자가 헷갈린다.
  useEffect(() => {
    if (!settingsCam) return
    const id = setInterval(() => bumpPreview([settingsCam]), 200)
    return () => clearInterval(id)
  }, [settingsCam])

  // 1단계: 스캔 (auto_connect=true → 병렬 연결 + 프리뷰)
  const handleScan = async () => {
    setScanning(true)
    try {
      const result = await api.get<CamInfo[]>('/cameras/scan?auto_connect=true')
      setCams(result)
      bumpPreview(result.map((c) => c.id))
    } catch {}
    setScanning(false)
  }

  const handleDisconnect = async (id: string) => {
    await api.post('/cameras/disconnect', { id })
    // 연결 해제는 `connected` 만 내린다 — 등록(`ready`)은 백엔드가 유지하므로
    // 여기서 같이 내리면 새로고침 때 값이 되살아나 화면이 어긋난다.
    setCams((prev) => prev.map((c) => c.id === id ? { ...c, connected: false } : c))
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
      const label = (labelDraft[id] ?? '').trim()
      const updated = await api.post<CamInfo>('/cameras/register', { id, label })
      setCams((prev) => prev.map((c) => (c.id === id ? updated : c)))
    } catch { alert('등록 실패') }
    finally { setConnectingId(null) }
  }

  // 등록 후에도 별칭을 고칠 수 있다 — 카메라를 옮겨 달면 탑뷰가 손목이 된다.
  const handleLabel = async (id: string, label: string) => {
    try {
      const updated = await api.post<CamInfo>('/cameras/label', { id, label })
      setCams((prev) => prev.map((c) => (c.id === id ? updated : c)))
    } catch { /* 표시 이름일 뿐이라 실패해도 조용히 둔다 */ }
  }
  const handleUnregister = async (id: string) => {
    await api.post('/cameras/unregister', { id })
    setCams((prev) => prev.map((c) => (c.id === id ? { ...c, ready: false } : c)))
  }

  // 설정 모달 열기 — 컨트롤 목록은 열 때 한 번 읽는다.
  // RealSense 는 스트리밍 중 컨트롤 질의가 충돌하면 멈출 수 있어(D405 UVC hang)
  // 필요할 때만 묻는다.
  const openSettings = async (camId: string) => {
    setSettingsCam(camId)
    setControls([])
    setControlsCam(camId)
    try {
      setControls(await api.get<CamControl[]>(`/cameras/${encodeURIComponent(camId)}/controls`))
    } catch { setControls([]) }
  }

  const closeSettings = () => {
    setSettingsCam(null)
    setControlsCam(null)
    setControls([])
  }

  const handleResetControls = async (camId: string) => {
    if (busyId) return
    setBusyId(camId)
    try {
      const data = await api.post<CamControl[]>('/cameras/controls/reset', { id: camId })
      setControls(data)
    } catch {}
    finally { setBusyId(null) }
  }

  // RealSense 하드웨어 리셋 (펌웨어 파워사이클) → 재열거 대기 후 재스캔
  const handleResetDevice = async (camId: string) => {
    if (busyId) return
    if (!confirm('카메라를 하드웨어 리셋합니다.\n수 초간 사라졌다 다시 나타납니다. 계속할까요?')) return
    setBusyId(camId)
    // 실시간 보기/컨트롤 패널 정리 (리셋 중 프리뷰는 무의미)
    setLiveIds((prev) => { const next = new Set(prev); next.delete(camId); return next })
    if (controlsCam === camId) { setControlsCam(null); setControls([]) }
    try {
      await api.post('/cameras/reset-device', { id: camId })
      // 리셋 후 USB 재열거까지 대기 → 재스캔으로 엔트리 갱신
      await new Promise((r) => setTimeout(r, 4000))
      const result = await api.get<CamInfo[]>('/cameras/scan?auto_connect=true')
      setCams(result)
      bumpPreview(result.map((c) => c.id))
    } catch (e) {
      alert('하드웨어 리셋 실패: ' + (e instanceof Error ? e.message : '알 수 없는 오류'))
    } finally {
      setBusyId(null)
    }
  }

  // RealSense 카메라에만 노출되는 하드웨어 리셋 버튼
  const ResetDeviceButton = ({ cam }: { cam: CamInfo }) =>
    cam.cam_type === 'realsense' ? (
      <button onClick={() => handleResetDevice(cam.id)} disabled={busyId === cam.id}
        title="하드웨어 리셋 (펌웨어 파워사이클)"
        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-orange-600 text-neutral-300 hover:text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
        {busyId === cam.id ? <Spinner className="inline" /> : '리셋'}
      </button>
    ) : null

  const handleProbe = async (camId: string) => {
    if (busyId) return
    setBusyId(camId)
    try {
      await api.post('/cameras/probe', { id: camId })
      bumpPreview([camId])
    } catch {}
    finally { setBusyId(null) }
  }

  const handleControl = async (camId: string, name: string, value: number) => {
    setControls((prev) => prev.map((c) => c.name === name ? { ...c, value } : c))
    try {
      await api.post('/cameras/control', { id: camId, name, value })
      // auto 계열 변경 시 다른 컨트롤의 inactive 상태가 바뀔 수 있음 → 다시 조회
      if (name.startsWith('auto') || name.includes('automatic') || name.includes('white_balance_automatic')) {
        const data = await api.get<CamControl[]>(`/cameras/${encodeURIComponent(camId)}/controls`)
        setControls(data)
      }
    } catch {}
  }

  // ⚠ 세 그룹은 **배타적이어야 한다.** 등록된 카메라는 프리뷰를 끄면
  // `ready && !connected` 가 되는데(정상 상태 — 등록은 유지, 장치만 놓아준다),
  // 예전 `unconnected` 는 `ready` 를 안 봐서 그 카메라가 "사용 가능"과
  // "미등록"에 **동시에** 떴다. 6대인데 5+2=7 로 보이던 원인.
  // 모달이 최신 카드 데이터를 보게 id 로 찾는다 — 스냅샷을 들고 있으면
  // 설정을 바꿔도 모달 안 값이 안 갱신된다.
  const settingsCamera = settingsCam ? cams.find((c) => c.id === settingsCam) ?? null : null

  const ready = cams.filter((c) => c.ready)
  const connected = cams.filter((c) => c.connected && !c.ready)
  const unconnected = cams.filter((c) => !c.connected && !c.ready)

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
                    src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs[cam.id] ?? 0}`}
                    alt={cam.name}
                    className="w-full aspect-[4/3] object-cover bg-neutral-900"
                    onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                    onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
                  />
                  <div className="p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="text-green-400 text-sm">✓</span>
                      {/* 별칭이 주인공 — 카메라를 옮겨 달면 여기서 바로 고친다.
                          blur 에 저장해 타이핑 중 요청이 쏟아지지 않게 한다. */}
                      <input
                        type="text"
                        defaultValue={cam.label ?? ''}
                        key={cam.label ?? ''}
                        onBlur={(e) => {
                          const v = e.target.value.trim()
                          if (v !== (cam.label ?? '')) handleLabel(cam.id, v)
                        }}
                        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                        placeholder="이름 없음"
                        title="표시 이름 — 데이터셋 피처 이름은 바뀌지 않습니다"
                        className="flex-1 min-w-0 px-1.5 py-0.5 text-sm rounded bg-transparent border border-transparent hover:border-neutral-700 focus:bg-neutral-900 focus:border-blue-500 text-neutral-100 placeholder:text-neutral-600 focus:outline-none" />
                      {cam.usb_port && <span className="font-mono text-[10px] text-neutral-500" title="USB 포트">{cam.usb_port}</span>}
                      {cam.usb_warning && <UsbWarning reason={cam.usb_warning} />}
                    </div>
                    <div className="text-xs text-neutral-400">
                      <span className="font-mono text-[10px] text-neutral-500">{cam.id}</span>
                      <span className="ml-2">{cam.name}</span>
                      {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                      {cam.config.fps && <span className="ml-2">{cam.config.fps}fps</span>}
                    </div>
                    <div className="flex gap-1.5">
                      <button onClick={() => handleProbe(cam.id)} disabled={busyId === cam.id}
                        className="flex-1 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                        {busyId === cam.id ? <><Spinner className="inline" /> 처리 중</> : '업데이트'}
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
                            bumpPreview([cam.id])
                          } catch { alert('카메라 연결 실패') }
                        }
                      }}
                        className={`flex-1 py-1 text-xs rounded transition-colors ${isLive ? 'bg-red-600 hover:bg-red-500 text-white' : 'bg-neutral-700 hover:bg-green-600 text-neutral-300 hover:text-white'}`}>
                        {isLive ? '중단' : '실시간 보기'}
                      </button>
                      <button onClick={() => openSettings(cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-neutral-700 hover:bg-yellow-600 text-neutral-300 hover:text-white transition-colors">
                        설정
                      </button>
                      <button onClick={() => handleUnregister(cam.id)}
                        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white transition-colors">
                        해제
                      </button>
                      <ResetDeviceButton cam={cam} />
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 미연결 카메라 — 스캔으로 찾기만 했고 아직 열지 않았다 */}
      {unconnected.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-neutral-400">
            미등록 ({unconnected.length})
            <span className="ml-2 font-normal text-neutral-500">
              — 찾기만 한 상태. 등록해야 녹화·추론에서 씁니다
            </span>
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {unconnected.map((cam) => (
              <div key={cam.id}
                className="rounded-lg border border-neutral-700 bg-neutral-800 overflow-hidden hover:border-blue-500 hover:bg-blue-500/5 transition-colors group"
              >
                {cam.has_preview ? (
                  <img src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs[cam.id] ?? 0}`} alt={cam.name}
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
                      {cam.usb_port && <span className="ml-auto font-mono text-[10px] text-neutral-500" title="USB 포트">{cam.usb_port}</span>}
                      {cam.usb_warning && <UsbWarning reason={cam.usb_warning} />}
                  </div>
                  <p className="text-xs text-neutral-400">{cam.name}
                    {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                  </p>
                  {/* 별칭 — 등록할 때 함께 보낸다. 나중에 카드에서 고칠 수도 있다. */}
                  <input
                    type="text"
                    value={labelDraft[cam.id] ?? ''}
                    onChange={(e) => setLabelDraft((prev) => ({ ...prev, [cam.id]: e.target.value }))}
                    onKeyDown={(e) => { if (e.key === 'Enter') handleRegister(cam.id) }}
                    placeholder="이름 (예: 탑뷰, 손목)"
                    className="w-full px-2 py-1 text-xs rounded bg-neutral-900 border border-neutral-700 text-neutral-100 placeholder:text-neutral-600 focus:outline-none focus:border-blue-500" />
                  <div className="flex gap-1.5">
                    <button onClick={() => handleRegister(cam.id)} disabled={connectingId === cam.id}
                      className="flex-1 py-1.5 text-xs rounded bg-neutral-700 group-hover:bg-green-600 text-neutral-300 group-hover:text-white transition-colors disabled:opacity-50 flex items-center justify-center gap-1">
                      {connectingId === cam.id ? <><Spinner /> 등록 중...</> : '등록'}
                    </button>
                    <ResetDeviceButton cam={cam} />
                  </div>
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
              return (
                <div key={cam.id} className="rounded-lg border border-blue-500/30 bg-blue-500/5 overflow-hidden hover:border-blue-500/60 transition-colors">
                  {/* 프리뷰 */}
                  <img
                    src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs[cam.id] ?? 0}`}
                    alt={cam.name}
                    className="w-full aspect-[4/3] object-cover bg-neutral-900"
                    onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                    onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
                  />
                  <div className="p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
                      <span className="font-mono text-sm">{cam.id}</span>
                      {cam.usb_port && <span className="ml-auto font-mono text-[10px] text-neutral-500" title="USB 포트">{cam.usb_port}</span>}
                      {cam.usb_warning && <UsbWarning reason={cam.usb_warning} />}
                    </div>
                    <div className="text-xs text-neutral-400">
                      {cam.name}
                      {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                      {cam.config.fps && <span className="ml-2">{cam.config.fps}fps</span>}
                    </div>
                    <div className="flex gap-1.5">
                      <button onClick={() => openSettings(cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-neutral-700 hover:bg-yellow-600 text-neutral-300 hover:text-white transition-colors">설정</button>
                      <button onClick={() => handleRegister(cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white">등록</button>
                      <button onClick={() => handleDisconnect(cam.id)}
                        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">해제</button>
                      <ResetDeviceButton cam={cam} />
                    </div>
                  </div>
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

      {/* 설정 모달 — 등록·미등록 카드가 같은 창을 연다.
          카드 안에서 펼치면 그리드 행 높이가 늘어나 옆 카드까지 커졌다. */}
      {settingsCamera && (
        <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center" onClick={closeSettings}>
          <div className="bg-neutral-800 rounded-xl border border-neutral-600 p-6 w-[960px] max-w-[95vw] max-h-[90vh] flex flex-col gap-4"
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-lg font-bold truncate">
                  {settingsCamera.label || settingsCamera.name}
                </h2>
                <p className="font-mono text-[11px] text-neutral-500 truncate">{settingsCamera.id}</p>
              </div>
              <button onClick={closeSettings}
                className="px-2 py-1 text-sm rounded text-neutral-400 hover:bg-neutral-700 hover:text-white">✕</button>
            </div>

            {/* 좌: 프리뷰 / 우: 설정. 세로로 쌓으면 슬라이더를 만질 때 화면이
                스크롤 밖으로 밀려 결과를 못 본다 — 붙여 놓는 것이 요점이다.
                좁은 화면(<lg)에서는 자동으로 위아래로 접힌다. */}
            <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px] gap-4 min-h-0 flex-1">
            {/* 프리뷰 — 오른쪽 설정을 만지면 여기서 바로 결과가 보인다 */}
            <div className="relative self-start">
              <img
                src={`/api/cameras/${encodeURIComponent(settingsCamera.id)}/preview?t=${previewTs[settingsCamera.id] ?? 0}`}
                alt={settingsCamera.display_name ?? settingsCamera.name}
                className="w-full max-h-[62vh] aspect-[4/3] object-contain rounded bg-neutral-900"
                onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
              />
              {!settingsCamera.connected && (
                // 연결 안 된 카메라는 프레임이 안 들어온다. 여기서 몰래 열지 않는다 —
                // 장치를 쥐는 것은 사용자가 결정할 일이다.
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/60 rounded">
                  <p className="text-xs text-neutral-300">연결되지 않아 화면이 갱신되지 않습니다</p>
                  <button onClick={async () => {
                    try {
                      const updated = await api.post<CamInfo>('/cameras/connect', { id: settingsCamera.id })
                      setCams((prev) => prev.map((c) => (c.id === updated.id ? updated : c)))
                      bumpPreview([settingsCamera.id])
                    } catch { /* 연결 실패는 카드 쪽 흐름에서 다룬다 */ }
                  }}
                    className="px-3 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white">연결</button>
                </div>
              )}
            </div>

            {/* 오른쪽 열 — 항목이 길어지면 여기만 스크롤한다 */}
            <div className="space-y-4 min-h-0 overflow-y-auto pr-1">
            {/* 이름 — 등록 여부와 무관하게 여기서 고칠 수 있다 */}
            <div className="space-y-1">
              <span className="text-xs text-neutral-400">이름</span>
              <input type="text" defaultValue={settingsCamera.label ?? ''} key={`label-${settingsCamera.id}`}
                onBlur={(e) => {
                  const v = e.target.value.trim()
                  if (v !== (settingsCamera.label ?? '')) handleLabel(settingsCamera.id, v)
                }}
                placeholder="예: 탑뷰, 손목"
                className="w-full px-2 py-1.5 text-sm rounded bg-neutral-900 border border-neutral-700 text-neutral-100 placeholder:text-neutral-600 focus:outline-none focus:border-blue-500" />
              <p className="text-[10px] text-neutral-500">
                화면에서 알아보기 위한 이름입니다. 데이터셋 피처 이름은 바뀌지 않습니다.
              </p>
            </div>

            {/* 해상도·FPS·회전 */}
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="flex items-center gap-2">
                <span className="text-neutral-400 w-14">Width</span>
                <input type="number" value={settingsCamera.config.width ?? ''} placeholder="auto"
                  onChange={(e) => handleConfig(settingsCamera.id, { width: e.target.value ? Number(e.target.value) : null })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-neutral-400 w-14">Height</span>
                <input type="number" value={settingsCamera.config.height ?? ''} placeholder="auto"
                  onChange={(e) => handleConfig(settingsCamera.id, { height: e.target.value ? Number(e.target.value) : null })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-neutral-400 w-14">FPS</span>
                <input type="number" value={settingsCamera.config.fps ?? ''} placeholder="auto"
                  onChange={(e) => handleConfig(settingsCamera.id, { fps: e.target.value ? Number(e.target.value) : null })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-neutral-400 w-14">Rot</span>
                <select value={settingsCamera.config.rotation}
                  onChange={(e) => handleConfig(settingsCamera.id, { rotation: Number(e.target.value) })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100">
                  <option value={0}>0°</option><option value={90}>90°</option>
                  <option value={180}>180°</option><option value={270}>270°</option>
                </select>
              </div>
            </div>

            {/* 이미지 조정 (v4l2 컨트롤) */}
            <div className="border-t border-neutral-700 pt-3 space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-xs text-neutral-400">이미지 조정</span>
                {controls.length > 0 && (
                  <button onClick={() => handleResetControls(settingsCamera.id)} disabled={busyId === settingsCamera.id}
                    className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-orange-600 text-neutral-400 hover:text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                    {busyId === settingsCamera.id ? '처리 중…' : '초기화'}
                  </button>
                )}
              </div>
              {controls.length === 0 ? (
                <p className="text-xs text-neutral-500">지원되는 컨트롤이 없습니다</p>
              ) : controls.map((ctrl) => {
                const locked = ctrl.inactive || ctrl.readonly
                return (
                  <div key={ctrl.name} className={`flex items-center gap-2 text-xs ${locked ? 'opacity-40' : ''}`}>
                    <span className="text-neutral-400 w-28 shrink-0 truncate" title={ctrl.label + (locked ? ' (자동 모드에 의해 잠김)' : '')}>{ctrl.label}</span>
                    {ctrl.type === 2 ? (
                      <input type="checkbox" checked={ctrl.value !== 0} disabled={locked}
                        onChange={(e) => handleControl(settingsCamera.id, ctrl.name, e.target.checked ? 1 : 0)}
                        className="accent-blue-500" />
                    ) : ctrl.type === 3 ? (
                      <select value={ctrl.value} disabled={locked}
                        onChange={(e) => handleControl(settingsCamera.id, ctrl.name, Number(e.target.value))}
                        className="flex-1 px-1 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 text-xs disabled:opacity-50">
                        {Array.from({ length: ctrl.max - ctrl.min + 1 }, (_, i) => ctrl.min + i).map((v) => (
                          <option key={v} value={v}>{v}</option>
                        ))}
                      </select>
                    ) : (
                      <input type="range" min={ctrl.min} max={ctrl.max} step={ctrl.step || 1}
                        value={ctrl.value} disabled={locked}
                        onChange={(e) => handleControl(settingsCamera.id, ctrl.name, Number(e.target.value))}
                        className="flex-1 h-1 accent-blue-500 disabled:opacity-50" />
                    )}
                    <span className="text-neutral-300 w-12 text-right font-mono">{ctrl.value}</span>
                  </div>
                )
              })}
            </div>
            </div>{/* /오른쪽 열 */}
            </div>{/* /좌우 그리드 */}
          </div>
        </div>
      )}
    </div>
  )
}
