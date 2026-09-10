import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'

/**
 * 시스템 로그 — 데몬 저널을 SSH 없이 (설정 → 로그).
 *
 * 컨테이너 게이트웨이는 호스트 저널을 못 읽는다 — unitd 가 읽어 준다. 데몬은 stdout
 * 으로 찍어 journald 우선순위가 전부 info 라, 레벨은 백엔드가 메시지 토큰으로 가른다.
 * 기본은 **경고 이상**만: "무슨 오류가 났나"가 질문이다. 전부 보려면 고른다.
 */

type Entry = { t: number; t_last?: number; count?: number; unit: string; level: 'error' | 'warning' | 'info' | 'debug'; msg: string }
type Result = { entries: Entry[]; scanned: number; source: string; truncated: boolean; partial?: boolean; via: string }

type UnitOpt = { id: string; label: string }

/** ⚠ **목록은 서버가 준다.** 예전엔 여기 배열을 손으로 들었는데, 카탈로그에 데몬을
 *  더하면(`so101d`·`simd` 를 더했던 것처럼) 백엔드는 따라가고 화면은 안 따라간다 —
 *  새 데몬 로그를 개별로 못 보는데 에러도 안 나서 아무도 눈치 못 챈다. 실제로
 *  `frontend` 가 그렇게 빠져 있었다: 백엔드는 받아주는데 고를 수가 없었다.
 *
 *  못 받았을 때만 쓰는 최소 폴백 — 서버가 답할 때까지 셀렉트가 비면 안 된다. */
const FALLBACK_UNITS: UnitOpt[] = [{ id: 'all', label: '전체 piper-*' }]
const SINCE: { id: string; label: string }[] = [
  { id: '1 hour ago', label: '최근 1시간' }, { id: '6 hours ago', label: '최근 6시간' },
  { id: 'today', label: '오늘' }, { id: '', label: '기간 제한 없음' },
]
const LEVEL_CLASS: Record<Entry['level'], string> = {
  error: 'text-red-300', warning: 'text-amber-300', info: 'text-neutral-300', debug: 'text-neutral-500',
}
const ts = (t: number) => t ? new Date(t * 1000).toLocaleTimeString('ko-KR', { hour12: false }) : '--:--:--'
const day = (t: number) => t ? new Date(t * 1000).toLocaleDateString('ko-KR', { month: '2-digit', day: '2-digit' }) : ''

export default function SystemLogPanel({ initialUnit }: { initialUnit?: string }) {
  const [unit, setUnit] = useState(initialUnit ?? 'all')
  const [level, setLevel] = useState<'error' | 'warning' | 'info'>('warning')
  const [since, setSince] = useState('6 hours ago')
  const [lines, setLines] = useState(300)
  const [query, setQuery] = useState('')
  const [follow, setFollow] = useState(false)
  const [res, setRes] = useState<Result | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [units, setUnits] = useState<UnitOpt[]>(FALLBACK_UNITS)
  const endRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let live = true
    api.get<{ units: UnitOpt[] }>('/system/log-units')
      .then((r) => { if (live && r.units?.length) setUnits(r.units) })
      .catch(() => { /* 폴백으로 둔다 — 목록을 못 받아도 전체는 볼 수 있다 */ })
    return () => { live = false }
  }, [])

  useEffect(() => { if (initialUnit) setUnit(initialUnit) }, [initialUnit])

  const load = useCallback(async () => {
    setBusy(true)
    try {
      const q = new URLSearchParams({ unit, lines: String(lines), level })
      if (since) q.set('since', since)
      setRes(await api.get<Result>(`/system/logs?${q}`)); setErr('')
    } catch (e) { setErr(e instanceof Error ? e.message : '로그를 읽지 못했습니다') }
    finally { setBusy(false) }
  }, [unit, lines, level, since])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!follow) return
    const iv = window.setInterval(load, 3000)
    return () => window.clearInterval(iv)
  }, [follow, load])
  useEffect(() => { if (follow) endRef.current?.scrollIntoView({ block: 'end' }) }, [res, follow])

  const q = query.trim().toLowerCase()
  const shown = (res?.entries ?? []).filter((e) => !q || e.msg.toLowerCase().includes(q) || e.unit.includes(q))
  const dl = `/api/system/logs?${new URLSearchParams({ unit, lines: String(lines), level, ...(since ? { since } : {}), format: 'text' })}`

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">로그</h2>
          <p className="mt-1 text-xs text-neutral-400">
            데몬 저널을 그대로 — 호스트의 서비스 관리 데몬(piper-unitd)이 읽어 줍니다. 기본은 <b>경고 이상</b>만.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <select value={unit} onChange={(e) => setUnit(e.target.value)} className="rounded bg-neutral-700 border border-neutral-600 px-2 py-1">
            {units.map((u) => <option key={u.id} value={u.id}>{u.label}</option>)}
          </select>
          <select value={level} onChange={(e) => setLevel(e.target.value as typeof level)} className="rounded bg-neutral-700 border border-neutral-600 px-2 py-1">
            <option value="error">오류만</option><option value="warning">경고 이상</option><option value="info">전부</option>
          </select>
          <select value={since} onChange={(e) => setSince(e.target.value)} className="rounded bg-neutral-700 border border-neutral-600 px-2 py-1">
            {SINCE.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
          <select value={lines} onChange={(e) => setLines(Number(e.target.value))} className="rounded bg-neutral-700 border border-neutral-600 px-2 py-1">
            {[100, 300, 1000, 3000].map((n) => <option key={n} value={n}>{n}줄</option>)}
          </select>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="찾기"
            className="w-32 rounded bg-neutral-700 border border-neutral-600 px-2 py-1" />
          <label className="flex items-center gap-1 text-neutral-400">
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-blue-500" /> 따라가기
          </label>
          <button onClick={load} disabled={busy} className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-50">
            {busy ? '읽는 중…' : '새로고침'}
          </button>
          <a href={dl} download className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600">내려받기</a>
        </div>
      </div>

      {err && <p className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">{err}</p>}
      {res && (
        <p className="text-[11px] text-neutral-500">
          {shown.length}줄 표시 · {res.scanned}줄 훑음{res.truncated ? ' · 더 있음 (줄 수를 늘리거나 기간을 좁히세요)' : ''}{res.partial ? ' · 시간 예산(8초) 안에 훑은 만큼 — 최신부터입니다. 기간을 좁히면 다 봅니다' : ''}
          {res.source === 'docker' ? ' · 컨테이너 로그' : ''}{res.via === 'local' ? ' · 게이트웨이가 직접 읽음' : ''}
        </p>
      )}
      <div className="max-h-[60vh] overflow-auto rounded border border-neutral-700 bg-neutral-900/70 p-2 font-mono text-[11px] leading-5">
        {shown.length === 0 && !busy && (
          <p className="px-1 text-neutral-500">해당하는 줄이 없습니다{level !== 'info' ? ' — "전부"로 바꾸면 info 도 보입니다' : ''}.</p>
        )}
        {shown.map((e, i) => (
          <div key={i} className={`flex gap-2 whitespace-pre-wrap break-all ${LEVEL_CLASS[e.level] ?? 'text-neutral-300'} ${e.level === 'error' ? 'bg-red-500/5' : ''}`}>
            <span className="shrink-0 tabular-nums text-neutral-500">{day(e.t)} {ts(e.t)}</span>
            {unit === 'all' && <span className="shrink-0 w-16 truncate text-neutral-400">{e.unit}</span>}
            <span className={`shrink-0 w-7 uppercase ${e.level === 'error' ? 'text-red-400' : e.level === 'warning' ? 'text-amber-400' : 'text-neutral-600'}`}>
              {e.level === 'warning' ? 'WARN' : e.level === 'error' ? 'ERR' : e.level === 'debug' ? 'DBG' : 'INF'}
            </span>
            <span>{e.msg}</span>
            {(e.count ?? 1) > 1 && (
              <span className="shrink-0 rounded bg-neutral-700 px-1 text-[10px] text-neutral-300"
                title={e.t_last ? `마지막 ${ts(e.t_last)}` : undefined}>×{e.count}</span>
            )}
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  )
}
