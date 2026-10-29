import { useEffect, useState } from 'react'
import { api } from '../../services/api'
import { tb, useT } from '../../i18n'
import type { BoardSpec } from './types'

/**
 * 작업판 인쇄 (feature/scenario-designer.md §10) — 용지·모드·선 굵기를 고르고 PDF 를 새 탭에.
 *
 * PDF 안 문구는 여기서 옮겨 넘긴다 — 백엔드는 번역을 모르고 받은 문장을 그린다
 * (`services/scenario/printing.py` 머리말). 자리표시자(`{mm}` 등)는 백엔드가 채운다.
 */

type Layout = { paper: string; orientation: string; cols: number; rows: number; pages: number }

type Props = { board: BoardSpec; title: string }

// 마지막 인쇄 설정 — 설계와 같이 브라우저에 기억한다
const STORE_KEY = 'piper_scenario_print'
type Stored = { paper?: 'a4' | 'a3'; mode?: 'faint' | 'bold' | 'overlay'; lineMm?: number }

function loadStored(): Stored {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') ?? {} } catch { return {} }
}

export default function PrintPanel({ board, title }: Props) {
  const t = useT()
  const [stored] = useState(loadStored)
  const [paper, setPaper] = useState<'a4' | 'a3'>(stored.paper === 'a3' ? 'a3' : 'a4')
  const [mode, setMode] = useState<'faint' | 'bold' | 'overlay'>(
    stored.mode === 'bold' || stored.mode === 'overlay' ? stored.mode : 'faint')
  const [lineMm, setLineMm] = useState(typeof stored.lineMm === 'number' ? stored.lineMm : 0.25)
  useEffect(() => {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ paper, mode, lineMm })) } catch { /* 사생활 모드 */ }
  }, [paper, mode, lineMm])
  const [layout, setLayout] = useState<Layout | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const body = () => ({
    board, paper, mode, line_mm: lineMm, title,
    labels: {
      robot: t('로봇 쪽'),
      gap: t('로봇 베이스 중심까지 {mm} mm'),
      scale: t("{mm} mm — 실제 크기(100%)로 인쇄, '페이지에 맞춤' 끄기"),
      fold: t('이 띠는 접거나 잘라도 됩니다 — 보정할 때만 펼치세요'),
      page: t('{n}/{total} 장 ({row}행 {col}열)'),
    },
  })

  useEffect(() => {
    let alive = true
    api.post<Layout>('/scenario/print/layout', { board, paper, mode })
      .then(l => { if (alive) { setLayout(l); setErr('') } })
      .catch(e => { if (alive) { setLayout(null); setErr((e as Error).message) } })
    return () => { alive = false }
  }, [board, paper, mode])

  const open = async () => {
    setBusy(true)
    setErr('')
    // 새 탭은 클릭 순간에 연다 — await 뒤에 열면 팝업 차단에 걸린다
    const win = window.open('', '_blank')
    try {
      const res = await fetch('/api/scenario/print', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body()),
      })
      if (!res.ok) {
        let detail = `${res.status}`
        try { detail = tb((await res.json()).detail) ?? detail } catch { /* 본문이 JSON 이 아니다 */ }
        throw new Error(detail)
      }
      const url = URL.createObjectURL(await res.blob())
      if (win) win.location.href = url
      else window.open(url, '_blank')
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (e) {
      win?.close()
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const modes: [typeof mode, string, string][] = [
    ['faint', t('흐린 판 (기본)'), t('사람은 보고 카메라엔 안 보이게 — 가는 연회색 선, 칸 이름은 둘레 띠에만. 배포 때도 같은 판을 깔아 둡니다')],
    ['bold', t('진한 판'), t('검정 선, 칸 이름이 칸 안에 — 배치 속도 우선. 판이 영상에 찍히므로 배포 때도 깔아야 합니다')],
    ['overlay', t('겹쳐보기 전용'), t('윤곽·태그·자만 찍는 보정 카드 — 보정 뒤 치우고 카메라 화면을 보며 놓습니다')],
  ]

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        {modes.map(([k, title, desc]) => (
          <label key={k} className="flex gap-2 items-start text-sm cursor-pointer">
            <input type="radio" name="printMode" checked={mode === k} onChange={() => setMode(k)}
              className="mt-1 accent-blue-500" />
            <span>
              <span className="text-neutral-200">{title}</span>
              <span className="block text-xs text-neutral-500">{desc}</span>
            </span>
          </label>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-neutral-400">{t('용지')}
          <select value={paper} onChange={e => setPaper(e.target.value as 'a4' | 'a3')}
            className="block px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100">
            <option value="a4">A4</option>
            <option value="a3">A3</option>
          </select>
        </label>
        {mode === 'faint' && (
          <label className="text-xs text-neutral-400"
            title={t('카메라 한 픽셀이 바닥에서 덮는 폭의 1/3 이하가 기준입니다 (탑 카메라 640px 가 600mm 를 보면 약 0.3mm)')}>
            {t('선 굵기 (mm)')}
            <input type="number" min={0.1} max={1} step={0.05} value={lineMm}
              onChange={e => setLineMm(Math.max(0.1, Math.min(1, Number(e.target.value) || 0.25)))}
              className="block w-24 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
          </label>
        )}
        {layout && (
          <span className="text-sm text-neutral-300 pb-1">
            {t('{paper} {orientation} {cols}×{rows} = {pages}장', {
              paper: layout.paper.toUpperCase(),
              orientation: layout.orientation === 'landscape' ? t('가로') : t('세로'),
              cols: layout.cols, rows: layout.rows, pages: layout.pages,
            })}
          </span>
        )}
        <button onClick={open} disabled={busy}
          className="px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-sm text-white disabled:opacity-50">
          {busy ? t('만드는 중...') : t('PDF 열기')}
        </button>
      </div>
      <p className="text-xs text-neutral-500">
        {t("인쇄할 때 '실제 크기(100%)' 로 — '페이지에 맞춤' 이 켜져 있으면 칸이 줄어듭니다. 띠의 100mm 자를 대 보면 바로 압니다.")}
      </p>
      {err && <p className="text-xs text-red-400">{err}</p>}
    </div>
  )
}
