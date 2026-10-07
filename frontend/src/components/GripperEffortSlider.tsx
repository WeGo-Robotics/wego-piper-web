import { useEffect, useRef, useState } from 'react'
import { api } from '../services/api'

/**
 * 그리퍼 힘(토크) 슬라이더 — 팔 하나에 하나 (2026-10-07).
 *
 * Piper 그리퍼 명령은 **프레임마다** 힘을 싣는다. 값은 robotd 가 들고(`gripper_store`)
 * 다음 프레임부터 그 힘이므로, 추론 도중 끌어도 바로 반영된다.
 *
 * ⚠ 녹화 중에는 백엔드가 거절한다 — 한 데이터셋 안에서 잡는 힘이 바뀌면 안 된다.
 *   그때는 `locked` 로 잠그고 이유를 말한다(눌러 보고 거절당하게 두지 않는다).
 */
type Effort = { iface: string; effort_nm: number; min_nm: number; max_nm: number; default_nm: number }

export default function GripperEffortSlider({ iface, label, locked }: {
  iface: string
  label?: string
  locked?: string | false
}) {
  const [e, setE] = useState<Effort | null>(null)
  // 못 읽으면(지원 안 하는 팔·옛 게이트웨이·robotd 없음) **아예 안 그린다** — 빈 슬라이더에
  // "Not Found" 를 다는 것보다 없는 편이 덜 헷갈린다(사용자 요청 2026-10-07)
  const [unsupported, setUnsupported] = useState(false)
  const [val, setVal] = useState(1)
  const [err, setErr] = useState('')
  const timer = useRef<number | null>(null)

  useEffect(() => {
    setE(null); setErr(''); setUnsupported(false)
    if (!iface) return
    api.get<Effort>(`/robots/gripper-effort?iface=${encodeURIComponent(iface)}`)
      .then((r) => { setE(r); setVal(r.effort_nm) })
      .catch(() => setUnsupported(true))
    return () => { if (timer.current) window.clearTimeout(timer.current) }
  }, [iface])

  // 끄는 동안 바로 보내되 매 픽셀 보내지는 않는다 — 마지막 값은 반드시 나간다(트레일링)
  const push = (v: number) => {
    setVal(v)
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      api.post<Effort>('/robots/gripper-effort', { iface, effort_nm: v })
        .then((r) => { setE(r); setVal(r.effort_nm); setErr('') })
        .catch((x) => setErr(x instanceof Error ? x.message : '그리퍼 힘 변경 실패'))
    }, 150)
  }

  if (!iface || unsupported) return null
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-3 text-sm">
        <span className="shrink-0 text-neutral-400">{label ?? '그리퍼 힘'}</span>
        <input type="range" className="flex-1 accent-blue-500"
          min={e?.min_nm ?? 0} max={e?.max_nm ?? 5} step={0.1} value={val}
          disabled={!e || !!locked}
          onChange={(ev) => push(Number(ev.target.value))} />
        <span className="w-20 shrink-0 text-right font-mono text-xs text-neutral-200">
          {val.toFixed(1)} N·m
        </span>
        {e && Math.abs(val - e.default_nm) > 1e-6 && !locked && (
          <button onClick={() => push(e.default_nm)}
            className="shrink-0 text-[11px] text-neutral-500 hover:text-neutral-200"
            title={`기본값 ${e.default_nm.toFixed(1)} N·m 로`}>기본</button>
        )}
      </div>
      {locked && <p className="text-[11px] text-amber-400">{locked}</p>}
      {err && <p className="text-[11px] text-red-400">{err}</p>}
    </div>
  )
}
