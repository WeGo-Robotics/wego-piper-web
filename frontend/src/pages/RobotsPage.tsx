import { Fragment, useEffect, useState, useRef, useCallback } from 'react'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'
import AlignmentPanel from '../components/AlignmentPanel'
import BusStatusPanel from '../components/BusStatusPanel'
import DiagnosticsPanel from '../components/DiagnosticsPanel'
import VersionPanel from '../components/VersionPanel'
import JogPanel from '../components/JogPanel'
import { ZeroCalibrationPanel } from '../components/ZeroCalibrationModal'
import { JOINT_NAMES } from '../config/joints'

// ── 파킹 보정 모달 ──
//
// ⚠ **순서를 강제하지 않는다.** 예전에는 `ready → moving → adjusting → saving`
//    한 줄로 흘러서, 한 번 지나간 단계로 못 돌아갔다 — 다시 파킹으로 보내려면
//    창을 닫았다 열어야 했고, 자세를 고쳐 잡고 다시 읽을 수도 없었다. 보정은
//    "맞을 때까지 되풀이하는" 일이라 그 흐름과 안 맞는다.
//
// ⚠ **읽기와 저장을 나눈다.** 예전 `handleSave` 는 `readJoints()` 를 부른 **직후**
//    `joints` 를 저장했는데, React 상태는 그 자리에서 안 바뀐다 — **직전 폴링
//    값**이 저장됐다. 최대 300ms 전의 자세다. 찍어 둔 값을 화면에 보여주고 그걸
//    저장하면, 무엇이 저장되는지 눈으로 확인한 뒤 누르게 된다.

/** 상세 창의 파킹 탭 — 파킹 하기 / 현재 위치 읽기 / 파킹 위치 저장. */
function ParkingPanel({ iface }: { iface: string }) {
  const [joints, setJoints] = useState<Record<string, number>>({})
  //: 저장할 값. **읽기 버튼으로만** 채워진다 — 폴링이 덮어쓰면 찍어 둔 뜻이 없다.
  const [captured, setCaptured] = useState<Record<string, number> | null>(null)
  const [busy, setBusy] = useState<'' | 'park' | 'read' | 'save'>('')
  const [msg, setMsg] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval>>(undefined)

  const readJoints = useCallback(async () => {
    try {
      const data = await api.get<Record<string, number>>(`/robots/parking/joints/${iface}`)
      setJoints(data)
      return data
    } catch { return null }
  }, [iface])

  useEffect(() => {
    readJoints()
    pollRef.current = setInterval(readJoints, 300)
    return () => clearInterval(pollRef.current)
  }, [readJoints])

  const goParking = async () => {
    setBusy('park'); setMsg('')
    try {
      await api.post('/robots/parking/torque?enable=true', { iface })
      await api.post('/robots/parking/go', { iface })
      // ⚠ **고정 3초가 아니라 멈출 때까지 기다린다.** 자세가 멀면 3초로는 안
      //    닿고, 가까우면 괜히 기다린다. 둘 다 사람이 "왜 엉뚱한 데서 멈췄지" 로
      //    만난다. 움직임이 멎으면 도착이다.
      let last: Record<string, number> | null = null
      let still = 0
      for (let i = 0; i < 40 && still < 4; i++) {         // 최대 ~8초
        await new Promise((r) => setTimeout(r, 200))
        const now = await readJoints()
        if (now && last && JOINT_NAMES.every((n) =>
              Math.abs((now[n] ?? 0) - (last![n] ?? 0)) < 0.3)) still++
        else still = 0
        last = now
      }
      await api.post('/robots/parking/torque?enable=false', { iface })
      setMsg('파킹 자세에 섰고 토크를 풀었습니다 — 손으로 자세를 맞추세요.')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '파킹으로 보내지 못했습니다')
    } finally { setBusy('') }
  }

  const capture = async () => {
    setBusy('read'); setMsg('')
    // ⚠ 폴링 상태가 아니라 **지금 응답**을 쓴다 — 상태는 한 박자 늦다
    const now = await readJoints()
    if (now) { setCaptured(now); setMsg('지금 자세를 찍었습니다. 저장을 누르면 이 값이 들어갑니다.') }
    else setMsg('관절값을 읽지 못했습니다')
    setBusy('')
  }

  const save = async () => {
    if (!captured) return
    setBusy('save'); setMsg('')
    try {
      await api.post('/robots/parking/save', { iface, positions: captured })
      await api.post('/robots/parking/torque?enable=true', { iface })
      setMsg('파킹 위치를 저장했고 토크를 걸었습니다.')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '저장하지 못했습니다')
    } finally { setBusy('') }
  }

  // ⚠ 떠날 때 토크를 복원한다 — 파킹 흐름이 토크를 풀어 두는데, 풀린 채로
  //   두면 팔이 중력으로 내려앉는다 (예전 모달의 닫기와 같은 책임).
  useEffect(() => () => {
    api.post('/robots/parking/torque?enable=true', { iface }).catch(() => {})
  }, [iface])

  const drift = (n: string) =>
    captured ? (joints[n] ?? 0) - (captured[n] ?? 0) : 0

  return (
    <div className="space-y-4">
        <p className="text-xs text-neutral-400">
          세 버튼은 <b>순서가 정해져 있지 않습니다</b>. 파킹으로 보내고, 손으로
          맞추고, 읽고, 마음에 안 들면 다시 맞춰 읽으면 됩니다.
        </p>

        <div className="rounded-lg border border-neutral-700 bg-neutral-900 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-semibold text-neutral-400">관절 위치</h3>
            {captured && <span className="text-[10px] text-green-400">찍어 둔 값이 있습니다</span>}
          </div>
          <div className="grid grid-cols-[1fr_auto_auto] gap-x-4 gap-y-1 text-xs">
            <span className="text-[10px] text-neutral-500">관절</span>
            <span className="text-[10px] text-neutral-500 text-right">지금</span>
            <span className="text-[10px] text-neutral-500 text-right w-20">저장할 값</span>
            {JOINT_NAMES.map((name) => (
              <Fragment key={name}>
                <span className="text-neutral-400 font-mono">{name}</span>
                <span className="text-neutral-100 font-mono tabular-nums text-right">
                  {joints[name] !== undefined ? joints[name].toFixed(1) : '—'}
                </span>
                <span className={`font-mono tabular-nums text-right w-20 ${
                  !captured ? 'text-neutral-600'
                  : Math.abs(drift(name)) > 0.5 ? 'text-amber-400' : 'text-green-400'}`}>
                  {captured ? captured[name]?.toFixed(1) ?? '—' : '—'}
                </span>
              </Fragment>
            ))}
          </div>
          {captured && JOINT_NAMES.some((n) => Math.abs(drift(n)) > 0.5) && (
            <p className="text-[10px] text-amber-400">
              찍은 뒤 팔이 움직였습니다 — 지금 자세를 저장하려면 다시 읽으세요.
            </p>
          )}
        </div>

        <div className="flex gap-2">
          <button onClick={goParking} disabled={!!busy}
            title="토크를 걸고 저장된 파킹 자세로 보낸 뒤, 멈추면 토크를 풉니다"
            className="flex-1 px-3 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white
                       text-sm font-medium disabled:opacity-50">
            {busy === 'park' ? '이동 중…' : '파킹'}
          </button>
          <button onClick={capture} disabled={!!busy}
            title="지금 관절값을 찍어 둡니다 — 저장은 이 값으로 됩니다"
            className="flex-1 px-3 py-2 rounded bg-neutral-700 hover:bg-neutral-600
                       text-neutral-100 text-sm font-medium disabled:opacity-50">
            {busy === 'read' ? '읽는 중…' : '현재 위치 읽기'}
          </button>
          <button onClick={save} disabled={!!busy || !captured}
            title={captured ? '찍어 둔 값을 파킹 자세로 저장합니다' : '먼저 현재 위치를 읽으세요'}
            className="flex-1 px-3 py-2 rounded bg-green-600 hover:bg-green-500 text-white
                       text-sm font-medium disabled:opacity-50">
            {busy === 'save' ? '저장 중…' : '파킹 위치 저장'}
          </button>
        </div>

        {msg && <p className="text-xs text-neutral-300 rounded border border-neutral-700
                              bg-neutral-900 px-2 py-1.5">{msg}</p>}
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
  /** 버스에서 무언가 오고 있나. false = 팔이 조용하다 — 마스터와 다르다. */
  responding?: boolean | null
  /** 역할과 팔 모드가 어긋난 사유. **판정은 백엔드가 한다.** */
  mode_mismatch?: string | null
  firmware: string; slot: string | null; side?: 'left' | 'right' | null
  ready: boolean; config: Record<string, unknown>; rx_packets?: number
  /** 최근 2초 안에 관절이 움직였나 — 카드 깜빡임용. 마스터는 지령, 슬레이브는
   *  피드백으로 감지한다 (robot_manager._is_moving). */
  moving?: boolean
}

type PortInfo = {
  iface: string; bus_info?: string; state?: string; can_state?: string | null
  bitrate?: number | null; rx_packets?: number | null; tx_packets?: number | null
  connected: boolean; ready: boolean
}

/**
 * 로봇 카드의 [상세] — 파킹 / 영점 / 조작 / 설정 탭.
 *
 * 셋 다 팔 하나에 고정된 조작이라 한 창에 모은다. 이전에는 파킹·영점·조작이
 * 제각각 모달이었는데, 같은 팔을 다루면서 창을 갈아타야 했다.
 */
function ArmDetailModal({ arm, leader, onConfig, onClose }: {
  arm: ArmInfo
  leader?: string
  onConfig: (cfg: Record<string, unknown>) => void
  onClose: () => void
}) {
  const [dtab, setDtab] = useState<'parking' | 'zero' | 'jog' | 'config'>('parking')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
         onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-lg border
                      border-neutral-700 bg-neutral-900 p-5 space-y-4"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold text-neutral-100">{arm.iface}</h2>
            <p className="text-xs text-neutral-400">
              {arm.role}{arm.side ? ` · ${arm.side === 'left' ? '왼팔' : '오른팔'}` : ''}
              {arm.master_slave ? ` · ${arm.master_slave}` : ''} — 팔이 실제로 움직일 수 있습니다
            </p>
          </div>
          <button onClick={onClose}
                  className="shrink-0 rounded px-2 py-1 text-sm text-neutral-400 hover:text-white">
            닫기
          </button>
        </div>

        <div className="flex overflow-hidden rounded border border-neutral-700 text-xs">
          {([['parking', '파킹'], ['zero', '영점'], ['jog', '조작'],
             ['config', '설정']] as const).map(([k, label]) => (
            <button key={k} onClick={() => setDtab(k)}
              className={`px-4 py-1.5 ${dtab === k
                ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white'}`}>
              {label}
            </button>
          ))}
        </div>

        {dtab === 'parking' && <ParkingPanel iface={arm.iface} />}
        {dtab === 'zero' && <ZeroCalibrationPanel iface={arm.iface} />}
        {dtab === 'jog' && (
          <div className="space-y-3">
            <JogPanel
              iface={arm.iface}
              commandable={arm.role === 'follower'}
              reason={arm.role === 'leader'
                ? '마스터(리더)는 외부 명령을 무시합니다 — 이 팔로 팔로워를 끄세요'
                : '역할을 모릅니다 — 카드의 역할 선택에서 지정하세요'}
              leader={leader}
              side={arm.side} />
            <button onClick={async () => {
              try {
                await api.post('/robots/parking/torque?enable=true', { iface: arm.iface })
                await api.post('/robots/parking/go', { iface: arm.iface })
              } catch { /* 조작 탭 부가 버튼 — 실패는 파킹 탭에서 다룬다 */ }
            }}
              className="w-full px-3 py-2 rounded bg-neutral-700 hover:bg-blue-600 text-sm
                         text-neutral-200 hover:text-white">
              파킹 자세로 이동
            </button>
          </div>
        )}
        {dtab === 'config' && (
          <div className="space-y-2">
            <h4 className="text-xs font-semibold text-neutral-400">
              {arm.role === 'leader' ? 'Leader 설정' : 'Follower 설정'}
            </h4>
            {arm.role === 'follower' || arm.role === 'unknown' ? (
              <div className="space-y-2">
                <label className="flex items-center gap-2 text-xs">
                  <input type="checkbox"
                    checked={(arm.config.disable_torque_on_disconnect as boolean) ?? true}
                    onChange={(e) => onConfig({ disable_torque_on_disconnect: e.target.checked })} />
                  연결 해제 시 토크 비활성화
                </label>
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-neutral-400 w-40">max_relative_target</span>
                  <input type="number" step="0.1"
                    value={(arm.config.max_relative_target as number) ?? ''}
                    onChange={(e) => onConfig({ max_relative_target: e.target.value ? Number(e.target.value) : null })}
                    placeholder="null (무제한)"
                    className="flex-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
                </div>
                <div className="text-xs">
                  <span className="text-neutral-400">cameras (JSON)</span>
                  <textarea value={JSON.stringify(arm.config.cameras ?? {}, null, 2)}
                    onChange={(e) => { try { onConfig({ cameras: JSON.parse(e.target.value) }) } catch { /* 입력 중 */ } }}
                    rows={3}
                    className="w-full mt-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 font-mono text-[11px] focus:outline-none focus:border-blue-500" />
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2 text-xs">
                <span className="text-neutral-400 w-40">gripper_open_pos</span>
                <input type="number" step="1"
                  value={(arm.config.gripper_open_pos as number) ?? 50}
                  onChange={(e) => onConfig({ gripper_open_pos: Number(e.target.value) })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// 하드웨어 마스터(示教输入)/슬레이브(运动输出) 모드 뱃지
function MasterSlaveBadge({ ms, responding }: {
  ms?: 'master' | 'slave' | null
  responding?: boolean | null
}) {
  // ⚠ **조용한 팔을 마스터라 부르지 않는다.** 둘 다 슬레이브 피드백이 없지만
  //   원인도 대처도 다르다 — 실기에서 전원을 내렸다 켠 뒤 네 대가 전부 "마스터"
  //   로 표시돼 원인을 가리기 어려웠다. 켜진 팔은 가만히 있어도 무언가 뿌린다.
  if (responding === false) {
    return (
      <span className="rounded bg-red-600/30 px-1.5 py-0.5 text-[10px] font-medium text-red-300"
        title="CAN 에서 아무 프레임도 오지 않습니다 — 전원·비상정지·케이블을 확인하세요">
        응답 없음
      </span>
    )
  }
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
  // 포트 패널 — 스캔된 포트 + 버스 통계(Rx/Tx·bps). 스캔은 버튼, 통계는 폴링.
  const [ports, setPorts] = useState<PortInfo[]>([])
  // 상세 창 (파킹/영점/조작 탭). 팔 하나에 고정된 모달 — 행 펼침은 긴 목록에서
  // 다른 행을 밀어낸다는 판단을 그대로 잇는다.
  const [detailIface, setDetailIface] = useState<string | null>(null)
  const [tab, setTab] = useState<'devices' | 'alignment' | 'bus' | 'versions' | 'diag'>('devices')
  // 정렬 탭에서 쓸 카메라 목록. **탭을 열 때만** 부른다 — 로봇 페이지가 늘
  // 카메라를 폴링할 이유가 없다.
  const [alignCams, setAlignCams] = useState<
    { id: string; label: string; connected: boolean }[]>([])
  useEffect(() => {
    if (tab !== 'alignment') return
    api.get<{ cameras: { id: string; label?: string; display_name?: string
                         name: string; connected: boolean; stream_type?: string }[] }>(
      '/cameras/current')
      .then((r) => setAlignCams(r.cameras
        // 깊이·적외선에는 태그가 안 보인다 — 컬러만 후보다
        .filter((c) => (c.stream_type ?? 'color') === 'color')
        .map((c) => ({ id: c.id, label: c.display_name || c.label || c.name,
                       connected: !!c.connected }))))
      .catch(() => setAlignCams([]))
  }, [tab])
  const [usbModalOpen, setUsbModalOpen] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  // CAN 관리
  const [renamingIface, setRenamingIface] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
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

  const loadPorts = useCallback(() => {
    api.get<{ ports: PortInfo[] }>('/robots/ports')
      .then((r) => setPorts(r.ports)).catch(() => {})
  }, [])

  // 디바이스 탭이 열려 있는 동안: 포트 통계 3초, 팔 상태(움직임 감지) 2초.
  // 움직임 깜빡임은 폴링이 곧 감지 주기다 — 탭을 떠나면 멈춘다.
  useEffect(() => {
    if (tab !== 'devices') return
    loadPorts()
    const iv1 = setInterval(loadPorts, 3000)
    const iv2 = setInterval(() => { refreshArms().catch(() => {}) }, 2000)
    return () => { clearInterval(iv1); clearInterval(iv2) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, loadPorts])

  // 카드의 [파킹] — 토크를 걸고 저장된 파킹 자세로 보낸다
  const handleParkNow = async (iface: string) => {
    try {
      await api.post('/robots/parking/torque?enable=true', { iface })
      await api.post('/robots/parking/go', { iface })
      notify({ level: 'info', text: `${iface} 파킹 자세로 이동`, source: '로봇' })
    } catch (e) { notifyError(e instanceof Error ? e.message : '파킹 실패') }
  }

  // 카드의 [리셋] — 0x150 재동기화. 간극(=슬립)이 있으면 백엔드 문구가 뜬다.
  const handleResetArm = async (iface: string) => {
    try {
      const r = await api.post<{ warnings: string[] }>('/robots/reset', { iface })
      for (const w of r.warnings) notify({ level: 'warn', text: w, source: '로봇' })
      if (!r.warnings.length)
        notify({ level: 'info', source: '로봇',
          text: `${iface} 리셋 완료 — 재동기화 간극 없음` })
    } catch (e) { notifyError(e instanceof Error ? e.message : '리셋 실패') }
  }

  // 카드의 [해제] — 등록을 걷고 연결도 끊어 포트로 되돌린다
  const handleDetach = async (iface: string) => {
    if (!await askConfirm(
      `${iface} 를 해제합니다.\n연결이 끊기며 토크가 빠질 수 있습니다 — 팔이 드는 자세면 먼저 파킹하세요.`)) return
    try {
      await api.post('/robots/unregister', { iface }).catch(() => {})
      await api.post('/robots/disconnect', { iface })
      await refreshArms()
      loadPorts()
    } catch (e) { notifyError(e instanceof Error ? e.message : '해제 실패') }
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

  // 성공하면 즉시 배지·버튼을 뒤집는다(낙관적) — 서버 확인은 loadPorts 가 한다.
  // /ports 가 라이브 링크 상태를 주므로 다음 폴링에서 사실과 다시 맞춰진다.
  const setPortState = (iface: string, state: 'UP' | 'DOWN') => {
    setPorts((prev) => prev.map((p) => p.iface === iface ? { ...p, state } : p))
    setArms((prev) => prev.map((a) => a.iface === iface ? { ...a, state } : a))
  }

  const handleCanUp = async (iface: string) => {
    try {
      await api.post('/robots/can/up', { iface })
      setPortState(iface, 'UP')
      loadPorts()
    } catch { notifyError('CAN UP 실패') }
  }

  const handleCanDown = async (iface: string) => {
    try {
      await api.post('/robots/can/down', { iface })
      setPortState(iface, 'DOWN')
      loadPorts()
    } catch (e) { notifyError(e instanceof Error ? e.message : 'CAN DOWN 실패') }
  }

  // [연결] — 연결 + **슬레이브 설정 + 토크 OFF** + follower 등록까지 한 번에.
  // 초기화 기본값은 슬레이브다 (백엔드 /attach 주석 참고).
  const handleAttach = async (iface: string) => {
    setConnectingIface(iface)
    try {
      const r = await api.post<ArmInfo & { warnings?: string[] }>('/robots/attach', { iface })
      for (const w of r.warnings ?? []) notify({ level: 'warn', text: w, source: '로봇' })
      await refreshArms()
      loadPorts()
    } catch (e) { notifyError(e instanceof Error ? e.message : '연결 실패') }
    finally { setConnectingIface(null) }
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


  // ── 3단계: 설정 ──
  const handleArmConfig = async (iface: string, cfg: Record<string, unknown>) => {
    const updated = await api.post<ArmInfo>('/robots/arm-config', { iface, config: cfg })
    setArms((prev) => prev.map((a) => (a.iface === iface ? updated : a)))
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
      setDetailIface(null)
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
  // 로봇 패널 = 등록됐거나 연결된 팔 전부. [연결]이 등록까지 하므로 보통 같지만,
  // 옛 세션·프리셋 잔재로 연결-미등록이 남을 수 있어 둘 다 담는다.
  const robotArms = arms.filter((a) => a.ready || a.connected)
  // ⚠ **전체에서 찾는다.** `connectedArms` 는 `!ready` 라 등록하는 순간 빠지는데,
  //   거기서 리더를 찾으면 등록된 팔끼리는 릴레이 버튼이 영영 안 뜬다.
  // ⚠ **같은 쪽 리더만 짝이 된다.** 예전에는 연결된 첫 리더를 아무 팔에나
  //   넘겼는데, 팔이 넷이면 왼팔을 오른쪽 리더로 끌 수 있었다.
  const leaderFor = (side: string | null | undefined) =>
    side ? arms.find((a) => a.role === 'leader' && a.connected && a.side === side)?.iface
         : undefined

  if (loading) {
    return <div className="flex items-center justify-center h-64 gap-2 text-neutral-400"><Spinner /> 로딩 중...</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">로봇</h1>
        <div className="flex overflow-hidden rounded-lg border border-neutral-700 text-sm">
          {([['devices', '디바이스'], ['alignment', '정렬'],
             ['bus', '버스 상태'], ['versions', '버전'],
             ['diag', '검사']] as const).map(([k, label]) => (
            <button key={k} onClick={() => setTab(k)}
              className={`px-4 py-1.5 ${tab === k
                ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white'}`}>
              {label}
            </button>
          ))}
        </div>
        <div className="ml-auto flex gap-2">
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

      {tab === 'devices' && (<>
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

      {/* 포트 — 스캔된 CAN 포트 카드. 버스에 지금 뭐가 흐르는지(Rx/Tx)와
          링크 상태(UP/DOWN)를 보여주고, [연결]은 UP 일 때만 산다. */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">포트</h2>
          <button onClick={handleScan} disabled={scanning}
            className="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
            {scanning ? <><Spinner className="inline" /> 스캔 중...</> : '스캔'}
          </button>
        </div>
        {ports.length === 0 ? (
          <p className="text-xs text-neutral-400">"스캔"을 눌러 CAN 포트를 검색하세요</p>
        ) : (
          // ⚠ FHD 에서 **네 장이 한 줄**에 들어가야 한다. 팔이 넷인 배치가 기본
          //    이라, 세 장에서 끊기면 마지막 하나만 다음 줄로 떨어져 한눈에 안
          //    들어온다.
          //
          // ⚠ **`2xl`(1536px)은 FHD 에 안 걸린다.** 브레이크포인트는 물리 해상도가
          //    아니라 **CSS 픽셀**이라, 1920 화면이라도 브라우저 확대나 디스플레이
          //    배율이 125% 면 유효 뷰포트가 ~1511px 이다 — 25px 차이로 못 미쳐
          //    계속 세 장이었다. `xl`(1280px)로 내려 배율이 걸려도 넷이 되게 한다.
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2">
            {ports.map((port) => {
              const isUp = port.state === 'UP'
              return (
                <div key={port.iface}
                     className={`rounded border p-2.5 space-y-1.5 ${
                       port.connected ? 'border-green-500/40 bg-green-500/5'
                                      : 'border-neutral-700'}`}>
                  <div className="flex items-center gap-2">
                    {renamingIface === port.iface ? (
                      <input type="text" value={renameValue} autoFocus
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') handleRename(port.iface); if (e.key === 'Escape') setRenamingIface(null) }}
                        onBlur={() => handleRename(port.iface)}
                        className="font-mono px-1.5 py-0.5 rounded bg-neutral-900 border border-blue-500 text-sm text-neutral-100 w-28 focus:outline-none" />
                    ) : (
                      <span className="font-mono text-sm cursor-pointer hover:text-blue-400"
                        onClick={() => { setRenamingIface(port.iface); setRenameValue(port.iface) }}
                        title="클릭하여 이름 변경">{port.iface}</span>
                    )}
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                      isUp ? 'bg-green-600/25 text-green-400' : 'bg-red-600/25 text-red-400'}`}>
                      {port.state ?? '?'}
                    </span>
                    {port.connected && <span className="text-[10px] text-green-400">연결됨</span>}
                  </div>
                  {/* ⚠ 카운터는 인터페이스를 다시 열면 0 이 된다 — 절대값 비교 금물,
                      "지금 흐르고 있나"의 감으로만 (백엔드 주석과 같은 규칙) */}
                  <p className="text-[11px] text-neutral-400 tabular-nums">
                    RX {port.rx_packets ?? '—'} · TX {port.tx_packets ?? '—'}
                    {port.bitrate ? ` · ${(port.bitrate / 1e6).toFixed(0)}M bps` : ''}
                    {port.can_state && port.can_state !== 'ERROR-ACTIVE' && (
                      <span className="ml-1 text-amber-400">{port.can_state}</span>
                    )}
                  </p>
                  <p className="truncate text-[10px] text-neutral-600">{port.bus_info || ''}</p>
                  <div className="flex gap-1">
                    {isUp ? (
                      <button onClick={() => handleCanDown(port.iface)} disabled={port.connected}
                        title={port.connected ? '연결된 로봇이 있어 내릴 수 없습니다' : '인터페이스를 내립니다'}
                        className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white disabled:opacity-40">
                        DOWN
                      </button>
                    ) : (
                      <button onClick={() => handleCanUp(port.iface)}
                        className="px-2 py-1 text-xs rounded bg-yellow-600 hover:bg-yellow-500 text-white">
                        UP
                      </button>
                    )}
                    <button onClick={() => handleAttach(port.iface)}
                      disabled={!isUp || port.connected || connectingIface === port.iface}
                      title={!isUp ? '포트가 UP 이어야 연결할 수 있습니다'
                        : port.connected ? '이미 연결됨'
                        : '연결 + 슬레이브 설정 + 토크 OFF + 등록까지 한 번에'}
                      className="flex-1 px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-40">
                      {connectingIface === port.iface ? <><Spinner className="inline" /> 연결 중…</>
                        : port.connected ? '연결됨' : '연결'}
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* 로봇 — 연결·등록된 팔 카드. 움직임이 감지되면 깜빡인다
          (마스터는 지령, 슬레이브는 피드백으로 — 백엔드가 가려서 잰다). */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <h2 className="text-sm font-semibold">로봇</h2>
        {robotArms.length === 0 ? (
          <p className="text-xs text-neutral-400">등록된 로봇이 없습니다 — 포트에서 [연결]을 누르세요.</p>
        ) : (
          <div className="space-y-1.5">
            {robotArms.map((arm) => (
              <div key={arm.iface}
                   className={`rounded border p-2.5 transition-shadow ${
                     arm.moving
                       ? 'border-amber-400 bg-amber-500/10 ring-1 ring-amber-400/70 animate-pulse'
                       : arm.ready ? 'border-green-500/30 bg-green-500/5'
                                   : 'border-blue-500/30 bg-blue-500/5'}`}>
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <div className="flex items-center gap-2 flex-wrap min-w-0">
                    <span className={arm.connected ? 'text-green-400 text-sm' : 'text-amber-400 text-sm'}>
                      {arm.connected ? '✓' : '⚠'}
                    </span>
                    <span className="font-mono text-sm cursor-pointer hover:text-blue-400"
                      onClick={() => { setRenamingIface(arm.iface); setRenameValue(arm.iface) }}
                      title="포트(=로봇) 이름 — 클릭하여 변경">{arm.iface}</span>
                    <select value={arm.role} onChange={(e) => handleRoleChange(arm.iface, e.target.value)}
                      className="px-1.5 py-0.5 text-[10px] rounded bg-neutral-900 border border-neutral-600 text-neutral-100">
                      <option value="unknown">역할?</option>
                      <option value="leader">leader</option>
                      <option value="follower">follower</option>
                    </select>
                    <MasterSlaveBadge ms={arm.master_slave} responding={arm.responding} />
                    <button onClick={() => handleSideToggle(arm)}
                      title="양팔에서 이 팔의 좌/우 (클릭해서 변경)"
                      className={`px-1.5 py-0.5 text-[10px] rounded border ${
                        arm.side ? 'bg-purple-600/30 text-purple-300 border-purple-500/40'
                                 : 'bg-neutral-700/50 text-neutral-500 border-neutral-600'}`}>
                      {arm.side === 'left' ? '왼팔' : arm.side === 'right' ? '오른팔' : '좌/우?'}
                    </button>
                    {arm.moving && <span className="text-[10px] text-amber-400">● 움직임</span>}
                    {!arm.connected && <span className="text-[10px] text-amber-400">연결 끊김</span>}
                    {arm.mode_mismatch && (
                      <span className="rounded bg-red-600/25 px-1.5 py-0.5 text-[10px] text-red-300"
                            title={arm.mode_mismatch}>⚠ 모드 불일치</span>
                    )}
                  </div>
                  <div className="flex gap-1.5 flex-wrap shrink-0">
                    {!arm.connected && (
                      <button onClick={() => handleAttach(arm.iface)} disabled={connectingIface === arm.iface}
                        className="px-2.5 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-50">
                        {connectingIface === arm.iface ? '연결 중…' : '연결'}
                      </button>
                    )}
                    <button onClick={() => handleSetMasterSlave(arm.iface, true)} disabled={settingMs === arm.iface}
                      className="px-2.5 py-1 text-xs rounded bg-purple-700 hover:bg-purple-600 text-white disabled:opacity-50"
                      title="이 팔을 마스터(示教入力)로 설정">마스터</button>
                    <button onClick={() => handleSetMasterSlave(arm.iface, false)} disabled={settingMs === arm.iface}
                      className="px-2.5 py-1 text-xs rounded bg-cyan-700 hover:bg-cyan-600 text-white disabled:opacity-50"
                      title="이 팔을 슬레이브(運動出力)로 설정">슬레이브</button>
                    <button onClick={async () => {
                      try { await api.post('/robots/parking/torque?enable=true', { iface: arm.iface }) }
                      catch { notifyError('토크 ON 실패') }
                    }} className="px-2.5 py-1 text-xs rounded bg-neutral-700 hover:bg-green-600 text-neutral-300 hover:text-white">토크 ON</button>
                    <button onClick={async () => {
                      try { await api.post('/robots/parking/torque?enable=false', { iface: arm.iface }) }
                      catch { notifyError('토크 OFF 실패') }
                    }} className="px-2.5 py-1 text-xs rounded bg-neutral-700 hover:bg-amber-600 text-neutral-300 hover:text-white">토크 OFF</button>
                    <button onClick={() => handleParkNow(arm.iface)} disabled={!arm.connected}
                      title="토크를 걸고 저장된 파킹 자세로 보냅니다"
                      className="px-2.5 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white disabled:opacity-40">파킹</button>
                    <button onClick={() => handleResetArm(arm.iface)} disabled={!arm.connected}
                      title="0x150 재동기화 — 슬립으로 밀린 보고 위치를 실제에 맞춥니다"
                      className="px-2.5 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white disabled:opacity-40">리셋</button>
                    <button onClick={() => setDetailIface(arm.iface)} disabled={!arm.connected}
                      className="px-2.5 py-1 text-xs rounded bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-40">상세</button>
                    <button onClick={() => handleDetach(arm.iface)}
                      className="px-2.5 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">해제</button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      </>)}

      {tab === 'bus' && <BusStatusPanel />}

      {tab === 'versions' && <VersionPanel />}

      {tab === 'diag' && (
        <DiagnosticsPanel
          arms={arms.map((a) => ({ iface: a.iface, connected: !!a.connected,
                                   master_slave: a.master_slave,
                                   responding: a.responding }))} />
      )}

      {tab === 'alignment' && (
        <AlignmentPanel
          arms={arms.map((a) => ({ iface: a.iface, connected: !!a.connected }))}
          cameras={alignCams} />
      )}

      {/* 모달은 탭 밖에 둔다 — 탭을 바꾼다고 사라져야 할 것들이 아니다. */}
      {/* 상세 창 — 파킹/영점/조작. 팔이 끊기면 조건이 깨져 스스로 닫힌다. */}
      {(() => {
        const arm = detailIface ? arms.find((a) => a.iface === detailIface) : null
        if (!arm || !arm.connected) return null
        return (
          <ArmDetailModal arm={arm} leader={leaderFor(arm.side)}
            onConfig={(cfg) => handleArmConfig(arm.iface, cfg)}
            onClose={() => setDetailIface(null)} />
        )
      })()}

      {/* USB 진단/복구 모달 */}
      {usbModalOpen && <UsbInfoModal onClose={() => setUsbModalOpen(false)} />}
    </div>
  )
}
