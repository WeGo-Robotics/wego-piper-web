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
  /** 처음 뜬 시각. 알림함에서 "언제 있었던 일인가"를 답한다. */
  at: number
}

type Ctx = {
  notify: (m: Omit<SystemMessage, 'id' | 'at'> & { id?: string }) => string
  dismiss: (id: string) => void
  /** 그 id 의 메시지를 지운다. 장치가 복구되면 경보를 거두는 데 쓴다. */
  clear: (idPrefix: string) => void
  messages: SystemMessage[]
  /** 알림함 — 토스트가 사라진 뒤에도 남고, 사람이 지울 때까지 유지된다. */
  history: SystemMessage[]
  /** 알림함에서 하나 지운다. */
  remove: (id: string) => void
  /** 알림함을 비운다. */
  clearAll: () => void
  /**
   * 되돌릴 수 없는 일 전에 묻는다. **`window.confirm` 을 대신한다.**
   *
   * `await confirm('…')` 로 쓴다. 논블로킹이라 heartbeat 가 계속 나간다 —
   * 이게 전부다. 삭제·리바인딩처럼 되돌릴 수 없는 것만 쓴다.
   */
  confirm: (text: string, opts?: { danger?: boolean }) => Promise<boolean>
}

const SystemMessageContext = createContext<Ctx | null>(null)

/**
 * 토스트는 **모든 레벨이 2초 뒤 사라진다.** 화면을 오래 가리지 않는 대신,
 * 사라진 메시지는 알림함(상태바 🔔)에 남아 사람이 지울 때까지 유지된다 —
 * "떠 있는 동안만 존재하는 경보"가 아니라 "잠깐 보이고 함에 쌓이는 알림"이다.
 */
const AUTO_DISMISS_MS = 2000

let _seq = 0

type Ask = { text: string; danger: boolean; resolve: (ok: boolean) => void }

export function SystemMessageProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<SystemMessage[]>([])
  const [history, setHistory] = useState<SystemMessage[]>([])
  const [ask, setAsk] = useState<Ask | null>(null)
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  const dismiss = useCallback((id: string) => {
    setMessages((prev) => prev.filter((m) => m.id !== id))
    const t = timers.current.get(id)
    if (t) { clearTimeout(t); timers.current.delete(id) }
  }, [])

  const notify = useCallback((m: Omit<SystemMessage, 'id' | 'at'> & { id?: string }) => {
    const id = m.id ?? `msg-${++_seq}`
    const now = Date.now()
    setMessages((prev) => {
      const next = prev.filter((x) => x.id !== id)
      return [...next, { ...m, id, at: now }]
    })
    // 알림함에도 넣는다. 같은 id 갱신이면 **처음 뜬 시각을 지킨다** — 장치 경보처럼
    // 재연결마다 다시 흘러드는 메시지가 매번 "방금"으로 둔갑하면 시각을 못 믿는다.
    setHistory((prev) => {
      const old = prev.find((x) => x.id === id)
      const next = prev.filter((x) => x.id !== id)
      return [{ ...m, id, at: old?.at ?? now }, ...next]
    })
    const existing = timers.current.get(id)
    if (existing) clearTimeout(existing)
    timers.current.set(id, setTimeout(() => dismiss(id), AUTO_DISMISS_MS))
    return id
  }, [dismiss])

  // 경보가 **거둬진** 것이다(장치 복구) — 알림함에서도 지운다. 복구된 장치의
  // 경보가 함에 남으면 재연결 때마다 같은 경보가 유령처럼 쌓인다.
  const clear = useCallback((idPrefix: string) => {
    setMessages((prev) => prev.filter((m) => !m.id.startsWith(idPrefix)))
    setHistory((prev) => prev.filter((m) => !m.id.startsWith(idPrefix)))
  }, [])

  const remove = useCallback((id: string) => {
    setHistory((prev) => prev.filter((m) => m.id !== id))
  }, [])

  const clearAll = useCallback(() => setHistory([]), [])

  useEffect(() => () => {
    for (const t of timers.current.values()) clearTimeout(t)
  }, [])

  const confirm = useCallback((text: string, opts?: { danger?: boolean }) =>
    new Promise<boolean>((resolve) =>
      setAsk({ text, danger: opts?.danger ?? true, resolve })), [])

  const answer = (ok: boolean) => { ask?.resolve(ok); setAsk(null) }

  const value = useMemo(
    () => ({ notify, dismiss, clear, messages, history, remove, clearAll, confirm }),
    [notify, dismiss, clear, messages, history, remove, clearAll, confirm])

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
      history: [], remove: () => {}, clearAll: () => {},
      // ⚠ 프로바이더가 없으면 **거절한다.** 되돌릴 수 없는 일을 물어보지도 않고
      //   진행하는 것보다 안 하는 게 낫다.
      confirm: async () => false,
    }
  }
  return ctx
}

// ⚠ **불투명이어야 한다.** 반투명(`/10`)은 뒤 화면과 섞여서 긴 로그 위에서는
//   글자가 안 읽혔다 — 배경색은 전부 진한 단색이다.
const STYLE: Record<MessageLevel, string> = {
  info: 'border-neutral-600 bg-neutral-800 text-neutral-100',
  warn: 'border-amber-600 bg-amber-950 text-amber-100',
  error: 'border-red-600 bg-red-950 text-red-100',
}

const ICON: Record<MessageLevel, string> = { info: 'ℹ', warn: '⚠', error: '⚠' }

/**
 * 메시지가 뜨는 자리. **`position: fixed` 다** — 문서 흐름에 두면 긴 페이지에서
 * 스크롤을 올려야 보이고, 그게 이 컴포넌트를 만든 이유다.
 *
 * 토스트는 2초 뒤 사라진다 — 놓쳤어도 상태바 🔔 알림함에 남아 있다.
 */
export function SystemMessageHost() {
  const { messages, dismiss } = useSystemMessage()
  if (messages.length === 0) return null
  return (
    // ⚠ `top` 은 **상단 상태바 높이(h-12)** 아래여야 한다. 상태바를 덮으면
    //   무엇이 도는지가 경보에 가려진다. 오른쪽 정렬이라 사이드바는 안 덮는다.
    <div className="fixed top-14 right-4 z-50 w-[min(28rem,calc(100vw-2rem))] space-y-2"
         role="status" aria-live="polite">
      {messages.map((m) => (
        <div key={m.id}
             className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm shadow-lg ${STYLE[m.level]}`}>
          <span aria-hidden>{ICON[m.level]}</span>
          <div className="flex-1 min-w-0">
            <p className="whitespace-pre-line break-words">{m.text}</p>
            {m.source && <p className="mt-0.5 text-[10px] opacity-60">{m.source}</p>}
          </div>
          {/* ✕ 아이콘은 작아서 못 찾고 못 눌렀다 — 글자 버튼이 과녁도 크다 */}
          <button onClick={() => dismiss(m.id)}
                  className="shrink-0 rounded border border-current/40 px-2 py-0.5 text-xs
                             opacity-80 hover:opacity-100">닫기</button>
        </div>
      ))}
    </div>
  )
}

function timeOf(at: number): string {
  const d = new Date(at)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/**
 * 상태바에 앉는 알림함 — 🔔 에 쌓인 개수가 붙고, 누르면 목록이 내려온다.
 *
 * 토스트가 2초 만에 사라지는 대신 여기가 기억을 맡는다. 목록에서는
 * 하나씩도, 한꺼번에도 지울 수 있다. 장치 경보는 장치가 복구되면
 * (`clear`) 여기서도 걷힌다 — 사람이 지우는 것만 남는 게 아니다.
 */
export function NotificationBell() {
  const { history, remove, clearAll } = useSystemMessage()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  // 바깥 클릭·Escape 로 닫는다 — 드롭다운의 기본 예의다
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const worst: MessageLevel = history.some((m) => m.level === 'error') ? 'error'
    : history.some((m) => m.level === 'warn') ? 'warn' : 'info'
  const BADGE: Record<MessageLevel, string> = {
    info: 'bg-neutral-600 text-neutral-100',
    warn: 'bg-amber-500 text-black',
    error: 'bg-red-600 text-white',
  }

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button onClick={() => setOpen((o) => !o)}
              className="relative flex items-center px-1 text-base leading-none
                         opacity-80 hover:opacity-100"
              aria-label={`알림 ${history.length}개`} aria-expanded={open}>
        <span aria-hidden>🔔</span>
        {history.length > 0 && (
          <span className={`absolute -top-1.5 -right-2 min-w-[1.1rem] rounded-full px-1
                            text-center text-[10px] font-bold leading-4 tabular-nums
                            ${BADGE[worst]}`} aria-hidden>
            {history.length}
          </span>
        )}
      </button>

      {open && (
        // ⚠ 여기도 불투명 단색 — 토스트와 같은 이유다
        <div className="absolute right-0 top-9 z-50 w-[min(26rem,calc(100vw-2rem))]
                        overflow-hidden rounded-lg border border-neutral-700 bg-neutral-900 shadow-xl">
          <div className="flex items-center justify-between border-b border-neutral-800 px-3 py-2">
            <span className="text-xs text-neutral-400">알림 {history.length}개</span>
            {history.length > 0 && (
              <button onClick={clearAll}
                      className="rounded border border-neutral-600 px-2 py-0.5 text-xs
                                 text-neutral-300 hover:bg-neutral-800">모두 지우기</button>
            )}
          </div>
          {history.length === 0 ? (
            <p className="px-3 py-6 text-center text-sm text-neutral-500">알림이 없습니다</p>
          ) : (
            <ul className="max-h-[60vh] overflow-y-auto">
              {history.map((m) => (
                <li key={m.id}
                    className="flex items-start gap-2 border-b border-neutral-800 px-3 py-2
                               text-sm last:border-b-0">
                  <span aria-hidden className={
                    m.level === 'error' ? 'text-red-400'
                      : m.level === 'warn' ? 'text-amber-400' : 'text-neutral-400'
                  }>{ICON[m.level]}</span>
                  <div className="flex-1 min-w-0">
                    <p className="whitespace-pre-line break-words text-neutral-100">{m.text}</p>
                    <p className="mt-0.5 text-[10px] text-neutral-500">
                      {timeOf(m.at)}{m.source ? ` · ${m.source}` : ''}
                    </p>
                  </div>
                  <button onClick={() => remove(m.id)}
                          className="shrink-0 rounded border border-neutral-600 px-2 py-0.5
                                     text-xs text-neutral-300 hover:bg-neutral-800">지우기</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
