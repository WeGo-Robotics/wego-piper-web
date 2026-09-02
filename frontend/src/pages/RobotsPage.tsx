import { useEffect, useState, useRef, useCallback } from 'react'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'
import JogPanel from '../components/JogPanel'
import ZeroCalibrationModal from '../components/ZeroCalibrationModal'
import { JOINT_NAMES } from '../config/joints'

// ── 파킹 보정 모달 ──

function ParkingCalibrationModal({ iface, onClose }: { iface: string; onClose: () => void }) {
  const [step, setStep] = useState<'ready' | 'moving' | 'adjusting' | 'saving'>('ready')
  const [joints, setJoints] = useState<Record<string, number>>({})
  const pollRef = useRef<ReturnType<typeof setInterval>>(undefined)

  const readJoints = useCallback(async () => {
    try {
      const data = await api.get<Record<string, number>>(`/robots/parking/joints/${iface}`)
      setJoints(data)
    } catch { /* ignore */ }
  }, [iface])

  // 관절 위치 폴링
  useEffect(() => {
    readJoints()
    pollRef.current = setInterval(readJoints, 300)
    return () => clearInterval(pollRef.current)
  }, [readJoints])

  const handleGoParking = async () => {
    setStep('moving')
    try {
      // 토크 ON → 파킹 이동
      await api.post('/robots/parking/torque?enable=true', { iface })
      await api.post('/robots/parking/go', { iface })
      // 3초 대기 (이동 완료) → 토크 해제 → 보정 단계
      setTimeout(async () => {
        await api.post('/robots/parking/torque?enable=false', { iface })
        setStep('adjusting')
      }, 3000)
    } catch { setStep('ready') }
  }

  const handleSave = async () => {
    setStep('saving')
    // 현재 위치 읽고 저장
    await readJoints()
    await api.post('/robots/parking/save', { iface, positions: joints })
    // 토크 복원
    await api.post('/robots/parking/torque?enable=true', { iface })
    onClose()
  }

  const handleCancel = async () => {
    // 토크 복원 후 닫기
    try { await api.post('/robots/parking/torque?enable=true', { iface }) } catch { /* ignore */ }
    onClose()
  }

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center" onClick={handleCancel}>
      <div className="bg-neutral-800 rounded-xl border border-neutral-600 p-6 w-[480px] max-h-[90vh] overflow-y-auto space-y-4"
        onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-bold">파킹 위치 보정 — {iface}</h2>

        {/* 단계 표시 */}
        <div className="flex gap-2 text-xs">
          {(['ready', 'moving', 'adjusting', 'saving'] as const).map((s, i) => (
            <div key={s} className={`flex items-center gap-1 ${step === s ? 'text-blue-400 font-medium' : 'text-neutral-500'}`}>
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] ${step === s ? 'bg-blue-600 text-white' : 'bg-neutral-700'}`}>
                {i + 1}
              </span>
              {s === 'ready' && '준비'}
              {s === 'moving' && '이동 + 토크 해제'}
              {s === 'adjusting' && '보정'}
              {s === 'saving' && '저장'}
            </div>
          ))}
        </div>

        {/* 관절 위치 표시 */}
        <div className="rounded-lg border border-neutral-700 bg-neutral-900 p-3 space-y-2">
          <h3 className="text-xs font-semibold text-neutral-400">현재 관절 위치</h3>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            {JOINT_NAMES.map((name) => (
              <div key={name} className="flex items-center justify-between text-xs">
                <span className="text-neutral-400 font-mono">{name}</span>
                <span className="text-neutral-100 font-mono tabular-nums">
                  {joints[name] !== undefined ? joints[name].toFixed(1) : '—'}
                </span>
              </div>
            ))}
          </div>
          {/* 바 차트 */}
          <div className="space-y-1 pt-1">
            {JOINT_NAMES.map((name) => {
              const val = joints[name] ?? 0
              const isGripper = name === 'gripper'
              const pct = isGripper ? val : (val + 100) / 2  // [-100,100] → [0,100]
              return (
                <div key={name} className="flex items-center gap-2 text-[10px]">
                  <span className="w-12 text-neutral-500 font-mono">{name.replace('joint', 'J')}</span>
                  <div className="flex-1 h-2 bg-neutral-700 rounded overflow-hidden relative">
                    {!isGripper && <div className="absolute left-1/2 w-px h-full bg-neutral-500" />}
                    <div className={`h-full rounded ${isGripper ? 'bg-green-500' : val >= 0 ? 'bg-blue-500' : 'bg-orange-500'}`}
                      style={{
                        width: `${Math.abs(isGripper ? pct : (val / 100) * 50)}%`,
                        marginLeft: isGripper ? 0 : val >= 0 ? '50%' : `${50 - Math.abs(val / 2)}%`,
                      }} />
                  </div>
                  <span className="w-12 text-right text-neutral-400 tabular-nums">{val.toFixed(1)}</span>
                </div>
              )
            })}
          </div>
        </div>

        {/* 액션 버튼 */}
        <div className="flex gap-2">
          {step === 'ready' && (
            <button onClick={handleGoParking}
              className="flex-1 px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium">
              1. 파킹 위치로 이동
            </button>
          )}
          {step === 'moving' && (
            <button disabled className="flex-1 px-4 py-2 rounded bg-neutral-700 text-neutral-400 text-sm">
              이동 중...
            </button>
          )}
          {step === 'adjusting' && (
            <button onClick={handleSave}
              className="flex-1 px-4 py-2 rounded bg-green-600 hover:bg-green-500 text-white text-sm font-medium">
              2. 현재 위치 저장
            </button>
          )}
          {step === 'saving' && (
            <button disabled className="flex-1 px-4 py-2 rounded bg-neutral-700 text-neutral-400 text-sm">
              저장 중...
            </button>
          )}
          <button onClick={handleCancel}
            className="px-4 py-2 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 text-sm">
            취소
          </button>
        </div>

        <p className="text-[10px] text-neutral-500">
          파킹 위치로 이동 후 토크가 자동 해제됩니다. 관절을 원하는 위치로 직접 조정한 후 저장하세요.
        </p>
      </div>
    </div>
  )
}

// ── USB 진단/복구 모달 ──
type UsbInfo = { flat: string; tree: string; controllers: string[] }

function UsbInfoModal({ onClose }: { onClose: () => void }) {
  // 리바인딩은 되돌릴 수 없고 USB 가 통째로 끊긴다 — 물어보고 한다.
  // `window.confirm` 은 heartbeat 를 막으므로 쓰지 않는다.
  const { confirm: askConfirm } = useSystemMessage()
  const [info, setInfo] = useState<UsbInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [recovering, setRecovering] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try { setInfo(await api.get<UsbInfo>('/robots/usb/info')) }
    catch (e) { setMsg(`정보 조회 실패: ${(e as Error).message}`) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const handleRecover = async () => {
    if (!await askConfirm('xHCI USB 컨트롤러를 재바인딩합니다.\n잠깐 동안 키보드/마우스 등 USB 장치가 모두 끊겼다 다시 연결됩니다.\n계속할까요?')) return
    setRecovering(true)
    setMsg(null)
    try {
      const r = await api.post<{ ok: boolean; message: string; rebound: string[]; usb: UsbInfo }>(
        '/robots/usb/recover', {})
      setInfo(r.usb)
      setMsg(r.ok
        ? `복구 완료 — 재바인딩: ${r.rebound.join(', ') || '없음'}`
        : `일부 실패: ${r.message}`)
    } catch (e) { setMsg(`복구 실패: ${(e as Error).message}`) }
    finally { setRecovering(false) }
  }

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center" onClick={onClose}>
      <div className="bg-neutral-800 rounded-xl border border-neutral-600 p-6 w-[720px] max-w-[95vw] max-h-[90vh] overflow-y-auto space-y-4"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold">USB 진단 / 복구</h2>
          <div className="flex gap-2">
            <button onClick={load} disabled={loading || recovering}
              className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 disabled:opacity-50">
              새로고침
            </button>
            <button onClick={handleRecover} disabled={recovering}
              className="px-3 py-1 text-xs rounded bg-red-600 hover:bg-red-500 text-white disabled:opacity-50 flex items-center gap-1">
              {recovering ? <><Spinner /> 복구 중...</> : 'USB 컨트롤러 복구'}
            </button>
          </div>
        </div>

        {info?.controllers && info.controllers.length > 0 && (
          <div className="text-xs text-neutral-400">
            xHCI 컨트롤러: <span className="font-mono text-neutral-200">{info.controllers.join(', ')}</span>
          </div>
        )}

        {msg && (
          <div className="rounded border border-neutral-700 bg-neutral-900 p-2 text-xs text-amber-300 whitespace-pre-wrap">{msg}</div>
        )}

        {loading ? (
          <div className="flex items-center gap-2 text-neutral-400 text-sm py-6"><Spinner /> 불러오는 중...</div>
        ) : (
          <div className="space-y-3">
            <div>
              <h3 className="text-xs font-semibold text-neutral-400 mb-1">lsusb -t (트리)</h3>
              <pre className="rounded-lg border border-neutral-700 bg-neutral-900 p-3 text-[11px] font-mono text-neutral-200 overflow-x-auto whitespace-pre">
{info?.tree || '(없음)'}
              </pre>
            </div>
            <div>
              <h3 className="text-xs font-semibold text-neutral-400 mb-1">lsusb (목록)</h3>
              <pre className="rounded-lg border border-neutral-700 bg-neutral-900 p-3 text-[11px] font-mono text-neutral-200 overflow-x-auto whitespace-pre">
{info?.flat || '(없음)'}
              </pre>
            </div>
          </div>
        )}

        <p className="text-[10px] text-neutral-500">
          'HC died'로 USB 트리가 사라졌을 때 컨트롤러 복구를 누르면 재부팅 없이 재열거됩니다. 복구 후 "스캔"으로 CAN 포트를 다시 확인하세요.
        </p>
      </div>
    </div>
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

type ArmInfo = {
  iface: string; bus_info: string; state: string; connected: boolean
  role: string; ctrl_mode: string; master_slave?: 'master' | 'slave' | null
  /** 역할과 팔 모드가 어긋난 사유. **판정은 백엔드가 한다.** */
  mode_mismatch?: string | null
  firmware: string; slot: string | null; side?: 'left' | 'right' | null
  ready: boolean; config: Record<string, unknown>; rx_packets?: number
}

// 하드웨어 마스터(示教输入)/슬레이브(运动输出) 모드 뱃지
function MasterSlaveBadge({ ms }: { ms?: 'master' | 'slave' | null }) {
  if (!ms) return null
  const isMaster = ms === 'master'
  return (
    <span className={`px-1.5 py-0.5 text-[10px] rounded font-medium ${
      isMaster ? 'bg-purple-600/30 text-purple-300' : 'bg-cyan-600/30 text-cyan-300'}`}
      title={isMaster ? '마스터 모드 (示教入力/직접 조작)' : '슬레이브 모드 (運動出力/제어 대상)'}>
      {isMaster ? '마스터' : '슬레이브'}
    </span>
  )
}
type ProbeResult = {
  ok: boolean; role?: 'master' | 'slave'; moved_raw?: number
  commanded_raw?: number; error?: string
}
type MotionStatus = {
  status: string
  /** 지금 무엇을 하고 있나. 몇 초씩 조용한 절차라 이게 없으면 멈춘 것처럼 보인다. */
  phase?: string
  /** 이 단계가 끝나기까지 남은 초. 0 이면 안 보여준다. */
  remaining: number
  iface?: string
  index?: number
  total?: number
  results?: Record<string, ProbeResult>
  // 예전 손 감지 방식이 쓰던 것들 — 아직 엔드포인트가 남아 있다
  max_delta?: number; threshold?: number; found_iface?: string | null
}

export default function RobotsPage() {
  // ⚠ `window.alert` 를 쓰지 않는다 — 이벤트 루프를 막아 E-stop heartbeat 가
  //   끊기고, 2초 타임아웃에 추론이 강제 종료된다 (confirm 으로 실제로 겪었다).
  const { notify, confirm: askConfirm } = useSystemMessage()
  const notifyError = (text: string) =>
    notify({ level: 'error', text, source: '로봇' })
  const [loading, setLoading] = useState(true)
  const [arms, setArms] = useState<ArmInfo[]>([])
  const [scanning, setScanning] = useState(false)
  const [connectingIface, setConnectingIface] = useState<string | null>(null)
  const [expandedArm, setExpandedArm] = useState<string | null>(null)
  // 움직임 감지
  const [motionIface, setMotionIface] = useState<string | null>(null)
  const [motionStatus, setMotionStatus] = useState<MotionStatus | null>(null)
  const motionPollRef = useRef<ReturnType<typeof setInterval>>(undefined)
  const [zeroIface, setZeroIface] = useState<string | null>(null)
  const [parkingIface, setParkingIface] = useState<string | null>(null)
  // 조작(조그)은 **모달**로 띄운다. 행 아래로 펼치면 긴 목록에서 다른 팔 행이
  // 밀려 내려가고, 팔을 실제로 움직이는 조작은 화면이 그 팔 하나에 고정되는 게
  // 맞다 — 영점·파킹 모달과 같은 판단이다.
  const [jogIface, setJogIface] = useState<string | null>(null)
  const [usbModalOpen, setUsbModalOpen] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  // CAN 관리
  const [renamingIface, setRenamingIface] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [canActive, setCanActive] = useState<Record<string, boolean | 'checking'>>({})  // iface → active
  // 프리셋
  // 저장 실패 사유를 화면에 띄운다 — 예전에는 빈 프리셋이 조용히 저장돼
  // "저장은 됐는데 불러오면 아무 일도 없다" 가 됐다.
  const [presetMsg, setPresetMsg] = useState<string | null>(null)
  // 프리셋 로드는 스캔 + 팔마다 CAN 열기라 수 초 걸린다
  const [presetBusy, setPresetBusy] = useState(false)
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

  // 연결된 팔의 상태(마스터/슬레이브·ctrl_mode)를 라이브 재읽기
  const handleRefresh = async () => {
    setRefreshing(true)
    try { await refreshArms() } catch {}
    finally { setRefreshing(false) }
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
    } catch { notifyError('연결 실패') }
    finally { setConnectingIface(null) }
  }
  const handleCheckActive = async (iface: string) => {
    setCanActive((prev) => ({ ...prev, [iface]: 'checking' }))
    try {
      const r = await api.get<{ active: boolean }>(`/robots/can/check/${encodeURIComponent(iface)}`)
      setCanActive((prev) => ({ ...prev, [iface]: r.active }))
    } catch {
      setCanActive((prev) => ({ ...prev, [iface]: false }))
    }
  }

  // CAN UP 후 자동으로 활성 체크
  const handleCanUp = async (iface: string) => {
    try {
      await api.post('/robots/can/up', { iface })
      setArms((prev) => prev.map((a) => a.iface === iface ? { ...a, state: 'UP' } : a))
      handleCheckActive(iface)
    } catch { notifyError('CAN UP 실패') }
  }

  const handleRename = async (oldName: string) => {
    if (!renameValue.trim() || renameValue === oldName) {
      setRenamingIface(null)
      return
    }
    try {
      await api.post('/robots/can/rename', { old_name: oldName, new_name: renameValue.trim() })
      setArms((prev) => prev.map((a) => a.iface === oldName ? { ...a, iface: renameValue.trim() } : a))
      setRenamingIface(null)
    } catch { notifyError('이름 변경 실패') }
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

  // 좌/우 스왑 — 등록에 박제된다 (세션 사이에 뒤바뀌면 양팔 데이터셋이 거울상 오염)
  const handleSideToggle = async (arm: ArmInfo) => {
    const next = arm.side === 'left' ? 'right' : arm.side === 'right' ? null : 'left'
    const updated = await api.post<ArmInfo>('/robots/side', { iface: arm.iface, side: next })
    setArms((prev) => prev.map((a) => (a.iface === arm.iface ? updated : a)))
  }

  // 마스터(示教入力)/슬레이브(運動出力) 모드 설정
  const [settingMs, setSettingMs] = useState<string | null>(null)
  const handleSetMasterSlave = async (iface: string, master: boolean) => {
    setSettingMs(iface)
    try {
      const updated = await api.post<ArmInfo>('/robots/master-slave', { iface, master })
      setArms((prev) => prev.map((a) => (a.iface === iface ? updated : a)))
    } catch (e) { notifyError(`설정 실패: ${(e as Error).message}`) }
    finally { setSettingMs(null) }
  }

  /**
   * 마스터/슬레이브를 **팔에 직접 물어서** 가린다.
   *
   * 예전에는 사람이 팔을 손으로 움직이면 그걸 감지했다. 이제는 각 팔에 작은 이동
   * 명령을 넣고 반응을 본다 — 마스터는 외부 명령을 무시하고 피드백도 안 보내므로
   * 움직이지도, 관절값이 바뀌지도 않는다.
   *
   * ⚠ **팔이 실제로 움직인다**(손목 4%). 그리고 시작까지 **10초를 기다린다** —
   * 부팅 중인 팔에 CAN 이 도착하면 부팅이 깨지는데, 방금 전원을 넣었는지
   * 화면에서는 알 수 없다.
   */
  const handleIdentify = async (iface: string) => {
    // ⚠ 상태 키는 슬롯이지만 **행을 가리는 값은 iface** 다. 여기에 슬롯 이름을
    //   넣었다가 어느 행과도 안 맞아 진행 표시가 통째로 안 보인 적이 있다.
    const slot = `identify_${iface}`
    const yes = await askConfirm(
      `${iface} 를 움직여 마스터/슬레이브를 가립니다.\n\n` +
      '· 손목(joint6)이 가동범위의 4%만큼 돌았다가 제자리로 돌아옵니다\n' +
      '· 부팅 중인 팔을 깨뜨리지 않으려고 시작까지 10초를 기다립니다\n\n' +
      '이 팔 주변이 비어 있는지 확인하세요.')
    if (!yes) return
    try {
      await api.post('/robots/identify', { slot, iface })
      setMotionIface(iface)
      setMotionStatus({ status: 'waiting', phase: '부팅 중인 팔을 깨뜨리지 않으려고 대기',
                        remaining: 10, results: {} })
      motionPollRef.current = setInterval(async () => {
        const st = await api.get<MotionStatus>(`/robots/find-by-motion/status?slot=${slot}`)
        setMotionStatus(st)
        if (st.status === 'done' || st.status === 'error') {
          clearInterval(motionPollRef.current)
          setMotionIface(null)
          const lines = Object.entries(st.results ?? {}).map(([iface, r]) =>
            r.ok ? `${iface}: ${r.role === 'master' ? '마스터' : '슬레이브'}` +
                   ` (${r.moved_raw}/${r.commanded_raw} raw 이동)`
                 : `${iface}: 판별 실패 — ${r.error}`)
          notify({ level: 'info', source: '로봇',
            text: lines.join('\n') || '판별할 팔이 없습니다' })
          await refreshArms()
        }
        // 카운트다운을 보여주므로 촘촘히 본다 — 0.4초면 숫자가 뚝뚝 끊긴다
      }, 150)
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '판별을 시작하지 못했습니다')
    }
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
    } catch { notifyError('등록 실패: 연결 및 역할 지정을 확인하세요') }
  }
  const handleUnregister = async (iface: string) => {
    await api.post('/robots/unregister', { iface })
    setArms((prev) => prev.map((a) => (a.iface === iface ? { ...a, ready: false } : a)))
  }

  // ── 프리셋 ──
  const handlePresetSave = async () => {
    if (!presetName.trim()) return
    setPresetMsg(null)
    try {
      await api.post('/robots/presets/save', { name: presetName.trim() })
      setPresets(await api.get<string[]>('/robots/presets'))
      setPresetName('')
      setPresetMsg(`"${presetName.trim()}" 저장됨`)
    } catch (e) {
      setPresetMsg(`저장 실패: ${(e as Error).message}`)
    }
  }
  const handlePresetLoad = async (name: string) => {
    setPresetMsg(null)
    setPresetBusy(true)
    try {
      // 백엔드가 **실제로 적용된 것**을 돌려준다. 예전에는 프리셋에 적힌 팔 수를
      // 세어서, 하나도 못 붙였는데도 "적용됨"이라고 떴다.
      const d = await api.post<{ applied?: string[]; missing?: string[]; failed?: string[] }>(
        '/robots/presets/load', { name })
      await refreshArms()
      const applied = d.applied ?? []
      const notes: string[] = []
      if (d.missing?.length) notes.push(`못 찾음: ${d.missing.join(', ')} (연결 확인)`)
      if (d.failed?.length) notes.push(`연결 실패: ${d.failed.join(', ')}`)
      setPresetMsg(
        applied.length
          ? `"${name}" 적용 — ${applied.join(', ')}${notes.length ? ` / ${notes.join(' / ')}` : ''}`
          : `"${name}" 을 적용하지 못했습니다${notes.length ? ` — ${notes.join(' / ')}` : ''}`
      )
    } catch (e) {
      setPresetMsg(`불러오기 실패: ${(e as Error).message}`)
    } finally { setPresetBusy(false) }
  }
  const handlePresetDelete = async (name: string) => {
    if (!await askConfirm(`"${name}" 프리셋을 삭제하시겠습니까?`)) return
    await api.delete(`/robots/presets/${name}`)
    setPresets(await api.get<string[]>('/robots/presets'))
  }

  const [clearing, setClearing] = useState(false)

  // ⚠ **연결을 끊으면 토크가 빠진다.** 드는 자세에서 누르면 팔이 주저앉으므로
  //   확인창이 파킹을 먼저 권한다. 실행 중인 활동은 서버가 409 로 막는다.
  const handleClearAll = async () => {
    const n = arms.length
    if (!n) return
    const connected = arms.filter((a) => a.connected).length
    if (!await askConfirm(
      `등록된 로봇과 1·2단계 목록을 모두 비웁니다 (팔 ${n}대).\n\n` +
      '· 역할·좌우·슬롯·설정이 사라지고 세션 파일도 지워집니다\n' +
      (connected ? `· 연결된 ${connected}대는 끊깁니다 — 토크가 빠져 팔이 주저앉습니다\n` : '') +
      '\n' + (connected ? '먼저 파킹 자세로 내려두세요. 계속할까요?' : '계속할까요?'))) return
    setClearing(true)
    try {
      await api.post('/robots/clear', {})
      setArms([])
      setExpandedArm(null)
      setJogIface(null)
      notify({ level: 'info', source: '로봇',
               text: '로봇 상태를 초기화했습니다 — 1단계부터 다시 시작하세요.' })
    } catch (e) {
      notify({ level: 'error', source: '로봇',
               text: e instanceof Error ? e.message : '초기화 실패' })
    } finally {
      setClearing(false)
    }
  }

  // 파생 데이터
  //
  // ⚠ 세 목록은 **배타적이어야 한다.** 등록된 팔(`ready`)은 이미 사용 가능 목록에
  // 있으므로 1·2단계에 또 나오면 안 된다 — 같은 팔을 두 번 등록하게 된다.
  //
  // 프리셋을 불러오면 `ready` 만 서고 연결은 안 되므로 `ready && !connected` 가
  // 생기는데, 예전 `unconnectedArms` 는 `ready` 를 안 봐서 그 팔이 1단계와
  // 사용 가능 목록에 **동시에** 떴다.
  const readyArms = arms.filter((a) => a.ready)
  // ⚠ **전체에서 찾는다.** `connectedArms` 는 `!ready` 라 등록하는 순간 빠지는데,
  //   거기서 리더를 찾으면 등록된 팔끼리는 릴레이 버튼이 영영 안 뜬다.
  // ⚠ **같은 쪽 리더만 짝이 된다.** 예전에는 연결된 첫 리더를 아무 팔에나
  //   넘겼는데, 팔이 넷이면 왼팔을 오른쪽 리더로 끌 수 있었다.
  const leaderFor = (side: string | null | undefined) =>
    side ? arms.find((a) => a.role === 'leader' && a.connected && a.side === side)?.iface
         : undefined
  const connectedArms = arms.filter((a) => a.connected && !a.ready)
  const unconnectedArms = arms.filter((a) => !a.connected && !a.ready)

  if (loading) {
    return <div className="flex items-center justify-center h-64 gap-2 text-neutral-400"><Spinner /> 로딩 중...</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">로봇</h1>
        <div className="flex gap-2">
          <button onClick={handleRefresh} disabled={refreshing}
            className="px-3 py-1.5 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 disabled:opacity-50 flex items-center gap-1">
            {refreshing ? <><Spinner /> 새로고침 중...</> : '↻ 상태 새로고침'}
          </button>
          <button onClick={() => setUsbModalOpen(true)}
            className="px-3 py-1.5 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300">
            USB 진단 / 복구
          </button>
          {/* 비울 게 없으면 안 보인다 — 이미 깨끗한데 "초기화" 를 권하면
              누를 이유를 만들어 주는 셈이고, 위험한 버튼일수록 그러면 안 된다. */}
          {arms.length > 0 && (
            <button onClick={handleClearAll} disabled={clearing}
              className="px-3 py-1.5 text-xs rounded bg-neutral-700 hover:bg-red-600
                         text-neutral-400 hover:text-white disabled:opacity-50">
              {clearing ? '초기화 중…' : '전체 초기화'}
            </button>
          )}
        </div>
      </div>

      {/* 프리셋 */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <h2 className="text-sm font-semibold">프리셋</h2>
        <div className="flex gap-2 flex-wrap">
          {presets.length === 0
            ? <span className="text-xs text-neutral-500">저장된 프리셋 없음</span>
            : presets.map((p) => (
                <div key={p} className="flex items-center gap-1">
                  <button onClick={() => handlePresetLoad(p)} disabled={presetBusy}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white disabled:opacity-50">
                    {presetBusy ? '적용 중…' : p}
                  </button>
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
        {presetMsg && <p className="text-xs text-amber-300 whitespace-pre-wrap">{presetMsg}</p>}
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
              <div key={arm.iface} className="rounded border border-neutral-700 p-2.5 space-y-1.5">
                <div className="flex items-center justify-between">
                  <div className="text-sm flex items-center gap-2">
                    {renamingIface === arm.iface ? (
                      <input type="text" value={renameValue} autoFocus
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') handleRename(arm.iface); if (e.key === 'Escape') setRenamingIface(null) }}
                        onBlur={() => handleRename(arm.iface)}
                        className="font-mono px-1.5 py-0.5 rounded bg-neutral-900 border border-blue-500 text-sm text-neutral-100 w-32 focus:outline-none" />
                    ) : (
                      <span className="font-mono cursor-pointer hover:text-blue-400"
                        onClick={() => { setRenamingIface(arm.iface); setRenameValue(arm.iface) }}
                        title="클릭하여 이름 변경">{arm.iface}</span>
                    )}
                    <span className="text-xs text-neutral-400">{arm.bus_info || ''}</span>
                    <span className={`text-xs ${arm.state === 'UP' ? 'text-green-400' : 'text-red-400'}`}>{arm.state}</span>
                    {arm.state === 'UP' && (() => {
                      const status = canActive[arm.iface]
                      if (status === 'checking') return <span className="text-xs text-yellow-400"><Spinner className="inline" /></span>
                      if (status === true) return <span className="text-xs text-green-400">RX</span>
                      if (status === false) return <span className="text-xs text-red-400">NO DATA</span>
                      return null
                    })()}
                  </div>
                  <div className="flex gap-1">
                    {arm.state !== 'UP' ? (
                      <button onClick={() => handleCanUp(arm.iface)}
                        className="px-2 py-1 text-xs rounded bg-yellow-600 hover:bg-yellow-500 text-white">
                        UP
                      </button>
                    ) : (
                      <button onClick={() => handleCheckActive(arm.iface)} disabled={canActive[arm.iface] === 'checking'}
                        className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 disabled:opacity-50">
                        체크
                      </button>
                    )}
                    <button onClick={() => handleConnect(arm.iface)} disabled={connectingIface === arm.iface}
                      className="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50 flex items-center gap-1">
                      {connectingIface === arm.iface ? <><Spinner /> 연결 중...</> : '연결'}
                    </button>
                  </div>
                </div>
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
              // 누른 그 팔만 움직인다 — 진행 표시도 그 행에만.
              const isDetecting = motionIface === arm.iface && !!motionStatus
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
                        <MasterSlaveBadge ms={arm.master_slave} />
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
                          {/* 절차가 몇 초씩 조용하다 — **무엇을, 얼마나 더**
                              기다리는지 둘 다 보여야 멈춘 것과 구분된다. */}
                          <span className="flex items-center gap-1.5 text-amber-400">
                            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400"
                                  aria-hidden />
                            <span>{motionStatus.phase ?? '확인 중'}</span>
                            {motionStatus.remaining > 0 && (
                              <span className="tabular-nums font-medium">
                                {motionStatus.remaining.toFixed(1)}s
                              </span>
                            )}
                          </span>
                        </div>
                      ) : (
                        <button onClick={() => handleIdentify(arm.iface)} disabled={!!motionIface}
                          className="px-2 py-1 text-xs rounded bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-50"
                          title="이동 명령에 반응하는지로 마스터/슬레이브 판별 (팔이 움직입니다)">찾기</button>
                      )}
                      <button onClick={() => handleSetMasterSlave(arm.iface, true)} disabled={settingMs === arm.iface}
                        className="px-2 py-1 text-xs rounded bg-purple-700 hover:bg-purple-600 text-white disabled:opacity-50"
                        title="이 팔을 마스터(示教入力)로 설정">마스터</button>
                      <button onClick={() => handleSetMasterSlave(arm.iface, false)} disabled={settingMs === arm.iface}
                        className="px-2 py-1 text-xs rounded bg-cyan-700 hover:bg-cyan-600 text-white disabled:opacity-50"
                        title="이 팔을 슬레이브(運動出力)로 설정">슬레이브</button>
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
                      {/* 웹 조그 — 추론을 안 띄우고 이 팔을 움직인다.
                          마스터는 외부 명령을 무시하므로 이유를 적고 막는다. */}
                      <JogPanel
                        iface={arm.iface}
                        commandable={arm.connected && arm.role === 'follower'}
                        leader={leaderFor(arm.side)}
                        side={arm.side}
                        reason={
                          !arm.connected ? '연결되지 않아 조작할 수 없습니다'
                          : arm.role === 'leader'
                            ? '마스터(리더)는 외부 명령을 무시합니다 — 슬레이브로 바꾸거나 팔로워 팔을 고르세요'
                            : '역할을 모릅니다 — 먼저 [찾기] 로 판별하세요'
                        } />

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
              <div key={arm.iface} className="rounded border border-green-500/30 bg-green-500/5">
              <div className="flex items-center justify-between p-2.5">
                <div className="flex items-center gap-2">
                  {/* 등록됐다고 연결까지 되어 있는 것은 아니다 — 팔 전원이 꺼져 있거나
                      프리셋 로드 중 연결이 실패하면 여기 끊긴 채로 남는다. */}
                  <span className={arm.connected ? 'text-green-400 text-sm' : 'text-amber-400 text-sm'}>
                    {arm.connected ? '✓' : '⚠'}
                  </span>
                  <span className="font-mono text-sm">{arm.iface}</span>
                  <span className={`px-1.5 py-0.5 text-[10px] rounded ${arm.role === 'leader' ? 'bg-amber-600/30 text-amber-400' : 'bg-blue-600/30 text-blue-400'}`}>
                    {arm.role}
                  </span>
                  <MasterSlaveBadge ms={arm.master_slave} />
                  {/* 좌/우 (양팔) — 클릭 순환 왼→오른→해제. 등록에 저장된다 */}
                  <button onClick={() => handleSideToggle(arm)}
                    title="양팔에서 이 팔의 좌/우 (클릭해서 변경)"
                    className={`px-1.5 py-0.5 text-[10px] rounded border ${
                      arm.side ? 'bg-purple-600/30 text-purple-300 border-purple-500/40'
                               : 'bg-neutral-700/50 text-neutral-500 border-neutral-600'}`}>
                    {arm.side === 'left' ? '왼팔' : arm.side === 'right' ? '오른팔' : '좌/우?'}
                  </button>
                  <span className="text-xs text-neutral-400">
                    {arm.role === 'leader' ? 'piper_leader' : 'piper_follower'}
                  </span>
                  {!arm.connected && <span className="text-[10px] text-amber-400">연결 끊김</span>}
                  {/* ⚠ 어긋나면 **조용히 안 움직인다** — 팔로워가 마스터 모드면
                      외부 명령을 통째로 무시한다. 문구는 백엔드가 만든다. */}
                  {arm.mode_mismatch && (
                    <span className="rounded bg-red-600/25 px-1.5 py-0.5 text-[10px] text-red-300"
                          title={arm.mode_mismatch}>⚠ 모드 불일치</span>
                  )}
                </div>
                <div className="flex gap-2">
                  {/* 끊긴 팔을 다시 붙일 길 — 1단계 목록에서는 이미 빠졌으므로
                      여기에 없으면 등록을 끝낼 방법이 없어진다. */}
                  {!arm.connected && (
                    <button onClick={() => handleConnect(arm.iface)} disabled={connectingIface === arm.iface}
                      className="px-3 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-50">
                      {connectingIface === arm.iface ? '연결 중...' : '연결'}
                    </button>
                  )}
                  <button onClick={() => handleSetMasterSlave(arm.iface, true)} disabled={settingMs === arm.iface}
                    className="px-3 py-1 text-xs rounded bg-purple-700 hover:bg-purple-600 text-white disabled:opacity-50"
                    title="이 팔을 마스터(示教入力)로 설정">마스터</button>
                  <button onClick={() => handleSetMasterSlave(arm.iface, false)} disabled={settingMs === arm.iface}
                    className="px-3 py-1 text-xs rounded bg-cyan-700 hover:bg-cyan-600 text-white disabled:opacity-50"
                    title="이 팔을 슬레이브(運動出力)로 설정">슬레이브</button>
                  <button onClick={() => setParkingIface(arm.iface)}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white">파킹 보정</button>
                  {/* ⚠ ON 이 없으면 토크를 끈 뒤 되돌릴 길이 화면에 없다.
                      조그·릴레이는 시작할 때 알아서 켜지만, 그 전에 팔을 세워
                      두고 싶을 때가 있다. */}
                  <button onClick={async () => {
                    try {
                      await api.post('/robots/parking/torque?enable=true', { iface: arm.iface })
                    } catch { notifyError('토크 ON 실패') }
                  }}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-green-600 text-neutral-300 hover:text-white">토크 ON</button>
                  <button onClick={async () => {
                    try {
                      await api.post('/robots/parking/torque?enable=false', { iface: arm.iface })
                    } catch { notifyError('토크 OFF 실패') }
                  }}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-amber-600 text-neutral-300 hover:text-white">토크 OFF</button>
                  {/* ⚠ 조작 패널을 여는 자리. 등록된 팔이 **여기** 그려지는데
                      패널을 미등록 목록에만 붙였다가 화면에서 통째로 안 보였다. */}
                  {arm.connected && (
                    <button onClick={() => setZeroIface(arm.iface)}
                      title="모터 플래시에 영점을 굽습니다 — 되돌릴 수 없습니다"
                      className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">
                      영점(HW)</button>
                  )}
                  {arm.connected && (
                    <button onClick={() => setJogIface(arm.iface)}
                      className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-amber-600
                                 text-neutral-300 hover:text-white">
                      조작</button>
                  )}
                  <button onClick={() => handleUnregister(arm.iface)}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">등록해제</button>
                </div>
              </div>

              </div>
            ))}
          </div>
        )}
      </div>

      {/* 조작(조그) 모달 — 행 아래 펼침이었는데 긴 목록에서 다른 팔 행이 밀려
          내려갔다. 팔이 사라지거나 연결이 끊기면 조건이 깨져 스스로 닫힌다. */}
      {(() => {
        const arm = jogIface ? readyArms.find((a) => a.iface === jogIface) : null
        if (!arm || !arm.connected) return null
        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
               onClick={() => setJogIface(null)}>
            <div className="max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-lg border
                            border-neutral-700 bg-neutral-900 p-5 space-y-3"
                 onClick={(e) => e.stopPropagation()}>
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="text-lg font-semibold text-neutral-100">
                    조작 — {arm.iface}
                  </h2>
                  <p className="text-xs text-neutral-400">
                    {arm.role}{arm.side ? ` · ${arm.side === 'left' ? '왼팔' : '오른팔'}` : ''}
                    {' '}— 팔이 실제로 움직입니다
                  </p>
                </div>
                <button onClick={() => setJogIface(null)}
                        className="shrink-0 rounded px-2 py-1 text-sm text-neutral-400 hover:text-white">
                  닫기
                </button>
              </div>
              <JogPanel
                iface={arm.iface}
                commandable={arm.role === 'follower'}
                reason={arm.role === 'leader'
                  ? '마스터(리더)는 외부 명령을 무시합니다 — 이 팔로 팔로워를 끄세요'
                  : '역할을 모릅니다 — 먼저 [찾기] 로 판별하세요'}
                leader={leaderFor(arm.side)}
                side={arm.side} />
            </div>
          </div>
        )
      })()}

      {/* 하드웨어 영점 모달 — 파킹 보정(소프트웨어)과 **다른 물건**이다 */}
      {zeroIface && (
        <ZeroCalibrationModal iface={zeroIface} onClose={() => setZeroIface(null)} />
      )}

      {/* 파킹 보정 모달 */}
      {parkingIface && (
        <ParkingCalibrationModal iface={parkingIface} onClose={() => setParkingIface(null)} />
      )}

      {/* USB 진단/복구 모달 */}
      {usbModalOpen && <UsbInfoModal onClose={() => setUsbModalOpen(false)} />}
    </div>
  )
}
