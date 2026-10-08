import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 그리퍼 행정 선택 — 팔 하나에 하나 (2026-10-08).
 *
 * Piper 그리퍼는 소형 70mm / 대형 100mm 두 가지고 리더·팔로워에 섞여 달린다. 정규화 값(0..100)은
 * **행정에 대한 비율**이라, 대형에 소형 설정을 두면 "100" 이 68mm 에서 끝나 팔로워가
 * 토크로 버티며 끝까지 안 열린다. 값은 robotd 가 든다(`gripper_store`).
 *
 * ⚠ 같은 값이 다른 물리 폭이 된다 — 추론·녹화·텔레옵 중에는 백엔드가 거절하고, 그 이유를
 *   그대로 보여 준다(눌러 보고 막히는 것까지는 화면이 모른다). 이 팔로 수집한 데이터·정책은
 *   **같은 행정의 그리퍼에서만** 맞다는 것을 화면이 말한다.
 */
type Stroke = { iface: string; stroke_mm: number; options_mm: number[]; default_mm: number }

export default function GripperStrokeSelect({ iface }: { iface: string }) {
  const [s, setS] = useState<Stroke | null>(null)
  // 못 읽으면(지원 안 하는 팔·옛 게이트웨이·robotd 없음) 아예 안 그린다 — 슬라이더와 같은 규칙
  const [unsupported, setUnsupported] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    setS(null); setErr(''); setUnsupported(false)
    if (!iface) return
    api.get<Stroke>(`/robots/gripper-stroke?iface=${encodeURIComponent(iface)}`)
      .then(setS)
      .catch(() => setUnsupported(true))
  }, [iface])

  const choose = (mm: number) => {
    if (busy || !s || mm === s.stroke_mm) return
    setBusy(true); setErr('')
    api.post<Stroke>('/robots/gripper-stroke', { iface, stroke_mm: mm })
      .then(setS)
      .catch((x) => setErr(x instanceof Error ? x.message : '그리퍼 행정 변경 실패'))
      .finally(() => setBusy(false))
  }

  if (!iface || unsupported) return null
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-xs">
        <span className="w-40 shrink-0 text-neutral-400">그리퍼 행정</span>
        <div className="inline-flex overflow-hidden rounded border border-neutral-700">
          {(s?.options_mm ?? [70, 100]).map((mm) => (
            <button key={mm} onClick={() => choose(mm)} disabled={!s || busy}
              className={`px-3 py-1 text-xs disabled:opacity-50 ${
                s?.stroke_mm === mm ? 'bg-blue-600 text-white'
                                    : 'bg-neutral-800 text-neutral-300 hover:bg-neutral-700'}`}>
              {mm}mm
            </button>
          ))}
        </div>
        <span className="text-[11px] text-neutral-500">
          {s?.stroke_mm === s?.default_mm ? '소형 (기본)' : '대형'}
        </span>
      </div>
      <p className="text-[11px] text-neutral-500">
        달린 그리퍼와 맞춰야 "100" 이 끝까지 열림입니다. 다른 행정의 그리퍼로 수집한
        데이터·정책은 같은 값이 다른 폭이 됩니다.
      </p>
      {err && <p className="text-[11px] text-red-400">{err}</p>}
    </div>
  )
}
