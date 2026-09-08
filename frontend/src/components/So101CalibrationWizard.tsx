/**
 * SO-101 캘리브레이션 위저드 — lerobot-calibrate 를 UI 로 옮기되 한 단계 줄인 것.
 *
 * 열리면 begin(토크 해제 + 서보 공장 초기화) 후 바로 범위 스윕: 각 관절을
 * 양 끝까지 움직이면 데몬이 60Hz 로 min/max 를 추적하고(0/4095 롤오버는
 * 언랩으로 이어붙인다), **중앙은 [저장]에서 (min+max)/2 로 자동 산출**된다 —
 * 사람이 중앙 자세를 눈대중으로 잡는 단계가 없다.
 *
 * 판정(스팬 충분한가)은 데몬이 한다 — 여기는 그리기만. 실패 문구도 데몬 것
 * 그대로 보여준다 (백엔드가 문구를 만든다는 저장소 규칙).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'

type CalibStatus = {
  arm: string
  stage: 'range' | null
  raw: Record<string, number | null>
  min: Record<string, number>
  max: Record<string, number>
  spans: Record<string, number>
  min_span: number
  ok: Record<string, boolean>
  calibrated: boolean
}

// ── 원형 게이지 — 관절은 원이다. 직선 막대는 0/4095 롤오버에서 두 동강
// 나고(실기: 4096→0 반대 범위 관절), 마커가 끝에서 끝으로 점프한다.
// 원 위의 호는 어느 방향으로 돌든, 어디서 0 을 지나든 그냥 이어진다.

const polar = (cx: number, cy: number, r: number, deg: number): [number, number] => {
  const a = ((deg - 90) * Math.PI) / 180
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)]
}

const tickDeg = (t: number) => (((t % 4096) + 4096) % 4096) / 4096 * 360

function arcPath(cx: number, cy: number, r: number, startDeg: number, deltaDeg: number): string {
  // 한 바퀴를 다 돌면 arc 명령이 퇴화한다 — 반씩 두 번 그린다
  if (deltaDeg >= 359.9) {
    const [x1, y1] = polar(cx, cy, r, startDeg)
    const [x2, y2] = polar(cx, cy, r, startDeg + 180)
    return `M ${x1} ${y1} A ${r} ${r} 0 1 1 ${x2} ${y2} A ${r} ${r} 0 1 1 ${x1} ${y1}`
  }
  const [x1, y1] = polar(cx, cy, r, startDeg)
  const [x2, y2] = polar(cx, cy, r, startDeg + deltaDeg)
  return `M ${x1} ${y1} A ${r} ${r} 0 ${deltaDeg > 180 ? 1 : 0} 1 ${x2} ${y2}`
}

function JointGauge({ label, ok, mn, mx, cur, span }: {
  label: string; ok: boolean
  mn?: number; mx?: number; cur?: number | null; span: number
}) {
  const has = mn !== undefined && mx !== undefined
  const delta = has ? Math.min(360, ((mx! - mn!) / 4096) * 360) : 0
  const dot = cur != null ? polar(36, 36, 28, tickDeg(cur)) : null
  return (
    <div className="flex flex-col items-center gap-0.5">
      <svg width="72" height="72" viewBox="0 0 72 72">
        <circle cx="36" cy="36" r="28" fill="none" stroke="#404040" strokeWidth="6" />
        {has && delta > 0.5 && (
          <path d={arcPath(36, 36, 28, tickDeg(mn!), delta)} fill="none"
                stroke={ok ? '#22c55e' : '#f59e0b'} strokeWidth="6"
                strokeLinecap="round" opacity="0.85" />
        )}
        {dot && <circle cx={dot[0]} cy={dot[1]} r="4" fill="#fff" />}
      </svg>
      <span className={`text-[11px] ${ok ? 'text-green-400' : 'text-neutral-400'}`}>
        {ok ? '✓' : '…'} {label}
      </span>
      <span className="text-[10px] font-mono text-neutral-500">{span}</span>
    </div>
  )
}

const JOINT_LABELS: Record<string, string> = {
  shoulder_pan: '어깨 팬', shoulder_lift: '어깨 리프트', elbow_flex: '팔꿈치',
  wrist_flex: '손목 굽힘', wrist_roll: '손목 롤', gripper: '그리퍼',
}

export default function So101CalibrationWizard({ arm, onClose }: {
  arm: string
  onClose: () => void
}) {
  const [st, setSt] = useState<CalibStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const started = useRef(false)

  const step = useCallback(async (s: 'begin' | 'save' | 'cancel') => {
    setBusy(true)
    setError('')
    try {
      const r = await api.post<CalibStatus>('/robots/serial/calib', { arm, step: s })
      setSt(r)
      return r
    } catch (e) {
      setError(e instanceof Error ? e.message : `${s} 실패`)
      return null
    } finally { setBusy(false) }
  }, [arm])

  // 열리면 시작. StrictMode 이중 마운트에 begin 을 두 번 치지 않는다 —
  // 두 번째 begin 이 첫 호밍을 다시 0 으로 되돌린다.
  useEffect(() => {
    if (started.current) return
    started.current = true
    step('begin')
  }, [step])

  // 진행 중엔 300ms 폴링 — 사람이 팔을 움직이는 걸 실시간으로 봐야 한다
  useEffect(() => {
    if (!st?.stage) return
    const iv = setInterval(() => {
      api.get<CalibStatus>(`/robots/serial/calib/status?arm=${encodeURIComponent(arm)}`)
        .then(setSt).catch(() => {})
    }, 300)
    return () => clearInterval(iv)
  }, [arm, st?.stage])

  const handleClose = async () => {
    if (st?.stage) await step('cancel')
    onClose()
  }

  const joints = Object.keys(JOINT_LABELS)
  const allOk = st ? joints.every((j) => st.ok[j]) : false

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-lg rounded-lg border border-neutral-600 bg-neutral-800 p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">SO-101 캘리브레이션 — {arm}</h3>
          <button onClick={handleClose} className="text-xs text-neutral-400 hover:text-white">
            {st?.stage ? '중단' : '닫기'}
          </button>
        </div>

        {!st && !error && <p className="text-xs text-neutral-400">시작 중…</p>}

        {st?.stage === 'range' && (
          <div className="space-y-3">
            <p className="text-xs text-neutral-300 leading-relaxed">
              토크가 풀려 있습니다 — 팔을 잡은 채로 <b>각 관절을 양 끝까지
              천천히</b> 움직여 주세요 (그리퍼는 완전 개폐). 호가 실제로 훑은
              범위입니다 — <b>도는 방향은 상관없고</b>, 중앙은 저장할 때 자동으로
              계산됩니다.
            </p>
            <div className="grid grid-cols-3 gap-2">
              {joints.map((j) => (
                <JointGauge key={j} label={JOINT_LABELS[j]} ok={!!st.ok[j]}
                  mn={st.min[j]} mx={st.max[j]} cur={st.raw[j]}
                  span={st.spans[j] ?? 0} />
              ))}
            </div>
            <button onClick={() => step('save')} disabled={busy || !allOk}
              title={allOk ? 'EEPROM 리밋 + 캘리브레이션 파일 저장'
                : '아직 충분히 안 움직인 관절이 있습니다'}
              className="w-full px-3 py-1.5 text-xs rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-40">
              저장
            </button>
          </div>
        )}

        {st && !st.stage && st.calibrated && (
          <div className="space-y-3">
            <p className="text-xs text-green-400">
              ✓ 저장됐습니다 — 서보 리밋과 캘리브레이션 파일(LeRobot 호환)이
              구워졌고, 발행 값이 즉시 새 캘리브레이션을 씁니다.
            </p>
            <button onClick={onClose}
              className="w-full px-3 py-1.5 text-xs rounded bg-neutral-600 hover:bg-neutral-500 text-white">
              닫기
            </button>
          </div>
        )}

        {error && <p className="text-xs text-red-400 whitespace-pre-wrap">{error}</p>}
      </div>
    </div>
  )
}
