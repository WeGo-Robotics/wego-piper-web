import { useCallback, useEffect, useRef, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'
import { useT } from '../i18n'

/**
 * 양팔 리더 조종 — 녹화 없이 두 쌍을 함께 (feature/bimanual.md §8).
 *
 * 쌍은 **고르지 않는다** — 좌/우 등록이 정한다. 고르게 하면 왼팔을 오른쪽 리더로 끄는
 * 실수가 생긴다(단팔 릴레이가 같은 쪽 리더만 받는 것과 같은 규칙). 모드는 관절 복제
 * 하나 — robotd 안전 필터를 그대로 탄다.
 */

type RelayStatus = {
  running: boolean; holding: boolean; leader: string | null; follower: string | null
  sent: number; stale: boolean; blocked: string
}
type SidePair = {
  side: 'left' | 'right'; leader: string | null; follower: string | null
  /** 이 쪽을 못 잇는 이유 — 사람이 할 일이 적혀 온다 */
  problem: string
  relay: RelayStatus | null
}

export default function BimanualRelayPanel({ onClose }: { onClose: () => void }) {
  const t = useT()
  const { notify } = useSystemMessage()
  const [pairs, setPairs] = useState<SidePair[]>([])
  const [busy, setBusy] = useState(false)

  const read = useCallback(() => {
    api.get<{ pairs: SidePair[] }>('/robots/relay/pairs')
      .then((r) => setPairs(r.pairs))
      .catch(() => {})
  }, [])
  useEffect(() => {
    read()
    const id = setInterval(read, 700)
    return () => clearInterval(id)
  }, [read])

  const running = pairs.filter((p) => p.relay?.running && p.follower).map((p) => p.follower as string)
  const runningRef = useRef<string[]>([])
  runningRef.current = running
  // ⚠ 닫거나 화면을 떠나면 **반드시 끝낸다** — 단팔 패널과 같은 규칙. 남기면 리더를 놓아도
  //   팔로워가 계속 따라가고, 추론·녹화는 "수동 조작 중"으로 막힌다.
  //   팔로워를 하나씩 준다 — 비우면 이 패널 것이 아닌 릴레이(조종 창 등)까지 닫힌다.
  useEffect(() => () => {
    for (const f of runningRef.current) api.post('/robots/relay/stop', { follower: f }).catch(() => {})
  }, [])

  const fail = (e: unknown, fallback: string) =>
    notify({ level: 'error', source: t('양팔 조종'), text: e instanceof Error ? e.message : fallback })

  const start = async () => {
    setBusy(true)
    try {
      await api.post('/robots/relay/pairs/start', { mode: 'joint' }, { timeoutMs: 30000 })
      read()
    } catch (e) { fail(e, t('양팔 조종을 시작하지 못했습니다')) }
    finally { setBusy(false) }
  }

  const stop = async (followers: string[]) => {
    setBusy(true)
    try {
      await Promise.all(followers.map((f) => api.post('/robots/relay/stop', { follower: f })))
      read()
    } catch (e) { fail(e, t('양팔 조종을 멈추지 못했습니다')) }
    finally { setBusy(false) }
  }

  const ready = pairs.length === 2 && pairs.every((p) => !p.problem)
  const allRunning = pairs.length === 2 && pairs.every((p) => p.relay?.running)

  const stateOf = (p: SidePair): { text: string; cls: string } => {
    if (p.problem) return { text: p.problem, cls: 'text-amber-300' }
    const r = p.relay
    if (!r) return { text: t('준비됨'), cls: 'text-neutral-400' }
    // "멈췄다" 가 아니다 — 쌍은 그대로고 전송만 쉰다. 리더가 다시 발행하면 이어진다
    if (r.stale) return { text: t('리더 상태가 낡았습니다 — 전송 보류'), cls: 'text-amber-400' }
    if (r.blocked) return { text: t('멈춤: {relayBlocked}', { relayBlocked: r.blocked }), cls: 'text-amber-300' }
    return { text: t('● 따라가는 중 · {sent}회', { sent: r.sent }), cls: 'text-green-400' }
  }

  return (
    <div className="rounded border border-purple-500/40 bg-purple-500/5 p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold text-neutral-200">
          {t('양팔 조종')}
          <span className="ml-2 font-normal text-neutral-500">{t('관절 복제')}</span>
        </span>
        <button onClick={onClose} className="text-xs text-neutral-400 hover:text-white">{t('닫기')}</button>
      </div>

      {pairs.map((p) => {
        const st = stateOf(p)
        return (
          <div key={p.side} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <span className="w-12 shrink-0 text-neutral-400">{p.side === 'left' ? t('왼팔') : t('오른팔')}</span>
            <span className="shrink-0 font-mono text-neutral-200">{p.leader ?? '—'} → {p.follower ?? '—'}</span>
            <span className={`min-w-0 flex-1 ${st.cls}`}>{st.text}</span>
            {p.relay?.running && p.follower && (
              <button onClick={() => void stop([p.follower as string])} disabled={busy}
                className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white disabled:opacity-50">
                {t('이 쪽만 정지')}
              </button>
            )}
          </div>
        )
      })}

      <div className="flex gap-2">
        <button onClick={() => void start()} disabled={busy || !ready || allRunning}
          className="px-3 py-1.5 text-xs rounded bg-purple-700 hover:bg-purple-600 text-white disabled:opacity-40">
          {busy ? '…' : t('양팔 시작')}
        </button>
        <button onClick={() => void stop(running)} disabled={busy || running.length === 0}
          className="px-3 py-1.5 text-xs rounded bg-red-600 hover:bg-red-500 text-white disabled:opacity-40">
          {t('정지')}
        </button>
      </div>
      <p className="text-[10px] leading-relaxed text-neutral-500">
        {t('쌍은 좌/우 등록이 정합니다 — 같은 쪽 리더가 같은 쪽 팔로워를 끕니다. 시작하면 팔로워가 리더 자세로 움직이니 두 리더를 팔로워와 비슷한 자세에 두고 시작하세요. 도는 동안 녹화·추론은 막히고, 닫거나 페이지를 떠나면 둘 다 멈춥니다.')}
      </p>
    </div>
  )
}
