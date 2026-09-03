import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 버전 — 팔별 펌웨어와 관절별 하드웨어 정보.
 *
 * ⚠ **관절별 펌웨어 버전은 없다.** 프로토콜에 그런 필드가 없고 팔은 문자열
 *   하나(`S-VX.X-X`)만 신고한다. 없는 칸을 만들어 두면 사람이 "왜 비어 있지" 를
 *   묻게 되므로, 관절마다는 **실제로 읽히는 것**만 보여주고 그 사실을 적는다.
 */

type Joint = {
  joint: string; motor: number
  voltage_v?: number; driver_temp_c?: number; motor_temp_c?: number
  enabled?: boolean; flags?: string[]
  /** 저속 피드백이 왔나. 마스터 팔은 안 보낸다 — 그때 0 을 그리면 측정값처럼 보인다. */
  feedback?: boolean
  angle_min_deg?: number; angle_max_deg?: number
  max_spd_rad_s?: number; max_acc_rad_s2?: number
}
type ArmVersions = {
  iface: string; firmware: string | null
  master_slave: string | null; ctrl_mode: string | null
  sdk: string | null; protocol: string | null; interface: string | null
  joints: Joint[]
}

const FLAG_LABEL: Record<string, string> = {
  voltage_too_low: '저전압', motor_overheating: '모터 과열',
  driver_overcurrent: '과전류', driver_overheating: '드라이버 과열',
  collision_status: '충돌 보호', driver_error_status: '드라이버 오류',
}

/** 온도 경고선(℃). 이 위는 색을 준다 — 늘 색이 있으면 아무도 안 본다. */
const TEMP_WARN = 55

/** 표의 열. 팔마다 표가 따로라 **너비를 여기서 한 번에 정해야** 열이 맞는다. */
const COLS = [
  { key: 'joint', label: '관절', width: '9%' },
  { key: 'volt', label: '전압', width: '9%' },
  { key: 'dtemp', label: '드라이버 온도', width: '13%' },
  { key: 'mtemp', label: '모터 온도', width: '12%' },
  { key: 'angle', label: '각도 범위', width: '17%' },
  { key: 'spd', label: '최대 속도', width: '13%' },
  { key: 'acc', label: '최대 가속', width: '13%' },
  { key: 'state', label: '상태', width: '14%' },
] as const

const n = (v: number | undefined, unit = '', digits = 0) =>
  v == null ? '—' : `${v.toFixed(digits)}${unit}`

export default function VersionPanel() {
  const [arms, setArms] = useState<ArmVersions[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [at, setAt] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setBusy(true)
    try {
      const d = await api.get<{ arms: ArmVersions[] }>('/robots/versions')
      setArms(d.arms); setErr(null); setAt(new Date())
    } catch (e) {
      setErr(e instanceof Error ? e.message : '읽지 못했습니다')
    } finally { setBusy(false) }
  }, [])
  useEffect(() => { void load() }, [load])

  // ⚠ 자동 새로고침을 두지 않는다. 버전은 안 변하고, 이 조회는 팔마다 한계값을
  //   **물어보는**(Search→대기→Get) 일이라 주기적으로 돌릴 성질이 아니다.
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <button onClick={() => void load()} disabled={busy}
          className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300
                     hover:bg-neutral-600 disabled:opacity-50">
          {busy ? '읽는 중…' : '↻ 다시 읽기'}
        </button>
        <span className="text-xs text-neutral-500">
          {at ? `${at.toLocaleTimeString('ko-KR')} 기준` : '아직 안 읽음'}
        </span>
      </div>

      {err && <p className="text-xs text-red-400">{err}</p>}
      {arms.length === 0 && !err && !busy && (
        <p className="text-xs text-neutral-500">연결된 팔이 없습니다 — 디바이스 탭에서 먼저 연결하세요.</p>
      )}

      {arms.map((a) => (
        <div key={a.iface} className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 space-y-3">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-mono text-sm text-neutral-100">{a.iface}</span>
            <span className="rounded bg-blue-600/20 px-2 py-0.5 text-xs text-blue-300">
              {a.firmware || '펌웨어 미상'}
            </span>
            {a.master_slave && (
              <span className="text-xs text-neutral-400">{a.master_slave}</span>
            )}
            {a.ctrl_mode && <span className="text-xs text-neutral-500">{a.ctrl_mode}</span>}
            {/* 오른쪽 끝에서 잘리던 줄 — 좁아지면 접히게 둔다 */}
            <span className="ml-auto shrink-0 text-xs text-neutral-500">
              SDK {a.sdk ?? '—'} · {a.protocol ?? '—'}
            </span>
          </div>

          <div className="overflow-x-auto">
            {/* ⚠ **너비를 고정한다.** 팔마다 표가 따로라 내용에 맞춰 칸이 잡히면
                팔끼리 열이 어긋난다 — 값이 없는 팔(`—`)과 있는 팔이 나란히 있을
                때 특히 심하다. 위아래로 훑으며 비교하는 표라 열이 맞아야 한다. */}
            <table className="w-full min-w-[52rem] table-fixed text-xs tabular-nums">
              <colgroup>
                {COLS.map((c) => <col key={c.key} style={{ width: c.width }} />)}
              </colgroup>
              <thead className="text-neutral-500">
                <tr className="text-left">
                  {COLS.map((c) => (
                    <th key={c.key} className="py-1 pr-3 font-normal">{c.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="text-neutral-300">
                {a.joints.map((j) => (
                  <tr key={j.joint} className="border-t border-neutral-700/60">
                    <td className="py-1 pr-3 text-neutral-100">{j.joint}</td>
                    <td className="py-1 pr-3">{n(j.voltage_v, 'V', 1)}</td>
                    <td className={`py-1 pr-3 ${(j.driver_temp_c ?? 0) >= TEMP_WARN ? 'text-amber-400' : ''}`}>
                      {n(j.driver_temp_c, '℃')}
                    </td>
                    <td className={`py-1 pr-3 ${(j.motor_temp_c ?? 0) >= TEMP_WARN ? 'text-amber-400' : ''}`}>
                      {n(j.motor_temp_c, '℃')}
                    </td>
                    <td className="py-1 pr-3">
                      {j.angle_min_deg == null ? '—'
                        : `${j.angle_min_deg}° ~ ${j.angle_max_deg}°`}
                    </td>
                    <td className="py-1 pr-3">{n(j.max_spd_rad_s, ' rad/s', 2)}</td>
                    <td className="py-1 pr-3">{n(j.max_acc_rad_s2, ' rad/s²', 1)}</td>
                    <td className="py-1">
                      {j.feedback === false
                        ? <span className="text-neutral-500"
                                title="마스터(示教输入臂)는 저속 피드백을 보내지 않습니다 — 값이 없는 것이지 0 인 것이 아닙니다">
                            피드백 없음
                          </span>
                        : j.flags?.length
                        ? <span className="text-red-400">
                            {j.flags.map((f) => FLAG_LABEL[f] ?? f).join(' · ')}
                          </span>
                        : <span className={j.enabled ? 'text-green-400' : 'text-neutral-500'}>
                            {j.enabled ? '토크 켜짐' : '토크 꺼짐'}
                          </span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      <p className="text-xs text-neutral-600">
        ⚠ <b>관절별 펌웨어 버전은 없습니다.</b> Piper 프로토콜에 그런 필드가 없고
        팔이 문자열 하나(<code>S-VX.X-X</code>)만 신고합니다. 관절마다는 실제로
        읽히는 값 — 전압·온도·설정된 각도/속도/가속 한계 — 을 보여줍니다.
        같은 모델인데 한 관절만 한계가 다르면 그게 단서입니다.
      </p>
    </div>
  )
}
