import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

/**
 * 시스템 메시지 — **어느 페이지에 있든** 같은 자리에 뜬다.
 *
 * ## 왜 만들었나
 *
 * 장치 경보는 `Layout` 안 문서 흐름에 있어서, 수집 페이지처럼 긴 화면에서는
 * 스크롤을 올려야 보였다 — 정작 그 경보가 필요한 순간에 안 보인 것이다.
 * 나머지 실패는 `window.alert` 17곳으로 흩어져 있었다.
 *
 * ## `window.alert` 를 쓰지 않는 이유 — 안전이다
 *
 * `alert`/`confirm` 은 **JS 이벤트 루프를 멈춘다.** E-stop heartbeat(500ms 주기)도
 * 같이 멈추고, 2초 타임아웃이 지나면 estopd 가 추론을 강제 종료한다.
 * 이 저장소가 `window.confirm` 으로 실제로 겪은 사고다. 모달을 안 띄우는 게
 * 취향 문제가 아니라 **팔이 멈추느냐 마느냐**의 문제다.
 *
 * ## 페이지는 "무슨 일이 있었나"만 말한다
 *
 * 색·위치·사라지는 시점은 여기서 정한다. 페이지마다 정하면 같은 실패가
 * 화면마다 다르게 보이고, 오늘처럼 어떤 화면에서는 아예 안 보인다.
 */

export type MessageLevel = 'info' | 'warn' | 'error'

export type SystemMessage = {
  /** 같은 id 는 **갱신**된다 — 같은 실패가 쌓이지 않는다. */
  id: string
  level: MessageLevel
  text: string
  /** 어디서 왔는지. 화면에 작게 붙는다 — "이게 왜 떴지"의 첫 단서다. */
  source?: string
}

type Ctx = {
  notify: (m: Omit<SystemMessage, 'id'> & { id?: string }) => string
  dismiss: (id: string) => void
  /** 그 id 의 메시지를 지운다. 장치가 복구되면 경보를 거두는 데 쓴다. */
  clear: (idPrefix: string) => void
  messages: SystemMessage[]
  /**
   * 되돌릴 수 없는 일 전에 묻는다. **`window.confirm` 을 대신한다.**
   *
   * `await confirm('…')` 로 쓴다. 논블로킹이라 heartbeat 가 계속 나간다 —
   * 이게 전부다. 삭제·리바인딩처럼 되돌릴 수 없는 것만 쓴다.
   */
  confirm: (text: string, opts?: { danger?: boolean }) => Promise<boolean>
}

const SystemMessageContext = createContext<Ctx | null>(null)

/** `info` 는 알림이라 스스로 사라지고, 경고·오류는 사람이 닫을 때까지 남는다. */
const AUTO_DISMISS_MS: Record<MessageLevel, number> = {
  info: 4000,
  warn: 0,
  error: 0,
}

let _seq = 0

type Ask = { text: string; danger: boolean; resolve: (ok: boolean) => void }

export function SystemMessageProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<SystemMessage[]>([])
  const [ask, setAsk] = useState<Ask | null>(null)
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  const dismiss = useCallback((id: string) => {
    setMessages((prev) => prev.filter((m) => m.id !== id))
    const t = timers.current.get(id)
    if (t) { clearTimeout(t); timers.current.delete(id) }
  }, [])

  const notify = useCallback((m: Omit<SystemMessage, 'id'> & { id?: string }) => {
    const id = m.id ?? `msg-${++_seq}`
    setMessages((prev) => {
      const next = prev.filter((x) => x.id !== id)
      return [...next, { ...m, id }]
    })
    const ttl = AUTO_DISMISS_MS[m.level]
    const existing = timers.current.get(id)
    if (existing) clearTimeout(existing)
    if (ttl > 0) timers.current.set(id, setTimeout(() => dismiss(id), ttl))
    return id
  }, [dismiss])

  const clear = useCallback((idPrefix: string) => {
    setMessages((prev) => prev.filter((m) => !m.id.startsWith(idPrefix)))
  }, [])

  useEffect(() => () => {
    for (const t of timers.current.values()) clearTimeout(t)
  }, [])

  const confirm = useCallback((text: string, opts?: { danger?: boolean }) =>
    new Promise<boolean>((resolve) =>
      setAsk({ text, danger: opts?.danger ?? true, resolve })), [])

  const answer = (ok: boolean) => { ask?.resolve(ok); setAsk(null) }

  const value = useMemo(() => ({ notify, dismiss, clear, messages, confirm }),
    [notify, dismiss, clear, messages, confirm])

  return (
    <SystemMessageContext.Provider value={value}>
      {children}
      {/* ⚠ **논블로킹 모달이다.** `window.confirm` 은 이벤트 루프를 멈춰
          E-stop heartbeat 를 끊는다 — 추론 중이면 2초 뒤 팔이 강제로 선다. */}
      {ask && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4"
             role="dialog" aria-modal="true"
             onKeyDown={(e) => { if (e.key === 'Escape') answer(false) }}>
          <div className="w-full max-w-md rounded-lg border border-neutral-700 bg-neutral-900 p-4 space-y-4">
            <p className="whitespace-pre-line text-sm text-neutral-100">{ask.text}</p>
            <div className="flex justify-end gap-2">
              <button onClick={() => answer(false)}
                className="px-3 py-1.5 text-sm rounded bg-neutral-700 hover:bg-neutral-600">취소</button>
              <button autoFocus onClick={() => answer(true)}
                className={`px-3 py-1.5 text-sm rounded text-white ${ask.danger
                  ? 'bg-red-600 hover:bg-red-500' : 'bg-blue-600 hover:bg-blue-500'}`}>확인</button>
            </div>
          </div>
        </div>
      )}
    </SystemMessageContext.Provider>
  )
}

export function useSystemMessage(): Ctx {
  const ctx = useContext(SystemMessageContext)
  if (!ctx) {
    // 프로바이더 밖에서 불려도 **화면을 깨뜨리지 않는다.** 메시지를 못 띄우는 것보다
    // 페이지가 통째로 죽는 게 나쁘다 — 콘솔에는 남긴다.
    return {
      notify: (m) => { console.warn('[system-message] 프로바이더 밖:', m.text); return '' },
      dismiss: () => {}, clear: () => {}, messages: [],
      // ⚠ 프로바이더가 없으면 **거절한다.** 되돌릴 수 없는 일을 물어보지도 않고
      //   진행하는 것보다 안 하는 게 낫다.
      confirm: async () => false,
    }
  }
  return ctx
}

const STYLE: Record<MessageLevel, string> = {
  info: 'border-neutral-600 bg-neutral-800 text-neutral-200',
  warn: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
  error: 'border-red-500/40 bg-red-500/10 text-red-200',
}

const ICON: Record<MessageLevel, string> = { info: 'ℹ', warn: '⚠', error: '⚠' }

/**
 * 메시지가 뜨는 자리. **`position: fixed` 다** — 문서 흐름에 두면 긴 페이지에서
 * 스크롤을 올려야 보이고, 그게 이 컴포넌트를 만든 이유다.
 */
export function SystemMessageHost() {
  const { messages, dismiss } = useSystemMessage()
  if (messages.length === 0) return null
  return (
    <div className="fixed top-16 right-4 z-50 w-[min(28rem,calc(100vw-2rem))] space-y-2"
         role="status" aria-live="polite">
      {messages.map((m) => (
        <div key={m.id}
             className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm shadow-lg ${STYLE[m.level]}`}>
          <span aria-hidden>{ICON[m.level]}</span>
          <div className="flex-1 min-w-0">
            <p className="whitespace-pre-line break-words">{m.text}</p>
            {m.source && <p className="mt-0.5 text-[10px] opacity-60">{m.source}</p>}
          </div>
          <button onClick={() => dismiss(m.id)}
                  className="text-xs opacity-60 hover:opacity-100" aria-label="닫기">✕</button>
        </div>
      ))}
    </div>
  )
}
