import { useEffect, useState } from 'react'
import { api } from '../services/api'
import { useT } from '../i18n'

/**
 * 실시간 조명 표시 — 수집·추론 화면에서 프리뷰 옆에 놓는다
 * (feature/lighting-watch.md §5 소비자 "수집·추론 화면").
 *
 * 게이트웨이 샘플러가 2초마다 재는 값의 REST 미러를 같은 주기로 폴링한다.
 * **판정은 여기서 안 한다** — 급변 경보는 장치 경보 경로(토스트+벨)로 온다.
 * 여기는 "지금 몇인가"를 보여줄 뿐이다. 카메라가 없으면 아무것도 안 그린다.
 */

type LightInfo = {
  id: string; label: string; ts: number
  luma: number; sat_pct: number; dark_pct: number
  log_rg: number; log_bg: number
}

const fmt = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`

export default function LightStrip() {
  const t = useT()
  const [cams, setCams] = useState<LightInfo[]>([])

  useEffect(() => {
    const tick = () => api.get<{ cameras: LightInfo[] }>('/cameras/light')
      .then((r) => setCams(r.cameras)).catch(() => {})
    tick()
    const iv = setInterval(tick, 2000)
    return () => clearInterval(iv)
  }, [])

  if (cams.length === 0) return null

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-neutral-500">{t('조명')}</span>
      {cams.map((c) => (
        <span key={c.id}
              className="flex items-center gap-1.5 rounded-full border border-neutral-700
                         bg-neutral-800 px-2.5 py-0.5 tabular-nums"
              title={t('{label} — 밝기 {luma}/255 · 포화 {sat_pct}% · 암부 {dark_pct}%\n색 비율(log₂): R/G {log_rg} · B/G {log_bg}\n급변하면 경보(🔔)가 따로 뜹니다', { label: c.label, luma: c.luma, sat_pct: c.sat_pct, dark_pct: c.dark_pct, log_rg: fmt(c.log_rg), log_bg: fmt(c.log_bg) })}>
          <span className="max-w-[8rem] truncate text-neutral-300">{c.label}</span>
          <span aria-hidden>☀</span>
          <span className="text-neutral-200">{Math.round((c.luma / 255) * 100)}%</span>
          {(c.sat_pct > 5 || c.dark_pct > 50) && <span aria-hidden title={t('노출 이상')}>⚠</span>}
        </span>
      ))}
    </div>
  )
}
