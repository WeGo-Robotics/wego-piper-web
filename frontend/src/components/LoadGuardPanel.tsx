import { useCallback, useEffect, useRef, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 관절 부하 감시 — 슬립이 나는 **조건**을 잡는다 (robot/piper_robot/load.py).
 *
 * ## 이 화면이 답하는 질문
 *
 * **"이 팔의 관절이 얼마까지 힘을 쓰고 있고, 어디부터 위험한가?"**
 *
 * ⚠ **이건 전류를 제한하지 않는다.** Piper 펌웨어에는 관절 전류/토크 상한을
 *   거는 명령 자체가 없다 (프로토콜 0x470~0x47E 어디에도 없다 — 그리퍼의
 *   `gripper_effort` 만 예외다). 여기서 하는 일은 넘었을 때 **알리는 것**뿐이고,
 *   그래서 이름도 "리미트" 가 아니라 "감시" 다. 이 구분을 화면이 흐리면 사람은
 *   설정해 두었으니 팔이 보호된다고 믿는다 — 실제로는 아무것도 안 막는다.
 *
 * ## 왜 팔마다 따로인가
 *
 * 같은 모델이라도 슬립은 특정 개체의 특정 관절에서 반복해서 난다 (실기에서
 * can3 joint5). 설치 전체에 하나로 묶으면 그 관절을 잡으려고 조인 임계가
 * 멀쩡한 나머지 팔을 종일 울린다.
 *
 * ## 왜 임계 옆에 실측을 같이 놓나
 *
 * 기본 임계는 **잠정값이다** — 첫 실측이 이미 근거 하나를 뒤집었다(J5 는
 * 10.3A 에서 끊겼는데 J2 는 11.4A 까지 갔다). 사람이 숫자를 고르려면 "이 팔이
 * 평소 어디까지 가나"를 같은 화면에서 봐야 한다. 그래서 지금값·최대값이 임계
 * 입력 옆에 있다.
 */

const JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6'] as const

type Limits = {
  iface: string
  enabled: boolean
  warn_nm: number
  dwell_s: number
  per_joint: Record<string, number>
  effective: Record<string, { nm: number; a: number }>
  range_nm: [number, number]
  range_dwell_s: [number, number]
  default_nm: number
  provisional: boolean
}

type JointState = {
  now_nm: number; now_a: number
  peak_nm: number; peak_at: number
  over_s: number; events: number; raised: boolean
}

type Status = { joints: Record<string, JointState> } | null

/** 켜짐/꺼짐 스위치 — `FloorGuardPanel` 과 같은 물건이다.
 *
 * ⚠ 세 번째 사용처가 생겼으니 공용으로 빼야 한다. 지금 빼지 않는 이유는 이
 *   변경이 감시 기능이지 리팩터링이 아니어서다 — 다음에 손대는 사람이 하면 된다. */
function Switch({ on, onChange, disabled }: {
  on: boolean; onChange: (v: boolean) => void; disabled?: boolean
}) {
  return (
    <button
      type="button" role="switch" aria-checked={on} disabled={disabled}
      onClick={() => onChange(!on)}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full
        transition-colors disabled:opacity-50
        focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500
        focus-visible:ring-offset-2 focus-visible:ring-offset-neutral-800
        ${on ? 'bg-green-500' : 'bg-neutral-600'}`}>
      <span className={`inline-block h-4 w-4 rounded-full bg-white shadow
        transition-transform ${on ? 'translate-x-6' : 'translate-x-1'}`} />
    </button>
  )
}

/** 지금 토크를 임계 대비 막대로. 숫자만으로는 "여유가 얼마나" 가 안 읽힌다. */
function Bar({ nm, warn }: { nm: number; warn: number }) {
  const pct = Math.max(0, Math.min(100, (nm / warn) * 100))
  const color = pct >= 100 ? 'bg-red-500' : pct >= 75 ? 'bg-amber-500' : 'bg-green-600'
  return (
    <div className="h-1.5 w-full overflow-hidden rounded bg-neutral-700">
      <div className={`h-full ${color} transition-[width] duration-200`}
           style={{ width: `${pct}%` }} />
    </div>
  )
}

export default function LoadGuardPanel({ iface }: { iface: string }) {
  const { notify } = useSystemMessage()
  const [limits, setLimits] = useState<Limits | null>(null)
  const [status, setStatus] = useState<Status>(null)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [warnDraft, setWarnDraft] = useState('')
  const [dwellDraft, setDwellDraft] = useState('')
  const [jointDraft, setJointDraft] = useState<Record<string, string>>({})
  // ⚠ **입력 중에 폴링이 값을 덮으면 안 된다.** 1초마다 응답이 오는데 그때마다
  //   draft 를 되돌리면 사람이 숫자를 다 못 친다. 그래서 폴링은 status 만 쓰고
  //   limits 는 처음과 저장 직후에만 draft 에 반영한다.
  const seeded = useRef(false)

  const refresh = useCallback(async (seed: boolean) => {
    try {
      const r = await api.get<{ limits: Limits; status: Status }>(
        `/robots/load?iface=${encodeURIComponent(iface)}`)
      setStatus(r.status)
      setLimits(r.limits)
      if (seed || !seeded.current) {
        seeded.current = true
        setWarnDraft(String(r.limits.warn_nm))
        setDwellDraft(String(r.limits.dwell_s))
        setJointDraft(Object.fromEntries(
          JOINTS.map((j) => [j, r.limits.per_joint[j] != null
            ? String(r.limits.per_joint[j]) : ''])))
      }
    } catch {
      setLimits(null)
    } finally {
      setLoaded(true)
    }
  }, [iface])

  useEffect(() => {
    void refresh(true)
    // 상세 창이 열려 있는 동안만 돈다 — 모달이 닫히면 언마운트되며 멈춘다.
    const t = setInterval(() => { void refresh(false) }, 1000)
    return () => clearInterval(t)
  }, [refresh])

  const send = async (patch: Record<string, unknown>) => {
    setBusy(true)
    try {
      const r = await api.post<{ limits: Limits }>('/robots/load', { iface, ...patch })
      setLimits(r.limits)
      setWarnDraft(String(r.limits.warn_nm))
      setDwellDraft(String(r.limits.dwell_s))
      setJointDraft(Object.fromEntries(
        JOINTS.map((j) => [j, r.limits.per_joint[j] != null
          ? String(r.limits.per_joint[j]) : ''])))
      notify({ level: 'info', source: '부하 감시',
        text: r.limits.enabled
          ? `${iface} 부하 감시 켜짐 — 공통 임계 ${r.limits.warn_nm}N·m`
          : `${iface} 부하 감시 꺼짐 (기록은 계속됩니다)` })
    } catch (e) {
      notify({ level: 'error', source: '부하 감시',
        text: e instanceof Error ? e.message : '설정 변경 실패' })
      void refresh(true)
    } finally {
      setBusy(false)
    }
  }

  if (!loaded) return <div className="text-sm text-neutral-500">불러오는 중…</div>

  // ⚠ 게이트웨이가 기본값을 지어내지 않는다 — 데몬이 없으면 아무것도 안 재고
  //   있는데 화면만 "감시 중" 이라고 말하는 상태가 제일 나쁘다.
  if (!limits) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-amber-400">
          robotd 가 응답하지 않아 부하 설정을 읽을 수 없습니다.
        </p>
        <p className="text-xs text-neutral-500">
          감시는 robotd 안에서 돕니다 — 데몬이 없으면 아무것도 재고 있지 않습니다.
        </p>
      </div>
    )
  }

  const [lo, hi] = limits.range_nm
  const [dlo, dhi] = limits.range_dwell_s
  const warnNum = Number(warnDraft)
  const warnOk = warnDraft.trim() !== '' && Number.isFinite(warnNum)
    && warnNum >= lo && warnNum <= hi
  const dwellNum = Number(dwellDraft)
  const dwellOk = dwellDraft.trim() !== '' && Number.isFinite(dwellNum)
    && dwellNum >= dlo && dwellNum <= dhi

  const jointPatch = () => Object.fromEntries(JOINTS.map((j) => {
    const raw = (jointDraft[j] ?? '').trim()
    // 빈 칸 = "이 관절은 공통값을 쓴다". `null` 로 보내야 덮어쓰기가 지워진다.
    return [j, raw === '' ? null : Number(raw)]
  }))
  const jointsValid = JOINTS.every((j) => {
    const raw = (jointDraft[j] ?? '').trim()
    if (raw === '') return true
    const n = Number(raw)
    return Number.isFinite(n) && n >= lo && n <= hi
  })
  const jointsDirty = JOINTS.some((j) => {
    const raw = (jointDraft[j] ?? '').trim()
    const saved = limits.per_joint[j]
    return raw === '' ? saved != null : Number(raw) !== saved
  })

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold text-neutral-100">
            부하 감시 (전류·토크)
          </h3>
          <p className="text-xs text-neutral-400">
            관절이 임계를 넘긴 채 버티면 알립니다 — 슬립이 나는 조건입니다.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className={`text-xs ${limits.enabled ? 'text-green-400' : 'text-neutral-500'}`}>
            {limits.enabled ? '켜짐' : '꺼짐'}
          </span>
          <Switch on={limits.enabled} disabled={busy}
                  onChange={(v) => void send({ enabled: v })} />
        </div>
      </div>

      {/* ⚠ 이 문단을 빼면 안 된다. "리미트" 로 읽은 사람은 설정해 뒀으니
          팔이 보호된다고 믿는데, 실제로 막는 것은 아무것도 없다. */}
      <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2
                    text-xs leading-relaxed text-amber-300">
        <b>전류를 제한하지 않습니다.</b> Piper 펌웨어에는 관절 전류·토크 상한을 거는
        명령이 없습니다 — 이 설정은 넘었을 때 <b>알리기만</b> 합니다. 실제로 밀렸는지는
        리셋(0x150) 전후 간극으로 확인하세요 ([영점] 탭의 리셋).
      </p>

      <div className="flex flex-wrap items-end gap-3">
        <label className="space-y-1">
          <span className="block text-xs text-neutral-400">공통 임계</span>
          <div className="flex items-center gap-1.5">
            <input type="number" step="0.5" min={lo} max={hi} value={warnDraft}
              onChange={(e) => setWarnDraft(e.target.value)}
              disabled={busy}
              className="w-24 rounded border border-neutral-600 bg-neutral-900 px-2 py-1
                         text-sm text-neutral-100 disabled:opacity-50" />
            <span className="text-xs text-neutral-400">N·m</span>
          </div>
        </label>
        <label className="space-y-1">
          <span className="block text-xs text-neutral-400">지속 시간</span>
          <div className="flex items-center gap-1.5">
            <input type="number" step="0.05" min={dlo} max={dhi} value={dwellDraft}
              onChange={(e) => setDwellDraft(e.target.value)}
              disabled={busy}
              className="w-24 rounded border border-neutral-600 bg-neutral-900 px-2 py-1
                         text-sm text-neutral-100 disabled:opacity-50" />
            <span className="text-xs text-neutral-400">초</span>
          </div>
        </label>
        <button
          onClick={() => void send({ warn_nm: warnNum, dwell_s: dwellNum })}
          disabled={busy || !warnOk || !dwellOk
            || (warnNum === limits.warn_nm && dwellNum === limits.dwell_s)}
          className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white
                     hover:bg-blue-500 disabled:opacity-40">
          적용
        </button>
        {limits.warn_nm !== limits.default_nm && (
          <button onClick={() => void send({ warn_nm: limits.default_nm })}
            disabled={busy}
            className="rounded bg-neutral-700 px-3 py-1.5 text-sm text-neutral-300
                       hover:bg-neutral-600 disabled:opacity-40">
            기본값 ({limits.default_nm})
          </button>
        )}
      </div>
      {(!warnOk || !dwellOk) && (
        <p className="text-xs text-red-400">
          임계는 {lo}~{hi} N·m, 지속 시간은 {dlo}~{dhi} 초 사이여야 합니다
        </p>
      )}

      <div className="space-y-1">
        <div className="flex items-baseline justify-between">
          <span className="text-xs font-semibold text-neutral-400">관절별</span>
          <span className="text-[11px] text-neutral-500">
            임계 칸을 비우면 공통값을 씁니다
          </span>
        </div>
        <table className="w-full text-xs">
          <thead className="text-[11px] text-neutral-500">
            <tr className="border-b border-neutral-700">
              <th className="py-1 text-left font-normal">관절</th>
              <th className="py-1 text-right font-normal">지금</th>
              <th className="py-1 text-right font-normal">최대</th>
              <th className="py-1 text-right font-normal">과부하</th>
              <th className="py-1 pl-2 text-left font-normal">임계 (N·m)</th>
            </tr>
          </thead>
          <tbody>
            {JOINTS.map((j) => {
              const st = status?.joints?.[j]
              const eff = limits.effective[j]
              return (
                <tr key={j} className="border-b border-neutral-800/60">
                  <td className="py-1.5">
                    <div className="flex items-center gap-1.5">
                      <span className={st?.raised ? 'text-red-400' : 'text-neutral-300'}>
                        {j.replace('joint', 'J')}
                      </span>
                      {st?.raised && (
                        <span className="rounded bg-red-600/30 px-1 py-0.5 text-[10px] text-red-300">
                          과부하
                        </span>
                      )}
                    </div>
                    {st && eff && <div className="mt-1 w-16"><Bar nm={st.now_nm} warn={eff.nm} /></div>}
                  </td>
                  <td className="py-1.5 text-right tabular-nums text-neutral-300">
                    {st ? <>{st.now_nm.toFixed(2)}<span className="text-neutral-500"> N·m</span>
                      <div className="text-[10px] text-neutral-500">{st.now_a.toFixed(2)} A</div></>
                      : <span className="text-neutral-600">—</span>}
                  </td>
                  <td className="py-1.5 text-right tabular-nums text-neutral-300">
                    {st ? st.peak_nm.toFixed(2) : <span className="text-neutral-600">—</span>}
                  </td>
                  <td className="py-1.5 text-right tabular-nums text-neutral-400">
                    {st ? <>{st.over_s.toFixed(1)}s
                      {st.events > 0 && <span className="text-amber-400"> ·{st.events}회</span>}</>
                      : <span className="text-neutral-600">—</span>}
                  </td>
                  <td className="py-1.5 pl-2">
                    <div className="flex items-center gap-1.5">
                      <input type="number" step="0.5" min={lo} max={hi}
                        value={jointDraft[j] ?? ''}
                        onChange={(e) => setJointDraft(
                          (d) => ({ ...d, [j]: e.target.value }))}
                        placeholder={String(limits.warn_nm)}
                        disabled={busy}
                        className="w-20 rounded border border-neutral-600 bg-neutral-900 px-1.5 py-0.5
                                   text-xs text-neutral-100 disabled:opacity-50" />
                      {eff && <span className="text-[10px] text-neutral-500">
                        ≈{eff.a.toFixed(1)}A
                      </span>}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <button
          onClick={() => void send({ per_joint: jointPatch() })}
          disabled={busy || !jointsValid || !jointsDirty}
          className="rounded bg-blue-600 px-3 py-1 text-xs text-white
                     hover:bg-blue-500 disabled:opacity-40">
          관절별 임계 적용
        </button>
      </div>

      {!status && (
        <p className="text-xs text-neutral-500">
          이 팔의 부하를 아직 못 읽습니다 — 연결돼 있고 전원이 켜져 있어야
          0x251~0x256 고속 피드백이 옵니다.
        </p>
      )}

      <div className="space-y-1 rounded border border-neutral-700 bg-neutral-900/60 px-3 py-2
                      text-xs leading-relaxed text-neutral-400">
        <p className="text-amber-400/80">
          기본 임계 {limits.default_nm}N·m 는 <b>잠정값</b>입니다. 관측한 것은 둘뿐입니다 —
          J5 가 기구 한계에 걸렸을 때 드라이버가 10.3A 에서 스스로 끊었고,
          J2 를 손으로 눌렀을 때는 <b>11.4A(13.5N·m)까지 갔는데 안 끊었습니다.</b>
          같은 전류라도 관절군마다 토크가 다르므로(J1~3 ×1.18125, J4~6 ×0.95844)
          여섯 관절에 한 숫자를 쓸 근거는 아직 없습니다.
        </p>
        <p>
          진짜 임계는 위 <b>최대</b> 칸에 쌓이는 기록과, 리셋이 재는 실제 슬립각을
          맞대어 정합니다. 경보가 떴는데 슬립이 0 이면 임계가 낮은 것이고,
          경보 없이 밀렸으면 임계가 높은 것입니다.
        </p>
        <p>
          감시를 꺼도 <b>기록은 계속됩니다</b> — 끄는 것은 &quot;울리지 마라&quot;지
          &quot;재지 마라&quot;가 아닙니다.
        </p>
      </div>
    </div>
  )
}
