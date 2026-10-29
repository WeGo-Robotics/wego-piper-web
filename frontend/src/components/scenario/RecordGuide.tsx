import { useEffect, useRef, useState } from 'react'
import { api } from '../../services/api'
import { useT } from '../../i18n'
import type { TFn } from '../../i18n'
import { useSystemMessage } from '../SystemMessages'
import BoardView from './BoardView'
import { hintText, variantLabel } from './PlanSummary'
import type { BoardSpec, CatalogItem, PlanEpisode } from './types'

/**
 * 수집 도우미 (feature/scenario-designer.md §9) — 계획을 따르는 녹화에서 지금 시도와 다음 배치.
 *
 * 어느 시도를 찍는지는 녹화 프로세스(wrapper)가 정하고 여기는 **읽기만** 한다 — ←/→ 와 경합하지
 * 않는다. [실패로 표시]·[다음 배치 미루기] 도 같은 제어 큐로 간다.
 *
 * 다음 배치는 **앞 시도가 끝난 판과의 차이**만 말한다(연쇄면 "그대로"). 앞이 실패·미루기라 판을
 * 알 수 없으면 판 전체를 그림대로 놓으라고 한다 — 그 계산은 게이트웨이(`scenario/progress.py`).
 */

export type Change = { obj: string; from: string | null; to: string | null }
export type TrialView = PlanEpisode & { relayout: boolean; changes: Change[] | null }

export type ScenarioProgress = {
  active: boolean
  recording?: boolean
  phase?: string
  plan_id?: string
  board?: BoardSpec
  catalog?: CatalogItem[]
  total?: number
  done?: number
  remaining?: number
  block?: { now: number | null; total: number }
  current?: TrialView | null
  next?: TrialView | null
  current_failed?: boolean
  rerecord?: boolean
  finished?: boolean
  session?: { saved: number; failed: number; deferred: number }
}

/** 녹화 중 진행을 1초마다 읽는다. `enabled` 가 꺼지면 마지막 값을 둔다(정지 직후 요약용). */
export function useScenarioProgress(enabled: boolean): ScenarioProgress | null {
  const [data, setData] = useState<ScenarioProgress | null>(null)
  useEffect(() => {
    if (!enabled) return
    let alive = true
    const tick = () => api.get<ScenarioProgress>('/recording/scenario')
      .then(d => { if (alive) setData(d) }).catch(() => {})
    tick()
    const id = setInterval(tick, 1000)
    return () => { alive = false; clearInterval(id) }
  }, [enabled])
  return data
}

function changeText(t: TFn, c: Change, name: (id: string) => string): string {
  if (!c.from) return t('{obj} 를 {to} 에 놓기', { obj: name(c.obj), to: c.to ?? '' })
  if (!c.to) return t('{obj} 를 {from} 에서 치우기', { obj: name(c.obj), from: c.from })
  return t('{obj} {from} → {to} 로 옮기기', { obj: name(c.obj), from: c.from, to: c.to })
}

/** 리셋에 들어갈 때 읽어 줄 문장 — 시연자는 리더 팔을 잡고 있어 화면을 볼 손이 없다 (§9-2). */
function speechFor(t: TFn, d: ScenarioProgress, name: (id: string) => string, zone: (id: string) => string): string {
  const n = d.next
  if (!n) return d.finished ? t('계획을 다 찍었습니다') : ''
  const parts: string[] = []
  if (d.rerecord) parts.push(t('다시 찍기 — 같은 배치로 되돌리세요'))
  if (n.relayout) {
    parts.push(t('다음: 판을 새로 늘어놓으세요. {list}', {
      list: n.layout.map(p => `${name(p.obj)} ${p.cell}`).join(', ') }))
  } else if (!d.rerecord) {
    parts.push(n.changes?.length
      ? t('다음: {list}', { list: n.changes.map(c => changeText(t, c, name)).join(', ') })
      : t('다음: 그대로'))
  } else if (n.changes?.length) {
    parts.push(n.changes.map(c => changeText(t, c, name)).join(', '))
  }
  if (n.hint) parts.push(hintText(t, n.hint, name, zone))
  return parts.join('. ')
}

type Props = {
  data: ScenarioProgress
  /** 리셋에 들어갈 때 다음 배치를 읽어 준다 */
  voice?: boolean
  /** 안내 창 — 그림을 크게 */
  big?: boolean
}

export default function RecordGuide({ data, voice = false, big = false }: Props) {
  const t = useT()
  const { notify } = useSystemMessage()
  const [busy, setBusy] = useState(false)
  const catalog = data.catalog ?? []
  const board = data.board
  const name = (id: string) => catalog.find(i => i.id === id)?.label ?? id
  const zone = (id: string) => board?.zones.find(z => z.id === id)?.label || id
  const resetting = data.phase === 'resetting'

  // 리셋에 들어가는 순간 한 번 — 같은 단계에 머무는 동안 되풀이하지 않는다
  const lastPhase = useRef(data.phase)
  useEffect(() => {
    const prev = lastPhase.current
    lastPhase.current = data.phase
    if (!voice || data.phase !== 'resetting' || prev === 'resetting' || !('speechSynthesis' in window)) return
    const text = speechFor(t, data, name, zone)
    if (!text) return
    const u = new SpeechSynthesisUtterance(text)
    u.lang = t.lang
    window.speechSynthesis.speak(u)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.phase, voice])

  const send = async (path: string, body?: unknown) => {
    setBusy(true)
    try { await api.post(path, body, { timeoutMs: 10_000 }) }
    catch (e) { notify({ id: 'scenario-guide', level: 'error', source: t('시나리오'), text: (e as Error).message }) }
    finally { setBusy(false) }
  }

  if (!board) return null
  const cur = data.current
  const nxt = data.next
  const btn = 'px-3 py-1.5 rounded text-sm disabled:opacity-40'

  const panel = (label: string, tv: TrialView | null | undefined, emphasis: boolean, isNext: boolean) => (
    <div className={`rounded-lg border p-3 space-y-2 ${emphasis ? 'border-sky-500 bg-sky-500/5' : 'border-neutral-700'}`}>
      <div className="flex items-baseline gap-2 text-xs">
        <span className={`font-semibold ${emphasis ? 'text-sky-300' : 'text-neutral-300'}`}>{label}</span>
        {tv && <span className="font-mono text-neutral-500">{tv.id}</span>}
        {tv && tv.variant !== 'normal' && <span className="text-amber-300">{variantLabel(t, tv.variant)}</span>}
        {tv && Object.entries(tv.nuisance ?? {}).map(([k, v]) => <span key={k} className="text-neutral-400">{k}: {v}</span>)}
      </div>
      {tv ? (
        <>
          <BoardView board={board} catalog={catalog} layout={tv.layout} picks={tv.picks} inZones={tv.in_zones}
            perturbTo={tv.hint?.kind === 'perturb' && tv.hint.cell && tv.hint.to
              ? { from: tv.hint.cell, to: tv.hint.to } : undefined}
            className={big ? 'max-h-[45vh]' : 'max-h-64'} />
          {!isNext && <p className={`${big ? 'text-xl' : 'text-sm'} text-neutral-100 break-words`}>“{tv.task}”</p>}
          {isNext && (
            tv.relayout
              ? <p className={`${big ? 'text-lg' : 'text-sm'} text-sky-300`}>{t('판 전체를 그림대로 놓으세요')}</p>
              : tv.changes && tv.changes.length
                ? <ul className={`${big ? 'text-lg' : 'text-sm'} text-sky-200 list-disc pl-5`}>
                    {tv.changes.map((c, i) => <li key={i}>{changeText(t, c, name)}</li>)}
                  </ul>
                : <p className={`${big ? 'text-lg' : 'text-sm'} text-neutral-300`}>{t('그대로 — 놓을 것 없음')}</p>
          )}
          {tv.hint && <p className={`${big ? 'text-lg' : 'text-xs'} text-amber-300`}>{hintText(t, tv.hint, name, zone)}</p>}
        </>
      ) : (
        <p className="text-sm text-neutral-500">
          {isNext ? (data.finished || !cur ? t('계획을 다 찍었습니다') : t('마지막 시도입니다')) : t('에피소드 시작을 기다리는 중')}
        </p>
      )}
    </div>
  )

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <h3 className="font-semibold">{t('시나리오')}</h3>
        <span className="text-neutral-300">
          {t('시도 {done}/{total} · 블록 {block}/{blocks}', {
            done: data.done ?? 0, total: data.total ?? 0, block: data.block?.now ?? '—', blocks: data.block?.total ?? 0 })}
        </span>
        {data.session && (
          <span className="text-xs text-neutral-500">
            {t('이번 수집: 저장 {saved} · 실패 표시 {failed} · 미룸 {deferred}', data.session)}
          </span>
        )}
        {!big && (
          <button onClick={() => window.open('/scenario-guide', 'piper-scenario-guide', 'popup,width=1280,height=900')}
            className="ml-auto text-xs text-blue-400 hover:underline">{t('안내 창 열기 ↗')}</button>
        )}
      </div>

      {data.rerecord && (
        <p className="rounded bg-amber-500/10 border border-amber-500/30 px-2 py-1 text-sm text-amber-300">
          {t('← 다시 찍기 — 리셋이 끝나면 같은 시도를 다시 찍습니다. 판을 이 시도의 배치로 되돌리세요')}
        </p>
      )}
      {data.current_failed && (
        <p className="rounded bg-red-500/10 border border-red-500/30 px-2 py-1 text-sm text-red-300">
          {t('실패로 표시됨 — 저장은 되고, 이 시도는 블록 끝에 다시 나옵니다. 학습에 넣을지는 나중에 정합니다')}
        </p>
      )}

      <div className={`grid gap-3 ${big ? 'lg:grid-cols-2' : 'md:grid-cols-2'}`}>
        {panel(t('지금'), cur, !resetting && !!cur, false)}
        {panel(data.rerecord ? t('다음 (다시 찍기)') : t('다음'), nxt, resetting, true)}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {data.current_failed ? (
          <button disabled={busy || !cur} onClick={() => send('/recording/scenario/fail', { failed: false })}
            className={`${btn} bg-neutral-700 hover:bg-neutral-600 text-neutral-200`}>{t('실패 표시 취소')}</button>
        ) : (
          <button disabled={busy || !cur || data.rerecord} onClick={() => send('/recording/scenario/fail', { failed: true })}
            title={t('지금 에피소드는 저장하되 실패로 남긴다 — 학습에 넣을지는 나중에 정한다. 버리려면 ← 재녹화')}
            className={`${btn} bg-red-700 hover:bg-red-600 text-white`}>{t('실패로 표시')}</button>
        )}
        {/* "건너뛰기" 라고 쓰지 않는다 — → 는 지금 에피소드를 **저장**한다(§8-2). 미루기는 아무것도 저장하지 않는다 */}
        <button disabled={busy || !nxt || data.rerecord} onClick={() => send('/recording/scenario/defer')}
          title={t('다음 배치가 물리적으로 안 될 때 — 그 시도를 블록 끝으로 미룬다. 아무것도 저장하지 않는다')}
          className={`${btn} bg-neutral-700 hover:bg-neutral-600 text-neutral-200`}>{t('다음 배치 미루기')}</button>
      </div>
    </div>
  )
}
