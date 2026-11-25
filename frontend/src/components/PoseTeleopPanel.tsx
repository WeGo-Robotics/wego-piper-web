import { useCallback, useEffect, useRef, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'
import { useT } from '../i18n'

/**
 * 자세 조종 — 손에 든 6자유도 자세 소스(PIKA Sense 등)로 팔로워를 끈다 (feature/pika.md §5).
 *
 * 팔로워마다 소스 하나 — 양팔은 줄이 둘이다. 시작은 **해제 상태**다(팔은 그 자리). 손잡이를 팔 끝과
 * 같은 방향으로 쥔 뒤 [정합] 또는 손잡이를 두 번 쥐면 그때부터 따라온다 — 몸체 좌표 상대 매핑이라 쥔 방향이
 * 다르면 "앞" 이 팔에서 다른 쪽이 된다. 그래서 정합 전에 둘의 기울기를 나란히 보여 준다.
 *
 * 닫거나 페이지를 떠나면 이 패널이 연 세션은 끝난다 — 양팔·단팔 패널과 같은 규칙.
 */

type Source = {
  id: string; age_s: number | null; tracking_ok: boolean | null
  opening_mm: number | null; trigger_seq: number | null; used_by: string | null
}
type Tilt = { pitch: number; roll: number }
type Session = {
  source: string; follower: string; leader: string
  engaged: boolean; lost: boolean; blocked: string; note: string; catching_up: boolean
  goal: Record<string, number>; toggles: number
  align: { arm: Tilt; hand: Tilt | null }
  relay: { holding: boolean; sent: number; stale: boolean; blocked: string }
  extension: { title: string } | null; feedback: boolean
}
type Params = { min_cutoff: number; beta: number; scale: number }
type Status = {
  sessions: Session[]; params: Params
  ranges: Record<keyof Params, [number, number]>
  ended: { follower: string; source: string; ended: string }[]
}
type Follower = { iface: string; label: string; side?: string | null }

/** 정합 전 기울기가 이 안이면 맞춘 것으로 본다 — 손으로 맞출 수 있는 정도 */
const ALIGN_OK_DEG = 15

export default function PoseTeleopPanel({ followers, onClose }: {
  followers: Follower[]
  onClose: () => void
}) {
  const t = useT()
  const { notify } = useSystemMessage()
  const [sources, setSources] = useState<Source[]>([])
  const [st, setSt] = useState<Status | null>(null)
  const [pick, setPick] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')
  const [params, setParams] = useState<Params | null>(null)

  const read = useCallback(() => {
    api.get<Status>('/leader/pose/status').then((s) => {
      setSt(s)
      setParams((p) => p ?? s.params)
    }).catch(() => {})
  }, [])
  const readSources = useCallback(() => {
    api.get<{ sources: Source[] }>('/leader/pose/sources').then((r) => setSources(r.sources)).catch(() => {})
  }, [])
  useEffect(() => {
    read(); readSources()
    const a = setInterval(read, 500)
    const b = setInterval(readSources, 1500)
    return () => { clearInterval(a); clearInterval(b) }
  }, [read, readSources])

  const mine = (st?.sessions ?? []).filter((s) => followers.some((f) => f.iface === s.follower))
  const mineRef = useRef<string[]>([])
  mineRef.current = mine.map((s) => s.follower)
  // ⚠ 닫거나 화면을 떠나면 **반드시 끝낸다** — 남기면 손을 내려놓아도 팔이 계속 따라가고,
  //   녹화·추론은 "수동 조작 중" 으로 막힌다. 팔로워를 하나씩 준다(비우면 다른 화면의 세션까지 닫힌다)
  useEffect(() => () => {
    for (const f of mineRef.current) api.post('/leader/pose/stop', { follower: f }).catch(() => {})
  }, [])

  const fail = (e: unknown, fallback: string) =>
    notify({ level: 'error', source: t('자세 조종'), text: e instanceof Error ? e.message : fallback })

  const call = async (key: string, path: string, body: unknown, fallback: string) => {
    setBusy(key)
    try { await api.post(path, body, { timeoutMs: 30000 }); read(); readSources() } catch (e) { fail(e, fallback) }
    finally { setBusy('') }
  }

  // 파라미터 — 끌 때마다 보내지 않고 손을 뗀 뒤 한 번 (도는 세션에 바로 들어간다)
  const pushTimer = useRef<number | null>(null)
  const changeParam = (k: keyof Params, v: number) => {
    setParams((p) => (p ? { ...p, [k]: v } : p))
    if (pushTimer.current) window.clearTimeout(pushTimer.current)
    pushTimer.current = window.setTimeout(() => {
      api.post('/leader/pose/params', { [k]: v }).catch((e) => fail(e, t('파라미터를 바꾸지 못했습니다')))
    }, 250)
  }

  // 좌우가 정해진 팔은 이름 끝(_l·_r)이 같은 쪽인 소스를 먼저 민다
  const defaultSource = (f: Follower): string => {
    const free = sources.filter((s) => !s.used_by)
    const suffix = f.side === 'left' ? '_l' : f.side === 'right' ? '_r' : ''
    return (suffix && free.find((s) => s.id.endsWith(suffix))?.id) || free[0]?.id || ''
  }

  const tiltText = (x: Tilt) => t('기울기 {p}° · 옆 {r}°', { p: x.pitch.toFixed(0), r: x.roll.toFixed(0) })
  const aligned = (a: Session['align']) => !!a.hand
    && Math.abs(a.arm.pitch - a.hand.pitch) <= ALIGN_OK_DEG && Math.abs(a.arm.roll - a.hand.roll) <= ALIGN_OK_DEG

  const stateOf = (s: Session): { text: string; cls: string } => {
    if (!s.relay.holding) return { text: t('릴레이가 팔로워를 놓았습니다'), cls: 'text-amber-300' }
    if (!s.engaged) {
      if (s.note) return { text: s.note, cls: 'text-amber-300' }
      return { text: t('○ 해제 — 팔은 그 자리에 있습니다. 손잡이를 맞춘 뒤 정합하세요'), cls: 'text-neutral-400' }
    }
    if (s.blocked) return { text: t('막힘: {why}', { why: s.blocked }), cls: 'text-amber-300' }
    if (s.catching_up) return { text: t('● 따라잡는 중 — 손이 빠릅니다'), cls: 'text-green-300' }
    return { text: t('● 정합 — 따라가는 중 · {sent}회', { sent: s.relay.sent }), cls: 'text-green-400' }
  }

  const sourceLabel = (s: Source) => {
    const bits = [s.id]
    if (s.age_s === null || s.age_s > 0.5) bits.push(t('자세 끊김'))
    else if (s.tracking_ok === false) bits.push(t('추적 부정확'))
    if (s.used_by) bits.push(t('{f} 가 씀', { f: s.used_by }))
    return bits.join(' · ')
  }

  const ended = (st?.ended ?? []).filter((e) => followers.some((f) => f.iface === e.follower)
    && !mine.some((s) => s.follower === e.follower))

  return (
    <div className="rounded border border-teal-500/40 bg-teal-500/5 p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold text-neutral-200">
          {t('자세 조종')}
          <span className="ml-2 font-normal text-neutral-500">{t('손잡이(PIKA Sense 등) → 팔 끝')}</span>
        </span>
        <button onClick={onClose} className="text-xs text-neutral-400 hover:text-white">{t('닫기')}</button>
      </div>

      {sources.length === 0 && (
        <p className="text-xs text-amber-300">
          {t('자세 소스가 없습니다 — 서비스 화면에서 장치 확장(PIKA)을 추가하고, 손잡이가 기지국에 보이는지 확장 화면에서 확인하세요.')}
        </p>
      )}

      {followers.map((f) => {
        const s = mine.find((x) => x.follower === f.iface)
        const side = f.side === 'left' ? t('왼팔') : f.side === 'right' ? t('오른팔') : ''
        if (!s) {
          const val = pick[f.iface] ?? defaultSource(f)
          return (
            <div key={f.iface} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <span className="shrink-0 font-mono text-neutral-200">{f.label}</span>
              {side && <span className="text-neutral-500">{side}</span>}
              <select value={val} onChange={(e) => setPick((p) => ({ ...p, [f.iface]: e.target.value }))}
                className="min-w-0 flex-1 rounded bg-neutral-700 border border-neutral-600 px-2 py-1">
                {sources.length === 0 && <option value="">{t('소스 없음')}</option>}
                {sources.map((x) => (
                  <option key={x.id} value={x.id} disabled={!!x.used_by}>{sourceLabel(x)}</option>
                ))}
              </select>
              <button onClick={() => void call(f.iface, '/leader/pose/start', { source: val, follower: f.iface },
                t('자세 조종을 시작하지 못했습니다'))}
                disabled={!!busy || !val}
                className="px-3 py-1 rounded bg-teal-700 hover:bg-teal-600 text-white disabled:opacity-40">
                {busy === f.iface ? '…' : t('시작')}
              </button>
            </div>
          )
        }
        const state = stateOf(s)
        const a = s.align
        return (
          <div key={f.iface} className="rounded border border-neutral-700 bg-neutral-900/40 p-2 space-y-1.5 text-xs">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="shrink-0 font-mono text-neutral-200">{s.source} → {s.follower}</span>
              {side && <span className="text-neutral-500">{side}</span>}
              <span className={`min-w-0 flex-1 ${state.cls}`}>{state.text}</span>
            </div>
            {s.lost && <p className="text-amber-400">{t('추적 상실 — 손잡이가 기지국에 안 보입니다. 팔은 마지막 자리에 서 있습니다')}</p>}
            {!s.engaged && (
              <p className={aligned(a) ? 'text-green-400' : 'text-neutral-400'}>
                {t('팔 끝')} {tiltText(a.arm)} · {t('손잡이')} {a.hand ? tiltText(a.hand) : '—'}
                {aligned(a) ? ` ✓` : ` — ${t('손잡이를 팔 끝과 같은 방향으로 쥐세요')}`}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              {s.engaged ? (
                <button onClick={() => void call(f.iface, '/leader/pose/disengage', { follower: f.iface }, t('해제하지 못했습니다'))}
                  disabled={!!busy}
                  className="px-3 py-1 rounded bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-40">
                  {t('해제')}
                </button>
              ) : (
                <button onClick={() => void call(f.iface, '/leader/pose/engage', { follower: f.iface }, t('정합하지 못했습니다'))}
                  disabled={!!busy || s.lost}
                  className="px-3 py-1 rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-40">
                  {t('정합')}
                </button>
              )}
              <button onClick={() => void call(f.iface, '/leader/pose/stop', { follower: f.iface }, t('멈추지 못했습니다'))}
                disabled={!!busy}
                className="px-3 py-1 rounded bg-red-600 hover:bg-red-500 text-white disabled:opacity-40">
                {t('정지')}
              </button>
              <span className="text-[10px] text-neutral-500">
                {t('손잡이를 두 번 쥐어도 정합·해제됩니다')}
                {s.feedback ? ` · ${t('막히면 손잡이가 떨립니다')}` : ''}
              </span>
            </div>
          </div>
        )
      })}

      {ended.map((e) => (
        <p key={e.follower} className="text-xs text-amber-300">{t('{f}: 자세 조종이 끝났습니다 — {why}', { f: e.follower, why: e.ended })}</p>
      ))}

      {params && st && (
        <div className="grid grid-cols-1 gap-1 pt-1 sm:grid-cols-3 sm:gap-3">
          {([['min_cutoff', t('정지 떨림 컷오프 (Hz)'), 0.1], ['beta', t('빠를 때 반응 (beta)'), 0.1],
             ['scale', t('위치 배율'), 0.05]] as const).map(([k, label, step]) => (
            <label key={k} className="block text-[11px] text-neutral-400">
              {label} <span className="font-mono text-neutral-200">{params[k].toFixed(2)}</span>
              <input type="range" min={st.ranges[k][0]} max={st.ranges[k][1]} step={step} value={params[k]}
                onChange={(e) => changeParam(k, Number(e.target.value))} className="w-full" />
            </label>
          ))}
        </div>
      )}
      <p className="text-[10px] leading-relaxed text-neutral-500">
        {t('시작하면 팔은 그 자리에 서 있습니다. 손잡이를 팔 끝과 같은 방향으로 쥐고 정합하면 그때부터 손의 움직임이 팔 끝으로 갑니다. 해제하고 손을 옮겨 다시 정합하면 작업 공간을 이어 씁니다. 추적을 잃으면 팔은 마지막 자리에 서고, 자세가 크게 튀면 해제됩니다. 필터를 낮추면(컷오프↓) 정지 떨림이 줄고 반응이 느려집니다. 도는 동안 녹화·추론은 막히고, 닫거나 페이지를 떠나면 멈춥니다.')}
      </p>
    </div>
  )
}
