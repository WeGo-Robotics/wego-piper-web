import { useEffect, useState, useRef } from 'react'
import { api } from '../services/api'

function Spinner({ className = '' }: { className?: string }) {
  return (
    <svg className={`animate-spin h-4 w-4 ${className}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

type ArmInfo = {
  iface: string; bus_info: string; state: string; connected: boolean
  role: string; ctrl_mode: string; firmware: string; slot: string | null
  ready: boolean; config: Record<string, unknown>
}
type MotionStatus = {
  status: string; remaining: number; max_delta: number; threshold: number; found_iface: string | null
}

export default function RobotsPage() {
  const [loading, setLoading] = useState(true)
  const [arms, setArms] = useState<ArmInfo[]>([])
  const [scanning, setScanning] = useState(false)
  const [connectingIface, setConnectingIface] = useState<string | null>(null)
  const [expandedArm, setExpandedArm] = useState<string | null>(null)
  // 움직임 감지
  const [motionIface, setMotionIface] = useState<string | null>(null)
  const [motionStatus, setMotionStatus] = useState<MotionStatus | null>(null)
  const motionPollRef = useRef<ReturnType<typeof setInterval>>(undefined)
  // 프리셋
  const [presets, setPresets] = useState<string[]>([])
  const [presetName, setPresetName] = useState('')

  // 초기 로드
  useEffect(() => {
    Promise.all([
      api.get<string[]>('/robots/presets').then(setPresets).catch(() => {}),
      api.get<{ arms: ArmInfo[] }>('/robots/current')
        .then((cur) => { if (cur.arms?.length) setArms(cur.arms) })
        .catch(() => {}),
    ]).finally(() => setLoading(false))
  }, [])

  const refreshArms = async () => {
    const cur = await api.get<{ arms: ArmInfo[] }>('/robots/current')
    if (cur.arms) setArms(cur.arms)
  }

  // ── 1단계: 포트 찾기 ──
  const handleScan = async () => {
    setScanning(true)
    try { setArms(await api.get<ArmInfo[]>('/robots/can')) } catch {}
    setScanning(false)
  }

  // ── 2단계: 인식 & 초기화 ──
  const handleConnect = async (iface: string) => {
    setConnectingIface(iface)
    try {
      const updated = await api.post<ArmInfo>('/robots/connect', { iface })
      setArms((prev) => prev.map((a) => (a.iface === iface ? updated : a)))
    } catch { alert('연결 실패') }
    finally { setConnectingIface(null) }
  }
  const handleDisconnect = async (iface: string) => {
    await api.post('/robots/disconnect', { iface })
    setArms((prev) => prev.map((a) =>
      a.iface === iface ? { ...a, connected: false, role: 'unknown', ready: false, config: {} } : a
    ))
  }
  const handleRoleChange = async (iface: string, role: string) => {
    const updated = await api.post<ArmInfo>('/robots/role', { iface, role })
    setArms((prev) => prev.map((a) => (a.iface === iface ? updated : a)))
  }

  // 움직임 감지 (역할 식별용 — 슬롯 대신 iface 기반)
  const handleFindByMotion = async (targetIface: string) => {
    // 임시 슬롯명으로 사용
    const slot = `find_${targetIface}`
    try {
      await api.post('/robots/find-by-motion', { slot })
      setMotionIface(targetIface)
      setMotionStatus({ status: 'detecting', remaining: 30, max_delta: 0, threshold: 45000, found_iface: null })
      motionPollRef.current = setInterval(async () => {
        const st = await api.get<MotionStatus>(`/robots/find-by-motion/status?slot=${slot}`)
        setMotionStatus(st)
        if (st.status !== 'detecting') {
          clearInterval(motionPollRef.current)
          setMotionIface(null)
          await refreshArms()
        }
      }, 300)
    } catch { alert('감지할 미연결 팔이 없습니다') }
  }
  useEffect(() => () => clearInterval(motionPollRef.current), [])

  // ── 3단계: 설정 ──
  const handleArmConfig = async (iface: string, cfg: Record<string, unknown>) => {
    const updated = await api.post<ArmInfo>('/robots/arm-config', { iface, config: cfg })
    setArms((prev) => prev.map((a) => (a.iface === iface ? updated : a)))
  }

  // ── 4단계: 등록 ──
  const handleRegister = async (iface: string) => {
    try {
      const updated = await api.post<ArmInfo>('/robots/register', { iface })
      setArms((prev) => prev.map((a) => (a.iface === iface ? updated : a)))
    } catch { alert('등록 실패: 연결 및 역할 지정을 확인하세요') }
  }
  const handleUnregister = async (iface: string) => {
    await api.post('/robots/unregister', { iface })
    setArms((prev) => prev.map((a) => (a.iface === iface ? { ...a, ready: false } : a)))
  }

  // ── 프리셋 ──
  const handlePresetSave = async () => {
    if (!presetName.trim()) return
    await api.post('/robots/presets/save', { name: presetName.trim() })
    setPresets(await api.get<string[]>('/robots/presets'))
    setPresetName('')
  }
  const handlePresetLoad = async (name: string) => {
    await api.post('/robots/presets/load', { name })
    await refreshArms()
  }
  const handlePresetDelete = async (name: string) => {
    if (!confirm(`"${name}" 프리셋을 삭제하시겠습니까?`)) return
    await api.delete(`/robots/presets/${name}`)
    setPresets(await api.get<string[]>('/robots/presets'))
  }

  // 파생 데이터
  const connectedArms = arms.filter((a) => a.connected && !a.ready)
  const readyArms = arms.filter((a) => a.ready)
  const unconnectedArms = arms.filter((a) => !a.connected)

  if (loading) {
    return <div className="flex items-center justify-center h-64 gap-2 text-neutral-400"><Spinner /> 로딩 중...</div>
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">로봇</h1>

      {/* 프리셋 */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <h2 className="text-sm font-semibold">프리셋</h2>
        <div className="flex gap-2 flex-wrap">
          {presets.length === 0
            ? <span className="text-xs text-neutral-500">저장된 프리셋 없음</span>
            : presets.map((p) => (
                <div key={p} className="flex items-center gap-1">
                  <button onClick={() => handlePresetLoad(p)}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white">{p}</button>
                  <button onClick={() => handlePresetDelete(p)}
                    className="px-1.5 py-1 text-xs rounded hover:bg-red-600 text-neutral-500 hover:text-white">x</button>
                </div>
              ))
          }
        </div>
        <div className="flex gap-2">
          <input type="text" value={presetName} onChange={(e) => setPresetName(e.target.value)} placeholder="프리셋 이름"
            className="flex-1 px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500" />
          <button onClick={handlePresetSave} disabled={!presetName.trim()}
            className="px-4 py-1.5 text-xs rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-50">저장</button>
        </div>
      </div>

      {/* 1단계: 포트 찾기 */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">1단계: 포트 찾기</h2>
          <button onClick={handleScan} disabled={scanning}
            className="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
            {scanning ? <><Spinner className="inline" /> 스캔 중...</> : '스캔'}
          </button>
        </div>
        {unconnectedArms.length === 0 && connectedArms.length === 0 && readyArms.length === 0 ? (
          <p className="text-xs text-neutral-400">"스캔"을 눌러 CAN 포트를 검색하세요</p>
        ) : unconnectedArms.length > 0 ? (
          <div className="space-y-1">
            {unconnectedArms.map((arm) => (
              <div key={arm.iface} className="flex items-center justify-between rounded border border-neutral-700 p-2.5">
                <div className="text-sm">
                  <span className="font-mono">{arm.iface}</span>
                  <span className="text-xs text-neutral-400 ml-2">{arm.bus_info || ''}</span>
                  <span className="text-xs text-neutral-500 ml-2">{arm.state}</span>
                </div>
                <button onClick={() => handleConnect(arm.iface)} disabled={connectingIface === arm.iface}
                  className="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50 flex items-center gap-1">
                  {connectingIface === arm.iface ? <><Spinner /> 연결 중...</> : '연결'}
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs text-green-400">모든 포트가 연결되었습니다</p>
        )}
      </div>

      {/* 2단계: 인식 & 초기화 + 3단계: 설정 + 4단계: 등록 */}
      {connectedArms.length > 0 && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
          <h2 className="text-sm font-semibold">2~4단계: 인식 → 설정 → 등록</h2>
          <div className="space-y-2">
            {connectedArms.map((arm) => {
              const isExpanded = expandedArm === arm.iface
              const isDetecting = motionIface === arm.iface
              const canRegister = arm.connected && arm.role !== 'unknown'

              return (
                <div key={arm.iface} className="rounded border border-blue-500/30 bg-blue-500/5 overflow-hidden">
                  {/* 팔 헤더 */}
                  <div className="p-3 flex items-center justify-between">
                    <div className="space-y-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-mono text-sm">{arm.iface}</span>
                        <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
                        {/* 역할 드롭다운 */}
                        <select value={arm.role} onChange={(e) => handleRoleChange(arm.iface, e.target.value)}
                          className="px-1.5 py-0.5 text-[10px] rounded bg-neutral-900 border border-neutral-600 text-neutral-100">
                          <option value="unknown">미지정</option>
                          <option value="leader">leader</option>
                          <option value="follower">follower</option>
                        </select>
                      </div>
                      <div className="text-xs text-neutral-400 space-x-3">
                        <span>모드: {arm.ctrl_mode}</span>
                        {arm.firmware && <span>FW: {arm.firmware}</span>}
                        <span>bus: {arm.bus_info || '-'}</span>
                      </div>
                    </div>
                    <div className="flex gap-2 shrink-0">
                      {/* 움직임 감지 */}
                      {isDetecting && motionStatus ? (
                        <div className="text-xs text-neutral-400 flex items-center gap-2">
                          <span>{motionStatus.remaining}s</span>
                          <div className="w-16 h-1.5 bg-neutral-700 rounded-full overflow-hidden">
                            <div className="h-full bg-amber-500 rounded-full transition-all"
                              style={{ width: `${Math.min(100, (motionStatus.max_delta / motionStatus.threshold) * 100)}%` }} />
                          </div>
                        </div>
                      ) : (
                        <button onClick={() => handleFindByMotion(arm.iface)} disabled={!!motionIface}
                          className="px-2 py-1 text-xs rounded bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-50"
                          title="팔을 움직여서 식별">찾기</button>
                      )}
                      <button onClick={() => setExpandedArm(isExpanded ? null : arm.iface)}
                        className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300">설정</button>
                      <button onClick={() => handleRegister(arm.iface)} disabled={!canRegister}
                        className="px-3 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-50">등록</button>
                      <button onClick={() => handleDisconnect(arm.iface)}
                        className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">해제</button>
                    </div>
                  </div>

                  {/* 설정 패널 (펼침) */}
                  {isExpanded && (
                    <div className="border-t border-neutral-700 p-3 space-y-3 bg-neutral-900/50">
                      <h4 className="text-xs font-semibold text-neutral-400">
                        {arm.role === 'leader' ? 'Leader 설정' : 'Follower 설정'}
                      </h4>
                      {arm.role === 'follower' || arm.role === 'unknown' ? (
                        <div className="space-y-2">
                          <label className="flex items-center gap-2 text-xs">
                            <input type="checkbox"
                              checked={(arm.config.disable_torque_on_disconnect as boolean) ?? true}
                              onChange={(e) => handleArmConfig(arm.iface, { disable_torque_on_disconnect: e.target.checked })} />
                            연결 해제 시 토크 비활성화
                          </label>
                          <div className="flex items-center gap-2 text-xs">
                            <span className="text-neutral-400 w-40">max_relative_target</span>
                            <input type="number" step="0.1"
                              value={(arm.config.max_relative_target as number) ?? ''}
                              onChange={(e) => handleArmConfig(arm.iface, { max_relative_target: e.target.value ? Number(e.target.value) : null })}
                              placeholder="null (무제한)"
                              className="flex-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
                          </div>
                          <div className="text-xs">
                            <span className="text-neutral-400">cameras (JSON)</span>
                            <textarea value={JSON.stringify(arm.config.cameras ?? {}, null, 2)}
                              onChange={(e) => { try { handleArmConfig(arm.iface, { cameras: JSON.parse(e.target.value) }) } catch {} }}
                              rows={3}
                              className="w-full mt-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 font-mono text-[11px] focus:outline-none focus:border-blue-500" />
                          </div>
                        </div>
                      ) : (
                        <div className="flex items-center gap-2 text-xs">
                          <span className="text-neutral-400 w-40">gripper_open_pos</span>
                          <input type="number" step="1"
                            value={(arm.config.gripper_open_pos as number) ?? 50}
                            onChange={(e) => handleArmConfig(arm.iface, { gripper_open_pos: Number(e.target.value) })}
                            className="flex-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
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

      {/* 사용 가능 로봇 */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <h2 className="text-sm font-semibold">사용 가능 로봇</h2>
        {readyArms.length === 0 ? (
          <p className="text-xs text-neutral-400">등록된 로봇이 없습니다. 위 단계를 완료하세요.</p>
        ) : (
          <div className="space-y-1">
            {readyArms.map((arm) => (
              <div key={arm.iface} className="flex items-center justify-between rounded border border-green-500/30 bg-green-500/5 p-2.5">
                <div className="flex items-center gap-2">
                  <span className="text-green-400 text-sm">✓</span>
                  <span className="font-mono text-sm">{arm.iface}</span>
                  <span className={`px-1.5 py-0.5 text-[10px] rounded ${arm.role === 'leader' ? 'bg-amber-600/30 text-amber-400' : 'bg-blue-600/30 text-blue-400'}`}>
                    {arm.role}
                  </span>
                  <span className="text-xs text-neutral-400">
                    {arm.role === 'leader' ? 'piper_leader' : 'piper_follower'}
                  </span>
                </div>
                <button onClick={() => handleUnregister(arm.iface)}
                  className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">등록해제</button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
