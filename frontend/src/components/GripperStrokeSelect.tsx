import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 그리퍼 행정 선택 — 팔 하나에 하나 (2026-10-08).
 *
 * Piper 그리퍼는 소형 70mm / 대형 100mm 두 가지고 리더·팔로워에 섞여 달린다. 정규화 값(0..100)은
 * **행정에 대한 비율**이라, 대형에 소형 설정을 두면 "100" 이 68mm 에서 끝나 팔로워가
 * 토크로 버티며 끝까지 안 열린다. 값은 robotd 가 든다(`gripper_store`).
 *
 * **리더(티칭 핸들)는 그리퍼 모델이 아니다** — 자기 끝에서 멈춘다(실기: 끝까지 열어도 지령이 82.9mm,
 * 팔로워는 99.5mm). 그래서 리더에는 **열림 끝 맞추기**가 따로 있다: 끝까지 열고 누르면 그 값이
 * 리더의 "100" 이 된다. 팔로워에는 안 보인다(백엔드도 거절) — 팔로워의 끝에서 저장하면 매번 스톱을
 * 누른다.
 *
 * ⚠ 같은 값이 다른 물리 폭이 된다 — 추론·녹화·텔레옵 중에는 백엔드가 거절하고, 그 이유를
 *   그대로 보여 준다(눌러 보고 막히는 것까지는 화면이 모른다). 이 팔로 수집한 데이터·정책은
 *   **같은 행정의 그리퍼에서만** 맞다는 것을 화면이 말한다.
 */
type Stroke = {
  iface: string; stroke_mm: number; options_mm: number[]; default_mm: number
  /** 직접 맞춘 열림 끝(µm) — 없으면 null */
  end_um?: number | null
  /** 지금 변환에 쓰는 상한(µm) / 맞춤을 지우면 돌아갈 표 값(µm) */
  raw_max_um?: number; table_raw_max_um?: number
}

export default function GripperStrokeSelect({ iface, role }: { iface: string; role?: string }) {
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

  // 열림 끝 맞추기 — 리더만. 같은 오류 처리 (활동 중이면 409 사유를 그대로 보여 준다)
  const setEnd = (clear: boolean) => {
    if (busy) return
    setBusy(true); setErr('')
    api.post<Stroke>('/robots/gripper-stroke/end', { iface, clear })
      .then(setS)
      .catch((x) => setErr(x instanceof Error ? x.message : '열림 끝 변경 실패'))
      .finally(() => setBusy(false))
  }
  const mmOf = (um?: number | null) => (um == null ? '' : `${(um / 1000).toFixed(1)}mm`)

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
      {role === 'leader' && (
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="w-40 shrink-0 text-neutral-400">리더 열림 끝</span>
            <span className="font-mono text-neutral-200">
              {s?.end_um != null ? `${mmOf(s.end_um)} (직접 맞춤)`
                                 : `${mmOf(s?.table_raw_max_um)} (기본)`}
            </span>
            <button onClick={() => setEnd(false)} disabled={!s || busy}
              title="리더 그리퍼를 끝까지 연 채로 누르세요 — 지금 열림이 리더의 100 이 됩니다"
              className="rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-500 disabled:opacity-50">
              현재 열림을 끝으로 저장
            </button>
            {s?.end_um != null && (
              <button onClick={() => setEnd(true)} disabled={busy}
                className="text-[11px] text-neutral-500 hover:text-neutral-200"
                title="직접 맞춘 끝을 지우고 행정 기본값으로">초기화</button>
            )}
          </div>
          <p className="text-[11px] text-neutral-500">
            리더 핸들은 그리퍼보다 덜 열릴 수 있습니다. <b>리더를 끝까지 연 채로</b> 누르면 그 열림이
            리더의 100 이 되어, 팔로워도 끝까지 열립니다. 행정을 바꾸면 이 값은 지워집니다.
          </p>
        </div>
      )}
      {err && <p className="text-[11px] text-red-400">{err}</p>}
    </div>
  )
}
